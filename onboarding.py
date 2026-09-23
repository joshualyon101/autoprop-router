"""Fail-closed zero-touch onboarding for verified challenge cohorts.

A newly linked, untouched challenge may inherit the exact rules of an already configured
challenge in the same account-name cohort and at the same starting balance. Known account
identities also have explicit built-in profiles. Unknown or ambiguous rule sets are
discovered but quarantined; merely sharing a CrossTrade connection is never treated as
proof of a prop firm's trading rules.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from models import AccountRule, AccountState, VerifiedRiskState
from state_refresh import durable_fills
from store import Store


_FN_CHALLENGE = re.compile(r"^FNFTCH[A-Z0-9_-]+$", re.IGNORECASE)
_MFFU_PRO_CHALLENGE = re.compile(r"^MFFUEVPRO[A-Z0-9_-]+$", re.IGNORECASE)
_FN_LEGACY_PROFILES: dict[int, dict[str, float | int]] = {
    25_000: {"max_loss": 1_000.0, "target": 1_250.0, "max_contracts": 20},
    50_000: {"max_loss": 2_000.0, "target": 3_000.0, "max_contracts": 30},
    100_000: {"max_loss": 3_000.0, "target": 6_000.0, "max_contracts": 50},
}
_MFFU_PRO_PROFILES: dict[int, dict[str, float | int]] = {
    50_000: {"max_loss": 2_000.0, "target": 3_000.0, "max_contracts": 30},
    100_000: {"max_loss": 3_000.0, "target": 6_000.0, "max_contracts": 60},
    150_000: {"max_loss": 4_500.0, "target": 9_000.0, "max_contracts": 90},
}


class AutoOnboardingRejected(RuntimeError):
    """The account was discovered, but automatic activation could not be proven safe."""


def challenge_cohort(name: str) -> str:
    """Return a stable identity cohort without using the trailing broker account number."""
    normalized = re.sub(r"\s+", "", str(name).strip().upper())
    if _FN_CHALLENGE.fullmatch(normalized):
        return "FUNDEDNEXT_CHALLENGE"
    if _MFFU_PRO_CHALLENGE.fullmatch(normalized):
        return "MFFU_PRO_CHALLENGE"
    return re.sub(r"[_-]*\d+$", "", normalized)


def _rule_fingerprint(rule: AccountRule) -> str:
    payload = rule.model_dump(exclude={"account_id", "crosstrade_account", "notes"})
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _matching_templates(name: str, balance: float, tolerance: float,
                        configured_accounts: list[AccountRule]) -> list[AccountRule]:
    cohort = challenge_cohort(name)
    if not cohort:
        return []
    return [
        rule for rule in configured_accounts
        if rule.account_type == "challenge"
        and rule.enabled and rule.rules_verified
        and challenge_cohort(rule.crosstrade_account) == cohort
        and abs(float(rule.starting_balance) - balance) <= tolerance
    ]


def _data(payload: Any) -> Any:
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def _rows(payload: Any) -> list[dict[str, Any]]:
    value = _data(payload)
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("accounts", "items", "orders", "positions", "data"):
            rows = value.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return [value]
    return []


def linked_account_records(payload: Any) -> list[dict[str, Any]]:
    """Return normalized CrossTrade account identities without inventing missing names."""
    out = []
    for raw in _rows(payload):
        name = str(
            raw.get("name") or raw.get("accountName") or raw.get("account_name") or ""
        ).strip()
        if name:
            out.append({"name": name, "raw": raw})
    return out


def _account_detail(payload: Any) -> dict[str, Any]:
    value = _data(payload)
    if not isinstance(value, dict):
        raise AutoOnboardingRejected("account detail response is not an object")
    account = value.get("account")
    if isinstance(account, dict):
        merged = dict(value)
        merged.update(account)
        return merged
    return value


def _account_numeric_id(list_row: dict[str, Any], detail: dict[str, Any]) -> int:
    candidates = (
        detail.get("id"), detail.get("accountId"), detail.get("account_id"),
        list_row.get("id"), list_row.get("accountId"), list_row.get("account_id"),
    )
    for raw in candidates:
        if raw is None or isinstance(raw, bool):
            continue
        text = str(raw).strip()
        if text.isdigit() and int(text) > 0:
            return int(text)
    raise AutoOnboardingRejected("CrossTrade account numeric id is missing")


def _closed_cash(detail: dict[str, Any]) -> float:
    balance = detail.get("balance")
    candidates: list[Any] = []
    if isinstance(balance, dict):
        candidates.extend((
            balance.get("amount"), balance.get("cashBalance"),
            balance.get("cash_balance"), balance.get("closedCashBalance"),
        ))
    candidates.extend((
        detail.get("cashBalance"), detail.get("cash_balance"),
        detail.get("closedCashBalance"), detail.get("balanceAmount"),
    ))
    for raw in candidates:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    raise AutoOnboardingRejected("CrossTrade closed-cash balance is missing")


def _supported_size(balance: float, tolerance: float,
                    profiles: dict[int, dict[str, float | int]]) -> int:
    matches = [size for size in profiles if abs(balance - size) <= tolerance]
    if len(matches) != 1:
        supported = ", ".join(f"${size:,}" for size in profiles)
        raise AutoOnboardingRejected(
            f"untouched starting balance not proven; expected {supported}, observed ${balance:,.2f}"
        )
    return matches[0]


def _assert_account_active(detail: dict[str, Any]) -> None:
    if detail.get("active") is False or detail.get("enabled") is False:
        raise AutoOnboardingRejected("CrossTrade account is explicitly inactive")
    status = str(detail.get("status") or detail.get("state") or "").strip().lower()
    if status in {"inactive", "disabled", "breached", "closed", "liquidation_only"}:
        raise AutoOnboardingRejected(f"CrossTrade account status is {status}")


def _nonzero_positions(payload: Any) -> list[dict[str, Any]]:
    unsafe = []
    for row in _rows(payload):
        raw = row.get("netPos", row.get("netPosition", row.get("quantity")))
        try:
            qty = float(raw)
        except (TypeError, ValueError) as exc:
            raise AutoOnboardingRejected(
                "position snapshot contains a row without a valid signed quantity"
            ) from exc
        if not math.isfinite(qty):
            raise AutoOnboardingRejected("position snapshot quantity is not finite")
        if qty != 0:
            unsafe.append(row)
    return unsafe


def infer_legacy_challenge(name: str, numeric_id: int, balance: float,
                           *, tolerance: float) -> tuple[AccountRule, VerifiedRiskState,
                                                         AccountState, dict[str, Any]]:
    if not _FN_CHALLENGE.fullmatch(str(name).strip()):
        raise AutoOnboardingRejected("account name is not a FundedNext challenge identity")
    size = _supported_size(balance, tolerance, _FN_LEGACY_PROFILES)
    profile = _FN_LEGACY_PROFILES[size]
    return _build_rule_state(
        name, numeric_id, balance, size=size, profile=profile,
        consistency_enabled=True, consistency_pct=40.0,
        profile_contract="FN_LEGACY_CHALLENGE_V1",
        notes="Built-in verified FundedNext Legacy Challenge profile",
    )


def infer_mffu_pro_challenge(name: str, numeric_id: int, balance: float,
                             *, tolerance: float) -> tuple[AccountRule, VerifiedRiskState,
                                                           AccountState, dict[str, Any]]:
    if not _MFFU_PRO_CHALLENGE.fullmatch(str(name).strip()):
        raise AutoOnboardingRejected("account name is not an MFFU Pro challenge identity")
    size = _supported_size(balance, tolerance, _MFFU_PRO_PROFILES)
    profile = _MFFU_PRO_PROFILES[size]
    return _build_rule_state(
        name, numeric_id, balance, size=size, profile=profile,
        consistency_enabled=True, consistency_pct=50.0,
        profile_contract="MFFU_PRO_CHALLENGE_V1",
        notes="Built-in verified MyFundedFutures Pro Challenge profile",
    )


def _build_rule_state(name: str, numeric_id: int, balance: float, *, size: int,
                      profile: dict[str, float | int], consistency_enabled: bool,
                      consistency_pct: float, profile_contract: str,
                      notes: str) -> tuple[AccountRule, VerifiedRiskState,
                                           AccountState, dict[str, Any]]:
    account_id = f"CT_{numeric_id}"
    now = datetime.now(timezone.utc)
    floor = float(size - float(profile["max_loss"]))
    rule = AccountRule(
        account_id=account_id,
        crosstrade_account=str(name).strip(),
        enabled=True,
        rules_verified=True,
        account_type="challenge",
        profile="standard",
        starting_balance=float(size),
        max_loss=float(profile["max_loss"]),
        max_contracts=int(profile["max_contracts"]),
        drawdown="eod",
        challenge_target=float(profile["target"]),
        challenge_consistency_enabled=consistency_enabled,
        challenge_consistency_pct=consistency_pct,
        notes=f"Zero-touch challenge onboarding; {notes}",
    )
    risk = VerifiedRiskState(
        account_id=account_id,
        mll_floor=floor,
        mll_verified=True,
        funded_locked=False,
        largest_winning_day=0.0,
        ledger_verified=True,
        cycle_start_utc=now,
        verified_at=now,
        source=f"auto_new_challenge:{profile_contract.lower()}",
    )
    state = AccountState(
        account_id=account_id,
        closed_cash_balance=float(balance),
        mll_floor=floor,
        mll_verified=True,
        funded_locked=False,
        realized_today=0.0,
        largest_winning_day=0.0,
        state_timestamp=now,
        daily_ledger_verified=True,
        source="zero-touch onboarding: exact starting cash + empty durable fill history",
    )
    evidence = {
        "profile_contract": profile_contract,
        "rule_source": "built_in_verified_profile",
        "challenge_cohort": challenge_cohort(name),
        "account_id": account_id,
        "crosstrade_account": rule.crosstrade_account,
        "observed_closed_cash": float(balance),
        "inferred_starting_balance": size,
        "initial_mll_floor": floor,
        "verified_at": now.isoformat(),
    }
    return rule, risk, state, evidence


def infer_from_verified_cohort(name: str, numeric_id: int, balance: float,
                               configured_accounts: list[AccountRule], *,
                               tolerance: float) -> tuple[AccountRule, VerifiedRiskState,
                                                          AccountState, dict[str, Any]]:
    """Clone one unique verified challenge rule at the exact same account size."""
    templates = _matching_templates(name, balance, tolerance, configured_accounts)
    if not templates:
        raise AutoOnboardingRejected(
            "no verified challenge template matches this account family and starting balance"
        )
    by_fingerprint = {_rule_fingerprint(rule): rule for rule in templates}
    if len(by_fingerprint) != 1:
        raise AutoOnboardingRejected(
            "matching challenge cohort has conflicting verified rule profiles"
        )
    template = next(iter(by_fingerprint.values()))
    rule = AccountRule.model_validate({
        **template.model_dump(),
        "account_id": f"CT_{numeric_id}",
        "crosstrade_account": str(name).strip(),
        "notes": (
            "Zero-touch challenge onboarding; exact verified cohort inheritance from "
            + ",".join(sorted(rule.account_id for rule in templates))
        ),
    })
    now = datetime.now(timezone.utc)
    floor = float(rule.starting_balance - rule.max_loss)
    risk = VerifiedRiskState(
        account_id=rule.account_id, mll_floor=floor, mll_verified=True,
        funded_locked=False, largest_winning_day=0.0, ledger_verified=True,
        cycle_start_utc=now, verified_at=now,
        source="auto_new_challenge:verified_cohort_inheritance_v1",
    )
    state = AccountState(
        account_id=rule.account_id, closed_cash_balance=float(balance),
        mll_floor=floor, mll_verified=True, funded_locked=False,
        realized_today=0.0, largest_winning_day=0.0, state_timestamp=now,
        daily_ledger_verified=True,
        source="zero-touch onboarding: verified cohort + exact starting cash + empty history",
    )
    evidence = {
        "profile_contract": "VERIFIED_CHALLENGE_COHORT_V1",
        "rule_source": "verified_configured_cohort",
        "source_account_ids": sorted(item.account_id for item in templates),
        "challenge_cohort": challenge_cohort(name),
        "account_id": rule.account_id,
        "crosstrade_account": rule.crosstrade_account,
        "observed_closed_cash": float(balance),
        "inferred_starting_balance": float(rule.starting_balance),
        "initial_mll_floor": floor,
        "verified_at": now.isoformat(),
    }
    return rule, risk, state, evidence


def infer_challenge(name: str, numeric_id: int, balance: float,
                    configured_accounts: list[AccountRule], settings, *,
                    tolerance: float) -> tuple[AccountRule, VerifiedRiskState,
                                               AccountState, dict[str, Any]]:
    """Prefer existing verified cohort rules, then explicit built-in identities."""
    try:
        return infer_from_verified_cohort(
            name, numeric_id, balance, configured_accounts, tolerance=tolerance,
        )
    except AutoOnboardingRejected as cohort_error:
        if "conflicting" in str(cohort_error):
            raise

    if _FN_CHALLENGE.fullmatch(str(name).strip()):
        model = str(getattr(settings, "FUNDEDNEXT_DEFAULT_MODEL", "")).strip().lower()
        if model != "legacy":
            raise AutoOnboardingRejected(
                "FundedNext challenge model is not encoded by CrossTrade and the configured "
                "default is not Legacy"
            )
        if not bool(getattr(
            settings, "AUTO_ONBOARD_FUNDEDNEXT_LEGACY_CHALLENGES", True
        )):
            raise AutoOnboardingRejected("FundedNext Legacy built-in profile is disabled")
        return infer_legacy_challenge(name, numeric_id, balance, tolerance=tolerance)
    if _MFFU_PRO_CHALLENGE.fullmatch(str(name).strip()):
        return infer_mffu_pro_challenge(name, numeric_id, balance, tolerance=tolerance)
    raise AutoOnboardingRejected(
        "no verified challenge cohort or built-in profile matches this account"
    )


async def discover_and_onboard(settings, client, store: Store,
                               configured_accounts: list[AccountRule],
                               *, publish_guard=None) -> dict[str, Any]:
    """Discover and atomically activate every provably new supported challenge."""
    if not bool(getattr(settings, "AUTO_DISCOVERY", False)):
        return {"enabled": False, "onboarded": [], "quarantined": [], "existing": []}
    if not bool(getattr(settings, "AUTO_ONBOARD_VERIFIED_CHALLENGE_COHORTS", True)):
        return {
            "enabled": False, "reason": "zero-touch challenge onboarding disabled",
            "onboarded": [], "quarantined": [], "existing": [],
        }

    linked = linked_account_records(await client.list_accounts())
    configured_names = {rule.crosstrade_account for rule in configured_accounts}
    configured_ids = {rule.account_id for rule in configured_accounts}
    tolerance = max(0.0, float(getattr(
        settings, "AUTO_ONBOARD_BALANCE_TOLERANCE", 1.0
    )))
    lookback_days = max(30, int(getattr(
        settings, "AUTO_ONBOARD_FILL_LOOKBACK_DAYS", 35
    )))
    now = datetime.now(timezone.utc)
    out: dict[str, Any] = {
        "enabled": True,
        "fundednext_default_model": str(getattr(
            settings, "FUNDEDNEXT_DEFAULT_MODEL", ""
        )).strip() or "unset",
        "onboarded": [], "quarantined": [], "existing": [],
    }
    template_cohorts = {
        challenge_cohort(rule.crosstrade_account)
        for rule in configured_accounts
        if rule.account_type == "challenge" and rule.enabled and rule.rules_verified
    }

    for record in linked:
        name = record["name"]
        if name in configured_names:
            out["existing"].append(name)
            continue
        cohort = challenge_cohort(name)
        known_identity = bool(
            _FN_CHALLENGE.fullmatch(name) or _MFFU_PRO_CHALLENGE.fullmatch(name)
        )
        if not known_identity and cohort not in template_cohorts:
            reason = (
                "unsupported account identity with no verified challenge cohort; "
                "automatic trading disabled"
            )
            store.save_auto_discovery_audit(
                name, status="QUARANTINED", reason=reason,
                payload={"name": name, "challenge_cohort": cohort},
            )
            out["quarantined"].append({"account": name, "reason": reason})
            continue

        try:
            detail_payload = await client.get_account(name)
            detail = _account_detail(detail_payload)
            _assert_account_active(detail)
            numeric_id = _account_numeric_id(record["raw"], detail)
            account_id = f"CT_{numeric_id}"
            if account_id in configured_ids:
                raise AutoOnboardingRejected(
                    "CrossTrade numeric id collides with an existing configured account"
                )
            balance = _closed_cash(detail)
            rule, risk, state, evidence = infer_challenge(
                name, numeric_id, balance, configured_accounts, settings,
                tolerance=tolerance,
            )

            positions_payload, orders_payload, fills = await asyncio.gather(
                client.positions(name),
                client.orders(name),
                durable_fills(
                    client, name, now - timedelta(days=lookback_days),
                    now + timedelta(seconds=1),
                ),
            )
            positions = _nonzero_positions(positions_payload)
            orders = _rows(orders_payload)
            if positions:
                raise AutoOnboardingRejected(
                    f"untouched account proof failed: {len(positions)} open position(s)"
                )
            if orders:
                raise AutoOnboardingRejected(
                    f"untouched account proof failed: {len(orders)} working order(s)"
                )
            if fills:
                raise AutoOnboardingRejected(
                    f"untouched account proof failed: {len(fills)} historical fill(s)"
                )
            evidence.update({
                "fill_lookback_days": lookback_days,
                "fills_observed": 0,
                "open_positions_observed": 0,
                "working_orders_observed": 0,
            })
            if publish_guard is not None and not bool(publish_guard()):
                raise AutoOnboardingRejected(
                    "entry wave active; automatic activation deferred until the next scan"
                )
            inserted = store.commit_auto_onboarded_account(rule, risk, state, evidence)
            configured_names.add(name)
            configured_ids.add(account_id)
            out["onboarded"].append({
                "account_id": account_id,
                "crosstrade_account": name,
                "starting_balance": rule.starting_balance,
                "status": "ONBOARDED" if inserted else "ALREADY_ONBOARDED",
            })
        except Exception as exc:
            reason = str(exc)[:2000]
            store.save_auto_discovery_audit(
                name, status="QUARANTINED", reason=reason,
                payload={"name": name, "checked_at": now.isoformat()},
            )
            out["quarantined"].append({"account": name, "reason": reason})

    return out
