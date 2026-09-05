from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone

from config import Settings
from crosstrade import CrossTradeClient
from models import AccountConfig, RouteDecision, RouteResponse, TradeSignal, SignalEvent
from persistence import Store
from risk import contract_risk_from_geometry, size_trade, consistency_adjust
from state import AccountStateCache


class AutoPropRouter:
    def __init__(self, settings: Settings, accounts: list[AccountConfig], client: CrossTradeClient, cache: AccountStateCache, store: Store):
        self.settings, self.accounts, self.client, self.cache, self.store = settings, accounts, client, cache, store

    def _contract_risk(self, signal: TradeSignal) -> float:
        if signal.contract_risk_dollars is not None: return float(signal.contract_risk_dollars)
        assert signal.entry is not None and signal.stop is not None
        return contract_risk_from_geometry(signal.entry, signal.stop, self.settings.mnq_point_value, self.settings.modeled_round_trip_cost)

    def _execution_symbol(self, signal: TradeSignal) -> str:
        return signal.execution_symbol or self.settings.default_execution_symbol or signal.instrument

    def _split_qty(self, signal: TradeSignal, qty: int) -> tuple[int,int]:
        if qty < 2: return qty, 0
        src_total = signal.source_qty or ((signal.tp1_qty or 0) + (signal.runner_qty or 0))
        src_runner = signal.runner_qty or 0
        if src_total and src_runner > 0:
            runner = int(math.floor(qty * src_runner / src_total + 0.5))
            runner = min(qty - 1, max(1, runner))
            return qty - runner, runner
        if qty >= signal.min_runner_qty:
            runner = qty // 2
            return qty - runner, runner
        return qty, 0

    def _atm_for_decision(self, signal: TradeSignal, d: RouteDecision) -> dict | None:
        if signal.entry is None or signal.stop is None or d.routed_tp1 is None: return None
        tick = self.settings.mnq_tick_size
        stop_ticks = max(1, round(abs(signal.entry - signal.stop) / tick))
        tp1_ticks = max(1, round(abs(d.routed_tp1 - signal.entry) / tick))
        first, runner = d.routed_tp1_qty, d.routed_runner_qty
        if runner <= 0 or d.routed_tp2 is None:
            return {"atm_targets": str(tp1_ticks), "atm_stops": str(stop_ticks), "atm_qtys": str(d.quantity)}
        tp2_ticks = max(1, round(abs(d.routed_tp2 - signal.entry) / tick))
        use_be = self.settings.native_atm_breakeven_after_tp1 or signal.breakeven_after_tp1
        return {
            "atm_targets": f"{tp1_ticks},{tp2_ticks}", "atm_stops": str(stop_ticks),
            "atm_qtys": f"{first},{runner}",
            "atm_breakeven": 1 if use_be else None,
            "atm_breakeven_offset": str(signal.breakeven_offset_ticks) if use_be else None,
        }

    async def route(self, signal: TradeSignal, *, mutate: bool | None = None, claim: bool = True) -> RouteResponse:
        t0 = time.perf_counter()
        live = self.settings.live_enabled if mutate is None else bool(mutate and self.settings.live_enabled)
        if claim and not self.store.claim_event(signal.dedupe_key(), signal.trade_id, signal.event.value, signal.model_dump(mode="json")):
            return RouteResponse(trade_id=signal.trade_id, event=signal.event.value, mode=self.settings.execution_mode,
                management_mode=self.settings.management_mode, router_processing_ms=(time.perf_counter()-t0)*1000,
                decisions=[RouteDecision(account_id="*",account_name="*",eligible=False,reason="duplicate event blocked")])

        states = await self.cache.snapshot(); configs = await self.cache.configs_snapshot()
        if signal.event == SignalEvent.ENTRY:
            return await self._entry(signal, states, configs, t0, live)
        return await self._manage(signal, states, configs, t0, live)

    async def _entry(self, signal, states, configs, t0, live):
        decisions=[]; executable=[]; now=datetime.now(timezone.utc)
        contract_risk=self._contract_risk(signal)
        for a in configs:
            s=states.get(a.id)
            if not a.enabled:
                decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,reason="account not enrolled in AutoProp Router")); continue
            if not a.rules_verified:
                decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,reason="unclassified account: rule profile not verified")); continue
            if not a.risk_ready:
                decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,reason="one-time MLL bootstrap required")); continue
            if self.settings.canary_enabled and a.id != self.settings.canary_account_id:
                decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,reason="canary mode: not selected account")); continue
            if s is None or s.source_updated_at is None:
                decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,reason="no hot account snapshot")); continue
            age=(now-s.source_updated_at).total_seconds(); age_ms=age*1000
            if age > self.settings.max_state_age_seconds:
                decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,reason=f"stale state ({age:.1f}s)",cache_age_ms=age_ms,cushion=s.cushion,mll_floor=s.mll_floor)); continue
            if s.balance is None and s.net_liq is None:
                decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,reason="snapshot has no usable balance",cache_age_ms=age_ms)); continue
            if not s.is_flat:
                decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,reason="hot-cache guard: account not flat",cache_age_ms=age_ms)); continue
            if s.has_working_orders:
                decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,reason="hot-cache guard: working orders exist",cache_age_ms=age_ms)); continue
            rr=size_trade(a,s,contract_risk)
            qty,tp1,tp2,room=consistency_adjust(a=a,s=s,pre_qty=rr.quantity,entry=signal.entry,tp1=signal.tp1,tp2=signal.tp2,
                point_value=self.settings.mnq_point_value,tick_size=self.settings.mnq_tick_size,min_runner_qty=signal.min_runner_qty,
                min_profit_room=self.settings.consistency_min_profit_room)
            if self.settings.canary_enabled: qty=min(qty,max(1,self.settings.canary_max_qty))
            elif self.settings.live_max_qty_per_account > 0: qty=min(qty,self.settings.live_max_qty_per_account)
            if qty <= 0:
                decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,reason=rr.reason+" -> 0 contracts",risk_budget=rr.budget,contract_risk=rr.contract_risk,cushion=s.cushion,mll_floor=s.mll_floor,cache_age_ms=age_ms,consistency_room=room)); continue
            first,runner=self._split_qty(signal,qty)
            d=RouteDecision(account_id=a.id,account_name=s.account_name,eligible=True,reason=rr.reason,quantity=qty,risk_budget=rr.budget,
                contract_risk=rr.contract_risk,cushion=s.cushion,mll_floor=s.mll_floor,cache_age_ms=age_ms,routed_tp1=tp1,routed_tp2=tp2,
                routed_tp1_qty=first,routed_runner_qty=runner,consistency_room=room)
            decisions.append(d); executable.append((a,d))
        if not live: return self._finish(signal,t0,decisions)

        symbol=self._execution_symbol(signal); action="buy" if signal.direction in {"LONG","BUY"} else "sell"
        async def send(a,d):
            try:
                kwargs=dict(account=d.account_name,instrument=symbol,action=action,qty=d.quantity,
                    order_id=f"AP-{signal.trade_id}-{a.id}"[:64],
                    require_market_position="flat" if self.settings.use_crosstrade_position_gate else None,
                    max_positions=1 if self.settings.use_crosstrade_max_positions_gate else None)
                if self.settings.management_mode == "native_atm":
                    atm=self._atm_for_decision(signal,d)
                    if self.settings.execution_require_protective_stop and atm is None:
                        d.eligible=False; d.reason="live blocked: native ATM needs entry/stop/target geometry"; return
                    kwargs.update(atm or {})
                elif self.settings.management_mode == "tv_managed":
                    if self.settings.execution_require_protective_stop and signal.stop is None:
                        d.eligible=False; d.reason="live blocked: broker catastrophic stop required"; return
                    kwargs["stop_loss"]=signal.stop
                else:
                    d.eligible=False; d.reason=f"invalid AUTOPROP_MANAGEMENT_MODE={self.settings.management_mode}"; return
                d.execution_result=await self.client.place_order(**kwargs)
                self.store.upsert_allocation(trade_id=signal.trade_id,account_id=a.id,account_name=d.account_name,
                    instrument=symbol,direction=signal.direction or action,management_mode=self.settings.management_mode,
                    initial_qty=d.quantity,remaining_qty=d.quantity,tp1_qty=d.routed_tp1_qty,runner_qty=d.routed_runner_qty,
                    entry=signal.entry,stop=signal.stop,tp1=d.routed_tp1,tp2=d.routed_tp2,status="submitted")
            except Exception as exc:
                d.eligible=False; d.reason=f"CrossTrade rejected/error: {exc!r}"
        await asyncio.gather(*(send(a,d) for a,d in executable))
        return self._finish(signal,t0,decisions)

    async def _manage(self, signal, states, configs, t0, live):
        decisions=[]; jobs=[]; symbol=self._execution_symbol(signal)
        # Native ATM owns normal stop/target lifecycle. Only force-flat is routed,
        # avoiding double exits from TradingView order-fill alerts.
        if self.settings.management_mode == "native_atm" and signal.event != SignalEvent.FLATTEN:
            for a in configs:
                if a.enabled:
                    decisions.append(RouteDecision(account_id=a.id,account_name=a.account_name,eligible=False,
                        reason=f"{signal.event.value} ignored: Tradovate native ATM owns normal exits"))
            return self._finish(signal,t0,decisions)

        for a in configs:
            if not a.enabled: continue
            if self.settings.canary_enabled and a.id != self.settings.canary_account_id: continue
            s=states.get(a.id); name=s.account_name if s else a.account_name
            alloc=self.store.get_active_allocation(a.id,symbol)
            if signal.event == SignalEvent.FLATTEN:
                d=RouteDecision(account_id=a.id,account_name=name,eligible=True,reason="force-flat allowed even when entry risk state is not ready")
                decisions.append(d); jobs.append((a,d,alloc)); continue
            if self.settings.management_mode != "tv_managed": continue
            # For a normal exit/stop change, require evidence the Router owns a current
            # AutoProp lifecycle. This prevents touching unrelated manual positions.
            if alloc is None:
                decisions.append(RouteDecision(account_id=a.id,account_name=name,eligible=False,reason="no active AutoProp allocation for this account/instrument")); continue
            d=RouteDecision(account_id=a.id,account_name=name,eligible=True,reason=f"tv-managed {signal.event.value}",quantity=int(alloc['remaining_qty']))
            decisions.append(d); jobs.append((a,d,alloc))

        if not live: return self._finish(signal,t0,decisions)

        async def manage_one(a,d,alloc):
            try:
                if signal.event == SignalEvent.FLATTEN:
                    d.execution_result=await self.client.flatten_position(account=d.account_name,instrument=symbol)
                    if alloc: self.store.update_allocation(alloc['trade_id'],a.id,remaining_qty=0,status='closed')
                    return
                assert alloc is not None
                direction=str(alloc['direction']).upper(); protected_action='buy' if direction in {'LONG','BUY'} else 'sell'
                remaining=int(alloc['remaining_qty'])
                if signal.event == SignalEvent.STOP_MOVE:
                    d.execution_result=await self.client.cancel_and_bracket(account=d.account_name,instrument=symbol,action=protected_action,qty=remaining,stop_loss=signal.stop)
                    self.store.update_allocation(alloc['trade_id'],a.id,stop=signal.stop)
                elif signal.event == SignalEvent.PARTIAL_EXIT:
                    close_qty=int(alloc['tp1_qty'] or 0)
                    if signal.exit_qty and signal.source_qty:
                        close_qty=max(1,int(math.floor(int(alloc['initial_qty'])*signal.exit_qty/signal.source_qty+0.5)))
                    close_qty=min(max(1,close_qty),remaining)
                    close_result=await self.client.close_position(account=d.account_name,instrument=symbol,qty=close_qty)
                    new_remaining=max(0,remaining-close_qty)
                    protect_result=None
                    if new_remaining > 0:
                        new_stop=signal.stop if signal.stop is not None else float(alloc['stop'])
                        protect_result=await self.client.cancel_and_bracket(account=d.account_name,instrument=symbol,action=protected_action,qty=new_remaining,stop_loss=new_stop)
                        self.store.update_allocation(alloc['trade_id'],a.id,remaining_qty=new_remaining,stop=new_stop,status='active')
                    else:
                        self.store.update_allocation(alloc['trade_id'],a.id,remaining_qty=0,status='closed')
                    d.execution_result={'close':close_result,'reprotect':protect_result,'remaining_qty':new_remaining}
                elif signal.event == SignalEvent.EXIT:
                    d.execution_result=await self.client.flatten_position(account=d.account_name,instrument=symbol)
                    self.store.update_allocation(alloc['trade_id'],a.id,remaining_qty=0,status='closed')
            except Exception as exc:
                d.eligible=False; d.reason=f"CrossTrade management rejected/error: {exc!r}"
        await asyncio.gather(*(manage_one(a,d,alloc) for a,d,alloc in jobs))
        return self._finish(signal,t0,decisions)

    def _finish(self, signal, t0, decisions):
        r=RouteResponse(trade_id=signal.trade_id,event=signal.event.value,
            mode=(self.settings.execution_mode if self.settings.live_enabled else "shadow"),
            management_mode=self.settings.management_mode,router_processing_ms=(time.perf_counter()-t0)*1000,decisions=decisions)
        self.store.log_route(signal.trade_id,r.model_dump(mode="json")); return r
