"""Fail-closed orchestration seam.

RC2 deliberately separates formula/allocation, broker state, execution, and management.
The production webhook uses this service only after state refresh supplied a verified
AccountState for every destination. Missing state skips that account; it never invents
or substitutes a different Fusion setup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from allocation import allocate, AllocationBlocked
from events import ParsedEvent
from models import AccountRule, AccountState, Allocation
from org_reentry import prove_prior_org_stop
from store import Store


@dataclass
class RouteResult:
    account_id: str
    status: str
    reason: str = ""
    allocation: Allocation | None = None


async def fanout_entry(event: ParsedEvent, accounts: list[AccountRule],
                       states: dict[str, AccountState], store: Store,
                       execute: Callable[[AccountRule, Allocation], Awaitable[None]],
                       org_fill_history: Callable[[AccountRule], Awaitable[list[dict]]],
                       max_state_age_seconds: float = 20.0) -> list[RouteResult]:
    if event.plan is None:
        raise ValueError("entry event missing plan")
    out: list[RouteResult] = []
    for rule in accounts:
        state = states.get(rule.account_id)
        if state is None:
            out.append(RouteResult(rule.account_id, "SKIP", "state missing")); continue
        if event.plan.engine == "ORG" and event.plan.reentry:
            prior = store.get_org_attempt(rule.account_id)
            if not prior:
                out.append(RouteResult(rule.account_id, "SKIP", "ORG re-entry: no destination prior attempt")); continue
            fills = await org_fill_history(rule)
            if not prove_prior_org_stop(fills=fills, stop_order_ids=set(prior.get("stop_order_ids", []))):
                out.append(RouteResult(rule.account_id, "SKIP", "ORG re-entry: destination stop outcome not proven")); continue
        try:
            alloc = allocate(event.plan, rule, state, max_state_age_seconds=max_state_age_seconds)
        except AllocationBlocked as e:
            out.append(RouteResult(rule.account_id, "SKIP", str(e))); continue
        await execute(rule, alloc)
        out.append(RouteResult(rule.account_id, "ROUTED", allocation=alloc))
    return out
