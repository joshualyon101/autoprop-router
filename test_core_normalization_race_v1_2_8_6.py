import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from crosstrade import RateLimitExceeded
from execution import Executor, NormalizationMutationUnconfirmed, ProtectionFailure
from live import LiveRouter
from models import AccountRule, AccountState, EntryAttempt
from store import Store


def _rule(i: int) -> AccountRule:
    return AccountRule(
        account_id=f"A{i}", crosstrade_account=f"broker-{i}", enabled=True,
        rules_verified=True, account_type="personal", starting_balance=20_000,
        max_loss=0, max_contracts=20, personal_risk_method="fixed",
        personal_risk_value=250,
    )


def _core_attempt(i: int, *, age: float = 1.0) -> EntryAttempt:
    now = time.time()
    return EntryAttempt(
        attempt_key=f"core-attempt-{i}", account_id=f"A{i}",
        crosstrade_account=f"broker-{i}", event_id="core-event", engine="CORE",
        side="SHORT", qty=3, planned_entry=30_913.5, stop=30_940.25,
        tp1=30_889.25, tp2=30_854.5, tp1_qty=2, runner_qty=1,
        custom_order_id=f"AP-{i}", state="ACCEPTED",
        parent_order_id=f"parent-{i}", accepted_at_epoch=now - age,
        submit_started_at_epoch=now - age - 0.5, created_at_epoch=now - age,
        updated_at_epoch=now - age, next_reconcile_at_epoch=0.0,
    )


