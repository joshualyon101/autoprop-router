"""Bounded, conservative sizing when live account-state reads are transiently unavailable.

This module does not fabricate cash, drawdown, MLL, or daily-ledger facts.  It supplies a
small fixed risk budget derived only from verified account configuration.  The durable
Store separately limits how many entry *waves* may use this path and for how long.
"""
from __future__ import annotations

import math

from allocation import AllocationBlocked, org_regime_quantity, validate_org_regime
from crosstrade import CrossTradeError, RateLimitExceeded
from models import AccountRule, Allocation, CanonicalPlan
from parity import (
    MNQ_POINT_VALUE, addon_risk_multiplier, core_risk_multiplier, core_split,
    engine_contract_cap, risk_per_contract,
)
from state import StateUnverified


_TRANSIENT_MARKERS = (
    "readtimeout", "connecttimeout", "pooltimeout", "write timeout", "timed out",
    "timeout", "network error", "connection reset", "connection refused",
    "temporarily unavailable", "temporary failure", "snapshot_refresh_pending",
    "balance refresh is already in progress", "http 429", "http 500", "http 502",
    "http 503", "http 504", "entry state cache is not initialized",
    "entry state cache stale", "account state stale",
    "daily realized-p&l ledger unverified",
)

_HARD_MARKERS = (
    "http 400", "http 401", "http 403", "invalid_bearer", "unauthorized",
    "forbidden", "account disabled", "rules not verified", "mll floor missing",
    "mll/failure floor missing", "configured crosstrade account is not linked",
    "account not found", "account closed", "account breached",
)


def is_transient_state_failure(error: Exception | str) -> bool:
    """Recognize only state-read failures that are safe to treat as temporary.

    Authentication, configuration, account-status, and verified-risk failures never use
    the fallback path. Unknown errors also fail closed.
    """
    text = str(error).strip().lower()
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if not isinstance(error, str):
        text = f"{type(error).__name__}: {text}".lower()
    if any(marker in text for marker in _HARD_MARKERS):
        return False
    if isinstance(error, RateLimitExceeded):
        return True
    if isinstance(error, StateUnverified):
        return any(marker in text for marker in _TRANSIENT_MARKERS)
    if isinstance(error, CrossTradeError):
        return any(marker in text for marker in _TRANSIENT_MARKERS)
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def fallback_base_risk(rule: AccountRule, settings) -> float:
    """Return the conservative, state-independent account-type risk budget."""
    if rule.account_type == "challenge":
        pct = float(getattr(settings, "STATE_FALLBACK_CHALLENGE_MAX_LOSS_PCT", 10.0))
        return rule.max_loss * max(0.0, pct) / 100.0
    if rule.account_type == "funded":
        pct = float(getattr(settings, "STATE_FALLBACK_FUNDED_MAX_LOSS_PCT", 7.5))
        return rule.max_loss * max(0.0, pct) / 100.0

    multiplier = max(0.0, float(getattr(
        settings, "STATE_FALLBACK_PERSONAL_RISK_MULTIPLIER", 0.5
    )))
    if rule.personal_risk_method == "percent":
        basis = (rule.personal_virtual_start_balance
                 if rule.personal_balance_mode == "virtual_eod"
                 else rule.starting_balance)
        normal = basis * rule.personal_risk_value / 100.0
    else:
        normal = rule.personal_risk_value
    return normal * multiplier


def _fallback_budget(plan: CanonicalPlan, rule: AccountRule, settings) -> tuple[float, float]:
    base = fallback_base_risk(rule, settings)
    if base <= 0 or not math.isfinite(base):
        raise AllocationBlocked("state fallback has no configured risk budget")
    multiplier = 1.0
    if plan.engine == "CORE":
        if plan.module in {"LOL_ADD", "PDL_ADD"}:
            multiplier = addon_risk_multiplier(
                module=plan.module, score=plan.score, account_type=rule.account_type
            )
        else:
            multiplier = core_risk_multiplier(
                source=plan.source, score=plan.score, side=plan.side,
                account_type=rule.account_type,
            )
    # State fallback is a ceiling, never an excuse for an aggressive confidence uplift.
    return base, base * min(1.0, max(0.0, multiplier))


