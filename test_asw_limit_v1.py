from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import pytest

from events import parse_alert
from models import AccountRule, AccountState, Allocation, CanonicalPlan
from allocation import allocate_asw, AllocationBlocked
from execution import Executor, ProtectionFailure

NOW=datetime(2026,9,15,4,30,tzinfo=timezone.utc)

def plan(native_qty=2, rpc=125.0, side='LONG'):
    return CanonicalPlan(
        event_id='asw-e',engine='ASW',side=side,entry=20000.0,
        stop=19937.5 if side=='LONG' else 20062.5,
        tp1=20093.75 if side=='LONG' else 19906.25,
        source_qty=native_qty,contract_risk_dollars=rpc,
        native_time_ms=int(NOW.timestamp()*1000),
        expiry_time_ms=int((NOW+timedelta(minutes=45)).timestamp()*1000),
        contract_version='ASW_LIMIT_V1')

def state(account_id='A', cash=50000,mll=48000,locked=False,realized=0):
    return AccountState(account_id=account_id,closed_cash_balance=cash,mll_floor=mll,mll_verified=True,
                        funded_locked=locked,realized_today=realized,largest_winning_day=0,
                        state_timestamp=NOW,daily_ledger_verified=True)

def rule(kind='challenge', maxc=20, funded_cap=0):
    return AccountRule(account_id='A',crosstrade_account='acct',enabled=True,rules_verified=True,
                       account_type=kind,profile='standard',starting_balance=50000,max_loss=2000,
                       max_contracts=maxc,drawdown='eod',challenge_target=3000,
                       challenge_consistency_enabled=(kind=='challenge'),challenge_consistency_pct=40,
                       funded_consistency_enabled=(kind=='funded' and funded_cap>0),
                       funded_daily_profit_cap=funded_cap,
                       personal_risk_method='fixed',personal_risk_value=250)

def test_parser_working_limit_contract():
    raw='AUTOPROP_ICT_FUSION|ASW|WORKING_LIMIT|LONG|CONTRACT=ASW_LIMIT_V1|ORDER_TYPE=LIMIT|TIF=DAY|QTY=6|Q0=2|ENTRY=20000|SL=19937.5|TP=20093.75|CR=125.00|T5=1789446600000|EXP=1789449300000'
    e=parse_alert(raw)
    assert e.kind=='ASW_WORKING_LIMIT' and e.plan.engine=='ASW'
    assert e.plan.source_qty==2 and e.plan.expiry_time_ms==1789449300000

def test_parser_rejects_wrong_contract():
    raw='AUTOPROP_ICT_FUSION|ASW|WORKING_LIMIT|LONG|CONTRACT=BAD|ORDER_TYPE=LIMIT|Q0=2|ENTRY=20000|SL=19937.5|TP=20093.75|CR=125|T5=1|EXP=2'
    with pytest.raises(ValueError,match='ASW_LIMIT_V1'):
        parse_alert(raw)

def test_challenge_exact_native_qty():
    # 50K EOD Standard new-account base risk = min(2000*.1875, cap) = $375.
    a=allocate_asw(plan(2),rule('challenge'),state(),now=NOW)
    assert a.qty==2 and a.tp1==20093.75

def test_challenge_never_downsizes_native_plan():
    with pytest.raises(AllocationBlocked,match='exact native plan exceeds Challenge risk budget'):
        allocate_asw(plan(4),rule('challenge'),state(),now=NOW)

def test_funded_whole_native_multiples():
    # $2k MLL healthy prelock standard funded risk = $300. Native plan risk=$250 => 1x.
    a=allocate_asw(plan(2),rule('funded'),state(),now=NOW)
    assert a.qty==2
    # Post-lock cushion $4k -> max(300, 12.5%*4000=$500) => 2x => 4 qty.
    a2=allocate_asw(plan(2),rule('funded'),state(cash=52000,mll=48000,locked=True),now=NOW)
    assert a2.qty==4 and a2.qty % 2 == 0

def test_funded_contract_cap_floors_to_whole_multiple():
    a=allocate_asw(plan(2),rule('funded',maxc=5),state(cash=54000,mll=48000,locked=True),now=NOW)
    assert a.qty==4

def test_funded_consistency_never_clips_target():
    # Post-lock risk allows 4, but daily cap room $400 allows only 1 native 2-lot plan ($375 profit).
    r=rule('funded',funded_cap=400)
    a=allocate_asw(plan(2),r,state(cash=52000,mll=48000,locked=True),now=NOW)
    assert a.qty==2 and a.tp1==20093.75

def test_rpc_mismatch_fails_closed():
    with pytest.raises(AllocationBlocked,match='contract-risk mismatch'):
        allocate_asw(plan(2,rpc=124.0),rule('challenge'),state(),now=NOW)

