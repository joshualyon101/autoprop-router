from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from config import Settings
from crosstrade import CrossTradeClient
from models import TradeSignal
from persistence import Store
from router import AutoPropRouter
from state import AccountStateCache, StatePoller
from admin_ui import render_account_manager
from signal_adapter import normalize_payload

settings=Settings.from_env(); accounts=settings.load_accounts(); store=Store(settings.sqlite_path)
client=CrossTradeClient(settings.crosstrade_base_url,settings.crosstrade_token,settings.request_timeout_seconds)
cache=AccountStateCache(accounts,store,auto_discovery=settings.auto_discovery,fundednext_default_model=settings.fundednext_default_model)
poller=StatePoller(client,cache,settings.state_poll_seconds); router=AutoPropRouter(settings,accounts,client,cache,store)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.crosstrade_token:
        poller.start()
        try: await asyncio.wait_for(poller.refresh_once(),timeout=settings.request_timeout_seconds+1)
        except Exception: pass
    yield
    await poller.stop(); await client.close()

app=FastAPI(title="AutoProp Router",version="1.0.0-rc1",lifespan=lifespan)

def _auth(token):
    if not settings.webhook_token or token != settings.webhook_token: raise HTTPException(404,"Not found")

@app.get('/health')
async def health():
    states=await cache.snapshot()
    return {'ok':True,'version':'1.0.0-rc1','execution_mode':settings.execution_mode,'management_mode':settings.management_mode,
        'live_armed':settings.live_enabled,'full_scale_armed':settings.full_scale_armed,'alert_contract_verified':settings.alert_contract_verified,
        'live_max_qty_per_account':settings.live_max_qty_per_account,'cached_accounts':len(states),'auto_discovery':settings.auto_discovery,
        'registry_accounts':len(store.list_registry()),'poll_last_success':poller.last_success,'poll_last_error':poller.last_error,
        'state_poll_seconds':settings.state_poll_seconds,'max_state_age_seconds':settings.max_state_age_seconds}

@app.get('/admin/discover/{hook_token}')
async def discover(hook_token:str): _auth(hook_token); return await client.get_accounts()

@app.get('/admin/registry/{hook_token}')
async def registry(hook_token:str): _auth(hook_token); return {'manual_on_off':'CrossTrade Tradovate Account Manager -> Closing Only / Block Signals','accounts':store.list_registry()}

@app.get('/admin/accounts/{hook_token}')
async def admin_accounts(hook_token:str):
    _auth(hook_token); states=await cache.snapshot()
    return render_account_manager(token=hook_token,registry=store.list_registry(),states=states,overrides=store.list_overrides(),
        mode=settings.execution_mode,management_mode=settings.management_mode,armed=settings.live_enabled)

@app.post('/admin/account/{hook_token}/{account_id}/override')
async def save_override(hook_token:str,account_id:str,firm:str=Form(...),program:str=Form(...),phase:str=Form(...),
    starting_balance:float=Form(...),max_loss:float=Form(...),profit_target:float=Form(0),max_micros:int=Form(...),
    consistency_pct:str=Form(''),drawdown_type:str=Form(...),lock_offset:float=Form(0),risk_profile:str=Form('Standard'),
    mll_floor:str=Form(''),mll_locked:str=Form('false'),rules_verified:str|None=Form(None),risk_ready:str|None=Form(None)):
    _auth(hook_token)
    payload={'firm':firm.strip(),'program':program.strip(),'phase':phase,'starting_balance':starting_balance,'max_loss':max_loss,
        'profit_target':profit_target,'max_micros':max_micros,'consistency_pct':float(consistency_pct) if consistency_pct.strip() else None,
        'drawdown_type':drawdown_type,'lock_offset':lock_offset,'risk_profile':risk_profile,'rules_verified':rules_verified=='true','risk_ready':risk_ready=='true'}
    if mll_floor.strip(): payload['bootstrap_mll_floor']=float(mll_floor)
    store.set_override(account_id,payload)
    if mll_floor.strip():
        floor=float(mll_floor); peak=floor+max_loss
        store.reset_prop_state(account_id,mll_floor=floor,peak_eod_balance=peak,live_high_water=max(starting_balance,peak),mll_locked=mll_locked=='true')
    await cache.apply_override_now(account_id)
    return RedirectResponse(url=f'/admin/accounts/{hook_token}#edit-{account_id}',status_code=303)

