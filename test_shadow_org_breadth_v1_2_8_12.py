from __future__ import annotations

import asyncio
import json

import pytest

from events import parse_alert
from shadow import ShadowObserver
from test_shadow_org_breadth_v1_2_8_11 import ShadowStore, org_alert


def final_alert(qty=4, base=8, spread="0.75", enabled=1, flagged=1):
    return org_alert(qty=qty, base=base, spread=spread, enabled=enabled, flagged=flagged) + "|RG_POLICY=ALWAYS_ON"


async def observe(raw):
    store = ShadowStore()
    result = await ShadowObserver(store, lambda: []).observe(parse_alert(raw), "final-org", 1.0)
    # The audit must remain JSON-safe even with NaN / infinity in a source field.
    json.dumps(result, allow_nan=False)
    assert result["status"] == "SHADOW_OBSERVED"
    assert result["broker_mutation_performed"] is False
    assert set(store.runtime) == {"shadow_observer_summary", "shadow_last_event"}
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected,reason",
    [
        (final_alert(), "MATCH", None),
        (final_alert(qty=3, base=7), "MATCH", None),
        (final_alert(qty=1, base=1), "MATCH", None),
        (final_alert(qty=8, spread="0.49999999", flagged=0), "MATCH", None),
        (final_alert(spread="0.50000001"), "MATCH", None),
        (final_alert(spread="0.5"), "UNVERIFIABLE", "BREADTH_ROUNDING_BOUNDARY"),
        (final_alert(qty=8, spread="0.5", flagged=0), "UNVERIFIABLE", "BREADTH_ROUNDING_BOUNDARY"),
        (final_alert(qty=8, enabled=0, flagged=0), "MISMATCH", "INCONSISTENT_POLICY_OR_FLAGS"),
        (final_alert(qty=8, flagged=0), "MISMATCH", None),
        (final_alert(qty=2), "MISMATCH", None),
        (final_alert(qty=8, spread="NaN", flagged=0), "UNVERIFIABLE", "INDEX_DATA_UNAVAILABLE"),
        (final_alert(spread="NaN"), "MISMATCH", "INDEX_DATA_UNAVAILABLE"),
        (final_alert(spread="inf"), "UNVERIFIABLE", "INVALID_BREADTH_FIELD"),
        (final_alert(spread="garbage"), "UNVERIFIABLE", "INVALID_BREADTH_FIELD"),
        (final_alert().replace("|B5=0.75", ""), "UNVERIFIABLE", "MISSING_BREADTH_FIELD"),
        (final_alert().replace("ALWAYS_ON", "UNKNOWN"), "UNVERIFIABLE", "UNKNOWN_POLICY"),
        (final_alert().replace("Q_BASE=8", "Q_BASE=bad"), "UNVERIFIABLE", None),
        (final_alert().replace("Q_BASE=8", "Q_BASE=0"), "UNVERIFIABLE", None),
        (final_alert().replace("RG_EN=1", "RG_EN=bad"), "UNVERIFIABLE", None),
    ],
)
async def test_final_breadth_contract_and_failure_modes(raw, expected, reason):
    result = await observe(raw)
    check = result["regime_sizing"]
    assert check["verification"] == expected
    if reason:
        assert check["reason"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("kind", ["PRIMARY", "FRESH_FVG", "RECLAIM"])
async def test_final_alert_routes_and_reentries_preserve_actual_quantity(side, kind):
    raw = final_alert(qty=3, base=7).replace("|LONG|", f"|{side}|")
    if side == "SHORT":
        raw = raw.replace("SL=20980|TP=21040", "SL=21020|TP=20960")
    if kind != "PRIMARY":
        raw += f"|REENTRY=1|TYPE={kind}"
    if kind == "RECLAIM":
        raw = raw.replace("|ENTRY=21000", "|REF=21000")
    result = await observe(raw)
    assert result["plan"]["source_qty"] == 3
    assert result["regime_sizing"]["expected_source_qty"] == 3
    assert result["regime_sizing"]["verification"] == "MATCH"


@pytest.mark.asyncio
async def test_optional_legacy_contract_still_accepts_disabled_rule():
    result = await observe(org_alert(qty=8, base=8, spread="0.75", enabled=0, flagged=0))
    assert result["regime_sizing"]["policy"] == "OPTIONAL"
    assert result["regime_sizing"]["verification"] == "MATCH"


@pytest.mark.asyncio
async def test_actual_shadow_endpoint_accepts_final_pine_contract(monkeypatch, tmp_path):
    import main
    from test_shadow_mode_v1_2_8_10 import _BodyRequest, _install_shadow_globals, _post_endpoint

    store = _install_shadow_globals(monkeypatch, tmp_path)
    endpoint = _post_endpoint("/webhook/tradingview-shadow/{token}")
    result = await endpoint("secret", _BodyRequest(final_alert()))
    assert result["accepted"] is True
    assert result["broker_mutation_performed"] is False
    worker = asyncio.create_task(main._webhook_worker(control=False))
    try:
        for _ in range(300):
            rows = store.webhook_rows()
            if rows and rows[0]["status"] == "DONE":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("shadow worker did not record the final Pine alert")
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    recorded = store.get_runtime_state(ShadowObserver.LAST_EVENT_KEY)
    assert recorded["regime_sizing"]["verification"] == "MATCH"
    assert main._SHADOW_MODE is True
