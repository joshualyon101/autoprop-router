from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any
from models import TradeSignal

_EVENT_MAP = {
    "ENTRY": "ENTRY", "ENTRY_FILL": "ENTRY", "ENTRY_SIGNAL": "ENTRY",
    "TP1_FILL": "PARTIAL_EXIT", "TP1_TOUCH": "PARTIAL_EXIT",
    "PARTIAL_EXIT": "PARTIAL_EXIT",
    "TP2": "EXIT", "RUNNER_TP2_FILL": "EXIT", "RUNNER_STOP_FILL": "EXIT",
    "STOP_FILL": "EXIT", "STOP": "EXIT", "RUNNER_STOP": "EXIT", "EXIT": "EXIT",
    "SESSION_EXIT": "FLATTEN", "REQUIRED_FLAT_EXIT": "FLATTEN", "FRIDAY_FLAT": "FLATTEN", "FLATTEN": "FLATTEN",
    "MOVE_STOP": "STOP_MOVE", "STOP_MOVE": "STOP_MOVE", "STOP_MOVE_BE": "STOP_MOVE", "STOP_MOVE_HALF_RISK": "STOP_MOVE",
}


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).lower() in {"na","null","none","-"}: return None
    try: return float(v)
    except Exception: return None


def _i(v: Any) -> int | None:
    if v is None or v == "": return None
    try: return int(round(float(v)))
    except Exception: return None


def _trade_id(engine: str, event: str, payload: dict[str, Any]) -> str:
    explicit = payload.get("trade_id") or payload.get("tradeId") or payload.get("id")
    if explicit: return str(explicit)[:160]
    # ENTRY gets a time/geometry id. Lifecycle exits can still find the one active
    # allocation per account/instrument, so they do not depend on this generated id.
    basis = f"{engine}|{event}|{payload.get('side') or payload.get('direction')}|{payload.get('price') or payload.get('entry')}|{payload.get('emitted_at_ms') or int(time.time()*1000)}"
    return "AP-" + hashlib.sha256(basis.encode()).hexdigest()[:24]


def normalize_dict(d: dict[str, Any]) -> TradeSignal:
    raw_event = str(d.get("event") or d.get("command") or "").upper().strip()
    event = _EVENT_MAP.get(raw_event, raw_event)
    if event not in {"ENTRY","PARTIAL_EXIT","EXIT","FLATTEN","STOP_MOVE"}:
        raise ValueError(f"unsupported/unrecognized TradingView event: {raw_event or '<missing>'}")
    engine = str(d.get("engine") or d.get("source") or d.get("module") or "FUSION")
    side = d.get("direction") or d.get("side")
    source_qty = _i(d.get("source_qty", d.get("qty")))
    tp1_qty = _i(d.get("tp1_qty"))
    runner_qty = _i(d.get("runner_qty"))
    exit_qty = _i(d.get("exit_qty"))
    if event == "PARTIAL_EXIT" and exit_qty is None:
        exit_qty = tp1_qty
    payload = dict(d)
    trade_id = _trade_id(engine, event, payload)
    event_id = d.get("event_id") or d.get("eventId")
    entry = _f(d.get("entry", d.get("price") if event == "ENTRY" else None))
    stop = _f(d.get("stop", d.get("sl")))
    tp1 = _f(d.get("tp1", d.get("target")))
    tp2 = _f(d.get("tp2"))
    return TradeSignal(
        event_id=str(event_id) if event_id else None,
        trade_id=trade_id, event=event, engine=engine, direction=str(side).upper() if side else None,
        instrument=str(d.get("instrument") or d.get("symbol") or "MNQ1!"),
        execution_symbol=d.get("execution_symbol"), entry=entry, stop=stop, tp1=tp1, tp2=tp2,
        contract_risk_dollars=_f(d.get("contract_risk_dollars") or d.get("contract_risk")),
        source_qty=source_qty, tp1_qty=tp1_qty, runner_qty=runner_qty, exit_qty=exit_qty,
        exit_reason=str(d.get("exit_reason") or raw_event)[:80],
        emitted_at_ms=_i(d.get("emitted_at_ms") or d.get("timestamp_ms")),
        breakeven_after_tp1=bool(d.get("breakeven_after_tp1", False)),
        breakeven_offset_ticks=_i(d.get("breakeven_offset_ticks")) or 0,
    )


def normalize_pipe(text: str) -> TradeSignal:
    parts = [p.strip() for p in text.strip().split("|") if p.strip()]
    if len(parts) < 3:
        raise ValueError("not a recognized AutoProp pipe alert")
    up = [p.upper() for p in parts]
    engine = "FUSION"
    if "AUTOPROP_ICT_FUSION" in up:
        i = up.index("AUTOPROP_ICT_FUSION")
        if i + 1 < len(parts): engine = parts[i+1]
    # Find first known event token.
    raw_event = next((p for p in up if p in _EVENT_MAP), "")
    if not raw_event:
        # Research prefixes sometimes contain REQUIRED_FLAT_EXIT at end.
        raw_event = next((k for k in _EVENT_MAP if any(k in p for p in up)), "")
    event = _EVENT_MAP.get(raw_event)
    if not event: raise ValueError("AutoProp pipe alert has no recognized executable event")
    kv = {}
    for p in parts:
        if "=" in p:
            k,v = p.split("=",1); kv[k.strip().upper()] = v.strip()
    side = next((p for p in up if p in {"LONG","SHORT","BUY","SELL"}), None)
    d = {
        "event": event, "engine": engine, "direction": side,
        "instrument": kv.get("SYMBOL") or "MNQ1!",
        "entry": kv.get("ENTRY"), "stop": kv.get("SL"),
        "tp1": kv.get("TP1") or kv.get("TP"), "tp2": kv.get("TP2"),
        "qty": kv.get("QTY") or kv.get("Q"), "tp1_qty": kv.get("TP1_QTY"),
        "runner_qty": kv.get("RUNNER_QTY"), "exit_reason": raw_event,
    }
    return normalize_dict(d)


def normalize_payload(raw: bytes, content_type: str | None = None) -> TradeSignal:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text: raise ValueError("empty webhook body")
    if "json" in (content_type or "").lower() or text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(obj, dict): raise ValueError("JSON webhook body must be an object")
        return normalize_dict(obj)
    if "|" in text:
        return normalize_pipe(text)
    raise ValueError("plain-English TradingView alerts are intentionally rejected for live routing; use structured JSON or AutoProp pipe format")
