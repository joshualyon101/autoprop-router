import pytest

import crosstrade as ct


class _Response:
    status_code = 200
    headers = {}
    content = b"{}"
    text = "{}"

    @staticmethod
    def json():
        return {"success": True, "accounts": []}


class _Http:
    async def request(self, *args, **kwargs):
        return _Response()


@pytest.mark.asyncio
async def test_entry_snapshot_uses_fast_mutation_pacing_without_bypassing_limiter(monkeypatch):
    observed = []

    async def acquire(*args, **kwargs):
        observed.append(kwargs)

    client = ct.CrossTradeClient(
        "https://example.invalid", "token",
        request_min_interval_seconds=0.10,
        mutation_min_interval_seconds=0.02,
    )
    monkeypatch.setattr(ct, "_acquire_rate_slot", acquire)
    monkeypatch.setattr(client, "_http_client", lambda: _Http())

    await client.accounts_snapshot()
    await client.get_account("A")

    assert len(observed) == 2
    assert observed[0]["min_interval_seconds"] == pytest.approx(0.02)
    assert observed[0]["reserve_tokens"] == 0.0
    assert observed[1]["min_interval_seconds"] == pytest.approx(0.10)
    assert observed[1]["reserve_tokens"] == 9.0
