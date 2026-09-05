from __future__ import annotations

import math
from dataclasses import dataclass

from models import AccountConfig, AccountRuntime, AccountPhase, DrawdownType, RiskProfile


@dataclass(frozen=True)
class RiskResult:
    budget: float
    quantity: int
    reason: str
    contract_risk: float


def pine_round_positive(x: float) -> int:
    """Pine-style nearest whole contract for non-negative x (0.5 rounds up)."""
    return int(math.floor(max(0.0, x) + 0.5))


def challenge_risk_pct(a: AccountConfig) -> tuple[float, float]:
    aggressive = a.risk_profile == RiskProfile.AGGRESSIVE
    if a.drawdown_type == DrawdownType.STATIC:
        base = 32.50 if aggressive else 15.00
        near_pass = 0.85 if aggressive else 0.75
    elif a.drawdown_type in {DrawdownType.LIVE_TRAIL_TO_LOCK, DrawdownType.LIVE_TRAIL_NO_PREPASS_LOCK}:
        base = 19.75 if aggressive else 18.75
        near_pass = 0.85
    else:
        base = 19.25 if aggressive else 18.75
        near_pass = 0.85
    return base, near_pass


def challenge_budget(a: AccountConfig, s: AccountRuntime) -> float:
    cushion = max(0.0, float(s.cushion or 0.0))
    base_pct, near_pass_mult = challenge_risk_pct(a)
    max_loss_cap_pct = base_pct * 1.30

    raw = cushion * base_pct / 100.0
    cap = a.max_loss * max_loss_cap_pct / 100.0

    balance = s.balance if s.balance is not None else s.net_liq
    closed_pnl = max(0.0, (balance or a.starting_balance) - a.starting_balance)

    # Match Fusion: consistency can raise the effective target when the largest
    # winning day would otherwise exceed the firm's percentage.
    target = a.profit_target
    if a.consistency_pct and target > 0:
        frac = max(0.0001, a.consistency_pct / 100.0)
        target = max(target, s.largest_winning_day / frac)

    progress_pct = (closed_pnl / target * 100.0) if target > 0 else 0.0
    progress_mult = near_pass_mult if progress_pct >= 80.0 else 1.0
    return max(0.0, min(raw, cap) * progress_mult)


def funded_budget(a: AccountConfig, s: AccountRuntime) -> tuple[float, bool, float | None]:
    cushion = max(0.0, float(s.cushion or 0.0))
    cushion_pct = (cushion / a.max_loss * 100.0) if a.max_loss > 0 else 0.0
    aggressive = a.risk_profile == RiskProfile.AGGRESSIVE
    m = 1.10 if aggressive else 1.0

    healthy_pct = 15.00 * m
    mid_pct = 13.75 * m
    low_pct = 12.50 * m
    post_lock_pct = 15.0 if aggressive else 12.5

    pre = a.max_loss * (healthy_pct if cushion_pct >= 75 else mid_pct if cushion_pct >= 50 else low_pct) / 100.0
    raw_post = cushion * post_lock_pct / 100.0
    post_floor = a.max_loss * healthy_pct / 100.0
    budget = max(post_floor, raw_post) if s.mll_locked else pre

    one_contract_mode = (not s.mll_locked) and cushion_pct < 30.0
    one_contract_max_risk = cushion * 0.35 if one_contract_mode else None
    return max(0.0, budget), one_contract_mode, one_contract_max_risk


def contract_risk_from_geometry(entry: float, stop: float, point_value: float, round_trip_cost: float) -> float:
    return abs(entry - stop) * point_value + round_trip_cost


def size_trade(
    a: AccountConfig,
    s: AccountRuntime,
    contract_risk: float,
    *,
    challenge_budget_override: float | None = None,
) -> RiskResult:
    if contract_risk <= 0:
        return RiskResult(0, 0, "invalid contract risk", contract_risk)

    if a.phase == AccountPhase.CHALLENGE:
        budget = challenge_budget_override if challenge_budget_override is not None else challenge_budget(a, s)
        qty = pine_round_positive(budget / contract_risk)
        qty = min(qty, a.max_micros)
        return RiskResult(budget, qty, "challenge current-cushion profile", contract_risk)

    if a.phase == AccountPhase.FUNDED:
        budget, one_contract, one_contract_cap = funded_budget(a, s)
        qty = min(pine_round_positive(budget / contract_risk), a.max_micros)
        if one_contract:
            if one_contract_cap is None or contract_risk > one_contract_cap:
                return RiskResult(budget, 0, "funded low-cushion: 1 contract not structurally safe", contract_risk)
            qty = min(qty, 1)
        return RiskResult(budget, qty, "funded current-cushion profile", contract_risk)

    # Personal accounts are intentionally not enabled by this prop-router build.
    return RiskResult(0, 0, "personal sizing not configured in v0.1", contract_risk)


def consistency_room(a: AccountConfig, s: AccountRuntime, min_room: float = 50.0) -> float | None:
    if a.phase != AccountPhase.CHALLENGE or not a.consistency_pct or a.profit_target <= 0:
        return None
    return max(0.0, a.profit_target * (a.consistency_pct / 100.0) - max(0.0, s.realized_today))


def consistency_adjust(
    *,
    a: AccountConfig,
    s: AccountRuntime,
    pre_qty: int,
    entry: float | None,
    tp1: float | None,
    tp2: float | None,
    point_value: float,
    tick_size: float,
    min_runner_qty: int,
    min_profit_room: float = 50.0,
) -> tuple[int, float | None, float | None, float | None]:
    """
    Port of Fusion f_core_consistency_qty + f_core_consistency_targets.

    Returns qty, routed_tp1, routed_tp2, room.
    """
    room = consistency_room(a, s, min_profit_room)
    if room is None or pre_qty <= 0:
        return pre_qty, tp1, tp2, room
    if room <= min_profit_room:
        return 0, tp1, tp2, room
    if entry is None or tp1 is None:
        return pre_qty, tp1, tp2, room

    tp2_eff = tp2 if tp2 is not None else tp1
    tp1_pc = abs(tp1 - entry) * point_value
    tp2_pc = abs(tp2_eff - entry) * point_value

    best_fit = 0
    for q in range(1, pre_qty + 1):
        runner = math.floor(q / 2.0) if q >= min_runner_qty else 0
        first = q - runner
        projected = first * max(0.0, tp1_pc) + runner * max(0.0, tp2_pc)
        if projected <= room + 0.0001:
            best_fit = q
    qty = best_fit if best_fit >= 1 else 1

    out1, out2 = tp1, tp2_eff
    runner = math.floor(qty / 2.0) if qty >= min_runner_qty else 0
    first = qty - runner
    projected = first * tp1_pc + runner * tp2_pc

    if projected > room and room > min_profit_room:
        long_target = tp1 > entry
        if runner > 0 and room >= qty * tp1_pc:
            runner_room = room - first * tp1_pc
            ticks = math.floor(runner_room / (runner * point_value * tick_size))
            if ticks >= 1:
                out2 = entry + ticks * tick_size if long_target else entry - ticks * tick_size
        else:
            ticks = math.floor(room / (qty * point_value * tick_size))
            if ticks >= 1:
                common = entry + ticks * tick_size if long_target else entry - ticks * tick_size
                out1, out2 = common, common

    return qty, out1, out2, room
