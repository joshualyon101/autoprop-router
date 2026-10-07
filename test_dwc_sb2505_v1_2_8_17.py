"""Offline-only contract, ownership, restart and execution regressions for DWC SB25/05."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

from dwc_protection import (DWC_STOP_CONTRACT, blocks_new_entry, dwc_lock_stop,
                            dwc_broker_stop, matches, record_intent, service_intents)
from events import parse_alert
from execution import Executor, NormalizationSuperseded
from live import LiveRouter
from models import AccountRule, ActiveTrade, EntryAttempt
from store import Store

SID = 1791266340000
ENTRY = (f"AUTOPROP_ICT_FUSION|DWC|ENTRY|SHORT|QTY=3|ENTRY=100|SL=110|TP=70"
         f"|GATE_Q=2|CONTRACT={DWC_STOP_CONTRACT}|DWC_ID={SID}")
MOVE = (f"AUTOPROP_ICT_FUSION|DWC|MOVE_STOP|SHORT|CONTRACT={DWC_STOP_CONTRACT}"
        f"|DWC_ID={SID}|STAGE=1|TRIGGER_R=2.50|LOCK_R=0.50|QTY=3|SL=95|T1={SID+60000}")


def rule(account="A", **updates):
    d = dict(account_id=account, crosstrade_account="broker-"+account, enabled=True,
             rules_verified=True, account_type="personal", starting_balance=100000,
             max_loss=0, max_contracts=50)
    d.update(updates)
    return AccountRule(**d)


def trade(account="A", **updates):
    d = dict(account_id=account, event_id="entry-"+account, engine="DWC", side="SHORT",
             entry=100., initial_stop=110., current_stop=110., tp1=70., total_qty=3,
             tp1_qty=3, runner_qty=0, current_position_qty=3, previous_position_qty=3,
             management_signal_time_ms=SID, management_contract=DWC_STOP_CONTRACT,
             stop_order_ids=["s-"+account], target_order_ids=["t-"+account],
             parent_order_id="p-"+account)
    d.update(updates)
    return ActiveTrade(**d)


def attempt(account="A", **updates):
    now = time.time()
    d = dict(attempt_key="entry-"+account+":"+account, account_id=account,
             crosstrade_account="broker-"+account, event_id="entry-"+account,
             engine="DWC", side="SHORT", qty=3, planned_entry=100., stop=110.,
             tp1=70., tp1_qty=3, runner_qty=0, custom_order_id="AP-"+account,
             created_at_epoch=now, updated_at_epoch=now, state="ACTIVE",
             management_signal_time_ms=SID, management_contract=DWC_STOP_CONTRACT,
             entry_receipt_epoch=now)
    d.update(updates)
    return EntryAttempt(**d)


class BrokerFake:
    def __init__(self):
        self.calls=[]; self.rows=[]; self.error=None; self.before_change=None
    async def change_stop_orders(self, account, ids, stop, qty, *, still_current=None):
        if self.before_change:
            self.before_change()
        if still_current is not None and not still_current():
            raise NormalizationSuperseded("old generation")
        self.calls.append((account,list(ids),stop,qty))
        if self.error:
            raise self.error
        return list(ids)
    async def working_orders(self, account):
        return list(self.rows)


def runtime(tmp_path):
    rt = LiveRouter.__new__(LiveRouter)
    rt.store = Store(str(tmp_path / "dwc.sqlite3"))
    rt.accounts = [rule()]
    rt.executor = BrokerFake()
    rt.dwc_management_wakeup = asyncio.Event()
    rt.dwc_management_lock = asyncio.Lock()
    rt.flat_calls=[]; rt.read_calls=[]; rt.signed={"A":-3}; rt.flatten_succeeds=True
    async def position(r):
        rt.read_calls.append(r.account_id)
        return rt.signed.get(r.account_id,-3)
    async def reconcile():
        return {}
    async def finalize(r,t,*,status,reason):
        assert rt.store.get_trade(r.account_id).event_id == t.event_id
        rt.store.delete_trade(r.account_id)
        return {"account_id":r.account_id,"status":status}
    async def flatten(r,t,reason):
        rt.flat_calls.append((r.account_id,t.event_id))
        if rt.flatten_succeeds:
            rt.store.delete_trade(r.account_id)
            return {"account_id":r.account_id,"status":"FLATTENED"}
        return {"account_id":r.account_id,"status":"ERROR"}
    rt.position_qty=position
    rt.reconcile_entry_attempts=reconcile
    rt._finalize_observed_flat_trade=finalize
    rt._hard_flat_rule_once=flatten
    rt._crossed_control_fence=lambda *args: None
    return rt


def due(store):
    for i in store.pending_dwc_stop_intents():
        store._update_dwc_stop_intent(i["signal_id"], {"next_attempt_at_epoch":0})


@pytest.mark.parametrize("kind,raw", [("ENTRY",ENTRY),("DWC_STOP_MOVE",MOVE),
    ("DWC_STOP_MOVE",MOVE.replace("|MOVE_STOP|","|STOP_MOVE|"))])
def test_valid_contract(kind,raw):
    event=parse_alert(raw)
    assert event.kind==kind
    if event.plan:
        assert event.plan.native_time_ms==SID
        assert event.plan.contract_version==DWC_STOP_CONTRACT


@pytest.mark.parametrize("old,new", [
    ("|SHORT|","|LONG|"), (DWC_STOP_CONTRACT,"WRONG"),
    (f"|DWC_ID={SID}",""), (f"DWC_ID={SID}","DWC_ID=0"),
    (f"DWC_ID={SID}","DWC_ID=1.5"), (f"DWC_ID={SID}","DWC_ID=inf"),
    ("STAGE=1","STAGE=2"), ("TRIGGER_R=2.50","TRIGGER_R=2.0"),
    ("LOCK_R=0.50","LOCK_R=1.0"), ("TRIGGER_R=2.50","TRIGGER_R=NaN"),
    ("LOCK_R=0.50","LOCK_R=inf"), ("QTY=3","QTY=0"), ("QTY=3","QTY=0.5"),
    ("SL=95","SL=-1"), ("SL=95","SL=nan"),
    (f"T1={SID+60000}",f"T1={SID-1}"),
    ("STAGE=1","STAGE=1|STAGE=1"),
])
def test_bad_movement_contract_rejected(old,new):
    with pytest.raises(ValueError): parse_alert(MOVE.replace(old,new))


@pytest.mark.parametrize("old,new",[("ENTRY=100","ENTRY=NaN"),
    ("TP=70","TP=inf"),("TP=70","TP=100"),("TP=70","TP=0"),
    ("SL=110","SL=90"),("QTY=3","QTY=-1"),
    ("GATE_Q=2","GATE_Q=2|GATE_Q=3")])
def test_bad_new_entry_contract_rejected(old,new):
    with pytest.raises(ValueError): parse_alert(ENTRY.replace(old,new))


def test_legacy_dwc_unchanged_and_not_management_eligible():
    e=parse_alert(ENTRY.split("|CONTRACT=")[0])
    assert e.plan.contract_version=="" and e.plan.native_time_ms is None
    t=trade(management_contract="",management_signal_time_ms=None)
    assert not matches(t,SID)


@pytest.mark.parametrize("entry,original,current,raw,broker",[
    (100,110,110,95,95), (100.25,110,110,95.375,95.5),
    (100.0,110.25,110.25,94.875,95), (100,110,94,94,94),
    (100,110,94.125,94.125,94),
])
def test_actual_fill_frozen_r_and_tick_transport(entry,original,current,raw,broker):
    t=trade(entry=entry,initial_stop=original,current_stop=current)
    assert dwc_lock_stop(t)==pytest.approx(raw)
    assert dwc_broker_stop(t)==pytest.approx(broker)
    assert dwc_broker_stop(t)<=current


@pytest.mark.parametrize("updates",[{"engine":"CORE"},{"side":"LONG"},
    {"entry":float("nan")},{"initial_stop":100},{"current_stop":float("inf")}])
def test_invalid_risk_geometry_rejected(updates):
    with pytest.raises(ValueError): dwc_lock_stop(trade(**updates))


def test_old_trade_and_attempt_json_load_with_safe_defaults(tmp_path):
    s=Store(str(tmp_path/"old.sqlite3"))
    t=trade().model_dump(exclude={"management_signal_time_ms","management_contract"})
    a=attempt().model_dump(exclude={"management_signal_time_ms","management_contract"})
    assert ActiveTrade.model_validate(t).management_contract==""
    assert EntryAttempt.model_validate(a).management_signal_time_ms is None
    s.save_trade(ActiveTrade.model_validate(t))
    s=Store(s.path)
    assert not matches(s.get_trade("A"),SID)


def test_intent_durable_duplicates_tombstone_and_entry_gate(tmp_path):
    s=Store(str(tmp_path/"x.sqlite3")); fields=parse_alert(MOVE).fields
    s.record_dwc_stop_intent(SID,"first",fields)
    s.record_dwc_stop_intent(SID,"second",fields)
    assert len(s.pending_dwc_stop_intents())==1
    assert s.get_dwc_stop_intent(SID)["event_key"]=="first"
    assert not blocks_new_entry(s,parse_alert(ENTRY))
    assert blocks_new_entry(s,parse_alert(ENTRY.replace(str(SID),str(SID+1000))))
    s=Store(s.path)
    assert s.dwc_stop_pending()
    s.complete_dwc_stop_intent(SID,"NO_MATCHING_OWNERSHIP")
    assert not s.record_dwc_stop_intent(SID,"replay",fields)["pending"]
    assert blocks_new_entry(s,parse_alert(ENTRY))  # no late generation resurrection
    assert not blocks_new_entry(s,parse_alert(ENTRY.replace(str(SID),str(SID+1000))))


def test_trade_save_cas_never_resurrects_or_loosens(tmp_path):
    s=Store(str(tmp_path/"x.sqlite3")); t=trade(); s.save_trade(t)
    p=t.model_copy(update={"current_stop":95,"stop_stage":1})
    assert s.save_dwc_trade_if_current(p)
    assert not s.save_dwc_trade_if_current(t)
    s.save_trade(trade(event_id="new",management_signal_time_ms=SID+1))
    assert not s.save_dwc_trade_if_current(p)
    s.delete_trade("A")
    assert not s.save_dwc_trade_if_current(p)


@pytest.mark.asyncio
async def test_record_does_no_broker_io(tmp_path):
    rt=runtime(tmp_path)
    r=await record_intent(rt,parse_alert(MOVE),"stop")
    assert r["status"]=="INTENT_RECORDED" and rt.dwc_management_wakeup.is_set()
    assert not rt.executor.calls and not rt.read_calls and not rt.flat_calls


@pytest.mark.asyncio
async def test_two_account_actual_fill_size_and_disabled_owned_protection(tmp_path):
    rt=runtime(tmp_path)
    rt.accounts=[rule(),rule("B",enabled=False)]
    rt.signed["B"]=-7
    rt.store.save_trade(trade())
    rt.store.save_trade(trade("B",entry=101,current_position_qty=7,total_qty=7,tp1_qty=7))
    await record_intent(rt,parse_alert(MOVE))
    result=await service_intents(rt)
    assert result["results"][0]["status"]=="COMPLETED"
    assert rt.executor.calls==[("broker-A",["s-A"],95.,3),("broker-B",["s-B"],96.5,7)]
    assert rt.store.get_trade("A").tp1==70 and rt.store.get_trade("B").tp1==70
    assert all(t.stop_stage==1 for t in rt.store.all_trades())
    await record_intent(rt,parse_alert(MOVE),"duplicate")
    await service_intents(rt)
    assert len(rt.executor.calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize("updates",[{"management_signal_time_ms":SID+1},
    {"engine":"SILVER"},{"engine":"CORE"},{"management_contract":""},
    {"side":"LONG"}])
async def test_never_touches_unmatched_owned_trade(tmp_path,updates):
    rt=runtime(tmp_path);rt.store.save_trade(trade(**updates))
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert not rt.executor.calls and not rt.read_calls and not rt.flat_calls


@pytest.mark.asyncio
async def test_flat_position_cleans_up_without_rebracket(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade());rt.signed["A"]=0
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert rt.store.get_trade("A") is None
    assert not rt.executor.calls and not rt.flat_calls
    assert not rt.store.dwc_stop_pending()


@pytest.mark.asyncio
async def test_preapplied_stage_is_idempotent(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade(current_stop=95,stop_stage=1))
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert not rt.executor.calls and not rt.store.dwc_stop_pending()


@pytest.mark.asyncio
@pytest.mark.parametrize("signed",[3,-2,-4])
async def test_position_mismatch_quarantines_and_no_stop_change(tmp_path,signed):
    rt=runtime(tmp_path);rt.store.save_trade(trade());rt.signed["A"]=signed
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert not rt.executor.calls and len(rt.flat_calls)==1
    assert rt.store.entry_circuit()["open"]


@pytest.mark.asyncio
async def test_ambiguous_change_is_never_resent_and_exit_not_blindly_repeated(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade());rt.flatten_succeeds=False
    rt.executor.error=RuntimeError("ambiguous broker response")
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert len(rt.executor.calls)==1 and len(rt.flat_calls)==1
    assert rt.store.dwc_stop_pending()
    due(rt.store);await service_intents(rt)
    assert len(rt.executor.calls)==1 and len(rt.flat_calls)==1
    rt.signed["A"]=0;due(rt.store);await service_intents(rt)
    assert not rt.store.dwc_stop_pending() and rt.store.get_trade("A") is None


@pytest.mark.asyncio
async def test_restart_after_modify_reservation_accepts_exact_readback_no_resend(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade())
    await record_intent(rt,parse_alert(MOVE))
    rt.store.mark_dwc_stop_account(SID,"A",phase="MODIFY_RESERVED",stop=95.)
    rt.store=Store(rt.store.path)
    rt.executor.rows=[{"id":"s-A","orderType":"Stop","qty":3,"stopPrice":95},
                      {"id":"t-A","orderType":"Limit","qty":3,"limitPrice":70}]
    await service_intents(rt)
    assert not rt.executor.calls and not rt.flat_calls
    assert rt.store.get_trade("A").stop_stage==1
    assert not rt.store.dwc_stop_pending()


@pytest.mark.asyncio
async def test_restart_with_no_modify_proof_uses_safety_exit_not_resend(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade())
    await record_intent(rt,parse_alert(MOVE))
    rt.store.mark_dwc_stop_account(SID,"A",phase="MODIFY_RESERVED",stop=95.)
    await service_intents(rt)
    assert not rt.executor.calls and not rt.flat_calls
    assert rt.store.dwc_stop_pending()
    rt.store.mark_dwc_stop_account(SID,"A",first_read_failure_epoch=time.time()-91)
    due(rt.store);await service_intents(rt)
    assert not rt.executor.calls and len(rt.flat_calls)==1
    assert rt.store.entry_circuit()["open"]


@pytest.mark.asyncio
async def test_deleted_generation_during_modify_not_resurrected(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade())
    rt.executor.before_change=lambda:rt.store.delete_trade("A")
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert rt.store.get_trade("A") is None
    assert not rt.executor.calls and not rt.flat_calls


@pytest.mark.asyncio
async def test_hard_flat_fence_wins(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade());rt.store.create_entry_attempt(attempt())
    rt._crossed_control_fence=lambda *args:{"kind":"HARD_FLAT"}
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert not rt.read_calls and not rt.executor.calls
    assert rt.store.dwc_stop_pending() # existing hard-flat worker retains authority


@pytest.mark.asyncio
async def test_orphan_grace_then_late_entry_never_reopens(tmp_path):
    rt=runtime(tmp_path)
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert rt.store.dwc_stop_pending()
    rt.store._update_dwc_stop_intent(SID,{"created_at_epoch":time.time()-31,"next_attempt_at_epoch":0})
    await service_intents(rt)
    assert not rt.store.dwc_stop_pending()
    assert blocks_new_entry(rt.store,parse_alert(ENTRY))


@pytest.mark.asyncio
async def test_out_of_order_entry_promotes_and_protects_in_grace(tmp_path):
    rt=runtime(tmp_path)
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert not blocks_new_entry(rt.store,parse_alert(ENTRY))
    rt.store.create_entry_attempt(attempt(state="ACCEPTED"))
    async def promote():
        rt.store.save_trade(trade())
        rt.store.transition_entry_attempt("entry-A:A",("ACCEPTED",),"ACTIVE")
    rt.reconcile_entry_attempts=promote
    due(rt.store);await service_intents(rt)
    assert len(rt.executor.calls)==1 and not rt.store.dwc_stop_pending()


@pytest.mark.asyncio
async def test_missing_account_config_blocks_without_touching_other_accounts(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade("unknown"))
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert rt.store.dwc_stop_pending() and not rt.executor.calls


@pytest.mark.asyncio
async def test_concurrent_service_calls_coalesce(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade())
    await record_intent(rt,parse_alert(MOVE))
    await asyncio.gather(service_intents(rt),service_intents(rt))
    assert len(rt.executor.calls)==1


@pytest.mark.asyncio
async def test_entry_route_persists_contract_into_attempt(tmp_path):
    from test_live_org_breadth_v1_2_8_16 import live_router
    rt=live_router(tmp_path)
    result=await rt.route_entry(parse_alert(ENTRY),receipt_epoch=time.time())
    attempts=rt.store.all_entry_attempts(("ACCEPTED",))
    assert attempts,result
    assert all(a.management_contract==DWC_STOP_CONTRACT and
               a.management_signal_time_ms==SID for a in attempts)


@pytest.mark.asyncio
async def test_executor_only_changes_owned_stop_and_rechecks_current():
    from test_execution_flat import FakeClient
    client=FakeClient()
    client.rows=[{"id":"s","orderType":"Stop","qty":3,"stopPrice":110},
                 {"id":"other","orderType":"Stop","qty":7,"stopPrice":120},
                 {"id":"t","orderType":"Limit","qty":3,"limitPrice":70}]
    ex=Executor(client,bracket_confirm_retries=1,bracket_confirm_delay=0,change_delay=0)
    ids=await ex.change_stop_orders("broker",["s"],95,3,still_current=lambda:True)
    assert ids==["s"] and [x[0] for x in client.change_calls]==["s"]
    assert client.rows[1]["stopPrice"]==120 and client.rows[2]["limitPrice"]==70
    with pytest.raises(NormalizationSuperseded):
        await ex.change_stop_orders("broker",["s"],94,3,still_current=lambda:False)
    assert len(client.change_calls)==1


@pytest.mark.asyncio
async def test_transient_position_read_does_not_flatten_or_mutate(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade())
    normal=rt.position_qty
    async def timeout(r):
        raise TimeoutError("temporary position read timeout")
    rt.position_qty=timeout
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert rt.store.dwc_stop_pending() and not rt.executor.calls and not rt.flat_calls
    assert not rt.store.entry_circuit()["open"]
    rt.position_qty=normal;due(rt.store);await service_intents(rt)
    assert len(rt.executor.calls)==1 and not rt.flat_calls
    assert not rt.store.dwc_stop_pending()


@pytest.mark.asyncio
async def test_pending_modify_does_not_accept_price_alone(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade())
    await record_intent(rt,parse_alert(MOVE))
    rt.store.mark_dwc_stop_account(SID,"A",phase="MODIFY_RESERVED",stop=95.)
    rt.executor.rows=[{"id":"s-A","orderType":"Stop","qty":3,"stopPrice":95,
                       "_lifecycle_unavailable":["commands"]},
                      {"id":"t-A","orderType":"Limit","qty":3,"limitPrice":70}]
    await service_intents(rt)
    assert rt.store.dwc_stop_pending() and rt.store.get_trade("A").stop_stage==0
    assert not rt.executor.calls and not rt.flat_calls
    rt.executor.rows[0].pop("_lifecycle_unavailable")
    due(rt.store);await service_intents(rt)
    assert not rt.store.dwc_stop_pending() and rt.store.get_trade("A").stop_stage==1
    assert not rt.executor.calls


@pytest.mark.asyncio
async def test_target_stop_crossing_never_issues_invalid_change(tmp_path):
    rt=runtime(tmp_path);rt.store.save_trade(trade(tp1=97))
    await record_intent(rt,parse_alert(MOVE));await service_intents(rt)
    assert not rt.executor.calls and len(rt.flat_calls)==1


@pytest.mark.asyncio
async def test_shadow_observer_recognizes_dwc_without_broker_access():
    from shadow import ShadowObserver
    from test_shadow_observer_v1_2_8_10 import ShadowOnlyStore
    observer=ShadowObserver(ShadowOnlyStore(), lambda:[rule()])
    result=await observer.observe(parse_alert(MOVE),"d",time.time())
    assert result["action"]=="WOULD_MOVE_DWC_STOPS"
    assert result["broker_mutation_performed"] is False


@pytest.mark.asyncio
async def test_api_dispatch_records_dwc_without_waiting_for_broker(tmp_path,monkeypatch):
    import main
    rt=runtime(tmp_path)
    monkeypatch.setattr(main,"_SHADOW_MODE",False)
    monkeypatch.setattr(main,"_runtime",lambda:rt)
    result=await main._process_event(parse_alert(MOVE),event_key="d")
    assert result["kind"]=="DWC_STOP_MOVE"
    assert rt.store.dwc_stop_pending() and not rt.read_calls


@pytest.mark.asyncio
async def test_idle_dwc_loop_performs_no_broker_reads(tmp_path):
    rt=runtime(tmp_path)
    assert (await service_intents(rt))["status"]=="IDLE"
    assert not rt.read_calls and not rt.executor.calls
