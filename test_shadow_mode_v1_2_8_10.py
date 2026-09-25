from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

import crosstrade
import main
from models import AccountRule
from settings import Settings
from shadow import ShadowObserver
from store import Store


ENTRY = "AUTOPROP_ICT_FUSION|ORG|ENTRY|LONG|ENTRY=100|SL=95|TP=110"

SHADOW_EVENT_MATRIX = (
    ENTRY,
    "AUTOPROP_ICT_FUSION|ORG|EXIT|LONG",
    "AUTOPROP_ICT_FUSION|ORG|REQUIRED_FLAT_EXIT",
    (
        "AUTOPROP_ICT_FUSION|MARKET_PULSE|T5=1|TC5=2|B5=3|"
        "O=100|H=101|L=99|C=100|ATR=2"
    ),
    (
        "AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|LONG|"
        "CONTRACT=SILVER_SB35_STAGE_V1|STAGE=1|LOCK_R=0.25|"
        "TRIGGER_R=2|QTY=3|SL=97.50"
    ),
    (
        "AUTOPROP_ICT_FUSION|SILVER|MOVE_STOP|LONG|"
        "CONTRACT=SILVER_SB35_STAGE_V1|STAGE=2|LOCK_R=0.50|"
        "TRIGGER_R=3|QTY=2|SL=99.00"
    ),
    (
        "AUTOPROP_ICT_FUSION|ASW|WORKING_LIMIT|LONG|CONTRACT=ASW_LIMIT_V1|"
        "ORDER_TYPE=LIMIT|TIF=DAY|QTY=6|Q0=2|ENTRY=20000|SL=19937.5|"
        "TP=20093.75|CR=125.00|T5=1789446600000|EXP=1789449300000"
    ),
    (
        "AUTOPROP_ICT_FUSION|ASW|CANCEL_PENDING|LONG|"
        "CONTRACT=ASW_LIMIT_V1|T5=1789446600000"
    ),
    "AUTOPROP_ICT_FUSION|ASW|TIME_FLAT|LONG|CONTRACT=ASW_LIMIT_V1",
)


class _BodyRequest:
    def __init__(self, raw: str):
        self.raw = raw

    async def body(self) -> bytes:
        return self.raw.encode("utf-8")


def _rule() -> AccountRule:
    return AccountRule(
        account_id="A1",
        crosstrade_account="broker-A1",
        enabled=True,
        rules_verified=True,
        account_type="personal",
        starting_balance=20_000,
        max_loss=0,
        max_contracts=20,
        personal_risk_method="fixed",
        personal_risk_value=250,
    )


def _shadow_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        AUTOPROP_EXECUTION_MODE="shadow",
        AUTOPROP_WEBHOOK_TOKEN="secret",
        SQLITE_PATH=str(tmp_path / "live.sqlite3"),
        SHADOW_SQLITE_PATH=str(tmp_path / "shadow.sqlite3"),
        CROSSTRADE_TOKEN="",
        SHADOW_BROKER_READS_ENABLED=False,
        TRADINGVIEW_ALERT_CONTRACT_VERIFIED=False,
        AUTO_DISCOVERY=True,
    )


def _post_endpoint(path: str):
    for route in main.app.routes:
        if getattr(route, "path", None) == path and "POST" in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"missing POST route {path}")


def _install_shadow_globals(monkeypatch, tmp_path: Path) -> Store:
    settings = _shadow_settings(tmp_path)
    shadow_store = Store(settings.SHADOW_SQLITE_PATH)
    rule = _rule()
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "_EXECUTION_MODE", "shadow")
    monkeypatch.setattr(main, "_SHADOW_MODE", True)
    monkeypatch.setattr(main, "_LIVE_MODE", False)
    monkeypatch.setattr(main, "store", shadow_store)
    monkeypatch.setattr(main, "_accounts", lambda: [rule])
    monkeypatch.setattr(
        main,
        "_shadow_instance",
        ShadowObserver(shadow_store, lambda: [rule]),
    )
    monkeypatch.setattr(main, "_worker_wakeup", asyncio.Event())
    monkeypatch.setattr(main, "_control_worker_wakeup", None)
    return shadow_store


