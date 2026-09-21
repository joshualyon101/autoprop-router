from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from events import ParsedEvent, parse_alert
from execution import ExecutionReceipt
from live import LiveRouter
from models import (
    AccountRule, AccountState, ActiveTrade, AswPending, EntryAttempt, MarketPulse,
)
from store import Store


def _rule(*, enabled: bool = True) -> AccountRule:
    return AccountRule(
        account_id="A1", crosstrade_account="broker-1", enabled=enabled,
        rules_verified=True, account_type="personal", starting_balance=20_000,
        max_loss=0, max_contracts=20, personal_risk_method="fixed",
        personal_risk_value=250,
    )


def _attempt(*, state: str, age: float = 30.0) -> EntryAttempt:
    now = time.time()
    return EntryAttempt(
        attempt_key="attempt-1", account_id="A1",
        crosstrade_account="broker-1", event_id="event-1", engine="ORG",
        side="LONG", qty=2, planned_entry=100.0, stop=95.0, tp1=110.0,
        tp1_qty=2, runner_qty=0, custom_order_id="CID-1",
        entry_receipt_epoch=now - age, inbox_event_key="inbox-1", state=state,
        parent_order_id="parent-1", child_order_ids=["target-1", "stop-1"],
        target_order_ids=["target-1"], stop_order_ids=["stop-1"],
        accepted_at_epoch=(now - age if state != "PREPARED" else None),
        submit_started_at_epoch=(now - age if state != "PREPARED" else None),
        created_at_epoch=now - age, updated_at_epoch=now - age,
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        ENTRY_SIGNAL_MAX_AGE_SECONDS=8.0,
        ENTRY_RECONCILE_TIMEOUT_SECONDS=30.0,
        ENTRY_RECONCILE_BASE_DELAY_SECONDS=0.01,
        ENTRY_RECONCILE_MAX_DELAY_SECONDS=0.01,
    )


class _FlatProofClient:
    def __init__(self, position: int):
        self.position = position
        self.flatten_calls = 0

    async def flatten(self, account, instrument):
        self.flatten_calls += 1
        return {"success": True}

    async def positions(self, account):
        if not self.position:
            return {"success": True, "data": []}
        return {"success": True, "data": [
            {"contractId": 991, "instrument": "MNQZ6", "netPos": self.position}
        ]}

    async def order_status(self, account, order_id):
        return {"success": True, "data": {"status": "Canceled"}}


class _StickyOwnedOrderClient(_FlatProofClient):
    """Cancel acknowledgement can arrive before the order becomes terminal."""

    def __init__(self):
        super().__init__(position=0)
        self.terminalize_on_cancel = False
        self.statuses = {
            "parent-1": "Filled", "target-1": "Working", "stop-1": "Working",
        }

    async def order_status(self, account, order_id):
        return {"success": True, "data": {
            "status": self.statuses.get(order_id, "Canceled"),
        }}

    async def cancel_order(self, account, order_id):
        if self.terminalize_on_cancel:
            self.statuses[order_id] = "Canceled"
        return {"success": True}

    async def fills_order(self, order_id):
        return {"success": True, "data": [{
            "id": "fill-1", "qty": 2, "price": 100.0,
            "instrument": "MNQZ6", "contractId": 991,
        }]}


def _router(store: Store, client, *, enabled: bool = True) -> LiveRouter:
    router = LiveRouter.__new__(LiveRouter)
    router.settings = _settings()
    router.accounts = [_rule(enabled=enabled)]
    router.store = store
    router.client = client
    router.execution_symbol = "MNQ1!"
    return router


def _trade(engine: str = "ORG") -> ActiveTrade:
    return ActiveTrade(
        account_id="A1", event_id="event-1", engine=engine, side="LONG",
        entry=100.0, initial_stop=95.0, current_stop=95.0, tp1=110.0,
        total_qty=2, tp1_qty=2, runner_qty=0, current_position_qty=2,
        previous_position_qty=2, parent_order_id="parent-1",
        target_order_ids=["target-1"], stop_order_ids=["stop-1"],
    )