def _settings(**updates):
    values = {
        "ENTRY_SIGNAL_MAX_AGE_SECONDS": 8.0,
        "ENTRY_RECONCILE_TIMEOUT_SECONDS": 90.0,
        "ENTRY_RECONCILE_BASE_DELAY_SECONDS": 0.01,
        "ENTRY_RECONCILE_MAX_DELAY_SECONDS": 0.01,
        "CORE_NORMALIZATION_MAX_CONCURRENCY": 8,
        "MAX_STATE_AGE_SECONDS": 20,
        "ENTRY_STATE_CACHE_MAX_AGE_SECONDS": 20.0,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _router(store: Store, rules: list[AccountRule], executor) -> LiveRouter:
    router = LiveRouter.__new__(LiveRouter)
    router.settings = _settings()
    router.accounts = rules
    router.store = store
    router.executor = executor
    router.execution_symbol = "MNQ1!"
    return router


@pytest.mark.asyncio
async def test_stale_core_normalizer_cannot_reopen_circuit_after_exit_closes_attempt(
        tmp_path):
    store = Store(str(tmp_path / "normalizer-exit-race.sqlite3"))
    attempt = _core_attempt(1)
    assert store.create_entry_attempt(attempt)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingExecutor:
        async def finalize_core(self, *args, **kwargs):
            entered.set()
            await release.wait()
            raise ProtectionFailure("late lifecycle read failed")

    router = _router(store, [_rule(1)], BlockingExecutor())
    task = asyncio.create_task(router.reconcile_entry_attempts())
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert store.transition_entry_attempt(
        attempt.attempt_key, ("ACCEPTED",), "CLOSED",
        last_error="ordinary EXIT observed destination position flat",
    ) is not None
    release.set()
    result = await asyncio.wait_for(task, timeout=1)

    assert result["results"] == [{
        "account_id": "A1", "status": "NORMALIZATION_SUPERSEDED",
        "attempt_state": "CLOSED",
    }]
    assert store.entry_circuit()["open"] is False


@pytest.mark.asyncio
async def test_core_normalization_fans_out_across_all_accounts(tmp_path):
    store = Store(str(tmp_path / "normalizer-fanout.sqlite3"))
    rules = [_rule(i) for i in range(8)]
    for i in range(8):
        assert store.create_entry_attempt(_core_attempt(i))
    all_entered = asyncio.Event()
    release = asyncio.Event()

    class ConcurrentExecutor:
        def __init__(self):
            self.active = 0
            self.maximum = 0

        async def finalize_core(self, *args, **kwargs):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == 8:
                all_entered.set()
            await release.wait()
            self.active -= 1
            raise ProtectionFailure("broker children not visible yet")

    executor = ConcurrentExecutor()
    router = _router(store, rules, executor)
    task = asyncio.create_task(router.reconcile_entry_attempts())
    await asyncio.wait_for(all_entered.wait(), timeout=1)
    release.set()
    result = await asyncio.wait_for(task, timeout=1)

    assert executor.maximum == 8
    assert len(result["results"]) == 8
    assert {row["status"] for row in result["results"]} == {
        "CORE_NORMALIZATION_PENDING"
    }
    assert store.entry_circuit()["open"] is False


@pytest.mark.asyncio
async def test_unconfirmed_core_change_flattens_once_and_opens_review_circuit(tmp_path):
    store = Store(str(tmp_path / "unconfirmed-change.sqlite3"))
    attempt = _core_attempt(1)
    assert store.create_entry_attempt(attempt)

    class ExecutorWithUnsafeChange:
        async def finalize_core(self, *args, **kwargs):
            raise NormalizationMutationUnconfirmed("Modify report never finalized")

    class FlatClient:
        def __init__(self):
            self.flatten_calls = 0

        async def flatten(self, account, instrument):
            self.flatten_calls += 1
            return {"success": True}

        async def positions(self, account):
            return {"success": True, "data": []}

        async def orders(self, account):
            return {"success": True, "data": []}

        async def order_status(self, account, order_id):
            return {"success": True, "data": {"status": "Canceled"}}

    router = _router(store, [_rule(1)], ExecutorWithUnsafeChange())
    router.client = FlatClient()
    result = await router.reconcile_entry_attempts()

    assert result["results"][0]["status"] == "FLATTENED"
    assert router.client.flatten_calls == 1
    assert store.get_entry_attempt(attempt.attempt_key).state == "FLAT"
    assert store.entry_circuit()["outcome"] == (
        "CORE_NORMALIZATION_MUTATION_UNCONFIRMED"
    )


def test_conditional_circuit_trip_requires_attempt_to_still_own_uncertainty(tmp_path):
    store = Store(str(tmp_path / "conditional-circuit.sqlite3"))
    attempt = _core_attempt(1)
    assert store.create_entry_attempt(attempt)
    assert store.transition_entry_attempt(
        attempt.attempt_key, ("ACCEPTED",), "CLOSED"
    ) is not None

    payload = store.trip_entry_circuit_for_attempt(
        attempt.attempt_key, ("ACCEPTED",), reason="stale verifier", outcome="STALE"
    )

    assert payload is None
    assert store.entry_circuit()["open"] is False


@pytest.mark.asyncio
async def test_ordinary_exit_checks_destination_accounts_concurrently(tmp_path):
    store = Store(str(tmp_path / "exit-fanout.sqlite3"))
    rules = [_rule(i) for i in range(8)]
    for i in range(8):
        assert store.create_entry_attempt(_core_attempt(i))
    router = _router(store, rules, executor=None)
    all_entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum = 0

    async def position_qty(rule):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 8:
            all_entered.set()
        await release.wait()
        active -= 1
        return 1

    router.position_qty = position_qty
    task = asyncio.create_task(router.ordinary_exit(SimpleNamespace(engine="CORE")))
    await asyncio.wait_for(all_entered.wait(), timeout=1)
    release.set()
    result = await asyncio.wait_for(task, timeout=1)

    assert maximum == 8
    assert len(result["results"]) == 8
    assert {row["status"] for row in result["results"]} == {
        "STILL_OPEN_DESTINATION_MANAGED"
    }


@pytest.mark.asyncio
async def test_snapshot_refresh_pending_reuses_only_fresh_cache(tmp_path):
    store = Store(str(tmp_path / "snapshot-coalesce.sqlite3"))
    rule = _rule(1)
    router = _router(store, [rule], executor=None)

    async def pending(_rule):
        raise RateLimitExceeded("HTTP 429 snapshot_refresh_pending")

    router.state_for = pending
    fresh = AccountState(
        account_id=rule.account_id, closed_cash_balance=20_000,
        state_timestamp=datetime.now(timezone.utc), daily_ledger_verified=True,
    )
    store.save_cached_account_state(fresh)
    first = await router.refresh_state_cache()
    assert first["results"][0]["status"] == "FRESH_CACHED"

    store.delete_cached_account_state(rule.account_id)
    stale = fresh.model_copy(update={
        "state_timestamp": datetime.now(timezone.utc) - timedelta(seconds=60)
    })
    store.save_cached_account_state(stale)
    second = await router.refresh_state_cache()
    assert second["results"][0]["status"] == "ERROR"


@pytest.mark.asyncio
async def test_owned_child_lifecycle_reads_are_parallelized():
    all_entered = asyncio.Event()
    release = asyncio.Event()

    class Client:
        def __init__(self):
            self.active = 0
            self.maximum = 0

        async def orders(self, account):
            return {"success": True, "data": [
                {"id": f"child-{i}", "ordStatus": "Working"} for i in range(4)
            ]}

        async def order_lifecycle(self, account, oid):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            if self.active == 4:
                all_entered.set()
            await release.wait()
            self.active -= 1
            return {"success": True, "data": {
                "order": {"id": oid, "ordStatus": "Working"},
                "version": {
                    "orderId": oid, "orderType": "Limit", "orderQty": 1,
                    "price": 100.0,
                },
                "commands": [], "reports": [],
            }}

    client = Client()
    executor = Executor(client)
    task = asyncio.create_task(
        executor._discover_owned_children("broker", set(), set())
    )
    await asyncio.wait_for(all_entered.wait(), timeout=1)
    release.set()
    rows, owned = await asyncio.wait_for(task, timeout=1)

    assert client.maximum == 4
    assert len(rows) == 4
    assert owned == {f"child-{i}" for i in range(4)}
