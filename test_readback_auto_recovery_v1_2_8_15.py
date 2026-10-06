import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

import crosstrade as ct
import entry_recovery as recovery
from live import LiveRouter
from models import AccountRule, AccountState, EntryAttempt
from store import Store


def attempt(state='CLOSED', key='origin', account_id='A1'):
    return EntryAttempt(
        attempt_key=key, account_id=account_id, crosstrade_account='broker-1',
        event_id='signal', engine='DWC', side='SHORT', qty=1,
        planned_entry=100, stop=105, tp1=90, tp1_qty=1, runner_qty=0,
        custom_order_id='custom', state=state, parent_order_id='parent',
        stop_order_ids=['stop'], target_order_ids=['target'],
        child_order_ids=['stop', 'target'], created_at_epoch=time.time() - 100,
        accepted_at_epoch=time.time() - 100,
        updated_at_epoch=time.time(),
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(recovery, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], time=time.time,
    ))
    store = Store(str(tmp_path / 'router.sqlite3'))
    store.create_entry_attempt(attempt())
    store.trip_entry_circuit(
        reason='A1: accepted entry readback timed out: GET /accounts/broker-1/positions network error after 4 attempt(s): ReadTimeout',
        event_key='origin', outcome='READBACK_UNCONFIRMED',
    )
    store.save_cached_account_state(AccountState(
        account_id='A1', closed_cash_balance=20_000, daily_ledger_verified=True,
        state_timestamp=datetime.now(timezone.utc),
    ))
    router = LiveRouter.__new__(LiveRouter)
    router.store = store
    router.settings = SimpleNamespace(
        AUTOPROP_EXECUTION_MODE='live', READBACK_AUTO_RECOVERY_ENABLED=True,
        READBACK_AUTO_RECOVERY_INTERVAL_SECONDS=10,
        READBACK_AUTO_RECOVERY_CONFIRMATIONS=2,
        MAX_STATE_AGE_SECONDS=20, ENTRY_STATE_CACHE_MAX_AGE_SECONDS=20,
        ENTRY_PREFLIGHT_TIMEOUT_SECONDS=0.1,
    )
    router.accounts = [AccountRule(
        account_id='A1', crosstrade_account='broker-1', enabled=True,
        rules_verified=True, account_type='personal', starting_balance=20_000,
        max_loss=0, max_contracts=20, personal_risk_method='fixed', personal_risk_value=250,
    )]
    router.entry_wave_active = asyncio.Event()

    class Client:
        calls = 0
        payload = {'accounts': [{'name': 'broker-1', 'positions': [], 'workingOrders': []}]}
        error = None
        async def accounts_snapshot(self):
            self.calls += 1
            if self.error:
                raise self.error
            return self.payload
        async def flatten(self, *args):
            pytest.fail('recovery must never mutate broker exposure')
        async def place(self, *args):
            pytest.fail('recovery must never submit an entry')
    router.client = Client()
    return router, store, clock


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['CLOSED', 'FLAT'])
async def test_timeout_recovers_only_after_two_spaced_flat_clear_reads(setup, terminal):
    router, store, clock = setup
    if terminal == 'FLAT':
        store.transition_entry_attempt('origin', ('CLOSED',), 'FLAT')
    first = await router.recover_readback_circuit()
    assert first['status'] == 'CONFIRMING'
    assert store.entry_circuit()['open'] is True
    assert (await router.recover_readback_circuit())['status'] == 'BACKOFF'
    assert router.client.calls == 1
    clock[0] += 11
    result = await router.recover_readback_circuit()
    assert result['status'] == 'RECOVERED'
    assert router.client.calls == 2
    assert store.entry_circuit()['open'] is False
    assert store.entry_circuit()['previous_circuit']['outcome'] == 'READBACK_UNCONFIRMED'
    assert store.get_entry_attempt('origin').state == terminal


@pytest.mark.parametrize('outcome', [
    'AMBIGUOUS_PLACE', 'POSITION_MISMATCH', 'PROTECTION_MISMATCH',
    'PROTECTION_READBACK_UNCONFIRMED', 'UNPROTECTED_ACCEPTANCE',
    'STATE_FALLBACK_EXHAUSTED', 'STARTUP_RECOVERY', 'UNKNOWN_ACCOUNT',
])
@pytest.mark.asyncio
async def test_other_circuits_never_auto_reset(setup, outcome):
    router, store, _ = setup
    store.trip_entry_circuit(reason='network error ReadTimeout', event_key='origin', outcome=outcome)
    assert (await router.recover_readback_circuit())['status'] == 'INELIGIBLE'
    assert router.client.calls == 0
    assert store.entry_circuit()['open'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['HTTP 401: unauthorized', 'position data malformed', 'fill quantity mismatch'])
