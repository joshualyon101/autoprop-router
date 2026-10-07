"""DWC SB25/05 contract and durable, trade-scoped profit protection.

Pine owns the +2.5R trigger. Only the matching DWC generation can be managed.
Broker stop = actual destination fill - 0.5 * (original stop - actual fill).
The existing executor performs in-place, owned-stop changes and exact readback.
No target modification, continuous trail, or autonomous market-data trigger is added.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR
import time
from typing import Any

DWC_STOP_CONTRACT = "DWC_SB25_05_V1"
DWC_TRIGGER_R = 2.50
DWC_LOCK_R = 0.50


def _number(fields: dict[str, str], key: str) -> float:
    try:
        value = float(fields[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"DWC missing/invalid {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"DWC nonfinite {key}")
    return value


def _integer(fields: dict[str, str], key: str) -> int:
    value = _number(fields, key)
    if value != int(value) or value <= 0:
        raise ValueError(f"DWC {key} must be a positive integer")
    return int(value)


def validate_dwc_fields(fields: dict[str, str], side: str, *, move: bool) -> int:
    if side != "SHORT" or fields.get("CONTRACT") != DWC_STOP_CONTRACT:
        raise ValueError("DWC protection requires SHORT and CONTRACT=" + DWC_STOP_CONTRACT)
    signal_id = _integer(fields, "DWC_ID")
    if move:
        if _integer(fields, "STAGE") != 1:
            raise ValueError("DWC supports protection stage 1 only")
        for name, expected in (("TRIGGER_R", DWC_TRIGGER_R), ("LOCK_R", DWC_LOCK_R)):
            if abs(_number(fields, name) - expected) > 1e-9:
                raise ValueError(f"DWC {name} does not match the frozen contract")
        if _integer(fields, "T1") < signal_id:
            raise ValueError("DWC protection time precedes its entry signal")
        _integer(fields, "QTY")  # telemetry, never overrides broker position size
        if _number(fields, "SL") <= 0:
            raise ValueError("DWC SL must be positive")
    else:
        entry, stop, target = (_number(fields, k) for k in ("ENTRY", "SL", "TP"))
        if not 0 < target < entry < stop:
            raise ValueError("DWC entry requires positive short entry/stop/target geometry")
        _integer(fields, "QTY")
    return signal_id


def matches(trade: Any, signal_id: int) -> bool:
    return (getattr(trade, "engine", "") == "DWC"
            and getattr(trade, "side", "") == "SHORT"
            and getattr(trade, "management_contract", "") == DWC_STOP_CONTRACT
            and getattr(trade, "management_signal_time_ms", None) == signal_id)


def dwc_lock_stop(trade: Any) -> float:
    """Raw-price formula, as in the accepted research; cannot loosen a stop."""
    if trade.engine != "DWC" or trade.side != "SHORT":
        raise ValueError("DWC stop cannot manage another engine or direction")
    entry, original, current = map(float, (trade.entry, trade.initial_stop, trade.current_stop))
    if not all(math.isfinite(x) and x > 0 for x in (entry, original, current)):
        raise ValueError("DWC actual-fill risk geometry is invalid")
    risk = original - entry
    if risk <= 0:
        raise ValueError("DWC original stop is not above the actual short fill")
    return min(current, entry - DWC_LOCK_R * risk)


def dwc_broker_stop(trade: Any) -> float:
    """Map the frozen raw-price formula onto MNQ's 0.25 tick, ties upward as in Pine.

    Only the broker command is quantized. The backtest formula is not retuned.
    Never round a stop above a pre-existing tighter short stop.
    """
    tick = Decimal("0.25")
    raw = Decimal(str(dwc_lock_stop(trade)))
    current = Decimal(str(trade.current_stop))
    rounded = (raw / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    if rounded > current:
        rounded = (current / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    if rounded <= 0:
        raise ValueError("DWC protected stop must remain positive")
    return float(rounded)


def blocks_new_entry(store: Any, event: Any) -> bool:
    # A stop event can reach the receiver before its ENTRY. Allow that SAME generation
    # to finish normal admission; do not let its queued protection deadlock its entry.
    plan = getattr(event, "plan", None)
    same_id = (plan.native_time_ms if plan is not None and plan.engine == "DWC"
               and plan.contract_version == DWC_STOP_CONTRACT else None)
    if same_id is not None:
        intent = store.get_dwc_stop_intent(same_id)
        if intent and not intent.get("pending"):
            # The generation was already resolved (possibly before a delayed ENTRY).
            # Never admit fresh exposure after its one-shot protection was retired.
            return True
    return store.dwc_stop_pending(exclude_signal_id=same_id)


async def record_intent(rt: Any, event: Any, event_key: str = "") -> dict:
    fields = event.fields or {}
    signal_id = validate_dwc_fields(fields, event.side, move=True)
    intent = rt.store.record_dwc_stop_intent(signal_id, event_key, fields)
    rt.dwc_management_wakeup.set()
    return {"kind": "DWC_STOP_MOVE", "status": "INTENT_RECORDED" if intent["pending"]
            else "ALREADY_COMPLETED", "signal_id": signal_id, "stage": 1}


def _current(rt: Any, trade: Any) -> bool:
    stored = rt.store.get_trade(trade.account_id)
    if (stored is None or stored.event_id != trade.event_id
            or not matches(stored, trade.management_signal_time_ms)):
        return False
    # Check hard-flat history from the owning entry, not from a possibly delayed stop.
    attempts = rt.store.all_entry_attempts()
    own = next((a for a in attempts if a.account_id == trade.account_id
                and a.event_id == trade.event_id), None)
    if own is not None:
        epoch = float(own.entry_receipt_epoch or own.created_at_epoch)
        if rt._crossed_control_fence("DWC", epoch, epoch) is not None:
            return False
    return True


async def _destination(rt: Any, intent: dict, rule: Any, trade: Any) -> dict:
    """At most one stop-modification attempt per generation/account, restart-safe."""
    from execution import NormalizationSuperseded, NormalizationReadbackPending, _latest_modify_outcome
    sid = int(intent["signal_id"])
    store = rt.store
    account = trade.account_id
    state = store.get_dwc_stop_intent(sid).get("accounts", {}).get(account, {})
    phase = state.get("phase", "")
    if not _current(rt, trade):
        return {"account_id": account, "status": "SUPERSEDED"}
    try:
        signed = await rt.position_qty(rule)
        if not _current(rt, trade):
            return {"account_id": account, "status": "SUPERSEDED"}
        if signed == 0:
            return await rt._finalize_observed_flat_trade(
                rule, trade, status="CLOSED", reason="DWC protection observed flat position")
        if phase == "FLATTEN_SENT":
            # An ambiguous exit is never blindly repeated. Keep quarantine until flat proof.
            return {"account_id": account, "status": "ERROR",
                    "reason": "DWC safety exit awaiting flat proof; no mutation resend"}
        if signed >= 0 or abs(signed) != trade.current_position_qty:
            raise ValueError("DWC broker position side/quantity differs from owned trade")
        desired = dwc_broker_stop(trade)
        if not math.isfinite(trade.tp1) or not 0 < trade.tp1 < desired:
            raise ValueError("DWC protected stop and existing target have invalid bracket geometry")
        if int(trade.stop_stage) >= 1:
            return {"account_id": account, "status": "NO_CHANGE", "stop": trade.current_stop}
        if phase == "MODIFY_RESERVED":
            # A process may have died after CHANGE but before state commit. Read only;
            # never send another CHANGE while its previous outcome is unknown.
            from execution import verify_single_bracket
            from models import Allocation
            rows = await rt.executor.working_orders(rule.crosstrade_account)
            allocation = Allocation(account_id=account, event_id=trade.event_id,
                engine="DWC", side="SHORT", qty=abs(signed), entry=trade.entry,
                stop=float(state["stop"]), tp1=trade.tp1, tp1_qty=abs(signed), runner_qty=0,
                base_risk=0.0, effective_risk_budget=0.0, risk_per_contract=0.0)
            if not verify_single_bracket(rows, allocation,
                    set(trade.stop_order_ids + trade.target_order_ids)):
                raise NormalizationReadbackPending("DWC reserved stop update has no exact broker proof")
            # Exact prices alone are insufficient while a Modify command is pending.
            for row in rows:
                oid = str(row.get("id") or row.get("orderId") or "")
                if oid in trade.stop_order_ids:
                    outcome, detail = _latest_modify_outcome(row)
                    if "commands" in row.get("_lifecycle_unavailable", []) or outcome == "pending":
                        raise NormalizationReadbackPending("DWC Modify lifecycle pending: " + detail)
                    if outcome == "rejected":
                        raise ValueError("DWC Modify rejected: " + detail)
            desired = float(state["stop"])
            stop_ids = list(trade.stop_order_ids)
        else:
            # Reserve before await/mutation so a crash cannot cause a blind duplicate.
            store.mark_dwc_stop_account(sid, account, phase="MODIFY_RESERVED", stop=desired,
                                        reserved_at_epoch=time.time())
            stop_ids = await rt.executor.change_stop_orders(
                rule.crosstrade_account, trade.stop_order_ids, desired, abs(signed),
                still_current=lambda: _current(rt, trade))
        if not _current(rt, trade):
            return {"account_id": account, "status": "SUPERSEDED"}
        updated = trade.model_copy(update={"current_stop": desired, "stop_stage": 1,
                    "stop_order_ids": stop_ids, "previous_position_qty": trade.current_position_qty,
                    "current_position_qty": abs(signed)})
        if not store.save_dwc_trade_if_current(updated):
            return {"account_id": account, "status": "SUPERSEDED"}
        store.mark_dwc_stop_account(sid, account, phase="APPLIED", stop=desired)
        return {"account_id": account, "status": "STOP_MOVED", "stage": 1, "stop": desired}
    except NormalizationSuperseded:
        return {"account_id": account, "status": "SUPERSEDED"}
    except Exception as exc:
        # DWC failure never cancels/rebuilds unrelated positions. Quarantine before an
        # owned safety exit; retain both intent and ownership until flat is proven.
        if not _current(rt, trade):
            return {"account_id": account, "status": "SUPERSEDED"}
        state = store.get_dwc_stop_intent(sid).get("accounts", {}).get(account, {})
        from state_fallback import is_transient_state_failure
        if (state.get("phase") != "FLATTEN_SENT" and
                (isinstance(exc, NormalizationReadbackPending) or is_transient_state_failure(exc))):
            first = float(state.get("first_read_failure_epoch") or time.time())
            timeout = max(1.0, float(getattr(getattr(rt, "settings", None),
                                          "ENTRY_RECONCILE_TIMEOUT_SECONDS", 90.0)))
            if time.time() - first < timeout:
                # Keep the existing broker orders, block new risk through the durable intent,
                # and retry reads. A reserved CHANGE is NEVER resubmitted on this path.
                store.mark_dwc_stop_account(sid, account,
                    phase=state.get("phase") or "READ_RETRY", first_read_failure_epoch=first,
                    error=str(exc)[:2000])
                return {"account_id": account, "status": "READBACK_PENDING", "reason": str(exc)}
        store.trip_entry_circuit(reason=f"{account}: DWC protection unconfirmed: {exc}",
            event_key=intent.get("event_key", ""), outcome="DWC_PROTECTION_UNCONFIRMED")
        if state.get("phase") == "FLATTEN_SENT":
            return {"account_id": account, "status": "ERROR", "reason": str(exc)}
        store.mark_dwc_stop_account(sid, account, phase="FLATTEN_SENT", error=str(exc))
        return await rt._hard_flat_rule_once(rule, trade, f"DWC protection failure: {exc}")


async def service_intents(rt: Any) -> dict:
    """Dedicated one-process loop; normal ENTRY/EXIT workers remain available."""
    async with rt.dwc_management_lock:
        intents = rt.store.pending_dwc_stop_intents()
        if not intents:
            return {"kind": "DWC_STOP_SERVICE", "status": "IDLE"}
        all_results = []
        progressed = False
        for intent in intents:
            if float(intent.get("next_attempt_at_epoch", 0)) > time.time():
                continue
            progressed = True
            sid = int(intent["signal_id"])
            attempts = [a for a in rt.store.all_entry_attempts() if matches(a, sid)]
            unresolved_states = {"PREPARED", "SUBMITTING", "ACCEPTED", "FLATTENING"}
            reconciliation_error = ""
            if any(a.state in unresolved_states for a in attempts):
                try:
                    await rt.reconcile_entry_attempts()
                except Exception as exc:
                    reconciliation_error = str(exc)
            rule_map = {a.account_id: a for a in rt.accounts}
            results = []
            for trade in rt.store.all_trades():
                if not matches(trade, sid):
                    continue
                rule = rule_map.get(trade.account_id)
                if rule is None:
                    results.append({"account_id": trade.account_id, "status": "ERROR",
                        "reason": "owned DWC account is not in current configuration"})
                    continue
                # Disabled-for-entry accounts still need their owned exposure protected.
                results.append(await _destination(rt, intent, rule, trade))
            unresolved = [a for a in rt.store.all_entry_attempts()
                          if matches(a, sid) and a.state in unresolved_states]
            behind = [t for t in rt.store.all_trades()
                      if matches(t, sid) and int(t.stop_stage) < 1]
            errors = [r for r in results if r.get("status") == "ERROR"]
            orphan_grace = (not attempts and not results and
                            time.time() - float(intent["created_at_epoch"]) < 30.0)
            if unresolved or behind or errors or reconciliation_error or orphan_grace:
                delay = min(5.0, 0.5 * 2 ** min(int(intent.get("attempts", 0)), 4))
                rt.store.defer_dwc_stop_intent(sid, delay,
                    f"unresolved={len(unresolved)} behind={len(behind)} errors={len(errors)} "
                    f"orphan_grace={orphan_grace} {reconciliation_error}")
                status = "PENDING"
            else:
                status = "COMPLETED" if attempts or results else "NO_MATCHING_OWNERSHIP"
                rt.store.complete_dwc_stop_intent(sid, status)
            all_results.append({"signal_id": sid, "status": status, "results": results})
        return {"kind": "DWC_STOP_SERVICE", "status": "SERVICED" if progressed else "BACKOFF",
                "results": all_results}
