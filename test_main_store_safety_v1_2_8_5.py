from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import crosstrade as ct
import main
import store as store_module
from execution import Executor
from live import LiveRouter
from models import AccountRule, EntryAttempt
from store import Store


def _rule(account_id: str = "A1", broker: str = "broker-1") -> AccountRule:
    return AccountRule(
        account_id=account_id,
        crosstrade_account=broker,
        enabled=True,
        rules_verified=True,
        account_type="personal",
        starting_balance=20_000,
        max_loss=0,
        max_contracts=20,
        personal_risk_method="fixed",
        personal_risk_value=250,
    )


def _attempt(*, state: str = "ACCEPTED", engine: str = "ORG",
             accepted_at_epoch: float | None = None) -> EntryAttempt:
    now = time.time()
    return EntryAttempt(
        attempt_key=f"attempt-{engine.lower()}",
        account_id="A1",
        crosstrade_account="broker-1",
        event_id=f"event-{engine.lower()}",
        engine=engine,
        side="LONG",
        qty=2,
        planned_entry=100.0,
        stop=95.0,
        tp1=110.0,
        tp1_qty=2,
        runner_qty=0,
        custom_order_id=f"CID-{engine}",
        entry_receipt_epoch=now - 1.0,
        inbox_event_key=f"inbox-{engine.lower()}",
        state=state,
        parent_order_id="parent-1",
        target_order_ids=["target-1"],
        stop_order_ids=["stop-1"],
        child_order_ids=["target-1", "stop-1"],
        accepted_at_epoch=(accepted_at_epoch if accepted_at_epoch is not None
                           else (now if state == "ACCEPTED" else None)),
        submit_started_at_epoch=now - 0.5,
        created_at_epoch=now - 1.0,
        updated_at_epoch=now,
        next_reconcile_at_epoch=0.0,
    )


class _BodyRequest:
    def __init__(self, raw: str):
        self.raw = raw

    async def body(self) -> bytes:
        return self.raw.encode("utf-8")


class _RaceWakeup:
    """Deterministic Event double that exposes clear-after-claim lost wakeups."""

    def __init__(self):
        self._set = False

    def clear(self):
        self._set = False

    def set(self):
        self._set = True

    async def wait(self):
        if not self._set:
            raise AssertionError("webhook wakeup was cleared after the empty claim")
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "raw", "kind"),
    [
        (False, "AUTOPROP_ICT_FUSION|ACCOUNT|ACCOUNT", "ACCOUNT"),
        (True, "AUTOPROP_ICT_FUSION|ORG|EXIT|LONG", "EXIT"),
    ],
    ids=["regular-lane", "control-lane"],
)
async def test_worker_does_not_lose_wakeup_between_empty_claim_and_wait(
        monkeypatch, control, raw, kind):
    wakeup = _RaceWakeup()
    row = {
        "event_key": f"race-{kind.lower()}",
        "raw": raw,
        "kind": kind,
        "receipt_epoch": 1.0,
        "attempts": 1,
    }

    class RaceStore:
        def __init__(self):
            self.claims = 0

        def _claim(self):
            self.claims += 1
            if self.claims == 1:
                # Model enqueue_webhook's set() after the worker observed an empty
                # queue but before it starts waiting.
                wakeup.set()
                return None
            return row

        def claim_next_regular_webhook(self):
            assert control is False
            return self._claim()

        def claim_next_control_webhook(self):
            assert control is True
            return self._claim()

        def prune_dedupe(self, _days):
            return None

    reached_processing = asyncio.Event()

    async def stop_after_claim(event, *, event_key="", receipt_epoch=None):
        assert event.kind == kind
        assert event_key == row["event_key"]
        assert receipt_epoch == 1.0
        reached_processing.set()
        raise asyncio.CancelledError

    race_store = RaceStore()
    monkeypatch.setattr(main, "store", race_store)
    monkeypatch.setattr(main, "_process_event", stop_after_claim)
    monkeypatch.setattr(main, "_worker_wakeup", wakeup if not control else None)
    monkeypatch.setattr(main, "_control_worker_wakeup", wakeup if control else None)

    worker = asyncio.create_task(main._webhook_worker(control=control))
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert reached_processing.is_set()
    assert race_store.claims == 2


def _reconcile_settings(timeout: float = 30.0):
    return SimpleNamespace(
        ENTRY_RECONCILE_TIMEOUT_SECONDS=timeout,
        ENTRY_RECONCILE_BASE_DELAY_SECONDS=0.25,
        ENTRY_RECONCILE_MAX_DELAY_SECONDS=2.0,
    )