async def test_hard_or_unknown_readback_error_never_auto_resets(setup, reason):
    router, store, _ = setup
    store.trip_entry_circuit(reason='A1: accepted entry readback timed out: ' + reason,
                             event_key='origin', outcome='READBACK_UNCONFIRMED')
    assert (await router.recover_readback_circuit())['status'] == 'INELIGIBLE'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['shadow', 'invalid'])
async def test_non_live_mode_never_reads_or_resets(setup, mode):
    router, store, _ = setup
    router.settings.AUTOPROP_EXECUTION_MODE = mode
    assert (await router.recover_readback_circuit())['status'] == 'DISABLED'
    assert router.client.calls == 0
    assert store.entry_circuit()['open']


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['ACCEPTED', 'SUBMITTING', 'ACTIVE', 'FAILED', 'ABORTED'])
async def test_unresolved_or_unproven_origin_blocks_recovery(setup, state):
    router, store, _ = setup
    store.transition_entry_attempt('origin', ('CLOSED',), state)
    assert (await router.recover_readback_circuit())['status'] == 'BLOCKED'
    assert router.client.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['positions', 'workingOrders'])
async def test_live_position_or_working_order_blocks_reset(setup, field):
    router, store, _ = setup
    router.client.payload['accounts'][0][field] = [{'id': 'external'}]
    assert (await router.recover_readback_circuit())['status'] == 'BLOCKED'
    assert store.entry_circuit()['open']


@pytest.mark.asyncio
async def test_stale_account_data_does_not_use_fallback_for_recovery(setup):
    router, store, _ = setup
    state = store.get_cached_account_state('A1')
    store.delete_cached_account_state('A1')
    store.save_cached_account_state(state.model_copy(update={
        'state_timestamp': datetime.now(timezone.utc) - timedelta(seconds=30),
    }))
    assert (await router.recover_readback_circuit())['status'] == 'BLOCKED'
    assert router.client.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'accounts': []}, {'accounts': [{'name': 'broker-1', 'positions': [], 'workingOrders': None}]},
    {'accounts': [{'name': 'broker-1', 'error': 'unavailable', 'positions': [], 'workingOrders': []}]},
])
async def test_incomplete_broker_proof_keeps_circuit_open(setup, payload):
    router, store, _ = setup
    router.client.payload = payload
    assert (await router.recover_readback_circuit())['status'] == 'BLOCKED'
    assert store.entry_circuit()['open']


@pytest.mark.asyncio
async def test_read_outage_discards_previous_confirmation(setup):
    router, store, clock = setup
    assert (await router.recover_readback_circuit())['status'] == 'CONFIRMING'
    clock[0] += 11
    router.client.error = httpx.ReadTimeout('test')
    assert (await router.recover_readback_circuit())['status'] == 'BLOCKED'
    clock[0] += 11
    router.client.error = None
    assert (await router.recover_readback_circuit())['status'] == 'CONFIRMING'
    assert store.entry_circuit()['open']


@pytest.mark.asyncio
async def test_changed_circuit_during_read_is_not_cleared(setup):
    router, store, _ = setup
    async def snapshot():
        store.trip_entry_circuit(reason='new protection failure', outcome='PROTECTION_MISMATCH')
        return router.client.payload
    router.client.accounts_snapshot = snapshot
    assert (await router.recover_readback_circuit())['status'] == 'BLOCKED'
    assert store.entry_circuit()['outcome'] == 'PROTECTION_MISMATCH'


@pytest.mark.asyncio
async def test_atomic_reset_refuses_ownership_created_after_checks(setup, monkeypatch):
    router, store, clock = setup
    assert (await router.recover_readback_circuit())['status'] == 'CONFIRMING'
    clock[0] += 11
    real_reset = store.recover_readback_circuit_if_unchanged
    def racing_reset(*args):
        store.create_entry_attempt(attempt('ACCEPTED', 'racing', 'A2'))
        return real_reset(*args)
    monkeypatch.setattr(store, 'recover_readback_circuit_if_unchanged', racing_reset)
    assert (await router.recover_readback_circuit())['status'] == 'BLOCKED'
    assert store.entry_circuit()['open']


