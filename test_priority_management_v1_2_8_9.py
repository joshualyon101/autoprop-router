from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from events import parse_alert
from live import LiveRouter
from models import AccountRule, ActiveTrade, EntryAttempt
from store import Store


def _rule(account_id: str, broker: str) -> AccountRule:
    return AccountRule(
        account_id=account_id, crosstrade_account=broker, enabled=True,
        rules_verified=True, account_type='personal', profile='standard',
        starting_balance=100_000, max_loss=0, max_contracts=20,
        personal_risk_method='fixed', personal_risk_value=250,
    )


def _trade(account_id: str = 'A', *, stage: int = 0, stop: float = 110.0) -> ActiveTrade:
    return ActiveTrade(
        account_id=account_id, event_id='entry-1', engine='SILVER', side='SHORT',
        entry=100.0, initial_stop=110.0, current_stop=stop, tp1=70.0,
        total_qty=3, tp1_qty=3, runner_qty=0, current_position_qty=3,
        previous_position_qty=3, stop_stage=stage, stop_order_ids=['stop-1'],
        target_order_ids=['target-1'], parent_order_id='parent-1',
    )


def _attempt(account_id: str = 'B') -> EntryAttempt:
    now = time.time()
    return EntryAttempt(
        attempt_key=f'entry-1:{account_id}', account_id=account_id,
        crosstrade_account=f'broker-{account_id}', event_id='entry-1',
        engine='SILVER', side='SHORT', qty=3, planned_entry=100.0,
        stop=110.0, tp1=70.0, tp1_qty=3, runner_qty=0,
        custom_order_id=f'AP-{account_id}', state='ACCEPTED',
        parent_order_id=f'parent-{account_id}', stop_order_ids=[f'stop-{account_id}'],
        target_order_ids=[f'target-{account_id}'], child_order_ids=[
            f'stop-{account_id}', f'target-{account_id}'
        ], accepted_at_epoch=now, created_at_epoch=now, updated_at_epoch=now,
    )


class Executor:
    def __init__(self):
        self.calls = []

    async def change_stop_orders(self, account, ids, new_stop, qty):
        self.calls.append((account, list(ids), new_stop, qty))
        return list(ids)


def _settings():
    return SimpleNamespace(
        SILVER_MANAGEMENT_RETRY_BASE_SECONDS=0.01,
        SILVER_MANAGEMENT_RETRY_MAX_SECONDS=0.05,
    )


def test_store_coalesces_to_highest_stage_and_cas_clear(tmp_path):
    store = Store(str(tmp_path / 'intent.sqlite3'))
    first = store.record_silver_stop_intent(1, 'stage-1')
    assert first['stage'] == 1
    second = store.record_silver_stop_intent(2, 'stage-2')
    assert second['stage'] == 2
    assert store.clear_silver_stop_intent(1) is False
    assert store.silver_stop_intent()['stage'] == 2
    assert store.clear_silver_stop_intent(2) is True
    assert store.silver_stop_intent() == {}
    # A completed stage 2 belongs to the old trade generation. A future trade may start
    # normally at stage 1 and must not inherit the old stage.
    next_trade = store.record_silver_stop_intent(1, 'next-trade-stage-1')
    assert next_trade['stage'] == 1
    assert next_trade['event_key'] == 'next-trade-stage-1'


@pytest.mark.asyncio
async def test_webhook_records_intent_without_waiting_for_entry_promotion(tmp_path):
    store = Store(str(tmp_path / 'record.sqlite3'))
    router = LiveRouter.__new__(LiveRouter)
    router.store = store
    router.silver_management_wakeup = asyncio.Event()
    event = parse_alert(
        'AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|SHORT|'
        'CONTRACT=SILVER_SB35_STAGE_V1|STAGE=1|TRIGGER_R=2.00|LOCK_R=0.25'
    )
    result = await router.silver_stop(event, event_key='event-1')
    assert result == {
        'kind': 'SILVER_STOP_MOVE', 'status': 'INTENT_RECORDED',
        'stage': 1, 'coalesced': False,
    }
    assert router.silver_management_wakeup.is_set()
    assert store.silver_stop_intent()['event_key'] == 'event-1'


