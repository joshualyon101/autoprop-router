import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import crosstrade as ct
from events import parse_alert
from execution import ExecutionReceipt, Executor
from live import LiveRouter
from models import AccountRule, AccountState, EntryAttempt
from state import StateUnverified
from store import Store


def _rule(i: int) -> AccountRule:
    return AccountRule(
        account_id=f"A{i}", crosstrade_account=f"broker-{i}", enabled=True,
        rules_verified=True, account_type="personal", starting_balance=20_000,
        max_loss=0, max_contracts=50, personal_risk_method="fixed",
        personal_risk_value=250,
    )


def _state(rule: AccountRule) -> AccountState:
    return AccountState(
        account_id=rule.account_id, closed_cash_balance=20_000,
        state_timestamp=datetime.now(timezone.utc), daily_ledger_verified=True,
    )


def _attempt(i: int, *, key_prefix: str = "attempt") -> EntryAttempt:
    now = time.time()
    return EntryAttempt(
        attempt_key=f"{key_prefix}-{i}",
        account_id=f"A{i}",
        crosstrade_account=f"broker-{i}",
        event_id=f"event-{key_prefix}",
        engine="SILVER",
        side="LONG",
        qty=1,
        planned_entry=100.0,
        stop=95.0,
        tp1=110.0,
        tp1_qty=1,
        runner_qty=0,
        custom_order_id=f"CID-{key_prefix}-{i}",
        created_at_epoch=now,
        updated_at_epoch=now,
    )


