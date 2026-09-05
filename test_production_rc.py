import asyncio
from datetime import datetime, timezone
from pathlib import Path

from config import Settings
from models import AccountConfig, AccountRuntime, TradeSignal
from persistence import Store
from state import AccountStateCache
from router import AutoPropRouter
from rules_profiles import infer_account_config
from signal_adapter import normalize_payload

class FakeClient:
    def __init__(self): self.orders=[]; self.flats=[]; self.closes=[]; self.brackets=[]
    async def place_order(self, **kw): self.orders.append(kw); return {'success':True,'response':{'orderId':len(self.orders)}}
    async def flatten_position(self, **kw): self.flats.append(kw); return {'success':True}
    async def close_position(self, **kw): self.closes.append(kw); return {'success':True}
    async def cancel_and_bracket(self, **kw): self.brackets.append(kw); return {'success':True}

async def native_full_scale():
    db=Path('/tmp/ap100_native.sqlite3'); db.unlink(missing_ok=True); store=Store(str(db))
    a=AccountConfig(id='CT_1',crosstrade_account_id=1,account_name='FN100',firm='FundedNext',program='Legacy',phase='challenge',starting_balance=100000,max_loss=3000,profit_target=6000,max_micros=50,drawdown_type='eod_trail_to_lock',consistency_pct=40,enabled=True,rules_verified=True,risk_ready=True)
    cache=AccountStateCache([a],store,auto_discovery=False); now=datetime.now(timezone.utc)
    cache._state['CT_1']=AccountRuntime(account_id='CT_1',account_name='FN100',balance=100000,net_liq=100000,mll_floor=97000,cushion=3000,positions=[],working_orders=[],source_updated_at=now,cache_updated_at=now)
    client=FakeClient(); settings=Settings(crosstrade_token='x',webhook_token='y',execution_mode='live',live_arm='I_UNDERSTAND_LIVE_ORDERS',full_scale_arm='I_UNDERSTAND_FULL_SCALE',alert_contract_verified=True,management_mode='native_atm',live_max_qty_per_account=0,sqlite_path='/tmp/x')
    assert settings.live_enabled
    r=AutoPropRouter(settings,[a],client,cache,store)
    sig=TradeSignal(event_id='e1',trade_id='t100',event='ENTRY',engine='CORE',direction='LONG',instrument='MNQ1!',entry=20000,stop=19990,tp1=20010,tp2=20020,contract_risk_dollars=50,source_qty=10,tp1_qty=6,runner_qty=4)
    rr=await r.route(sig)
    assert len(client.orders)==1, rr.model_dump()
    # 18.75% of $3k = $562.50; 562.5/50 = 11.25 -> nearest = 11, no 1-contract cap.
    assert client.orders[0]['qty']==11, client.orders[0]
    assert client.orders[0]['atm_qtys']=='7,4', client.orders[0]
    assert client.orders[0]['atm_breakeven']==1
    # Same trade_id, different event must NOT be blocked by old trade-id dedupe.
    sm=TradeSignal(event_id='e2',trade_id='t100',event='STOP_MOVE',engine='CORE',direction='LONG',instrument='MNQ1!',stop=20000)
    rr2=await r.route(sm)
    assert 'ignored' in rr2.decisions[0].reason
    # Force flat still routes and does not care about risk_ready.
    a.risk_ready=False
    fl=TradeSignal(event_id='e3',trade_id='t100',event='FLATTEN',engine='CORE',instrument='MNQ1!')
    rr3=await r.route(fl)
    assert len(client.flats)==1, rr3.model_dump()

