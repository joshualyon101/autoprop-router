from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from events import ParsedEvent
from models import AccountRule


_ACTION_BY_KIND = {
    "ENTRY": "WOULD_ENTER",
    "ASW_WORKING_LIMIT": "WOULD_PLACE_LIMIT",
    "ASW_CANCEL_PENDING": "WOULD_CANCEL_PENDING_LIMITS",
    "ASW_TIME_FLAT": "WOULD_TIME_FLAT",
    "MARKET_PULSE": "WOULD_PROCESS_MARKET_PULSE",
    "SILVER_STOP_MOVE": "WOULD_MOVE_STOPS",
    "DWC_STOP_MOVE": "WOULD_MOVE_DWC_STOPS",
    "HARD_FLAT": "WOULD_HARD_FLAT",
    "EXIT": "WOULD_OBSERVE_EXIT",
    "ACCOUNT": "WOULD_OBSERVE_ACCOUNT_STATE",
}


def _model_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return None


def _fallback_action(kind: str) -> str:
    token = re.sub(r"[^A-Z0-9]+", "_", str(kind).strip().upper()).strip("_")
    return f"WOULD_OBSERVE_{token or 'UNKNOWN'}"


def _org_regime_sizing(event: ParsedEvent) -> dict[str, Any] | None:
    """Audit Pine's source quantity only; never derive an account order quantity."""
    fields = event.fields or {}
    if event.kind != "ENTRY" or event.engine != "ORG" or fields.get("RG_VER") != "EW5_B50":
        return None
    result: dict[str, Any] = {
        "rule": "EW5_B50",
        "enabled": fields.get("RG_EN") == "1",
        "flagged": fields.get("RG_HALF") == "1",
        "source_qty": event.plan.source_qty if event.plan else None,
        "base_qty": None,
        "breadth_5_pp": None,
        "expected_source_qty": None,
        "policy": fields.get("RG_POLICY", "OPTIONAL"),
        "data_status": "UNKNOWN",
        "quantity_verification": "UNVERIFIABLE",
        "condition_verification": "UNVERIFIABLE",
        "verification": "UNVERIFIABLE",
    }
    try:
        if fields.get("RG_EN") not in {"0", "1"} or fields.get("RG_HALF") not in {"0", "1"}:
            return result
        base_qty = int(fields["Q_BASE"])
        if base_qty < 1 or result["source_qty"] is None:
            return result
        result["base_qty"] = base_qty
        # Check arithmetic against the reported decision independently of the
        # serialized market reading. This is not per-account destination sizing.
        expected_qty = max(1, base_qty // 2) if result["flagged"] else base_qty
        result["expected_source_qty"] = expected_qty
        result["quantity_verification"] = (
            "MATCH" if result["source_qty"] == expected_qty
            else "MISMATCH"
        )
        policy_valid = result["policy"] in {"OPTIONAL", "ALWAYS_ON"}
        policy_mismatch = result["policy"] == "ALWAYS_ON" and not result["enabled"]
        if policy_mismatch or (not result["enabled"] and result["flagged"]):
            result["condition_verification"] = "MISMATCH"
            result["reason"] = "INCONSISTENT_POLICY_OR_FLAGS"
        elif not policy_valid:
            result["reason"] = "UNKNOWN_POLICY"
        elif "B5" not in fields:
            result["reason"] = "MISSING_BREADTH_FIELD"
        else:
            try:
                breadth = float(fields["B5"])
            except (ValueError, TypeError):
                breadth = float("inf")
            if math.isnan(breadth):
                # Pine deliberately keeps normal size when the index series is
                # unavailable. Quantity can be checked, but breadth cannot.
                result["data_status"] = "UNAVAILABLE"
                result["condition_verification"] = "MISMATCH" if result["flagged"] else "UNVERIFIABLE"
                result["reason"] = "INDEX_DATA_UNAVAILABLE"
            elif not math.isfinite(breadth):
                result["data_status"] = "INVALID"
                result["reason"] = "INVALID_BREADTH_FIELD"
            else:
                result["data_status"] = "AVAILABLE"
                result["breadth_5_pp"] = breadth
                # Pine transmits B5 to eight decimal places. Around +0.50 the
                # original strict comparison cannot always be reconstructed.
                if result["enabled"] and math.isclose(breadth, 0.50, rel_tol=0.0, abs_tol=5.001e-9):
                    result["reason"] = "BREADTH_ROUNDING_BOUNDARY"
                else:
                    expected_flag = bool(result["enabled"] and breadth > 0.50)
                    result["condition_verification"] = "MATCH" if result["flagged"] == expected_flag else "MISMATCH"
        checks = {result["quantity_verification"], result["condition_verification"]}
        result["verification"] = (
            "MISMATCH" if "MISMATCH" in checks else
            "UNVERIFIABLE" if "UNVERIFIABLE" in checks else "MATCH"
        )
    except (ValueError, TypeError, KeyError):
        pass
    return result


class ShadowObserver:
    """Record what an alert means without touching any broker or live ownership state.

    ``accounts_provider`` is evaluated for each event so zero-touch account discovery can
    publish a new immutable registry without rebuilding the observer.  The observer has no
    broker/client/executor dependency by design.  Its only durable writes are the two
    ``shadow_*`` runtime-state documents used for health/diagnostic reporting.
    """

    SUMMARY_KEY = "shadow_observer_summary"
    LAST_EVENT_KEY = "shadow_last_event"

    def __init__(
        self,
        store: Any,
        accounts_provider: Callable[[], Iterable[AccountRule]],
    ) -> None:
        if not callable(accounts_provider):
            raise TypeError("accounts_provider must be callable")
        self.store = store
        self.accounts_provider = accounts_provider
        self._summary_lock = asyncio.Lock()

    def _accounts(self) -> tuple[AccountRule, ...]:
        return tuple(self.accounts_provider())

    async def _record_summary(
        self,
        *,
        event: ParsedEvent,
        event_key: str,
        action: str,
        receipt_epoch: float,
        observed_at_epoch: float,
        intended_account_ids: list[str],
        regime_sizing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        # Both workers can observe alerts concurrently.  Serialize the tiny runtime-state
        # read/modify/write so counters are not lost.  No live attempt/trade/circuit method
        # is called from this class.
        async with self._summary_lock:
            current = self.store.get_runtime_state(self.SUMMARY_KEY) or {}
            counts = dict(current.get("counts") or {})
            counts[event.kind] = int(counts.get(event.kind, 0)) + 1
            total = int(current.get("total_events", 0)) + 1
            last_event = {
                "event_key": event_key,
                "kind": event.kind,
                "engine": event.engine,
                "side": event.side,
                "action": action,
                "receipt_epoch": receipt_epoch,
                "observed_at_epoch": observed_at_epoch,
                "intended_account_ids": list(intended_account_ids),
            }
            if event.plan is not None:
                last_event["plan_event_id"] = event.plan.event_id
            if regime_sizing is not None:
                last_event["regime_sizing"] = regime_sizing
            summary = {
                "mode": "shadow",
                "total_events": total,
                "counts": counts,
                "last_event": last_event,
                "updated_at_epoch": observed_at_epoch,
            }
            self.store.set_runtime_state(self.SUMMARY_KEY, summary)
            self.store.set_runtime_state(self.LAST_EVENT_KEY, last_event)
            return summary

    async def observe(
        self,
        event: ParsedEvent,
        event_key: str = "",
        receipt_epoch: float | None = None,
    ) -> dict[str, Any]:
        """Return and durably count a mutation-free interpretation of ``event``."""
        observed_at = time.time()
        receipt = observed_at if receipt_epoch is None else float(receipt_epoch)
        plan = _model_payload(event.plan)
        pulse = _model_payload(event.pulse)
        key = str(
            event_key
            or (event.plan.event_id if event.plan is not None else "")
        )
        action = _ACTION_BY_KIND.get(event.kind, _fallback_action(event.kind))
        regime_sizing = _org_regime_sizing(event)

        accounts = self._accounts()
        intended = [
            {
                "account_id": rule.account_id,
                "crosstrade_account": rule.crosstrade_account,
            }
            for rule in accounts
            if rule.enabled
        ]
        intended_ids = [row["account_id"] for row in intended]

        result: dict[str, Any] = {
            "kind": event.kind,
            "status": "SHADOW_OBSERVED",
            "action": action,
            "broker_mutation_performed": False,
            "event_key": key,
            "engine": event.engine,
            "side": event.side,
            "receipt_epoch": receipt,
            "observed_at_epoch": observed_at,
            "intended_accounts": intended,
            "intended_account_ids": intended_ids,
            "configured_accounts": len(accounts),
            "enabled_accounts": len(intended),
        }
        if plan is not None:
            # Preserve the complete canonical plan, including exact entry/stop/targets,
            # native timestamps, quantity hints, and ASW expiry/contract fields.
            result["plan"] = plan
        if pulse is not None:
            result["pulse"] = pulse
        if event.fields:
            result["fields"] = dict(event.fields)
        if regime_sizing is not None:
            result["regime_sizing"] = regime_sizing

        summary = await self._record_summary(
            event=event,
            event_key=key,
            action=action,
            receipt_epoch=receipt,
            observed_at_epoch=observed_at,
            intended_account_ids=intended_ids,
            regime_sizing=regime_sizing,
        )
        result["shadow_total_events"] = summary["total_events"]
        return result