def _router_settings(**overrides):
    values = dict(
        MAX_STATE_AGE_SECONDS=20,
        ENTRY_SIGNAL_MAX_AGE_SECONDS=8.0,
        ENTRY_PREFLIGHT_TIMEOUT_SECONDS=5.0,
        ENTRY_FANOUT_MAX_CONCURRENCY=8,
        ENTRY_RECONCILE_TIMEOUT_SECONDS=30.0,
        ENTRY_RECONCILE_BASE_DELAY_SECONDS=0.75,
        ENTRY_RECONCILE_MAX_DELAY_SECONDS=5.0,
        USE_CROSSTRADE_POSITION_GATE=True,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _PluralPositionClient:
    def __init__(self, rows):
        self.rows = rows
        self.plural_calls = 0
        self.singular_calls = 0

    async def positions(self, account):
        self.plural_calls += 1
        return {"success": True, "data": list(self.rows)}

    async def position(self, account, instrument):
        self.singular_calls += 1
        raise AssertionError("the stale singular /position endpoint must never be used")


@pytest.mark.asyncio
@pytest.mark.parametrize("rows, expected", [
    ([], 0),
    ([{"contractId": 991, "netPos": 3}], 3),
    ([{"contractId": 991, "netPos": -2}], -2),
])
async def test_position_qty_uses_fill_reconciled_plural_positions_only(rows, expected):
    router = LiveRouter.__new__(LiveRouter)
    router.client = _PluralPositionClient(rows)
    router.execution_symbol = "MNQ1!"

    assert await router.position_qty(_rule(1)) == expected
    assert router.client.plural_calls == 1
    assert router.client.singular_calls == 0


@pytest.mark.asyncio
async def test_position_qty_refuses_to_net_multiple_contract_rows():
    router = LiveRouter.__new__(LiveRouter)
    router.client = _PluralPositionClient([
        {"contractId": 991, "netPos": 2},
        {"contractId": 992, "netPos": -1},
    ])
    router.execution_symbol = "MNQ1!"

    with pytest.raises(StateUnverified, match="multiple|ambiguous|contract"):
        await router.position_qty(_rule(1))
    assert router.client.singular_calls == 0


@pytest.mark.asyncio
async def test_position_qty_rejects_fractional_netpos():
    router = LiveRouter.__new__(LiveRouter)
    router.client = _PluralPositionClient([{"contractId": 991, "netPos": 1.5}])
    router.execution_symbol = "MNQ1!"

    with pytest.raises(StateUnverified, match="netPos|integral"):
        await router.position_qty(_rule(1))
    assert router.client.singular_calls == 0


@pytest.mark.asyncio
async def test_position_qty_requires_expected_fill_contract_identity():
    router = LiveRouter.__new__(LiveRouter)
    router.client = _PluralPositionClient([{"contractId": 992, "netPos": 2}])
    router.execution_symbol = "MNQ1!"

    with pytest.raises(StateUnverified, match="contract|identity"):
        await router.position_qty(_rule(1), expected_contract_id="991")
    assert router.client.singular_calls == 0


@pytest.mark.asyncio
async def test_native_oso_role_labels_return_receipt_without_any_readback():
    class Client:
        def __init__(self):
            self.place_calls = 0

        async def place(self, account, payload):
            self.place_calls += 1
            return {"response": {
                "orderId": "parent-1", "oso1Id": "target-1", "oso2Id": "stop-1",
                "osoChildIds": ["target-1", "stop-1"],
            }}

        async def orders(self, account):
            raise AssertionError("single-target submit must not GET working orders")

        async def order_lifecycle(self, account, order_id):
            raise AssertionError("native OSO role labels must not require lifecycle GET")

        async def all_orders(self):
            raise AssertionError("accepted native OSO must not scan global order history")

        async def flatten(self, account, instrument):
            raise AssertionError("valid native OSO response must not flatten")

    from models import Allocation
    alloc = Allocation(
        account_id="A", event_id="e", engine="SILVER", side="SHORT", qty=2,
        entry=100, stop=105, tp1=90, tp1_qty=2, runner_qty=0,
        base_risk=100, effective_risk_budget=100, risk_per_contract=10,
    )
    client = Client()

    receipt = await Executor(client).place_single("broker", alloc, "CID-1")

    assert client.place_calls == 1
    assert receipt.parent_order_id == "parent-1"
    assert receipt.target_order_ids == ["target-1"]
    assert receipt.stop_order_ids == ["stop-1"]
    assert set(receipt.child_order_ids) == {"target-1", "stop-1"}


class _HttpRecorder:
    def __init__(self):
        self.calls = []

    async def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("admission must close before the irreversible HTTP request")


@pytest.mark.asyncio
async def test_place_rechecks_abort_guard_after_rate_wait(monkeypatch):
    admitted = True

    async def rate_wait(*args, **kwargs):
        nonlocal admitted
        admitted = False

    recorder = _HttpRecorder()
    client = ct.CrossTradeClient("https://example.invalid", "x")
    monkeypatch.setattr(ct, "_acquire_rate_slot", rate_wait)
    monkeypatch.setattr(client, "_http_client", lambda: recorder)

    with pytest.raises(ct.EntryAdmissionClosed, match="control fence"):
        await client.place(
            "A", {"instrument": "MNQ1!", "qty": 1},
            admission_guard=lambda: admitted,
        )
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_place_rechecks_deadline_after_rate_wait(monkeypatch):
    now = 100.0

    async def rate_wait(*args, **kwargs):
        nonlocal now
        now = 102.0

    recorder = _HttpRecorder()
    client = ct.CrossTradeClient("https://example.invalid", "x")
    monkeypatch.setattr(ct, "_acquire_rate_slot", rate_wait)
    monkeypatch.setattr(ct.time, "time", lambda: now)
    monkeypatch.setattr(client, "_http_client", lambda: recorder)

    with pytest.raises(ct.EntryAdmissionClosed, match="deadline expired"):
        await client.place("A", {"instrument": "MNQ1!", "qty": 1}, deadline_epoch=101.0)
    assert recorder.calls == []


def test_prepare_entry_attempts_uses_one_commit_and_rolls_back_only_conflicting_pair(
        monkeypatch, tmp_path):
    store = Store(str(tmp_path / "batch-prepare.sqlite3"))
    # Hold A1 open so the new A1 row violates the one-unresolved-attempt constraint.
    assert store.create_entry_attempt(_attempt(1, key_prefix="existing"))

    real_connect = store._connect
    commits = 0

    class CountingConnection:
        def __init__(self, connection):
            self.connection = connection

        def commit(self):
            nonlocal commits
            commits += 1
            return self.connection.commit()

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        store, "_connect",
        lambda timeout=10.0: CountingConnection(real_connect(timeout)),
    )
    conflicting = _attempt(1, key_prefix="new")
    accepted = _attempt(2, key_prefix="new")

    result = store.prepare_entry_attempts([
        ("dedupe-conflict", conflicting),
        ("dedupe-accepted", accepted),
    ])

    assert result == {
        conflicting.attempt_key: False,
        accepted.attempt_key: True,
    }
    assert commits == 1
    assert store.get_entry_attempt(conflicting.attempt_key) is None
    assert store.get_entry_attempt(accepted.attempt_key) is not None
    # An explicitly detected conflict must not leave an orphan dedupe claim.
    assert store.claim_event("dedupe-conflict") is True
    assert store.claim_event("dedupe-accepted") is False


