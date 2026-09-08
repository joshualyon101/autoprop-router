from datetime import datetime, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.models import AccountRule, AccountState, Allocation
from app.events import parse_alert, ParsedEvent
from app.store import Store
from app.service import fanout_entry
from app import main
from app.settings import Settings
from app.readiness import readiness

NOW=datetime.now(timezone.utc)

def rule(account_id="A"):
    return AccountRule(account_id=account_id,crosstrade_account="acct"+account_id,enabled=True,rules_verified=True,account_type="personal",profile="standard",starting_balance=100000,max_loss=0,max_contracts=20,personal_risk_method="fixed",personal_risk_value=250)

def state(account_id="A"):
    return AccountState(account_id=account_id,closed_cash_balance=100000,state_timestamp=NOW,daily_ledger_verified=True)

def test_store_event_dedupe_and_trade_roundtrip(tmp_path):
    s=Store(str(tmp_path/"x.sqlite3"))
    assert s.claim_event("abc") is True
    assert s.claim_event("abc") is False

def test_parser_rejects_unpatched_core_without_absolute_geometry():
    with pytest.raises(ValueError): parse_alert("D34|SIDE=LONG|SRC=ORL|SC=4")

def test_parser_generic_market_pulse_is_trade_independent():
    e=parse_alert("AUTOPROP_ICT_FUSION|MARKET_PULSE|T5=1|TC5=2|B5=3|O=100|H=101|L=99|C=100.5|ATR=2|RTL=98|RTH=102")
    assert e.kind=="MARKET_PULSE" and e.plan is None and e.pulse.native_bar_index==3

@pytest.mark.asyncio
async def test_fanout_skips_missing_state_without_substitute(tmp_path):
    e=parse_alert("AUTOPROP_ICT_FUSION|SILVER|ENTRY|LONG|ENTRY=100|SL=90|TP=120")
    called=[]
    async def execute(r,a): called.append(r.account_id)
    async def fills(r): return []
    s=Store(str(tmp_path/"x.sqlite3"))
    out=await fanout_entry(e,[rule("A"),rule("B")],{"A":state("A")},s,execute,fills)
    assert [x.status for x in out]==["ROUTED","SKIP"] and called==["A"]

@pytest.mark.asyncio
async def test_org_reentry_is_destination_outcome_aware(tmp_path):
    e=parse_alert("AUTOPROP_ICT_FUSION|ORG|ENTRY|LONG|ENTRY=100|SL=90|TP=120|REENTRY=1|TYPE=STOP_RETRY")
    called=[]; s=Store(str(tmp_path/"x.sqlite3"))
    s.save_org_attempt("A",{"stop_order_ids":["sA"]})
    s.save_org_attempt("B",{"stop_order_ids":["sB"]})
    async def execute(r,a): called.append(r.account_id)
    async def fills(r):
        return [{"orderId":"sA","qty":1}] if r.account_id=="A" else [{"orderId":"targetB","qty":1}]
    out=await fanout_entry(e,[rule("A"),rule("B")],{"A":state("A"),"B":state("B")},s,execute,fills)
    assert called==["A"]
    assert out[0].status=="ROUTED" and out[1].status=="SKIP"
    assert "stop outcome not proven" in out[1].reason

def test_default_settings_are_fail_closed():
    s=Settings(_env_file=None)
    assert s.TRADINGVIEW_ALERT_CONTRACT_VERIFIED is False
    assert s.AUTOPROP_LIVE_ARM=="" and s.AUTOPROP_FULL_SCALE_ARM==""
    assert s.NATIVE_ATM_BREAKEVEN_AFTER_TP1 is False
    assert s.LIVE_MAX_QTY_PER_ACCOUNT==0

def test_readiness_separates_staging_readiness_from_final_gate():
    s=Settings(_env_file=None,CROSSTRADE_TOKEN="x",AUTOPROP_WEBHOOK_TOKEN="y",
               AUTOPROP_LIVE_ARM="I_UNDERSTAND_LIVE_ORDERS",
               AUTOPROP_FULL_SCALE_ARM="I_UNDERSTAND_FULL_SCALE",
               TRADINGVIEW_ALERT_CONTRACT_VERIFIED=False)
    r=readiness(s,[rule()])
    assert r["configuration_ready"] is True
    assert r["broker_mutation_armed"] is False
    assert any("alert contract gate is false" in x for x in r["warnings"])

    s.TRADINGVIEW_ALERT_CONTRACT_VERIFIED=True
    r2=readiness(s,[rule()])
    assert r2["configuration_ready"] is True
    assert r2["broker_mutation_armed"] is True

def test_webhook_disarmed_parses_but_never_enters_mutation_path(monkeypatch):
    monkeypatch.setattr(main.settings,"AUTOPROP_WEBHOOK_TOKEN","secret")
    monkeypatch.setattr(main.settings,"TRADINGVIEW_ALERT_CONTRACT_VERIFIED",False)
    c=TestClient(main.app)
    r=c.post("/webhook/tradingview/secret",content="AUTOPROP_ICT_FUSION|SILVER|ENTRY|LONG|ENTRY=100|SL=90|TP=120")
    assert r.status_code==200 and r.json()["disarmed"] is True

def test_gate_true_production_release_reaches_live_router(monkeypatch):
    monkeypatch.setattr(main.settings,"AUTOPROP_WEBHOOK_TOKEN","secret")
    monkeypatch.setattr(main.settings,"CROSSTRADE_TOKEN","x")
    monkeypatch.setattr(main.settings,"AUTOPROP_LIVE_ARM","I_UNDERSTAND_LIVE_ORDERS")
    monkeypatch.setattr(main.settings,"AUTOPROP_FULL_SCALE_ARM","I_UNDERSTAND_FULL_SCALE")
    monkeypatch.setattr(main.settings,"TRADINGVIEW_ALERT_CONTRACT_VERIFIED",True)
    monkeypatch.setattr(main,"_accounts",lambda:[rule()])
    class FakeRuntime:
        async def route_entry(self,event):
            return {"kind":"ENTRY","engine":event.plan.engine,"results":[{"account_id":"A","status":"ROUTED"}]}
    monkeypatch.setattr(main,"_runtime",lambda:FakeRuntime())
    c=TestClient(main.app)
    r=c.post("/webhook/tradingview/secret",content="AUTOPROP_ICT_FUSION|SILVER|ENTRY|LONG|ENTRY=100|SL=90|TP=120")
    assert r.status_code==200
    assert r.json()["results"][0]["status"]=="ROUTED"

def test_global_short_flat_messages_parse_as_hard_flat():
    assert parse_alert("AUTOPROP_ICT_FUSION|REQUIRED_FLAT_EXIT").kind=="HARD_FLAT"
    assert parse_alert("AUTOPROP_ICT_FUSION|CROSS_DAY_EXIT").kind=="HARD_FLAT"
    assert parse_alert("AUTOPROP_ICT_FUSION|CORE|REQUIRED_FLAT_EXIT").kind=="HARD_FLAT"

def test_production_sqlite_default_uses_versioned_db_to_avoid_legacy_schema_collision():
    s=Settings(_env_file=None)
    assert s.SQLITE_PATH.endswith('autoprop_router_v122.sqlite3')

def test_linked_account_name_extractor():
    p={"success":True,"data":[{"name":"A"},{"name":"B"},{"name":None}]}
    assert main._linked_account_names(p)=={"A","B"}
