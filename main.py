from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from version import __version__
from config import load_accounts, load_risk_states
from events import parse_alert, stable_event_id
from live import LiveRouter
from models import VerifiedRiskState
from readiness import readiness
from settings import Settings
from state import StateUnverified, ny_date
from store import Store
from crosstrade import InstrumentContractError, normalize_tradovate_symbol
from management import SILVER_STOP_CONTRACT

settings = Settings()
store = Store(settings.SQLITE_PATH)
app = FastAPI(title='AutoProp Router', version=__version__)
logger = logging.getLogger('autoprop.router')

_worker_task: asyncio.Task | None = None
_control_worker_task: asyncio.Task | None = None
_worker_wakeup: asyncio.Event | None = None
_control_worker_wakeup: asyncio.Event | None = None
_asw_reconcile_task: asyncio.Task | None = None
_entry_reconcile_task: asyncio.Task | None = None
_state_refresh_task: asyncio.Task | None = None
_runtime_instance: LiveRouter | None = None




def _annotate_execution_result(result):
    """Add execution outcome telemetry without changing durable worker retry semantics.

    webhook_inbox.status remains DONE when processing completed. Broker/account execution
    outcome is reported separately so a terminal all-destination failure cannot look healthy.
    """
    if not isinstance(result, dict) or result.get('kind') not in {'ENTRY','ASW_WORKING_LIMIT'}:
        return result
    rows = result.get('results')
    if not isinstance(rows, list):
        return result
    routed = sum(1 for r in rows if isinstance(r, dict)
                 and r.get('status') in {'ROUTED','PENDING_LIMIT','ACTIVE'})
    accepted_pending = sum(1 for r in rows if isinstance(r, dict)
                           and r.get('status') == 'ACCEPTED')
    errors = []
    skipped = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        status = str(r.get('status') or '')
        reason = str(r.get('reason') or '')
        # ERROR is the RC6 symbol-hotfix contract.  The HTTP prefix also recognizes
        # already-stored RC6 incidents such as the 2026-09-10 bare-MNQ HTTP 400.
        if status in {'ERROR', 'FLATTENED'} or (status == 'SKIP' and reason.startswith('HTTP ')):
            errors.append(reason)
        elif status in {'SKIP', 'ABORTED'}:
            skipped += 1
    total = len([r for r in rows if isinstance(r, dict)])
    if accepted_pending > 0 and errors:
        outcome = 'PARTIAL_ACCEPTANCE'
    elif accepted_pending > 0:
        outcome = 'ACCEPTED_PENDING_CONFIRMATION'
    elif routed > 0 and not errors:
        outcome = 'ROUTED'
    elif routed > 0:
        outcome = 'PARTIAL_DESTINATION_FAILURE'
    elif errors and len(errors) == total:
        outcome = 'ALL_DESTINATIONS_FAILED'
    elif errors:
        outcome = 'NO_ROUTES_WITH_EXECUTION_FAILURES'
    else:
        outcome = 'NO_DESTINATIONS_ELIGIBLE'
    out = dict(result)
    summary = {'outcome': outcome, 'total': total, 'routed': routed,
               'accepted_pending': accepted_pending,
               'execution_errors': len(errors), 'skipped': skipped}
    global_error = str(result.get('global_execution_error') or '')
    if global_error:
        summary['common_error'] = global_error
    elif errors and len(set(errors)) == 1:
        summary['common_error'] = errors[0]
    out['execution_summary'] = summary
    return out

async def _process_event(event, *, event_key: str = '', receipt_epoch: float | None = None):
    rt = _runtime()
    if event.kind == 'ENTRY':
        return await rt.route_entry(
            event, event_key=event_key, receipt_epoch=receipt_epoch
        )
    if event.kind == 'ASW_WORKING_LIMIT':
        return await rt.route_asw_working_limit(
            event, event_key=event_key, receipt_epoch=receipt_epoch
        )
    if event.kind == 'ASW_CANCEL_PENDING':
        return await rt.cancel_asw_pending(event)
    if event.kind == 'ASW_TIME_FLAT':
        return await rt.asw_time_flat(event)
    if event.kind == 'MARKET_PULSE':
        return await rt.market_pulse(event)
    if event.kind == 'SILVER_STOP_MOVE':
        return await rt.silver_stop(event)
    if event.kind == 'HARD_FLAT':
        return await rt.hard_flat(event)
    if event.kind == 'EXIT':
        return await rt.ordinary_exit(event)
    return {'accepted': True, 'kind': event.kind, 'action': 'observed_no_mutation'}