@pytest.mark.asyncio
async def test_reconciler_aborts_prepared_attempt_after_signal_deadline(tmp_path):
    store = Store(str(tmp_path / "prepared.sqlite3"))
    assert store.create_entry_attempt(_attempt(state="PREPARED", age=30.0))
    router = _router(store, _FlatProofClient(position=0))

    result = await router.reconcile_entry_attempts()

    assert result["results"] == [{
        "account_id": "A1", "status": "ABORTED_STALE_PREPARED",
    }]
    assert store.get_entry_attempt("attempt-1").state == "ABORTED"


@pytest.mark.asyncio
async def test_attempt_stays_flattening_until_plural_position_proves_zero(tmp_path):
    store = Store(str(tmp_path / "attempt-flat.sqlite3"))
    attempt = _attempt(state="ACCEPTED", age=1.0)
    assert store.create_entry_attempt(attempt)
    client = _FlatProofClient(position=2)
    router = _router(store, client)

    first = await router._flatten_attempt_once(attempt, "test safety flatten")

    assert first["status"] == "ERROR"
    assert first["attempt_state"] == "FLATTENING"
    assert store.get_entry_attempt(attempt.attempt_key).state == "FLATTENING"

    client.position = 0
    store.transition_entry_attempt(
        attempt.attempt_key, "FLATTENING", "FLATTENING",
        next_reconcile_at_epoch=0.0,
    )
    second = await router.reconcile_entry_attempts()

    assert second["results"][0]["status"] == "FLATTENED"
    assert store.get_entry_attempt(attempt.attempt_key).state == "FLAT"


class _UnknownWorkingOrderClient(_FlatProofClient):
    """Broker state for an accepted PLACE whose response identities were lost."""

    def __init__(self, *, fail_order_reads: bool = False):
        super().__init__(position=0)
        self.fail_order_reads = fail_order_reads
        self.terminalize_on_cancel = False
        self.statuses = {
            "unknown-parent": "Working",
            "unknown-child": "Working",
        }
        self.cancel_calls: list[str] = []

    async def orders(self, account):
        if self.fail_order_reads:
            raise RuntimeError("working-order snapshot unavailable")
        return {"success": True, "data": [
            {"id": oid, "ordStatus": status}
            for oid, status in self.statuses.items()
            if status == "Working"
        ]}

    async def order_status(self, account, order_id):
        return {"success": True, "data": {
            "status": self.statuses.get(order_id, "Canceled"),
        }}

    async def cancel_order(self, account, order_id):
        self.cancel_calls.append(order_id)
        if self.terminalize_on_cancel:
            self.statuses[order_id] = "Canceled"
        return {"success": True}


def _ambiguous_flattening_attempt(**updates) -> EntryAttempt:
    return _attempt(state="FLATTENING", age=60.0).model_copy(update={
        "parent_order_id": None,
        "child_order_ids": [],
        "target_order_ids": [],
        "stop_order_ids": [],
        **updates,
    })


@pytest.mark.asyncio
async def test_ambiguous_attempt_waits_for_unknown_orders_to_be_terminal_and_absent(
        tmp_path):
    store = Store(str(tmp_path / "ambiguous-unknown-orders.sqlite3"))
    attempt = _ambiguous_flattening_attempt()
    assert store.create_entry_attempt(attempt)
    client = _UnknownWorkingOrderClient()
    router = _router(store, client)

    first = await router.reconcile_entry_attempts()

    assert first["results"][0]["status"] == "ERROR"
    assert store.get_entry_attempt(attempt.attempt_key).state == "FLATTENING"
    assert set(client.cancel_calls) == {"unknown-parent", "unknown-child"}

    client.terminalize_on_cancel = True
    store.transition_entry_attempt(
        attempt.attempt_key, "FLATTENING", "FLATTENING",
        next_reconcile_at_epoch=0.0,
    )
    second = await router.reconcile_entry_attempts()

    assert second["results"][0]["status"] == "FLATTENED"
    assert store.get_entry_attempt(attempt.attempt_key).state == "FLAT"
    assert await client.orders("broker-1") == {"success": True, "data": []}


