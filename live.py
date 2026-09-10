from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from allocation import allocate, AllocationBlocked
from crosstrade import CrossTradeClient, CrossTradeError, InstrumentContractError, normalize_tradovate_symbol
from events import ParsedEvent
from execution import Executor, ProtectionFailure
from management import core_on_market_pulse, silver_lock_stop
from models import AccountRule, AccountState, ActiveTrade
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
        self.client = CrossTradeClient(
            settings.CROSSTRADE_BASE_URL, settings.CROSSTRADE_TOKEN,
            settings.REQUEST_TIMEOUT_SECONDS,
            settings.CROSSTRADE_RATE_LIMIT_PER_MINUTE,
            settings.CROSSTRADE_RATE_LIMIT_WINDOW_SECONDS,
            settings.CROSSTRADE_RATE_LIMIT_MAX_RETRIES,
            settings.CROSSTRADE_RATE_LIMIT_FALLBACK_SECONDS,
        )
        self.execution_symbol = normalize_tradovate_symbol(settings.DEFAULT_EXECUTION_SYMBOL or 'MNQ1!')
        self.executor = Executor(self.client,
                                 execution_symbol=self.execution_symbol,
                                 bracket_confirm_retries=settings.BRACKET_CONFIRM_RETRIES,
                                 bracket_confirm_delay=settings.BRACKET_CONFIRM_RETRY_DELAY_SECONDS,
                                 change_retries=settings.MANAGEMENT_CHANGE_RETRIES,
                                 change_delay=settings.MANAGEMENT_CHANGE_RETRY_DELAY_SECONDS)

    async def position_qty(self, rule: AccountRule) -> int:
        payload = await self.client.position(rule.crosstrade_account,
                                             self.execution_symbol)
        data = payload.get('data', payload)
        if not isinstance(data, dict) or data.get('netPos') is None:
            raise StateUnverified('fill-reconciled position response missing netPos')
        return int(float(data['netPos']))

    async def state_for(self, rule: AccountRule) -> AccountState:
        risk = self.store.get_risk_state(rule.account_id)
        return await refresh_account_state(self.client, rule, risk)

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

    async def entry_fill_price(self, parent_order_id: str | None, expected_qty: int) -> float:
        if not parent_order_id:
            raise ProtectionFailure('accepted entry missing Tradovate parent order id')
        for _ in range(self.settings.BRACKET_CONFIRM_RETRIES):
            payload = await self.client.fills_order(parent_order_id)
            rows = _rows(payload)
            seen = set()
            fills = []
            for r in rows:
                fid = str(r.get('id') or r.get('fillId') or r.get('executionId') or '')
                if fid and fid in seen:
                    continue
                if fid:
                    seen.add(fid)
                q = int(float(r.get('qty') or 0))
                px = float(r.get('price') or 0)
                if q > 0 and px > 0:
                    fills.append((q, px))
            qty = sum(q for q, _ in fills)
            if qty == int(expected_qty):
                return sum(q * px for q, px in fills) / qty
            if qty > int(expected_qty):
                raise ProtectionFailure(f'entry fills exceed routed qty: {qty}>{expected_qty}')
            await asyncio.sleep(self.settings.BRACKET_CONFIRM_RETRY_DELAY_SECONDS)
        raise ProtectionFailure('destination entry fill price/quantity not proven')

    async def route_entry(self, event: ParsedEvent) -> dict:
        if event.plan is None:
            raise ValueError('ENTRY missing canonical plan')
        now = datetime.now(timezone.utc)
        results = []
        global_instrument_error: str | None = None
        for rule in self.accounts:
            if not rule.enabled:
                results.append({'account_id': rule.account_id, 'status': 'SKIP', 'reason': 'disabled'})
                continue
            if global_instrument_error is not None:
                results.append({'account_id': rule.account_id, 'status': 'ERROR',
                                'reason': 'global instrument contract failure; fanout aborted before broker mutation: ' + global_instrument_error})
                continue
            try:
                await self.entry_gate(rule)
                state = await self.state_for(rule)
                if event.plan.engine == 'ORG' and event.plan.reentry:
                    prior = self.store.get_org_attempt(rule.account_id)
                    if not prior:
                        raise AllocationBlocked('ORG re-entry: no destination prior attempt')
                    fills = await self.org_history(rule)
                    if not prove_prior_org_stop(fills=fills,
                                                stop_order_ids=set(prior.get('stop_order_ids', []))):
                        raise AllocationBlocked('ORG re-entry: destination stop outcome not proven')
                alloc = allocate(event.plan, rule, state,
                                 max_state_age_seconds=self.settings.MAX_STATE_AGE_SECONDS,
                                 now=now)
                dedupe = f'{event.plan.event_id}:{ny_date(now)}:{rule.account_id}'
                if not self.store.claim_event(dedupe):
                    results.append({'account_id': rule.account_id, 'status': 'SKIP', 'reason': 'duplicate event'})
                    continue
                cid = self._custom_id(dedupe, rule.account_id)
                receipt = (await self.executor.place_core(rule.crosstrade_account, alloc, cid)
                           if alloc.engine == 'CORE' else
                           await self.executor.place_single(rule.crosstrade_account, alloc, cid))
                target_ids, stop_ids = await self.executor.owned_roles(rule.crosstrade_account,
                                                                        receipt.child_order_ids)
                if not stop_ids:
                    await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
                    raise ProtectionFailure('accepted entry has no proven owned protective stop')
                try:
                    actual_entry = await self.entry_fill_price(receipt.parent_order_id, alloc.qty)
                except (CrossTradeError, ProtectionFailure, ValueError) as exc:
                    # An accepted broker position may already exist. Never manage it from the
                    # canonical/planned Pine entry when the destination fill cannot be proven.
                    await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
                    raise ProtectionFailure(f'destination fill proof failed; flattened: {exc}') from exc
                signed = await self.position_qty(rule)
                if signed == 0 or (alloc.side == 'LONG' and signed < 0) or (alloc.side == 'SHORT' and signed > 0):
                    await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
                    raise ProtectionFailure(f'post-entry position mismatch netPos={signed}')
                pos_qty = abs(signed)
                if pos_qty != alloc.qty:
                    await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
                    raise ProtectionFailure(f'post-entry position qty {pos_qty} != routed qty {alloc.qty}')
                trade = ActiveTrade(account_id=rule.account_id, event_id=event.plan.event_id,
                                    engine=alloc.engine, side=alloc.side, entry=actual_entry,
                                    initial_stop=alloc.stop, current_stop=alloc.stop,
                                    tp1=alloc.tp1, tp2=alloc.tp2, total_qty=alloc.qty,
                                    tp1_qty=alloc.tp1_qty, runner_qty=alloc.runner_qty,
                                    current_position_qty=pos_qty, previous_position_qty=pos_qty,
                                    entry_native_bar_index=event.plan.native_bar_index,
                                    stop_order_ids=stop_ids, target_order_ids=target_ids,
                                    parent_order_id=receipt.parent_order_id, custom_order_id=cid)
                self.store.save_trade(trade)
                if alloc.engine == 'ORG' and not event.plan.reentry:
                    self.store.save_org_attempt(rule.account_id, {
                        'event_id': event.plan.event_id,
                        'stop_order_ids': stop_ids,
                        'custom_order_id': cid,
                        'created_at': now.isoformat(),
                    })
                results.append({'account_id': rule.account_id, 'status': 'ROUTED',
                                'qty': alloc.qty, 'tp1_qty': alloc.tp1_qty,
                                'runner_qty': alloc.runner_qty, 'stop': alloc.stop,
                                'tp1': alloc.tp1, 'tp2': alloc.tp2})
            except InstrumentContractError as exc:
                # A symbol translation/contract failure is global to this AutoProp MNQ fanout.
                # Stop after the first definitive failure rather than repeating the same bad
                # broker mutation across every account.  Do not retry the entry automatically.
                global_instrument_error = str(exc)
                results.append({'account_id': rule.account_id, 'status': 'ERROR', 'reason': global_instrument_error})
            except (CrossTradeError, ProtectionFailure, ValueError) as exc:
                results.append({'account_id': rule.account_id, 'status': 'ERROR', 'reason': str(exc)})
            except (AllocationBlocked, StateUnverified) as exc:
                results.append({'account_id': rule.account_id, 'status': 'SKIP', 'reason': str(exc)})
        out = {'kind': 'ENTRY', 'engine': event.plan.engine, 'results': results}
        if global_instrument_error is not None:
            out['global_execution_error'] = global_instrument_error
        return out

    async def _change_trade_stop(self, rule: AccountRule, trade: ActiveTrade, new_stop: float) -> None:
        signed = await self.position_qty(rule)
        if signed == 0:
            self.store.delete_trade(rule.account_id)
            return
        expected_sign = 1 if trade.side == 'LONG' else -1
        if (signed > 0) != (expected_sign > 0):
            await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
            self.store.delete_trade(rule.account_id)
            raise ProtectionFailure('position side mismatch during management; flattened')
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
                    self.store.delete_trade(rule.account_id)
                    results.append({'account_id': rule.account_id, 'status': 'CLOSED'})
                    continue
                if (trade.side == 'LONG' and signed < 0) or (trade.side == 'SHORT' and signed > 0):
                    await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
                    self.store.delete_trade(rule.account_id)
                    results.append({'account_id': rule.account_id, 'status': 'FLATTENED', 'reason': 'side mismatch'})
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
                try:
                    await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
                finally:
                    self.store.delete_trade(rule.account_id)
                results.append({'account_id': rule.account_id, 'status': 'FLATTENED', 'reason': str(exc)})
        return {'kind': 'MARKET_PULSE', 'results': results}

    async def silver_stop(self, event: ParsedEvent) -> dict:
        rule_map = {a.account_id: a for a in self.accounts}
        results = []
        for trade in self.store.all_trades():
            if trade.engine != 'SILVER':
                continue
            rule = rule_map.get(trade.account_id)
            if rule is None or not rule.enabled:
                continue
            try:
                signed = await self.position_qty(rule)
                if signed == 0:
                    self.store.delete_trade(rule.account_id)
                    results.append({'account_id': rule.account_id, 'status': 'CLOSED'})
                    continue
                trade.previous_position_qty = trade.current_position_qty
                trade.current_position_qty = abs(signed)
                decision = silver_lock_stop(trade)
                if decision.new_stop is None:
                    results.append({'account_id': rule.account_id, 'status': 'NO_CHANGE'})
                    continue
                live_stop_ids = await self.executor.change_stop_orders(rule.crosstrade_account,
                                                                       trade.stop_order_ids,
                                                                       decision.new_stop,
                                                                       trade.current_position_qty)
                trade.current_stop = decision.new_stop
                trade.stop_order_ids = live_stop_ids
                self.store.save_trade(trade)
                results.append({'account_id': rule.account_id, 'status': 'STOP_MOVED',
                                'stop': decision.new_stop, 'reason': decision.reason})
            except (CrossTradeError, StateUnverified, ProtectionFailure) as exc:
                try:
                    await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
                finally:
                    self.store.delete_trade(rule.account_id)
                results.append({'account_id': rule.account_id, 'status': 'FLATTENED', 'reason': str(exc)})
        return {'kind': 'SILVER_STOP_MOVE', 'results': results}

    async def hard_flat(self, event: ParsedEvent) -> dict:
        results = []
        for rule in self.accounts:
            if not rule.enabled:
                continue
            trade = self.store.get_trade(rule.account_id)
            # Engine-scoped required-flat/time-exit only acts on matching active trades.
            if event.engine not in {'', 'GLOBAL', 'ACCOUNT'} and trade is not None and trade.engine != event.engine:
                continue
            if event.engine not in {'', 'GLOBAL', 'ACCOUNT'} and trade is None:
                continue
            try:
                await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
                self.store.delete_trade(rule.account_id)
                results.append({'account_id': rule.account_id, 'status': 'FLATTENED'})
            except CrossTradeError as exc:
                results.append({'account_id': rule.account_id, 'status': 'ERROR', 'reason': str(exc)})
        return {'kind': 'HARD_FLAT', 'engine': event.engine, 'results': results}

    async def ordinary_exit(self, event: ParsedEvent) -> dict:
        # Broker-hosted destination targets/stops own normal exits. Source-account exits must
        # not prematurely close a destination whose account-specific target/runner differs.
        rule_map = {a.account_id: a for a in self.accounts}
        results = []
        for trade in self.store.all_trades():
            if event.engine and event.engine not in {'GLOBAL','ACCOUNT'} and trade.engine != event.engine:
                continue
            rule = rule_map.get(trade.account_id)
            if rule is None:
                continue
            try:
                signed = await self.position_qty(rule)
                if signed == 0:
                    self.store.delete_trade(rule.account_id)
                    results.append({'account_id': rule.account_id, 'status': 'CLOSED_RECONCILED'})
                else:
                    results.append({'account_id': rule.account_id, 'status': 'STILL_OPEN_DESTINATION_MANAGED',
                                    'position_qty': abs(signed)})
            except (CrossTradeError, StateUnverified) as exc:
                results.append({'account_id': rule.account_id, 'status': 'UNKNOWN', 'reason': str(exc)})
        return {'kind': 'EXIT', 'results': results}
