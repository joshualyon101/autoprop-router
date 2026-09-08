from __future__ import annotations

import json
import os
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import __version__
from .config import load_accounts, load_risk_states
from .events import parse_alert
from .live import LiveRouter
from .models import VerifiedRiskState
from .readiness import readiness
from .settings import Settings
from .state import StateUnverified
from .store import Store

settings = Settings()
store = Store(settings.SQLITE_PATH)
app = FastAPI(title='AutoProp Router', version=__version__)


def _accounts():
    return load_accounts(settings.ACCOUNT_CONFIG_PATH, settings.ACCOUNT_CONFIG_JSON)


def _seed_env_risk_states():
    for state in load_risk_states(settings.RISK_STATE_JSON):
        store.save_risk_state(state)


_seed_env_risk_states()
try:
    store.prune_dedupe(settings.DEDUPE_RETENTION_DAYS)
except Exception:
    pass


def _auth(token: str):
    if not settings.AUTOPROP_WEBHOOK_TOKEN or token != settings.AUTOPROP_WEBHOOK_TOKEN:
        raise HTTPException(404)


def _runtime():
    return LiveRouter(settings, _accounts(), store)


def _linked_account_names(payload):
    data = payload.get('data', payload) if isinstance(payload, dict) else []
    if not isinstance(data, list):
        return set()
    return {str(x.get('name')) for x in data if isinstance(x, dict) and x.get('name')}


@app.get('/health')
def health():
    accounts = _accounts()
    base = readiness(settings, accounts)
    return {
        'ok': True,
        'version': __version__,
        'execution_mode': settings.AUTOPROP_EXECUTION_MODE,
        'management_mode': settings.AUTOPROP_MANAGEMENT_MODE,
        'tradingview_alert_contract_verified': settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED,
        'configuration_ready': base['configuration_ready'],
        'broker_mutation_armed': base['broker_mutation_armed'],
        'registered_accounts': len(accounts),
        'active_trades': len(store.all_trades()),
    }


@app.get('/admin/accounts/{token}')
def accounts_admin(token: str):
    _auth(token)
    risk = {x.account_id: x for x in store.all_risk_states()}
    rows = []
    for a in _accounts():
        r = risk.get(a.account_id)
        rows.append({
            'rule': a.model_dump(),
            'risk_state': r.model_dump(mode='json') if r else None,
            'active_trade': store.get_trade(a.account_id).model_dump() if store.get_trade(a.account_id) else None,
        })
    return {'version': __version__, 'accounts': rows}


@app.get('/admin/live-readiness/{token}')
async def live_readiness(token: str):
    _auth(token)
    accounts = _accounts()
    base = readiness(settings, accounts)
    rt = LiveRouter(settings, accounts, store)
    states = []
    state_problems = []
    discovery_warnings = []
    try:
        linked = _linked_account_names(await rt.client.list_accounts())
        configured = {a.crosstrade_account for a in accounts}
        for a in accounts:
            if a.enabled and a.crosstrade_account not in linked:
                state_problems.append(f'{a.account_id}: configured CrossTrade account is not linked/discovered')
        extras = sorted(linked - configured)
        if extras:
            discovery_warnings.append(f'{len(extras)} linked CrossTrade account(s) are unconfigured and will not receive trades')
    except Exception as exc:
        state_problems.append(f'CrossTrade account discovery failed: {exc}')
    for a in accounts:
        if not a.enabled:
            continue
        try:
            state = await rt.state_for(a)
            signed = await rt.position_qty(a)
            states.append({
                'account_id': a.account_id,
                'closed_cash_balance': state.closed_cash_balance,
                'mll_floor': state.mll_floor,
                'mll_verified': state.mll_verified,
                'daily_ledger_verified': state.daily_ledger_verified,
                'realized_today': state.realized_today,
                'largest_winning_day': state.largest_winning_day,
                'state_timestamp': state.state_timestamp.isoformat(),
                'net_position': signed,
            })
        except Exception as exc:
            state_problems.append(f'{a.account_id}: {exc}')
    problems = list(base['problems']) + state_problems
    warnings = list(base['warnings']) + discovery_warnings
    return {
        **base,
        'configuration_ready': not problems,
        'broker_mutation_armed': (not problems and settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED),
        'problems': problems,
        'warnings': warnings,
        'states': states,
        'active_trades': len(store.all_trades()),
    }


