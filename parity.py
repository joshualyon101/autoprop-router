"""Literal AutoProp Fusion account/allocation math.

This module intentionally mirrors the frozen Pine semantics. It does not discover
signals. Pine owns opportunity selection/arbitration; the Router only applies the
same account formulas to each destination independently.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Literal, Tuple

MNQ_POINT_VALUE = 2.0
MNQ_TICK_SIZE = 0.25
CONSISTENCY_MIN_PROFIT_ROOM = 50.0
FUNDED_MINIMUM_LOSS_BUFFER_R = 12.0
LOW_CUSHION_ONE_CONTRACT_PCT = 35.0
ORG_MODELED_EXECUTION_COST = 2.90  # 0.95/side + 2 modeled stop-slippage ticks on MNQ.

Profile = Literal["standard", "aggressive"]
Drawdown = Literal["static", "eod", "live"]
Side = Literal["LONG", "SHORT"]
AccountType = Literal["challenge", "funded", "personal"]


def pine_round_positive(x: float) -> int:
    """Pine math.round parity for nonnegative quantities: .5 ties upward."""
    if x < 0:
        raise ValueError("quantity rounding accepts nonnegative values only")
    return int(floor(x + 0.5))


def challenge_base_pct(drawdown: Drawdown, profile: Profile) -> float:
    if drawdown == "static":
        return 0.325 if profile == "aggressive" else 0.15
    if drawdown == "live":
        return 0.1975 if profile == "aggressive" else 0.1875
    if drawdown == "eod":
        return 0.1925 if profile == "aggressive" else 0.1875
    raise ValueError(drawdown)


def challenge_near_pass_mult(drawdown: Drawdown, profile: Profile) -> float:
    return 0.75 if drawdown == "static" and profile == "standard" else 0.85


@dataclass(frozen=True)
class ChallengeRiskResult:
    pct: float
    raw: float
    cap: float
    base: float
    progress_mult: float
    final: float


def challenge_risk(*, cushion: float, max_loss: float, drawdown: Drawdown,
                   profile: Profile, progress_pct: float) -> ChallengeRiskResult:
    pct = challenge_base_pct(drawdown, profile)
    raw = max(0.0, cushion) * pct
    cap = max(0.0, max_loss) * pct * 1.30
    base = max(0.0, min(raw, cap))
    pm = challenge_near_pass_mult(drawdown, profile) if progress_pct >= 80.0 else 1.0
    return ChallengeRiskResult(pct, raw, cap, base, pm, base * pm)


def challenge_effective_target(base_target: float, largest_winning_day: float,
                               actual_consistency_fraction: float,
                               consistency_enabled: bool = True) -> float:
    if not consistency_enabled:
        return base_target
    frac = max(0.0001, actual_consistency_fraction)
    return max(base_target, largest_winning_day / frac)


def challenge_planning_ceiling(actual_pct: float, profile: Profile,
                               enabled: bool = True) -> float:
    if not enabled:
        return 1.0
    if profile == "aggressive" and actual_pct < 50.0:
        return min(50.0, actual_pct + 10.0) / 100.0
    return actual_pct / 100.0


def funded_prelock_risk(*, cushion: float, max_loss: float, profile: Profile) -> float:
    cushion_pct = (cushion / max_loss * 100.0) if max_loss > 0 else 0.0
    pct = 0.15 if cushion_pct >= 75.0 else 0.1375 if cushion_pct >= 50.0 else 0.125
    if profile == "aggressive":
        pct *= 1.10
    return max_loss * pct


def funded_postlock_risk(*, cushion: float, max_loss: float, profile: Profile) -> float:
    healthy_pct = 0.15 * (1.10 if profile == "aggressive" else 1.0)
    scale_pct = 0.15 if profile == "aggressive" else 0.125
    return max(max_loss * healthy_pct, cushion * scale_pct)


def funded_one_contract_max_risk(*, cushion: float, max_loss: float, locked: bool) -> float | None:
    cushion_pct = (cushion / max_loss * 100.0) if max_loss > 0 else 0.0
    if locked or cushion_pct >= 30.0:
        return None
    return cushion * LOW_CUSHION_ONE_CONTRACT_PCT / 100.0


def funded_survival_qty(*, risk_per_contract: float, cushion: float,
                        max_loss: float, locked: bool) -> int | None:
    cap = funded_one_contract_max_risk(cushion=cushion, max_loss=max_loss, locked=locked)
    if cap is None:
        return None
    return 1 if risk_per_contract > 0 and risk_per_contract <= cap else 0


def funded_core_safe_qty(*, cushion: float, risk_per_contract: float) -> int:
    if risk_per_contract <= 0 or cushion <= 0:
        return 0
    return pine_round_positive((cushion / FUNDED_MINIMUM_LOSS_BUFFER_R) / risk_per_contract)


def consistency_room(*, realized_today: float, enabled: bool,
                     ceiling_fraction: float, base_target: float) -> float:
    return max(0.0, base_target * ceiling_fraction - realized_today) if enabled else 1e12


def consistency_qty(*, pre_qty: int, projected_profit_per_contract: float,
                    realized_today: float, enabled: bool,
                    ceiling_fraction: float, base_target: float) -> int:
    result = pre_qty
    if enabled and pre_qty > 0:
        room = consistency_room(realized_today=realized_today, enabled=True,
                                ceiling_fraction=ceiling_fraction, base_target=base_target)
        if room <= CONSISTENCY_MIN_PROFIT_ROOM:
            result = 0
        elif projected_profit_per_contract > 0:
            fit_qty = int(floor(room / projected_profit_per_contract))
            result = min(pre_qty, fit_qty) if fit_qty >= 1 else 1
    return result


def consistency_target(*, entry: float, natural_target: float, qty: int,
                       realized_today: float, enabled: bool,
                       ceiling_fraction: float, base_target: float,
                       point_value: float = MNQ_POINT_VALUE,
                       tick_size: float = MNQ_TICK_SIZE) -> float:
    result = natural_target
    if enabled and qty > 0:
        room = consistency_room(realized_today=realized_today, enabled=True,
                                ceiling_fraction=ceiling_fraction, base_target=base_target)
        natural_profit = abs(natural_target - entry) * point_value * qty
        if room > CONSISTENCY_MIN_PROFIT_ROOM and natural_profit > room:
            ticks = int(floor(room / (qty * point_value * tick_size)))
            if ticks >= 1:
                result = entry + ticks * tick_size if natural_target > entry else entry - ticks * tick_size
    return result


def core_split(qty: int, min_runner_qty: int = 2) -> Tuple[int, int]:
    runner = int(floor(qty / 2.0)) if qty >= min_runner_qty else 0
    return qty - runner, runner


def core_consistency_qty(*, pre_qty: int, tp1_profit_per_contract: float,
                         tp2_profit_per_contract: float, min_runner_qty: int,
                         realized_today: float, enabled: bool,
                         ceiling_fraction: float, base_target: float) -> int:
    result = pre_qty
    if enabled and pre_qty > 0:
        room = consistency_room(realized_today=realized_today, enabled=True,
                                ceiling_fraction=ceiling_fraction, base_target=base_target)
        if room <= CONSISTENCY_MIN_PROFIT_ROOM:
            return 0
        best_fit = 0
        for test_qty in range(1, pre_qty + 1):
            first_qty, runner_qty = core_split(test_qty, min_runner_qty)
            projected = (first_qty * max(0.0, tp1_profit_per_contract)
                         + runner_qty * max(0.0, tp2_profit_per_contract))
            if projected <= room + 0.0001:
                best_fit = test_qty
        result = best_fit if best_fit >= 1 else 1
    return result


def core_consistency_targets(*, entry: float, natural_tp1: float, natural_tp2: float,
                             qty: int, min_runner_qty: int, realized_today: float,
                             enabled: bool, ceiling_fraction: float, base_target: float,
                             point_value: float = MNQ_POINT_VALUE,
                             tick_size: float = MNQ_TICK_SIZE) -> Tuple[float, float]:
    out_tp1, out_tp2 = natural_tp1, natural_tp2
    if enabled and qty > 0:
        room = consistency_room(realized_today=realized_today, enabled=True,
                                ceiling_fraction=ceiling_fraction, base_target=base_target)
        first_qty, runner_qty = core_split(qty, min_runner_qty)
        tp1_pc = abs(natural_tp1 - entry) * point_value
        tp2_pc = abs(natural_tp2 - entry) * point_value
        projected = first_qty * tp1_pc + runner_qty * tp2_pc
        if room > CONSISTENCY_MIN_PROFIT_ROOM and projected > room:
            if runner_qty > 0 and room >= qty * tp1_pc:
                runner_room = room - first_qty * tp1_pc
                runner_ticks = int(floor(runner_room / (runner_qty * point_value * tick_size)))
                if runner_ticks >= 1:
                    out_tp2 = entry + runner_ticks * tick_size if natural_tp2 > entry else entry - runner_ticks * tick_size
            else:
                common = consistency_target(entry=entry, natural_target=natural_tp1, qty=qty,
                                            realized_today=realized_today, enabled=True,
                                            ceiling_fraction=ceiling_fraction, base_target=base_target,
                                            point_value=point_value, tick_size=tick_size)
                out_tp1 = common
                out_tp2 = common
    return out_tp1, out_tp2


def score_risk_multiplier(score: int) -> float:
    return 1.50 if score >= 4 else 1.25 if score == 3 else 1.00 if score == 2 else 0.75


def source_risk_multiplier(source: str) -> float:
    if source in {"PDL", "LOH"}:
        return 1.50
    if source in {"ASH", "NYH"}:
        return 1.00
    if source in {"HPL", "PDH"}:
        return 0.50
    return 1.00


def core_risk_multiplier(*, source: str, score: int, side: Side,
                         account_type: AccountType = "challenge",
                         hpl_core_enabled: bool = True) -> float:
    # Frozen Pine: Personal uses score-only confidence scaling. Prop uses source*score,
    # with HPL/ORL/IBL long and ORH/IBH short score-only exceptions.
    if account_type == "personal":
        return score_risk_multiplier(score)
    primary = ((side == "LONG" and ((source == "HPL" and hpl_core_enabled) or source in {"ORL", "IBL"}))
               or (side == "SHORT" and source in {"ORH", "IBH"}))
    if primary:
        return score_risk_multiplier(score)
    return min(1.50, source_risk_multiplier(source) * score_risk_multiplier(score))


def addon_risk_multiplier(*, module: str, score: int, account_type: AccountType) -> float:
    if account_type == "personal":
        return score_risk_multiplier(score)
    return 1.0 if module == "LOL_ADD" else 0.75


def module_contract_cap(*, module: str, score: int, account_cap: int,
                        account_type: AccountType, funded_locked: bool = False) -> int:
    if module in {"CORE", "SC_FVG"}:
        if account_type == "personal" or (account_type == "funded" and funded_locked):
            return account_cap
        score_cap = 6 if score >= 4 else 5 if score == 3 else 4 if score == 2 else 3
        return min(account_cap, score_cap)
    if module == "LOL_ADD":
        return account_cap if account_type == "funded" and funded_locked else min(account_cap, 4)
    if module == "PDL_ADD":
        return 0
    return account_cap


def engine_contract_cap(*, engine: str, account_cap: int,
                        account_type: AccountType, funded_locked: bool,
                        module: str = "", score: int = 0) -> int:
    engine = engine.upper()
    if engine == "ORG":
        return account_cap if account_type == "funded" and funded_locked else min(account_cap, 8)
    if engine == "SILVER":
        return account_cap if account_type == "funded" and funded_locked else min(account_cap, 30)
    if engine == "CORE":
        return module_contract_cap(module=module or "CORE", score=score,
                                   account_cap=account_cap, account_type=account_type,
                                   funded_locked=funded_locked)
    return account_cap


def risk_per_contract(*, engine: str, entry: float, stop: float,
                      point_value: float = MNQ_POINT_VALUE,
                      org_execution_cost: float = ORG_MODELED_EXECUTION_COST) -> float:
    structural = abs(entry - stop) * point_value
    return structural + org_execution_cost if engine.upper() == "ORG" else structural


def natural_qty(*, risk_budget: float, risk_pc: float, cap: int,
                account_type: AccountType, funded_cushion: float | None = None,
                funded_max_loss: float | None = None, funded_locked: bool = False,
                core_funded_buffer: bool = False) -> int:
    if risk_pc <= 0 or cap <= 0 or risk_budget <= 0:
        return 0
    q = min(cap, pine_round_positive(risk_budget / risk_pc))
    if account_type == "funded":
        if funded_cushion is None or funded_max_loss is None:
            raise ValueError("funded sizing requires cushion and max loss")
        survival = funded_survival_qty(risk_per_contract=risk_pc, cushion=funded_cushion,
                                       max_loss=funded_max_loss, locked=funded_locked)
        if survival is not None:
            q = survival
        if core_funded_buffer:
            q = min(q, funded_core_safe_qty(cushion=funded_cushion, risk_per_contract=risk_pc))
    return max(0, q)
