from datetime import datetime, timezone
import pytest
from app.models import AccountRule, VerifiedRiskState
from app.state import StateUnverified
from app.state_refresh import refresh_account_state, durable_fills

NOW=datetime(2026,9,7,15,0,tzinfo=timezone.utc)

def rule(kind="challenge"):
    return AccountRule(account_id="A",crosstrade_account="acct",enabled=True,rules_verified=True,account_type=kind,profile="standard",starting_balance=100000,max_loss=3000 if kind!="personal" else 0,max_contracts=50,challenge_target=6000 if kind=="challenge" else 0)

def risk(**kw):
    d=dict(account_id="A",mll_floor=98000,mll_verified=True,funded_locked=False,largest_winning_day=0,ledger_verified=True,cycle_start_utc=datetime(2026,9,1,tzinfo=timezone.utc),verified_at=NOW,source="test")
    d.update(kw); return VerifiedRiskState(**d)

class C:
    def __init__(self): self.calls=[]
    async def get_account(self,a):
        return {"success":True,"data":{"balance":{"amount":100500,"timestamp":"2026-09-07T14:59:55Z","netLiquidation":101000}}}
    async def fills_history(self,**kw):
        self.calls.append(kw)
        return {"data":[
          {"executionId":"1","root":"MNQ","instrument":"MNQZ6","timestamp":"2026-09-07T14:00:00Z","action":"Buy","qty":1,"price":100},
          {"executionId":"2","root":"MNQ","instrument":"MNQZ6","timestamp":"2026-09-07T14:30:00Z","action":"Sell","qty":1,"price":110},
        ],"nextCursor":None}

@pytest.mark.asyncio
async def test_refresh_uses_cash_not_netliq_and_verified_mll_and_ny_ledger():
    c=C(); s=await refresh_account_state(c,rule(),risk(),now=NOW)
    assert s.closed_cash_balance==100500
    assert s.mll_floor==98000 and s.mll_verified
    assert s.realized_today==20
    assert s.daily_ledger_verified

@pytest.mark.asyncio
async def test_prop_refresh_refuses_missing_verified_mll():
    with pytest.raises(StateUnverified,match="MLL"):
        await refresh_account_state(C(),rule(),None,now=NOW)

@pytest.mark.asyncio
async def test_refresh_refuses_unverified_durable_ledger():
    with pytest.raises(StateUnverified,match="ledger"):
        await refresh_account_state(C(),rule(),risk(ledger_verified=False),now=NOW)

@pytest.mark.asyncio
async def test_durable_fill_pagination_is_exhaustive():
    class P(C):
        async def fills_history(self,**kw):
            self.calls.append(kw)
            if kw.get("cursor") is None: return {"data":[{"executionId":"a"}],"nextCursor":"c1"}
            return {"data":[{"executionId":"b"}],"nextCursor":None}
    c=P(); rows=await durable_fills(c,"acct",datetime(2026,9,1,tzinfo=timezone.utc),NOW)
    assert [x["executionId"] for x in rows]==["a","b"] and len(c.calls)==2