class LimitClient:
    def __init__(self, ambiguous=False):
        self.payload=None; self.ambiguous=ambiguous; self.cancelled=[]; self.flattened=[]
    async def orders(self, account): return {'data':[]}
    async def all_orders(self): return {'data':[]}
    async def place(self, account, payload):
        self.payload=dict(payload)
        return {'response':{'orderId':'p1','oso1Id':'t1','oso2Id':'s1','osoChildIds':['t1','s1']}}
    async def cancel_order(self, account, oid): self.cancelled.append(oid); return {'ok':True}
    async def flatten(self, account, instrument='MNQ1!'): self.flattened.append((account,instrument)); return {'ok':True}

@pytest.mark.asyncio
async def test_executor_places_true_limit_with_bracket_and_timeout():
    c=LimitClient(); ex=Executor(c,execution_symbol='MNQ',bracket_confirm_delay=0)
    a=Allocation(account_id='A',event_id='e',engine='ASW',side='LONG',qty=4,entry=20000,stop=19937.5,
                 tp1=20093.75,tp1_qty=4,runner_qty=0,base_risk=500,effective_risk_budget=500,risk_per_contract=125)
    now_ms=1_000_000; exp=now_ms+44*60_000+1
    r=await ex.place_asw_limit('acct',a,'AP-ASW',expiry_time_ms=exp,now_ms=now_ms)
    assert r.parent_order_id=='p1' and set(r.child_order_ids)=={'t1','s1'}
    p=c.payload
    assert p['orderType']=='limit' and p['limitPrice']==20000
    assert p['takeProfit']==20093.75 and p['stopLoss']==19937.5
    assert p['cancelAfter']==45 and p['requireMarketPosition']=='flat' and p['maxPositions']==1

@pytest.mark.asyncio
async def test_executor_rejects_expired_limit_without_mutation():
    c=LimitClient(); ex=Executor(c)
    a=Allocation(account_id='A',event_id='e',engine='ASW',side='LONG',qty=2,entry=20000,stop=19937.5,
                 tp1=20093.75,tp1_qty=2,runner_qty=0,base_risk=250,effective_risk_budget=250,risk_per_contract=125)
    with pytest.raises(ProtectionFailure,match='expired'):
        await ex.place_asw_limit('acct',a,'AP',expiry_time_ms=100,now_ms=100)
    assert c.payload is None

from pathlib import Path
from live import LiveRouter
from store import Store
from models import AswPending

class RouteExecutor:
    def __init__(self): self.calls=[]
    async def place_asw_limit(self, account, alloc, custom_order_id, *, expiry_time_ms,
                              now_ms, **kwargs):
        from execution import ExecutionReceipt
        self.calls.append((account,alloc,custom_order_id,expiry_time_ms,now_ms))
        return ExecutionReceipt({'ok':True},['t1','s1'],'p1')

@pytest.mark.asyncio
async def test_live_route_persists_durable_pending_limit(tmp_path):
    r=LiveRouter.__new__(LiveRouter)
    rr=rule('funded')
    r.accounts=[rr]
    r.settings=SimpleNamespace(MAX_STATE_AGE_SECONDS=20)
    r.store=Store(str(tmp_path/'router.sqlite3'))
    r.executor=RouteExecutor()
    r.execution_symbol='MNQ1!'
    async def gate(x): return None
    async def st(x):
        z=state(); z.state_timestamp=datetime.now(timezone.utc); return z
    r.entry_gate=gate; r.state_for=st
    r._custom_id=lambda event_id,account_id:'CID1'
    raw=(f'AUTOPROP_ICT_FUSION|ASW|WORKING_LIMIT|LONG|CONTRACT=ASW_LIMIT_V1|ORDER_TYPE=LIMIT|TIF=DAY|'
         f'QTY=2|Q0=2|ENTRY=20000|SL=19937.5|TP=20093.75|CR=125.00|'
         f'T5={int(NOW.timestamp()*1000)}|EXP={int((NOW+timedelta(minutes=45)).timestamp()*1000)}')
    # route uses real UTC now; give future timestamps relative to actual now for this integration check
    now=datetime.now(timezone.utc)
    raw=(f'AUTOPROP_ICT_FUSION|ASW|WORKING_LIMIT|LONG|CONTRACT=ASW_LIMIT_V1|ORDER_TYPE=LIMIT|TIF=DAY|'
         f'QTY=2|Q0=2|ENTRY=20000|SL=19937.5|TP=20093.75|CR=125.00|'
         f'T5={int(now.timestamp()*1000)}|EXP={int((now+timedelta(minutes=45)).timestamp()*1000)}')
    out=await r.route_asw_working_limit(parse_alert(raw))
    assert out['results'][0]['status']=='PENDING_LIMIT'
    p=r.store.get_asw_pending('A')
    assert p and p.parent_order_id=='p1' and p.child_order_ids==['t1','s1']

