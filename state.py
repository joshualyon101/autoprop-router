from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from models import AccountConfig, AccountRuntime, DrawdownType
from persistence import Store
from crosstrade import CrossTradeClient


_BALANCE_KEYS = (
    "cashBalance", "cash_balance", "balance", "accountBalance", "closedBalance",
    "realizedBalance", "amount"
)
_NETLIQ_KEYS = ("netLiq", "net_liq", "netLiquidation", "netLiquidationValue", "equity")
_NAME_KEYS = ("accountName", "account", "name")
_ACCOUNT_ID_KEYS = ("accountId", "account_id", "id")


def _record_account_id(obj: dict[str, Any]) -> int | None:
    for k in _ACCOUNT_ID_KEYS:
        v = obj.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def _record_account_name(obj: dict[str, Any]) -> str | None:
    for k in _NAME_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

ET = ZoneInfo("America/New_York")


def _hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":", 1)
    return int(h), int(m)


def _trading_day_key(now_utc: datetime, start_et: str) -> str:
    local = now_utc.astimezone(ET)
    h, m = _hhmm(start_et)
    boundary = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if local < boundary:
        local = local - timedelta(days=1)
    return local.date().isoformat()


def _first_number(obj: Any, keys: tuple[str, ...]) -> float | None:
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, dict):
                # Tradovate/CrossTrade sometimes nests values in balance objects.
                for vk in ("value", "amount", "balance"):
                    nv = v.get(vk)
                    if isinstance(nv, (int, float)):
                        return float(nv)
        for v in obj.values():
            found = _first_number(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _first_number(v, keys)
            if found is not None:
                return found
    return None


def _find_list(obj: Any, keys: tuple[str, ...]) -> list[dict]:
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        for v in obj.values():
            found = _find_list(v, keys)
            if found:
                return found
    return []


def _find_account_record(
    snapshot: Any,
    account_name: str | None,
    account_id: int | None = None,
) -> dict[str, Any] | None:
    target = (account_name or "").lower()

    def walk(obj: Any) -> dict[str, Any] | None:
        if isinstance(obj, dict):
            # Unique numeric ID wins. This avoids ambiguity when display names repeat.
            if account_id is not None and _record_account_id(obj) == account_id:
                return obj
            if account_id is None and target:
                n = _record_account_name(obj)
                if n and n.lower() == target:
                    return obj
            for v in obj.values():
                r = walk(v)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = walk(v)
                if r is not None:
                    return r
        return None

    return walk(snapshot)


class AccountStateCache:
    def __init__(self, accounts: list[AccountConfig], store: Store):
        self.accounts = {a.id: a for a in accounts}
        self.store = store
        self._state: dict[str, AccountRuntime] = {}
        self._lock = asyncio.Lock()

    async def snapshot(self) -> dict[str, AccountRuntime]:
        async with self._lock:
            return copy.deepcopy(self._state)

    async def update_from_snapshot(self, raw_snapshot: Any):
        now = datetime.now(timezone.utc)
        updates: dict[str, AccountRuntime] = {}

        for a in self.accounts.values():
            if a.crosstrade_account_id is None and (
                not a.account_name or a.account_name.startswith("REPLACE_")
            ):
                continue
            rec = _find_account_record(
                raw_snapshot, a.account_name, a.crosstrade_account_id
            )
            if rec is None:
                continue

            balance = _first_number(rec, _BALANCE_KEYS)
            net_liq = _first_number(rec, _NETLIQ_KEYS)
            positions = _find_list(rec, ("positions", "openPositions"))
            working_orders = _find_list(rec, ("orders", "workingOrders", "activeOrders"))
            working_orders = [
                o for o in working_orders
                if str(o.get("orderStatus", o.get("status", "Working"))).lower()
                not in {"filled", "cancelled", "canceled", "rejected", "expired"}
            ]

            runtime = self._apply_prop_state(a, balance, net_liq, positions, working_orders, rec, now)
            updates[a.id] = runtime

        async with self._lock:
            self._state.update(updates)

    def _apply_prop_state(
        self,
        a: AccountConfig,
        balance: float | None,
        net_liq: float | None,
        positions: list[dict],
        orders: list[dict],
        raw: dict,
        now: datetime,
    ) -> AccountRuntime:
        persisted = self.store.get_prop_state(a.id) or {}

        initial_floor = a.starting_balance - a.max_loss
        floor = persisted.get("mll_floor")
        if floor is None:
            floor = a.bootstrap_mll_floor if a.bootstrap_mll_floor is not None else initial_floor

        peak_eod = persisted.get("peak_eod_balance")
        if peak_eod is None:
            peak_eod = a.bootstrap_peak_eod_balance or a.starting_balance

        live_high = persisted.get("live_high_water")
        if live_high is None:
            live_high = a.bootstrap_live_high_water or a.starting_balance

        locked = bool(persisted.get("mll_locked", False))

        effective_balance = balance if balance is not None else net_liq
        if effective_balance is not None:
            live_mark = max(effective_balance, net_liq or effective_balance)
            live_high = max(live_high or live_mark, live_mark)

        # Static is always exact.
        if a.drawdown_type == DrawdownType.STATIC:
            floor = initial_floor
            locked = True

        # Live-trailing rules can update continuously using net-liq/high-water.
        elif a.drawdown_type in {DrawdownType.LIVE_TRAIL_TO_LOCK, DrawdownType.LIVE_TRAIL_NO_PREPASS_LOCK}:
            candidate = (live_high or a.starting_balance) - a.max_loss
            if a.drawdown_type == DrawdownType.LIVE_TRAIL_TO_LOCK:
                candidate = min(a.lock_level, candidate)
            floor = max(float(floor), candidate)
            if a.drawdown_type == DrawdownType.LIVE_TRAIL_TO_LOCK and floor >= a.lock_level - 0.01:
                floor = a.lock_level
                locked = True

        # EOD trailing is intentionally advanced only by mark_eod().
        elif a.drawdown_type in {DrawdownType.EOD_TRAIL_TO_LOCK, DrawdownType.EOD_TRAIL_NO_PREPASS_LOCK}:
            candidate = (peak_eod or a.starting_balance) - a.max_loss
            if a.drawdown_type == DrawdownType.EOD_TRAIL_TO_LOCK:
                candidate = min(a.lock_level, candidate)
            floor = max(float(floor), candidate)
            if a.drawdown_type == DrawdownType.EOD_TRAIL_TO_LOCK and floor >= a.lock_level - 0.01:
                floor = a.lock_level
                locked = True

        # Track realized P&L on the prop trading day (not calendar midnight).
        day_key = _trading_day_key(now, a.trading_day_start_et)
        old_day = persisted.get("day_key")
        day_start = persisted.get("day_start_balance")
        largest_day = float(persisted.get("largest_winning_day") or 0.0)
        if effective_balance is not None:
            if not old_day:
                day_start = effective_balance
                old_day = day_key
            elif old_day != day_key:
                if day_start is not None:
                    largest_day = max(largest_day, effective_balance - float(day_start))
                day_start = effective_balance
                old_day = day_key

        self.store.upsert_prop_state(
            a.id, mll_floor=floor, peak_eod_balance=peak_eod, live_high_water=live_high,
            mll_locked=locked, day_start_balance=day_start, day_key=old_day,
            largest_winning_day=largest_day
        )

        realized_today = 0.0
        if effective_balance is not None and day_start is not None:
            realized_today = effective_balance - float(day_start)
        current_winning_day = max(0.0, realized_today)
        largest_live = max(largest_day, current_winning_day)

        cushion = None if effective_balance is None else max(0.0, effective_balance - float(floor))
        return AccountRuntime(
            account_id=a.id,
            account_name=_record_account_name(raw) or a.account_name,
            balance=balance,
            net_liq=net_liq,
            mll_floor=float(floor),
            cushion=cushion,
            peak_eod_balance=peak_eod,
            live_high_water=live_high,
            mll_locked=locked,
            positions=positions,
            working_orders=orders,
            source_updated_at=now,
            cache_updated_at=now,
            day_start_balance=day_start,
            realized_today=realized_today,
            largest_winning_day=largest_live,
            trading_day_key=old_day,
            raw=raw,
        )

    async def mark_eod(self, account_id: str, eod_balance: float):
        """Advance an EOD trailing floor from an authoritative completed-EOD balance."""
        a = self.accounts[account_id]
        persisted = self.store.get_prop_state(account_id) or {}
        peak = max(float(persisted.get("peak_eod_balance") or a.starting_balance), eod_balance)
        floor = float(persisted.get("mll_floor") or (a.starting_balance - a.max_loss))
        locked = bool(persisted.get("mll_locked", False))

        if a.drawdown_type in {DrawdownType.EOD_TRAIL_TO_LOCK, DrawdownType.EOD_TRAIL_NO_PREPASS_LOCK}:
            candidate = peak - a.max_loss
            if a.drawdown_type == DrawdownType.EOD_TRAIL_TO_LOCK:
                candidate = min(a.lock_level, candidate)
            floor = max(floor, candidate)
            if a.drawdown_type == DrawdownType.EOD_TRAIL_TO_LOCK and floor >= a.lock_level - 0.01:
                floor, locked = a.lock_level, True

        self.store.upsert_prop_state(
            account_id, peak_eod_balance=peak, mll_floor=floor, mll_locked=locked
        )


    async def auto_mark_eod(self, now_utc: datetime | None = None):
        """
        Advance EOD-trailing floors once per configured trading date.

        This runs in the background poller, never inside the webhook critical path.
        It uses the latest CLOSED balance after each account's configured EOD snapshot time.
        """
        now_utc = now_utc or datetime.now(timezone.utc)
        local = now_utc.astimezone(ET)
        states = await self.snapshot()

        for a in self.accounts.values():
            if not a.eod_snapshot_time_et:
                continue
            if a.drawdown_type not in {DrawdownType.EOD_TRAIL_TO_LOCK, DrawdownType.EOD_TRAIL_NO_PREPASS_LOCK}:
                continue

            h, m = _hhmm(a.eod_snapshot_time_et)
            cutoff = local.replace(hour=h, minute=m, second=0, microsecond=0)
            if local < cutoff:
                continue

            # The EOD mark corresponds to the calendar trading date that just closed.
            mark_date = local.date().isoformat()
            if self.store.has_eod_mark(a.id, mark_date):
                continue

            state = states.get(a.id)
            if state is None:
                continue
            bal = state.balance if state.balance is not None else state.net_liq
            if bal is None:
                continue

            await self.mark_eod(a.id, float(bal))
            self.store.record_eod_mark(a.id, mark_date, float(bal))


class StatePoller:
    def __init__(self, client: CrossTradeClient, cache: AccountStateCache, interval: float):
        self.client = client
        self.cache = cache
        self.interval = interval
        self.task: asyncio.Task | None = None
        self.last_error: str | None = None
        self.last_success: datetime | None = None

    async def refresh_once(self):
        raw = await self.client.get_accounts_snapshot()
        await self.cache.update_from_snapshot(raw)
        await self.cache.auto_mark_eod()
        self.last_success = datetime.now(timezone.utc)
        self.last_error = None

    async def _loop(self):
        while True:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
            await asyncio.sleep(self.interval)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="autoprop-state-poller")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