def allocate_state_fallback(plan: CanonicalPlan, rule: AccountRule, settings,
                            *, reason: str) -> Allocation:
    """Allocate without state while keeping actual initial risk at/below the fallback cap."""
    org_half = validate_org_regime(plan)
    if not bool(getattr(settings, "STATE_FALLBACK_ENABLED", True)):
        raise AllocationBlocked("state fallback disabled")
    if not rule.enabled:
        raise AllocationBlocked("account disabled")
    if not rule.rules_verified:
        raise AllocationBlocked("account rules not verified")

    base, budget = _fallback_budget(plan, rule, settings)
    rpc = risk_per_contract(engine=plan.engine, entry=plan.entry, stop=plan.stop)
    cap = engine_contract_cap(
        engine=plan.engine, account_cap=rule.max_contracts,
        account_type=rule.account_type, funded_locked=False,
        module=plan.module, score=plan.score,
    )
    # Floor is intentional. Normal parity sizing uses nearest-contract rounding, but an
    # unknown-state fallback must never exceed its conservative dollar ceiling.
    qty = min(cap, int(math.floor((budget + 1e-9) / rpc))) if rpc > 0 else 0
    if qty < 1:
        raise AllocationBlocked(
            f"state fallback budget ${budget:.2f} is below one-contract risk ${rpc:.2f}"
        )
    qty = org_regime_quantity(qty, org_half)

    if plan.engine == "CORE":
        tp2 = plan.tp2 if plan.tp2 is not None else plan.tp1
        first, runner = core_split(qty)
        return Allocation(
            account_id=rule.account_id, event_id=plan.event_id, engine=plan.engine,
            side=plan.side, qty=qty, entry=plan.entry, stop=plan.stop,
            tp1=plan.tp1, tp2=tp2, tp1_qty=first, runner_qty=runner,
            base_risk=base, effective_risk_budget=budget, risk_per_contract=rpc,
            state_fallback=True, state_fallback_reason=str(reason)[:1000],
        )

    return Allocation(
        account_id=rule.account_id, event_id=plan.event_id, engine=plan.engine,
        side=plan.side, qty=qty, entry=plan.entry, stop=plan.stop,
        tp1=plan.tp1, tp2=None, tp1_qty=qty, runner_qty=0,
        base_risk=base, effective_risk_budget=budget, risk_per_contract=rpc,
        state_fallback=True, state_fallback_reason=str(reason)[:1000],
    )


def allocate_asw_state_fallback(plan: CanonicalPlan, rule: AccountRule, settings,
                                *, reason: str) -> Allocation:
    """Allocate only whole native ASW plans within the conservative fallback budget."""
    if plan.engine != "ASW":
        raise AllocationBlocked("ASW state fallback requires ASW plan")
    if not bool(getattr(settings, "STATE_FALLBACK_ENABLED", True)):
        raise AllocationBlocked("state fallback disabled")
    if not rule.enabled or not rule.rules_verified:
        raise AllocationBlocked("account disabled or rules not verified")
    native_qty = int(plan.source_qty or 0)
    if native_qty < 1:
        raise AllocationBlocked("ASW native quantity missing")

    base, budget = _fallback_budget(plan, rule, settings)
    rpc = risk_per_contract(engine="ASW", entry=plan.entry, stop=plan.stop)
    supplied = float(plan.contract_risk_dollars or 0.0)
    if supplied <= 0 or abs(supplied - rpc) > 0.011:
        raise AllocationBlocked(
            f"ASW contract-risk mismatch Pine={supplied:.2f} Router={rpc:.2f}"
        )
    native_risk = native_qty * rpc
    units = min(rule.max_contracts // native_qty,
                int(math.floor((budget + 1e-9) / native_risk)))
    # Challenge and Personal keep ASW's exact native plan contract. Funded may use a
    # whole-plan multiple, matching the normal ASW path, but never above fallback budget.
    if rule.account_type in {"challenge", "personal"}:
        units = min(units, 1)
    qty = native_qty * max(0, units)
    if qty < 1:
        raise AllocationBlocked(
            f"ASW state fallback budget ${budget:.2f} cannot fund one native plan "
            f"(${native_risk:.2f})"
        )
    return Allocation(
        account_id=rule.account_id, event_id=plan.event_id, engine="ASW",
        side=plan.side, qty=qty, entry=plan.entry, stop=plan.stop,
        tp1=plan.tp1, tp2=None, tp1_qty=qty, runner_qty=0,
        base_risk=base, effective_risk_budget=budget, risk_per_contract=rpc,
        state_fallback=True, state_fallback_reason=str(reason)[:1000],
    )
