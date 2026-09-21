from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from crosstrade import (
    CrossTradeClient,
    InstrumentContractError,
    normalize_tradovate_symbol,
)
from events import parse_alert
from execution import Executor, ProtectionFailure
from live import LiveRouter
from models import AccountRule, AccountState, Allocation
from readiness import readiness
from settings import Settings
from store import Store


def _alloc(engine: str, qty: int = 3) -> Allocation:
    if engine == "CORE":
        return Allocation(
            account_id="A", event_id="e", engine="CORE", side="LONG", qty=qty,
            entry=100, stop=95, tp1=105, tp2=110,
            tp1_qty=(qty + 1) // 2, runner_qty=qty // 2,
            base_risk=100, effective_risk_budget=100, risk_per_contract=10,
        )
    return Allocation(
        account_id="A", event_id="e", engine=engine, side="LONG", qty=qty,
        entry=100, stop=95, tp1=105, tp1_qty=qty, runner_qty=0,
        base_risk=100, effective_risk_budget=100, risk_per_contract=10,
    )


class CapturingCrossTrade(CrossTradeClient):
    def __init__(self):
        super().__init__("https://example.invalid", "x")
        self.calls = []

    async def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"ok": True}


def test_symbol_normalizer_maps_root_to_continuous_and_rejects_other_roots():
    assert normalize_tradovate_symbol("MNQ") == "MNQ1!"
    assert normalize_tradovate_symbol("mnq1!") == "MNQ1!"
    assert normalize_tradovate_symbol("MNQU6") == "MNQU6"
    assert normalize_tradovate_symbol("MNQZ26") == "MNQZ26"
    with pytest.raises(InstrumentContractError):
        normalize_tradovate_symbol("ES1!")


@pytest.mark.asyncio
async def test_crosstrade_boundary_never_sends_bare_mnq_for_place_position_or_flatten():
    c = CapturingCrossTrade()
    original = {"instrument": "MNQ", "action": "buy", "qty": 1}
    await c.place("acct", original)
    await c.position("acct", "MNQ")
    await c.flatten("acct", "MNQ")

    assert original["instrument"] == "MNQ"  # caller payload is not mutated
    place = c.calls[0][2]["json"]
    pos = c.calls[1][2]["params"]
    flat = c.calls[2][2]["json"]
    assert place["instrument"] == "MNQ1!"
    assert pos["instrument"] == "MNQ1!"
    assert flat["instrument"] == "MNQ1!"