@pytest.mark.asyncio
async def test_ambiguous_attempt_retains_ownership_when_working_order_read_fails(
        tmp_path):
    store = Store(str(tmp_path / "ambiguous-order-read-failure.sqlite3"))
    attempt = _ambiguous_flattening_attempt()
    assert store.create_entry_attempt(attempt)
    router = _router(store, _UnknownWorkingOrderClient(fail_order_reads=True))

    result = await router.reconcile_entry_attempts()

    assert result["results"][0]["status"] == "ERROR"
    assert "working-order enumeration failed" in result["results"][0]["reason"]
    assert store.get_entry_attempt(attempt.attempt_key).state == "FLATTENING"
    assert store.entry_circuit()["outcome"] == "FLATTEN_UNCONFIRMED"


@pytest.mark.asyncio
async def test_ambiguous_attempt_never_cancels_preexisting_order_and_fails_closed(
        tmp_path):
    store = Store(str(tmp_path / "ambiguous-preexisting-order.sqlite3"))
    attempt = _ambiguous_flattening_attempt(
        preexisting_order_ids=["unknown-parent"],
    )
    assert store.create_entry_attempt(attempt)
    client = _UnknownWorkingOrderClient()
    client.statuses = {"unknown-parent": "Working"}
    client.terminalize_on_cancel = True
    router = _router(store, client)

    result = await router.reconcile_entry_attempts()

    assert result["results"][0]["status"] == "ERROR"
    assert "pre-existing working orders" in result["results"][0]["reason"]
    assert client.cancel_calls == []
    assert store.get_entry_attempt(attempt.attempt_key).state == "FLATTENING"


@pytest.mark.asyncio
@pytest.mark.parametrize("fill_qty", [0, 1])
async def test_flat_zero_or_partial_fill_times_out_of_accepted_state(
        tmp_path, fill_qty):
    store = Store(str(tmp_path / f"fill-timeout-{fill_qty}.sqlite3"))
    attempt = _attempt(state="ACCEPTED", age=60.0)
    assert store.create_entry_attempt(attempt)

    class Client(_FlatProofClient):
        def __init__(self):
            super().__init__(position=0)
            self.statuses = {
                "parent-1": "Working", "target-1": "Working", "stop-1": "Working",
            }

        async def fills_order(self, order_id):
            rows = [] if fill_qty == 0 else [{
                "id": "fill-1", "qty": fill_qty, "price": 100.0,
                "instrument": "MNQZ6", "contractId": 991,
            }]
            return {"success": True, "data": rows}

        async def order_status(self, account, order_id):
            return {"success": True, "data": {
                "status": self.statuses.get(order_id, "Canceled"),
            }}

        async def cancel_order(self, account, order_id):
            self.statuses[order_id] = "Canceled"
            return {"success": True}

    client = Client()
    router = _router(store, client)
    router.settings.ENTRY_RECONCILE_TIMEOUT_SECONDS = 1.0

    result = await router.reconcile_entry_attempts()

    assert result["results"][0]["status"] == "FLATTENED"
    assert client.flatten_calls == 1
    assert store.get_entry_attempt(attempt.attempt_key).state == "FLAT"
    assert store.entry_circuit()["outcome"] == "FILL_TIMEOUT"


