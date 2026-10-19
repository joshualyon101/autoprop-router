import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from events import parse_alert
from execution import ExecutionReceipt
from live import LiveRouter
from models import AccountRule, AccountState
from state_fallback import circuit_blocks_new_entry
from store import Store


def _settings(**overrides):
    values = dict(
        ARMED_LAST_KNOWN_STATE_EXECUTION=True,
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


def _rule():
    return AccountRule(
        account_id="A1", crosstrade_account="broker-1", enabled=True,
        rules_verified=True, account_type="personal", profile="standard",
        starting_balance=50_000, max_loss=0, max_contracts=100,
        challenge_target=0, personal_risk_method="percent",
        personal_risk_value=0.75, personal_balance_mode="actual_eod",
    )


def test_read_only_circuit_is_nonblocking_but_ambiguous_place_still_blocks():
    settings = _settings()
    assert circuit_blocks_new_entry({
        "open": True, "outcome": "PREFLIGHT_FAILED", "reason": "ReadTimeout",
    }, settings) is False
    assert circuit_blocks_new_entry({
        "open": True, "outcome": "AMBIGUOUS_PLACE", "reason": "timeout after PLACE",
    }, settings) is True


@pytest.mark.asyncio
async def test_snapshot_outage_uses_stale_verified_balance_and_still_places(tmp_path):
    rule = _rule()
    store = Store(str(tmp_path / "router.sqlite3"))
    store.save_cached_account_state(AccountState(
        account_id=rule.account_id, closed_cash_balance=50_000,
        state_timestamp=datetime.now(timezone.utc) - timedelta(hours=6),
        daily_ledger_verified=True,
    ))

    class Executor:
        def __init__(self):
            self.calls = 0

        async def submit_single(self, account, allocation, custom_id, **_kwargs):
            self.calls += 1
            return ExecutionReceipt(
                result={"success": True}, parent_order_id="p-1",
                child_order_ids=["t-1", "s-1"], target_order_ids=["t-1"],
                stop_order_ids=["s-1"],
            )

        place_single = submit_single

    router = LiveRouter.__new__(LiveRouter)
    router.accounts = [rule]
    router.store = store
    router.entry_wave_active = asyncio.Event()
    router.executor = Executor()
    router.settings = _settings()

    async def failed_snapshot():
        raise TimeoutError("account snapshot ReadTimeout")

    router._entry_snapshot = failed_snapshot
    event = parse_alert(
        "AUTOPROP_ICT_FUSION|SILVER|ENTRY|SHORT|"
        "ENTRY=100|SL=105|TP=90|B5=1"
    )
    result = await router.route_entry(
        event, event_key="last-known-wave", receipt_epoch=time.time()
    )

    row = result["results"][0]
    assert row["status"] == "ACCEPTED"
    assert row["state_fallback"] is True
    assert row["qty"] > 7  # More than configuration-only $75 fallback sizing.
    assert result["position_preflight"]["status"] == "BROKER_SIDE_GUARD"
    assert result["state_fallback"]["decision"] == "LAST_KNOWN_WAVE_RESERVED"
    assert result["state_fallback"]["unlimited_while_armed"] is True
    assert router.executor.calls == 1


def test_armed_last_known_waves_do_not_exhaust(tmp_path):
    store = Store(str(tmp_path / "router.sqlite3"))
    for wave in range(1, 21):
        result = store.reserve_state_fallback_wave(
            f"wave-{wave}", max_entry_waves=5, max_duration_seconds=1,
            unlimited=True,
        )
        assert result["allowed"] is True
        assert result["entry_waves_used"] == wave
