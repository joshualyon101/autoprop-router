from types import SimpleNamespace
import pytest

from app.live import LiveRouter
from app.execution import ProtectionFailure


class FillClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0
    async def fills_order(self, order_id):
        self.calls += 1
        return {"data": list(self.rows)}


def settings(retries=1):
    return SimpleNamespace(
        CROSSTRADE_BASE_URL="https://example.invalid",
        CROSSTRADE_TOKEN="x",
        REQUEST_TIMEOUT_SECONDS=1,
        BRACKET_CONFIRM_RETRIES=retries,
        BRACKET_CONFIRM_RETRY_DELAY_SECONDS=0,
        MANAGEMENT_CHANGE_RETRIES=1,
        MANAGEMENT_CHANGE_RETRY_DELAY_SECONDS=0,
    )


@pytest.mark.asyncio
async def test_entry_fill_price_uses_exact_weighted_destination_fill():
    r = LiveRouter.__new__(LiveRouter)
    r.settings = settings()
    r.client = FillClient([
        {"id": 1, "qty": 1, "price": 100.25},
        {"id": 2, "qty": 2, "price": 100.50},
    ])
    px = await r.entry_fill_price("123", 3)
    assert px == pytest.approx((100.25 + 2 * 100.50) / 3)


@pytest.mark.asyncio
async def test_entry_fill_price_dedupes_repeated_fill_rows():
    r = LiveRouter.__new__(LiveRouter)
    r.settings = settings()
    r.client = FillClient([
        {"id": 1, "qty": 1, "price": 100.25},
        {"id": 1, "qty": 1, "price": 100.25},
        {"id": 2, "qty": 1, "price": 100.50},
    ])
    px = await r.entry_fill_price("123", 2)
    assert px == pytest.approx(100.375)


@pytest.mark.asyncio
async def test_entry_fill_price_fails_closed_when_qty_not_proven():
    r = LiveRouter.__new__(LiveRouter)
    r.settings = settings()
    r.client = FillClient([{"id": 1, "qty": 1, "price": 100.25}])
    with pytest.raises(ProtectionFailure, match="not proven"):
        await r.entry_fill_price("123", 2)


@pytest.mark.asyncio
async def test_entry_fill_price_rejects_overfill():
    r = LiveRouter.__new__(LiveRouter)
    r.settings = settings()
    r.client = FillClient([{"id": 1, "qty": 3, "price": 100.25}])
    with pytest.raises(ProtectionFailure, match="exceed"):
        await r.entry_fill_price("123", 2)