async def tv_managed_partial():
    db=Path('/tmp/ap100_tv.sqlite3'); db.unlink(missing_ok=True); store=Store(str(db))
    a=AccountConfig(id='CT_2',crosstrade_account_id=2,account_name='MFFU',firm='My Funded Futures',program='Pro',phase='challenge',starting_balance=50000,max_loss=2000,profit_target=3000,max_micros=30,drawdown_type='eod_trail_to_lock',lock_offset=100,consistency_pct=50,enabled=True,rules_verified=True,risk_ready=True)
    cache=AccountStateCache([a],store,auto_discovery=False); now=datetime.now(timezone.utc)
    cache._state['CT_2']=AccountRuntime(account_id='CT_2',account_name='MFFU',balance=50000,net_liq=50000,mll_floor=48000,cushion=2000,positions=[],working_orders=[],source_updated_at=now,cache_updated_at=now)
    client=FakeClient(); settings=Settings(crosstrade_token='x',webhook_token='y',execution_mode='live',live_arm='I_UNDERSTAND_LIVE_ORDERS',full_scale_arm='I_UNDERSTAND_FULL_SCALE',alert_contract_verified=True,management_mode='tv_managed',sqlite_path='/tmp/x')
    r=AutoPropRouter(settings,[a],client,cache,store)
    e=TradeSignal(event_id='x1',trade_id='life1',event='ENTRY',engine='CORE',direction='SHORT',instrument='MNQ1!',entry=20000,stop=20010,tp1=19990,tp2=19980,contract_risk_dollars=50,source_qty=10,tp1_qty=6,runner_qty=4)
    rr=await r.route(e); assert client.orders and client.orders[0]['stop_loss']==20010 and 'atm_targets' not in client.orders[0]
    alloc=store.get_active_allocation('CT_2','MNQ1!'); assert alloc
    p=TradeSignal(event_id='x2',trade_id='life1',event='PARTIAL_EXIT',engine='CORE',direction='SHORT',instrument='MNQ1!',stop=19999,source_qty=10,exit_qty=6)
    rr2=await r.route(p); assert client.closes and client.brackets
    assert client.brackets[-1]['qty'] < alloc['initial_qty']
    assert client.brackets[-1]['stop_loss']==19999

async def dry_run_no_mutation():
    db=Path('/tmp/ap100_dry.sqlite3'); db.unlink(missing_ok=True); store=Store(str(db))
    a=AccountConfig(id='CT_3',account_name='A',firm='F',program='P',phase='challenge',starting_balance=50000,max_loss=2000,profit_target=3000,max_micros=30,drawdown_type='static',consistency_pct=None,enabled=True,rules_verified=True,risk_ready=True)
    cache=AccountStateCache([a],store,auto_discovery=False); now=datetime.now(timezone.utc)
    cache._state['CT_3']=AccountRuntime(account_id='CT_3',account_name='A',balance=50000,net_liq=50000,mll_floor=48000,cushion=2000,positions=[],working_orders=[],source_updated_at=now,cache_updated_at=now)
    client=FakeClient(); settings=Settings(execution_mode='live',live_arm='I_UNDERSTAND_LIVE_ORDERS',full_scale_arm='I_UNDERSTAND_FULL_SCALE',alert_contract_verified=True,management_mode='native_atm')
    r=AutoPropRouter(settings,[a],client,cache,store)
    s=TradeSignal(trade_id='dry1',event='ENTRY',engine='ORG',direction='LONG',entry=100,stop=90,tp1=110,contract_risk_dollars=50)
    await r.route(s,mutate=False,claim=False); assert not client.orders

async def main():
    # Profile regression.
    m=infer_account_config(account_id=9,account_name='MFFUEVPRO999',effective_balance=48800)
    assert m.max_micros==30 and m.lock_offset==100 and m.drawdown_type.value=='eod_trail_to_lock'
    f=infer_account_config(account_id=10,account_name='FNFTCHABC',effective_balance=99600)
    assert f.max_micros==50 and f.drawdown_type.value=='eod_trail_to_lock'
    # Adapter regression.
    j=normalize_payload(b'{"event":"TP1_FILL","source":"CORE","side":"LONG","symbol":"MNQ1!","qty":6,"tp1_qty":3,"runner_qty":3,"stop":20000}', 'application/json')
    assert j.event.value=='PARTIAL_EXIT' and j.exit_qty==3
    p=normalize_payload(b'AUTOPROP_ICT_FUSION|SILVER|ENTRY|SHORT|QTY=3|ENTRY=20000|SL=20010|TP=19970','text/plain')
    assert p.event.value=='ENTRY' and p.source_qty==3 and p.stop==20010
    # Full-scale arm regression.
    assert not Settings(execution_mode='live',live_arm='I_UNDERSTAND_LIVE_ORDERS',alert_contract_verified=True).live_enabled
    await native_full_scale(); await tv_managed_partial(); await dry_run_no_mutation()
    print('AutoProp Router v1.0.0 RC1 production tests PASS')

asyncio.run(main())