def test_control_enqueue_and_fence_commit_together_and_duplicate_rolls_back(tmp_path):
    store = Store(str(tmp_path / "control.sqlite3"))

    assert store.enqueue_control_webhook(
        "exit-1", "AUTOPROP_ICT_FUSION|ORG|EXIT|LONG", "EXIT",
        receipt_epoch=10.0, priority=90, engine="ORG", side="LONG",
        fence_scope="ORG",
    ) is True
    assert store.latest_control_fence("ORG") == 10.0
    assert store.latest_control_fence_info("ORG") == {
        "scope": "ORG",
        "fence_epoch": 10.0,
        "event_key": "exit-1",
        "kind": "EXIT",
    }
    assert store.entry_aborted_after("ORG", 9.0) is True
    assert store.entry_aborted_after("CORE", 9.0) is False

    # The duplicate inbox insert fails before the new scope can be committed. Both
    # writes are in the same SQLite transaction, so CORE must remain unfenced.
    assert store.enqueue_control_webhook(
        "exit-1", "AUTOPROP_ICT_FUSION|CORE|EXIT|LONG", "EXIT",
        receipt_epoch=20.0, priority=90, engine="CORE", side="LONG",
        fence_scope="CORE",
    ) is False
    assert store.latest_control_fence("CORE") is None

    row = store.claim_next_control_webhook()
    assert row is not None
    assert row["event_key"] == "exit-1"
    assert row["receipt_epoch"] == 10.0


def test_existing_control_fence_table_migrates_with_safe_exit_kind(tmp_path):
    path = tmp_path / "legacy-control.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE control_fences ("
            "scope TEXT PRIMARY KEY, fence_epoch REAL NOT NULL, event_key TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO control_fences(scope,fence_epoch,event_key) VALUES(?,?,?)",
            ("ORG", 12.5, "legacy-exit"),
        )

    store = Store(str(path))

    assert store.latest_control_fence_info("ORG") == {
        "scope": "ORG",
        "fence_epoch": 12.5,
        "event_key": "legacy-exit",
        "kind": "EXIT",
    }