@pytest.mark.asyncio
async def test_global_hard_flat_keeps_disabled_account_trade_until_flat_proof(tmp_path):
    store = Store(str(tmp_path / "hard-flat.sqlite3"))
    trade = ActiveTrade(
        account_id="A1", event_id="event-1", engine="ORG", side="LONG",
        entry=100.0, initial_stop=95.0, current_stop=95.0, tp1=110.0,
        total_qty=2, tp1_qty=2, runner_qty=0, current_position_qty=2,
        previous_position_qty=2, parent_order_id="parent-1",
        target_order_ids=["target-1"], stop_order_ids=["stop-1"],
    )
    store.save_trade(trade)
    client = _FlatProofClient(position=2)
    router = _router(store, client, enabled=False)
    event = ParsedEvent(kind="HARD_FLAT", engine="GLOBAL")

    first = await router.hard_flat(event)

    assert first["results"][0]["status"] == "ERROR"
    assert first["deferred"] is True
    assert store.get_trade("A1") is not None

    client.position = 0
    second = await router.hard_flat(event)

    assert second["results"][0]["status"] == "FLATTENED"
    assert "deferred" not in second
    assert store.get_trade("A1") is None


@pytest.mark.asyncio
async def test_asw_time_flat_defers_until_pending_position_is_proven_flat(tmp_path):
    store = Store(str(tmp_path / "asw-time-flat.sqlite3"))
    now = datetime.now(timezone.utc)
    store.save_asw_pending(AswPending(
        account_id="A1", crosstrade_account="broker-1", event_id="asw-1",
        side="LONG", qty=2, native_qty=2, entry=20_000, stop=19_937.5,
        target=20_093.75, risk_per_contract=125,
        signal_time_ms=int(now.timestamp() * 1000),
        expiry_time_ms=int((now + timedelta(minutes=45)).timestamp() * 1000),
        parent_order_id="parent-1", custom_order_id="CID-ASW",
        child_order_ids=["target-1", "stop-1"], created_at=now,
    ))
    client = _FlatProofClient(position=2)
    router = _router(store, client)
    event = ParsedEvent(kind="ASW_TIME_FLAT", engine="ASW", side="LONG")

    first = await router.asw_time_flat(event)

    assert first["deferred"] is True
    assert first["results"][0]["status"] == "ERROR"
    assert store.get_asw_pending("A1") is not None

    client.position = 0
    second = await router.asw_time_flat(event)

    assert "deferred" not in second
    assert second["results"][0]["status"] == "FLATTENED"
    assert store.get_asw_pending("A1") is None


@pytest.mark.asyncio
async def test_asw_new_risk_is_blocked_while_entry_attempt_is_unresolved(tmp_path):
    store = Store(str(tmp_path / "asw-block.sqlite3"))
    assert store.create_entry_attempt(_attempt(state="PREPARED", age=1.0))
    router = _router(store, _FlatProofClient(position=0))
    now = datetime.now(timezone.utc)
    raw = (
        "AUTOPROP_ICT_FUSION|ASW|WORKING_LIMIT|LONG|CONTRACT=ASW_LIMIT_V1|"
        "ORDER_TYPE=LIMIT|TIF=DAY|QTY=2|Q0=2|ENTRY=20000|SL=19937.5|"
        f"TP=20093.75|CR=125.00|T5={int(now.timestamp() * 1000)}|"
        f"EXP={int((now + timedelta(minutes=45)).timestamp() * 1000)}"
    )

    result = await router.route_asw_working_limit(parse_alert(raw))

    assert result["results"][0]["status"] == "ABORTED"
    assert store.all_asw_pending() == []


