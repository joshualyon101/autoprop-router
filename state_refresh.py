"""Exact live account-state refresh.

CrossTrade/Tradovate supplies cash balance and durable fills. The prop firm's actual
current MLL/failure floor is deliberately a separate verified fact because the standard
Tradovate account read does not document it. Missing/unverified prop risk state fails
closed; this module never derives MLL from net liquidation or a nominal account size.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from crosstrade import CrossTradeClient
from models import AccountRule, AccountState, VerifiedRiskState
from state import StateUnverified, extract_closed_cash, realized_by_ny_day, ny_date


def _unwrap_balance(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise StateUnverified("invalid account response")
    bal = data.get("balance")
    if not isinstance(bal, dict):
        raise StateUnverified("Tradovate cash balance snapshot missing")
    return bal


async def durable_fills(client: CrossTradeClient, account: str, start: datetime,
                        end: datetime) -> list[dict[str, Any]]:
    cursor: str | None = None
    out: list[dict[str, Any]] = []
    seen_cursor: set[str] = set()
    while True:
        r = await client.fills_history(account=account,
                                       start=start.astimezone(timezone.utc).isoformat(),
                                       end=end.astimezone(timezone.utc).isoformat(),
                                       cursor=cursor, limit=1000)
        rows = r.get("data") or []
        if not isinstance(rows, list):
            raise StateUnverified("durable fill-history response malformed")
        out.extend(x for x in rows if isinstance(x, dict))
        nxt = r.get("nextCursor")
        if not nxt:
            break
        nxt = str(nxt)
        if nxt in seen_cursor:
            raise StateUnverified("durable fill-history cursor loop")
        seen_cursor.add(nxt); cursor = nxt
    return out


async def refresh_account_state(client: CrossTradeClient, rule: AccountRule,
                                risk: VerifiedRiskState | None,
                                *, now: datetime | None = None) -> AccountState:
    now = now or datetime.now(timezone.utc)
    account = await client.get_account(rule.crosstrade_account)
    cash, cash_ts = extract_closed_cash(_unwrap_balance(account))

    if rule.account_type in {"challenge", "funded"}:
        if risk is None or risk.account_id != rule.account_id or not risk.mll_verified or risk.mll_floor is None:
            raise StateUnverified("prop MLL/failure floor missing or unverified")
        if not risk.ledger_verified:
            raise StateUnverified("durable fill ledger not independently verified")
    elif risk is not None and not risk.ledger_verified:
        raise StateUnverified("durable fill ledger not independently verified")

    # For exact daily accounting the NY calendar date comes from UTC fill timestamps, never
    # Tradovate's session tradeDate. Query enough history to include the NY day boundary.
    today_start = now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
    cycle_start = risk.cycle_start_utc if risk and risk.cycle_start_utc else today_start
    start = min(cycle_start, today_start)
    fills = await durable_fills(client, rule.crosstrade_account, start, now + timedelta(seconds=1))
    pnl = realized_by_ny_day(fills)
    realized_today = pnl.get(ny_date(now), 0.0)
    largest = max([0.0, *(x for x in pnl.values() if x > 0)])
    if risk is not None:
        # A verified externally maintained prior-day maximum may predate the query window.
        largest = max(largest, risk.largest_winning_day)

    return AccountState(account_id=rule.account_id, closed_cash_balance=cash,
                        mll_floor=risk.mll_floor if risk else None,
                        mll_verified=bool(risk and risk.mll_verified),
                        funded_locked=bool(risk and risk.funded_locked),
                        realized_today=realized_today,
                        largest_winning_day=largest,
                        state_timestamp=cash_ts,
                        daily_ledger_verified=bool(risk and risk.ledger_verified),
                        source="CrossTrade cash + durable fills + separately verified risk state")
