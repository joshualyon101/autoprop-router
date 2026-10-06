from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re

from models import AccountRule, AccountState, Allocation, CanonicalPlan
from parity import (
    MNQ_POINT_VALUE, challenge_effective_target, challenge_planning_ceiling,
    challenge_risk, funded_postlock_risk, funded_prelock_risk,
    consistency_qty, consistency_target, core_consistency_qty, core_consistency_targets,
    core_risk_multiplier, addon_risk_multiplier, engine_contract_cap, natural_qty,
    risk_per_contract, core_split, consistency_room, funded_one_contract_max_risk,
)


class AllocationBlocked(RuntimeError):
    pass


def validate_org_regime(plan: CanonicalPlan) -> bool:
    """Validate the configured Pine source's frozen ORG decision; return its half flag.

    Legacy alerts without metadata keep their existing sizing. This validates reported
    observations, not an independently fetched index feed. Pine emits B5 at eight decimal
    places, so a transmitted boundary reading cannot reconstruct the strict comparison;
    preserve the reported decision there after checking source-quantity arithmetic.
    Shadow parsing deliberately retains malformed metadata for observation.
    """
    fields = plan.org_regime_fields
    if plan.engine != "ORG" or not fields:
        return False
    required = {"RG_VER", "RG_POLICY", "RG_EN", "RG_HALF", "Q_BASE", "B5", "QTY"}
    if set(fields) != required:
        raise AllocationBlocked("ORG regime metadata incomplete, duplicated, or unknown")
    if (fields["RG_VER"] != "EW5_B50" or fields["RG_POLICY"] != "ALWAYS_ON"
            or fields["RG_EN"] != "1" or fields["RG_HALF"] not in {"0", "1"}):
        raise AllocationBlocked("ORG regime contract/policy/flags invalid")
    if any(re.fullmatch(r"[0-9]+", fields[key]) is None for key in {"Q_BASE", "QTY"}):
        raise AllocationBlocked("ORG regime source quantities must be positive integers")
    base_qty, source_qty = int(fields["Q_BASE"]), int(fields["QTY"])
    half = fields["RG_HALF"] == "1"
    expected_qty = max(1, base_qty // 2) if half else base_qty
    if (base_qty < 1 or source_qty < 1 or plan.source_qty != source_qty
            or source_qty != expected_qty):
        raise AllocationBlocked("ORG regime source-quantity mismatch")
    reading = fields["B5"]
    if reading in {"NaN", "na"}:
        if half:
            raise AllocationBlocked("ORG regime half flag requires available breadth data")
        return False
    try:
        breadth = float(reading)
    except (TypeError, ValueError) as exc:
        raise AllocationBlocked("ORG regime breadth invalid") from exc
    if not math.isfinite(breadth):
        raise AllocationBlocked("ORG regime breadth invalid")
    boundary = math.isclose(breadth, 0.50, rel_tol=0.0, abs_tol=5.001e-9)
    if not boundary and half != (breadth > 0.50):
        raise AllocationBlocked("ORG regime breadth/half mismatch")
    return half


def org_regime_quantity(qty: int, half: bool) -> int:
    """Halve a valid destination quantity after its existing caps/consistency checks."""
    return max(1, qty // 2) if half and qty > 0 else qty


def personal_eod_risk_basis(rule: AccountRule, state: AccountState) -> float:
    """Return the completed New York EOD balance used for Personal percent sizing.

    closed_cash_balance is the current confirmed cash balance. realized_today is the
    Router's verified New York-calendar-day realized P&L (including reported fees).
    Subtracting today's realized P&L freezes the risk basis at the prior completed EOD
    instead of resizing after each intraday winner/loss.

    virtual_eod applies the same cumulative realized account change to a configured
    reference starting balance. Deposits/withdrawals are cash flows, not trading P&L,
    so the broker anchor must be updated when external cash is added/removed.
    """
    actual_eod = state.closed_cash_balance - state.realized_today
    if rule.personal_balance_mode == "virtual_eod":
        basis = rule.personal_virtual_start_balance + (
            actual_eod - rule.personal_broker_anchor_balance
        )
    else:
        basis = actual_eod
    if basis <= 0:
        raise AllocationBlocked("personal EOD risk basis <= 0")
    return basis


def _state_fresh(state: AccountState, max_age_seconds: float, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    ts = state.state_timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() <= max_age_seconds


def _base_risk(rule: AccountRule, state: AccountState) -> tuple[float, float, float, bool, float]:
    """Return base risk, cushion, effective target, consistency enabled, planning ceiling."""
    if rule.account_type in {"challenge", "funded"}:
        if state.mll_floor is None or not state.mll_verified:
            raise AllocationBlocked("MLL floor missing/unverified")
        cushion = max(0.0, state.closed_cash_balance - state.mll_floor)
    else:
        cushion = 0.0

    if rule.account_type == "challenge":
        eff_target = challenge_effective_target(
            rule.challenge_target, state.largest_winning_day,
            rule.challenge_consistency_pct / 100.0,
            rule.challenge_consistency_enabled,
        )
        cycle_closed = state.closed_cash_balance - rule.starting_balance
        progress = max(0.0, cycle_closed / eff_target * 100.0) if eff_target > 0 else 0.0
        base = challenge_risk(cushion=cushion, max_loss=rule.max_loss,
                              drawdown=rule.drawdown, profile=rule.profile,
                              progress_pct=progress).final
        ceiling = challenge_planning_ceiling(rule.challenge_consistency_pct, rule.profile,
                                             rule.challenge_consistency_enabled)
        return base, cushion, eff_target, rule.challenge_consistency_enabled, ceiling

    if rule.account_type == "funded":
        base = (funded_postlock_risk(cushion=cushion, max_loss=rule.max_loss, profile=rule.profile)
                if state.funded_locked else
                funded_prelock_risk(cushion=cushion, max_loss=rule.max_loss, profile=rule.profile))
        consistency = rule.funded_consistency_enabled and rule.funded_daily_profit_cap > 0
        return base, cushion, rule.funded_daily_profit_cap, consistency, 1.0

    if rule.personal_risk_method == "percent":
        base = personal_eod_risk_basis(rule, state) * rule.personal_risk_value / 100.0
    else:
        base = rule.personal_risk_value
    if rule.profile == "aggressive":
        # Locked v1.1.1 Personal / Real Money profile uses aggressionMult = 1.20.
        # Funded pre-lock is the separate 1.10 multiplier implemented in parity.py.
        base *= 1.20
    return base, 0.0, 0.0, False, 1.0


def allocate(plan: CanonicalPlan, rule: AccountRule, state: AccountState,
             *, max_state_age_seconds: float = 20.0, now: datetime | None = None) -> Allocation:
    org_half = validate_org_regime(plan)
    if not rule.enabled:
        raise AllocationBlocked("account disabled")
    if not rule.rules_verified:
        raise AllocationBlocked("account rules not verified")
    if not _state_fresh(state, max_state_age_seconds, now):
        raise AllocationBlocked("account state stale")
    if not state.daily_ledger_verified:
        raise AllocationBlocked("New York daily realized-P&L ledger unverified")

    base_risk, cushion, consistency_base_target, consistency_enabled, ceiling = _base_risk(rule, state)
    if base_risk <= 0:
        raise AllocationBlocked("no active risk budget")

    engine = plan.engine
    risk_mult = 1.0
    if engine == "CORE":
        if plan.module in {"LOL_ADD", "PDL_ADD"}:
            risk_mult = addon_risk_multiplier(module=plan.module, score=plan.score,
                                              account_type=rule.account_type)
        else:
            risk_mult = core_risk_multiplier(source=plan.source, score=plan.score,
                                             side=plan.side, account_type=rule.account_type)
    budget = base_risk * risk_mult
    rpc = risk_per_contract(engine=engine, entry=plan.entry, stop=plan.stop)
    cap = engine_contract_cap(engine=engine, account_cap=rule.max_contracts,
                              account_type=rule.account_type,
                              funded_locked=state.funded_locked,
                              module=plan.module, score=plan.score)
    q0 = natural_qty(risk_budget=budget, risk_pc=rpc, cap=cap,
                     account_type=rule.account_type,
                     funded_cushion=cushion if rule.account_type == "funded" else None,
                     funded_max_loss=rule.max_loss if rule.account_type == "funded" else None,
                     funded_locked=state.funded_locked,
                     core_funded_buffer=(engine == "CORE" and rule.account_type == "funded"))
    if q0 < 1:
        raise AllocationBlocked("nearest-contract sizing returned zero")

    if engine == "CORE":
        tp2 = plan.tp2 if plan.tp2 is not None else plan.tp1
        tp1_pc = abs(plan.tp1 - plan.entry) * MNQ_POINT_VALUE
        tp2_pc = abs(tp2 - plan.entry) * MNQ_POINT_VALUE
        q = core_consistency_qty(pre_qty=q0, tp1_profit_per_contract=tp1_pc,
                                 tp2_profit_per_contract=tp2_pc, min_runner_qty=2,
                                 realized_today=state.realized_today,
                                 enabled=consistency_enabled, ceiling_fraction=ceiling,
                                 base_target=consistency_base_target)
        if q < 1:
            raise AllocationBlocked("daily profit room <= $50")
        out_tp1, out_tp2 = core_consistency_targets(entry=plan.entry, natural_tp1=plan.tp1,
                                                    natural_tp2=tp2, qty=q, min_runner_qty=2,
                                                    realized_today=state.realized_today,
                                                    enabled=consistency_enabled,
                                                    ceiling_fraction=ceiling,
                                                    base_target=consistency_base_target)
        first, runner = core_split(q)
        return Allocation(account_id=rule.account_id, event_id=plan.event_id, engine=engine,
                          side=plan.side, qty=q, entry=plan.entry, stop=plan.stop,
                          tp1=out_tp1, tp2=out_tp2, tp1_qty=first, runner_qty=runner,
                          base_risk=base_risk, effective_risk_budget=budget,
                          risk_per_contract=rpc)

    projected_pc = abs(plan.tp1 - plan.entry) * MNQ_POINT_VALUE
    q = consistency_qty(pre_qty=q0, projected_profit_per_contract=projected_pc,
                        realized_today=state.realized_today, enabled=consistency_enabled,
                        ceiling_fraction=ceiling, base_target=consistency_base_target)
    if q < 1:
        raise AllocationBlocked("daily profit room <= $50")
    q = org_regime_quantity(q, org_half)
    target = consistency_target(entry=plan.entry, natural_target=plan.tp1, qty=q,
                                realized_today=state.realized_today,
                                enabled=consistency_enabled, ceiling_fraction=ceiling,
                                base_target=consistency_base_target)
    return Allocation(account_id=rule.account_id, event_id=plan.event_id, engine=engine,
                      side=plan.side, qty=q, entry=plan.entry, stop=plan.stop, tp1=target,
                      tp2=None, tp1_qty=q, runner_qty=0, base_risk=base_risk,
                      effective_risk_budget=budget, risk_per_contract=rpc)


def allocate_asw(plan: CanonicalPlan, rule: AccountRule, state: AccountState,
                 *, max_state_age_seconds: float = 20.0, now: datetime | None = None) -> Allocation:
    """ASW_LIMIT_V1 destination allocation.

    Challenge: exact native source plan or skip.
    Funded: maximum safe whole native-plan multiple (1x, 2x, 3x...).
    Personal: exact native source plan in this coordinated release.
    Target geometry is never compressed.
    """
    if plan.engine != "ASW":
        raise AllocationBlocked("allocate_asw requires ASW plan")
    if not rule.enabled:
        raise AllocationBlocked("account disabled")
    if not rule.rules_verified:
        raise AllocationBlocked("account rules not verified")
    if not _state_fresh(state, max_state_age_seconds, now):
        raise AllocationBlocked("account state stale")
    if not state.daily_ledger_verified:
        raise AllocationBlocked("New York daily realized-P&L ledger unverified")

    base_risk, cushion, consistency_base_target, consistency_enabled, ceiling = _base_risk(rule, state)
    if base_risk <= 0:
        raise AllocationBlocked("no active risk budget")

    native_qty = int(plan.source_qty or 0)
    if native_qty < 1:
        raise AllocationBlocked("ASW native quantity missing")
    rpc = risk_per_contract(engine="ASW", entry=plan.entry, stop=plan.stop)
    supplied_rpc = float(plan.contract_risk_dollars or 0.0)
    if supplied_rpc <= 0 or abs(supplied_rpc - rpc) > 0.011:
        raise AllocationBlocked(f"ASW contract-risk mismatch Pine={supplied_rpc:.2f} Router={rpc:.2f}")

    cap = int(rule.max_contracts)
    native_risk = native_qty * rpc
    profit_pc = abs(plan.tp1 - plan.entry) * MNQ_POINT_VALUE
    native_profit = native_qty * profit_pc
    room = consistency_room(realized_today=state.realized_today, enabled=consistency_enabled,
                            ceiling_fraction=ceiling, base_target=consistency_base_target)

    if rule.account_type == "challenge":
        qty = native_qty
        if qty > cap:
            raise AllocationBlocked("ASW exact native plan exceeds contract cap")
        if native_risk > base_risk + 0.0001:
            raise AllocationBlocked("ASW exact native plan exceeds Challenge risk budget")
        if consistency_enabled and (room <= 50.0 or native_profit > room + 0.0001):
            raise AllocationBlocked("ASW exact native plan does not fit Challenge consistency room")

    elif rule.account_type == "funded":
        units_cap = cap // native_qty
        units_risk = int((base_risk + 0.0001) // native_risk) if native_risk > 0 else 0
        units_consistency = (int((room + 0.0001) // native_profit)
                             if consistency_enabled and native_profit > 0 else 10**9)
        units = max(0, min(units_cap, units_risk, units_consistency))
        qty = native_qty * units
        if qty < 1:
            raise AllocationBlocked("ASW funded whole-plan multiple returned zero")
        survival_cap = funded_one_contract_max_risk(cushion=cushion, max_loss=rule.max_loss,
                                                     locked=state.funded_locked)
        if survival_cap is not None and not (qty == 1 and rpc <= survival_cap + 0.0001):
            raise AllocationBlocked("ASW funded low-cushion survival gate")
        if consistency_enabled and (room <= 50.0 or qty * profit_pc > room + 0.0001):
            raise AllocationBlocked("ASW funded whole-plan multiple exceeds consistency room")

    else:
        qty = native_qty
        if qty > cap:
            raise AllocationBlocked("ASW native Personal plan exceeds contract cap")

    return Allocation(account_id=rule.account_id, event_id=plan.event_id, engine="ASW",
                      side=plan.side, qty=qty, entry=plan.entry, stop=plan.stop,
                      tp1=plan.tp1, tp2=None, tp1_qty=qty, runner_qty=0,
                      base_risk=base_risk, effective_risk_budget=base_risk,
                      risk_per_contract=rpc)
