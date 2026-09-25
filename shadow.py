from __future__ import annotations

import asyncio
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

        summary = await self._record_summary(
            event=event,
            event_key=key,
            action=action,
            receipt_epoch=receipt,
            observed_at_epoch=observed_at,
            intended_account_ids=intended_ids,
        )
        result["shadow_total_events"] = summary["total_events"]
        return result