def test_prepare_entry_attempts_unexpected_integrity_error_rolls_back_whole_batch(
        tmp_path):
    path = tmp_path / "batch-trigger-failure.sqlite3"
    store = Store(str(path))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_second_batch_attempt "
            "BEFORE INSERT ON entry_attempts "
            "WHEN NEW.account_id='A2' BEGIN "
            "SELECT RAISE(ABORT, 'injected entry integrity failure'); END"
        )

    first = _attempt(1, key_prefix="trigger")
    second = _attempt(2, key_prefix="trigger")
    with pytest.raises(sqlite3.IntegrityError, match="injected entry integrity failure"):
        store.prepare_entry_attempts([
            ("trigger-dedupe-1", first),
            ("trigger-dedupe-2", second),
        ])

    # The first pair had already been inserted in the transaction. The unexpected
    # trigger failure must roll it back too, rather than masquerading as a duplicate.
    with sqlite3.connect(path) as connection:
        attempts = connection.execute(
            "SELECT attempt_key FROM entry_attempts WHERE attempt_key IN (?,?)",
            (first.attempt_key, second.attempt_key),
        ).fetchall()
        dedupe = connection.execute(
            "SELECT event_id FROM dedupe WHERE event_id IN (?,?)",
            ("trigger-dedupe-1", "trigger-dedupe-2"),
        ).fetchall()
    assert attempts == []
    assert dedupe == []


def test_begin_entry_submissions_is_all_or_none_and_durable(tmp_path):
    path = tmp_path / "batch-submit.sqlite3"
    store = Store(str(path))
    first = _attempt(1)
    second = _attempt(2)
    assert store.prepare_entry_attempts([
        ("dedupe-1", first), ("dedupe-2", second),
    ]) == {first.attempt_key: True, second.attempt_key: True}
    assert store.transition_entry_attempt(
        first.attempt_key, "PREPARED", "ABORTED",
        last_error="safety control won",
    ) is not None

    # A conflicting member prevents a partial claim of the rest of the wave.
    assert store.begin_entry_submissions(
        [first.attempt_key, second.attempt_key], submit_started_epoch=123.5,
    ) is None
    assert store.get_entry_attempt(first.attempt_key).state == "ABORTED"
    assert store.get_entry_attempt(second.attempt_key).state == "PREPARED"

    claimed = store.begin_entry_submissions(
        [second.attempt_key], submit_started_epoch=124.5,
    )
    assert claimed is not None
    assert claimed[second.attempt_key].state == "SUBMITTING"
    assert claimed[second.attempt_key].submit_started_at_epoch == 124.5

    # Simulate a process restart: SUBMITTING remains durable and therefore must be
    # reconciled rather than permitting another PLACE.
    restarted = Store(str(path))
    persisted = restarted.get_entry_attempt(second.attempt_key)
    assert persisted is not None
    assert persisted.state == "SUBMITTING"
    assert persisted.submit_started_at_epoch == 124.5


