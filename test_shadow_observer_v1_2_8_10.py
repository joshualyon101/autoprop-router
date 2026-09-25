from __future__ import annotations

import asyncio

import pytest

from events import ParsedEvent, parse_alert
from models import AccountRule
from shadow import ShadowObserver


class ShadowOnlyStore:
    def __init__(self):
        self.runtime: dict[str, dict] = {}

    def get_runtime_state(self, key: str):
        assert key.startswith("shadow_")
        return self.runtime.get(key)

    def set_runtime_state(self, key: str, payload: dict):
        assert key.startswith("shadow_")
        self.runtime[key] = payload

    def __getattr__(self, name: str):
        raise AssertionError(f"shadow observer attempted forbidden store access: {name}")


def _rule(account_id: str, *, enabled: bool = True) -> AccountRule:
    return AccountRule(
        account_id=account_id,
        crosstrade_account=f"broker-{account_id}",
        enabled=enabled,
        rules_verified=True,
        account_type="personal",
        starting_balance=20_000,
        max_loss=0,
        max_contracts=20,
    )


@pytest.mark.asyncio
async def test_core_entry_preserves_exact_plan_and_enabled_destination_set():
    store = ShadowOnlyStore()
    accounts = [_rule("A"), _rule("B", enabled=False), _rule("C")]
    observer = ShadowObserver(store, lambda: accounts)
    event = parse_alert(
        "D34|SIDE=SHORT|E=30733.5|SL=30832|TP1=30644.75|TP2=30370.75|"
        "MOD=D34|SRC=test|SC=4|T5=1790271000000|B5=5153|Q=2"
    )

    result = await observer.observe(event, "event-key", 1790271010.5)

    assert result["status"] == "SHADOW_OBSERVED"
    assert result["action"] == "WOULD_ENTER"
    assert result["broker_mutation_performed"] is False
    assert result["plan"] == event.plan.model_dump(mode="json")
    assert result["intended_account_ids"] == ["A", "C"]
    assert result["configured_accounts"] == 3
    assert result["enabled_accounts"] == 2
    assert store.runtime["shadow_observer_summary"]["counts"] == {"ENTRY": 1}
    assert store.runtime["shadow_last_event"]["event_key"] == "event-key"


@pytest.mark.asyncio
async def test_asw_limit_preserves_contract_geometry_without_broker_identifiers():
    store = ShadowOnlyStore()
    observer = ShadowObserver(store, lambda: [_rule("A")])
    event = parse_alert(
        "AUTOPROP_ICT_FUSION|ASW|WORKING_LIMIT|LONG|CONTRACT=ASW_LIMIT_V1|"
        "ORDER_TYPE=LIMIT|ENTRY=100|SL=90|TP=120|Q0=2|CR=40|T5=1000|EXP=2000"
    )

    result = await observer.observe(event, "asw-key", 1.0)

    assert result["action"] == "WOULD_PLACE_LIMIT"
    assert result["plan"] == event.plan.model_dump(mode="json")
    assert not any("order_id" in key.lower() for key in result)


@pytest.mark.asyncio
async def test_every_control_and_management_kind_is_observational_and_counted():
    store = ShadowOnlyStore()
    observer = ShadowObserver(store, lambda: [_rule("A")])
    events = [
        ParsedEvent(kind="EXIT", engine="CORE"),
        ParsedEvent(kind="HARD_FLAT", engine="GLOBAL"),
        ParsedEvent(kind="ASW_CANCEL_PENDING", engine="ASW", side="LONG"),
        ParsedEvent(kind="ASW_TIME_FLAT", engine="ASW", side="LONG"),
        ParsedEvent(kind="MARKET_PULSE", engine=""),
        ParsedEvent(kind="SILVER_STOP_MOVE", engine="SILVER", side="SHORT"),
        ParsedEvent(kind="FUTURE_KIND", engine="TEST"),
    ]

    results = await asyncio.gather(*(
        observer.observe(event, f"event-{index}", float(index))
        for index, event in enumerate(events)
    ))

    assert all(row["status"] == "SHADOW_OBSERVED" for row in results)
    assert all(row["action"].startswith("WOULD_") for row in results)
    assert all(row["broker_mutation_performed"] is False for row in results)
    summary = store.runtime["shadow_observer_summary"]
    assert summary["total_events"] == len(events)
    assert summary["counts"] == {event.kind: 1 for event in events}
