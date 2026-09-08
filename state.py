from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Iterable

NY = ZoneInfo("America/New_York")


class StateUnverified(RuntimeError):
    pass


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # CrossTrade timestamps may be ms or seconds.
        v = float(value)
        dt = datetime.fromtimestamp(v / 1000.0 if v > 1e12 else v, tz=timezone.utc)
    elif isinstance(value, str):
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    else:
        raise StateUnverified("missing broker timestamp")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extract_closed_cash(balance: dict[str, Any], *, observed_at: datetime | None = None) -> tuple[float, datetime]:
    """Extract confirmed cash balance without silently substituting net liquidation.

    If the broker payload omits its own timestamp, ``observed_at`` may be supplied by
    the caller as the UTC time at which a successful live broker GET completed. This
    preserves the freshness gate without pretending the observation time is a broker
    event timestamp.
    """
    for key in ("amount", "totalCashValue", "cashValue"):
        if balance.get(key) is not None:
            value = float(balance[key])
            break
    else:
        if balance.get("netLiquidation") is not None:
            raise StateUnverified("net liquidation present but closed cash balance unavailable")
        raise StateUnverified("closed cash balance unavailable")
    ts = None
    for key in ("timestamp", "updatedAt", "asOf", "createdAt"):
        if balance.get(key) is not None:
            ts = parse_timestamp(balance[key])
            break
    if ts is None:
        if observed_at is None:
            raise StateUnverified("cash balance timestamp unavailable")
        ts = observed_at.astimezone(timezone.utc) if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    return value, ts


def ny_date(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(NY).date().isoformat()


def _commission_and_fees(fill: dict[str, Any]) -> float:
    total = 0.0
    for key in ("commission", "fees", "fee"):
        v = fill.get(key)
        if isinstance(v, (int, float)):
            total += float(v)
    return total


def realized_by_ny_day(fills: Iterable[dict[str, Any]], *, point_value: float = 2.0,
                       instrument_root: str = "MNQ") -> dict[str, float]:
    """FIFO realized P&L by New York calendar date.

    Exact fail-closed scope: if an executable fill for another instrument is present,
    its point value is unknown to this MNQ-only Router and the ledger is rejected.
    Fees/commissions are attributed on their fill timestamp when provided.
    """
    rows = sorted(list(fills), key=lambda f: parse_timestamp(f.get("timestamp") or f.get("time")))
    # lots are signed qty/price; positive long, negative short.
    lots: deque[list[float]] = deque()
    out: dict[str, float] = {}
    seen_exec: set[str] = set()
    for f in rows:
        exec_id = str(f.get("executionId") or f.get("fillId") or "")
        if exec_id and exec_id in seen_exec:
            continue
        if exec_id:
            seen_exec.add(exec_id)
        root = str(f.get("root") or f.get("instrumentRoot") or f.get("instrument") or "")
        if root and not root.upper().startswith(instrument_root.upper()):
            raise StateUnverified(f"non-{instrument_root} fill in account ledger: {root}")
        ts = parse_timestamp(f.get("timestamp") or f.get("time"))
        day = ny_date(ts)
        out.setdefault(day, 0.0)
        qty = int(float(f.get("qty", 0)))
        price = float(f.get("price", 0))
        if qty <= 0 or price <= 0:
            raise StateUnverified("invalid fill qty/price")
        action = str(f.get("action", "")).lower()
        incoming = qty if action in {"buy", "b", "long"} else -qty if action in {"sell", "s", "short"} else 0
        if incoming == 0:
            raise StateUnverified("unknown fill action")
        remaining = incoming
        while remaining and lots and (lots[0][0] > 0) != (remaining > 0):
            lot_qty, lot_price = lots[0]
            close_qty = min(abs(int(lot_qty)), abs(int(remaining)))
            if lot_qty > 0:  # selling a prior long
                pnl = (price - lot_price) * point_value * close_qty
            else:  # buying back a prior short
                pnl = (lot_price - price) * point_value * close_qty
            out[day] += pnl
            lot_sign = 1 if lot_qty > 0 else -1
            rem_sign = 1 if remaining > 0 else -1
            lot_qty -= lot_sign * close_qty
            remaining -= rem_sign * close_qty
            if abs(lot_qty) < 1e-9:
                lots.popleft()
            else:
                lots[0][0] = lot_qty
        if remaining:
            lots.append([float(remaining), price])
        out[day] -= _commission_and_fees(f)
    return out


def current_ny_realized(fills: Iterable[dict[str, Any]], now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return realized_by_ny_day(fills).get(ny_date(now), 0.0)
