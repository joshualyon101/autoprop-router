from __future__ import annotations

import pytest

from events import parse_alert
from models import AccountRule
from shadow import ShadowObserver


class ShadowStore:
    def __init__(self):
        self.runtime: dict[str, dict] = {}

    def get_runtime_state(self, key: str):
        assert key.startswith("shadow_")
        return self.runtime.get(key)

    def set_runtime_state(self, key: str, value: dict):
        assert key.startswith("shadow_")
        self.runtime[key] = value

    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected live store access: {name}")


def org_alert(*, qty: int, base: int, spread: str, enabled: int, flagged: int) -> str:
    return (
        "AUTOPROP_ICT_FUSION|ORG|ENTRY|LONG|"
        f"QTY={qty}|ENTRY=21000|SL=20980|TP=21040|RG_VER=EW5_B50|"
        f"RG_EN={enabled}|B5={spread}|RG_HALF={flagged}|Q_BASE={base}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("qty", "base", "spread", "enabled", "flagged", "expected"),
    [
        (4, 8, "0.566", 1, 1, "MATCH"),
        (1, 3, "0.75", 1, 1, "MATCH"),
        (1, 1, "0.75", 1, 1, "MATCH"),
        (8, 8, "0.50", 1, 0, "MATCH"),
        (8, 8, "0.51", 0, 0, "MATCH"),
        (3, 3, "NaN", 1, 0, "MATCH"),
        (8, 8, "0.51", 1, 0, "MISMATCH"),
        (5, 8, "0.51", 1, 1, "MISMATCH"),
    ],
)
async def test_org_breadth_is_observed_without_recalculating_account_sizes(
    qty, base, spread, enabled, flagged, expected
):
    store = ShadowStore()
    account = AccountRule(
        account_id="A", crosstrade_account="broker-A", enabled=True,
        rules_verified=True, account_type="personal",
        starting_balance=20_000, max_loss=0, max_contracts=20,
    )
    observer = ShadowObserver(store, lambda: [account])
    event = parse_alert(org_alert(qty=qty, base=base, spread=spread,
                                  enabled=enabled, flagged=flagged))

    result = await observer.observe(event, "org-event", 1.0)

    assert result["status"] == "SHADOW_OBSERVED"
    assert result["broker_mutation_performed"] is False
    assert result["plan"]["source_qty"] == qty
    assert result["regime_sizing"]["verification"] == expected
    assert result["regime_sizing"]["base_qty"] == base
    assert store.runtime["shadow_last_event"]["regime_sizing"] == result["regime_sizing"]


@pytest.mark.asyncio
async def test_legacy_org_entry_remains_observable_without_regime_diagnostic():
    store = ShadowStore()
    observer = ShadowObserver(store, lambda: [])
    event = parse_alert("AUTOPROP_ICT_FUSION|ORG|ENTRY|SHORT|QTY=3|ENTRY=21000|SL=21020|TP=20960")
    result = await observer.observe(event, "legacy", 1.0)
    assert result["action"] == "WOULD_ENTER"
    assert "regime_sizing" not in result