@app.get('/admin/discovery/{token}')
async def discovery(token: str):
    _auth(token)
    rt = LiveRouter(settings, _accounts(), store)
    payload = await rt.client.list_accounts()
    linked = sorted(_linked_account_names(payload))
    configured = {a.crosstrade_account: a.account_id for a in _accounts()}
    return {
        'linked_accounts': linked,
        'configured_accounts': configured,
        'unconfigured_linked_accounts': [x for x in linked if x not in configured],
        'missing_linked_accounts': [name for name in configured if name not in linked],
    }


@app.get('/admin/dry-run/{token}')
async def dry_run(token: str):
    _auth(token)
    # Read-only broker/state pass. No order mutation and no synthetic strategy plan.
    result = await live_readiness(token)
    result['dry_run'] = True
    result['broker_mutation_performed'] = False
    return result


class RiskStateWrite(BaseModel):
    account_id: str
    mll_floor: float | None = None
    mll_verified: bool = False
    funded_locked: bool = False
    largest_winning_day: float = 0.0
    ledger_verified: bool = False
    cycle_start_utc: datetime | None = None
    source: str = 'manual_verified_dashboard'


@app.post('/admin/risk-state/{token}')
def write_risk_state(token: str, body: RiskStateWrite):
    _auth(token)
    known = {a.account_id for a in _accounts()}
    if body.account_id not in known:
        raise HTTPException(400, 'unknown account_id')
    state = VerifiedRiskState(**body.model_dump(), verified_at=datetime.now(timezone.utc))
    store.save_risk_state(state)
    return {'saved': True, 'risk_state': state.model_dump(mode='json')}


@app.get('/admin/storage/{token}')
def storage(token: str):
    _auth(token)
    root = Path('/data')
    files = []
    if root.exists():
        for p in root.rglob('*'):
            try:
                if p.is_file():
                    files.append({'path': str(p), 'bytes': p.stat().st_size})
            except OSError:
                pass
    files.sort(key=lambda x: x['bytes'], reverse=True)
    return {
        'sqlite_path': settings.SQLITE_PATH,
        'sqlite_bytes': Path(settings.SQLITE_PATH).stat().st_size if Path(settings.SQLITE_PATH).exists() else 0,
        'largest_files': files[:25],
        'sqlite_tables': store.table_inventory(),
    }


@app.post('/webhook/tradingview/{token}')
async def webhook(token: str, request: Request):
    _auth(token)
    raw = (await request.body()).decode('utf-8').strip()
    try:
        event = parse_alert(raw)
    except Exception as exc:
        raise HTTPException(400, f'alert parse failed: {exc}')

    if not settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED:
        return {'accepted': False, 'disarmed': True, 'kind': event.kind,
                'reason': 'TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false'}

    base = readiness(settings, _accounts())
    if not base['configuration_ready']:
        raise HTTPException(503, {'reason': 'router configuration not ready', 'problems': base['problems']})

    rt = _runtime()
    try:
        if event.kind == 'ENTRY':
            return await rt.route_entry(event)
        if event.kind == 'MARKET_PULSE':
            return await rt.market_pulse(event)
        if event.kind == 'SILVER_STOP_MOVE':
            return await rt.silver_stop(event)
        if event.kind == 'HARD_FLAT':
            return await rt.hard_flat(event)
        if event.kind == 'EXIT':
            return await rt.ordinary_exit(event)
        return {'accepted': True, 'kind': event.kind, 'action': 'observed_no_mutation'}
    finally:
        try:
            store.prune_dedupe(settings.DEDUPE_RETENTION_DAYS)
        except Exception:
            pass