async def _webhook_worker(*, control: bool = False):
    # A separate control lane lets EXIT/HARD_FLAT advance while an ENTRY wave is awaiting
    # broker responses. Per-account durable attempt state supplies ordering across the lanes.
    global _worker_wakeup, _control_worker_wakeup
    while True:
        wakeup = _control_worker_wakeup if control else _worker_wakeup
        # Clear before checking durable work. If a webhook is committed after this clear,
        # its set() remains visible to the wait below; if it was committed earlier, the
        # following claim observes the durable row even though the hint was cleared.
        if wakeup is not None:
            wakeup.clear()
        row = (store.claim_next_control_webhook() if control
               else store.claim_next_regular_webhook())
        if row is None:
            if wakeup is None:
                await asyncio.sleep(0.25)
                continue
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            continue
        key = row['event_key']
        try:
            event = parse_alert(row['raw'])
            new_risk = event.kind in {'ENTRY', 'ASW_WORKING_LIMIT'}
            base = readiness(settings, _accounts()) if new_risk else None
            if new_risk and not settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED:
                raise RuntimeError('worker disarmed: TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false')
            if new_risk and store.entry_circuit().get('open'):
                raise RuntimeError(
                    f"new-risk circuit open: {store.entry_circuit().get('reason') or 'reset required'}"
                )
            if new_risk and store.unresolved_entry_attempt_count():
                raise RuntimeError('new-risk blocked: prior entry attempt is unresolved')
            if new_risk and base is not None and not base['configuration_ready']:
                raise RuntimeError(f"router configuration not ready: {base['problems']}")
            result = _annotate_execution_result(await _process_event(
                event, event_key=key,
                receipt_epoch=(time.time() if row.get('receipt_epoch') is None
                               else float(row['receipt_epoch'])),
            ))
            if isinstance(result, dict) and result.get('deferred'):
                retry_delay = min(
                    5.0,
                    0.5 * (2 ** min(max(0, int(row.get('attempts') or 0)), 4)),
                )
                store.defer_webhook(
                    key, str(result.get('reason') or 'awaiting broker reconciliation'),
                    retry_delay,
                )
                await asyncio.sleep(0.05)
                continue
            store.complete_webhook(key, result)
            summary = result.get('execution_summary') if isinstance(result, dict) else None
            if isinstance(summary, dict):
                outcome = str(summary.get('outcome') or '')
                log_args = (event.kind, getattr(event, 'engine', ''), outcome,
                            summary.get('routed'), summary.get('execution_errors'), summary.get('skipped'))
                if outcome in {'ALL_DESTINATIONS_FAILED', 'NO_ROUTES_WITH_EXECUTION_FAILURES'}:
                    logger.error('AutoProp execution kind=%s engine=%s outcome=%s routed=%s errors=%s skipped=%s', *log_args)
                elif outcome in {'PARTIAL_DESTINATION_FAILURE', 'PARTIAL_ACCEPTANCE'}:
                    logger.warning('AutoProp execution kind=%s engine=%s outcome=%s routed=%s errors=%s skipped=%s', *log_args)
                else:
                    logger.info('AutoProp execution kind=%s engine=%s outcome=%s routed=%s errors=%s skipped=%s', *log_args)
        except asyncio.CancelledError:
            # Leave PROCESSING durable; startup recovery will requeue it.
            raise
        except Exception as exc:
            store.fail_webhook(key, str(exc))
            logger.exception('AutoProp webhook worker failed event_key=%s kind=%s', key, row.get('kind'))
        finally:
            try:
                store.prune_dedupe(settings.DEDUPE_RETENTION_DAYS)
            except Exception:
                pass


async def _asw_reconcile_loop():
    while True:
        try:
            rt = _runtime()
            if not getattr(rt, 'entry_wave_active', None) or not rt.entry_wave_active.is_set():
                result = await rt.reconcile_asw_pending()
                if result.get('results'):
                    logger.info('ASW pending reconciliation %s', result['results'])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('ASW pending reconciliation failed')
        await asyncio.sleep(max(1.0, float(settings.ASW_PENDING_RECONCILE_SECONDS)))


