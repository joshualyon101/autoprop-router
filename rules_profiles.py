from __future__ import annotations

from models import AccountConfig


def _nearest_size(value: float | None, allowed: list[int]) -> int:
    if value is None or value <= 0:
        return allowed[0]
    return min(allowed, key=lambda x: abs(float(value) - x))


def _ready_if_new(effective_balance: float | None, size: int) -> bool:
    return abs((effective_balance or size) - size) <= 25.0


def infer_account_config(
    *, account_id: int, account_name: str, effective_balance: float | None,
    fundednext_default_model: str = "Legacy",
) -> AccountConfig:
    """High-confidence auto-classification only. Unknowns are quarantined."""
    name = account_name.upper()
    rid = f"CT_{account_id}"

    # MyFundedFutures Pro evaluation. Current official evaluation: 50/100/150k,
    # 2k/3k/4.5k EOD MLL, 30/60/90 micros, 50% consistency. MFFU EOD MLL
    # locks at starting balance + $100.
    if name.startswith("MFFUEVPRO"):
        size = _nearest_size(effective_balance, [50000, 100000, 150000])
        return AccountConfig(
            id=rid, crosstrade_account_id=account_id, account_name=account_name,
            firm="My Funded Futures", program="Pro", phase="challenge",
            starting_balance=size,
            max_loss={50000: 2000, 100000: 3000, 150000: 4500}[size],
            profit_target={50000: 3000, 100000: 6000, 150000: 9000}[size],
            max_micros={50000: 30, 100000: 60, 150000: 90}[size],
            drawdown_type="eod_trail_to_lock", lock_offset=100,
            consistency_pct=50, risk_profile="Standard",
            enabled=True, rules_verified=True, risk_ready=_ready_if_new(effective_balance, size),
            auto_discovered=True, profile_source="name:MFFUEVPRO"
        )

    # FundedNext Futures Legacy Challenge. Name identifies Challenge phase; model
    # is selected by FUNDEDNEXT_DEFAULT_MODEL because the display name does not.
    if name.startswith("FNFTCH") and fundednext_default_model.lower() == "legacy":
        size = _nearest_size(effective_balance, [25000, 50000, 100000])
        return AccountConfig(
            id=rid, crosstrade_account_id=account_id, account_name=account_name,
            firm="FundedNext", program="Legacy", phase="challenge",
            starting_balance=size,
            max_loss={25000: 1000, 50000: 2000, 100000: 3000}[size],
            profit_target={25000: 1250, 50000: 3000, 100000: 6000}[size],
            max_micros={25000: 20, 50000: 30, 100000: 50}[size],
            drawdown_type="eod_trail_to_lock", lock_offset=0,
            consistency_pct=40, risk_profile="Standard",
            enabled=True, rules_verified=True, risk_ready=_ready_if_new(effective_balance, size),
            auto_discovered=True, profile_source="name:FNFTCH+default:Legacy"
        )

    if name.startswith("FNFTFA") and fundednext_default_model.lower() == "legacy":
        size = _nearest_size(effective_balance, [25000, 50000, 100000])
        return AccountConfig(
            id=rid, crosstrade_account_id=account_id, account_name=account_name,
            firm="FundedNext", program="Legacy", phase="funded",
            starting_balance=size,
            max_loss={25000: 1000, 50000: 2000, 100000: 3000}[size],
            profit_target=0,
            max_micros={25000: 30, 50000: 50, 100000: 70}[size],
            drawdown_type="eod_trail_to_lock", lock_offset=0,
            consistency_pct=None, risk_profile="Standard",
            enabled=True, rules_verified=True, risk_ready=_ready_if_new(effective_balance, size),
            auto_discovered=True, profile_source="name:FNFTFA+default:Legacy"
        )

    size = _nearest_size(effective_balance, [25000, 50000, 100000, 150000])
    return AccountConfig(
        id=rid, crosstrade_account_id=account_id, account_name=account_name,
        firm="Unknown", program="UNCLASSIFIED", phase="challenge",
        starting_balance=size, max_loss=0, profit_target=0, max_micros=1,
        drawdown_type="static", consistency_pct=None,
        enabled=False, rules_verified=False, risk_ready=False,
        auto_discovered=True, profile_source="unclassified"
    )