@pytest.mark.asyncio
async def test_asw_control_fence_during_submit_is_flattened_and_not_persisted(tmp_path):
    store = Store(str(tmp_path / "asw-fence.sqlite3"))
    client = _FlatProofClient(position=0)
    router = _router(store, client)
    router.settings.MAX_STATE_AGE_SECONDS = 20

    async def gate(_rule):
        return None

    async def state_for(_rule):
        return AccountState(
            account_id="A1", closed_cash_balance=20_000,
            state_timestamp=datetime.now(timezone.utc),
            daily_ledger_verified=True,
        )

    class FenceDuringPlace:
        async def place_asw_limit(self, account, alloc, custom_order_id, **kwargs):
            store.record_control_fence(
                "ASW", time.time(), "hard-flat-during-place", "HARD_FLAT"
            )
            return ExecutionReceipt(
                {"success": True}, ["target-1", "stop-1"], "parent-1"
            )

    router.entry_gate = gate
    router.state_for = state_for
    router.executor = FenceDuringPlace()
    router._custom_id = lambda event_id, account_id: "CID-ASW"
    now = datetime.now(timezone.utc)
    raw = (
        "AUTOPROP_ICT_FUSION|ASW|WORKING_LIMIT|LONG|CONTRACT=ASW_LIMIT_V1|"
        "ORDER_TYPE=LIMIT|TIF=DAY|QTY=2|Q0=2|ENTRY=20000|SL=19937.5|"
        f"TP=20093.75|CR=125.00|T5={int(now.timestamp() * 1000)}|"
        f"EXP={int((now + timedelta(minutes=45)).timestamp() * 1000)}"
    )

    result = await router.route_asw_working_limit(parse_alert(raw))

    assert result["results"][0]["status"] == "ABORTED"
    assert client.flatten_calls == 1
    assert store.all_asw_pending() == []


@pytest.mark.asyncio
async def test_unexpected_submit_failure_does_not_strand_sibling_wave(tmp_path):
    store = Store(str(tmp_path / "submit-exception.sqlite3"))
    accounts = [
        _rule(),
        _rule().model_copy(update={
            "account_id": "A2", "crosstrade_account": "broker-2",
        }),
    ]
    router = LiveRouter.__new__(LiveRouter)
    router.settings = SimpleNamespace(
        MAX_STATE_AGE_SECONDS=20, ENTRY_SIGNAL_MAX_AGE_SECONDS=8.0,
        ENTRY_FANOUT_MAX_CONCURRENCY=2,
    )
    router.accounts = accounts
    router.store = store
    router.execution_symbol = "MNQ1!"
    both_started = asyncio.Event()
    started: list[str] = []

    class Executor:
        async def place_single(self, account, alloc, custom_order_id, **kwargs):
            started.append(account)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            if account == "broker-1":
                raise RuntimeError("unexpected serializer defect")
            return ExecutionReceipt(
                {"success": True}, ["target-2", "stop-2"], "parent-2",
                ["target-2"], ["stop-2"],
            )

        async def place_core(self, *args, **kwargs):
            raise AssertionError("not a Core fixture")

    async def gate(_rule):
        return None

    async def state_for(rule):
        return AccountState(
            account_id=rule.account_id, closed_cash_balance=20_000,
            state_timestamp=datetime.now(timezone.utc), daily_ledger_verified=True,
        )

    router.executor = Executor()
    router.entry_gate = gate
    router.state_for = state_for
    event = parse_alert(
        "AUTOPROP_ICT_FUSION|SILVER|ENTRY|SHORT|ENTRY=100|SL=105|TP=90"
    )

    result = await router.route_entry(
        event, event_key="unexpected-submit", receipt_epoch=time.time(),
    )

    statuses = {row["account_id"]: row["status"] for row in result["results"]}
    assert statuses == {"A1": "ERROR", "A2": "ACCEPTED"}
    attempts = {row.account_id: row for row in store.all_entry_attempts()}
    assert attempts["A1"].state == "SUBMITTING"
    assert attempts["A2"].state == "ACCEPTED"
    assert store.entry_circuit()["outcome"] in {
        "UNEXPECTED_SUBMIT_FAILURE", "PARTIAL_OR_FAILED_ENTRY",
    }


@pytest.mark.asyncio
async def test_full_fill_then_flat_retains_attempt_until_owned_orders_terminal(tmp_path):
    store = Store(str(tmp_path / "accepted-owned-orders.sqlite3"))
    attempt = _attempt(state="ACCEPTED", age=1.0)
    assert store.create_entry_attempt(attempt)
    client = _StickyOwnedOrderClient()
    router = _router(store, client)

    first = await router.reconcile_entry_attempts()

    assert first["results"][0]["status"] == "ERROR"
    assert store.get_entry_attempt(attempt.attempt_key).state == "ACCEPTED"

    client.terminalize_on_cancel = True
    store.transition_entry_attempt(
        attempt.attempt_key, "ACCEPTED", "ACCEPTED",
        next_reconcile_at_epoch=0.0,
    )
    second = await router.reconcile_entry_attempts()

    assert second["results"][0]["status"] == "CLOSED_BEFORE_PROMOTION"
    assert store.get_entry_attempt(attempt.attempt_key).state == "CLOSED"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["CORE", "SILVER"])
