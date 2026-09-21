import asyncio
import json

import httpx
import pytest

import crosstrade as ct
from execution import Executor, ProtectionFailure
from models import Allocation


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = json.dumps(self._payload)
        self.content = self.text.encode()
        self.headers = {}

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_broker_rate_limited_get_honors_retry_after_then_succeeds(monkeypatch):
    calls = 0
    cooldowns = []

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _Resp(429, {
                    "success": False,
                    "error": "broker_rate_limited",
                    "retryAfter": 5,
                })
            return _Resp(200, {"ok": True})

    async def record_global_cooldown(seconds):
        cooldowns.append(seconds)

    async def unexpected_account_cooldown(path, seconds):
        raise AssertionError("broker cooldown must cover the whole linked identity")

    monkeypatch.setattr(ct.httpx, "AsyncClient", Session)
    monkeypatch.setattr(ct, "_apply_global_cooldown", record_global_cooldown)
    monkeypatch.setattr(ct, "_apply_account_cooldown", unexpected_account_cooldown)
    client = ct.CrossTradeClient(
        "https://example.invalid", "x", rate_limit_per_minute=999,
        request_min_interval_seconds=0, get_retry_max_retries=2,
        get_retry_delay_seconds=0,
    )

    result = await client._request("GET", "/v1/api/tv/accounts/A/orders/1/lifecycle")

    assert result == {"ok": True}
    assert calls == 2
    assert cooldowns == [5.0]


@pytest.mark.asyncio
async def test_broker_rate_limited_place_is_never_resent(monkeypatch):
    calls = 0

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return _Resp(429, {
                "success": False,
                "error": "broker_rate_limited",
                "retryAfter": 5,
            })

    monkeypatch.setattr(ct.httpx, "AsyncClient", Session)
    async def no_wait_cooldown(seconds):
        return None
    async def unexpected_account_cooldown(path, seconds):
        raise AssertionError("broker cooldown must not be account-scoped")
    monkeypatch.setattr(ct, "_apply_global_cooldown", no_wait_cooldown)
    monkeypatch.setattr(ct, "_apply_account_cooldown", unexpected_account_cooldown)
    client = ct.CrossTradeClient(
        "https://example.invalid", "x", rate_limit_per_minute=999,
        request_min_interval_seconds=0,
    )

    with pytest.raises(ct.RateLimitExceeded, match="broker_rate_limited"):
        await client._request("POST", "/v1/api/tv/accounts/A/orders/place", json={"qty": 1})
    assert calls == 1


@pytest.mark.asyncio
async def test_safe_get_scheduler_caps_concurrency_at_two(monkeypatch):
    active = 0
    peak = 0

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, *args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _Resp()

    monkeypatch.setattr(ct.httpx, "AsyncClient", Session)
    client = ct.CrossTradeClient(
        "https://example.invalid", "x", rate_limit_per_minute=999,
        request_min_interval_seconds=0, safe_get_max_concurrency=2,
    )

    await asyncio.gather(*[
        client._request("GET", f"/v1/api/tv/accounts/A/orders/{i}/lifecycle")
        for i in range(6)
    ])
    assert peak == 2


def _single_alloc() -> Allocation:
    return Allocation(
        account_id="A", event_id="e", engine="ORG", side="LONG", qty=2,
        entry=100.0, stop=95.0, tp1=105.0, tp2=None,
        tp1_qty=2, runner_qty=0, base_risk=100,
        effective_risk_budget=100, risk_per_contract=10.0,
    )


@pytest.mark.asyncio
async def test_lifecycle_failure_uses_only_exact_alternate_snapshot():
    class Client:
        async def order_lifecycle(self, account, oid):
            raise ct.CrossTradeError("lifecycle unavailable")

        async def all_orders(self):
            return {"data": [{
                "id": "s1", "ordStatus": "Working", "orderType": "Stop",
                "qty": 2, "stopPrice": 95.0,
            }]}

    snapshot = await Executor(Client())._order_snapshot(
        "A", "s1", {"id": "s1", "ordStatus": "Working"}
    )
    assert snapshot is not None
    assert snapshot["orderType"] == "Stop"
    assert snapshot["qty"] == 2
    assert snapshot["stopPrice"] == 95.0


@pytest.mark.asyncio
async def test_accepted_place_missing_role_labels_flattens_once_without_resending_place():
    class Client:
        def __init__(self):
            self.place_calls = 0
            self.flatten_calls = 0

        async def orders(self, account):
            if not self.place_calls:
                return {"data": []}
            return {"data": [
                {"id": "t1", "ordStatus": "Working"},
                {"id": "s1", "ordStatus": "Working"},
            ]}

        async def all_orders(self):
            return {"data": []}

        async def place(self, account, payload):
            self.place_calls += 1
            # A generic child list does not prove which child is the stop.
            return {"response": {"orderId": "p1", "osoChildIds": ["t1", "s1"]}}

        async def flatten(self, account, instrument):
            self.flatten_calls += 1
            return {"ok": True}

    client = Client()
    executor = Executor(client, bracket_confirm_retries=2, bracket_confirm_delay=0)

    with pytest.raises(ProtectionFailure, match="flattened"):
        await executor.place_single("A", _single_alloc(), "CID-1")
    assert client.place_calls == 1
    assert client.flatten_calls == 1


@pytest.mark.asyncio
async def test_verified_receipt_carries_roles_without_second_lifecycle_pass():
    class Client:
        def __init__(self):
            self.placed = False
            self.lifecycle_calls = 0

        async def orders(self, account):
            if not self.placed:
                return {"data": []}
            return {"data": [
                {"id": "t1", "ordStatus": "Working"},
                {"id": "s1", "ordStatus": "Working"},
            ]}

        async def order_lifecycle(self, account, oid):
            self.lifecycle_calls += 1
            if oid == "t1":
                version = {"orderId": "t1", "orderQty": 2, "orderType": "Limit", "price": 105.0}
            else:
                version = {"orderId": "s1", "orderQty": 2, "orderType": "Stop", "stopPrice": 95.0}
            return {"data": {"order": {"id": oid, "ordStatus": "Working"}, "version": version}}

        async def place(self, account, payload):
            self.placed = True
            return {"response": {"orderId": "p1", "oso1Id": "t1", "oso2Id": "s1",
                                 "osoChildIds": ["t1", "s1"]}}

        async def flatten(self, account, instrument):
            raise AssertionError("valid bracket must not flatten")

    client = Client()
    receipt = await Executor(client, bracket_confirm_retries=1, bracket_confirm_delay=0).place_single(
        "A", _single_alloc(), "CID-1"
    )

    assert receipt.target_order_ids == ["t1"]
    assert receipt.stop_order_ids == ["s1"]
    assert client.lifecycle_calls == 0
