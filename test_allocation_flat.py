from datetime import datetime, timezone, timedelta
import pytest
from allocation import allocate, allocate_asw, AllocationBlocked
from models import AccountRule, AccountState, CanonicalPlan

NOW=datetime.now(timezone.utc)

def state(**kw):
    d=dict(account_id="A",closed_cash_balance=100000,mll_floor=None,mll_verified=False,funded_locked=False,realized_today=0,largest_winning_day=0,state_timestamp=NOW,daily_ledger_verified=True)
    d.update(kw); return AccountState(**d)

def personal(profile="standard"):
    return AccountRule(account_id="A",crosstrade_account="acct",enabled=True,rules_verified=True,account_type="personal",profile=profile,starting_balance=100000,max_loss=0,max_contracts=100,personal_risk_method="fixed",personal_risk_value=250)

def test_personal_aggressive_is_locked_1_20_not_funded_1_10():
    plan=CanonicalPlan(event_id="e",engine="SILVER",side="LONG",entry=100,stop=90,tp1=120)
    a=allocate(plan,personal("aggressive"),state(),now=NOW)
    assert a.base_risk==pytest.approx(300.0)

def test_personal_standard_250():
    plan=CanonicalPlan(event_id="e",engine="SILVER",side="LONG",entry=100,stop=90,tp1=120)
    assert allocate(plan,personal(),state(),now=NOW).base_risk==250

def test_prop_requires_verified_mll():
    rule=AccountRule(account_id="A",crosstrade_account="acct",enabled=True,rules_verified=True,account_type="challenge",profile="standard",starting_balance=100000,max_loss=3000,max_contracts=50,challenge_target=6000)
    plan=CanonicalPlan(event_id="e",engine="ORG",side="LONG",entry=100,stop=90,tp1=120)
    with pytest.raises(AllocationBlocked,match="MLL"):
        allocate(plan,rule,state(),now=NOW)

def test_stale_state_fails_closed():
    old=datetime(2020,1,1,tzinfo=timezone.utc)
    plan=CanonicalPlan(event_id="e",engine="SILVER",side="LONG",entry=100,stop=90,tp1=120)
    with pytest.raises(AllocationBlocked,match="stale"):
        allocate(plan,personal(),state(state_timestamp=old),now=NOW)

def test_unverified_daily_ledger_fails_closed():
    plan=CanonicalPlan(event_id="e",engine="SILVER",side="LONG",entry=100,stop=90,tp1=120)
    with pytest.raises(AllocationBlocked,match="ledger"):
        allocate(plan,personal(),state(daily_ledger_verified=False),now=NOW)

def test_core_destination_split_is_from_destination_qty():
    plan=CanonicalPlan(event_id="e",engine="CORE",side="LONG",entry=100,stop=90,tp1=120,tp2=140,module="CORE",source="ORL",score=4)
    a=allocate(plan,personal(),state(),now=NOW)
    assert (a.tp1_qty,a.runner_qty)==((a.qty+1)//2,a.qty//2)

def test_realized_loss_below_old_1_8r_threshold_does_not_block_allocation():
    plan=CanonicalPlan(event_id="e",engine="SILVER",side="LONG",entry=100,stop=90,tp1=120)
    a=allocate(plan,personal(),state(realized_today=-1000),now=NOW)
    assert a.qty >= 1

def test_realized_loss_below_old_1_8r_threshold_does_not_block_asw():
    plan=CanonicalPlan(event_id="e",engine="ASW",side="LONG",entry=100,stop=90,tp1=120,source_qty=1,contract_risk_dollars=20,native_time_ms=int(NOW.timestamp()*1000),expiry_time_ms=int((NOW+timedelta(minutes=45)).timestamp()*1000),contract_version="ASW_LIMIT_V1")
    a=allocate_asw(plan,personal(),state(realized_today=-1000),now=NOW)
    assert a.qty == 1
