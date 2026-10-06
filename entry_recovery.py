"""Read-only proof followed by an atomic reset of one resolved readback circuit."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone


def eligible_readback_circuit(circuit: dict) -> bool:
    reason = str(circuit.get('reason') or '').lower()
    return bool(
        circuit.get('open')
        and circuit.get('outcome') == 'READBACK_UNCONFIRMED'
        and circuit.get('event_key')
        and 'accepted entry readback timed out:' in reason
        and 'network error' in reason
        and any(marker in reason for marker in (
            'readtimeout', 'connecttimeout', 'pooltimeout', 'writetimeout',
            'readerror', 'connecterror', 'remoteprotocolerror',
        ))
    )


async def recover_readback_circuit(router) -> dict:
    settings = router.settings
    if (str(getattr(settings, 'AUTOPROP_EXECUTION_MODE', '')).lower() != 'live'
            or not getattr(settings, 'READBACK_AUTO_RECOVERY_ENABLED', True)):
        return {'status': 'DISABLED'}
    circuit = router.store.entry_circuit()
    if not eligible_readback_circuit(circuit):
        router._readback_recovery_candidate = None
        return {'status': 'INELIGIBLE' if circuit.get('open') else 'IDLE'}

    lock = getattr(router, '_readback_recovery_lock', None)
    if lock is None:
        lock = router._readback_recovery_lock = asyncio.Lock()
    if lock.locked():
        return {'status': 'COALESCED'}
    async with lock:
        now = time.monotonic()
        interval = max(5.0, float(getattr(
            settings, 'READBACK_AUTO_RECOVERY_INTERVAL_SECONDS', 10.0
        )))
        last_scan = getattr(router, '_readback_recovery_last_scan', None)
        if last_scan is not None and now - last_scan < interval:
            return {'status': 'BACKOFF'}
        router._readback_recovery_last_scan = now

        def report(status: str, reason: str = '', **extra) -> dict:
            router._readback_recovery_last_scan = time.monotonic()
            payload = {'status': status, 'reason': reason,
                       'checked_at_epoch': time.time(), **extra}
            router.store.set_runtime_state('readback_auto_recovery', payload)
            if status != 'CONFIRMING':
                router._readback_recovery_candidate = None
            return payload

        accounts = tuple(router.accounts)
        enabled = tuple(rule for rule in accounts if rule.enabled)
        fingerprint = tuple(rule.model_dump_json() for rule in accounts)
        if not enabled:
            return report('BLOCKED', 'no enabled accounts')
        if router.safety_work_pending():
            return report('BLOCKED', 'broker ownership or management remains unresolved')
        attempt = router.store.get_entry_attempt(str(circuit['event_key']))
        origin = next((rule for rule in enabled
                       if attempt is not None and rule.account_id == attempt.account_id
                       and rule.crosstrade_account == attempt.crosstrade_account), None)
        if attempt is None or attempt.state not in {'CLOSED', 'FLAT'} or origin is None:
            return report('BLOCKED', 'originating attempt is not proven closed on an enabled account')

        try:
            # Cached state is admitted only under the existing strict freshness gate.
            # A configuration-only fallback is deliberately insufficient for recovery.
            for rule in enabled:
                state = router.state_for_entry(rule, datetime.now(timezone.utc))
                if not rule.rules_verified or not state.daily_ledger_verified:
                    return report('BLOCKED', 'account rules or daily ledger unverified')
                if rule.account_type in {'challenge', 'funded'} and (
                        not state.mll_verified or state.mll_floor is None):
                    return report('BLOCKED', 'prop failure floor unverified')
            cards = await router._entry_snapshot()
            for rule in enabled:
                card = cards.get(rule.crosstrade_account)
                if not isinstance(card, dict) or card.get('error'):
                    return report('BLOCKED', 'account snapshot missing or rejected')
                if card.get('positions') != [] or card.get('workingOrders') != []:
                    return report('BLOCKED', 'broker accounts are not verified flat and clear')
        except asyncio.CancelledError:
            router._readback_recovery_candidate = None
            raise
        except Exception as exc:
            return report('BLOCKED', f'live verification failed: {type(exc).__name__}: {exc}')

        if (router.safety_work_pending()
                or tuple(rule.model_dump_json() for rule in router.accounts) != fingerprint
                or router.store.entry_circuit() != circuit):
            return report('BLOCKED', 'circuit, account configuration, or ownership changed during verification')
        previous = getattr(router, '_readback_recovery_candidate', None)
        key = (circuit, fingerprint)
        # A lengthy gap/outage invalidates earlier proof. Process restarts also begin at 0.
        count = (previous['count'] + 1 if previous and previous['key'] == key
                 and time.monotonic() - previous['at'] <= max(60.0, interval * 3)
                 else 1)
        required = max(2, int(getattr(settings, 'READBACK_AUTO_RECOVERY_CONFIRMATIONS', 2)))
        if count < required:
            router._readback_recovery_candidate = {
                'key': key, 'count': count, 'at': time.monotonic(),
            }
            return report('CONFIRMING', confirmations=count, required_confirmations=required)
        max_age = min(float(getattr(settings, 'MAX_STATE_AGE_SECONDS', 20)),
                      float(getattr(settings, 'ENTRY_STATE_CACHE_MAX_AGE_SECONDS', 20)))
        reset = router.store.recover_readback_circuit_if_unchanged(
            circuit, [rule.account_id for rule in enabled], max_age,
            [rule.account_id for rule in enabled if rule.account_type in {'challenge', 'funded'}],
        )
        if not reset:
            return report('BLOCKED', 'atomic reset refused: state or ownership changed')
        return report('RECOVERED', confirmations=count, required_confirmations=required,
                      reset_at_epoch=reset['reset_at_epoch'])