async def test_management_flat_path_defers_until_owned_orders_terminal(
        tmp_path, engine):
    store = Store(str(tmp_path / f"management-owned-{engine}.sqlite3"))
    store.save_trade(_trade(engine))
    client = _StickyOwnedOrderClient()
    router = _router(store, client)
    if engine == "CORE":
        event = ParsedEvent(kind="MARKET_PULSE", pulse=MarketPulse(
            native_time_ms=1, native_close_time_ms=2, native_bar_index=3,
            open=100, high=101, low=99, close=100, atr=1,
        ))
        handler = router.market_pulse
    else:
        event = parse_alert(
            "AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|LONG|"
            "CONTRACT=SILVER_SB35_STAGE_V1|STAGE=1|LOCK_R=0.25|TRIGGER_R=2"
        )
        handler = router.silver_stop

    first = await handler(event)

    assert first["results"][0]["status"] == "ERROR"
    assert first["deferred"] is True
    assert store.get_trade("A1") is not None

    client.terminalize_on_cancel = True
    second = await handler(event)

    assert second["results"][0]["status"] == "CLOSED"
    assert "deferred" not in second
    assert store.get_trade("A1") is None


@pytest.mark.asyncio
async def test_ordinary_exit_defers_until_owned_orders_terminal(tmp_path):
    store = Store(str(tmp_path / "exit-owned-orders.sqlite3"))
    store.save_trade(_trade("ORG"))
    client = _StickyOwnedOrderClient()
    router = _router(store, client)
    event = ParsedEvent(kind="EXIT", engine="ORG")

    first = await router.ordinary_exit(event)

    assert first["results"][0]["status"] == "ERROR"
    assert first["deferred"] is True
    assert store.get_trade("A1") is not None

    client.terminalize_on_cancel = True
    second = await router.ordinary_exit(event)

    assert second["results"][0]["status"] == "CLOSED_RECONCILED"
    assert "deferred" not in second
    assert store.get_trade("A1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("record_kind", ["pending", "active"])
async def test_asw_reconcile_retains_ownership_until_orders_terminal(
        tmp_path, record_kind):
    store = Store(str(tmp_path / f"asw-owned-{record_kind}.sqlite3"))
    if record_kind == "pending":
        now = datetime.now(timezone.utc)
        store.save_asw_pending(AswPending(
            account_id="A1", crosstrade_account="broker-1", event_id="event-1",
            side="LONG", qty=2, native_qty=2, entry=100, stop=95,
            target=110, risk_per_contract=10,
            signal_time_ms=int(now.timestamp() * 1000),
            expiry_time_ms=int((now + timedelta(minutes=45)).timestamp() * 1000),
            parent_order_id="parent-1", custom_order_id="CID-ASW",
            child_order_ids=["target-1", "stop-1"], created_at=now,
        ))
    else:
        store.save_trade(_trade("ASW"))
    client = _StickyOwnedOrderClient()
    router = _router(store, client)

    first = await router.reconcile_asw_pending()

    assert first["results"][0]["status"] == "ERROR"
    if record_kind == "pending":
        assert store.get_asw_pending("A1") is not None
    else:
        assert store.get_trade("A1") is not None

    client.terminalize_on_cancel = True
    second = await router.reconcile_asw_pending()

    expected = ("CLOSED_BEFORE_RECONCILE" if record_kind == "pending"
                else "CLOSED_RECONCILED")
    assert second["results"][0]["status"] == expected
    assert store.get_asw_pending("A1") is None
    assert store.get_trade("A1") is None