@pytest.mark.asyncio
async def test_disarmed_webhook_rejects_entry_but_durably_queues_safety_control(
        monkeypatch, tmp_path):
    store = Store(str(tmp_path / "webhook.sqlite3"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_worker_wakeup", None)
    monkeypatch.setattr(main.settings, "AUTOPROP_WEBHOOK_TOKEN", "secret")
    monkeypatch.setattr(main.settings, "TRADINGVIEW_ALERT_CONTRACT_VERIFIED", False)

    entry = await main.webhook(
        "secret",
        _BodyRequest(
            "AUTOPROP_ICT_FUSION|ORG|ENTRY|LONG|ENTRY=100|SL=95|TP=110"
        ),
    )
    assert entry == {
        "accepted": False,
        "disarmed": True,
        "kind": "ENTRY",
        "reason": "TRADINGVIEW_ALERT_CONTRACT_VERIFIED=false",
    }
    assert store.webhook_rows() == []

    safety = await main.webhook(
        "secret", _BodyRequest("AUTOPROP_ICT_FUSION|ORG|EXIT|LONG")
    )
    assert safety["accepted"] is True
    assert safety["queued"] is True
    assert safety["kind"] == "EXIT"
    assert safety["safety_control_allowed_while_disarmed"] is True

    claimed = store.claim_next_control_webhook()
    assert claimed is not None
    assert claimed["kind"] == "EXIT"
    assert store.entry_aborted_after("ORG", claimed["receipt_epoch"] - 0.001)


def test_health_reports_open_circuit_and_unresolved_exposure(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "health.sqlite3"))
    assert store.create_entry_attempt(_attempt())
    store.trip_entry_circuit(
        reason="accepted exposure needs reconciliation",
        event_key="attempt-org",
        outcome="READBACK_UNCONFIRMED",
    )
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_accounts", lambda: [_rule()])
    monkeypatch.setattr(main, "readiness", lambda settings, accounts: {
        "configuration_ready": True,
        "broker_mutation_armed": True,
        "problems": [],
        "warnings": [],
        "registered_accounts": 1,
        "enabled_accounts": 1,
    })
    monkeypatch.setattr(main.settings, "TRADINGVIEW_ALERT_CONTRACT_VERIFIED", True)

    payload = main.health()

    assert payload["entry_circuit_open"] is True
    assert payload["entry_circuit_reason"] == "accepted exposure needs reconciliation"
    assert payload["unresolved_entry_attempts"] == 1
    assert payload["broker_mutation_armed"] is False


@pytest.mark.asyncio
async def test_management_webhook_defers_and_requeues_until_entry_reconciles(
        monkeypatch, tmp_path):
    store = Store(str(tmp_path / "management.sqlite3"))
    assert store.create_entry_attempt(_attempt(engine="SILVER"))
    raw = (
        "AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|LONG|"
        "CONTRACT=SILVER_SB35_STAGE_V1|STAGE=1|LOCK_R=0.25|TRIGGER_R=2"
    )
    assert store.enqueue_webhook(
        "silver-management", raw, "SILVER_STOP_MOVE", receipt_epoch=0.0,
        priority=60, engine="SILVER", side="LONG",
    )

    router = LiveRouter.__new__(LiveRouter)
    router.store = store
    router.accounts = []
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_runtime", lambda: router)
    monkeypatch.setattr(main, "_worker_wakeup", None)

    seen_receipts: list[float | None] = []
    original_process = main._process_event

    async def capture_process(event, *, event_key="", receipt_epoch=None):
        seen_receipts.append(receipt_epoch)
        return await original_process(
            event, event_key=event_key, receipt_epoch=receipt_epoch
        )

    monkeypatch.setattr(main, "_process_event", capture_process)
    clock = [100.0]
    monkeypatch.setattr(store_module.time, "time", lambda: clock[0])
    deferred = asyncio.Event()
    original_defer = store.defer_webhook

    def record_defer(event_key, reason, delay_seconds=0.5):
        original_defer(event_key, reason, delay_seconds)
        deferred.set()

    monkeypatch.setattr(store, "defer_webhook", record_defer)
    worker = asyncio.create_task(main._webhook_worker(control=False))
    await asyncio.wait_for(deferred.wait(), timeout=1.0)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    rows = store.webhook_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["attempts"] == 1
    assert "awaiting broker reconciliation" in rows[0]["last_error"]
    assert seen_receipts == [0.0]

    assert store.claim_next_regular_webhook() is None
    clock[0] = 101.0
    reclaimed = store.claim_next_regular_webhook()
    assert reclaimed is not None
    assert reclaimed["event_key"] == "silver-management"
    assert reclaimed["attempts"] == 2
    assert reclaimed["receipt_epoch"] == 0.0


def test_entry_attempt_cas_accepts_string_state_and_validates_before_update(tmp_path):
    store = Store(str(tmp_path / "cas.sqlite3"))
    attempt = _attempt(state="PREPARED")
    assert store.create_entry_attempt(attempt)

    moved = store.transition_entry_attempt(
        attempt.attempt_key, "PREPARED", "SUBMITTING",
        submit_started_at_epoch=123.0,
    )
    assert moved is not None
    assert moved.state == "SUBMITTING"
    assert moved.submit_started_at_epoch == 123.0
    assert [a.attempt_key for a in store.all_entry_attempts("SUBMITTING")] == [
        attempt.attempt_key
    ]

    assert store.transition_entry_attempt(
        attempt.attempt_key, "PREPARED", "ACCEPTED"
    ) is None
    with pytest.raises(ValueError, match="invalid EntryAttempt transition fields"):
        store.transition_entry_attempt(
            attempt.attempt_key, "SUBMITTING", "ACCEPTED", account_id="other"
        )
    for controlled_update in ({"state": "FAILED"}, {"updated_at_epoch": 0.0}):
        with pytest.raises(ValueError, match="invalid EntryAttempt transition fields"):
            store.transition_entry_attempt(
                attempt.attempt_key, "SUBMITTING", "ACCEPTED",
                **controlled_update,
            )
    with pytest.raises(ValidationError):
        store.transition_entry_attempt(
            attempt.attempt_key, "SUBMITTING", "NOT_A_REAL_STATE"
        )

    persisted = store.get_entry_attempt(attempt.attempt_key)
    assert persisted is not None
    assert persisted.state == "SUBMITTING"
    assert persisted.account_id == "A1"


def test_store_preserves_explicit_zero_receipt_epoch(tmp_path):
    store = Store(str(tmp_path / "zero-receipt.sqlite3"))
    assert store.enqueue_webhook(
        "epoch-zero", "AUTOPROP_ICT_FUSION|ORG|ACCOUNT", "ACCOUNT",
        receipt_epoch=0.0,
    )

    row = store.claim_next_regular_webhook()

    assert row is not None
    assert row["receipt_epoch"] == 0.0


@pytest.mark.asyncio
async def test_circuit_reset_requires_router_to_be_disarmed(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "reset-armed.sqlite3"))
    store.trip_entry_circuit(reason="test")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main.settings, "AUTOPROP_WEBHOOK_TOKEN", "secret")
    monkeypatch.setattr(main.settings, "TRADINGVIEW_ALERT_CONTRACT_VERIFIED", True)

    with pytest.raises(HTTPException) as error:
        await main.reset_entry_circuit("secret")

    assert error.value.status_code == 409
    assert "disarm" in str(error.value.detail)
    assert store.entry_circuit()["open"] is True


