from __future__ import annotations

import asyncio
import hashlib
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from allocation import allocate, allocate_asw, AllocationBlocked
from crosstrade import (AmbiguousMutation, CrossTradeClient, CrossTradeError,
                        EntryAdmissionClosed, InstrumentContractError, RateLimitExceeded,
                        normalize_tradovate_symbol)
from events import ParsedEvent
from execution import (AcceptedOrderError, Executor, NormalizationMutationUnconfirmed,
                       NormalizationSuperseded, ProtectionFailure,
                       verify_single_bracket)
from management import core_on_market_pulse, silver_lock_stop
from models import (AccountRule, AccountState, ActiveTrade, Allocation, AswPending,
                    EntryAttempt)
from org_reentry import prove_prior_org_stop
from state import StateUnverified, ny_date
from state_refresh import refresh_account_state, durable_fills
from store import Store


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get('data', payload)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ('orders','items','data'):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
        return [data]
    return []


def _order_id(row: dict[str, Any]) -> str:
    return str(row.get('id') or row.get('orderId') or row.get('order_id') or '')


class LiveRouter:
    def __init__(self, settings, accounts: list[AccountRule], store: Store):
        self.settings = settings
        self.accounts = accounts
        self.store = store
        self.entry_wave_active = asyncio.Event()
        self.silver_management_wakeup = asyncio.Event()
        self._entry_reconcile_lock = asyncio.Lock()
        self._state_refresh_locks: dict[str, asyncio.Lock] = {}
        self._state_refresh_batch_lock = asyncio.Lock()
        self.client = CrossTradeClient(
            base_url=settings.CROSSTRADE_BASE_URL,
            token=settings.CROSSTRADE_TOKEN,
            timeout=settings.REQUEST_TIMEOUT_SECONDS,
            rate_limit_per_minute=settings.CROSSTRADE_RATE_LIMIT_PER_MINUTE,
            rate_limit_window_seconds=settings.CROSSTRADE_RATE_LIMIT_WINDOW_SECONDS,
            rate_limit_max_retries=settings.CROSSTRADE_RATE_LIMIT_MAX_RETRIES,
            rate_limit_fallback_seconds=settings.CROSSTRADE_RATE_LIMIT_FALLBACK_SECONDS,
            request_min_interval_seconds=settings.CROSSTRADE_REQUEST_MIN_INTERVAL_SECONDS,
            safe_get_max_concurrency=settings.CROSSTRADE_SAFE_GET_MAX_CONCURRENCY,
            get_retry_max_retries=settings.CROSSTRADE_GET_RETRY_MAX_RETRIES,
            get_retry_delay_seconds=settings.CROSSTRADE_GET_RETRY_DELAY_SECONDS,
            get_retry_backoff_multiplier=settings.CROSSTRADE_GET_RETRY_BACKOFF_MULTIPLIER,
            get_retry_max_delay_seconds=settings.CROSSTRADE_GET_RETRY_MAX_DELAY_SECONDS,
            mutation_min_interval_seconds=getattr(
                settings, 'CROSSTRADE_MUTATION_MIN_INTERVAL_SECONDS', 0.02
            ),
        )
        self.execution_symbol = normalize_tradovate_symbol(settings.DEFAULT_EXECUTION_SYMBOL or 'MNQ1!')
        self.executor = Executor(self.client,
                                 execution_symbol=self.execution_symbol,
                                 bracket_confirm_retries=settings.BRACKET_CONFIRM_RETRIES,
                                 bracket_confirm_delay=settings.BRACKET_CONFIRM_RETRY_DELAY_SECONDS,
                                 change_retries=settings.MANAGEMENT_CHANGE_RETRIES,
                                 change_delay=settings.MANAGEMENT_CHANGE_RETRY_DELAY_SECONDS)

    def replace_accounts(self, accounts: list[AccountRule]) -> None:
        """Atomically publish a new immutable account-list snapshot for future work."""
        self.accounts = list(accounts)

    def safety_work_pending(self) -> bool:
        """True while broker read capacity must be reserved for exposure management."""
        if getattr(self, 'entry_wave_active', None) is not None:
            if self.entry_wave_active.is_set():
                return True
        unresolved_fn = getattr(self.store, 'unresolved_entry_attempt_count', None)
        if unresolved_fn is not None and unresolved_fn():
            return True
        trades_fn = getattr(self.store, 'all_trades', None)
        if trades_fn is not None and trades_fn():
            return True
        pending_fn = getattr(self.store, 'all_asw_pending', None)
        if pending_fn is not None and pending_fn():
            return True
        intent_fn = getattr(self.store, 'silver_stop_intent', None)
        return bool(intent_fn is not None and intent_fn())

    @staticmethod
    def _position_identity(row: dict[str, Any]) -> tuple[str, str]:
        contract = row.get('contract') if isinstance(row.get('contract'), dict) else {}
        instrument = str(
            row.get('instrument') or row.get('symbol') or row.get('contractName')
            or contract.get('name') or contract.get('symbol') or ''
        ).strip().upper()
        contract_id = str(row.get('contractId') or contract.get('id') or '').strip()
        return instrument, contract_id

    @staticmethod
    def _signed_position(row: dict[str, Any]) -> int:
        if row.get('netPos') is None:
            raise StateUnverified('fill-reconciled position row missing signed netPos')
        try:
            value = float(row['netPos'])
        except (TypeError, ValueError) as exc:
            raise StateUnverified('fill-reconciled position netPos is invalid') from exc
        if not math.isfinite(value) or not value.is_integer():
            raise StateUnverified('fill-reconciled position netPos must be integral')
        return int(value)

    async def position_proof(self, rule: AccountRule, *, expected_instrument: str = '',
                             expected_contract_id: str = '') -> dict[str, Any]:
        """Read the fill-reconciled collection, never the lagging singular position row.

        Tradovate can briefly return ``netPos=0`` from the singular endpoint after a fill.
        The plural endpoint reconciles fills first. Multiple open contracts are deliberately
        rejected rather than summed because an inter-expiry spread is not a flat MNQ book.
        """
        payload = await self.client.positions(rule.crosstrade_account)
        data = payload.get('data', payload) if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise StateUnverified('fill-reconciled positions response is not a list')
        rows: list[tuple[dict[str, Any], int, str, str]] = []
        for raw in data:
            if not isinstance(raw, dict):
                raise StateUnverified('fill-reconciled positions response contains a malformed row')
            signed = self._signed_position(raw)
            if signed:
                instrument, contract_id = self._position_identity(raw)
                rows.append((raw, signed, instrument, contract_id))
        if not rows:
            return {'signed_qty': 0, 'instrument': '', 'contract_id': '',
                    'net_price': None}
        if len(rows) != 1:
            raise StateUnverified(
                f'expected at most one open contract; broker returned {len(rows)} positions'
            )
        row, signed, instrument, contract_id = rows[0]
        expected_instrument = str(expected_instrument or '').strip().upper()
        expected_contract_id = str(expected_contract_id or '').strip()
        identity_requested = bool(expected_contract_id or expected_instrument)
        identity_matched = False
        if expected_contract_id and contract_id:
            if expected_contract_id != contract_id:
                raise StateUnverified(
                    f'open contractId {contract_id} differs from entry fill {expected_contract_id}'
                )
            identity_matched = True
        if expected_instrument and instrument:
            if expected_instrument != instrument:
                raise StateUnverified(
                    f'open instrument {instrument} differs from entry fill {expected_instrument}'
                )
            identity_matched = True
        if identity_requested and not identity_matched:
            raise StateUnverified('open position is missing comparable fill contract identity')
        raw_price = row.get('netPrice', row.get('averagePrice'))
        net_price = None
        if raw_price is not None:
            try:
                candidate = float(raw_price)
                if math.isfinite(candidate) and candidate > 0:
                    net_price = candidate
            except (TypeError, ValueError):
                pass
        return {'signed_qty': signed, 'instrument': instrument,
                'contract_id': contract_id, 'net_price': net_price}

    async def position_qty(self, rule: AccountRule, *, expected_instrument: str = '',
                           expected_contract_id: str = '') -> int:
        proof = await self.position_proof(
            rule, expected_instrument=expected_instrument,
            expected_contract_id=expected_contract_id,
        )
        return int(proof['signed_qty'])

    async def state_for(self, rule: AccountRule) -> AccountState:
        lock = self._state_refresh_locks.setdefault(rule.account_id, asyncio.Lock())
        async with lock:
            risk = self.store.get_risk_state(rule.account_id)
            state = await refresh_account_state(self.client, rule, risk)
            self.store.save_cached_account_state(state)
            return state

    def state_for_entry(self, rule: AccountRule, now: datetime) -> AccountState:
        state = self.store.get_cached_account_state(rule.account_id)
        if state is None:
            raise StateUnverified('entry state cache is not initialized')
        stamp = state.state_timestamp
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = max(0.0, (now - stamp.astimezone(timezone.utc)).total_seconds())
        max_age = min(
            float(getattr(self.settings, 'MAX_STATE_AGE_SECONDS', 20)),
            float(getattr(self.settings, 'ENTRY_STATE_CACHE_MAX_AGE_SECONDS', 20.0)),
        )
        if age > max_age:
            raise StateUnverified(f'entry state cache stale: {age:.1f}s > {max_age:.1f}s')
        return state

    async def refresh_state_cache(self) -> dict:
        lock = getattr(self, '_state_refresh_batch_lock', None)
        if lock is None:
            lock = asyncio.Lock()
            self._state_refresh_batch_lock = lock
        if lock.locked():
            return {'coalesced': True, 'results': []}

        if self.safety_work_pending():
            return {'paused_for_safety': True, 'results': []}

        async def refresh(rule: AccountRule) -> dict:
            if not rule.enabled:
                return {'account_id': rule.account_id, 'status': 'DISABLED'}
            try:
                state = await self.state_for(rule)
                return {'account_id': rule.account_id, 'status': 'FRESH',
                        'state_timestamp': state.state_timestamp.isoformat()}
            except RateLimitExceeded as exc:
                # CrossTrade can report that its own balance snapshot refresh is already
                # running. Reuse only a still-entry-eligible cache row; once stale, retain
                # the ERROR so readiness/new risk fails closed.
                cached = self.store.get_cached_account_state(rule.account_id)
                text = str(exc)
                if cached is not None and 'snapshot_refresh_pending' in text:
                    stamp = cached.state_timestamp
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    age = max(0.0, (
                        datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)
                    ).total_seconds())
                    max_age = min(
                        float(getattr(self.settings, 'MAX_STATE_AGE_SECONDS', 20)),
                        float(getattr(
                            self.settings, 'ENTRY_STATE_CACHE_MAX_AGE_SECONDS', 20.0
                        )),
                    )
                    if age <= max_age:
                        return {
                            'account_id': rule.account_id,
                            'status': 'FRESH_CACHED',
                            'state_timestamp': stamp.isoformat(),
                            'reason': 'CrossTrade snapshot refresh already in progress',
                        }
                return {'account_id': rule.account_id, 'status': 'ERROR',
                        'reason': text}
            except Exception as exc:
                return {'account_id': rule.account_id, 'status': 'ERROR', 'reason': str(exc)}
        # Do not enqueue an eight-account background GET wave behind the global semaphore.
        # One account at a time leaves at most one low-priority read in flight when an
        # entry, reconciliation, EXIT, or protection request becomes safety-critical.
        results = []
        async with lock:
            for rule in list(self.accounts):
                if self.safety_work_pending():
                    results.append({
                        'account_id': rule.account_id,
                        'status': 'PAUSED_FOR_SAFETY',
                    })
                    break
                results.append(await refresh(rule))
        return {'paused_for_safety': any(
                    row.get('status') == 'PAUSED_FOR_SAFETY' for row in results
                ), 'results': results}

    async def org_history(self, rule: AccountRule) -> list[dict]:
        now = datetime.now(timezone.utc)
        return await durable_fills(self.client, rule.crosstrade_account,
                                   now - timedelta(days=4), now + timedelta(seconds=1))

    async def entry_gate(self, rule: AccountRule) -> None:
        if self.settings.USE_CROSSTRADE_POSITION_GATE:
            q = await self.position_qty(rule)
            if q != 0:
                raise AllocationBlocked(f'broker position gate: netPos={q}')
        working = _rows(await self.client.orders(rule.crosstrade_account))
        if working:
            # Exact parity does not adopt or cancel pre-existing account orders.
            raise AllocationBlocked(f'working-order gate: {len(working)} pre-existing working order(s)')

    def _custom_id(self, event_id: str, account_id: str) -> str:
        h = hashlib.sha256(f'{event_id}|{account_id}'.encode()).hexdigest()[:20]
        return f'AP-{h}'

    async def entry_fill_proof(self, parent_order_id: str | None) -> dict[str, Any]:
        if not parent_order_id:
            raise ProtectionFailure('accepted entry missing Tradovate parent order id')
        payload = await self.client.fills_order(parent_order_id)
        rows = _rows(payload)
        seen: set[str] = set()
        fills: list[tuple[int, float]] = []
        instruments: set[str] = set()
        contract_ids: set[str] = set()
        for r in rows:
            fid = str(r.get('id') or r.get('fillId') or r.get('executionId') or '')
            if fid and fid in seen:
                continue
            if fid:
                seen.add(fid)
            q = abs(int(float(r.get('qty') or r.get('quantity') or 0)))
            px = float(r.get('price') or r.get('fillPrice') or 0)
            if q > 0 and px > 0:
                fills.append((q, px))
                instrument, contract_id = self._position_identity(r)
                if instrument:
                    instruments.add(instrument)
                if contract_id:
                    contract_ids.add(contract_id)
        if len(instruments) > 1 or len(contract_ids) > 1:
            raise ProtectionFailure('entry fills span multiple broker contracts')
        qty = sum(q for q, _ in fills)
        return {
            'qty': qty,
            'price': (sum(q * px for q, px in fills) / qty) if qty else None,
            'instrument': next(iter(instruments), ''),
            'contract_id': next(iter(contract_ids), ''),
        }

    async def entry_fill_price(self, parent_order_id: str | None, expected_qty: int) -> float:
        for _ in range(getattr(self.settings, 'BRACKET_CONFIRM_RETRIES', 12)):
            proof = await self.entry_fill_proof(parent_order_id)
            qty = int(proof['qty'])
            if qty == int(expected_qty):
                return float(proof['price'])
            if qty > int(expected_qty):
                raise ProtectionFailure(f'entry fills exceed routed qty: {qty}>{expected_qty}')
            await asyncio.sleep(getattr(self.settings, 'BRACKET_CONFIRM_RETRY_DELAY_SECONDS', 0.25))
        raise ProtectionFailure('destination entry fill price/quantity not proven')

    def _asw_alloc_for_qty(self, pending: AswPending, qty: int) -> Allocation:
        return Allocation(account_id=pending.account_id, event_id=pending.event_id, engine="ASW",
                          side=pending.side, qty=qty, entry=pending.entry, stop=pending.stop,
                          tp1=pending.target, tp2=None, tp1_qty=qty, runner_qty=0,
                          base_risk=0.0, effective_risk_budget=0.0,
                          risk_per_contract=pending.risk_per_contract)

    async def _order_status(self, account: str, order_id: str) -> str:
        payload = await self.client.order_status(account, order_id)
        data = payload.get('data', payload)
        if not isinstance(data, dict):
            raise ProtectionFailure('ASW order-status response malformed')
        return str(data.get('status') or data.get('ordStatus') or '').strip()

    async def _cancel_order_if_live(self, account: str, order_id: str) -> None:
        if not order_id:
            return
        terminal = {'filled','canceled','cancelled','rejected','expired','completed'}
        try:
            status = (await self._order_status(account, order_id)).lower()
            if status in terminal:
                return
        except CrossTradeError:
            rows = _rows(await self.client.orders(account))
            if not any(_order_id(r) == str(order_id) for r in rows):
                return
        try:
            await self.client.cancel_order(account, order_id)
        except AmbiguousMutation:
            status = (await self._order_status(account, order_id)).lower()
            if status not in terminal:
                raise ProtectionFailure(f'ambiguous ASW cancel remains nonterminal: {status}')
            return
        # A successful mutation response is only acknowledgement. Require a terminal
        # status (or absence from the account's working-order collection) before releasing
        # durable ownership of the order.
        try:
            status = (await self._order_status(account, order_id)).lower()
            if status in terminal:
                return
        except CrossTradeError:
            rows = _rows(await self.client.orders(account))
            if not any(_order_id(r) == str(order_id) for r in rows):
                return
            raise
        raise ProtectionFailure(
            f'cancel acknowledged but order {order_id} remains nonterminal: {status}'
        )

    async def _cancel_asw_owned_orders(self, pending: AswPending, *, include_children: bool) -> None:
        await self._cancel_order_if_live(pending.crosstrade_account, pending.parent_order_id)
        if include_children:
            for oid in pending.child_order_ids:
                await self._cancel_order_if_live(pending.crosstrade_account, oid)

    async def _prove_owned_flat(self, rule: AccountRule, account: str,
                                order_ids) -> tuple[bool, str]:
        """Prove every known order terminal/absent, then re-prove plural position flat."""
        errors: list[str] = []
        for oid in dict.fromkeys(order_ids):
            if not oid:
                continue
            try:
                await self._cancel_order_if_live(account, str(oid))
            except Exception as exc:
                errors.append(f'owned order {oid}: {exc}')
        signed: int | None = None
        try:
            signed = await self.position_qty(rule)
        except Exception as exc:
            errors.append(f'plural-position proof: {exc}')
        if signed:
            errors.append(f'fill-reconciled netPos changed to {signed}')
        return (not errors and signed == 0, '; '.join(errors))

    @staticmethod
    def _attempt_order_ownership_complete(attempt: EntryAttempt) -> bool:
        """Return whether a PLACE response supplied the complete expected native map."""
        parent = str(attempt.parent_order_id or '')
        children = {str(x) for x in attempt.child_order_ids if x}
        targets = {str(x) for x in attempt.target_order_ids if x}
        stops = {str(x) for x in attempt.stop_order_ids if x}
        tiers = (1 + int(bool(attempt.runner_qty))) if attempt.engine == 'CORE' else 1
        return bool(
            parent
            and parent not in children
            and len(targets) == tiers
            and len(stops) == tiers
            and len(children) == tiers * 2
            and children == targets | stops
            and not (targets & stops)
        )

    async def _strict_working_order_ids(self, account: str) -> set[str]:
        """Read the complete account working-order set without dropping malformed rows."""
        payload = await self.client.orders(account)
        if not isinstance(payload, dict) or payload.get('success') is False:
            raise StateUnverified('account working-orders response failed')
        data = payload.get('data', payload)
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = None
            for key in ('orders', 'items', 'data'):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
            if rows is None:
                raise StateUnverified('account working-orders response is not a list')
        else:
            raise StateUnverified('account working-orders response is not a list')
        ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise StateUnverified('account working-orders response contains a malformed row')
            oid = _order_id(row)
            if not oid:
                raise StateUnverified('working order is missing broker identity')
            ids.add(oid)
        return ids

    async def _clear_incomplete_attempt_orders(
            self, attempt: EntryAttempt) -> tuple[bool, str]:
        """Sweep only post-preflight orders, then prove this dedicated account clear.

        A lost PLACE response can hide every OSO/ATM identity. Production preflight rejects
        any existing working order, so every subsequently observed ID is attributable to the
        in-flight attempt. Legacy/non-production rows that recorded pre-existing IDs remain
        fail-closed and those IDs are never mutated.
        """
        errors: list[str] = []
        preexisting = {str(x) for x in attempt.preexisting_order_ids if x}
        try:
            observed = await self._strict_working_order_ids(attempt.crosstrade_account)
        except Exception as exc:
            return False, f'working-order enumeration failed: {exc}'

        if preexisting:
            errors.append(
                'entry preflight recorded pre-existing working orders; '
                'account-wide ownership is unprovable'
            )
        for oid in sorted(observed - preexisting):
            try:
                await self._cancel_order_if_live(attempt.crosstrade_account, oid)
            except Exception as exc:
                errors.append(f'post-preflight order {oid}: {exc}')

        try:
            remaining = await self._strict_working_order_ids(attempt.crosstrade_account)
        except Exception as exc:
            errors.append(f'working-order empty proof failed: {exc}')
        else:
            if remaining:
                errors.append(
                    'account working orders remain after cleanup: '
                    + ','.join(sorted(remaining))
                )
        return (not errors, '; '.join(errors))

    async def _finalize_observed_flat_pending(self, pending: AswPending,
                                               *, status: str,
                                               reason: str) -> dict:
        rule = next((a for a in self.accounts if a.account_id == pending.account_id), None)
        trade = self.store.get_trade(pending.account_id)
        same_trade = (trade if trade is not None and trade.engine == 'ASW'
                      and trade.event_id == pending.event_id else None)
        owned_ids = [pending.parent_order_id, *pending.child_order_ids]
        if same_trade is not None:
            owned_ids.extend([
                same_trade.parent_order_id,
                *same_trade.target_order_ids,
                *same_trade.stop_order_ids,
            ])
        if rule is None:
            detail = 'unconfigured account prevents plural flat proof'
            ok = False
        else:
            ok, detail = await self._prove_owned_flat(
                rule, pending.crosstrade_account, owned_ids,
            )
        if ok:
            self.store.delete_asw_pending(pending.account_id)
            if same_trade is not None:
                self.store.delete_trade(pending.account_id)
            return {'account_id': pending.account_id, 'status': status}
        self.store.trip_entry_circuit(
            reason=f'{pending.account_id}: {reason} cleanup unconfirmed: {detail}',
            event_key=pending.event_id, outcome='OWNED_ORDER_CLEANUP_UNCONFIRMED',
        )
        return {'account_id': pending.account_id, 'status': 'ERROR',
                'reason': f'{reason} cleanup unconfirmed: {detail}'}

    async def _finalize_observed_flat_trade(self, rule: AccountRule,
                                             trade: ActiveTrade,
                                             *, status: str,
                                             reason: str) -> dict:
        ok, detail = await self._prove_owned_flat(
            rule, rule.crosstrade_account,
            [trade.parent_order_id, *trade.target_order_ids, *trade.stop_order_ids],
        )
        if ok:
            self.store.delete_trade(rule.account_id)
            self.store.delete_cached_account_state(rule.account_id)
            return {'account_id': rule.account_id, 'status': status}
        self.store.trip_entry_circuit(
            reason=f'{rule.account_id}: {reason} cleanup unconfirmed: {detail}',
            event_key=trade.event_id, outcome='OWNED_ORDER_CLEANUP_UNCONFIRMED',
        )
        return {'account_id': rule.account_id, 'status': 'ERROR',
                'reason': f'{reason} cleanup unconfirmed: {detail}'}

    async def _finalize_observed_flat_attempt(
            self, rule: AccountRule, attempt: EntryAttempt, *, new_state: str,
            status: str, reason: str, actual_entry: float | None = None) -> dict:
        trade = self.store.get_trade(attempt.account_id)
        same_trade = (trade if trade is not None and trade.event_id == attempt.event_id
                      else None)
        owned_ids = [
            attempt.parent_order_id, *attempt.child_order_ids,
            *attempt.target_order_ids, *attempt.stop_order_ids,
        ]
        if same_trade is not None:
            owned_ids.extend([
                same_trade.parent_order_id,
                *same_trade.target_order_ids,
                *same_trade.stop_order_ids,
            ])
        ok, detail = await self._prove_owned_flat(
            rule, attempt.crosstrade_account, owned_ids,
        )
        if ok:
            updates: dict[str, Any] = {'last_error': reason}
            if actual_entry is not None:
                updates['actual_entry'] = actual_entry
            terminal = self.store.transition_entry_attempt(
                attempt.attempt_key, ('ACCEPTED',), new_state, **updates
            )
            if terminal is not None:
                if same_trade is not None:
                    self.store.delete_trade(attempt.account_id)
                self.store.delete_cached_account_state(attempt.account_id)
                return {'account_id': attempt.account_id, 'status': status}
            detail = 'entry-attempt terminal transition lost its compare-and-swap'
        current = self.store.get_entry_attempt(attempt.attempt_key) or attempt
        if current.state != 'ACCEPTED':
            # Another worker (normally EXIT) already advanced ownership. The stale
            # verifier no longer has authority to open the circuit or resurrect work.
            return {
                'account_id': attempt.account_id,
                'status': 'ATTEMPT_ALREADY_ADVANCED',
                'attempt_state': current.state,
            }
        if current.state == 'ACCEPTED':
            self._schedule_entry_reconcile(current, detail)
        self.store.trip_entry_circuit_for_attempt(
            attempt.attempt_key, ('ACCEPTED',),
            reason=f'{attempt.account_id}: {reason} cleanup unconfirmed: {detail}',
            outcome='OWNED_ORDER_CLEANUP_UNCONFIRMED',
        )
        return {'account_id': attempt.account_id, 'status': 'ERROR',
                'reason': f'{reason} cleanup unconfirmed: {detail}'}

    async def _verify_asw_live_protection(self, pending: AswPending, live_qty: int) -> bool:
        if live_qty <= 0:
            return False
        owned = {str(x) for x in pending.child_order_ids}
        alloc = self._asw_alloc_for_qty(pending, live_qty)
        retries = max(1, int(self.settings.ASW_PROTECTION_CONFIRM_RETRIES))
        for _ in range(retries):
            rows = await self.executor.working_orders(pending.crosstrade_account)
            if verify_single_bracket(rows, alloc, owned):
                return True
            await asyncio.sleep(self.settings.ASW_PROTECTION_CONFIRM_RETRY_DELAY_SECONDS)
        return False

    async def _flatten_asw_owned(self, pending: AswPending, reason: str) -> None:
        trade = self.store.get_trade(pending.account_id)
        owned_ids = [pending.parent_order_id, *pending.child_order_ids]
        if trade is not None and trade.engine == 'ASW':
            owned_ids.extend([
                trade.parent_order_id,
                *trade.target_order_ids,
                *trade.stop_order_ids,
            ])
        cancellation_errors: list[str] = []
        try:
            await self._cancel_order_if_live(
                pending.crosstrade_account, pending.parent_order_id
            )
        except Exception as exc:
            cancellation_errors.append(f'parent {pending.parent_order_id}: {exc}')
        # Never infer flat from a failed position read. A scoped Tradovate flatten is safe
        # when already flat and guarantees any ASW-owned exposure is removed.
        flatten_error: Exception | None = None
        try:
            await self.client.flatten(pending.crosstrade_account, self.execution_symbol)
        except Exception as exc:
            # A mutation acknowledgement is not state proof. Continue to the plural-position
            # read: a timed-out flatten may still have succeeded at the broker.
            flatten_error = exc
        for oid in dict.fromkeys(owned_ids):
            if not oid or str(oid) == str(pending.parent_order_id):
                continue
            try:
                await self._cancel_order_if_live(pending.crosstrade_account, oid)
            except Exception as exc:
                cancellation_errors.append(f'owned order {oid}: {exc}')
        rule = next((a for a in self.accounts if a.account_id == pending.account_id), None)
        if rule is None:
            raise ProtectionFailure(
                f'{reason}: ASW flatten cannot be proven for an unconfigured account'
            )
        try:
            signed = await self.position_qty(rule)
        except Exception as exc:
            detail = f'{reason}: ASW flatten position proof failed: {exc}'
            self.store.trip_entry_circuit(
                reason=f'{pending.account_id}: {detail}',
                event_key=pending.event_id, outcome='FLATTEN_UNCONFIRMED',
            )
            raise ProtectionFailure(detail) from exc
        if signed or cancellation_errors:
            details = []
            if signed:
                details.append(f'fill-reconciled netPos remains {signed}')
            if cancellation_errors:
                details.append('owned-order cleanup failed: ' + '; '.join(cancellation_errors))
            if flatten_error is not None:
                details.append(f'flatten request error: {flatten_error}')
            detail = f'{reason}: ASW flatten unconfirmed: ' + '; '.join(details)
            self.store.trip_entry_circuit(
                reason=f'{pending.account_id}: {detail}',
                event_key=pending.event_id, outcome='FLATTEN_UNCONFIRMED',
            )
            raise ProtectionFailure(detail)
        self.store.delete_asw_pending(pending.account_id)
        if trade is not None and trade.engine == 'ASW':
            self.store.delete_trade(pending.account_id)

    async def route_asw_working_limit(self, event: ParsedEvent, *, event_key: str = '',
                                      receipt_epoch: float | None = None) -> dict:
        if event.plan is None or event.plan.engine != 'ASW':
            raise ValueError('ASW_WORKING_LIMIT missing canonical ASW plan')
        circuit_fn = getattr(self.store, 'entry_circuit', None)
        circuit = circuit_fn() if circuit_fn is not None else {'open': False}
        if circuit.get('open'):
            return {'kind': 'ASW_WORKING_LIMIT', 'engine': 'ASW', 'results': [
                {'account_id': a.account_id,
                 'status': 'SKIP' if not a.enabled else 'ERROR',
                 'reason': ('disabled' if not a.enabled else
                            f"new-risk circuit open: {circuit.get('reason') or 'reset required'}")}
                for a in self.accounts
            ]}
        if self.store.unresolved_entry_attempt_count():
            reason = 'another entry wave is still awaiting broker reconciliation'
            return {'kind': 'ASW_WORKING_LIMIT', 'engine': 'ASW', 'results': [
                {'account_id': a.account_id,
                 'status': 'SKIP' if not a.enabled else 'ABORTED',
                 'reason': 'disabled' if not a.enabled else reason}
                for a in self.accounts
            ]}
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        receipt_epoch = time.time() if receipt_epoch is None else float(receipt_epoch)
        if event.plan.expiry_time_ms is None or event.plan.expiry_time_ms <= now_ms:
            return {'kind':'ASW_WORKING_LIMIT','engine':'ASW','results':[
                {'account_id':a.account_id,'status':'SKIP','reason':'ASW candidate expired before routing'}
                for a in self.accounts if a.enabled]}
        results = []
        global_instrument_error: str | None = None
        # ASW is also a new-risk mutation. Its Pine signal timestamp gives the durable
        # fence baseline because the current webhook worker API predates receipt_epoch for
        # this route. A final local check is made directly before each PLACE, and any
        # control arriving while PLACE is in flight is reconciled after its receipt.
        admission_epoch = receipt_epoch
        deadline_epoch = min(
            float(event.plan.expiry_time_ms) / 1000.0,
            receipt_epoch + float(getattr(
                self.settings, 'ENTRY_SIGNAL_MAX_AGE_SECONDS', 8.0
            )),
        )

        def admitted() -> bool:
            return (time.time() < deadline_epoch
                    and not self.store.entry_circuit().get('open')
                    and not self.store.unresolved_entry_attempt_count()
                    and not self.store.entry_aborted_after('ASW', admission_epoch))

        for rule in self.accounts:
            if not rule.enabled:
                results.append({'account_id':rule.account_id,'status':'SKIP','reason':'disabled'}); continue
            if global_instrument_error is not None:
                results.append({'account_id':rule.account_id,'status':'ERROR',
                                'reason':'global instrument contract failure; fanout aborted before broker mutation: '+global_instrument_error}); continue
            try:
                await self.entry_gate(rule)
                state = await self.state_for(rule)
                alloc = allocate_asw(event.plan, rule, state,
                                    max_state_age_seconds=self.settings.MAX_STATE_AGE_SECONDS, now=now)
                dedupe = f'{event.plan.event_id}:{ny_date(now)}:{rule.account_id}'
                if not self.store.claim_event(dedupe):
                    results.append({'account_id':rule.account_id,'status':'SKIP','reason':'duplicate event'}); continue
                cid = self._custom_id(dedupe, rule.account_id)
                if not admitted():
                    raise EntryAdmissionClosed(
                        'ASW entry admission closed before LIMIT PLACE'
                    )
                submit_started = time.time()
                receipt = await self.executor.place_asw_limit(
                    rule.crosstrade_account, alloc, cid,
                    expiry_time_ms=int(event.plan.expiry_time_ms), now_ms=now_ms,
                    deadline_epoch=deadline_epoch, admission_guard=admitted,
                )
                accepted_at = time.time()
                pending = AswPending(
                    account_id=rule.account_id, crosstrade_account=rule.crosstrade_account,
                    event_id=event.plan.event_id, side=alloc.side, qty=alloc.qty,
                    native_qty=int(event.plan.source_qty or 0), entry=alloc.entry, stop=alloc.stop,
                    target=alloc.tp1, risk_per_contract=alloc.risk_per_contract,
                    signal_time_ms=int(event.plan.native_time_ms or 0),
                    expiry_time_ms=int(event.plan.expiry_time_ms),
                    parent_order_id=str(receipt.parent_order_id), custom_order_id=cid,
                    child_order_ids=[str(x) for x in receipt.child_order_ids], created_at=now)
                self.store.save_asw_pending(pending)
                fence = self._crossed_control_fence(
                    'ASW', submit_started, accepted_at
                )
                if fence is not None:
                    try:
                        await self._flatten_asw_owned(
                            pending, 'ASW LIMIT accepted after control fence'
                        )
                        results.append({
                            'account_id': rule.account_id, 'status': 'ABORTED',
                            'reason': 'ASW LIMIT accepted after control fence; flat proven',
                        })
                    except Exception as exc:
                        results.append({
                            'account_id': rule.account_id, 'status': 'ERROR',
                            'reason': str(exc),
                        })
                    continue
                results.append({'account_id':rule.account_id,'status':'PENDING_LIMIT','qty':alloc.qty,
                                'native_qty':int(event.plan.source_qty or 0),
                                'scale_multiple':alloc.qty / float(event.plan.source_qty or 1),
                                'entry':alloc.entry,'stop':alloc.stop,'tp1':alloc.tp1,
                                'expiry_time_ms':int(event.plan.expiry_time_ms)})
            except EntryAdmissionClosed as exc:
                results.append({'account_id': rule.account_id, 'status': 'ABORTED',
                                'reason': str(exc)})
            except InstrumentContractError as exc:
                global_instrument_error = str(exc)
                results.append({'account_id':rule.account_id,'status':'ERROR','reason':global_instrument_error})
            except (CrossTradeError, ProtectionFailure, ValueError) as exc:
                results.append({'account_id':rule.account_id,'status':'ERROR','reason':str(exc)})
            except (AllocationBlocked, StateUnverified) as exc:
                results.append({'account_id':rule.account_id,'status':'SKIP','reason':str(exc)})
        out={'kind':'ASW_WORKING_LIMIT','engine':'ASW','results':results}
        if global_instrument_error is not None:
            out['global_execution_error']=global_instrument_error
        execution_errors = [row for row in results if row.get('status') == 'ERROR']
        if execution_errors:
            reason = str(execution_errors[0].get('reason') or 'ASW execution failed')
            self.store.trip_entry_circuit(
                reason=f'ASW entry wave requires review: {reason}',
                event_key=event_key, outcome='ASW_PARTIAL_OR_FAILED_ENTRY',
            )
        return out

    async def cancel_asw_pending(self, event: ParsedEvent) -> dict:
        t5 = int((event.fields or {}).get('T5') or 0)
        results=[]
        for pending in list(self.store.all_asw_pending()):
            if t5 and pending.signal_time_ms != t5:
                continue
            if event.side and pending.side != event.side:
                continue
            try:
                # Canonical Pine says the setup is no longer pending. Cancel any unfilled
                # remainder. If broker exposure already exists while Pine still says pending,
                # flatten it rather than silently diverging from the canonical portfolio.
                rule = next((a for a in self.accounts if a.account_id == pending.account_id), None)
                had_position = None
                if rule is not None:
                    try:
                        had_position = (await self.position_qty(rule)) != 0
                    except Exception:
                        had_position = None
                # Canonical Pine canceled an unfilled setup. The helper keeps durable
                # ownership until both order cleanup and plural-position flat proof succeed.
                await self._flatten_asw_owned(pending, 'ASW_CANCEL_PENDING')
                results.append({'account_id':pending.account_id,'status':'CANCELED_FLATTENED' if had_position is not False else 'CANCELED_UNFILLED'})
            except (CrossTradeError, ProtectionFailure, StateUnverified) as exc:
                results.append({'account_id':pending.account_id,'status':'ERROR','reason':str(exc)})
        out = {'kind':'ASW_CANCEL_PENDING','engine':'ASW','results':results}
        if any(row.get('status') == 'ERROR' for row in results):
            out.update({
                'deferred': True,
                'reason': 'ASW cancel/flatten remains unconfirmed; retrying flat proof',
            })
        return out

    async def asw_time_flat(self, event: ParsedEvent) -> dict:
        results=[]; seen=set()
        for pending in list(self.store.all_asw_pending()):
            if event.side and pending.side != event.side: continue
            seen.add(pending.account_id)
            try:
                await self._flatten_asw_owned(pending, 'TIME_FLAT')
                results.append({'account_id':pending.account_id,'status':'FLATTENED'})
            except Exception as exc:
                results.append({'account_id':pending.account_id,'status':'ERROR','reason':str(exc)})
        rule_map={a.account_id:a for a in self.accounts}
        for trade in list(self.store.all_trades()):
            if trade.engine!='ASW' or trade.account_id in seen: continue
            if event.side and trade.side != event.side: continue
            rule=rule_map.get(trade.account_id)
            if rule is None: continue
            try:
                row = await self._hard_flat_rule_once(
                    rule, trade, 'ASW_TIME_FLAT safety control'
                )
                results.append(row)
            except Exception as exc:
                results.append({'account_id':trade.account_id,'status':'ERROR','reason':str(exc)})
        out = {'kind':'ASW_TIME_FLAT','engine':'ASW','results':results}
        if any(row.get('status') == 'ERROR' for row in results):
            out.update({
                'deferred': True,
                'reason': 'ASW time-flat remains unconfirmed; retrying flat proof',
            })
        return out

    async def reconcile_asw_pending(self) -> dict:
        now=datetime.now(timezone.utc); now_ms=int(now.timestamp()*1000)
        rule_map={a.account_id:a for a in self.accounts}
        results=[]
        for pending in list(self.store.all_asw_pending()):
            rule=rule_map.get(pending.account_id)
            if rule is None:
                results.append({'account_id':pending.account_id,'status':'UNKNOWN_ACCOUNT'}); continue
            try:
                status=(await self._order_status(pending.crosstrade_account,pending.parent_order_id)).lower()
                if now_ms >= pending.expiry_time_ms and status not in {'filled','canceled','cancelled','rejected','expired','completed'}:
                    await self._cancel_order_if_live(pending.crosstrade_account,pending.parent_order_id)
                    status=(await self._order_status(pending.crosstrade_account,pending.parent_order_id)).lower()
                signed=await self.position_qty(rule)
                absq=abs(signed)
                expected_sign=1 if pending.side=='LONG' else -1
                if signed and ((signed>0)!=(expected_sign>0)):
                    await self._flatten_asw_owned(pending,'SIDE_MISMATCH')
                    results.append({'account_id':pending.account_id,'status':'FLATTENED_SIDE_MISMATCH'}); continue

                terminal_cancel = status in {'canceled','cancelled','rejected','expired'}
                if terminal_cancel:
                    if absq:
                        # Partial broker fill after the canonical pending order expired/cancelled
                        # is not a full ASW allocation. Flatten rather than carry distorted risk.
                        await self._flatten_asw_owned(pending,'PARTIAL_AFTER_CANCEL')
                        results.append({'account_id':pending.account_id,'status':'FLATTENED_PARTIAL'})
                    else:
                        results.append(await self._finalize_observed_flat_pending(
                            pending,
                            status='EXPIRED_UNFILLED',
                            reason='ASW entry ended terminal while destination was flat',
                        ))
                    continue

                if absq == 0:
                    if status in {'filled','completed'}:
                        # Entry and broker-hosted exit completed between reconciliations.
                        # Cancel and confirm every known order, then re-prove the position flat
                        # before releasing durable ownership.
                        results.append(await self._finalize_observed_flat_pending(
                            pending,
                            status='CLOSED_BEFORE_RECONCILE',
                            reason='ASW entry completed and destination was observed flat',
                        ))
                    else:
                        results.append({'account_id':pending.account_id,'status':'WORKING'})
                    continue

                if absq != pending.qty:
                    if now_ms >= pending.expiry_time_ms or status in {'filled','completed'}:
                        await self._flatten_asw_owned(pending,'NONFULL_FILL')
                        results.append({'account_id':pending.account_id,'status':'FLATTENED_NONFULL_FILL','qty':absq})
                    else:
                        protected=await self._verify_asw_live_protection(pending,absq)
                        if not protected:
                            await self._flatten_asw_owned(pending,'PARTIAL_UNPROTECTED')
                            results.append({'account_id':pending.account_id,'status':'FLATTENED_UNPROTECTED_PARTIAL'})
                        else:
                            results.append({'account_id':pending.account_id,'status':'PARTIAL_PROTECTED','qty':absq})
                    continue

                protected=await self._verify_asw_live_protection(pending,pending.qty)
                if not protected:
                    await self._flatten_asw_owned(pending,'PROTECTION_UNPROVEN')
                    results.append({'account_id':pending.account_id,'status':'FLATTENED_PROTECTION_FAILURE'})
                    continue
                actual_entry=await self.entry_fill_price(pending.parent_order_id,pending.qty)
                target_ids, stop_ids = await self.executor.owned_roles(pending.crosstrade_account,
                                                                        pending.child_order_ids)
                if not target_ids or not stop_ids:
                    await self._flatten_asw_owned(pending,'OWNED_ROLE_DISCOVERY_FAILED')
                    results.append({'account_id':pending.account_id,'status':'FLATTENED_ROLE_DISCOVERY_FAILURE'})
                    continue
                trade=ActiveTrade(account_id=pending.account_id,event_id=pending.event_id,engine='ASW',
                                  side=pending.side,entry=actual_entry,initial_stop=pending.stop,current_stop=pending.stop,
                                  tp1=pending.target,tp2=None,total_qty=pending.qty,tp1_qty=pending.qty,runner_qty=0,
                                  current_position_qty=pending.qty,previous_position_qty=pending.qty,
                                  stop_order_ids=stop_ids, target_order_ids=target_ids,
                                  parent_order_id=pending.parent_order_id,custom_order_id=pending.custom_order_id)
                self.store.save_trade(trade); self.store.delete_asw_pending(pending.account_id)
                results.append({'account_id':pending.account_id,'status':'ACTIVE_PROTECTED','qty':pending.qty})
            except (CrossTradeError,StateUnverified,ProtectionFailure,ValueError) as exc:
                results.append({'account_id':pending.account_id,'status':'ERROR','reason':str(exc)})
        # Clean stale ASW active records after broker-hosted stop/target closes them.
        for trade in list(self.store.all_trades()):
            if trade.engine!='ASW': continue
            rule=rule_map.get(trade.account_id)
            if rule is None: continue
            try:
                if await self.position_qty(rule)==0:
                    results.append(await self._finalize_observed_flat_trade(
                        rule, trade,
                        status='CLOSED_RECONCILED',
                        reason='ASW active position was observed flat',
                    ))
            except Exception as exc:
                self.store.trip_entry_circuit(
                    reason=(f'{trade.account_id}: ASW active flat reconciliation '
                            f'unconfirmed: {exc}'),
                    event_key=trade.event_id,
                    outcome='OWNED_ORDER_CLEANUP_UNCONFIRMED',
                )
                results.append({'account_id': trade.account_id, 'status': 'ERROR',
                                'reason': str(exc)})
        return {'kind':'ASW_RECONCILE','results':results}

    async def _entry_snapshot(self) -> dict[str, dict[str, Any]]:
        timeout = float(getattr(self.settings, 'ENTRY_PREFLIGHT_TIMEOUT_SECONDS', 5.0))
        payload = await asyncio.wait_for(self.client.accounts_snapshot(), timeout=timeout)
        if not isinstance(payload, dict) or payload.get('success') is False:
            raise StateUnverified('Tradovate account snapshot failed')
        cards = payload.get('accounts')
        if not isinstance(cards, list):
            raise StateUnverified('Tradovate account snapshot missing accounts list')
        by_name: dict[str, dict[str, Any]] = {}
        for card in cards:
            if not isinstance(card, dict) or not card.get('name'):
                continue
            name = str(card['name'])
            if name in by_name:
                raise StateUnverified(f'duplicate account {name} in Tradovate snapshot')
            by_name[name] = card
        for rule in self.accounts:
            if not rule.enabled:
                continue
            card = by_name.get(rule.crosstrade_account)
            if card is None:
                raise StateUnverified(
                    f'{rule.account_id}: account missing from Tradovate snapshot'
                )
            if card.get('error'):
                raise StateUnverified(
                    f"{rule.account_id}: Tradovate snapshot error: {card['error']}"
                )
            if not isinstance(card.get('positions'), list):
                raise StateUnverified(f'{rule.account_id}: snapshot positions are unverified')
            if not isinstance(card.get('workingOrders'), list):
                raise StateUnverified(f'{rule.account_id}: snapshot working orders are unverified')
        return by_name

    @staticmethod
    def _snapshot_entry_gate(rule: AccountRule, card: dict[str, Any]) -> set[str]:
        positions = [x for x in card.get('positions', []) if isinstance(x, dict)]
        working = [x for x in card.get('workingOrders', []) if isinstance(x, dict)]
        if positions:
            raise AllocationBlocked(
                f'broker position gate: {len(positions)} open position(s) in batch snapshot'
            )
        if working:
            raise AllocationBlocked(
                f'working-order gate: {len(working)} pre-existing working order(s)'
            )
        return {_order_id(row) for row in working if _order_id(row)}

    def _attempt_allocation(self, attempt: EntryAttempt) -> Allocation:
        return Allocation(
            account_id=attempt.account_id, event_id=attempt.event_id,
            engine=attempt.engine, side=attempt.side, qty=attempt.qty,
            entry=attempt.planned_entry, stop=attempt.stop, tp1=attempt.tp1,
            tp2=attempt.tp2, tp1_qty=attempt.tp1_qty,
            runner_qty=attempt.runner_qty, base_risk=0.0,
            effective_risk_budget=0.0,
            risk_per_contract=(abs(attempt.planned_entry - attempt.stop) * 2.0),
        )

    def _crossed_control_fence(self, engine: str, submit_started: float,
                               accepted_at: float) -> dict[str, Any] | None:
        """Resolve any safety control that invalidates an accepted mutation.

        Store history prevents a later soft EXIT from hiding an earlier HARD_FLAT.
        The fallback keeps compatibility with narrow test doubles from older releases.
        """
        # Old/test rows can predate submit_started persistence or contain clocks captured
        # in the opposite order. Widening to the sorted interval is fail-closed and prevents
        # a malformed legacy timestamp from stopping all reconciliation.
        interval_start = min(float(submit_started), float(accepted_at))
        interval_end = max(float(submit_started), float(accepted_at))
        relevant = getattr(self.store, 'relevant_control_fence_info', None)
        if relevant is not None:
            return relevant(
                engine, submit_started_epoch=interval_start,
                accepted_at_epoch=interval_end,
            )
        fence = self.store.latest_control_fence_info(engine)
        if fence is None:
            return None
        epoch = float(fence['fence_epoch'])
        hard = str(fence.get('kind') or '').upper() in {
            'HARD_FLAT', 'ASW_TIME_FLAT', 'ASW_CANCEL_PENDING',
        }
        if interval_start <= epoch and (hard or epoch <= interval_end):
            return fence
        return None

    async def _settle_flattening_attempt(self, attempt: EntryAttempt,
                                         reason: str) -> dict:
        """Retry flatten/cleanup and terminalize only after broker state proves flat."""
        flatten_error: Exception | None = None
        try:
            await self.client.flatten(attempt.crosstrade_account, self.execution_symbol)
        except Exception as exc:
            # A timed-out mutation may still have succeeded. The plural position read below,
            # rather than the HTTP acknowledgement, is the authoritative exposure proof.
            flatten_error = exc

        cancellation_errors: list[str] = []
        owned_ids = dict.fromkeys([
            attempt.parent_order_id,
            *attempt.child_order_ids,
            *attempt.target_order_ids,
            *attempt.stop_order_ids,
        ])
        for order_id in owned_ids:
            if not order_id:
                continue
            try:
                await self._cancel_order_if_live(attempt.crosstrade_account, order_id)
            except Exception as exc:
                cancellation_errors.append(f'{order_id}: {exc}')

        # A lost or incomplete PLACE response can leave the parent and every native child
        # unknown. On the dedicated account, sweep only orders that were not present at the
        # clean entry preflight and require a second empty read. Never release durable
        # ownership merely because the position happens to be flat.
        if not self._attempt_order_ownership_complete(attempt):
            cleared, detail = await self._clear_incomplete_attempt_orders(attempt)
            if not cleared:
                cancellation_errors.append(
                    'incomplete-ownership cleanup failed: ' + detail
                )

        rule = next((a for a in self.accounts if a.account_id == attempt.account_id), None)
        proof_error: Exception | None = None
        signed: int | None = None
        if rule is None:
            proof_error = StateUnverified('unresolved attempt has no configured account')
        else:
            try:
                signed = await self.position_qty(rule)
            except Exception as exc:
                proof_error = exc

        if signed == 0 and proof_error is None and not cancellation_errors:
            terminal = self.store.transition_entry_attempt(
                attempt.attempt_key, ('FLATTENING',), 'FLAT', last_error=reason
            )
            if terminal is None:
                current = self.store.get_entry_attempt(attempt.attempt_key)
                return {'account_id': attempt.account_id,
                        'status': 'FLATTEN_ALREADY_CLAIMED',
                        'attempt_state': current.state if current else 'MISSING'}
            self.store.delete_trade(attempt.account_id)
            self.store.delete_cached_account_state(attempt.account_id)
            return {'account_id': attempt.account_id, 'status': 'FLATTENED',
                    'reason': reason}

        details: list[str] = []
        if signed:
            details.append(f'fill-reconciled netPos remains {signed}')
        if proof_error is not None:
            details.append(f'plural-position proof failed: {proof_error}')
        if cancellation_errors:
            details.append('owned-order cleanup failed: ' + '; '.join(cancellation_errors))
        if flatten_error is not None:
            details.append(f'flatten request error: {flatten_error}')
        detail = '; '.join(details) or 'flat state remains unconfirmed'
        current = self.store.get_entry_attempt(attempt.attempt_key) or attempt
        self._schedule_entry_reconcile(current, detail)
        self.store.trip_entry_circuit(
            reason=f'{attempt.account_id}: emergency flatten unconfirmed: {detail}',
            event_key=attempt.attempt_key, outcome='FLATTEN_UNCONFIRMED',
        )
        return {'account_id': attempt.account_id, 'status': 'ERROR',
                'attempt_state': 'FLATTENING',
                'reason': f'{reason}; emergency flatten unconfirmed: {detail}'}

    async def _flatten_attempt_once(self, attempt: EntryAttempt, reason: str) -> dict:
        claimed = self.store.transition_entry_attempt(
            attempt.attempt_key, ('ACCEPTED', 'SUBMITTING'), 'FLATTENING',
            last_error=reason,
        )
        if claimed is None:
            current = self.store.get_entry_attempt(attempt.attempt_key)
            return {'account_id': attempt.account_id,
                    'status': 'FLATTEN_ALREADY_CLAIMED',
                    'attempt_state': current.state if current else 'MISSING'}
        return await self._settle_flattening_attempt(claimed, reason)

    async def route_entry(self, event: ParsedEvent, *, event_key: str = '',
                          receipt_epoch: float | None = None) -> dict:
        """Prepare all accounts, dispatch one tight mutation wave, verify asynchronously."""
        if event.plan is None:
            raise ValueError('ENTRY missing canonical plan')
        # Zero-touch discovery may publish a new list while this coroutine is awaiting
        # broker I/O. One signal must use one stable destination set from preflight through
        # result construction; a newly onboarded account joins the following signal.
        accounts = tuple(self.accounts)
        now = datetime.now(timezone.utc)
        receipt_epoch = time.time() if receipt_epoch is None else float(receipt_epoch)
        event_key = event_key or event.plan.event_id
        deadline = receipt_epoch + float(
            getattr(self.settings, 'ENTRY_SIGNAL_MAX_AGE_SECONDS', 8.0)
        )
        circuit = self.store.entry_circuit()
        if circuit.get('open'):
            reason = f"entry circuit open: {circuit.get('reason') or 'manual reset required'}"
            return {'kind': 'ENTRY', 'engine': event.plan.engine,
                    'results': [
                        {'account_id': rule.account_id,
                         'status': 'SKIP' if not rule.enabled else 'ERROR',
                         'reason': 'disabled' if not rule.enabled else reason}
                        for rule in accounts
                    ], 'entry_circuit_open': True}
        if self.store.unresolved_entry_attempt_count():
            reason = 'prior entry wave is still awaiting broker reconciliation'
            return {'kind': 'ENTRY', 'engine': event.plan.engine,
                    'results': [
                        {'account_id': rule.account_id,
                         'status': 'SKIP' if not rule.enabled else 'ABORTED',
                         'reason': 'disabled' if not rule.enabled else reason}
                        for rule in accounts
                    ]}
        if time.time() > deadline:
            reason = 'entry signal expired before preflight'
            return {'kind': 'ENTRY', 'engine': event.plan.engine,
                    'results': [
                        {'account_id': rule.account_id,
                         'status': 'SKIP' if not rule.enabled else 'ABORTED',
                         'reason': 'disabled' if not rule.enabled else reason}
                        for rule in accounts
                    ]}

        # Production uses one all-account snapshot. Assigned test doubles from prior releases
        # may expose only the old entry_gate seam, which remains a non-production fallback.
        legacy_gate = 'entry_gate' in self.__dict__ and not hasattr(self, 'client')
        try:
            cards = {} if legacy_gate else await self._entry_snapshot()
        except Exception as exc:
            reason = f'entry batch preflight failed: {exc}'
            self.store.trip_entry_circuit(reason=reason, event_key=event_key,
                                          outcome='PREFLIGHT_FAILED')
            return {'kind': 'ENTRY', 'engine': event.plan.engine,
                    'results': [
                        {'account_id': rule.account_id,
                         'status': 'SKIP' if not rule.enabled else 'ERROR',
                         'reason': 'disabled' if not rule.enabled else reason}
                        for rule in accounts
                    ], 'global_execution_error': reason}

        unresolved_accounts = {
            a.account_id for a in self.store.all_entry_attempts(
                ('PREPARED', 'SUBMITTING', 'ACCEPTED', 'FLATTENING')
            )
        }
        prepared: list[tuple[AccountRule, Allocation, str, str, set[str]]] = []
        result_by_account: dict[str, dict[str, Any]] = {}
        fatal_preparation: str | None = None

        async def prepare(rule: AccountRule):
            if not rule.enabled:
                return None, {'account_id': rule.account_id, 'status': 'SKIP',
                              'reason': 'disabled'}, None
            try:
                if rule.account_id in unresolved_accounts:
                    raise AllocationBlocked('prior entry attempt is still unresolved')
                if self.store.get_trade(rule.account_id) is not None:
                    raise AllocationBlocked('destination already has an active managed trade')
                if legacy_gate:
                    await self.entry_gate(rule)
                    before_ids: set[str] = set()
                else:
                    before_ids = self._snapshot_entry_gate(
                        rule, cards[rule.crosstrade_account]
                    )
                if 'state_for' in self.__dict__:
                    state = await self.state_for(rule)
                else:
                    state = self.state_for_entry(rule, now)
                if event.plan.engine == 'ORG' and event.plan.reentry:
                    prior = self.store.get_org_attempt(rule.account_id)
                    if not prior:
                        raise AllocationBlocked('ORG re-entry: no destination prior attempt')
                    fills = await self.org_history(rule)
                    if not prove_prior_org_stop(
                        fills=fills, stop_order_ids=set(prior.get('stop_order_ids', []))
                    ):
                        raise AllocationBlocked(
                            'ORG re-entry: destination stop outcome not proven'
                        )
                alloc = allocate(
                    event.plan, rule, state,
                    max_state_age_seconds=getattr(self.settings, 'MAX_STATE_AGE_SECONDS', 20),
                    now=now,
                )
                attempt_key = f'{event.plan.event_id}:{ny_date(now)}:{rule.account_id}'
                cid = self._custom_id(attempt_key, rule.account_id)
                return (rule, alloc, attempt_key, cid, before_ids), None, None
            except AllocationBlocked as exc:
                return None, {'account_id': rule.account_id, 'status': 'SKIP',
                              'reason': str(exc)}, None
            except (StateUnverified, CrossTradeError, ProtectionFailure, ValueError) as exc:
                return None, None, f'{rule.account_id}: {exc}'

        prepared_rows = await asyncio.gather(*(prepare(rule) for rule in accounts))
        for item, row, fatal in prepared_rows:
            if row is not None:
                result_by_account[row['account_id']] = row
            if fatal and fatal_preparation is None:
                fatal_preparation = fatal
            if item is not None:
                prepared.append(item)
        if fatal_preparation is not None:
            reason = f'entry batch preparation failed before broker mutation: {fatal_preparation}'
            self.store.trip_entry_circuit(reason=reason, event_key=event_key,
                                          outcome='PREPARATION_FAILED')
            for rule in accounts:
                if rule.enabled and rule.account_id not in result_by_account:
                    result_by_account[rule.account_id] = {
                        'account_id': rule.account_id, 'status': 'ERROR', 'reason': reason,
                    }
            return {'kind': 'ENTRY', 'engine': event.plan.engine,
                    'results': [result_by_account[r.account_id] for r in accounts],
                    'global_execution_error': reason}

        attempt_candidates: list[tuple[AccountRule, Allocation, EntryAttempt]] = []
        created_epoch = time.time()
        for rule, alloc, attempt_key, cid, before_ids in prepared:
            attempt = EntryAttempt(
                attempt_key=attempt_key, account_id=rule.account_id,
                crosstrade_account=rule.crosstrade_account,
                event_id=event.plan.event_id, engine=alloc.engine, side=alloc.side,
                qty=alloc.qty, planned_entry=alloc.entry, stop=alloc.stop,
                tp1=alloc.tp1, tp2=alloc.tp2, tp1_qty=alloc.tp1_qty,
                runner_qty=alloc.runner_qty, custom_order_id=cid,
                entry_native_bar_index=event.plan.native_bar_index,
                entry_receipt_epoch=receipt_epoch, inbox_event_key=event_key,
                preexisting_order_ids=sorted(before_ids),
                created_at_epoch=created_epoch, updated_at_epoch=created_epoch,
            )
            attempt_candidates.append((rule, alloc, attempt))

        # Production Store commits the whole PREPARED wave once. Keep the single-row
        # fallback for narrow legacy test doubles that predate the batch API.
        prepare_many = getattr(self.store, 'prepare_entry_attempts', None)
        if callable(prepare_many):
            prepared_outcomes = prepare_many([
                (attempt.attempt_key, attempt)
                for _rule, _alloc, attempt in attempt_candidates
            ])
        else:
            prepared_outcomes = {
                attempt.attempt_key: self.store.prepare_entry_attempt(
                    attempt.attempt_key, attempt
                )
                for _rule, _alloc, attempt in attempt_candidates
            }

        submit_items: list[tuple[AccountRule, Allocation, EntryAttempt]] = []
        for rule, alloc, attempt in attempt_candidates:
            if not prepared_outcomes.get(attempt.attempt_key, False):
                result_by_account[rule.account_id] = {
                    'account_id': rule.account_id, 'status': 'SKIP',
                    'reason': 'duplicate event or unresolved destination attempt',
                }
                continue
            submit_items.append((rule, alloc, attempt))

        wave_abort = asyncio.Event()
        max_concurrency = max(1, int(getattr(
            self.settings, 'ENTRY_FANOUT_MAX_CONCURRENCY', 8
        )))
        semaphore = asyncio.Semaphore(max_concurrency)
        accepted_times: list[float] = []

        def admitted() -> bool:
            return (not wave_abort.is_set()
                    and not self.store.entry_aborted_after(event.plan.engine, receipt_epoch)
                    and not self.store.entry_circuit().get('open'))

        # One all-or-none SQLite transition replaces one FULL-sync transaction per
        # destination. All rows become conservatively SUBMITTING immediately before the
        # concurrent network wave; a restart will reconcile and never resend them.
        batch_claimed = False
        begin_many = getattr(self.store, 'begin_entry_submissions', None)
        if submit_items and callable(begin_many):
            if time.time() > deadline or not admitted():
                for rule, _alloc, attempt in submit_items:
                    self.store.transition_entry_attempt(
                        attempt.attempt_key, ('PREPARED',), 'ABORTED',
                        last_error='entry admission closed before submission wave',
                    )
                    result_by_account[rule.account_id] = {
                        'account_id': rule.account_id, 'status': 'ABORTED',
                        'reason': 'entry admission closed before submission wave',
                    }
                submit_items = []
            else:
                submit_started = time.time()
                try:
                    claimed_wave = begin_many(
                        [attempt.attempt_key for _rule, _alloc, attempt in submit_items],
                        submit_started_epoch=submit_started,
                    )
                except Exception as exc:
                    reason = f'durable entry-wave claim failed before PLACE: {exc}'
                    self.store.trip_entry_circuit(
                        reason=reason, event_key=event_key,
                        outcome='PERSISTENCE_FAILURE',
                    )
                    for rule, _alloc, _attempt in submit_items:
                        result_by_account[rule.account_id] = {
                            'account_id': rule.account_id, 'status': 'ERROR',
                            'reason': reason,
                        }
                    submit_items = []
                else:
                    if claimed_wave is None:
                        reason = 'durable entry wave was claimed by a safety event'
                        for rule, _alloc, attempt in submit_items:
                            # The all-or-none batch method changed no row. Abort any rows
                            # that remain PREPARED; a safety owner may already own others.
                            self.store.transition_entry_attempt(
                                attempt.attempt_key, ('PREPARED',), 'ABORTED',
                                last_error=reason,
                            )
                            result_by_account[rule.account_id] = {
                                'account_id': rule.account_id, 'status': 'ABORTED',
                                'reason': reason,
                            }
                        submit_items = []
                    else:
                        submit_items = [
                            (rule, alloc, claimed_wave[attempt.attempt_key])
                            for rule, alloc, attempt in submit_items
                        ]
                        batch_claimed = True

        async def _submit_one(rule: AccountRule, alloc: Allocation,
                              attempt: EntryAttempt) -> dict[str, Any]:
            async with semaphore:
                if time.time() > deadline or not admitted():
                    self.store.transition_entry_attempt(
                        attempt.attempt_key,
                        ('SUBMITTING',) if batch_claimed else ('PREPARED',),
                        'ABORTED',
                        last_error='entry admission closed before PLACE',
                    )
                    return {'account_id': rule.account_id, 'status': 'ABORTED',
                            'reason': 'entry admission closed before PLACE'}
                if batch_claimed:
                    claimed = attempt
                    submit_started = float(
                        claimed.submit_started_at_epoch or time.time()
                    )
                else:
                    submit_started = time.time()
                    claimed = self.store.transition_entry_attempt(
                        attempt.attempt_key, ('PREPARED',), 'SUBMITTING',
                        submit_started_at_epoch=submit_started,
                    )
                    if claimed is None:
                        return {'account_id': rule.account_id, 'status': 'ABORTED',
                                'reason': 'durable attempt was claimed by a safety event'}
                try:
                    if alloc.engine == 'CORE':
                        fn = getattr(self.executor, 'submit_core', self.executor.place_core)
                        receipt = await fn(
                            rule.crosstrade_account, alloc, attempt.custom_order_id,
                            deadline_epoch=deadline, admission_guard=admitted,
                        )
                    else:
                        fn = getattr(self.executor, 'submit_single', self.executor.place_single)
                        receipt = await fn(
                            rule.crosstrade_account, alloc, attempt.custom_order_id,
                            deadline_epoch=deadline, admission_guard=admitted,
                        )
                except AcceptedOrderError as exc:
                    receipt = exc.receipt
                    accepted_at = time.time()
                    persisted = self.store.transition_entry_attempt(
                        attempt.attempt_key, ('SUBMITTING',), 'ACCEPTED',
                        parent_order_id=receipt.parent_order_id,
                        target_order_ids=list(receipt.target_order_ids),
                        stop_order_ids=list(receipt.stop_order_ids),
                        child_order_ids=list(receipt.child_order_ids),
                        accepted_at_epoch=accepted_at,
                        next_reconcile_at_epoch=accepted_at,
                        last_error=str(exc),
                    )
                    if persisted is None:
                        return {'account_id': rule.account_id, 'status': 'ERROR',
                                'reason': 'accepted exposure could not be persisted'}
                    return await self._flatten_attempt_once(persisted, str(exc))
                except EntryAdmissionClosed as exc:
                    self.store.transition_entry_attempt(
                        attempt.attempt_key, ('SUBMITTING',), 'ABORTED',
                        last_error=str(exc),
                    )
                    return {'account_id': rule.account_id, 'status': 'ABORTED',
                            'reason': str(exc)}
                except InstrumentContractError as exc:
                    wave_abort.set()
                    self.store.transition_entry_attempt(
                        attempt.attempt_key, ('SUBMITTING',), 'FAILED',
                        last_error=str(exc),
                    )
                    return {'account_id': rule.account_id, 'status': 'ERROR',
                            'reason': str(exc), 'global_instrument_error': True}
                except (CrossTradeError, ProtectionFailure, ValueError) as exc:
                    current = self.store.get_entry_attempt(attempt.attempt_key)
                    # A late acceptance can appear after an immediate flatten. Preserve the
                    # durable SUBMITTING state, open the circuit, and let the reconciler watch
                    # fill-reconciled position state. Never resend PLACE.
                    if isinstance(exc, AmbiguousMutation) and current is not None:
                        self.store.transition_entry_attempt(
                            attempt.attempt_key, ('SUBMITTING',), 'SUBMITTING',
                            last_error=f'ambiguous PLACE; no resend: {exc}',
                            next_reconcile_at_epoch=time.time(),
                        )
                        self.store.trip_entry_circuit(
                            reason=f'{rule.account_id}: ambiguous PLACE; reconciliation required',
                            event_key=attempt.attempt_key, outcome='AMBIGUOUS_PLACE',
                        )
                        return {'account_id': rule.account_id, 'status': 'ERROR',
                                'reason': f'ambiguous PLACE; no resend: {exc}'}
                    self.store.transition_entry_attempt(
                        attempt.attempt_key, ('SUBMITTING',), 'FAILED',
                        last_error=str(exc),
                    )
                    return {'account_id': rule.account_id, 'status': 'ERROR',
                            'reason': str(exc)}

                accepted_at = time.time()
                persisted = self.store.transition_entry_attempt(
                    attempt.attempt_key, ('SUBMITTING',), 'ACCEPTED',
                    parent_order_id=receipt.parent_order_id,
                    target_order_ids=list(receipt.target_order_ids),
                    stop_order_ids=list(receipt.stop_order_ids),
                    child_order_ids=list(receipt.child_order_ids),
                    accepted_at_epoch=accepted_at,
                    next_reconcile_at_epoch=accepted_at,
                    last_error='',
                )
                if persisted is None:
                    self.store.trip_entry_circuit(
                        reason=f'{rule.account_id}: accepted exposure persistence CAS failed',
                        event_key=event_key, outcome='PERSISTENCE_FAILURE',
                    )
                    return {'account_id': rule.account_id, 'status': 'ERROR',
                            'reason': 'accepted exposure persistence CAS failed'}
                accepted_times.append(accepted_at)
                fence = self._crossed_control_fence(
                    event.plan.engine, submit_started, accepted_at
                )
                if fence is not None:
                    return await self._flatten_attempt_once(
                        persisted, 'PLACE accepted after EXIT/HARD_FLAT control fence'
                    )
                return {
                    'account_id': rule.account_id, 'status': 'ACCEPTED',
                    'qty': alloc.qty, 'tp1_qty': alloc.tp1_qty,
                    'runner_qty': alloc.runner_qty, 'stop': alloc.stop,
                    'tp1': alloc.tp1, 'tp2': alloc.tp2,
                    'verification': 'ASYNC_PENDING',
                }

        async def submit(rule: AccountRule, alloc: Allocation,
                         attempt: EntryAttempt) -> dict[str, Any]:
            """Contain every per-account failure so sibling PLACE tasks are always awaited."""
            try:
                return await _submit_one(rule, alloc, attempt)
            except asyncio.CancelledError:
                # Process shutdown preserves PREPARED/SUBMITTING for startup recovery.
                raise
            except Exception as exc:
                detail = (
                    f'unexpected entry submit failure ({type(exc).__name__}): {exc}'
                )
                # If broker mutation may have started, never mark the row terminal and never
                # resend PLACE. Reconciliation owns the ambiguous SUBMITTING/ACCEPTED state.
                try:
                    current = self.store.get_entry_attempt(attempt.attempt_key)
                except Exception:
                    current = None
                if current is not None and current.state == 'SUBMITTING':
                    try:
                        self.store.transition_entry_attempt(
                            attempt.attempt_key, ('SUBMITTING',), 'SUBMITTING',
                            last_error=detail, next_reconcile_at_epoch=time.time(),
                        )
                    except Exception:
                        pass
                elif current is not None and current.state == 'PREPARED':
                    try:
                        self.store.transition_entry_attempt(
                            attempt.attempt_key, ('PREPARED',), 'FAILED',
                            last_error=detail,
                        )
                    except Exception:
                        pass
                try:
                    self.store.trip_entry_circuit(
                        reason=f'{rule.account_id}: {detail}',
                        event_key=attempt.attempt_key,
                        outcome='UNEXPECTED_SUBMIT_FAILURE',
                    )
                except Exception:
                    pass
                return {'account_id': rule.account_id, 'status': 'ERROR',
                        'reason': detail}

        wave_marker = getattr(self, 'entry_wave_active', None)
        if wave_marker is None:
            wave_marker = asyncio.Event()
            self.entry_wave_active = wave_marker
        wave_marker.set()
        try:
            submitted_rows = await asyncio.gather(
                *(submit(rule, alloc, attempt) for rule, alloc, attempt in submit_items)
            )
        finally:
            wave_marker.clear()
        global_instrument_error: str | None = None
        for row in submitted_rows:
            if row.pop('global_instrument_error', False):
                global_instrument_error = str(row.get('reason') or '')
            result_by_account[row['account_id']] = row
        if global_instrument_error:
            for rule, _alloc, attempt in submit_items:
                if rule.account_id in result_by_account:
                    continue
                self.store.transition_entry_attempt(
                    attempt.attempt_key, ('PREPARED',), 'ABORTED',
                    last_error='global instrument contract failure',
                )
                result_by_account[rule.account_id] = {
                    'account_id': rule.account_id, 'status': 'ERROR',
                    'reason': 'global instrument contract failure; fanout aborted: '
                              + global_instrument_error,
                }

        rows = [result_by_account[rule.account_id] for rule in accounts]
        errors = [r for r in rows if r.get('status') in {'ERROR', 'FLATTENED'}]
        if errors:
            common = str(errors[0].get('reason') or errors[0].get('status'))
            self.store.trip_entry_circuit(
                reason=f'entry wave requires review: {common}', event_key=event_key,
                outcome='PARTIAL_OR_FAILED_ENTRY',
            )
        out = {
            'kind': 'ENTRY', 'engine': event.plan.engine, 'results': rows,
            'dispatch': {
                'ingress_to_completion_ms': round((time.time() - receipt_epoch) * 1000, 1),
                'accepted_accounts': len(accepted_times),
                'acceptance_spread_ms': (
                    round((max(accepted_times) - min(accepted_times)) * 1000, 1)
                    if len(accepted_times) > 1 else 0.0
                ),
                'verification': 'asynchronous',
            },
        }
        if global_instrument_error:
            out['global_execution_error'] = global_instrument_error
        return out

    def _schedule_entry_reconcile(self, attempt: EntryAttempt, error: str) -> None:
        count = int(attempt.reconcile_attempts) + 1
        base = float(getattr(self.settings, 'ENTRY_RECONCILE_BASE_DELAY_SECONDS', 0.75))
        maximum = float(getattr(self.settings, 'ENTRY_RECONCILE_MAX_DELAY_SECONDS', 5.0))
        delay = min(maximum, base * (2 ** min(count - 1, 8)))
        self.store.transition_entry_attempt(
            attempt.attempt_key, (attempt.state,), attempt.state,
            reconcile_attempts=count, next_reconcile_at_epoch=time.time() + delay,
            last_error=str(error)[:2000],
        )

    def _core_normalization_is_current(self, attempt: EntryAttempt) -> bool:
        latest = self.store.get_entry_attempt(attempt.attempt_key)
        if latest is None or latest.state != 'ACCEPTED':
            return False
        accepted = float(latest.accepted_at_epoch or latest.created_at_epoch)
        # Control fences are committed at webhook ingress, before the control worker reads
        # broker state. Stop mutating bracket children as soon as an EXIT/HARD_FLAT wins.
        return not self.store.entry_aborted_after(latest.engine, accepted)

    async def _normalize_core_attempt(self, attempt: EntryAttempt,
                                      timeout: float) -> dict[str, Any]:
        current = self.store.get_entry_attempt(attempt.attempt_key)
        if current is None or current.state != 'ACCEPTED':
            return {'account_id': attempt.account_id,
                    'status': 'NORMALIZATION_SUPERSEDED',
                    'attempt_state': current.state if current else 'MISSING'}
        alloc = self._attempt_allocation(current)
        try:
            finalized = await self.executor.finalize_core(
                current.crosstrade_account, alloc,
                set(current.preexisting_order_ids),
                still_current=lambda: self._core_normalization_is_current(current),
            )
        except NormalizationSuperseded:
            latest = self.store.get_entry_attempt(current.attempt_key)
            return {'account_id': current.account_id,
                    'status': 'NORMALIZATION_SUPERSEDED',
                    'attempt_state': latest.state if latest else 'MISSING'}
        except NormalizationMutationUnconfirmed as exc:
            latest = self.store.get_entry_attempt(current.attempt_key)
            if latest is None or latest.state != 'ACCEPTED':
                return {'account_id': current.account_id,
                        'status': 'NORMALIZATION_SUPERSEDED',
                        'attempt_state': latest.state if latest else 'MISSING'}
            reason = f'Core exact bracket CHANGE unconfirmed: {exc}'
            row = await self._flatten_attempt_once(latest, reason)
            self.store.delete_cached_account_state(current.account_id)
            if row.get('status') == 'FLATTENED':
                self.store.trip_entry_circuit(
                    reason=f'{current.account_id}: {reason}; fail-safe flatten completed',
                    event_key=current.attempt_key,
                    outcome='CORE_NORMALIZATION_MUTATION_UNCONFIRMED',
                )
            elif row.get('status') == 'ERROR':
                self.store.trip_entry_circuit_for_attempt(
                    current.attempt_key, ('FLATTENING',),
                    reason=f'{current.account_id}: {reason}',
                    outcome='CORE_NORMALIZATION_MUTATION_UNCONFIRMED',
                )
            return row
        except Exception as exc:
            latest = self.store.get_entry_attempt(current.attempt_key)
            if latest is None or latest.state != 'ACCEPTED':
                return {'account_id': current.account_id,
                        'status': 'NORMALIZATION_SUPERSEDED',
                        'attempt_state': latest.state if latest else 'MISSING'}
            age = time.time() - float(
                latest.accepted_at_epoch or latest.created_at_epoch
            )
            if age >= timeout:
                reason = f'Core exact bracket discovery timed out: {exc}'
                row = await self._flatten_attempt_once(latest, reason)
                self.store.delete_cached_account_state(current.account_id)
                if row.get('status') == 'FLATTENED':
                    self.store.trip_entry_circuit(
                        reason=f'{current.account_id}: {reason}; fail-safe flatten completed',
                        event_key=current.attempt_key,
                        outcome='CORE_NORMALIZATION_TIMEOUT',
                    )
                elif row.get('status') == 'ERROR':
                    self.store.trip_entry_circuit_for_attempt(
                        current.attempt_key, ('FLATTENING',),
                        reason=f'{current.account_id}: {reason}',
                        outcome='CORE_NORMALIZATION_TIMEOUT',
                    )
                return row
            scheduled = self.store.transition_entry_attempt(
                latest.attempt_key, ('ACCEPTED',), 'ACCEPTED',
                reconcile_attempts=int(latest.reconcile_attempts) + 1,
                next_reconcile_at_epoch=time.time() + min(
                    float(getattr(self.settings, 'ENTRY_RECONCILE_MAX_DELAY_SECONDS', 5.0)),
                    float(getattr(self.settings, 'ENTRY_RECONCILE_BASE_DELAY_SECONDS', 0.75))
                    * (2 ** min(int(latest.reconcile_attempts), 8)),
                ),
                last_error=str(exc)[:2000],
            )
            if scheduled is None:
                advanced = self.store.get_entry_attempt(current.attempt_key)
                return {'account_id': current.account_id,
                        'status': 'NORMALIZATION_SUPERSEDED',
                        'attempt_state': advanced.state if advanced else 'MISSING'}
            return {'account_id': current.account_id,
                    'status': 'CORE_NORMALIZATION_PENDING', 'reason': str(exc)}

        updated = self.store.transition_entry_attempt(
            current.attempt_key, ('ACCEPTED',), 'ACCEPTED',
            target_order_ids=list(finalized.target_order_ids),
            stop_order_ids=list(finalized.stop_order_ids),
            child_order_ids=list(finalized.child_order_ids),
            last_error='', next_reconcile_at_epoch=0.0,
        )
        if updated is None:
            advanced = self.store.get_entry_attempt(current.attempt_key)
            return {'account_id': current.account_id,
                    'status': 'NORMALIZATION_SUPERSEDED',
                    'attempt_state': advanced.state if advanced else 'MISSING'}
        return {'account_id': current.account_id, 'status': 'CORE_NORMALIZED',
                'child_orders': len(updated.child_order_ids)}

    async def reconcile_entry_attempts(self) -> dict:
        """Serialize safety-critical readback across the periodic and management loops."""
        lock = getattr(self, '_entry_reconcile_lock', None)
        if lock is None:
            lock = asyncio.Lock()
            self._entry_reconcile_lock = lock
        async with lock:
            return await self._reconcile_entry_attempts_once()

    async def _reconcile_entry_attempts_once(self) -> dict:
        """Confirm accepted exposure off the submit path using fill-reconciled state."""
        now_epoch = time.time()
        timeout = float(getattr(self.settings, 'ENTRY_RECONCILE_TIMEOUT_SECONDS', 30.0))
        rule_map = {rule.account_id: rule for rule in self.accounts}
        results: list[dict[str, Any]] = []
        attempts = self.store.all_entry_attempts(
            ('PREPARED', 'SUBMITTING', 'ACCEPTED', 'FLATTENING')
        )
        due_core = [
            attempt for attempt in attempts
            if (attempt.state == 'ACCEPTED' and attempt.engine == 'CORE'
                and not attempt.stop_order_ids
                and (not attempt.next_reconcile_at_epoch
                     or attempt.next_reconcile_at_epoch <= now_epoch))
        ]
        core_handled: set[str] = set()
        if due_core:
            concurrency = max(1, int(getattr(
                self.settings, 'CORE_NORMALIZATION_MAX_CONCURRENCY', 8
            )))
            semaphore = asyncio.Semaphore(concurrency)

            async def normalize(attempt: EntryAttempt):
                async with semaphore:
                    return await self._normalize_core_attempt(attempt, timeout)

            normalized = await asyncio.gather(*(normalize(a) for a in due_core))
            results.extend(normalized)
            core_handled = {
                attempt.attempt_key for attempt, row in zip(due_core, normalized)
                if row.get('status') != 'CORE_NORMALIZED'
            }
            # EXIT and normalization both use compare-and-swap. Reload authoritative rows
            # before any fill/protection decision instead of continuing from stale objects.
            attempts = self.store.all_entry_attempts(
                ('PREPARED', 'SUBMITTING', 'ACCEPTED', 'FLATTENING')
            )
        for attempt in attempts:
            if attempt.attempt_key in core_handled:
                continue
            if attempt.next_reconcile_at_epoch and attempt.next_reconcile_at_epoch > now_epoch:
                continue
            if attempt.state == 'PREPARED':
                receipt_epoch = float(
                    attempt.entry_receipt_epoch or attempt.created_at_epoch
                )
                max_age = float(getattr(
                    self.settings, 'ENTRY_SIGNAL_MAX_AGE_SECONDS', 8.0
                ))
                if now_epoch > receipt_epoch + max_age:
                    aborted = self.store.transition_entry_attempt(
                        attempt.attempt_key, ('PREPARED',), 'ABORTED',
                        last_error='prepared entry expired before PLACE',
                    )
                    if aborted is not None:
                        results.append({
                            'account_id': attempt.account_id,
                            'status': 'ABORTED_STALE_PREPARED',
                        })
                continue
            rule = rule_map.get(attempt.account_id)
            if rule is None:
                self.store.trip_entry_circuit(
                    reason=f'{attempt.account_id}: unresolved attempt has no configured account',
                    event_key=attempt.attempt_key, outcome='UNKNOWN_ACCOUNT',
                )
                results.append({'account_id': attempt.account_id, 'status': 'UNKNOWN_ACCOUNT'})
                continue
            age = now_epoch - float(attempt.accepted_at_epoch or attempt.created_at_epoch)

            if attempt.state == 'FLATTENING':
                row = await self._settle_flattening_attempt(
                    attempt, attempt.last_error or 'flatten reconciliation'
                )
                results.append(row)
                continue

            if attempt.state == 'SUBMITTING':
                # The process may have lost the PLACE response. Never resend. Watch the
                # fill-reconciled position and flatten if exposure appears.
                try:
                    signed = await self.position_qty(rule)
                except Exception as exc:
                    self._schedule_entry_reconcile(attempt, str(exc))
                    self.store.trip_entry_circuit(
                        reason=f'{attempt.account_id}: ambiguous PLACE state unreadable: {exc}',
                        event_key=attempt.attempt_key, outcome='AMBIGUOUS_PLACE',
                    )
                    results.append({'account_id': attempt.account_id, 'status': 'UNKNOWN',
                                    'reason': str(exc)})
                    continue
                if signed or age >= timeout:
                    row = await self._flatten_attempt_once(
                        attempt, 'ambiguous PLACE recovery; no automatic resend'
                    )
                    self.store.trip_entry_circuit(
                        reason=f'{attempt.account_id}: ambiguous PLACE required flatten recovery',
                        event_key=attempt.attempt_key, outcome='AMBIGUOUS_PLACE',
                    )
                    self.store.delete_cached_account_state(attempt.account_id)
                    results.append(row)
                else:
                    self._schedule_entry_reconcile(
                        attempt, 'awaiting ambiguity window before scoped flatten'
                    )
                    results.append({'account_id': attempt.account_id,
                                    'status': 'AMBIGUOUS_PENDING'})
                continue

            fence = None
            if (attempt.submit_started_at_epoch is not None
                    and attempt.accepted_at_epoch is not None):
                fence = self._crossed_control_fence(
                    attempt.engine, attempt.submit_started_at_epoch,
                    attempt.accepted_at_epoch,
                )
            if fence is not None:
                row = await self._flatten_attempt_once(
                    attempt, 'accepted entry crossed EXIT/HARD_FLAT control fence'
                )
                self.store.delete_cached_account_state(attempt.account_id)
                results.append(row)
                continue

            current = self.store.get_entry_attempt(attempt.attempt_key) or attempt
            alloc = self._attempt_allocation(current)
            if current.engine == 'CORE' and not current.stop_order_ids:
                # Not due yet or superseded during this pass. The dedicated concurrent
                # phase above owns Core normalization; never fall back to serial work.
                continue

            if not current.stop_order_ids:
                row = await self._flatten_attempt_once(
                    current, 'accepted entry has no owned protective stop identity'
                )
                self.store.delete_cached_account_state(current.account_id)
                self.store.trip_entry_circuit(
                    reason=f'{current.account_id}: accepted entry missing stop identity',
                    event_key=current.attempt_key, outcome='UNPROTECTED_ACCEPTANCE',
                )
                results.append(row)
                continue

            try:
                fill_proof: dict[str, Any] | None = None
                if current.engine == 'CORE':
                    position = await self.position_proof(rule)
                    fill_qty = abs(int(position['signed_qty']))
                    actual_entry = position.get('net_price')
                else:
                    fill_proof = await self.entry_fill_proof(current.parent_order_id)
                    fill_qty = int(fill_proof['qty'])
                    actual_entry = fill_proof.get('price')
                    position = await self.position_proof(
                        rule,
                        expected_instrument=str(fill_proof.get('instrument') or ''),
                        expected_contract_id=str(fill_proof.get('contract_id') or ''),
                    )
                signed = int(position['signed_qty'])
            except Exception as exc:
                self._schedule_entry_reconcile(current, str(exc))
                if age >= timeout:
                    # The broker-returned OSO stop remains the safety boundary. A read outage
                    # opens the circuit but does not itself justify a market flatten.
                    self.store.trip_entry_circuit(
                        reason=f'{current.account_id}: accepted entry readback timed out: {exc}',
                        event_key=current.attempt_key, outcome='READBACK_UNCONFIRMED',
                    )
                results.append({'account_id': current.account_id,
                                'status': 'READBACK_PENDING', 'reason': str(exc)})
                continue

            if signed == 0:
                if fill_qty == current.qty:
                    results.append(await self._finalize_observed_flat_attempt(
                        rule, current,
                        new_state='CLOSED',
                        status='CLOSED_BEFORE_PROMOTION',
                        reason=('accepted entry completed and destination position '
                                'was observed flat'),
                        actual_entry=actual_entry,
                    ))
                    continue
                if fill_qty == 0 and current.parent_order_id:
                    try:
                        payload = await self.client.order_status(
                            current.crosstrade_account, current.parent_order_id
                        )
                        data = payload.get('data', payload) if isinstance(payload, dict) else {}
                        status = str((data or {}).get('status') or
                                     (data or {}).get('ordStatus') or '').lower()
                    except Exception:
                        status = ''
                    if status in {'rejected', 'canceled', 'cancelled', 'expired'}:
                        row = await self._finalize_observed_flat_attempt(
                            rule, current,
                            new_state='FAILED',
                            status='FAILED',
                            reason=f'entry ended {status} without a fill',
                        )
                        if row.get('status') == 'FAILED':
                            row['reason'] = status
                            self.store.trip_entry_circuit(
                                reason=(f'{current.account_id}: entry {status} '
                                        'after acceptance'),
                                event_key=current.attempt_key,
                                outcome='LATE_REJECTION',
                            )
                        results.append(row)
                        continue
                if age >= timeout:
                    reason = (
                        f'accepted entry fill timed out while flat: fillQty={fill_qty} '
                        f'expected={current.qty}'
                    )
                    row = await self._flatten_attempt_once(current, reason)
                    self.store.trip_entry_circuit(
                        reason=f'{current.account_id}: {reason}',
                        event_key=current.attempt_key,
                        outcome='FILL_TIMEOUT',
                    )
                    results.append(row)
                    continue
                self._schedule_entry_reconcile(current, 'accepted entry not filled yet')
                results.append({'account_id': current.account_id,
                                'status': 'FILL_PENDING'})
                continue

            expected_sign = 1 if current.side == 'LONG' else -1
            wrong_side = (signed > 0) != (expected_sign > 0)
            complete = (abs(signed) == current.qty and fill_qty == current.qty
                        and actual_entry is not None)
            if wrong_side or (not complete and age >= timeout):
                reason = (f'post-entry mismatch netPos={signed} fillQty={fill_qty} '
                          f'expected={expected_sign * current.qty}')
                row = await self._flatten_attempt_once(current, reason)
                self.store.delete_cached_account_state(current.account_id)
                self.store.trip_entry_circuit(
                    reason=f'{current.account_id}: {reason}', event_key=current.attempt_key,
                    outcome='POSITION_MISMATCH',
                )
                results.append(row)
                continue
            if not complete:
                self._schedule_entry_reconcile(
                    current, f'partial reconciliation netPos={signed} fillQty={fill_qty}'
                )
                results.append({'account_id': current.account_id,
                                'status': 'PARTIAL_FILL_PENDING'})
                continue

            if current.engine != 'CORE':
                try:
                    working = await self.executor.working_orders(
                        current.crosstrade_account
                    )
                    protected = verify_single_bracket(
                        working, alloc, set(current.child_order_ids)
                    )
                except Exception as exc:
                    self._schedule_entry_reconcile(current, str(exc))
                    if age >= timeout:
                        self.store.trip_entry_circuit(
                            reason=(f'{current.account_id}: protective-stop readback timed out: '
                                    f'{exc}'),
                            event_key=current.attempt_key,
                            outcome='PROTECTION_READBACK_UNCONFIRMED',
                        )
                    results.append({'account_id': current.account_id,
                                    'status': 'PROTECTION_READBACK_PENDING',
                                    'reason': str(exc)})
                    continue
                if not protected:
                    if age >= timeout:
                        row = await self._flatten_attempt_once(
                            current, 'native OSO protection did not prove exact live coverage'
                        )
                        self.store.trip_entry_circuit(
                            reason=f'{current.account_id}: exact protective coverage missing',
                            event_key=current.attempt_key,
                            outcome='PROTECTION_MISMATCH',
                        )
                        results.append(row)
                    else:
                        self._schedule_entry_reconcile(
                            current, 'waiting for native OSO children to become live'
                        )
                        results.append({'account_id': current.account_id,
                                        'status': 'PROTECTION_ACTIVATION_PENDING'})
                    continue

            trade = ActiveTrade(
                account_id=current.account_id, event_id=current.event_id,
                engine=current.engine, side=current.side, entry=float(actual_entry),
                initial_stop=current.stop, current_stop=current.stop,
                tp1=current.tp1, tp2=current.tp2, total_qty=current.qty,
                tp1_qty=current.tp1_qty, runner_qty=current.runner_qty,
                current_position_qty=abs(signed), previous_position_qty=abs(signed),
                entry_native_bar_index=current.entry_native_bar_index,
                stop_order_ids=list(current.stop_order_ids),
                target_order_ids=list(current.target_order_ids),
                parent_order_id=current.parent_order_id,
                custom_order_id=current.custom_order_id,
            )
            if self.store.promote_entry_attempt(current.attempt_key, trade):
                if current.engine == 'ORG':
                    self.store.save_org_attempt(current.account_id, {
                        'event_id': current.event_id,
                        'stop_order_ids': list(current.stop_order_ids),
                        'custom_order_id': current.custom_order_id,
                        'created_at': datetime.now(timezone.utc).isoformat(),
                    })
                results.append({'account_id': current.account_id, 'status': 'ACTIVE',
                                'qty': current.qty, 'entry': actual_entry})
        return {'kind': 'ENTRY_RECONCILE', 'results': results}

    async def _change_trade_stop(self, rule: AccountRule, trade: ActiveTrade, new_stop: float) -> None:
        signed = await self.position_qty(rule)
        if signed == 0:
            row = await self._finalize_observed_flat_trade(
                rule, trade,
                status='CLOSED',
                reason='management observed destination position flat',
            )
            if row.get('status') == 'ERROR':
                raise ProtectionFailure(str(row.get('reason') or
                                            'owned-order cleanup unconfirmed'))
            return
        expected_sign = 1 if trade.side == 'LONG' else -1
        if (signed > 0) != (expected_sign > 0):
            row = await self._hard_flat_rule_once(
                rule, trade, 'position side mismatch during management'
            )
            if row.get('status') == 'FLATTENED':
                raise ProtectionFailure(
                    'position side mismatch during management; flat proven'
                )
            raise ProtectionFailure(str(row.get('reason') or 'flatten unconfirmed'))
        qty = abs(signed)
        live_stop_ids = await self.executor.change_stop_orders(rule.crosstrade_account,
                                                               trade.stop_order_ids,
                                                               new_stop, qty)
        trade.previous_position_qty = trade.current_position_qty
        trade.current_position_qty = qty
        trade.current_stop = new_stop
        trade.stop_order_ids = live_stop_ids
        self.store.save_trade(trade)

    async def market_pulse(self, event: ParsedEvent) -> dict:
        if event.pulse is None:
            raise ValueError('MARKET_PULSE missing pulse')
        attempts_fn = getattr(self.store, 'all_entry_attempts', None)
        unresolved = (attempts_fn(('PREPARED', 'SUBMITTING', 'ACCEPTED', 'FLATTENING'))
                      if attempts_fn is not None else [])
        if any(a.engine == 'CORE' for a in unresolved):
            return {'kind': 'MARKET_PULSE', 'deferred': True,
                    'reason': 'CORE entry is awaiting broker reconciliation'}
        rule_map = {a.account_id: a for a in self.accounts}
        results = []
        for trade in self.store.all_trades():
            if trade.engine != 'CORE':
                continue
            rule = rule_map.get(trade.account_id)
            if rule is None or not rule.enabled:
                continue
            try:
                signed = await self.position_qty(rule)
                if signed == 0:
                    results.append(await self._finalize_observed_flat_trade(
                        rule, trade,
                        status='CLOSED',
                        reason='CORE market pulse observed destination position flat',
                    ))
                    continue
                if (trade.side == 'LONG' and signed < 0) or (trade.side == 'SHORT' and signed > 0):
                    row = await self._hard_flat_rule_once(
                        rule, trade, 'CORE side mismatch'
                    )
                    row.setdefault('reason', 'side mismatch')
                    results.append(row)
                    continue
                trade.previous_position_qty = trade.current_position_qty
                trade.current_position_qty = abs(signed)
                decision = core_on_market_pulse(trade, event.pulse)
                if decision.tp1_transition:
                    trade.tp1_filled = True
                if decision.reason == 'PRE_TP1_50_HALF_STOP':
                    trade.stop_stage = max(trade.stop_stage, 1)
                elif decision.reason == 'PRE_TP1_75_TO_BE':
                    trade.stop_stage = max(trade.stop_stage, 2)
                if decision.new_stop is not None:
                    live_stop_ids = await self.executor.change_stop_orders(rule.crosstrade_account,
                                                                           trade.stop_order_ids,
                                                                           decision.new_stop,
                                                                           trade.current_position_qty)
                    trade.current_stop = decision.new_stop
                    trade.stop_order_ids = live_stop_ids
                    self.store.save_trade(trade)
                    results.append({'account_id': rule.account_id, 'status': 'STOP_MOVED',
                                    'stop': decision.new_stop, 'reason': decision.reason,
                                    'position_qty': trade.current_position_qty})
                else:
                    self.store.save_trade(trade)
                    results.append({'account_id': rule.account_id, 'status': 'NO_CHANGE',
                                    'position_qty': trade.current_position_qty})
            except (CrossTradeError, StateUnverified, ProtectionFailure) as exc:
                row = await self._hard_flat_rule_once(
                    rule, trade, f'CORE management failure: {exc}'
                )
                row.setdefault('reason', str(exc))
                results.append(row)
        out = {'kind': 'MARKET_PULSE', 'results': results}
        if any(row.get('status') == 'ERROR' for row in results):
            out.update({
                'deferred': True,
                'reason': 'CORE management remains unconfirmed; retrying flat proof',
            })
        return out

    async def silver_stop(self, event: ParsedEvent, *, event_key: str = '') -> dict:
        """Persist the highest requested stage; a dedicated loop owns broker mutation.

        The webhook inbox is therefore completed exactly once instead of accumulating
        hundreds of retries while an accepted entry is still becoming ACTIVE.
        """
        fields = event.fields or {}
        try:
            stage = int(float(fields.get('STAGE', '0')))
        except (TypeError, ValueError) as exc:
            raise ProtectionFailure('Silver staged stop event missing valid STAGE') from exc
        if stage not in {1, 2}:
            raise ProtectionFailure(f'unsupported Silver protection stage {stage}')
        record = getattr(self.store, 'record_silver_stop_intent', None)
        if record is None:
            # Compatibility for isolated unit fakes. Production Store always supplies the
            # durable intent API.
            return await self._apply_silver_stage(stage)
        intent = record(stage, event_key)
        wakeup = getattr(self, 'silver_management_wakeup', None)
        if wakeup is not None:
            wakeup.set()
        return {
            'kind': 'SILVER_STOP_MOVE',
            'status': 'INTENT_RECORDED',
            'stage': int(intent['stage']),
            'coalesced': int(intent['stage']) > stage,
        }

    async def _apply_silver_stage(self, stage: int) -> dict:
        """Apply one stage to every ACTIVE Silver destination independently."""
        rule_map = {a.account_id: a for a in self.accounts}
        results = []
        for trade in self.store.all_trades():
            if trade.engine != 'SILVER':
                continue
            rule = rule_map.get(trade.account_id)
            if rule is None or not rule.enabled:
                continue
            if int(trade.stop_stage or 0) >= int(stage):
                results.append({
                    'account_id': rule.account_id, 'status': 'NO_CHANGE',
                    'stage': stage,
                    'reason': f'SILVER_STAGE_{stage}_ALREADY_APPLIED',
                })
                continue
            try:
                signed = await self.position_qty(rule)
                if signed == 0:
                    results.append(await self._finalize_observed_flat_trade(
                        rule, trade,
                        status='CLOSED',
                        reason='SILVER stop event observed destination position flat',
                    ))
                    continue
                trade.previous_position_qty = trade.current_position_qty
                trade.current_position_qty = abs(signed)
                decision = silver_lock_stop(trade, stage)
                if decision.new_stop is None:
                    trade.stop_stage = max(int(trade.stop_stage or 0), stage)
                    self.store.save_trade(trade)
                    results.append({'account_id': rule.account_id, 'status': 'NO_CHANGE',
                                    'stage': stage, 'reason': decision.reason})
                    continue
                live_stop_ids = await self.executor.change_stop_orders(rule.crosstrade_account,
                                                                       trade.stop_order_ids,
                                                                       decision.new_stop,
                                                                       trade.current_position_qty)
                trade.current_stop = decision.new_stop
                trade.stop_stage = max(int(trade.stop_stage or 0), stage)
                trade.stop_order_ids = live_stop_ids
                self.store.save_trade(trade)
                results.append({'account_id': rule.account_id, 'status': 'STOP_MOVED',
                                'stage': stage, 'stop': decision.new_stop, 'reason': decision.reason})
            except (CrossTradeError, StateUnverified, ProtectionFailure) as exc:
                row = await self._hard_flat_rule_once(
                    rule, trade, f'SILVER management failure: {exc}'
                )
                row.setdefault('reason', str(exc))
                results.append(row)
        out = {'kind': 'SILVER_STOP_SERVICE', 'stage': stage, 'results': results}
        if any(row.get('status') == 'ERROR' for row in results):
            out.update({
                'deferred': True,
                'reason': 'SILVER management remains unconfirmed; durable intent retained',
            })
        return out

    async def service_silver_stop_intent(self) -> dict:
        """Promote eligible entries, manage ACTIVE accounts, and retain work until proven."""
        get_intent = getattr(self.store, 'silver_stop_intent', None)
        if get_intent is None:
            return {'kind': 'SILVER_STOP_SERVICE', 'status': 'UNSUPPORTED'}
        intent = get_intent()
        if not intent:
            return {'kind': 'SILVER_STOP_SERVICE', 'status': 'IDLE'}
        now = time.time()
        if float(intent.get('next_attempt_at_epoch') or 0.0) > now:
            return {'kind': 'SILVER_STOP_SERVICE', 'status': 'BACKOFF'}
        stage = int(intent['stage'])
        reconciliation_error = ''
        attempts_fn = getattr(self.store, 'all_entry_attempts', None)
        unresolved = (attempts_fn(('PREPARED', 'SUBMITTING', 'ACCEPTED', 'FLATTENING'))
                      if attempts_fn is not None else [])
        if any(a.engine == 'SILVER' for a in unresolved):
            try:
                await self.reconcile_entry_attempts()
            except Exception as exc:
                # Existing ACTIVE destinations must still receive their stage even when
                # another account's readback is temporarily unavailable.
                reconciliation_error = str(exc)

        applied = await self._apply_silver_stage(stage)
        unresolved = (attempts_fn(('PREPARED', 'SUBMITTING', 'ACCEPTED', 'FLATTENING'))
                      if attempts_fn is not None else [])
        silver_unresolved = [a for a in unresolved if a.engine == 'SILVER']
        behind = [
            trade for trade in self.store.all_trades()
            if trade.engine == 'SILVER' and int(trade.stop_stage or 0) < stage
        ]
        errors = [
            row for row in applied.get('results', []) if row.get('status') == 'ERROR'
        ]
        if silver_unresolved or behind or errors or reconciliation_error:
            reasons = []
            if silver_unresolved:
                reasons.append(f'{len(silver_unresolved)} Silver entry attempt(s) unresolved')
            if behind:
                reasons.append(f'{len(behind)} active Silver trade(s) below stage {stage}')
            if errors:
                reasons.append(f'{len(errors)} destination management error(s)')
            if reconciliation_error:
                reasons.append(f'reconciliation error: {reconciliation_error}')
            attempts = int(intent.get('attempts') or 0)
            base = float(getattr(
                self.settings, 'SILVER_MANAGEMENT_RETRY_BASE_SECONDS', 0.5
            ))
            maximum = float(getattr(
                self.settings, 'SILVER_MANAGEMENT_RETRY_MAX_SECONDS', 5.0
            ))
            delay = min(maximum, base * (2 ** min(attempts, 4)))
            self.store.defer_silver_stop_intent(stage, '; '.join(reasons), delay)
            return {
                **applied, 'status': 'PENDING', 'reason': '; '.join(reasons),
            }
        cleared = self.store.clear_silver_stop_intent(stage)
        return {**applied, 'status': 'COMPLETED' if cleared else 'SUPERSEDED'}

    async def _hard_flat_rule_once(self, rule: AccountRule,
                                   trade: ActiveTrade | None,
                                   reason: str) -> dict:
        """Flatten one configured account, retaining ownership until flat is proven."""
        flatten_error: Exception | None = None
        try:
            await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
        except Exception as exc:
            flatten_error = exc

        cancellation_errors: list[str] = []
        if trade is not None:
            owned_ids = dict.fromkeys([
                trade.parent_order_id,
                *trade.target_order_ids,
                *trade.stop_order_ids,
            ])
            for oid in owned_ids:
                if not oid:
                    continue
                try:
                    await self._cancel_order_if_live(rule.crosstrade_account, oid)
                except Exception as exc:
                    cancellation_errors.append(f'{oid}: {exc}')

        proof_error: Exception | None = None
        signed: int | None = None
        try:
            signed = await self.position_qty(rule)
        except Exception as exc:
            proof_error = exc

        if signed == 0 and proof_error is None and not cancellation_errors:
            self.store.delete_trade(rule.account_id)
            self.store.delete_cached_account_state(rule.account_id)
            return {'account_id': rule.account_id, 'status': 'FLATTENED'}

        details: list[str] = []
        if signed:
            details.append(f'fill-reconciled netPos remains {signed}')
        if proof_error is not None:
            details.append(f'plural-position proof failed: {proof_error}')
        if cancellation_errors:
            details.append('owned-order cleanup failed: ' + '; '.join(cancellation_errors))
        if flatten_error is not None:
            details.append(f'flatten request error: {flatten_error}')
        detail = '; '.join(details) or 'flat state remains unconfirmed'
        self.store.trip_entry_circuit(
            reason=f'{rule.account_id}: {reason} unconfirmed: {detail}',
            event_key='', outcome='FLATTEN_UNCONFIRMED',
        )
        return {'account_id': rule.account_id, 'status': 'ERROR',
                'reason': f'{reason} unconfirmed: {detail}'}

    async def hard_flat(self, event: ParsedEvent) -> dict:
        results = []
        entry_attempt_handled: set[str] = set()
        for attempt in list(self.store.all_entry_attempts(
            ('PREPARED', 'SUBMITTING', 'ACCEPTED', 'FLATTENING')
        )):
            if (event.engine not in {'', 'GLOBAL', 'ACCOUNT'}
                    and attempt.engine != event.engine):
                continue
            if attempt.state == 'PREPARED':
                self.store.transition_entry_attempt(
                    attempt.attempt_key, ('PREPARED',), 'ABORTED',
                    last_error='aborted by HARD_FLAT before PLACE',
                )
                results.append({'account_id': attempt.account_id,
                                'status': 'ABORTED_PREPARED_ENTRY'})
            elif attempt.state == 'SUBMITTING':
                entry_attempt_handled.add(attempt.account_id)
                # The ingress fence prevents not-yet-sent PLACE and the submitter flattens
                # any response accepted after the fence. Do not race an in-flight request.
                results.append({'account_id': attempt.account_id,
                                'status': 'FENCED_IN_FLIGHT_ENTRY'})
            elif attempt.state == 'ACCEPTED':
                entry_attempt_handled.add(attempt.account_id)
                results.append(await self._flatten_attempt_once(
                    attempt, 'HARD_FLAT safety control'
                ))
            else:
                entry_attempt_handled.add(attempt.account_id)
                results.append(await self._settle_flattening_attempt(
                    attempt, attempt.last_error or 'HARD_FLAT safety control'
                ))
        asw_handled: set[str] = set()
        if event.engine in {'', 'GLOBAL', 'ACCOUNT', 'ASW'}:
            for pending in list(self.store.all_asw_pending()):
                asw_handled.add(pending.account_id)
                try:
                    await self._flatten_asw_owned(pending, 'HARD_FLAT')
                    results.append({'account_id': pending.account_id, 'status': 'FLATTENED_ASW_PENDING'})
                except Exception as exc:
                    results.append({'account_id': pending.account_id, 'status': 'ERROR', 'reason': str(exc)})
            rule_map = {a.account_id: a for a in self.accounts}
            for trade in list(self.store.all_trades()):
                if trade.engine != 'ASW' or trade.account_id in asw_handled:
                    continue
                rule = rule_map.get(trade.account_id)
                if rule is None:
                    continue
                asw_handled.add(trade.account_id)
                try:
                    row = await self._hard_flat_rule_once(
                        rule, trade, 'HARD_FLAT ASW active trade'
                    )
                    if row.get('status') == 'FLATTENED':
                        row['status'] = 'FLATTENED_ASW_ACTIVE'
                    results.append(row)
                except Exception as exc:
                    results.append({'account_id': trade.account_id, 'status': 'ERROR', 'reason': str(exc)})
        for rule in self.accounts:
            if rule.account_id in asw_handled or rule.account_id in entry_attempt_handled:
                continue
            trade = self.store.get_trade(rule.account_id)
            # Engine-scoped required-flat/time-exit only acts on matching active trades.
            if event.engine not in {'', 'GLOBAL', 'ACCOUNT'} and trade is not None and trade.engine != event.engine:
                continue
            if event.engine not in {'', 'GLOBAL', 'ACCOUNT'} and trade is None:
                continue
            try:
                results.append(await self._hard_flat_rule_once(
                    rule, trade, 'HARD_FLAT safety control'
                ))
            except Exception as exc:
                results.append({'account_id': rule.account_id, 'status': 'ERROR', 'reason': str(exc)})
        out = {'kind': 'HARD_FLAT', 'engine': event.engine, 'results': results}
        unresolved_statuses = {
            'ERROR', 'FENCED_IN_FLIGHT_ENTRY', 'FLATTEN_ALREADY_CLAIMED',
        }
        if any(row.get('status') in unresolved_statuses for row in results):
            out.update({
                'deferred': True,
                'reason': 'HARD_FLAT remains unconfirmed; retrying plural flat proof',
            })
        return out

    async def ordinary_exit(self, event: ParsedEvent) -> dict:
        # Broker-hosted destination targets/stops own normal exits. Source-account exits must
        # not prematurely close a destination whose account-specific target/runner differs.
        rule_map = {a.account_id: a for a in self.accounts}
        attempts = self.store.all_entry_attempts(
            ('PREPARED', 'SUBMITTING', 'ACCEPTED', 'FLATTENING')
        )

        async def reconcile_attempt(attempt: EntryAttempt) -> dict[str, Any] | None:
            if (event.engine and event.engine not in {'GLOBAL', 'ACCOUNT'}
                    and attempt.engine != event.engine):
                return None
            rule = rule_map.get(attempt.account_id)
            if rule is None:
                return None
            if attempt.state == 'PREPARED':
                self.store.transition_entry_attempt(
                    attempt.attempt_key, ('PREPARED',), 'ABORTED',
                    last_error='ordinary EXIT arrived before PLACE',
                )
                return {'account_id': attempt.account_id,
                        'status': 'ABORTED_PREPARED_ENTRY'}
            if attempt.state == 'SUBMITTING':
                return {'account_id': attempt.account_id,
                        'status': 'FENCED_IN_FLIGHT_ENTRY'}
            if attempt.state == 'FLATTENING':
                return {'account_id': attempt.account_id,
                        'status': 'FLATTEN_RECONCILIATION_PENDING'}
            try:
                signed = await self.position_qty(rule)
                if signed == 0:
                    return await self._finalize_observed_flat_attempt(
                        rule, attempt,
                        new_state='CLOSED',
                        status='CLOSED_RECONCILED',
                        reason='ordinary EXIT observed destination position flat',
                    )
                return {'account_id': attempt.account_id,
                        'status': 'STILL_OPEN_DESTINATION_MANAGED',
                        'position_qty': abs(signed)}
            except (CrossTradeError, StateUnverified) as exc:
                return {'account_id': attempt.account_id,
                        'status': 'UNKNOWN', 'reason': str(exc)}

        trades = self.store.all_trades()

        async def reconcile_trade(trade: ActiveTrade) -> dict[str, Any] | None:
            if event.engine and event.engine not in {'GLOBAL','ACCOUNT'} and trade.engine != event.engine:
                return None
            rule = rule_map.get(trade.account_id)
            if rule is None:
                return None
            try:
                signed = await self.position_qty(rule)
                if signed == 0:
                    return await self._finalize_observed_flat_trade(
                        rule, trade,
                        status='CLOSED_RECONCILED',
                        reason='ordinary EXIT observed destination position flat',
                    )
                return {'account_id': rule.account_id,
                        'status': 'STILL_OPEN_DESTINATION_MANAGED',
                        'position_qty': abs(signed)}
            except (CrossTradeError, StateUnverified) as exc:
                return {'account_id': rule.account_id,
                        'status': 'UNKNOWN', 'reason': str(exc)}

        # EXIT is a control fence. Query independent destination accounts together so one
        # slow balance refresh cannot leave later accounts in stale ACCEPTED ownership for
        # minutes. CrossTradeClient's safe-GET scheduler still enforces broker pacing.
        rows = await asyncio.gather(
            *(reconcile_attempt(attempt) for attempt in attempts),
            *(reconcile_trade(trade) for trade in trades),
        )
        results = [row for row in rows if row is not None]
        out = {'kind': 'EXIT', 'results': results}
        unresolved_statuses = {
            'ERROR', 'UNKNOWN', 'FENCED_IN_FLIGHT_ENTRY',
            'FLATTEN_RECONCILIATION_PENDING',
        }
        if any(row.get('status') in unresolved_statuses for row in results):
            out.update({
                'deferred': True,
                'reason': 'EXIT ownership/position state remains unconfirmed; retrying',
            })
        return out
