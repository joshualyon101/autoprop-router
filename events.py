from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from models import CanonicalPlan, MarketPulse
from dwc_protection import validate_dwc_fields


@dataclass(frozen=True)
class ParsedEvent:
    kind: str
    plan: CanonicalPlan | None = None
    pulse: MarketPulse | None = None
    engine: str = ""
    side: str = ""
    fields: dict[str, str] | None = None
    raw: str = ""


def _fields(parts: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k] = v
    return out


def _f(d: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        val = d.get(key)
        if val not in (None, "", "-", "na", "NaN"):
            return float(val)
    return None


def _i(d: dict[str, str], *keys: str) -> int | None:
    v = _f(d, *keys)
    return int(v) if v is not None else None


def stable_event_id(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_alert(raw: str) -> ParsedEvent:
    raw = raw.strip()
    if not raw:
        raise ValueError("empty TradingView message")
    parts = raw.split("|")

    if raw in {"AUTOPROP_ICT_FUSION|CROSS_DAY_EXIT", "AUTOPROP_ICT_FUSION|REQUIRED_FLAT_EXIT"}:
        return ParsedEvent(kind="HARD_FLAT", engine="GLOBAL", raw=raw)

    # RC2 generic completed-native-5m market state; deliberately trade-ID independent.
    if parts[:2] == ["AUTOPROP_ICT_FUSION", "MARKET_PULSE"]:
        d = _fields(parts[2:])
        required = ["T5", "TC5", "B5", "O", "H", "L", "C", "ATR"]
        if any(k not in d for k in required):
            raise ValueError("MARKET_PULSE missing required fields")
        pulse = MarketPulse(native_time_ms=int(d["T5"]), native_close_time_ms=int(d["TC5"]),
                            native_bar_index=int(d["B5"]), open=float(d["O"]),
                            high=float(d["H"]), low=float(d["L"]), close=float(d["C"]),
                            atr=float(d["ATR"]), runner_trail_low=_f(d, "RTL"),
                            runner_trail_high=_f(d, "RTH"))
        return ParsedEvent(kind="MARKET_PULSE", pulse=pulse, fields=d, raw=raw)

    # Locked compact Core D34 message, enriched by RC2 with E/SL/TP1/TP2.
    if parts[0] == "D34":
        d = _fields(parts[1:])
        side = d.get("SIDE", "")
        if not side:
            # The source entry ID determines side in TradingView, but the compact string did
            # not carry it. RC2 patch adds SIDE; fail closed if an unpatched D34 arrives.
            raise ValueError("Core D34 entry requires RC2 SIDE field")
        entry, stop, tp1, tp2 = _f(d, "E"), _f(d, "SL"), _f(d, "TP1"), _f(d, "TP2")
        if None in (entry, stop, tp1, tp2):
            raise ValueError("Core D34 entry requires RC2 absolute geometry")
        plan = CanonicalPlan(event_id=stable_event_id(raw), engine="CORE", side=side,
                             entry=entry, stop=stop, tp1=tp1, tp2=tp2,
                             module=d.get("MOD", "CORE"), source=d.get("SRC", ""),
                             score=int(d.get("SC", "0")), native_time_ms=_i(d, "T5"),
                             native_bar_index=_i(d, "B5", "HB"), source_qty=_i(d, "Q"))
        return ParsedEvent(kind="ENTRY", plan=plan, engine="CORE", side=side, fields=d, raw=raw)

    if parts[0] != "AUTOPROP_ICT_FUSION":
        raise ValueError("unknown alert prefix")
    if len(parts) < 3:
        raise ValueError("malformed AutoProp alert")

    engine, action = parts[1], parts[2]
    side = parts[3] if len(parts) > 3 and parts[3] in {"LONG", "SHORT"} else ""
    d = _fields(parts[4:] if side else parts[3:])

    if engine == "ASW" and action == "WORKING_LIMIT":
        if d.get("CONTRACT") != "ASW_LIMIT_V1":
            raise ValueError("ASW WORKING_LIMIT requires CONTRACT=ASW_LIMIT_V1")
        if str(d.get("ORDER_TYPE", "")).upper() != "LIMIT":
            raise ValueError("ASW WORKING_LIMIT requires ORDER_TYPE=LIMIT")
        entry, stop, target = _f(d, "ENTRY"), _f(d, "SL"), _f(d, "TP")
        native_qty, rpc = _i(d, "Q0"), _f(d, "CR")
        signal_time, expiry = _i(d, "T5"), _i(d, "EXP")
        if None in (entry, stop, target, native_qty, rpc, signal_time, expiry) or not side:
            raise ValueError("ASW WORKING_LIMIT missing exact native plan fields")
        plan = CanonicalPlan(
            event_id=stable_event_id(raw), engine="ASW", side=side,
            entry=entry, stop=stop, tp1=target,
            source_qty=native_qty, contract_risk_dollars=rpc,
            native_time_ms=signal_time, expiry_time_ms=expiry,
            contract_version="ASW_LIMIT_V1",
        )
        return ParsedEvent(kind="ASW_WORKING_LIMIT", plan=plan, engine=engine, side=side, fields=d, raw=raw)

    if engine == "ASW" and action == "CANCEL_PENDING":
        if d.get("CONTRACT") != "ASW_LIMIT_V1" or _i(d, "T5") is None or not side:
            raise ValueError("ASW CANCEL_PENDING missing contract/side/T5")
        return ParsedEvent(kind="ASW_CANCEL_PENDING", engine=engine, side=side, fields=d, raw=raw)

    if engine == "ASW" and action == "TIME_FLAT":
        if d.get("CONTRACT") != "ASW_LIMIT_V1" or not side:
            raise ValueError("ASW TIME_FLAT missing contract/side")
        return ParsedEvent(kind="ASW_TIME_FLAT", engine=engine, side=side, fields=d, raw=raw)

    if engine == "DWC" and (action in {"MOVE_STOP", "STOP_MOVE"} or
                            (action == "ENTRY" and ("CONTRACT" in d or "DWC_ID" in d))):
        keys = [p.split("=", 1)[0] for p in parts[4:] if "=" in p]
        if len(keys) != len(set(keys)):
            raise ValueError("DWC duplicated contract field")
        validate_dwc_fields(d, side, move=action != "ENTRY")
        if action != "ENTRY":
            return ParsedEvent(kind="DWC_STOP_MOVE", engine="DWC", side=side, fields=d, raw=raw)

    if action == "ENTRY" and engine in {"ORG", "SILVER", "TGIF", "DWC"}:
        entry = _f(d, "ENTRY", "REF")
        stop = _f(d, "SL")
        target = _f(d, "TP")
        if None in (entry, stop, target) or not side:
            raise ValueError(f"{engine} ENTRY missing exact geometry")
        org_regime_fields: dict[str, str] = {}
        if engine == "ORG" and any(
            key.startswith("RG_") or key == "Q_BASE" for key in d
        ):
            relevant = {"QTY", "Q", "Q_BASE", "B5"}
            org_regime_fields = {
                key: value for key, value in d.items()
                if key.startswith("RG_") or key in relevant
            }
            seen: set[str] = set()
            for part in parts[4:] if side else parts[3:]:
                if "=" not in part:
                    continue
                key = part.split("=", 1)[0]
                if key.startswith("RG_") or key in relevant:
                    if key in seen:
                        org_regime_fields["__DUPLICATE_FIELD__"] = key
                    seen.add(key)
        plan = CanonicalPlan(event_id=stable_event_id(raw), engine=engine, side=side,
                             entry=entry, stop=stop, tp1=target,
                             reentry=d.get("REENTRY") == "1",
                             reentry_type=d.get("TYPE", ""), source_qty=_i(d, "QTY", "Q"),
                             org_regime_fields=org_regime_fields,
                             native_time_ms=_i(d, "DWC_ID") if engine == "DWC" else None,
                             contract_version=d.get("CONTRACT", "") if engine == "DWC" else "")
        return ParsedEvent(kind="ENTRY", plan=plan, engine=engine, side=side, fields=d, raw=raw)

    if engine == "SILVER" and action in {"MOVE_STOP", "STOP_MOVE"}:
        if d.get("CONTRACT") != "SILVER_SB35_STAGE_V1":
            raise ValueError("Silver MOVE_STOP requires CONTRACT=SILVER_SB35_STAGE_V1")
        stage = _i(d, "STAGE")
        lock_r = _f(d, "LOCK_R")
        trigger_r = _f(d, "TRIGGER_R")
        expected = {1: (2.0, 0.25), 2: (3.0, 0.50)}
        if stage not in expected:
            raise ValueError("Silver MOVE_STOP stage must be 1 or 2")
        exp_trigger, exp_lock = expected[stage]
        if trigger_r is None or abs(trigger_r - exp_trigger) > 1e-9:
            raise ValueError("Silver MOVE_STOP trigger R does not match staged contract")
        if lock_r is None or abs(lock_r - exp_lock) > 1e-9:
            raise ValueError("Silver MOVE_STOP lock R does not match staged contract")
        return ParsedEvent(kind="SILVER_STOP_MOVE", engine=engine, side=side, fields=d, raw=raw)
    if (engine == "ACCOUNT" and action in {"CHALLENGE_MLL_FAIL_EXIT", "FUNDED_MLL_FAIL_EXIT"}) or action in {"TIME_EXIT", "FAILSAFE_EXIT", "REQUIRED_FLAT_EXIT", "FRIDAY_FLAT", "REQUIRED_FLAT"}:
        return ParsedEvent(kind="HARD_FLAT", engine=engine, side=side, fields=d, raw=raw)
    if action in {"EXIT", "FLATTEN"}:
        return ParsedEvent(kind="EXIT", engine=engine, side=side, fields=d, raw=raw)
    if engine == "ACCOUNT" or action == "ACCOUNT":
        return ParsedEvent(kind="ACCOUNT", engine=engine, side=side, fields=d, raw=raw)
    return ParsedEvent(kind=action, engine=engine, side=side, fields=d, raw=raw)