@pytest.mark.asyncio
async def test_circuit_reset_refuses_unresolved_entry_attempt(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "reset-unresolved.sqlite3"))
    assert store.create_entry_attempt(_attempt(state="PREPARED"))
    store.trip_entry_circuit(reason="test")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main.settings, "AUTOPROP_WEBHOOK_TOKEN", "secret")
    monkeypatch.setattr(main.settings, "TRADINGVIEW_ALERT_CONTRACT_VERIFIED", False)

    with pytest.raises(HTTPException) as error:
        await main.reset_entry_circuit("secret")

    assert error.value.status_code == 409
    assert "unresolved entry attempts" in str(error.value.detail)
    assert store.entry_circuit()["open"] is True


@pytest.mark.asyncio
async def test_circuit_reset_requires_and_confirms_empty_broker_snapshot(
        monkeypatch, tmp_path):
    store = Store(str(tmp_path / "reset-safe.sqlite3"))
    store.trip_entry_circuit(reason="test")
    rule = _rule()

    class Runtime:
        async def _entry_snapshot(self):
            return {
                rule.crosstrade_account: {
                    "name": rule.crosstrade_account,
                    "positions": [],
                    "workingOrders": [],
                }
            }

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_accounts", lambda: [rule])
    monkeypatch.setattr(main, "_runtime", lambda: Runtime())
    monkeypatch.setattr(main.settings, "AUTOPROP_WEBHOOK_TOKEN", "secret")
    monkeypatch.setattr(main.settings, "TRADINGVIEW_ALERT_CONTRACT_VERIFIED", False)

    result = await main.reset_entry_circuit("secret")

    assert result["reset"] is True
    assert result["entry_circuit"]["open"] is False
    assert store.entry_circuit()["open"] is False


class _MutationResponse:
    def __init__(self, payload, *, malformed_json: bool = False):
        self.status_code = 200
        self.payload = payload
        self.malformed_json = malformed_json
        self.headers = {}
        self.text = "{" if malformed_json else json.dumps(payload)
        self.content = self.text.encode("utf-8")

    def json(self):
        if self.malformed_json:
            raise ValueError("invalid JSON")
        return self.payload


class _MutationSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def request(self, *args, **kwargs):
        self.calls += 1
        return self.response


async def _no_rate_wait(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_2xx_api_success_false_is_definitive_and_never_retried(monkeypatch):
    session = _MutationSession(_MutationResponse({
        "success": False,
        "error": "risk check rejected",
    }))
    client = ct.CrossTradeClient("https://example.invalid", "token")
    monkeypatch.setattr(ct, "_acquire_rate_slot", _no_rate_wait)
    monkeypatch.setattr(client, "_http_client", lambda: session)

    with pytest.raises(ct.CrossTradeError, match="API rejected request") as error:
        await client._request("POST", "/mutation", json={"qty": 1})

    assert type(error.value) is ct.CrossTradeError
    assert session.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    pytest.param(_MutationResponse(None, malformed_json=True), id="invalid-json"),
    pytest.param(_MutationResponse([{"orderId": "1"}]), id="non-object-json"),
])
async def test_malformed_2xx_mutation_is_ambiguous_and_never_retried(
        monkeypatch, response):
    session = _MutationSession(response)
    client = ct.CrossTradeClient("https://example.invalid", "token")
    monkeypatch.setattr(ct, "_acquire_rate_slot", _no_rate_wait)
    monkeypatch.setattr(client, "_http_client", lambda: session)

    with pytest.raises(ct.AmbiguousMutation):
        await client._request("POST", "/mutation", json={"qty": 1})

    assert session.calls == 1