class ReconcileClient:
    def __init__(self,status='Filled',pos=2):
        self.status=status; self.pos=pos; self.cancel_calls=[]; self.flatten_calls=[]
    async def order_status(self,account,oid): return {'data':{'orderId':oid,'status':self.status}}
    async def orders(self,account):
        if self.pos:
            return {'data':[
                {'id':'t1','orderType':'Limit','qty':abs(self.pos),'limitPrice':20093.75},
                {'id':'s1','orderType':'Stop','qty':abs(self.pos),'stopPrice':19937.5},
            ]}
        return {'data':[]}
    async def fills_order(self,oid):
        return {'data':[{'id':'f1','qty':abs(self.pos) or 2,'price':20000.0,
                         'instrument':'MNQZ6','contractId':991}]}
    async def positions(self,account):
        return {'data':([{'netPos':self.pos,'contractId':991}] if self.pos else [])}
    async def position(self,account,instrument='MNQ1!'):
        raise AssertionError('singular position endpoint must never be used')
    async def cancel_order(self,account,oid): self.cancel_calls.append(oid); self.status='Canceled'; return {'ok':True}
    async def flatten(self,account,instrument='MNQ1!'): self.flatten_calls.append((account,instrument)); self.pos=0; return {'ok':True}


def make_pending(expiry_delta=10):
    now=datetime.now(timezone.utc)
    return AswPending(account_id='A',crosstrade_account='acct',event_id='e',side='LONG',qty=2,native_qty=2,
                      entry=20000,stop=19937.5,target=20093.75,risk_per_contract=125,
                      signal_time_ms=int((now-timedelta(minutes=1)).timestamp()*1000),
                      expiry_time_ms=int((now+timedelta(minutes=expiry_delta)).timestamp()*1000),
                      parent_order_id='p1',custom_order_id='CID',child_order_ids=['t1','s1'],created_at=now)

def make_reconcile_router(tmp_path,client,pending):
    r=LiveRouter.__new__(LiveRouter)
    r.accounts=[rule('funded')]
    r.settings=SimpleNamespace(ASW_PROTECTION_CONFIRM_RETRIES=1,ASW_PROTECTION_CONFIRM_RETRY_DELAY_SECONDS=0,
                               BRACKET_CONFIRM_RETRIES=1,BRACKET_CONFIRM_RETRY_DELAY_SECONDS=0)
    r.store=Store(str(tmp_path/'rec.sqlite3')); r.store.save_asw_pending(pending)
    r.client=client; r.execution_symbol='MNQ1!'
    r.executor=Executor(client,execution_symbol='MNQ1!',bracket_confirm_retries=1,bracket_confirm_delay=0)
    return r

@pytest.mark.asyncio
async def test_reconcile_full_fill_requires_exact_owned_bracket_then_promotes(tmp_path):
    c=ReconcileClient(status='Filled',pos=2); r=make_reconcile_router(tmp_path,c,make_pending())
    out=await r.reconcile_asw_pending()
    assert any(x['status']=='ACTIVE_PROTECTED' for x in out['results'])
    assert r.store.get_asw_pending('A') is None
    t=r.store.get_trade('A'); assert t and t.engine=='ASW' and t.total_qty==2

@pytest.mark.asyncio
async def test_reconcile_expired_partial_fill_flattens_not_carries(tmp_path):
    c=ReconcileClient(status='Working',pos=1); r=make_reconcile_router(tmp_path,c,make_pending(expiry_delta=-1))
    out=await r.reconcile_asw_pending()
    assert c.flatten_calls
    assert r.store.get_asw_pending('A') is None
    assert any('FLATTENED_PARTIAL'==x['status'] for x in out['results'])

@pytest.mark.asyncio
async def test_cancel_pending_flattens_broker_fill_if_pine_still_pending(tmp_path):
    c=ReconcileClient(status='Working',pos=2); p=make_pending(); r=make_reconcile_router(tmp_path,c,p)
    event=parse_alert(f'AUTOPROP_ICT_FUSION|ASW|CANCEL_PENDING|LONG|CONTRACT=ASW_LIMIT_V1|T5={p.signal_time_ms}')
    out=await r.cancel_asw_pending(event)
    assert c.flatten_calls and r.store.get_asw_pending('A') is None
    assert out['results'][0]['status']=='CANCELED_FLATTENED'