def test_atomic_reset_refuses_pending_safety_control(setup):
    _, store, _ = setup
    circuit = store.entry_circuit()
    store.enqueue_webhook('control', 'raw', 'HARD_FLAT')
    assert store.recover_readback_circuit_if_unchanged(circuit, ['A1'], 20) is None
    assert store.entry_circuit()['open']


@pytest.mark.asyncio
async def test_stale_readback_exception_cannot_reopen_circuit_after_exit(setup):
    router, store, _ = setup
    store.reset_entry_circuit()
    store.transition_entry_attempt('origin', ('CLOSED',), 'ACCEPTED')
    router.settings.ENTRY_RECONCILE_TIMEOUT_SECONDS = 1
    async def fill_proof(_order):
        return {'qty': 1, 'price': 100, 'instrument': '', 'contract_id': ''}
    async def position_proof(*args, **kwargs):
        # EXIT commits terminal ownership while the verifier awaits its position GET.
        store.transition_entry_attempt('origin', ('ACCEPTED',), 'CLOSED')
        raise ct.CrossTradeError('GET positions network error after 4 attempt(s): ReadTimeout')
    router.entry_fill_proof = fill_proof
    router.position_proof = position_proof
    await router.reconcile_entry_attempts()
    assert store.get_entry_attempt('origin').state == 'CLOSED'
    assert store.entry_circuit()['open'] is False


def test_atomic_reset_rechecks_prop_floor(setup):
    _, store, _ = setup
    assert store.recover_readback_circuit_if_unchanged(
        store.entry_circuit(), ['A1'], 20, ['A1'],
    ) is None
    assert store.entry_circuit()['open']


@pytest.mark.asyncio
async def test_restart_requires_new_flat_clear_observations(setup):
    router, store, clock = setup
    assert (await router.recover_readback_circuit())['status'] == 'CONFIRMING'
    clock[0] += 11
    del router._readback_recovery_candidate
    assert (await router.recover_readback_circuit())['status'] == 'CONFIRMING'
    assert store.entry_circuit()['open']


@pytest.mark.asyncio
async def test_transport_diagnostics_separate_retry_and_hide_credentials(monkeypatch, caplog):
    async def no_wait(*args, **kwargs):
        return None
    monkeypatch.setattr(ct, '_acquire_rate_slot', no_wait)
    calls = [0]
    class Session:
        async def request(self, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise httpx.ReadTimeout('sensitive error text')
            return httpx.Response(200, json={'data': []})
    client = ct.CrossTradeClient('https://example.invalid', 'secret-token',
                                get_retry_delay_seconds=0, get_retry_max_retries=1)
    monkeypatch.setattr(client, '_http_client', lambda: Session())
    await client.positions('private-account')
    diagnostic = client.transport_diagnostics['last_transport_failure']
    assert diagnostic['error_type'] == 'ReadTimeout'
    assert diagnostic['endpoint'] == '/v1/api/tv/accounts/{account}/positions'
    assert diagnostic['rate_wait_seconds'] >= 0
    assert diagnostic['semaphore_wait_seconds'] >= 0
    assert diagnostic['http_elapsed_seconds'] >= 0
    assert client.transport_diagnostics['last_retry_recovery']['attempts'] == 2
    assert 'private-account' not in caplog.text
    assert 'secret-token' not in caplog.text
    assert 'sensitive error text' not in caplog.text


@pytest.mark.asyncio
async def test_mutation_timeout_is_logged_but_never_resent(monkeypatch):
    async def no_wait(*args, **kwargs):
        return None
    monkeypatch.setattr(ct, '_acquire_rate_slot', no_wait)
    calls = [0]
    class Session:
        async def request(self, *args, **kwargs):
            calls[0] += 1
            raise httpx.ReadTimeout('test')
    client = ct.CrossTradeClient('https://example.invalid', 'token', get_retry_max_retries=8)
    monkeypatch.setattr(client, '_http_client', lambda: Session())
    with pytest.raises(ct.AmbiguousMutation):
        await client._request('POST', '/v1/api/tv/accounts/private/orders')
    assert calls[0] == 1
    assert client.transport_diagnostics['last_transport_failure']['exhausted'] is True