async def _entry_reconcile_loop():
    while True:
        try:
            rt = _runtime()
            if not getattr(rt, 'entry_wave_active', None) or not rt.entry_wave_active.is_set():
                result = await rt.reconcile_entry_attempts()
                if result.get('results'):
                    logger.info('ENTRY reconciliation %s', result['results'])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('ENTRY reconciliation failed')
        await asyncio.sleep(max(0.1, float(settings.ENTRY_RECONCILE_LOOP_SECONDS)))


async def _state_refresh_loop():
    while True:
        try:
            rt = _runtime()
            if not getattr(rt, 'entry_wave_active', None) or not rt.entry_wave_active.is_set():
                result = await rt.refresh_state_cache()
                failed = [r for r in result.get('results', []) if r.get('status') == 'ERROR']
                if failed:
                    logger.warning('Account-state cache refresh failures %s', failed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Account-state cache refresh failed')
        await asyncio.sleep(max(1.0, float(settings.STATE_POLL_SECONDS)))


@app.on_event('startup')
async def _start_worker():
    global _worker_task, _control_worker_task, _worker_wakeup, _control_worker_wakeup
    global _asw_reconcile_task, _entry_reconcile_task, _state_refresh_task
    store.recover_processing_webhooks()
    if store.unresolved_entry_attempt_count():
        store.trip_entry_circuit(
            reason='process restarted with unresolved entry attempt(s); reconciliation required',
            outcome='STARTUP_RECOVERY',
        )
    _worker_wakeup = asyncio.Event()
    _control_worker_wakeup = asyncio.Event()
    # Safety controls start immediately. New-risk work starts only after one synchronous
    # account-state warm-up so a deploy cannot reject the first valid signal merely because
    # its cache has not yet been initialized.
    _control_worker_task = asyncio.create_task(
        _webhook_worker(control=True), name='autoprop-control-worker'
    )
    _control_worker_wakeup.set()
    try:
        primed = await asyncio.wait_for(
            _runtime().refresh_state_cache(),
            timeout=max(1.0, float(settings.STARTUP_STATE_WARM_TIMEOUT_SECONDS)),
        )
        failed = [row for row in primed.get('results', []) if row.get('status') == 'ERROR']
        if failed:
            logger.warning('Startup account-state cache warm-up failures %s', failed)
            store.trip_entry_circuit(
                reason=f'startup account-state cache warm-up failed: {failed}',
                outcome='STARTUP_STATE_CACHE_UNREADY',
            )
    except Exception as exc:
        logger.exception('Startup account-state cache warm-up failed')
        store.trip_entry_circuit(
            reason=f'startup account-state cache warm-up failed: {exc}',
            outcome='STARTUP_STATE_CACHE_UNREADY',
        )
    _worker_task = asyncio.create_task(
        _webhook_worker(control=False), name='autoprop-webhook-worker'
    )
    _asw_reconcile_task = asyncio.create_task(_asw_reconcile_loop(), name='autoprop-asw-reconcile')
    _entry_reconcile_task = asyncio.create_task(
        _entry_reconcile_loop(), name='autoprop-entry-reconcile'
    )
    _state_refresh_task = asyncio.create_task(
        _state_refresh_loop(), name='autoprop-state-refresh'
    )
    _worker_wakeup.set()


@app.on_event('shutdown')
async def _stop_worker():
    global _worker_task, _control_worker_task, _asw_reconcile_task
    global _entry_reconcile_task, _state_refresh_task, _runtime_instance
    global _worker_wakeup, _control_worker_wakeup
    tasks = [_worker_task, _control_worker_task, _asw_reconcile_task,
             _entry_reconcile_task, _state_refresh_task]
    for task in tasks:
        if task is not None:
            task.cancel()
    for task in tasks:
        if task is None:
            continue
        try:
            await task
        except asyncio.CancelledError:
            pass
    _worker_task = None
    _control_worker_task = None
    _asw_reconcile_task = None
    _entry_reconcile_task = None
    _state_refresh_task = None
    _worker_wakeup = None
    _control_worker_wakeup = None
    if _runtime_instance is not None:
        await _runtime_instance.client.close()
        _runtime_instance = None


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
    global _runtime_instance
    if _runtime_instance is None:
        _runtime_instance = LiveRouter(settings, _accounts(), store)
    return _runtime_instance


def _linked_account_names(payload):
    data = payload.get('data', payload) if isinstance(payload, dict) else []
    if not isinstance(data, list):
        return set()
    return {str(x.get('name')) for x in data if isinstance(x, dict) and x.get('name')}


@app.get('/health')
def health():
    accounts = _accounts()
    base = readiness(settings, accounts)
    try:
        execution_symbol = normalize_tradovate_symbol(settings.DEFAULT_EXECUTION_SYMBOL)
    except InstrumentContractError:
        execution_symbol = 'INVALID'
    circuit = store.entry_circuit()
    unresolved = store.unresolved_entry_attempt_count()
    return {
        'ok': True,
        'version': __version__,
        'execution_mode': settings.AUTOPROP_EXECUTION_MODE,
        'management_mode': settings.AUTOPROP_MANAGEMENT_MODE,
        'execution_symbol': execution_symbol,
        'tradingview_alert_contract_verified': settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED,
        'configuration_ready': base['configuration_ready'],
        'broker_mutation_armed': (base['broker_mutation_armed']
                                  and not circuit.get('open') and unresolved == 0),
        'registered_accounts': len(accounts),
        'active_trades': len(store.all_trades()),
        'asw_limit_contract': 'ASW_LIMIT_V1',
        'silver_stop_contract': SILVER_STOP_CONTRACT,
        'asw_pending_limits': len(store.all_asw_pending()),
        'entry_circuit_open': bool(circuit.get('open')),
        'entry_circuit_reason': str(circuit.get('reason') or ''),
        'unresolved_entry_attempts': unresolved,
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
    runtime_problem = None
    try:
        rt = _runtime()
    except Exception as exc:
        rt = None
        runtime_problem = f'live runtime initialization failed: {exc}'
    if rt is None:
        circuit = store.entry_circuit()
        unresolved = store.unresolved_entry_attempt_count()
        problems = list(base['problems']) + [runtime_problem or 'live runtime unavailable']
        if unresolved:
            problems.append(f'{unresolved} unresolved entry attempt(s) require reconciliation')
        if circuit.get('open'):
            problems.append(
                f"entry circuit open: {circuit.get('reason') or 'manual reset required'}"
            )
        return {
            **base,
            'configuration_ready': False,
            'broker_mutation_armed': False,
            'problems': problems,
            'warnings': list(base['warnings']),
            'states': [],
            'active_trades': len(store.all_trades()),
            'asw_limit_contract': 'ASW_LIMIT_V1',
            'asw_pending_limits': len(store.all_asw_pending()),
            'entry_circuit_open': bool(circuit.get('open')),
            'entry_circuit_reason': str(circuit.get('reason') or ''),
            'unresolved_entry_attempts': unresolved,
        }
    wave_marker = getattr(rt, 'entry_wave_active', None)
    if wave_marker is not None and wave_marker.is_set():
        raise HTTPException(409, 'entry wave active; retry readiness after dispatch')
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

    snapshot_cards = None
    snapshot_fn = getattr(rt, '_entry_snapshot', None)
    if snapshot_fn is not None:
        try:
            snapshot_cards = await snapshot_fn()
        except Exception as exc:
            state_problems.append(f'CrossTrade all-account snapshot failed: {exc}')

    async def inspect_account(a):
        try:
            state, signed = await asyncio.gather(rt.state_for(a), rt.position_qty(a))
            problems = []
            if snapshot_cards is not None:
                card = snapshot_cards.get(a.crosstrade_account)
                if card is None:
                    problems.append(f'{a.account_id}: missing from all-account snapshot')
                else:
                    positions = [row for row in card.get('positions', [])
                                 if isinstance(row, dict)]
                    working = [row for row in card.get('workingOrders', [])
                               if isinstance(row, dict)]
                    if positions:
                        problems.append(
                            f'{a.account_id}: broker snapshot has {len(positions)} open position(s)'
                        )
                    if working:
                        problems.append(
                            f'{a.account_id}: broker snapshot has {len(working)} working order(s)'
                        )
            if signed:
                problems.append(f'{a.account_id}: fill-reconciled net position is {signed}')
            return ({
                'account_id': a.account_id,
                'closed_cash_balance': state.closed_cash_balance,
                'mll_floor': state.mll_floor,
                'mll_verified': state.mll_verified,
                'daily_ledger_verified': state.daily_ledger_verified,
                'realized_today': state.realized_today,
                'largest_winning_day': state.largest_winning_day,
                'state_timestamp': state.state_timestamp.isoformat(),
                'net_position': signed,
            }, problems)
        except Exception as exc:
            return (None, [f'{a.account_id}: {exc}'])

    inspected = await asyncio.gather(*(
        inspect_account(a) for a in accounts if a.enabled
    ))
    for state_row, account_problems in inspected:
        if state_row is not None:
            states.append(state_row)
        state_problems.extend(account_problems)
    if wave_marker is not None and wave_marker.is_set():
        raise HTTPException(409, 'entry wave started during readiness; retry after dispatch')
    circuit = store.entry_circuit()
    circuit_problems = ([f"entry circuit open: {circuit.get('reason') or 'manual reset required'}"]
                        if circuit.get('open') else [])
    unresolved = store.unresolved_entry_attempt_count()
    unresolved_problems = ([f'{unresolved} unresolved entry attempt(s) require reconciliation']
                           if unresolved else [])
    problems = (list(base['problems']) + state_problems + circuit_problems
                + unresolved_problems)
    warnings = list(base['warnings']) + discovery_warnings
    return {
        **base,
        'configuration_ready': not problems,
        'broker_mutation_armed': (not problems and settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED),
        'problems': problems,
        'warnings': warnings,
        'states': states,
        'active_trades': len(store.all_trades()),
        'asw_limit_contract': 'ASW_LIMIT_V1',
        'asw_pending_limits': len(store.all_asw_pending()),
        'entry_circuit_open': bool(circuit.get('open')),
        'entry_circuit_reason': str(circuit.get('reason') or ''),
        'unresolved_entry_attempts': unresolved,
    }


@app.get('/admin/discovery/{token}')
async def discovery(token: str):
    _auth(token)
    rt = _runtime()
    if rt.entry_wave_active.is_set():
        raise HTTPException(409, 'entry wave active; retry discovery after dispatch')
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


@app.post('/admin/entry-circuit/reset/{token}')
async def reset_entry_circuit(token: str):
    _auth(token)
    if settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED:
        raise HTTPException(409, 'disarm TRADINGVIEW_ALERT_CONTRACT_VERIFIED before reset')
    if store.unresolved_entry_attempt_count():
        raise HTTPException(409, 'unresolved entry attempts must be reconciled first')
    if store.all_trades() or store.all_asw_pending():
        raise HTTPException(409, 'managed trades/pending limits must be flat first')
    rt = _runtime()
    wave_marker = getattr(rt, 'entry_wave_active', None)
    if wave_marker is not None and wave_marker.is_set():
        raise HTTPException(409, 'entry wave active; reset is not allowed')
    cards = await rt._entry_snapshot()
    if wave_marker is not None and wave_marker.is_set():
        raise HTTPException(409, 'entry wave started during reset; retry when idle')
    unsafe = []
    for rule in _accounts():
        if not rule.enabled:
            continue
        card = cards[rule.crosstrade_account]
        if card.get('positions') or card.get('workingOrders'):
            unsafe.append(rule.account_id)
    if unsafe:
        raise HTTPException(409, {'reason': 'broker accounts are not flat/clear',
                                  'accounts': unsafe})
    return {'reset': True, 'entry_circuit': store.reset_entry_circuit()}


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


@app.get('/admin/legacy-inspect/{token}')
def legacy_inspect(token: str):
    _auth(token)
    legacy_path = Path('/data/autoprop_router.sqlite3')
    if not legacy_path.exists():
        return {'legacy_path': str(legacy_path), 'exists': False, 'tables': []}
    uri = f"file:{legacy_path}?mode=ro"
    out = []
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        for name in names:
            safe_name = name.replace('"', '""')
            cols = [dict(r) for r in conn.execute(f'PRAGMA table_info("{safe_name}")').fetchall()]
            rows = []
            try:
                raw_rows = conn.execute(f'SELECT * FROM "{safe_name}" LIMIT 200').fetchall()
                for rr in raw_rows:
                    item = dict(rr)
                    for key in list(item):
                        lk = key.lower()
                        if any(secret in lk for secret in ('token','secret','password','api_key','apikey','webhook')):
                            item[key] = '<redacted>'
                    rows.append(item)
            except Exception as exc:
                rows = [{'error': str(exc)}]
            out.append({'table': name, 'columns': cols, 'rows': rows})
    return {'legacy_path': str(legacy_path), 'exists': True, 'tables': out}


@app.get('/admin/webhook-inbox/{token}')
def webhook_inbox(token: str, limit: int = 50):
    _auth(token)
    rows = store.webhook_rows(limit)
    counts = {'PENDING': 0, 'PROCESSING': 0, 'DONE': 0, 'FAILED': 0}
    for row in rows:
        status = str(row.get('status') or '')
        counts[status] = counts.get(status, 0) + 1
        # Backfill the execution summary at read time for older DONE rows too.
        if isinstance(row.get('result'), dict):
            row['result'] = _annotate_execution_result(row['result'])
    return {'version': __version__, 'counts': counts, 'events': rows}


@app.get('/admin/entry-attempts/{token}')
def entry_attempts(token: str, limit: int = 100):
    _auth(token)
    rows = store.all_entry_attempts()
    rows = rows[-max(1, min(int(limit), 500)):]
    return {
        'version': __version__,
        'entry_circuit': store.entry_circuit(),
        'unresolved': store.unresolved_entry_attempt_count(),
        'attempts': [row.model_dump(mode='json') for row in reversed(rows)],
    }


@app.post('/webhook/tradingview/{token}')
async def webhook(token: str, request: Request):
    _auth(token)
    raw = (await request.body()).decode('utf-8').strip()
    try:
        event = parse_alert(raw)
    except Exception as exc:
        raise HTTPException(400, f'alert parse failed: {exc}')

    new_risk = event.kind in {'ENTRY', 'ASW_WORKING_LIMIT'}
    if new_risk and not settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED:
        return {'accepted': False, 'disarmed': True, 'kind': event.kind,
                'reason': 'TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false'}
    if new_risk and store.entry_circuit().get('open'):
        raise HTTPException(503, {
            'reason': 'new-risk circuit open',
            'entry_circuit': store.entry_circuit(),
        })
    if new_risk and store.unresolved_entry_attempt_count():
        raise HTTPException(503, {
            'reason': 'prior entry attempt is unresolved',
            'unresolved_entry_attempts': store.unresolved_entry_attempt_count(),
        })

    base = readiness(settings, _accounts()) if new_risk else None
    if new_risk and base is not None and not base['configuration_ready']:
        raise HTTPException(503, {'reason': 'router configuration not ready', 'problems': base['problems']})

    # Durable fast-ACK contract: validate -> commit raw event to SQLite -> return 200.
    # The worker performs CrossTrade state/risk/execution after the response, so TradingView
    # is never forced to wait through seven-account broker I/O.  The NY date keeps identical
    # required-flat messages valid on future trading days while deduping same-day retries.
    receipt_epoch = time.time()
    day = ny_date(datetime.now(timezone.utc))
    control_kinds = {'EXIT', 'HARD_FLAT', 'ASW_CANCEL_PENDING', 'ASW_TIME_FLAT'}
    priority = {
        'HARD_FLAT': 100, 'ASW_TIME_FLAT': 100, 'ASW_CANCEL_PENDING': 95,
        'EXIT': 90, 'SILVER_STOP_MOVE': 10, 'MARKET_PULSE': 10,
        'ENTRY': 10, 'ASW_WORKING_LIMIT': 10,
    }.get(event.kind, 10)
    if event.kind in control_kinds:
        relevant = []
        for attempt in store.all_entry_attempts(
            ('PREPARED', 'SUBMITTING', 'ACCEPTED', 'ACTIVE', 'FLATTENING')
        ):
            if event.engine in {'', 'GLOBAL', 'ACCOUNT'} or attempt.engine == event.engine:
                relevant.append(attempt.event_id)
        for trade in store.all_trades():
            if event.engine in {'', 'GLOBAL', 'ACCOUNT'} or trade.engine == event.engine:
                relevant.append(trade.event_id)
        generation = stable_event_id('|'.join(sorted(set(relevant))) or 'flat')
        event_key = f"{stable_event_id(raw)}:{day}:{generation}"
        scope = ('GLOBAL' if event.engine in {'', 'GLOBAL', 'ACCOUNT'}
                 else event.engine)
        inserted = store.enqueue_control_webhook(
            event_key, raw, event.kind, receipt_epoch=receipt_epoch,
            priority=priority, engine=event.engine, side=event.side,
            fence_scope=scope,
        )
    else:
        event_key = f"{stable_event_id(raw)}:{day}"
        inserted = store.enqueue_webhook(
            event_key, raw, event.kind, receipt_epoch=receipt_epoch,
            priority=priority, engine=event.engine, side=event.side,
        )
    wakeup = _control_worker_wakeup if event.kind in control_kinds else _worker_wakeup
    if wakeup is not None:
        wakeup.set()
    return {
        'accepted': True,
        'queued': inserted,
        'duplicate': not inserted,
        'kind': event.kind,
        'event_key': event_key,
        'safety_control_allowed_while_disarmed': (not new_risk),
    }
