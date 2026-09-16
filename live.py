from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from allocation import allocate, allocate_asw, AllocationBlocked
from crosstrade import AmbiguousMutation, CrossTradeClient, CrossTradeError, InstrumentContractError, normalize_tradovate_symbol
from events import ParsedEvent
from execution import Executor, ProtectionFailure, verify_single_bracket
from management import core_on_market_pulse, silver_lock_stop
from models import AccountRule, AccountState, ActiveTrade, Allocation, AswPending
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

    async def _cancel_asw_owned_orders(self, pending: AswPending, *, include_children: bool) -> None:
        await self._cancel_order_if_live(pending.crosstrade_account, pending.parent_order_id)
        if include_children:
            for oid in pending.child_order_ids:
                await self._cancel_order_if_live(pending.crosstrade_account, oid)

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
        try:
            await self._cancel_order_if_live(pending.crosstrade_account, pending.parent_order_id)
        except Exception:
            pass
        # Never infer flat from a failed position read. A scoped Tradovate flatten is safe
        # when already flat and guarantees any ASW-owned exposure is removed.
        await self.client.flatten(pending.crosstrade_account, self.execution_symbol)
        for oid in pending.child_order_ids:
            try:
                await self._cancel_order_if_live(pending.crosstrade_account, oid)
            except Exception:
                pass
        self.store.delete_asw_pending(pending.account_id)
        trade = self.store.get_trade(pending.account_id)
        if trade is not None and trade.engine == 'ASW':
            self.store.delete_trade(pending.account_id)

    async def route_asw_working_limit(self, event: ParsedEvent) -> dict:
        if event.plan is None or event.plan.engine != 'ASW':
            raise ValueError('ASW_WORKING_LIMIT missing canonical ASW plan')
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        if event.plan.expiry_time_ms is None or event.plan.expiry_time_ms <= now_ms:
            return {'kind':'ASW_WORKING_LIMIT','engine':'ASW','results':[
                {'account_id':a.account_id,'status':'SKIP','reason':'ASW candidate expired before routing'}
                for a in self.accounts if a.enabled]}
        results = []
        global_instrument_error: str | None = None
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
                receipt = await self.executor.place_asw_limit(
                    rule.crosstrade_account, alloc, cid,
                    expiry_time_ms=int(event.plan.expiry_time_ms), now_ms=now_ms)
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
                results.append({'account_id':rule.account_id,'status':'PENDING_LIMIT','qty':alloc.qty,
                                'native_qty':int(event.plan.source_qty or 0),
                                'scale_multiple':alloc.qty / float(event.plan.source_qty or 1),
                                'entry':alloc.entry,'stop':alloc.stop,'tp1':alloc.tp1,
                                'expiry_time_ms':int(event.plan.expiry_time_ms)})
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
                await self._cancel_asw_owned_orders(pending, include_children=False)
                rule = next((a for a in self.accounts if a.account_id == pending.account_id), None)
                had_position = None
                if rule is not None:
                    try:
                        had_position = (await self.position_qty(rule)) != 0
                    except Exception:
                        had_position = None
                # Canonical Pine canceled an unfilled setup. Flatten the scoped instrument
                # defensively even if the position read is unavailable or races a fill.
                await self.client.flatten(pending.crosstrade_account, self.execution_symbol)
                for oid in pending.child_order_ids:
                    await self._cancel_order_if_live(pending.crosstrade_account, oid)
                self.store.delete_asw_pending(pending.account_id)
                trade=self.store.get_trade(pending.account_id)
                if trade is not None and trade.engine=='ASW': self.store.delete_trade(pending.account_id)
                results.append({'account_id':pending.account_id,'status':'CANCELED_FLATTENED' if had_position is not False else 'CANCELED_UNFILLED'})
            except (CrossTradeError, ProtectionFailure, StateUnverified) as exc:
                results.append({'account_id':pending.account_id,'status':'ERROR','reason':str(exc)})
        return {'kind':'ASW_CANCEL_PENDING','engine':'ASW','results':results}

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
                await self.client.flatten(rule.crosstrade_account,self.execution_symbol)
                # ASW owns a simple bracket; explicit order cancel avoids orphan exits.
                for oid in [*trade.target_order_ids,*trade.stop_order_ids]:
                    await self._cancel_order_if_live(rule.crosstrade_account,oid)
                self.store.delete_trade(trade.account_id)
                results.append({'account_id':trade.account_id,'status':'FLATTENED'})
            except Exception as exc:
                results.append({'account_id':trade.account_id,'status':'ERROR','reason':str(exc)})
        return {'kind':'ASW_TIME_FLAT','engine':'ASW','results':results}

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
                        for oid in pending.child_order_ids:
                            await self._cancel_order_if_live(pending.crosstrade_account,oid)
                        self.store.delete_asw_pending(pending.account_id)
                        results.append({'account_id':pending.account_id,'status':'EXPIRED_UNFILLED'})
                    continue

                if absq == 0:
                    if status in {'filled','completed'}:
                        # Entry and broker-hosted exit completed between reconciliations.
                        # Explicitly clear any still-live owned OCO leg before forgetting ownership.
                        for oid in pending.child_order_ids:
                            try:
                                await self._cancel_order_if_live(pending.crosstrade_account, oid)
                            except Exception:
                                pass
                        self.store.delete_asw_pending(pending.account_id)
                        results.append({'account_id':pending.account_id,'status':'CLOSED_BEFORE_RECONCILE'})
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
                    for oid in [*trade.target_order_ids, *trade.stop_order_ids]:
                        try:
                            await self._cancel_order_if_live(rule.crosstrade_account, oid)
                        except Exception:
                            pass
                    self.store.delete_trade(trade.account_id)
                    results.append({'account_id':trade.account_id,'status':'CLOSED_RECONCILED'})
            except Exception:
                pass
        return {'kind':'ASW_RECONCILE','results':results}

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
                fields = event.fields or {}
                try:
                    stage = int(float(fields.get('STAGE', '0')))
                except (TypeError, ValueError):
                    raise ProtectionFailure('Silver staged stop event missing valid STAGE')
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
                try:
                    await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
                finally:
                    self.store.delete_trade(rule.account_id)
                results.append({'account_id': rule.account_id, 'status': 'FLATTENED', 'reason': str(exc)})
        return {'kind': 'SILVER_STOP_MOVE', 'results': results}

    async def hard_flat(self, event: ParsedEvent) -> dict:
        results = []
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
                    await self.client.flatten(rule.crosstrade_account, self.execution_symbol)
                    for oid in [*trade.target_order_ids, *trade.stop_order_ids]:
                        await self._cancel_order_if_live(rule.crosstrade_account, oid)
                    self.store.delete_trade(trade.account_id)
                    results.append({'account_id': trade.account_id, 'status': 'FLATTENED_ASW_ACTIVE'})
                except Exception as exc:
                    results.append({'account_id': trade.account_id, 'status': 'ERROR', 'reason': str(exc)})
        for rule in self.accounts:
            if rule.account_id in asw_handled:
                continue
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