@pytest.mark.asyncio
async def test_active_account_advances_while_sibling_attempt_is_unresolved(tmp_path):
    store = Store(str(tmp_path / 'partial.sqlite3'))
    store.save_trade(_trade('A'))
    assert store.create_entry_attempt(_attempt('B'))
    store.record_silver_stop_intent(1, 'stage-1')

    router = LiveRouter.__new__(LiveRouter)
    router.store = store
    router.accounts = [_rule('A', 'broker-A'), _rule('B', 'broker-B')]
    router.executor = Executor()
    router.settings = _settings()
    router._entry_reconcile_lock = asyncio.Lock()

    async def still_pending():
        return {'kind': 'ENTRY_RECONCILE', 'results': [
            {'account_id': 'B', 'status': 'READBACK_PENDING'}
        ]}

    async def position_qty(rule):
        assert rule.account_id == 'A'
        return -3

    router.reconcile_entry_attempts = still_pending
    router.position_qty = position_qty
    result = await router.service_silver_stop_intent()
    assert result['status'] == 'PENDING'
    assert router.executor.calls == [('broker-A', ['stop-1'], 97.5, 3)]
    assert store.get_trade('A').stop_stage == 1
    assert store.silver_stop_intent()['pending'] is True
    assert '1 Silver entry attempt(s) unresolved' in result['reason']


@pytest.mark.asyncio
async def test_intent_clears_only_after_all_active_trades_reach_stage(tmp_path):
    store = Store(str(tmp_path / 'complete.sqlite3'))
    store.save_trade(_trade('A'))
    store.record_silver_stop_intent(2, 'stage-2')

    router = LiveRouter.__new__(LiveRouter)
    router.store = store
    router.accounts = [_rule('A', 'broker-A')]
    router.executor = Executor()
    router.settings = _settings()

    async def position_qty(_rule):
        return -3

    router.position_qty = position_qty
    result = await router.service_silver_stop_intent()
    assert result['status'] == 'COMPLETED'
    assert router.executor.calls == [('broker-A', ['stop-1'], 95.0, 3)]
    assert store.get_trade('A').stop_stage == 2
    assert store.silver_stop_intent() == {}


class IdleStore:
    def unresolved_entry_attempt_count(self):
        return 0

    def all_trades(self):
        return []

    def all_asw_pending(self):
        return []

    def silver_stop_intent(self):
        return {}


@pytest.mark.asyncio
async def test_background_state_refresh_never_queues_an_account_fanout():
    router = LiveRouter.__new__(LiveRouter)
    router.store = IdleStore()
    router.entry_wave_active = asyncio.Event()
    router._state_refresh_batch_lock = asyncio.Lock()
    router.accounts = [_rule('A', 'broker-A'), _rule('B', 'broker-B')]
    active = 0
    maximum = 0

    async def state_for(rule):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return SimpleNamespace(
            state_timestamp=SimpleNamespace(isoformat=lambda: rule.account_id)
        )

    router.state_for = state_for
    result = await router.refresh_state_cache()
    assert [row['status'] for row in result['results']] == ['FRESH', 'FRESH']
    assert maximum == 1


@pytest.mark.asyncio
async def test_background_state_refresh_pauses_for_unresolved_exposure():
    router = LiveRouter.__new__(LiveRouter)
    router.entry_wave_active = asyncio.Event()
    router._state_refresh_batch_lock = asyncio.Lock()
    router.accounts = [_rule('A', 'broker-A')]
    router.store = IdleStore()
    router.store.unresolved_entry_attempt_count = lambda: 1

    async def forbidden(_rule):
        raise AssertionError('background broker read must not start')

    router.state_for = forbidden
    result = await router.refresh_state_cache()
    assert result == {'paused_for_safety': True, 'results': []}
