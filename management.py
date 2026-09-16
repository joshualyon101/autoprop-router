from __future__ import annotations

from dataclasses import dataclass
from models import ActiveTrade, MarketPulse

TICK = 0.25


@dataclass(frozen=True)
class StopDecision:
    new_stop: float | None
    reason: str = ""
    tp1_transition: bool = False


def _tighten(side: str, current: float, candidate: float) -> float:
    return max(current, candidate) if side == "LONG" else min(current, candidate)


def core_on_market_pulse(trade: ActiveTrade, pulse: MarketPulse) -> StopDecision:
    if trade.engine != "CORE":
        return StopDecision(None)
    new_stop = trade.current_stop
    reason = ""
    transitioned = False
    risk = abs(trade.entry - trade.initial_stop)

    # TP1 transition is destination-account specific and based on broker position reduction.
    if (trade.runner_qty > 0 and not trade.tp1_filled
            and trade.previous_position_qty > trade.runner_qty
            and trade.current_position_qty <= trade.runner_qty):
        candidate = trade.entry + TICK if trade.side == "LONG" else trade.entry - TICK
        new_stop = _tighten(trade.side, new_stop, candidate)
        reason = "TP1_FILLED_RUNNER_BE_PLUS_1T"
        transitioned = True

    if not transitioned and not trade.tp1_filled and trade.entry_native_bar_index is not None \
            and pulse.native_bar_index > trade.entry_native_bar_index:
        if trade.side == "LONG":
            trigger75 = trade.entry + 0.75 * (trade.tp1 - trade.entry)
            trigger50 = trade.entry + 0.50 * (trade.tp1 - trade.entry)
            if trade.stop_stage < 2 and pulse.high >= trigger75:
                new_stop = _tighten(trade.side, new_stop, trade.entry)
                reason = "PRE_TP1_75_TO_BE"
            elif trade.stop_stage < 1 and pulse.high >= trigger50:
                new_stop = _tighten(trade.side, new_stop, (trade.entry + trade.initial_stop) / 2.0)
                reason = "PRE_TP1_50_HALF_STOP"
        else:
            trigger75 = trade.entry - 0.75 * (trade.entry - trade.tp1)
            trigger50 = trade.entry - 0.50 * (trade.entry - trade.tp1)
            if trade.stop_stage < 2 and pulse.low <= trigger75:
                new_stop = _tighten(trade.side, new_stop, trade.entry)
                reason = "PRE_TP1_75_TO_BE"
            elif trade.stop_stage < 1 and pulse.low <= trigger50:
                new_stop = _tighten(trade.side, new_stop, (trade.entry + trade.initial_stop) / 2.0)
                reason = "PRE_TP1_50_HALF_STOP"

    effective_tp1_filled = trade.tp1_filled or transitioned
    if effective_tp1_filled and trade.runner_qty > 0:
        if trade.side == "LONG" and pulse.runner_trail_low is not None:
            candidate = pulse.runner_trail_low - 0.10 * pulse.atr
            tightened = _tighten(trade.side, new_stop, candidate)
            if tightened != new_stop:
                new_stop, reason = tightened, "RUNNER_5M_SWING_TRAIL"
        elif trade.side == "SHORT" and pulse.runner_trail_high is not None:
            candidate = pulse.runner_trail_high + 0.10 * pulse.atr
            tightened = _tighten(trade.side, new_stop, candidate)
            if tightened != new_stop:
                new_stop, reason = tightened, "RUNNER_5M_SWING_TRAIL"

    return StopDecision(new_stop if new_stop != trade.current_stop else None, reason, transitioned)


SILVER_STOP_CONTRACT = "SILVER_SB35_STAGE_V1"


def silver_lock_stop(trade: ActiveTrade, stage: int) -> StopDecision:
    """Apply the frozen SB35 staged protection using the destination's actual fill.

    Pine owns the canonical favorable-excursion trigger. The Router receives only the
    stage identity, then computes the destination stop from that account's proven broker
    fill and original structural stop. Stage transitions are monotonic and idempotent.
    """
    if trade.engine != "SILVER":
        return StopDecision(None)
    if stage not in {1, 2}:
        raise ValueError(f"unsupported Silver protection stage {stage}")
    if stage <= int(trade.stop_stage or 0):
        return StopDecision(None, f"SILVER_STAGE_{stage}_ALREADY_APPLIED")
    r = abs(trade.entry - trade.initial_stop)
    lock_r = 0.25 if stage == 1 else 0.50
    candidate = trade.entry + lock_r * r if trade.side == "LONG" else trade.entry - lock_r * r
    tightened = _tighten(trade.side, trade.current_stop, candidate)
    reason = "SILVER_2R_LOCK_0.25R" if stage == 1 else "SILVER_3R_LOCK_0.50R"
    return StopDecision(tightened if tightened != trade.current_stop else None, reason)
