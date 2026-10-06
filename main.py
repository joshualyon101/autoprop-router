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
from onboarding import discover_and_onboard
from shadow import ShadowObserver
from state_fallback import is_transient_state_failure

settings = Settings()
_EXECUTION_MODE = str(settings.AUTOPROP_EXECUTION_MODE or '').strip().lower()
_LIVE_MODE = _EXECUTION_MODE == 'live'
_SHADOW_MODE = _EXECUTION_MODE == 'shadow'
# Shadow observations must never recover or process durable live ownership rows.  A
# separate database also makes a mode change explicit and keeps old live circuits from
# blocking passive intake.
store = Store(
    str(getattr(settings, 'SHADOW_SQLITE_PATH', '/data/autoprop_router_shadow.sqlite3'))
    if not _LIVE_MODE else settings.SQLITE_PATH
)
app = FastAPI(title='AutoProp Router', version=__version__)
logger = logging.getLogger('autoprop.router')

_worker_task: asyncio.Task | None = None
_control_worker_task: asyncio.Task | None = None
_worker_wakeup: asyncio.Event | None = None
_control_worker_wakeup: asyncio.Event | None = None
_asw_reconcile_task: asyncio.Task | None = None
_entry_reconcile_task: asyncio.Task | None = None
_silver_management_task: asyncio.Task | None = None
_state_refresh_task: asyncio.Task | None = None
_auto_discovery_task: asyncio.Task | None = None
_auto_discovery_lock: asyncio.Lock | None = None
_last_auto_discovery: dict = {}
_runtime_instance: LiveRouter | None = None
_shadow_instance: ShadowObserver | None = None




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
    if _SHADOW_MODE:
        return await _shadow_runtime().observe(
            event, event_key=event_key, receipt_epoch=receipt_epoch
        )
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
        return await rt.silver_stop(event, event_key=event_key)
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
        row = (store.claim_next_webhook() if _SHADOW_MODE
               else (store.claim_next_control_webhook() if control
                     else store.claim_next_regular_webhook()))
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
            if not _SHADOW_MODE:
                if new_risk and not settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED:
                    raise RuntimeError('worker disarmed: TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false')
                if new_risk and store.entry_circuit().get('open'):
                    raise RuntimeError(
                        f"new-risk circuit open: {store.entry_circuit().get('reason') or 'reset required'}"
                    )
                if new_risk and store.unresolved_entry_attempt_count():
                    raise RuntimeError('new-risk blocked: prior entry attempt is unresolved')
                if new_risk and store.silver_stop_intent():
                    raise RuntimeError('new-risk blocked: Silver protection intent is pending')
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