@pytest.mark.asyncio
async def test_remote_crosstrade_translation_400_is_classified_as_global_instrument_error(monkeypatch):
    import crosstrade as ct

    class Response:
        status_code = 400
        text = '{"success": false, "error": "Cannot translate \'MNQ\' to a Tradovate symbol."}'
        headers = {}
        content = text.encode()
        def json(self):
            return {"success": False, "error": "Cannot translate 'MNQ' to a Tradovate symbol."}

    class Session:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def request(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(ct.httpx, "AsyncClient", Session)
    c = ct.CrossTradeClient("https://example.invalid", "x", rate_limit_per_minute=999)
    with pytest.raises(ct.InstrumentContractError, match="Cannot translate"):
        await c._request("POST", "/x", json={"instrument": "MNQ1!"})


class ExecutorClient:
    def __init__(self):
        self.last_payload = None
        self.rows = []
        self.flatten_symbols = []

    async def orders(self, account):
        return {"data": list(self.rows)}

    async def all_orders(self):
        return {"data": []}

    async def place(self, account, payload):
        self.last_payload = dict(payload)
        if "atmTargets" in payload:
            # Exact Core child topology for a 4-lot long.
            self.rows.extend([
                {"id": "t1", "orderType": "Limit", "qty": 2, "limitPrice": 105},
                {"id": "s1", "orderType": "Stop", "qty": 2, "stopPrice": 95},
                {"id": "t2", "orderType": "Limit", "qty": 2, "limitPrice": 110},
                {"id": "s2", "orderType": "Stop", "qty": 2, "stopPrice": 95},
            ])
            return {"response": {"orderId": "p"}}
        self.rows.extend([
            {"id": "t1", "orderType": "Limit", "qty": payload["qty"], "limitPrice": payload["takeProfit"]},
            {"id": "s1", "orderType": "Stop", "qty": payload["qty"], "stopPrice": payload["stopLoss"]},
        ])
        return {"response": {"orderId": "p", "oso1Id": "t1", "oso2Id": "s1",
                             "osoChildIds": ["t1", "s1"]}}

    async def change(self, account, oid, payload):
        for row in self.rows:
            if row["id"] == oid:
                if "qty" in payload:
                    row["qty"] = payload["qty"]
                if "limitPrice" in payload:
                    row["limitPrice"] = payload["limitPrice"]
                    row["orderType"] = "Limit"
                if "stopPrice" in payload:
                    row["stopPrice"] = payload["stopPrice"]
                    row["orderType"] = "Stop"
                return {"ok": True}
        raise AssertionError("unknown child")

    async def flatten(self, account, instrument="MNQ1!"):
        self.flatten_symbols.append(instrument)
        return {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["ORG", "SILVER", "TGIF", "DWC"])
async def test_all_single_target_engines_emit_mnq1(engine):
    c = ExecutorClient()
    e = Executor(c, execution_symbol="MNQ", bracket_confirm_retries=1, bracket_confirm_delay=0, change_delay=0)
    await e.place_single("acct", _alloc(engine), "AP-test")
    assert c.last_payload["instrument"] == "MNQ1!"


@pytest.mark.asyncio
async def test_core_emits_mnq1():
    c = ExecutorClient()
    e = Executor(c, execution_symbol="MNQ", bracket_confirm_retries=1, bracket_confirm_delay=0, change_delay=0)
    await e.place_core("acct", _alloc("CORE", 4), "AP-core")
    assert c.last_payload["instrument"] == "MNQ1!"


@pytest.mark.asyncio
async def test_protection_failure_flatten_uses_mnq1():
    class BadBracket(ExecutorClient):
        async def place(self, account, payload):
            self.last_payload = dict(payload)
            return {"response": {"orderId": "p"}}

    c = BadBracket()
    e = Executor(c, execution_symbol="MNQ", bracket_confirm_retries=1, bracket_confirm_delay=0)
    with pytest.raises(ProtectionFailure, match="flattened"):
        await e.place_single("acct", _alloc("ORG"), "AP-bad")
    assert c.flatten_symbols == ["MNQ1!"]


def _rule(i: int) -> AccountRule:
    return AccountRule(
        account_id=f"CT_{i}", crosstrade_account=f"acct{i}", enabled=True,
        rules_verified=True, account_type="personal", profile="standard",
        starting_balance=100000, max_loss=0, max_contracts=20,
        personal_risk_method="fixed", personal_risk_value=250,
    )


@pytest.mark.asyncio
async def test_definitive_symbol_error_aborts_eight_account_fanout_after_first_broker_attempt(tmp_path):
    router = LiveRouter.__new__(LiveRouter)
    router.accounts = [_rule(i) for i in range(8)]
    router.settings = SimpleNamespace(
        MAX_STATE_AGE_SECONDS=20,
        ENTRY_SIGNAL_MAX_AGE_SECONDS=8,
        # Serialize this compatibility test so the first definitive contract
        # rejection closes admission before another mutation can start.
        ENTRY_FANOUT_MAX_CONCURRENCY=1,
    )

    async def gate(rule):
        return None

    async def state_for(rule):
        return AccountState(
            account_id=rule.account_id, closed_cash_balance=100000,
            state_timestamp=datetime.now(timezone.utc), daily_ledger_verified=True,
        )

    router.entry_gate = gate
    router.state_for = state_for

    router.store = Store(str(tmp_path / "symbol-fanout.sqlite"))

    class FailingExecutor:
        def __init__(self):
            self.calls = 0
        async def place_single(self, account, alloc, custom_order_id, **kwargs):
            self.calls += 1
            raise InstrumentContractError("HTTP 400: Cannot translate execution symbol to a Tradovate symbol")

    router.executor = FailingExecutor()
    router._custom_id = lambda event_id, account_id: f"AP-{account_id}"

    event = parse_alert("AUTOPROP_ICT_FUSION|ORG|ENTRY|LONG|ENTRY=100|SL=95|TP=110")
    out = await router.route_entry(event)

    assert router.executor.calls == 1
    assert len(out["results"]) == 8
    assert out["results"][0]["status"] == "ERROR"
    assert all(row["status"] == "ABORTED" for row in out["results"][1:])
    assert "Cannot translate" in out["global_execution_error"]
    assert "admission closed before PLACE" in out["results"][1]["reason"]


def test_execution_summary_flags_legacy_eight_account_http400_as_terminal_execution_failure():
    # Importing main is intentionally last because it initializes the production-style Store.
    import main

    old_rc6 = {
        "kind": "ENTRY",
        "engine": "ORG",
        "results": [
            {"account_id": f"CT_{i}", "status": "SKIP", "reason": "HTTP 400: Cannot translate 'MNQ' to a Tradovate symbol."}
            for i in range(8)
        ],
    }
    out = main._annotate_execution_result(old_rc6)
    summary = out["execution_summary"]
    assert summary["outcome"] == "ALL_DESTINATIONS_FAILED"
    assert summary["total"] == 8
    assert summary["routed"] == 0
    assert summary["execution_errors"] == 8
    assert summary["skipped"] == 0
    assert "Cannot translate 'MNQ'" in summary["common_error"]


def test_execution_summary_preserves_intentional_all_skip_as_no_eligible_destinations():
    import main

    result = {
        "kind": "ENTRY",
        "engine": "ORG",
        "results": [
            {"account_id": "A", "status": "SKIP", "reason": "disabled"},
            {"account_id": "B", "status": "SKIP", "reason": "duplicate event"},
        ],
    }
    assert main._annotate_execution_result(result)["execution_summary"]["outcome"] == "NO_DESTINATIONS_ELIGIBLE"


def test_readiness_validates_execution_symbol_contract():
    rule = _rule(1)
    good = Settings(
        _env_file=None, CROSSTRADE_TOKEN="x", AUTOPROP_WEBHOOK_TOKEN="y",
        AUTOPROP_LIVE_ARM="I_UNDERSTAND_LIVE_ORDERS",
        AUTOPROP_FULL_SCALE_ARM="I_UNDERSTAND_FULL_SCALE",
        TRADINGVIEW_ALERT_CONTRACT_VERIFIED=False, DEFAULT_EXECUTION_SYMBOL="MNQ",
    )
    r = readiness(good, [rule])
    assert r["configuration_ready"] is True

    bad = good.model_copy(update={"DEFAULT_EXECUTION_SYMBOL": "ES1!"})
    r2 = readiness(bad, [rule])
    assert r2["configuration_ready"] is False
    assert any("execution symbol invalid" in p for p in r2["problems"])


def test_webhook_inbox_endpoint_backfills_legacy_all_destination_failure(monkeypatch, tmp_path):
    import main
    from fastapi.testclient import TestClient
    from store import Store

    test_store = Store(str(tmp_path / "inbox.sqlite3"))
    key = "legacy-org"
    assert test_store.enqueue_webhook(key, "raw", "ENTRY")
    claimed = test_store.claim_next_webhook()
    assert claimed and claimed["event_key"] == key
    test_store.complete_webhook(key, {
        "kind": "ENTRY",
        "engine": "ORG",
        "results": [
            {"account_id": f"CT_{i}", "status": "SKIP", "reason": "HTTP 400: Cannot translate 'MNQ' to a Tradovate symbol."}
            for i in range(8)
        ],
    })
    monkeypatch.setattr(main, "store", test_store)
    monkeypatch.setattr(main.settings, "AUTOPROP_WEBHOOK_TOKEN", "secret")
    with TestClient(main.app) as client:
        payload = client.get("/admin/webhook-inbox/secret").json()
    summary = payload["events"][0]["result"]["execution_summary"]
    assert summary["outcome"] == "ALL_DESTINATIONS_FAILED"
    assert summary["execution_errors"] == 8