@pytest.mark.asyncio
async def test_submit_wave_is_not_blocked_by_fill_or_position_verification(
        monkeypatch, tmp_path):
    """Contract test for the v1.2.8.5 two-phase ENTRY path.

    Every PLACE must be accepted and durably recorded before asynchronous fill/position
    verification can hold up the request. Events make this deterministic; there are no
    timing-performance assertions.
    """
    accounts = [_rule(i) for i in range(4)]
    store = Store(str(tmp_path / "router.sqlite3"))
    for account in accounts:
        store.save_cached_account_state(_state(account))
    router = LiveRouter.__new__(LiveRouter)
    router.settings = _router_settings(ENTRY_FANOUT_MAX_CONCURRENCY=4)
    router.accounts = accounts
    router.store = store
    router.execution_symbol = "MNQ1!"

    all_submits_started = asyncio.Event()
    release_submits = asyncio.Event()
    release_verification = asyncio.Event()
    submitted = []
    verification_calls = 0

    class ExecutorStub:
        async def place_single(self, account, alloc, custom_order_id, **kwargs):
            submitted.append(account)
            if len(submitted) == len(accounts):
                all_submits_started.set()
            await release_submits.wait()
            suffix = account.rsplit("-", 1)[-1]
            return ExecutionReceipt(
                {"response": {"orderId": f"p-{suffix}"}},
                [f"t-{suffix}", f"s-{suffix}"], f"p-{suffix}",
                [f"t-{suffix}"], [f"s-{suffix}"],
            )

        async def place_core(self, *args, **kwargs):
            raise AssertionError("this fixture is a SILVER entry")

    async def entry_gate(rule):
        return None

    async def state_for(rule):
        return _state(rule)

    async def blocked_fill_verification(*args, **kwargs):
        nonlocal verification_calls
        verification_calls += 1
        await release_verification.wait()
        return 100.0

    router.executor = ExecutorStub()
    router.entry_gate = entry_gate
    router.state_for = state_for
    router.entry_fill_price = blocked_fill_verification

    prepare_batch_sizes = []
    submit_batch_sizes = []
    real_prepare_many = store.prepare_entry_attempts
    real_begin_many = store.begin_entry_submissions
    real_transition = store.transition_entry_attempt

    def prepare_many(items):
        items = list(items)
        prepare_batch_sizes.append(len(items))
        return real_prepare_many(items)

    def begin_many(keys, *, submit_started_epoch):
        keys = list(keys)
        submit_batch_sizes.append(len(keys))
        return real_begin_many(keys, submit_started_epoch=submit_started_epoch)

    def transition(attempt_key, expected_states, new_state, **updates):
        if new_state == "SUBMITTING":
            raise AssertionError("production route must use the batch submission claim")
        return real_transition(attempt_key, expected_states, new_state, **updates)

    monkeypatch.setattr(store, "prepare_entry_attempt", lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(
            AssertionError("production route must use batch preparation")
        )
    ))
    monkeypatch.setattr(store, "prepare_entry_attempts", prepare_many)
    monkeypatch.setattr(store, "begin_entry_submissions", begin_many)
    monkeypatch.setattr(store, "transition_entry_attempt", transition)

    event = parse_alert(
        "AUTOPROP_ICT_FUSION|SILVER|ENTRY|SHORT|ENTRY=100|SL=105|TP=90"
    )
    receipt_epoch = time.time()
    route_task = asyncio.create_task(router.route_entry(
        event, event_key="entry-1", receipt_epoch=receipt_epoch,
    ))

    await asyncio.wait_for(all_submits_started.wait(), timeout=1.0)
    assert set(submitted) == {a.crosstrade_account for a in accounts}
    release_submits.set()
    result = await asyncio.wait_for(route_task, timeout=1.0)

    assert {r["account_id"] for r in result["results"]} == {a.account_id for a in accounts}
    assert all(r["status"] in {"ACCEPTED", "ROUTED"} for r in result["results"])
    assert {a.account_id for a in store.all_entry_attempts(["ACCEPTED", "ACTIVE"])} == {
        a.account_id for a in accounts
    }
    assert prepare_batch_sizes == [len(accounts)]
    assert submit_batch_sizes == [len(accounts)]
    # Submission and durable acceptance are the entire synchronous entry wave.
    # Fill/position proof belongs to the separate reconciliation loop.
    assert verification_calls == 0
    release_verification.set()