async def _silver_management_loop():
    """Service durable Silver protection independently of webhook retry accounting."""
    while True:
        rt = _runtime()
        wakeup = rt.silver_management_wakeup
        wakeup.clear()
        try:
            result = await rt.service_silver_stop_intent()
            if result.get('status') not in {'IDLE', 'BACKOFF'}:
                logger.info('SILVER priority management %s', result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('SILVER priority management failed')
        try:
            await asyncio.wait_for(
                wakeup.wait(),
                timeout=max(0.05, float(settings.SILVER_MANAGEMENT_LOOP_SECONDS)),
            )
        except asyncio.TimeoutError:
            pass


async def _state_refresh_loop():
    while True:
        delay = max(1.0, float(settings.STATE_POLL_SECONDS))
        try:
            rt = _runtime()
            if rt.safety_work_pending():
                # Recheck promptly after a trade closes so the entry cache can warm for
                # the next signal instead of remaining stale for a full poll interval.
                delay = 1.0
            else:
                result = await rt.refresh_state_cache()
                failed = [r for r in result.get('results', []) if r.get('status') == 'ERROR']
                if failed:
                    logger.warning('Account-state cache refresh failures %s', failed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Account-state cache refresh failed')
        await asyncio.sleep(delay)


async def _run_auto_discovery() -> dict:
    """Run one coalesced, read-first discovery pass and publish proven challenges."""
    global _auto_discovery_lock, _last_auto_discovery
    if not settings.AUTO_DISCOVERY:
        return {'enabled': False, 'onboarded': [], 'quarantined': [], 'existing': []}
    if _auto_discovery_lock is None:
        _auto_discovery_lock = asyncio.Lock()
    if _auto_discovery_lock.locked():
        return {'enabled': True, 'coalesced': True, 'onboarded': [], 'quarantined': []}
    async with _auto_discovery_lock:
        rt = _runtime()
        if rt.safety_work_pending():
            return {
                'enabled': True, 'paused_for_safety': True,
                'onboarded': [], 'quarantined': [], 'existing': [],
            }
        # Discovery and balance/history refresh are both low-priority broker scans. Keep
        # them off each other's account reads, and re-check safety after waiting for the
        # shared background lane.
        background_lock = rt._state_refresh_batch_lock
        async with background_lock:
            if rt.safety_work_pending():
                return {
                    'enabled': True, 'paused_for_safety': True,
                    'onboarded': [], 'quarantined': [], 'existing': [],
                }
            result = await discover_and_onboard(
                settings, rt.client, store, _accounts(),
                publish_guard=lambda: not rt.safety_work_pending(),
            )
        if result.get('onboarded'):
            rt.replace_accounts(_accounts())
            logger.warning('Zero-touch account onboarding %s', result['onboarded'])
        _last_auto_discovery = result
        return result


async def _auto_discovery_loop():
    while True:
        try:
            rt = _runtime()
            if not rt.safety_work_pending():
                result = await _run_auto_discovery()
                if result.get('quarantined'):
                    logger.warning('Auto-discovery quarantined account(s) %s',
                                   result['quarantined'])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Auto-discovery pass failed')
        await asyncio.sleep(max(
            2.0, float(getattr(settings, 'AUTO_DISCOVERY_INTERVAL_SECONDS', 10.0))
        ))


@app.on_event('startup')
async def _start_worker():
    global _worker_task, _control_worker_task, _worker_wakeup, _control_worker_wakeup
    global _asw_reconcile_task, _entry_reconcile_task, _silver_management_task
    global _state_refresh_task
    global _auto_discovery_task
    store.recover_processing_webhooks()
    if not _LIVE_MODE and not _SHADOW_MODE:
        # Invalid roles fail inert: no inbox worker, no broker client, no recovery loop.
        logger.error('Unsupported AUTOPROP_EXECUTION_MODE=%r; router remains inert',
                     settings.AUTOPROP_EXECUTION_MODE)
        return
    if _SHADOW_MODE:
        # A shadow process owns no broker state.  It runs one durable observation worker
        # and deliberately omits every live control/reconciliation/management/read loop.
        _worker_wakeup = asyncio.Event()
        _control_worker_wakeup = None
        _worker_task = asyncio.create_task(
            _webhook_worker(control=False), name='autoprop-shadow-observer-worker'
        )
        _worker_wakeup.set()
        logger.warning(
            'AutoProp started as shadow_observer; broker mutations disabled and live loops skipped'
        )
        return
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
        await _run_auto_discovery()
    except Exception:
        # Discovery is additive. A transient account-list failure must not prevent the
        # already-configured accounts from warming and retaining protection/control.
        logger.exception('Startup auto-discovery pass failed')
    try:
        primed = await asyncio.wait_for(
            _runtime().refresh_state_cache(),
            timeout=max(1.0, float(settings.STARTUP_STATE_WARM_TIMEOUT_SECONDS)),
        )
        failed = [row for row in primed.get('results', []) if row.get('status') == 'ERROR']
        if failed:
            logger.warning('Startup account-state cache warm-up failures %s', failed)
            transient = [row for row in failed if is_transient_state_failure(
                str(row.get('reason') or '')
            )]
            hard = [row for row in failed if row not in transient]
            if transient:
                store.note_state_fallback_failure(transient)
            if hard:
                store.trip_entry_circuit(
                    reason=f'startup account-state cache warm-up failed: {hard}',
                    outcome='STARTUP_STATE_CACHE_UNREADY',
                )
    except Exception as exc:
        logger.exception('Startup account-state cache warm-up failed')
        if is_transient_state_failure(exc):
            store.note_state_fallback_failure([{
                'account_id': 'GLOBAL', 'reason': str(exc),
            }])
        else:
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
    _silver_management_task = asyncio.create_task(
        _silver_management_loop(), name='autoprop-silver-management'
    )
    _state_refresh_task = asyncio.create_task(
        _state_refresh_loop(), name='autoprop-state-refresh'
    )
    _auto_discovery_task = asyncio.create_task(
        _auto_discovery_loop(), name='autoprop-auto-discovery'
    )
    _worker_wakeup.set()


@app.on_event('shutdown')
async def _stop_worker():
    global _worker_task, _control_worker_task, _asw_reconcile_task
    global _entry_reconcile_task, _silver_management_task
    global _state_refresh_task, _auto_discovery_task
    global _runtime_instance, _shadow_instance, _auto_discovery_lock
    global _worker_wakeup, _control_worker_wakeup
    tasks = [_worker_task, _control_worker_task, _asw_reconcile_task,
             _entry_reconcile_task, _silver_management_task,
             _state_refresh_task, _auto_discovery_task]
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
    _silver_management_task = None
    _state_refresh_task = None
    _auto_discovery_task = None
    _auto_discovery_lock = None
    _worker_wakeup = None
    _control_worker_wakeup = None
    if _runtime_instance is not None:
        await _runtime_instance.client.close()
        _runtime_instance = None
    _shadow_instance = None


def _accounts():
    configured = load_accounts(settings.ACCOUNT_CONFIG_PATH, settings.ACCOUNT_CONFIG_JSON)
    # Explicit configuration always wins. Auto-onboarded accounts are a durable overlay
    # stored on the Railway volume so no environment-variable edit is required.
    seen_ids = {rule.account_id for rule in configured}
    seen_names = {rule.crosstrade_account for rule in configured}
    merged = list(configured)
    for rule in store.all_auto_accounts():
        if rule.account_id in seen_ids or rule.crosstrade_account in seen_names:
            continue
        merged.append(rule)
        seen_ids.add(rule.account_id)
        seen_names.add(rule.crosstrade_account)
    return merged


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
    if _SHADOW_MODE:
        raise RuntimeError('live runtime is unavailable in shadow mode')
    if _runtime_instance is None:
        _runtime_instance = LiveRouter(settings, _accounts(), store)
    return _runtime_instance


def _shadow_runtime() -> ShadowObserver:
    global _shadow_instance
    if not _SHADOW_MODE:
        raise RuntimeError('shadow observer is unavailable in live mode')
    if _shadow_instance is None:
        _shadow_instance = ShadowObserver(store, _accounts)
    return _shadow_instance


def _linked_account_names(payload):
    data = payload.get('data', payload) if isinstance(payload, dict) else []
    if not isinstance(data, list):
        return set()
    return {str(x.get('name')) for x in data if isinstance(x, dict) and x.get('name')}


@app.get('/health')
def health():
    accounts = _accounts()
    base = readiness(settings, accounts)
    auto_onboarding_enabled = bool(
        settings.AUTO_DISCOVERY
        and getattr(settings, 'AUTO_ONBOARD_VERIFIED_CHALLENGE_COHORTS', True)
    )
    try:
        execution_symbol = normalize_tradovate_symbol(settings.DEFAULT_EXECUTION_SYMBOL)
    except InstrumentContractError:
        execution_symbol = 'INVALID'
    circuit = store.entry_circuit()
    unresolved = store.unresolved_entry_attempt_count()
    silver_intent = store.silver_stop_intent()
    shadow_summary = store.get_runtime_state('shadow_observer_summary') or {}
    shadow_last = shadow_summary.get('last_event') or {}
    shadow_ready = bool(base.get('shadow_observation_ready', base['configuration_ready']))
    state_fallback = store.state_fallback_status()
    broker_armed = False if _SHADOW_MODE else (
        base['broker_mutation_armed'] and not circuit.get('open') and unresolved == 0
        and not silver_intent
    )
    return {
        'ok': True,
        'version': __version__,
        'execution_mode': settings.AUTOPROP_EXECUTION_MODE,
        'router_role': 'shadow_observer' if _SHADOW_MODE else 'live_router',
        'production_execution_path': (
            'direct_tradingview_to_crosstrade' if _SHADOW_MODE
            else 'tradingview_to_router_to_crosstrade'
        ),
        'management_mode': settings.AUTOPROP_MANAGEMENT_MODE,
        'execution_symbol': execution_symbol,
        'tradingview_alert_contract_verified': settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED,
        'configuration_ready': base['configuration_ready'],
        'shadow_intake_ready': shadow_ready if _SHADOW_MODE else False,
        'shadow_observation_ready': shadow_ready if _SHADOW_MODE else False,
        'shadow_capability': 'alert_plan_observer' if _SHADOW_MODE else '',
        'shadow_quantity_simulation': False,
        'shadow_broker_reads_enabled': bool(
            getattr(settings, 'SHADOW_BROKER_READS_ENABLED', False)
        ) if _SHADOW_MODE else False,
        'broker_mutation_capable': not _SHADOW_MODE,
        'broker_mutation_armed': broker_armed,
        'broker_mutation_firewall': bool(_SHADOW_MODE),
        'broker_bracket_authority': (
            'direct_crosstrade' if _SHADOW_MODE else 'router_managed'
        ),
        'registered_accounts': len(accounts),
        'auto_discovery_enabled': bool(settings.AUTO_DISCOVERY) and not _SHADOW_MODE,
        'auto_discovery_configured': bool(settings.AUTO_DISCOVERY),
        'auto_onboarding_enabled': auto_onboarding_enabled and not _SHADOW_MODE,
        'auto_onboarding_policy': 'verified_challenge_cohort_or_known_profile',
        'fundednext_default_model': str(settings.FUNDEDNEXT_DEFAULT_MODEL),
        'auto_onboarded_accounts': len(store.all_auto_accounts()),
        'active_trades': len(store.all_trades()),
        'asw_limit_contract': 'ASW_LIMIT_V1',
        'silver_stop_contract': SILVER_STOP_CONTRACT,
        'asw_pending_limits': len(store.all_asw_pending()),
        'entry_circuit_open': bool(circuit.get('open')),
        'entry_circuit_reason': str(circuit.get('reason') or ''),
        'state_fallback_active': bool(state_fallback.get('active')),
        'state_fallback_entry_waves_used': int(
            state_fallback.get('entry_waves_used') or 0
        ),
        'state_fallback_max_entry_waves': int(getattr(
            settings, 'STATE_FALLBACK_MAX_ENTRY_WAVES', 5
        )),
        'state_fallback_max_duration_seconds': float(getattr(
            settings, 'STATE_FALLBACK_MAX_DURATION_SECONDS', 3600.0
        )),
        'state_fallback_failing_accounts': state_fallback.get('failing_accounts') or [],
        'unresolved_entry_attempts': unresolved,
        'silver_management_pending': bool(silver_intent),
        'silver_management_stage': int(silver_intent.get('stage') or 0),
        'shadow_observed_events': int(shadow_summary.get('total_events') or 0),
        'shadow_last_event_kind': str(shadow_last.get('kind') or ''),
        'shadow_last_event_key': str(shadow_last.get('event_key') or ''),
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
    if _SHADOW_MODE:
        raise HTTPException(
            409,
            'router role is shadow_observer; use /admin/shadow-readiness/{token}',
        )
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
        state_fallback = store.state_fallback_status()
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
            'state_fallback': state_fallback,
            'unresolved_entry_attempts': unresolved,
            'silver_management_pending': bool(store.silver_stop_intent()),
            'silver_management_stage': int(
                (store.silver_stop_intent() or {}).get('stage') or 0
            ),
        }
    wave_marker = getattr(rt, 'entry_wave_active', None)
    if wave_marker is not None and wave_marker.is_set():
        raise HTTPException(409, 'entry wave active; retry readiness after dispatch')
    if getattr(rt, 'safety_work_pending', lambda: False)():
        raise HTTPException(
            409,
            'safety-critical reconciliation/management active; use /health and retry '
            'live-readiness after positions are flat',
        )
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

    # Diagnostics are low-priority and must never enqueue a fleet-wide GET fanout that can
    # sit ahead of entry reconciliation or protection management.
    inspected = []
    for account in accounts:
        if not account.enabled:
            continue
        if getattr(rt, 'safety_work_pending', lambda: False)():
            raise HTTPException(
                409, 'safety-critical broker work started during readiness; retry when flat'
            )
        inspected.append(await inspect_account(account))
    for state_row, account_problems in inspected:
        if state_row is not None:
            states.append(state_row)
        for problem in account_problems:
            if is_transient_state_failure(problem):
                discovery_warnings.append(
                    f'{problem} (bounded conservative state fallback remains available)'
                )
                store.note_state_fallback_failure([{
                    'account_id': str(problem).split(':', 1)[0],
                    'reason': str(problem),
                }])
            else:
                state_problems.append(problem)
    if wave_marker is not None and wave_marker.is_set():
        raise HTTPException(409, 'entry wave started during readiness; retry after dispatch')
    circuit = store.entry_circuit()
    state_fallback = store.state_fallback_status()
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
        'state_fallback': state_fallback,
        'unresolved_entry_attempts': unresolved,
        'silver_management_pending': bool(store.silver_stop_intent()),
        'silver_management_stage': int(
            (store.silver_stop_intent() or {}).get('stage') or 0
        ),
    }


@app.get('/admin/shadow-readiness/{token}')
async def shadow_readiness(token: str):
    """Report passive-intake readiness without contacting the broker."""
    _auth(token)
    if not _SHADOW_MODE:
        raise HTTPException(409, 'router role is live_router; shadow intake is disabled')
    base = readiness(settings, _accounts())
    summary = store.get_runtime_state('shadow_observer_summary') or {}
    last_event = summary.get('last_event') or {}
    shadow_ready = bool(base.get('shadow_observation_ready', base['configuration_ready']))
    return {
        **base,
        'router_role': 'shadow_observer',
        'production_execution_path': 'direct_tradingview_to_crosstrade',
        'shadow_intake_ready': shadow_ready,
        'shadow_observation_ready': shadow_ready,
        'shadow_capability': 'alert_plan_observer',
        'shadow_quantity_simulation': False,
        'broker_mutation_capable': False,
        'broker_mutation_armed': False,
        'broker_mutation_firewall': True,
        'broker_bracket_authority': 'direct_crosstrade',
        'shadow_broker_reads_enabled': bool(
            getattr(settings, 'SHADOW_BROKER_READS_ENABLED', False)
        ),
        'shadow_database_path': store.path,
        'shadow_observed_events': int(summary.get('total_events') or 0),
        'shadow_last_event_kind': str(last_event.get('kind') or ''),
        'shadow_last_event_key': str(last_event.get('event_key') or ''),
        'live_circuit_gates_shadow_intake': False,
    }


@app.get('/admin/discovery/{token}')
async def discovery(token: str):
    _auth(token)
    if _SHADOW_MODE:
        raise HTTPException(
            409,
            'broker discovery is disabled in shadow mode to protect the direct execution path',
        )
    rt = _runtime()
    if rt.entry_wave_active.is_set():
        raise HTTPException(409, 'entry wave active; retry discovery after dispatch')
    if getattr(rt, 'safety_work_pending', lambda: False)():
        raise HTTPException(409, 'safety-critical broker work active; retry discovery later')
    scan = await _run_auto_discovery()
    payload = await rt.client.list_accounts()
    linked = sorted(_linked_account_names(payload))
    configured = {a.crosstrade_account: a.account_id for a in _accounts()}
    return {
        'linked_accounts': linked,
        'configured_accounts': configured,
        'unconfigured_linked_accounts': [x for x in linked if x not in configured],
        'missing_linked_accounts': [name for name in configured if name not in linked],
        'zero_touch_scan': scan,
        'auto_onboarded_accounts': [
            rule.model_dump() for rule in store.all_auto_accounts()
        ],
        'auto_discovery_audit': store.all_auto_discovery_audit(),
    }


@app.get('/admin/dry-run/{token}')
async def dry_run(token: str):
    _auth(token)
    if _SHADOW_MODE:
        result = await shadow_readiness(token)
        result['dry_run'] = True
        result['broker_mutation_performed'] = False
        return result
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
    if _SHADOW_MODE:
        raise HTTPException(409, 'shadow observer owns no live entry circuit')
    if settings.TRADINGVIEW_ALERT_CONTRACT_VERIFIED:
        raise HTTPException(409, 'disarm TRADINGVIEW_ALERT_CONTRACT_VERIFIED before reset')
    if store.unresolved_entry_attempt_count():
        raise HTTPException(409, 'unresolved entry attempts must be reconciled first')
    if store.all_trades() or store.all_asw_pending():
        raise HTTPException(409, 'managed trades/pending limits must be flat first')
    if store.silver_stop_intent():
        raise HTTPException(409, 'Silver protection intent must finish before circuit reset')
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
        'sqlite_path': store.path,
        'sqlite_bytes': Path(store.path).stat().st_size if Path(store.path).exists() else 0,
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


@app.post('/webhook/tradingview-shadow/{token}')
async def shadow_webhook(token: str, request: Request):
    """Durably accept a parsed alert for passive observation only.

    This route intentionally ignores the live alert gate, entry circuit, unresolved live
    attempts, and Silver management state.  It never creates control fences or ownership
    rows and is available only when the whole process booted as a shadow observer.
    """
    _auth(token)
    if not _SHADOW_MODE:
        raise HTTPException(409, 'shadow webhook is disabled while router role is live_router')
    raw = (await request.body()).decode('utf-8').strip()
    try:
        event = parse_alert(raw)
    except Exception as exc:
        raise HTTPException(400, f'alert parse failed: {exc}')
    base = readiness(settings, _accounts())
    if not base['configuration_ready']:
        raise HTTPException(503, {
            'reason': 'shadow observer configuration not ready',
            'problems': base['problems'],
        })
    receipt_epoch = time.time()
    day = ny_date(datetime.now(timezone.utc))
    # Pine's ordinary EXIT bodies do not carry a trade/time identifier.  A raw+day key
    # would collapse two legitimate same-engine exits into one observation, so shadow
    # intake records every successfully delivered alert.  Duplicate delivery is harmless
    # because this lane cannot mutate the broker.
    event_key = f"shadow:{stable_event_id(raw)}:{day}:{time.time_ns()}"
    inserted = store.enqueue_webhook(
        event_key, raw, event.kind, receipt_epoch=receipt_epoch,
        priority=10, engine=event.engine, side=event.side,
    )
    if _worker_wakeup is not None:
        _worker_wakeup.set()
    return {
        'accepted': True,
        'queued': inserted,
        'duplicate': not inserted,
        'kind': event.kind,
        'event_key': event_key,
        'router_role': 'shadow_observer',
        'broker_mutation_performed': False,
        'tradingview_gate_required': False,
        'live_circuit_gates_shadow_intake': False,
    }


@app.post('/webhook/tradingview/{token}')
async def webhook(token: str, request: Request):
    _auth(token)
    if _SHADOW_MODE:
        raise HTTPException(409, {
            'reason': 'live router webhook disabled in shadow mode',
            'use': '/webhook/tradingview-shadow/{token}',
        })
    if not _LIVE_MODE:
        raise HTTPException(503, 'router execution mode is invalid; webhook intake is inert')
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
    if new_risk and store.silver_stop_intent():
        raise HTTPException(503, {
            'reason': 'Silver protection intent is pending',
            'silver_stop_intent': store.silver_stop_intent(),
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
        'EXIT': 90, 'SILVER_STOP_MOVE': 80, 'MARKET_PULSE': 10,
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