def test_shadow_store_is_selected_from_shadow_sqlite_path(tmp_path):
    live_path = tmp_path / "must-not-open-live.sqlite3"
    shadow_path = tmp_path / "observer.sqlite3"
    script = (
        "import json, main; "
        "print(json.dumps({'mode': main._SHADOW_MODE, "
        "'store': main.store.path, 'configured': main.settings.SHADOW_SQLITE_PATH}))"
    )
    env = os.environ.copy()
    env.update(
        {
            "AUTOPROP_EXECUTION_MODE": "shadow",
            "SQLITE_PATH": str(live_path),
            "SHADOW_SQLITE_PATH": str(shadow_path),
            "RISK_STATE_JSON": "",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parent,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload == {
        "mode": True,
        "store": str(shadow_path),
        "configured": str(shadow_path),
    }
    assert shadow_path.exists()
    assert not live_path.exists()


@pytest.mark.asyncio
async def test_shadow_endpoint_isolated_from_gate_and_legacy_live_circuit(
    monkeypatch, tmp_path
):
    shadow_store = _install_shadow_globals(monkeypatch, tmp_path)
    legacy_live_store = Store(str(tmp_path / "legacy-live.sqlite3"))
    legacy_live_store.trip_entry_circuit(
        reason="legacy live incident", event_key="old", outcome="OLD_LIVE_FAILURE"
    )

    live_endpoint = _post_endpoint("/webhook/tradingview/{token}")
    shadow_endpoint = _post_endpoint("/webhook/tradingview-shadow/{token}")

    with pytest.raises(HTTPException) as blocked:
        await live_endpoint("secret", _BodyRequest(ENTRY))
    assert blocked.value.status_code == 409

    response = await shadow_endpoint("secret", _BodyRequest(ENTRY))

    assert response["accepted"] is True
    assert response["kind"] == "ENTRY"
    assert response["broker_mutation_performed"] is False
    assert len(shadow_store.webhook_rows()) == 1
    assert legacy_live_store.entry_circuit()["open"] is True
    assert legacy_live_store.webhook_rows() == []


@pytest.mark.asyncio
async def test_identical_same_day_shadow_exits_are_recorded_separately(
    monkeypatch, tmp_path
):
    shadow_store = _install_shadow_globals(monkeypatch, tmp_path)
    shadow_endpoint = _post_endpoint("/webhook/tradingview-shadow/{token}")
    raw = "AUTOPROP_ICT_FUSION|CORE|EXIT|SHORT|TP1_OR_STOP"

    first = await shadow_endpoint("secret", _BodyRequest(raw))
    second = await shadow_endpoint("secret", _BodyRequest(raw))

    assert first["event_key"] != second["event_key"]
    assert first["queued"] is True
    assert second["queued"] is True
    assert len(shadow_store.webhook_rows()) == 2


@pytest.mark.asyncio
async def test_shadow_webhook_worker_observes_full_matrix_with_zero_broker_mutations(
    monkeypatch, tmp_path
):
    shadow_store = _install_shadow_globals(monkeypatch, tmp_path)
    mutation_calls: list[str] = []

    async def forbidden_runtime_mutation(self, *args, **kwargs):
        mutation_calls.append("broker mutation")
        raise AssertionError("shadow path reached a broker mutation method")

    for method in ("place", "change", "cancel_order", "flatten"):
        monkeypatch.setattr(
            crosstrade.CrossTradeClient, method, forbidden_runtime_mutation
        )

    def forbidden_live_runtime():
        raise AssertionError("shadow worker constructed the live runtime")

    monkeypatch.setattr(main, "_runtime", forbidden_live_runtime)
    shadow_endpoint = _post_endpoint("/webhook/tradingview-shadow/{token}")

    responses = [
        await shadow_endpoint("secret", _BodyRequest(raw))
        for raw in SHADOW_EVENT_MATRIX
    ]
    assert all(response["accepted"] is True for response in responses)
    assert all(response["broker_mutation_performed"] is False for response in responses)

    worker = asyncio.create_task(main._webhook_worker(control=False))
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            rows = shadow_store.webhook_rows(50)
            if len(rows) == len(SHADOW_EVENT_MATRIX) and all(
                row["status"] == "DONE" for row in rows
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail(f"shadow worker did not drain its inbox: {rows!r}")
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    results = [row["result"] for row in shadow_store.webhook_rows(50)]
    assert {row["kind"] for row in results} == {
        "ENTRY",
        "EXIT",
        "HARD_FLAT",
        "MARKET_PULSE",
        "SILVER_STOP_MOVE",
        "ASW_WORKING_LIMIT",
        "ASW_CANCEL_PENDING",
        "ASW_TIME_FLAT",
    }
    assert all(row["status"] == "SHADOW_OBSERVED" for row in results)
    assert all(row["action"].startswith("WOULD_") for row in results)
    assert all(row["broker_mutation_performed"] is False for row in results)
    assert mutation_calls == []

    # Shadow observations may update only the inbox and shadow_* diagnostic documents.
    assert shadow_store.all_entry_attempts() == []
    assert shadow_store.all_trades() == []
    assert shadow_store.all_asw_pending() == []
    assert not shadow_store.silver_stop_intent()
    assert shadow_store.entry_circuit()["open"] is False
    with shadow_store.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM control_fences").fetchone()[0] == 0
        keys = {
            row[0]
            for row in conn.execute("SELECT key FROM runtime_state").fetchall()
        }
    assert keys <= {"shadow_observer_summary", "shadow_last_event"}


def test_shadow_health_exposes_role_path_and_hard_no_mutation_invariants(
    monkeypatch, tmp_path
):
    _install_shadow_globals(monkeypatch, tmp_path)

    payload = main.health()

    assert payload["execution_mode"] == "shadow"
    assert payload["router_role"] == "shadow_observer"
    assert (
        payload["production_execution_path"]
        == "direct_tradingview_to_crosstrade"
    )
    assert payload["shadow_intake_ready"] is True
    assert payload["shadow_broker_reads_enabled"] is False
    assert payload["broker_mutation_capable"] is False
    assert payload["broker_mutation_armed"] is False
    assert payload["tradingview_alert_contract_verified"] is False


@pytest.mark.asyncio
async def test_shadow_live_admin_controls_cannot_reach_runtime_or_reset_circuit(
    monkeypatch, tmp_path
):
    shadow_store = _install_shadow_globals(monkeypatch, tmp_path)
    shadow_store.trip_entry_circuit(
        reason="sentinel circuit", event_key="sentinel", outcome="TEST"
    )

    def forbidden_live_runtime():
        raise AssertionError("shadow admin path constructed the live runtime")

    monkeypatch.setattr(main, "_runtime", forbidden_live_runtime)

    for operation in (
        main.live_readiness,
        main.discovery,
        main.reset_entry_circuit,
    ):
        with pytest.raises(HTTPException) as blocked:
            await operation("secret")
        assert blocked.value.status_code == 409

    dry_run = await main.dry_run("secret")
    assert dry_run["router_role"] == "shadow_observer"
    assert dry_run["broker_mutation_performed"] is False
    assert shadow_store.entry_circuit()["reason"] == "sentinel circuit"


@pytest.mark.asyncio
async def test_shadow_startup_launches_only_one_observer_worker(monkeypatch, tmp_path):
    _install_shadow_globals(monkeypatch, tmp_path)
    created: list[tuple[str | None, object]] = []

    class _FakeTask:
        pass

    def fake_create_task(coroutine, *, name=None):
        coroutine.close()
        task = _FakeTask()
        created.append((name, task))
        return task

    def forbidden_live_runtime():
        raise AssertionError("shadow startup constructed the live runtime")

    async def forbidden_discovery():
        raise AssertionError("shadow startup ran broker discovery")

    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(main, "_runtime", forbidden_live_runtime)
    monkeypatch.setattr(main, "_run_auto_discovery", forbidden_discovery)
    for name in (
        "_worker_task",
        "_control_worker_task",
        "_asw_reconcile_task",
        "_entry_reconcile_task",
        "_silver_management_task",
        "_state_refresh_task",
        "_auto_discovery_task",
    ):
        monkeypatch.setattr(main, name, None)

    await main._start_worker()

    assert [name for name, _ in created] == ["autoprop-shadow-observer-worker"]
    assert main._worker_task is created[0][1]
    assert main._control_worker_task is None
    assert main._asw_reconcile_task is None
    assert main._entry_reconcile_task is None
    assert main._silver_management_task is None
    assert main._state_refresh_task is None
    assert main._auto_discovery_task is None
