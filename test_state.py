from pathlib import Path
import pytest

from app.models import AccountConfig
from app.persistence import Store
from app.state import AccountStateCache


@pytest.mark.asyncio
async def test_snapshot_normalizer_and_eod_mll(tmp_path: Path):
    a = AccountConfig(
        id="a", account_name="DEMO1", firm="X", program="Y", phase="challenge",
        starting_balance=50000, max_loss=2000, profit_target=3000, max_micros=30,
        drawdown_type="eod_trail_to_lock", enabled=True, rules_verified=True
    )
    store = Store(str(tmp_path / "db.sqlite"))
    cache = AccountStateCache([a], store)

    raw = {"accounts": [{
        "accountName": "DEMO1",
        "cashBalance": 50500,
        "netLiq": 50500,
        "positions": [],
        "orders": []
    }]}
    await cache.update_from_snapshot(raw)
    s = (await cache.snapshot())["a"]
    assert s.balance == 50500
    assert s.mll_floor == 48000
    assert s.cushion == 2500

    await cache.mark_eod("a", 50500)
    await cache.update_from_snapshot(raw)
    s = (await cache.snapshot())["a"]
    assert s.mll_floor == 48500
    assert s.cushion == 2000