@pytest.mark.asyncio
async def test_accepted_single_target_reconciles_to_active_from_exact_broker_proof(
        tmp_path):
    store = Store(str(tmp_path / "reconcile-active.sqlite3"))
    attempt = _attempt()
    assert store.create_entry_attempt(attempt)

    class Client:
        def __init__(self):
            self.fill_calls = 0
            self.position_calls = 0
            self.order_calls = 0
            self.flatten_calls = 0

        async def fills_order(self, order_id):
            self.fill_calls += 1
            assert order_id == "parent-1"
            return {"success": True, "data": [
                {"id": "fill-1", "qty": 1, "price": 100.0,
                 "instrument": "MNQZ6", "contractId": 991},
                {"id": "fill-2", "qty": 1, "price": 101.0,
                 "instrument": "MNQZ6", "contractId": 991},
            ]}

        async def positions(self, account):
            self.position_calls += 1
            assert account == "broker-1"
            return {"success": True, "data": [
                {"contractId": 991, "instrument": "MNQZ6",
                 "netPos": 2, "netPrice": 100.5},
            ]}

        async def position(self, account, instrument):
            raise AssertionError("singular stale position endpoint must not be used")

        async def orders(self, account):
            self.order_calls += 1
            return {"success": True, "data": [
                {"id": "target-1", "orderType": "limit", "qty": 2,
                 "limitPrice": 110.0, "ordStatus": "Working"},
                {"id": "stop-1", "orderType": "stop", "qty": 2,
                 "stopPrice": 95.0, "ordStatus": "Working"},
            ]}

        async def flatten(self, account, instrument):
            self.flatten_calls += 1
            raise AssertionError("exact accepted exposure must not be flattened")

    client = Client()
    router = LiveRouter.__new__(LiveRouter)
    router.settings = _reconcile_settings()
    router.accounts = [_rule()]
    router.store = store
    router.client = client
    router.executor = Executor(client, execution_symbol="MNQ1!")
    router.execution_symbol = "MNQ1!"

    result = await router.reconcile_entry_attempts()

    assert result["results"] == [{
        "account_id": "A1", "status": "ACTIVE", "qty": 2, "entry": 100.5,
    }]
    persisted = store.get_entry_attempt(attempt.attempt_key)
    trade = store.get_trade("A1")
    assert persisted is not None and persisted.state == "ACTIVE"
    assert persisted.actual_entry == 100.5
    assert trade is not None
    assert trade.entry == 100.5
    assert trade.current_position_qty == 2
    assert trade.target_order_ids == ["target-1"]
    assert trade.stop_order_ids == ["stop-1"]
    assert client.fill_calls == 1
    assert client.position_calls == 1
    assert client.order_calls == 1
    assert client.flatten_calls == 0


@pytest.mark.asyncio
async def test_accepted_read_timeout_stays_accepted_and_opens_circuit_without_flatten(
        tmp_path):
    store = Store(str(tmp_path / "reconcile-timeout.sqlite3"))
    attempt = _attempt(accepted_at_epoch=time.time() - 60.0)
    assert store.create_entry_attempt(attempt)

    class Client:
        def __init__(self):
            self.flatten_calls = 0

        async def fills_order(self, order_id):
            raise ct.CrossTradeError("GET fills read timeout after bounded retries")

        async def positions(self, account):
            raise AssertionError("position read must not follow a failed fill read")

        async def orders(self, account):
            raise AssertionError("bracket read must not follow a failed fill read")

        async def flatten(self, account, instrument):
            self.flatten_calls += 1

    client = Client()
    router = LiveRouter.__new__(LiveRouter)
    router.settings = _reconcile_settings(timeout=1.0)
    router.accounts = [_rule()]
    router.store = store
    router.client = client
    router.executor = Executor(client, execution_symbol="MNQ1!")
    router.execution_symbol = "MNQ1!"

    result = await router.reconcile_entry_attempts()

    assert result["results"][0]["status"] == "READBACK_PENDING"
    persisted = store.get_entry_attempt(attempt.attempt_key)
    assert persisted is not None
    assert persisted.state == "ACCEPTED"
    assert persisted.reconcile_attempts == 1
    assert store.get_trade("A1") is None
    circuit = store.entry_circuit()
    assert circuit["open"] is True
    assert circuit["outcome"] == "READBACK_UNCONFIRMED"
    assert client.flatten_calls == 0
