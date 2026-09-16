import httpx
import pytest

import crosstrade as ct


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        import json
        self.text = json.dumps(self._payload)
        self.content = self.text.encode()
        self.headers = {}
    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_get_timeout_retries_then_succeeds(monkeypatch):
    calls = 0
    class Session:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, *a, **k):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ReadTimeout("slow")
            return _Resp(200, {"ok": True})
    monkeypatch.setattr(ct.httpx, "AsyncClient", Session)
    c = ct.CrossTradeClient("https://example.invalid", "x", rate_limit_per_minute=999,
                            get_retry_max_retries=2, get_retry_delay_seconds=0)
    out = await c._request("GET", "/x")
    assert out == {"ok": True}
    assert calls == 2


@pytest.mark.asyncio
async def test_get_timeout_exhausts_bounded_retries(monkeypatch):
    calls = 0
    class Session:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, *a, **k):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(ct.httpx, "AsyncClient", Session)
    c = ct.CrossTradeClient("https://example.invalid", "x", rate_limit_per_minute=999,
                            get_retry_max_retries=2, get_retry_delay_seconds=0)
    with pytest.raises(ct.CrossTradeError, match="after 3 attempt"):
        await c._request("GET", "/x")
    assert calls == 3


@pytest.mark.asyncio
async def test_mutation_timeout_still_never_retries(monkeypatch):
    calls = 0
    class Session:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, *a, **k):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("ambiguous")
    monkeypatch.setattr(ct.httpx, "AsyncClient", Session)
    c = ct.CrossTradeClient("https://example.invalid", "x", rate_limit_per_minute=999,
                            get_retry_max_retries=2, get_retry_delay_seconds=0)
    with pytest.raises(ct.AmbiguousMutation):
        await c._request("POST", "/x", json={"x": 1})
    assert calls == 1


@pytest.mark.asyncio
async def test_snapshot_refresh_pending_get_retries_then_succeeds(monkeypatch):
    calls = 0
    class Session:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, *a, **k):
            nonlocal calls
            calls += 1
            if calls == 1:
                r = _Resp(429, {"success": False, "error": "snapshot_refresh_pending", "retryAfter": 0})
                return r
            return _Resp(200, {"ok": True})
    monkeypatch.setattr(ct.httpx, "AsyncClient", Session)
    c = ct.CrossTradeClient("https://example.invalid", "x", rate_limit_per_minute=999,
                            get_retry_max_retries=2, get_retry_delay_seconds=0)
    out = await c._request("GET", "/v1/api/tv/accounts/a")
    assert out == {"ok": True}
    assert calls == 2
