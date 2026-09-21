from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import main
from crosstrade import InstrumentContractError
from models import AccountRule, AccountState, EntryAttempt
from readiness import readiness
from settings import Settings
from store import Store


def _rule() -> AccountRule:
    return AccountRule(
        account_id="A1", crosstrade_account="broker-1", enabled=True,
        rules_verified=True, account_type="personal", starting_balance=20_000,
        max_loss=0, max_contracts=20, personal_risk_method="fixed",
        personal_risk_value=250,
    )


def _state() -> AccountState:
    return AccountState(
        account_id="A1", closed_cash_balance=20_000,
        state_timestamp=datetime.now(timezone.utc), daily_ledger_verified=True,
    )


def _attempt() -> EntryAttempt:
    now = datetime.now(timezone.utc).timestamp()
    return EntryAttempt(
        attempt_key="attempt-1", account_id="A1", crosstrade_account="broker-1",
        event_id="event-1", engine="SILVER", side="LONG", qty=1,
        planned_entry=100, stop=95, tp1=110, tp1_qty=1, runner_qty=0,
        custom_order_id="CID-1", state="ACCEPTED", parent_order_id="p1",
        target_order_ids=["t1"], stop_order_ids=["s1"],
        child_order_ids=["t1", "s1"], accepted_at_epoch=now,
        submit_started_at_epoch=now - 0.1, created_at_epoch=now - 0.2,
        updated_at_epoch=now,
    )


class _BodyRequest:
    async def body(self) -> bytes:
        return b"AUTOPROP_ICT_FUSION|SILVER|ENTRY|LONG|ENTRY=100|SL=95|TP=110"


def test_readiness_rejects_non_live_execution_label():
    settings = Settings(
        AUTOPROP_EXECUTION_MODE="paper",
        AUTOPROP_LIVE_ARM="I_UNDERSTAND_LIVE_ORDERS",
        AUTOPROP_FULL_SCALE_ARM="I_UNDERSTAND_FULL_SCALE",
        CROSSTRADE_TOKEN="token", AUTOPROP_WEBHOOK_TOKEN="webhook",
    )

    result = readiness(settings, [_rule()])

    assert result["configuration_ready"] is False
    assert "execution mode must be live" in result["problems"]


def test_health_disarms_when_an_entry_attempt_is_unresolved(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "health-unresolved.sqlite3"))
    assert store.create_entry_attempt(_attempt())
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_accounts", lambda: [_rule()])
    monkeypatch.setattr(main, "readiness", lambda settings, accounts: {
        "configuration_ready": True, "broker_mutation_armed": True,
        "problems": [], "warnings": [], "registered_accounts": 1,
        "enabled_accounts": 1,
    })

    result = main.health()

    assert result["configuration_ready"] is True
    assert result["unresolved_entry_attempts"] == 1
    assert result["broker_mutation_armed"] is False


@pytest.mark.asyncio
async def test_armed_webhook_rejects_new_risk_when_attempt_is_unresolved(
        monkeypatch, tmp_path):
    store = Store(str(tmp_path / "webhook-unresolved.sqlite3"))
    assert store.create_entry_attempt(_attempt())
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main.settings, "AUTOPROP_WEBHOOK_TOKEN", "secret")
    monkeypatch.setattr(main.settings, "TRADINGVIEW_ALERT_CONTRACT_VERIFIED", True)

    with pytest.raises(HTTPException) as caught:
        await main.webhook("secret", _BodyRequest())

    assert caught.value.status_code == 503
    assert caught.value.detail["unresolved_entry_attempts"] == 1
    assert store.webhook_rows() == []


@pytest.mark.asyncio
async def test_live_readiness_reports_runtime_initialization_failure(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "runtime-failure.sqlite3"))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_accounts", lambda: [_rule()])
    monkeypatch.setattr(main, "readiness", lambda settings, accounts: {
        "configuration_ready": False, "broker_mutation_armed": False,
        "problems": ["execution symbol invalid"], "warnings": [],
        "registered_accounts": 1, "enabled_accounts": 1,
    })
    monkeypatch.setattr(main, "_runtime", lambda: (_ for _ in ()).throw(
        InstrumentContractError("invalid execution symbol")
    ))
    monkeypatch.setattr(main.settings, "AUTOPROP_WEBHOOK_TOKEN", "secret")

    result = await main.live_readiness("secret")

    assert result["configuration_ready"] is False
    assert result["broker_mutation_armed"] is False
    assert any("runtime initialization failed" in p for p in result["problems"])


@pytest.mark.asyncio
async def test_live_readiness_disarms_for_snapshot_working_orders(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "working-order.sqlite3"))
    rule = _rule()

    class Client:
        async def list_accounts(self):
            return {"data": [{"name": rule.crosstrade_account}]}

    class Runtime:
        entry_wave_active = asyncio.Event()
        client = Client()

        async def _entry_snapshot(self):
            return {rule.crosstrade_account: {
                "name": rule.crosstrade_account, "positions": [],
                "workingOrders": [{"id": "manual-order"}],
            }}

        async def state_for(self, _rule):
            return _state()

        async def position_qty(self, _rule):
            return 0

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_accounts", lambda: [rule])
    monkeypatch.setattr(main, "_runtime", lambda: Runtime())
    monkeypatch.setattr(main, "readiness", lambda settings, accounts: {
        "configuration_ready": True, "broker_mutation_armed": True,
        "problems": [], "warnings": [], "registered_accounts": 1,
        "enabled_accounts": 1,
    })
    monkeypatch.setattr(main.settings, "AUTOPROP_WEBHOOK_TOKEN", "secret")
    monkeypatch.setattr(main.settings, "TRADINGVIEW_ALERT_CONTRACT_VERIFIED", True)

    result = await main.live_readiness("secret")

    assert result["configuration_ready"] is False
    assert result["broker_mutation_armed"] is False
    assert any("working order" in p for p in result["problems"])
