import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from allocation import AllocationBlocked, allocate, org_regime_quantity, validate_org_regime
from events import parse_alert
from execution import ExecutionReceipt
from live import LiveRouter
from models import AccountRule, AccountState, CanonicalPlan
from state_fallback import allocate_state_fallback
from state import StateUnverified
from store import Store


NOW = datetime.now(timezone.utc)
FALLBACK = SimpleNamespace(
    STATE_FALLBACK_ENABLED=True,
    STATE_FALLBACK_CHALLENGE_MAX_LOSS_PCT=10.0,
    STATE_FALLBACK_FUNDED_MAX_LOSS_PCT=7.5,
    STATE_FALLBACK_PERSONAL_RISK_MULTIPLIER=0.5,
)


def alert(*, half=True, breadth="0.75", base=8, qty=None, metadata=True):
    qty = (max(1, base // 2) if half else base) if qty is None else qty
    raw = f"AUTOPROP_ICT_FUSION|ORG|ENTRY|LONG|QTY={qty}|ENTRY=100|SL=90|TP=120"
    if metadata:
        raw += (f"|RG_VER=EW5_B50|RG_POLICY=ALWAYS_ON|RG_EN=1|B5={breadth}"
                f"|RG_HALF={int(half)}|Q_BASE={base}")
    return raw


def rule(account_type="personal", **updates):
    values = dict(
        account_id="A", crosstrade_account="broker-A", enabled=True,
        rules_verified=True, account_type=account_type, starting_balance=50_000,
        max_loss=0 if account_type == "personal" else 2_000,
        max_contracts=50, challenge_target=3_000,
        personal_risk_method="fixed", personal_risk_value=250,
    )
    values.update(updates)
    return AccountRule(**values)


def state(**updates):
    values = dict(
        account_id="A", closed_cash_balance=50_000, mll_floor=48_000,
        mll_verified=True, daily_ledger_verified=True, state_timestamp=NOW,
        funded_locked=False, realized_today=0,
    )
    values.update(updates)
    return AccountState(**values)


def allocated(plan, account, current=None):
    return allocate(plan, account, current or state(), now=NOW)


@pytest.mark.parametrize("account_type,locked,expected", [
    ("personal", False, 4), ("challenge", False, 4),
    ("funded", False, 4), ("funded", True, 6),
])
def test_destination_half_is_after_account_sizing_and_caps(account_type, locked, expected):
    plan = parse_alert(alert()).plan
    result = allocated(plan, rule(account_type), state(funded_locked=locked))
    assert result.qty == expected
    assert result.tp1_qty == expected and result.runner_qty == 0
    assert result.qty != plan.source_qty or expected == 4


@pytest.mark.parametrize("cap,expected", [(1, 1), (2, 1), (3, 1), (5, 2), (7, 3)])
def test_odd_caps_round_down_and_valid_one_stays_one(cap, expected):
    result = allocated(parse_alert(alert()).plan, rule(max_contracts=cap))
    assert result.qty == expected


def test_half_uses_destination_quantity_instead_of_pine_source_quantity():
    plan = parse_alert(alert(base=40)).plan
    result = allocated(plan, rule(max_contracts=5))
    assert plan.source_qty == 20
    assert result.qty == 2


def test_consistency_cap_runs_before_half():
    account = rule("challenge", challenge_consistency_enabled=True)
    # $120 room permits three contracts at $40 profit each, then half rounds to one.
    result = allocated(parse_alert(alert()).plan, account, state(realized_today=1080))
    assert result.qty == 1 and result.tp1 == 120


def test_compressed_target_is_recomputed_from_final_destination_quantity():
    account = rule("challenge", challenge_consistency_enabled=True)
    # One valid contract remains one: its natural $400 target is compressed to $120.
    plan = parse_alert(alert().replace("TP=120", "TP=300")).plan
    result = allocated(plan, account, state(realized_today=1080))
    assert result.qty == 1 and result.tp1 == 160


@pytest.mark.parametrize("base,half,breadth", [(8, False, "0.20"), (8, False, "NaN")])
def test_normal_and_unavailable_breadth_keep_existing_account_size(base, half, breadth):
    plan = parse_alert(alert(base=base, half=half, breadth=breadth)).plan
    assert allocated(plan, rule()).qty == 8


def test_legacy_alert_without_regime_metadata_remains_unchanged():
    plan = parse_alert(alert(metadata=False)).plan
    assert plan.org_regime_fields == {}
    assert allocated(plan, rule()).qty == 8


@pytest.mark.parametrize("half", [True, False])
def test_encoded_rounding_boundary_preserves_frozen_flag(half):
    plan = parse_alert(alert(half=half, breadth="0.5")).plan
    assert validate_org_regime(plan) is half
    assert allocated(plan, rule()).qty == (4 if half else 8)


@pytest.mark.parametrize("breadth,half", [("0.50000001", True), ("0.49999999", False)])
def test_unambiguous_readings_enforce_strict_threshold(breadth, half):
    assert validate_org_regime(parse_alert(alert(half=half, breadth=breadth)).plan) is half


@pytest.mark.parametrize("transform", [
    lambda raw: raw.replace("RG_VER=EW5_B50", "RG_VER=OTHER"),
    lambda raw: raw.replace("RG_POLICY=ALWAYS_ON", "RG_POLICY=OPTIONAL"),
    lambda raw: raw.replace("RG_EN=1", "RG_EN=0"),
    lambda raw: raw.replace("RG_HALF=1", "RG_HALF=2"),
    lambda raw: raw.replace("|Q_BASE=8", ""),
    lambda raw: raw.replace("|B5=0.75", ""),
    lambda raw: raw.replace("Q_BASE=8", "Q_BASE=8.5"),
    lambda raw: raw.replace("Q_BASE=8", "Q_BASE=0"),
    lambda raw: raw.replace("QTY=4", "QTY=3"),
    lambda raw: raw.replace("QTY=4", "QTY=4.5"),
    lambda raw: raw.replace("B5=0.75", "B5=bad"),
    lambda raw: raw.replace("B5=0.75", "B5=inf"),
    lambda raw: raw.replace("B5=0.75", "B5=NaN"),
    lambda raw: raw.replace("B5=0.75", "B5=0.49999999"),
    lambda raw: raw + "|RG_HALF=1",
    lambda raw: raw + "|QTY=4",
    lambda raw: raw + "|RG_UNKNOWN=1",
])
def test_malformed_metadata_is_observable_but_blocks_both_live_allocators(transform):
    plan = parse_alert(transform(alert())).plan
    assert plan.org_regime_fields
    with pytest.raises(AllocationBlocked, match="ORG regime"):
        allocated(plan, rule())
    with pytest.raises(AllocationBlocked, match="ORG regime"):
        allocate_state_fallback(plan, rule(), FALLBACK, reason="ReadTimeout")


@pytest.mark.parametrize("raw_suffix", ["|RG_HALF=1", "|Q_BASE=8"])
def test_partial_metadata_does_not_silently_restore_full_size(raw_suffix):
    plan = parse_alert(alert(metadata=False) + raw_suffix).plan
    with pytest.raises(AllocationBlocked, match="ORG regime"):
        allocated(plan, rule())


def test_legacy_b5_field_without_regime_markers_remains_unchanged():
    plan = parse_alert(alert(metadata=False) + "|B5=123").plan
    assert plan.org_regime_fields == {}
    assert allocated(plan, rule()).qty == 8


@pytest.mark.parametrize("account_type,expected", [
    ("personal", 2), ("challenge", 4), ("funded", 3),
])
def test_bounded_fallback_also_applies_half_after_conservative_floor(account_type, expected):
    result = allocate_state_fallback(
        parse_alert(alert()).plan, rule(account_type), FALLBACK, reason="ReadTimeout"
    )
    assert result.qty == expected and result.tp1_qty == expected
    assert result.qty * result.risk_per_contract <= result.effective_risk_budget
    assert result.state_fallback


def test_zero_budget_never_becomes_one_contract():
    plan = parse_alert(alert()).plan
    with pytest.raises(AllocationBlocked, match="zero"):
        allocated(plan, rule(personal_risk_value=1))
    with pytest.raises(AllocationBlocked, match="below one-contract"):
        allocate_state_fallback(plan, rule(personal_risk_value=1), FALLBACK, reason="timeout")
    assert org_regime_quantity(0, True) == 0


@pytest.mark.parametrize("engine", ["SILVER", "DWC", "TGIF", "CORE"])
def test_other_engines_keep_existing_sizes_despite_org_metadata(engine):
    plan = parse_alert(alert()).plan.model_copy(update={
        "engine": engine, "tp2": 140 if engine == "CORE" else None,
        "module": "CORE", "source": "ORL", "score": 4,
    })
    plain = plan.model_copy(update={"org_regime_fields": {}})
    assert allocated(plan, rule()) == allocated(plain, rule())
    assert allocate_state_fallback(plan, rule(), FALLBACK, reason="timeout") == (
        allocate_state_fallback(plain, rule(), FALLBACK, reason="timeout")
    )


def test_frozen_half_decision_survives_canonical_json_replay():
    plan = parse_alert(alert()).plan
    restored = CanonicalPlan.model_validate_json(plan.model_dump_json())
    assert restored.org_regime_fields == plan.org_regime_fields
    assert allocated(restored, rule(max_contracts=5)).qty == 2


def live_router(tmp_path, *, fallback=False):
    accounts = [
        rule(account_id="A", max_contracts=8),
        rule(account_id="B", crosstrade_account="broker-B", max_contracts=5),
        rule(account_id="C", crosstrade_account="broker-C", max_contracts=1),
        rule(account_id="D", crosstrade_account="broker-D", enabled=False),
    ]
    router = LiveRouter.__new__(LiveRouter)
    router.accounts = accounts
    router.store = Store(str(tmp_path / "router.sqlite3"))
    router.entry_wave_active = asyncio.Event()
    router.settings = SimpleNamespace(
        **vars(FALLBACK), ENTRY_SIGNAL_MAX_AGE_SECONDS=8.0,
        ENTRY_FANOUT_MAX_CONCURRENCY=16, MAX_STATE_AGE_SECONDS=20,
        STATE_FALLBACK_MAX_ENTRY_WAVES=5, STATE_FALLBACK_MAX_DURATION_SECONDS=3600.0,
    )

    class Executor:
        def __init__(self):
            self.submissions = []

        async def submit_single(self, account, allocation, custom_id, **_kwargs):
            assert account == f"broker-{allocation.account_id}"
            self.submissions.append((allocation.account_id, allocation))
            suffix = allocation.account_id
            return ExecutionReceipt(
                result={"success": True}, parent_order_id=f"p-{suffix}",
                child_order_ids=[f"t-{suffix}", f"s-{suffix}"],
                target_order_ids=[f"t-{suffix}"], stop_order_ids=[f"s-{suffix}"],
            )

        place_single = submit_single

    router.executor = Executor()

    async def snapshot():
        return {account.crosstrade_account: {
            "name": account.crosstrade_account, "positions": [], "workingOrders": [],
        } for account in accounts}

    async def state_for(account):
        if fallback:
            raise StateUnverified("entry state cache stale")
        return state(account_id=account.account_id,
                     state_timestamp=datetime.now(timezone.utc))

    router._entry_snapshot = snapshot
    router.state_for = state_for
    return router


@pytest.mark.asyncio
@pytest.mark.parametrize("side,fallback,expected", [
    ("LONG", False, {"A": 4, "B": 2, "C": 1}),
    ("SHORT", False, {"A": 4, "B": 2, "C": 1}),
    ("LONG", True, {"A": 2, "B": 2, "C": 1}),
    ("SHORT", True, {"A": 2, "B": 2, "C": 1}),
])
async def test_live_half_metadata_reaches_destination_place_and_durable_attempt(
    tmp_path, side, fallback, expected
):
    router = live_router(tmp_path, fallback=fallback)
    raw = alert()
    if side == "SHORT":
        raw = raw.replace("|LONG|", "|SHORT|").replace("SL=90", "SL=110").replace("TP=120", "TP=80")
    result = await router.route_entry(parse_alert(raw), receipt_epoch=time.time())
    assert {account: allocation.qty for account, allocation in router.executor.submissions} == expected, result
    assert {attempt.account_id: attempt.qty for attempt in
            router.store.all_entry_attempts(("ACCEPTED",))} == expected
    assert {row["account_id"]: row["status"] for row in result["results"]} == {
        "A": "ACCEPTED", "B": "ACCEPTED", "C": "ACCEPTED", "D": "SKIP",
    }
    assert all(allocation.side == side for _, allocation in router.executor.submissions)
    if fallback:
        assert result["state_fallback"]["entry_waves_used"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_live_malformed_regime_never_prepares_or_submits_place(tmp_path, fallback):
    router = live_router(tmp_path, fallback=fallback)
    event = parse_alert(alert().replace("B5=0.75", "B5=0.25"))
    result = await router.route_entry(event, receipt_epoch=time.time())
    assert router.executor.submissions == []
    assert router.store.all_entry_attempts() == []
    assert all(row["status"] == "SKIP" for row in result["results"])
    assert all("ORG regime" in row["reason"] for row in result["results"] if row["account_id"] != "D")
    assert router.store.state_fallback_status().get("entry_waves_used", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback,expected_qty", [(False, 4), (True, 2)])
async def test_live_half_reentry_preserves_destination_stop_proof_gate(
    tmp_path, fallback, expected_qty
):
    router = live_router(tmp_path, fallback=fallback)
    for account in router.accounts:
        router.store.save_org_attempt(account.account_id, {
            "stop_order_ids": [f"prior-stop-{account.account_id}"],
        })

    async def history(account):
        # Only A proves a filled prior stop. A target fill and an unfilled stop
        # must not grant destination reentry, regardless of source metadata.
        if account.account_id == "A":
            return [{"orderId": "prior-stop-A", "qty": 4}]
        if account.account_id == "B":
            return [{"orderId": "prior-target-B", "qty": 5}]
        return [{"orderId": "prior-stop-C", "qty": 0}]

    router.org_history = history
    event = parse_alert(alert() + "|REENTRY=1|TYPE=RECLAIM")
    result = await router.route_entry(event, receipt_epoch=time.time())
    assert [(account, allocation.qty) for account, allocation in
            router.executor.submissions] == [("A", expected_qty)], result
    assert [(attempt.account_id, attempt.qty) for attempt in
            router.store.all_entry_attempts(("ACCEPTED",))] == [("A", expected_qty)]
    rows = {row["account_id"]: row for row in result["results"]}
    assert rows["A"]["status"] == "ACCEPTED"
    assert rows["D"]["reason"] == "disabled"
    assert all(rows[account]["status"] == "SKIP" and
               "destination stop outcome not proven" in rows[account]["reason"]
               for account in ("B", "C"))
