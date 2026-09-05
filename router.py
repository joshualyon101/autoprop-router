from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from config import Settings
from crosstrade import CrossTradeClient
from models import AccountConfig, AccountRuntime, RouteDecision, RouteResponse, TradeSignal, SignalEvent
from persistence import Store
from risk import contract_risk_from_geometry, size_trade, consistency_adjust
from state import AccountStateCache


class AutoPropRouter:
    def __init__(
        self,
        settings: Settings,
        accounts: list[AccountConfig],
        client: CrossTradeClient,
        cache: AccountStateCache,
        store: Store,
    ):
        self.settings = settings
        self.accounts = accounts
        self.client = client
        self.cache = cache
        self.store = store

    def _contract_risk(self, signal: TradeSignal) -> float:
        if signal.contract_risk_dollars is not None:
            return float(signal.contract_risk_dollars)
        assert signal.entry is not None and signal.stop is not None
        return contract_risk_from_geometry(
            signal.entry, signal.stop,
            self.settings.mnq_point_value,
            self.settings.modeled_round_trip_cost
        )

    def _execution_symbol(self, signal: TradeSignal) -> str:
        # CrossTrade accepts TradingView continuous symbols on Tradovate and pins
        # the concrete contract server-side, so this removes manual rollover work.
        return signal.execution_symbol or self.settings.default_execution_symbol or signal.instrument

    def _atm_for_decision(self, signal: TradeSignal, d: RouteDecision):
        if signal.entry is None or signal.stop is None or d.routed_tp1 is None:
            return None
        tick = self.settings.mnq_tick_size
        stop_ticks = max(1, round(abs(signal.entry - signal.stop) / tick))
        tp1_ticks = max(1, round(abs(d.routed_tp1 - signal.entry) / tick))

        # One tier when only 1 contract or no second target.
        if d.quantity < 2 or d.routed_tp2 is None:
            return {
                "atm_targets": str(tp1_ticks),
                "atm_stops": str(stop_ticks),
                "atm_qtys": str(d.quantity),
                "atm_breakeven": None,
                "atm_breakeven_offset": None,
            }

        tp2_ticks = max(1, round(abs(d.routed_tp2 - signal.entry) / tick))
        runner = d.quantity // 2 if d.quantity >= signal.min_runner_qty else 0
        first = d.quantity - runner
        if runner <= 0:
            return {
                "atm_targets": str(tp1_ticks),
                "atm_stops": str(stop_ticks),
                "atm_qtys": str(d.quantity),
                "atm_breakeven": None,
                "atm_breakeven_offset": None,
            }

        return {
            "atm_targets": f"{tp1_ticks},{tp2_ticks}",
            "atm_stops": str(stop_ticks),
            "atm_qtys": f"{first},{runner}",
            "atm_breakeven": 1 if signal.breakeven_after_tp1 else None,
            "atm_breakeven_offset": (
                str(signal.breakeven_offset_ticks)
                if signal.breakeven_after_tp1 else None
            ),
        }

    async def route(self, signal: TradeSignal) -> RouteResponse:
        t0 = time.perf_counter()

        if not self.store.claim_signal(signal.trade_id, signal.model_dump(mode="json")):
            return RouteResponse(
                trade_id=signal.trade_id,
                mode=self.settings.execution_mode,
                router_processing_ms=(time.perf_counter() - t0) * 1000,
                decisions=[RouteDecision(
                    account_id="*", account_name="*", eligible=False,
                    reason="duplicate trade_id blocked"
                )]
            )

        states = await self.cache.snapshot()
        configs = await self.cache.configs_snapshot()
        decisions: list[RouteDecision] = []
        executable: list[tuple[AccountConfig, RouteDecision]] = []
        now = datetime.now(timezone.utc)

        if signal.event != SignalEvent.ENTRY:
            # Full EXIT/FLATTEN uses CrossTrade's Tradovate flatten endpoint, which
            # closes the position and clears the account/instrument order structure.
            if signal.event not in {SignalEvent.EXIT, SignalEvent.FLATTEN}:
                for a in configs:
                    decisions.append(RouteDecision(
                        account_id=a.id, account_name=a.account_name,
                        eligible=False, reason=f"{signal.event.value} not enabled in v0.3"
                    ))
                return self._finish(signal, t0, decisions)

            symbol = self._execution_symbol(signal)
            targets = []
            for a in configs:
                state = states.get(a.id)
                name = state.account_name if state else a.account_name
                if not a.rules_verified or not a.risk_ready:
                    decisions.append(RouteDecision(
                        account_id=a.id, account_name=name, eligible=False,
                        reason="not risk-ready"
                    ))
                    continue
                if self.settings.canary_enabled and a.id != self.settings.canary_account_id:
                    decisions.append(RouteDecision(
                        account_id=a.id, account_name=name, eligible=False,
                        reason="canary mode: not selected account"
                    ))
                    continue
                d = RouteDecision(
                    account_id=a.id, account_name=name, eligible=True,
                    reason=f"{signal.event.value} full account/instrument flatten"
                )
                decisions.append(d)
                targets.append((a, d))

            if not self.settings.live_enabled:
                return self._finish(signal, t0, decisions)

            async def flatten_one(a, d):
                try:
                    d.execution_result = await self.client.flatten_position(
                        account=d.account_name, instrument=symbol
                    )
                except Exception as exc:
                    d.eligible = False
                    d.reason = f"CrossTrade flatten rejected/error: {exc!r}"

            await asyncio.gather(*(flatten_one(a, d) for a, d in targets))
            return self._finish(signal, t0, decisions)

        contract_risk = self._contract_risk(signal)

        for a in configs:
            s = states.get(a.id)

            if not a.enabled:
                decisions.append(RouteDecision(
                    account_id=a.id, account_name=a.account_name, eligible=False,
                    reason="account disabled"
                ))
                continue
            if not a.rules_verified:
                decisions.append(RouteDecision(
                    account_id=a.id, account_name=a.account_name, eligible=False,
                    reason="unclassified account: no verified rule profile"
                ))
                continue
            if not a.risk_ready:
                decisions.append(RouteDecision(
                    account_id=a.id, account_name=a.account_name, eligible=False,
                    reason="auto-discovered existing account needs one-time MLL bootstrap"
                ))
                continue
            if self.settings.canary_enabled and a.id != self.settings.canary_account_id:
                decisions.append(RouteDecision(
                    account_id=a.id, account_name=a.account_name, eligible=False,
                    reason="canary mode: not selected account"
                ))
                continue
            if s is None or s.source_updated_at is None:
                decisions.append(RouteDecision(
                    account_id=a.id, account_name=a.account_name, eligible=False,
                    reason="no hot account snapshot"
                ))
                continue

            age = (now - s.source_updated_at).total_seconds()
            age_ms = age * 1000
            if age > self.settings.max_state_age_seconds:
                decisions.append(RouteDecision(
                    account_id=a.id, account_name=a.account_name, eligible=False,
                    reason=f"stale state ({age:.1f}s)", cache_age_ms=age_ms,
                    cushion=s.cushion, mll_floor=s.mll_floor
                ))
                continue
            if s.balance is None and s.net_liq is None:
                decisions.append(RouteDecision(
                    account_id=a.id, account_name=a.account_name, eligible=False,
                    reason="snapshot has no usable balance", cache_age_ms=age_ms
                ))
                continue
            if not s.is_flat:
                decisions.append(RouteDecision(
                    account_id=a.id, account_name=a.account_name, eligible=False,
                    reason="local hot-cache guard: account not flat", cache_age_ms=age_ms
                ))
                continue
            if s.has_working_orders:
                decisions.append(RouteDecision(
                    account_id=a.id, account_name=a.account_name, eligible=False,
                    reason="local hot-cache guard: working orders exist", cache_age_ms=age_ms
                ))
                continue

            rr = size_trade(a, s, contract_risk)
            routed_qty, routed_tp1, routed_tp2, consistency_room = consistency_adjust(
                a=a, s=s, pre_qty=rr.quantity,
                entry=signal.entry, tp1=signal.tp1, tp2=signal.tp2,
                point_value=self.settings.mnq_point_value,
                tick_size=self.settings.mnq_tick_size,
                min_runner_qty=signal.min_runner_qty,
                min_profit_room=self.settings.consistency_min_profit_room,
            )
            if self.settings.canary_enabled:
                routed_qty = min(routed_qty, max(1, self.settings.canary_max_qty))
            elif self.settings.live_enabled and self.settings.live_max_qty_per_account > 0:
                routed_qty = min(routed_qty, self.settings.live_max_qty_per_account)

            if routed_qty <= 0:
                decisions.append(RouteDecision(
                    account_id=a.id, account_name=a.account_name, eligible=False,
                    reason=rr.reason + ("; consistency room exhausted" if consistency_room is not None else "") + " -> 0 contracts",
                    risk_budget=rr.budget, contract_risk=rr.contract_risk,
                    cushion=s.cushion, mll_floor=s.mll_floor, cache_age_ms=age_ms
                ))
                continue

            d = RouteDecision(
                account_id=a.id, account_name=s.account_name, eligible=True,
                reason=rr.reason, quantity=routed_qty,
                risk_budget=rr.budget, contract_risk=rr.contract_risk,
                cushion=s.cushion, mll_floor=s.mll_floor, cache_age_ms=age_ms,
                routed_tp1=routed_tp1, routed_tp2=routed_tp2,
                consistency_room=consistency_room
            )
            decisions.append(d)
            executable.append((a, d))

        # Shadow mode ends here. No broker mutation.
        if not self.settings.live_enabled:
            return self._finish(signal, t0, decisions)

        symbol = self._execution_symbol(signal)
        action = "buy" if signal.direction in {"LONG", "BUY"} else "sell"

        async def send(a: AccountConfig, d: RouteDecision):
            try:
                atm = self._atm_for_decision(signal, d) if self.settings.use_native_atm else None
                if self.settings.execution_require_bracket and atm is None:
                    d.eligible = False
                    d.reason = "live blocked: valid stop/target bracket required"
                    return

                kwargs = dict(
                    account=d.account_name,
                    instrument=symbol,
                    action=action,
                    qty=d.quantity,
                    order_id=f"AP-{signal.trade_id}-{a.id}"[:64],
                    require_market_position="flat" if self.settings.use_crosstrade_position_gate else None,
                    max_positions=1 if self.settings.use_crosstrade_max_positions_gate else None,
                )
                if atm is not None:
                    kwargs.update(atm)
                else:
                    kwargs.update(
                        take_profit=d.routed_tp1,
                        stop_loss=signal.stop,
                    )

                d.execution_result = await self.client.place_order(**kwargs)
            except Exception as exc:
                d.eligible = False
                d.reason = f"CrossTrade rejected/error: {exc!r}"

        # Critical latency choice: account orders fan out concurrently, never sequentially.
        await asyncio.gather(*(send(a, d) for a, d in executable))
        return self._finish(signal, t0, decisions)

    def _finish(self, signal: TradeSignal, t0: float, decisions: list[RouteDecision]) -> RouteResponse:
        r = RouteResponse(
            trade_id=signal.trade_id,
            mode=(self.settings.execution_mode if self.settings.live_enabled else "shadow"),
            router_processing_ms=(time.perf_counter() - t0) * 1000,
            decisions=decisions,
        )
        self.store.log_route(signal.trade_id, r.model_dump(mode="json"))
        return r
