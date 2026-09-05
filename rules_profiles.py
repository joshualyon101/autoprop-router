
from __future__ import annotations

from models import AccountConfig


def _nearest_size(value: float | None, allowed: list[int]) -> int:
    if value is None or value <= 0:
        return allowed[0]
    return min(allowed, key=lambda x: abs(float(value) - x))


def infer_account_config(
    *,
    account_id: int,
    account_name: str,
    effective_balance: float | None,
    fundednext_default_model: str = "Legacy",
) -> AccountConfig:
    """
    Auto-classify only patterns we can recognize with high confidence.

    Unknown names are still auto-added, but quarantined:
    enabled=False, rules_verified=False, risk_ready=False.
    """
    name = account_name.upper()
    rid = f"CT_{account_id}"

    # MyFundedFutures Pro evaluation names explicitly encode EVPRO.
    if name.startswith("MFFUEVPRO"):
        size = _nearest_size(effective_balance, [50000, 100000, 150000])
        max_loss = {50000: 2000, 100000: 3000, 150000: 4500}[size]
        target = {50000: 3000, 100000: 6000, 150000: 9000}[size]
        micros = {50000: 30, 100000: 60, 150000: 90}[size]
        return AccountConfig(
            id=rid, crosstrade_account_id=account_id, account_name=account_name,
            firm="My Funded Futures", program="Pro", phase="challenge",
            starting_balance=size, max_loss=max_loss, profit_target=target,
            max_micros=micros, drawdown_type="eod_trail_no_prepass_lock",
            consistency_pct=50, risk_profile="Standard",
            enabled=True, rules_verified=True,
            # Existing accounts first seen away from exact starting balance need a
            # one-time MLL bootstrap before sizing. New accounts seen at creation
            # become ready automatically.
            risk_ready=abs((effective_balance or size) - size) <= 25.0,
            auto_discovered=True, profile_source="name:MFFUEVPRO"
        )

    # FundedNext challenge names identify challenge stage, but the account name
    # does not document the purchased model. For this AutoProp farm we default
    # recognized FN challenge accounts to the configured model (Legacy today).
    if name.startswith("FNFTCH") and fundednext_default_model.lower() == "legacy":
        size = _nearest_size(effective_balance, [25000, 50000, 100000])
        max_loss = {25000: 1000, 50000: 2000, 100000: 3000}[size]
        target = {25000: 1250, 50000: 3000, 100000: 6000}[size]
        micros = {25000: 20, 50000: 30, 100000: 50}[size]
        return AccountConfig(
            id=rid, crosstrade_account_id=account_id, account_name=account_name,
            firm="FundedNext", program="Legacy", phase="challenge",
            starting_balance=size, max_loss=max_loss, profit_target=target,
            max_micros=micros, drawdown_type="eod_trail_no_prepass_lock",
            consistency_pct=40, risk_profile="Standard",
            enabled=True, rules_verified=True,
            risk_ready=abs((effective_balance or size) - size) <= 25.0,
            auto_discovered=True, profile_source="name:FNFTCH+default:Legacy"
        )

    # FundedNext funded-stage name. Existing funded accounts must bootstrap MLL
    # once because current balance alone cannot reveal a historical EOD trail.
    if name.startswith("FNFTFA") and fundednext_default_model.lower() == "legacy":
        size = _nearest_size(effective_balance, [25000, 50000, 100000])
        max_loss = {25000: 1000, 50000: 2000, 100000: 3000}[size]
        micros = {25000: 30, 50000: 50, 100000: 70}[size]
        return AccountConfig(
            id=rid, crosstrade_account_id=account_id, account_name=account_name,
            firm="FundedNext", program="Legacy", phase="funded",
            starting_balance=size, max_loss=max_loss, profit_target=0,
            max_micros=micros, drawdown_type="eod_trail_to_lock",
            consistency_pct=None, risk_profile="Standard",
            enabled=True, rules_verified=True,
            risk_ready=abs((effective_balance or size) - size) <= 25.0,
            auto_discovered=True, profile_source="name:FNFTFA+default:Legacy"
        )

    # Unknown account: discover it, but never guess its prop rules.
    size = _nearest_size(effective_balance, [25000, 50000, 100000, 150000])
    return AccountConfig(
        id=rid, crosstrade_account_id=account_id, account_name=account_name,
        firm="Unknown", program="UNCLASSIFIED", phase="challenge",
        starting_balance=size, max_loss=0, profit_target=0, max_micros=1,
        drawdown_type="static", consistency_pct=None,
        enabled=False, rules_verified=False, risk_ready=False,
        auto_discovered=True, profile_source="unclassified"
    )
