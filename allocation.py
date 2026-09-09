from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from models import AccountRule, AccountState, Allocation, CanonicalPlan
from parity import (
    MNQ_POINT_VALUE, challenge_effective_target, challenge_planning_ceiling,
    challenge_risk, funded_postlock_risk, funded_prelock_risk, daily_loss_limit,
    consistency_qty, consistency_target, core_consistency_qty, core_consistency_targets,
    core_risk_multiplier, addon_risk_multiplier, engine_contract_cap, natural_qty,
    risk_per_contract, core_split,
)


class AllocationBlocked(RuntimeError):
    pass


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
    if state.realized_today <= -daily_loss_limit(base_risk):
        raise AllocationBlocked("daily loss limit reached")

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
    target = consistency_target(entry=plan.entry, natural_target=plan.tp1, qty=q,
                                realized_today=state.realized_today,
                                enabled=consistency_enabled, ceiling_fraction=ceiling,
                                base_target=consistency_base_target)
    return Allocation(account_id=rule.account_id, event_id=plan.event_id, engine=engine,
                      side=plan.side, qty=q, entry=plan.entry, stop=plan.stop, tp1=target,
                      tp2=None, tp1_qty=q, runner_qty=0, base_risk=base_risk,
                      effective_risk_budget=budget, risk_per_contract=rpc)
