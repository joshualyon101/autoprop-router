import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from events import parse_alert
from execution import ExecutionReceipt
from live import LiveRouter
from models import AccountRule
from state_fallback import (
    allocate_state_fallback, fallback_base_risk, is_transient_state_failure,
)
from store import Store


def _rule(account_type="challenge"):
    values = dict(
        account_id="A1", crosstrade_account="broker-1", enabled=True,
        rules_verified=True, account_type=account_type, profile="standard",
        starting_balance=50_000, max_loss=2_000, max_contracts=30,
        challenge_target=3_000,
    )
    if account_type == "personal":
        values.update(max_loss=0, challenge_target=0,
                      personal_risk_method="percent", personal_risk_value=0.75,
                      personal_balance_mode="virtual_eod",
                      personal_virtual_start_balance=20_000,
                      personal_broker_anchor_balance=12_000)
    return AccountRule(**values)


def _settings(**overrides):
    values = dict(
        STATE_FALLBACK_ENABLED=True,
        STATE_FALLBACK_MAX_ENTRY_WAVES=5,
        STATE_FALLBACK_MAX_DURATION_SECONDS=3600.0,
        STATE_FALLBACK_CHALLENGE_MAX_LOSS_PCT=10.0,
        STATE_FALLBACK_FUNDED_MAX_LOSS_PCT=7.5,
        STATE_FALLBACK_PERSONAL_RISK_MULTIPLIER=0.5,
        ENTRY_SIGNAL_MAX_AGE_SECONDS=8.0,
        ENTRY_FANOUT_MAX_CONCURRENCY=16,
        MAX_STATE_AGE_SECONDS=20,
        ENTRY_STATE_CACHE_MAX_AGE_SECONDS=20.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_fallback_budgets_are_conservative_and_account_type_specific():
    settings = _settings()
    assert fallback_base_risk(_rule("challenge"), settings) == pytest.approx(200.0)
    assert fallback_base_risk(_rule("funded"), settings) == pytest.approx(150.0)
    assert fallback_base_risk(_rule("personal"), settings) == pytest.approx(75.0)


def test_fallback_quantity_floors_and_never_exceeds_budget():
    plan = parse_alert(
        "AUTOPROP_ICT_FUSION|SILVER|ENTRY|SHORT|ENTRY=100|SL=107.25|TP=90|B5=1"
    ).plan
    alloc = allocate_state_fallback(
        plan, _rule("challenge"), _settings(), reason="ReadTimeout"
    )
    assert alloc.state_fallback is True
    assert alloc.risk_per_contract == pytest.approx(14.5)
    assert alloc.qty == 13
    assert alloc.qty * alloc.risk_per_contract <= alloc.effective_risk_budget
    assert (alloc.qty + 1) * alloc.risk_per_contract > alloc.effective_risk_budget


@pytest.mark.parametrize("reason", [
    "GET account network error after 4 attempts: ReadTimeout",
    "HTTP 429 snapshot_refresh_pending",
    "entry state cache stale: 42.0s > 20.0s",
])
def test_classifier_allows_only_known_transient_state_failures(reason):
    assert is_transient_state_failure(reason)


@pytest.mark.parametrize("reason", [
    'HTTP 401: {"error":"invalid_bearer"}',
    "prop MLL/failure floor missing or unverified",
    "account closed",
    "unexpected malformed response",
])
def test_classifier_rejects_auth_config_status_and_unknown_failures(reason):
    assert not is_transient_state_failure(reason)


def test_store_allows_five_waves_blocks_sixth_and_survives_restart(tmp_path):
    path = str(tmp_path / "fallback.sqlite3")
    store = Store(path)
    for wave in range(1, 6):
        result = store.reserve_state_fallback_wave(
            f"wave-{wave}", max_entry_waves=5, max_duration_seconds=3600,
            failures=[{"account_id": "A1", "reason": "ReadTimeout"}],
        )
        assert result["allowed"] is True
        assert result["entry_waves_used"] == wave

    restarted = Store(path)
    blocked = restarted.reserve_state_fallback_wave(
        "wave-6", max_entry_waves=5, max_duration_seconds=3600,
        failures=[{"account_id": "A1", "reason": "ReadTimeout"}],
    )
    assert blocked["allowed"] is False
    assert blocked["decision"] == "WAVE_LIMIT_EXHAUSTED"
    assert blocked["entry_waves_used"] == 5


def test_duplicate_event_does_not_consume_another_wave(tmp_path):
    store = Store(str(tmp_path / "fallback.sqlite3"))
    first = store.reserve_state_fallback_wave(
        "same-wave", max_entry_waves=5, max_duration_seconds=3600
    )
    duplicate = store.reserve_state_fallback_wave(
        "same-wave", max_entry_waves=5, max_duration_seconds=3600
    )
    assert first["entry_waves_used"] == 1
    assert duplicate["allowed"] is True
    assert duplicate["decision"] == "DUPLICATE_ALREADY_COUNTED"
    assert duplicate["entry_waves_used"] == 1


def test_duration_limit_blocks_even_before_five_waves(monkeypatch, tmp_path):
    import store as store_module

    now = 1_000.0
    monkeypatch.setattr(store_module.time, "time", lambda: now)
    store = Store(str(tmp_path / "fallback.sqlite3"))
    first = store.reserve_state_fallback_wave(
        "wave-1", max_entry_waves=5, max_duration_seconds=60
    )
    assert first["allowed"] is True
    now = 1_061.0
    expired = store.reserve_state_fallback_wave(
        "wave-2", max_entry_waves=5, max_duration_seconds=60
    )
    assert expired["allowed"] is False
    assert expired["decision"] == "DURATION_EXHAUSTED"
    assert expired["entry_waves_used"] == 1


def test_verified_recovery_resets_only_state_owned_circuit(tmp_path):
    store = Store(str(tmp_path / "fallback.sqlite3"))
    store.note_state_fallback_failure([{"account_id": "A1", "reason": "ReadTimeout"}])
    store.trip_entry_circuit(reason="fallback exhausted", outcome="STATE_FALLBACK_EXHAUSTED")
    recovered = store.recover_state_fallback()
    assert recovered["entry_circuit_auto_reset"] is True
    assert store.entry_circuit()["open"] is False
    assert store.state_fallback_status()["active"] is False

    store.note_state_fallback_failure([{"account_id": "A1", "reason": "ReadTimeout"}])
    store.trip_entry_circuit(reason="ambiguous PLACE", outcome="AMBIGUOUS_PLACE")
    recovered = store.recover_state_fallback()
    assert recovered["entry_circuit_auto_reset"] is False
    assert store.entry_circuit()["open"] is True
    assert store.entry_circuit()["outcome"] == "AMBIGUOUS_PLACE"


@pytest.mark.asyncio
async def test_live_route_uses_five_fallback_waves_then_blocks_before_sixth_place(tmp_path):
    rule = _rule("personal")
    store = Store(str(tmp_path / "router.sqlite3"))

    class Executor:
        def __init__(self):
            self.calls = 0

        async def submit_single(self, account, allocation, custom_id, **_kwargs):
            self.calls += 1
            return ExecutionReceipt(
                result={"success": True}, parent_order_id=f"p-{self.calls}",
                child_order_ids=[f"t-{self.calls}", f"s-{self.calls}"],
                target_order_ids=[f"t-{self.calls}"],
                stop_order_ids=[f"s-{self.calls}"],
            )

        place_single = submit_single

    router = LiveRouter.__new__(LiveRouter)
    router.accounts = [rule]
    router.store = store
    router.entry_wave_active = asyncio.Event()
    router.executor = Executor()
    router.settings = _settings()

    async def snapshot():
        return {rule.crosstrade_account: {
            "name": rule.crosstrade_account, "positions": [], "workingOrders": [],
        }}

    router._entry_snapshot = snapshot

    for wave in range(1, 7):
        event = parse_alert(
            "AUTOPROP_ICT_FUSION|SILVER|ENTRY|SHORT|"
            f"ENTRY=100|SL=105|TP=90|B5={wave}"
        )
        result = await router.route_entry(
            event, event_key=f"wave-{wave}", receipt_epoch=time.time()
        )
        if wave <= 5:
            assert result["results"][0]["status"] == "ACCEPTED"
            assert result["results"][0]["state_fallback"] is True
            attempts = store.all_entry_attempts(("ACCEPTED",))
            assert len(attempts) == 1
            store.transition_entry_attempt(
                attempts[0].attempt_key, ("ACCEPTED",), "CLOSED",
                last_error="test closes completed wave",
            )
        else:
            assert result["results"][0]["status"] == "ERROR"
            assert result["state_fallback"]["decision"] == "WAVE_LIMIT_EXHAUSTED"

    assert router.executor.calls == 5
    assert store.entry_circuit()["open"] is True
    assert store.entry_circuit()["outcome"] == "STATE_FALLBACK_EXHAUSTED"