@app.post('/admin/account/{hook_token}/{account_id}/override/reset')
async def reset_override(hook_token:str,account_id:str):
    _auth(hook_token); store.delete_override(account_id); await poller.refresh_once()
    return RedirectResponse(url=f'/admin/accounts/{hook_token}#edit-{account_id}',status_code=303)

@app.get('/admin/live-readiness/{hook_token}')
async def readiness(hook_token:str):
    _auth(hook_token); states=await cache.snapshot(); configs=await cache.configs_snapshot(); problems=[]; warnings=[]
    if not settings.crosstrade_token: problems.append('CROSSTRADE_TOKEN missing')
    if not settings.webhook_token: problems.append('AUTOPROP_WEBHOOK_TOKEN missing')
    if not settings.sqlite_path.startswith('/data/'): warnings.append('SQLite is not on Railway /data persistent volume')
    if poller.last_success is None: problems.append('No successful CrossTrade snapshot yet')
    if settings.management_mode not in {'native_atm','tv_managed'}: problems.append('Invalid AUTOPROP_MANAGEMENT_MODE')
    selected=[c for c in configs if c.enabled]
    if settings.execution_mode=='canary':
        selected=[c for c in selected if c.id==settings.canary_account_id]
        if not selected: problems.append('CANARY_ACCOUNT_ID does not match an enabled discovered account')
    for c in selected:
        if not c.rules_verified: problems.append(f'{c.id}: rules not verified')
        if not c.risk_ready: problems.append(f'{c.id}: MLL/risk state not ready')
        if c.id not in states: problems.append(f'{c.id}: no hot state')
    if settings.execution_mode=='live' and settings.live_max_qty_per_account==1:
        warnings.append('LIVE_MAX_QTY_PER_ACCOUNT=1 still caps production at one contract')
    if not settings.alert_contract_verified: warnings.append('TradingView alert contract is not marked verified; live mutation remains disarmed')
    return {'configuration_ready':len(problems)==0,'broker_mutation_armed':settings.live_enabled,'execution_mode':settings.execution_mode,
        'management_mode':settings.management_mode,'full_scale_armed':settings.full_scale_armed,'alert_contract_verified':settings.alert_contract_verified,
        'live_max_qty_per_account':settings.live_max_qty_per_account,'problems':problems,'warnings':warnings,'active_allocations':store.list_active_allocations()}

@app.get('/accounts')
async def account_state():
    states=await cache.snapshot(); return {'accounts':{k:v.model_dump(mode='json',exclude={'raw'}) for k,v in states.items()}}

@app.post('/admin/refresh')
async def refresh(background_tasks:BackgroundTasks):
    if not settings.crosstrade_token: raise HTTPException(503,'CROSSTRADE_TOKEN is not configured')
    background_tasks.add_task(poller.refresh_once); return {'accepted':True}

@app.post('/admin/dry-run/{hook_token}')
async def dry_run(hook_token:str, signal:TradeSignal):
    _auth(hook_token); response=await router.route(signal,mutate=False,claim=False); return JSONResponse(response.model_dump(mode='json'))

@app.post('/admin/normalize/{hook_token}')
async def normalize(hook_token:str,request:Request):
    _auth(hook_token); raw=await request.body()
    try: signal=normalize_payload(raw,request.headers.get('content-type'))
    except ValueError as exc: raise HTTPException(422,str(exc))
    return signal.model_dump(mode='json')

@app.post('/webhook/tradingview/{hook_token}')
async def tradingview(hook_token:str,request:Request):
    _auth(hook_token); raw=await request.body()
    try: signal=normalize_payload(raw,request.headers.get('content-type'))
    except ValueError as exc: raise HTTPException(422,str(exc))
    response=await router.route(signal); return JSONResponse(response.model_dump(mode='json'))
