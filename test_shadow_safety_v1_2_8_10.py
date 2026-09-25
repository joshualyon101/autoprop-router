from __future__ import annotations

import httpx
import pytest

import crosstrade as ct
from models import AccountRule
from readiness import readiness
from settings import Settings


def _rule() -> AccountRule:
    return AccountRule(
        account_id="A1",
        crosstrade_account="broker-1",
        enabled=True,
        rules_verified=True,
        account_type="personal",
        starting_balance=20_000,
        max_loss=0,
        max_contracts=20,
        personal_risk_method="fixed",
        personal_risk_value=250,
    )


@pytest.mark.asyncio
async def test_shadow_firewall_blocks_every_mutation_before_rate_or_transport(monkeypatch):
    rate_called = False
    transport_called = False

    async def forbidden_rate_slot(*args, **kwargs):
        nonlocal rate_called
        rate_called = True
        raise AssertionError("shadow mutation reached the rate limiter")

    def forbidden_http_client(self):
        nonlocal transport_called
        transport_called = True
        raise AssertionError("shadow mutation reached HTTP transport")

    monkeypatch.setattr(ct, "_acquire_rate_slot", forbidden_rate_slot)
    monkeypatch.setattr(ct.CrossTradeClient, "_http_client", forbidden_http_client)
    client = ct.CrossTradeClient("https://example.invalid", "token", execution_mode="shadow")

    mutations = [
        client.place("broker-1", {"instrument": "MNQ1!"}),
        client.cancel_order("broker-1", "order-1"),
        client.change("broker-1", "order-1", {"price": 100}),
        client.flatten("broker-1"),
    ]
    for mutation in mutations:
        with pytest.raises(ct.BrokerMutationDisabled, match="broker mutations are disabled"):
            await mutation

    assert rate_called is False
    assert transport_called is False


@pytest.mark.asyncio
async def test_shadow_firewall_allows_explicit_read_only_get(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def immediate_rate_slot(*args, **kwargs):
        return None

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            calls.append((method, url))
            return httpx.Response(200, json={"data": []})

    fake = FakeHttp()
    monkeypatch.setattr(ct, "_acquire_rate_slot", immediate_rate_slot)
    monkeypatch.setattr(ct.CrossTradeClient, "_http_client", lambda self: fake)
    client = ct.CrossTradeClient("https://example.invalid", "token", execution_mode="shadow")

    assert await client.list_accounts() == {"data": []}
    assert calls == [("GET", "https://example.invalid/v1/api/tv/accounts")]


def test_shadow_is_default_and_does_not_require_live_arms_gate_or_broker_token():
    settings = Settings(
        _env_file=None,
        AUTOPROP_WEBHOOK_TOKEN="webhook",
        AUTOPROP_LIVE_ARM="",
        AUTOPROP_FULL_SCALE_ARM="",
        CROSSTRADE_TOKEN="",
        TRADINGVIEW_ALERT_CONTRACT_VERIFIED=False,
        SHADOW_BROKER_READS_ENABLED=False,
    )

    result = readiness(settings, [_rule()])

    assert settings.AUTOPROP_EXECUTION_MODE == "shadow"
    assert settings.SHADOW_SQLITE_PATH == "/data/autoprop_router_shadow.sqlite3"
    assert result["execution_mode"] == "shadow"
    assert result["shadow_mode"] is True
    assert result["configuration_ready"] is True
    assert result["broker_mutation_armed"] is False
    assert "live arm missing" not in result["problems"]
    assert "full-scale arm missing" not in result["problems"]
    assert "CrossTrade token missing" not in result["problems"]


def test_shadow_never_arms_mutations_even_when_live_gates_are_present():
    settings = Settings(
        _env_file=None,
        AUTOPROP_EXECUTION_MODE="shadow",
        AUTOPROP_WEBHOOK_TOKEN="webhook",
        AUTOPROP_LIVE_ARM="I_UNDERSTAND_LIVE_ORDERS",
        AUTOPROP_FULL_SCALE_ARM="I_UNDERSTAND_FULL_SCALE",
        CROSSTRADE_TOKEN="token",
        TRADINGVIEW_ALERT_CONTRACT_VERIFIED=True,
    )

    result = readiness(settings, [_rule()])

    assert result["configuration_ready"] is True
    assert result["broker_mutation_armed"] is False


def test_shadow_broker_reads_fail_closed_until_observer_support_exists():
    settings = Settings(
        _env_file=None,
        AUTOPROP_EXECUTION_MODE="shadow",
        AUTOPROP_WEBHOOK_TOKEN="webhook",
        CROSSTRADE_TOKEN="",
        SHADOW_BROKER_READS_ENABLED=True,
    )

    result = readiness(settings, [_rule()])

    assert result["configuration_ready"] is False
    assert any("not implemented" in problem for problem in result["problems"])
    assert "CrossTrade token missing for shadow broker reads" in result["problems"]
    assert result["broker_mutation_armed"] is False


def test_shadow_database_must_not_alias_live_database(tmp_path):
    shared = str(tmp_path / "shared.sqlite3")
    settings = Settings(
        _env_file=None,
        AUTOPROP_EXECUTION_MODE="shadow",
        AUTOPROP_WEBHOOK_TOKEN="webhook",
        SQLITE_PATH=shared,
        SHADOW_SQLITE_PATH=shared,
        SHADOW_BROKER_READS_ENABLED=False,
    )

    result = readiness(settings, [_rule()])

    assert result["configuration_ready"] is False
    assert "SHADOW_SQLITE_PATH must differ from SQLITE_PATH" in result["problems"]
    assert result["broker_mutation_armed"] is False


def test_live_readiness_behavior_remains_armed_only_after_all_existing_gates():
    settings = Settings(
        _env_file=None,
        AUTOPROP_EXECUTION_MODE="live",
        AUTOPROP_WEBHOOK_TOKEN="webhook",
        AUTOPROP_LIVE_ARM="I_UNDERSTAND_LIVE_ORDERS",
        AUTOPROP_FULL_SCALE_ARM="I_UNDERSTAND_FULL_SCALE",
        CROSSTRADE_TOKEN="token",
        TRADINGVIEW_ALERT_CONTRACT_VERIFIED=True,
    )

    result = readiness(settings, [_rule()])

    assert result["execution_mode"] == "live"
    assert result["shadow_mode"] is False
    assert result["configuration_ready"] is True
    assert result["broker_mutation_armed"] is True


def test_unknown_execution_mode_fails_closed():
    settings = Settings(
        _env_file=None,
        AUTOPROP_EXECUTION_MODE="paper",
        AUTOPROP_WEBHOOK_TOKEN="webhook",
    )

    result = readiness(settings, [_rule()])

    assert result["configuration_ready"] is False
    assert "supported execution modes are live or shadow" in result["problems"]
    assert result["broker_mutation_armed"] is False
