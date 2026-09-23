import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from models import AccountRule
from models import AccountState
from events import parse_alert
from execution import ExecutionReceipt
from live import LiveRouter
from onboarding import (AutoOnboardingRejected, discover_and_onboard,
                        infer_from_verified_cohort, infer_legacy_challenge,
                        infer_mffu_pro_challenge)
from store import Store


def _settings(**updates):
    values = {
        "AUTO_DISCOVERY": True,
        "AUTO_ONBOARD_VERIFIED_CHALLENGE_COHORTS": True,
        "AUTO_ONBOARD_FUNDEDNEXT_LEGACY_CHALLENGES": True,
        "FUNDEDNEXT_DEFAULT_MODEL": "Legacy",
        "AUTO_ONBOARD_BALANCE_TOLERANCE": 1.0,
        "AUTO_ONBOARD_FILL_LOOKBACK_DAYS": 35,
    }
    values.update(updates)
    return SimpleNamespace(**values)


class FakeClient:
    def __init__(self, *, name="FNFTCHJOSHUALYON90001", account_id=90001,
                 balance=50_000.0, positions=None, orders=None, fills=None,
                 status="active"):
        self.name = name
        self.account_id = account_id
        self.balance = balance
        self.position_rows = [] if positions is None else positions
        self.order_rows = [] if orders is None else orders
        self.fill_rows = [] if fills is None else fills
        self.status = status
        self.detail_calls = 0

    async def list_accounts(self):
        return {"success": True, "data": [{"id": self.account_id, "name": self.name}]}

    async def get_account(self, account):
        assert account == self.name
        self.detail_calls += 1
        return {"success": True, "data": {
            "id": self.account_id, "name": self.name, "status": self.status,
            "balance": {"amount": self.balance},
        }}

    async def positions(self, account):
        assert account == self.name
        return {"success": True, "data": self.position_rows}

    async def orders(self, account):
        assert account == self.name
        return {"success": True, "data": self.order_rows}

    async def fills_history(self, *, account, start, end, cursor=None, limit=1000):
        assert account == self.name
        assert cursor is None
        return {"success": True, "data": self.fill_rows}


@pytest.mark.parametrize(
    "size,max_loss,target,max_contracts,floor",
    [
        (25_000, 1_000, 1_250, 20, 24_000),
        (50_000, 2_000, 3_000, 30, 48_000),
        (100_000, 3_000, 6_000, 50, 97_000),
    ],
)
def test_exact_current_legacy_profiles(size, max_loss, target, max_contracts, floor):
    rule, risk, state, evidence = infer_legacy_challenge(
        "FNFTCHJOSHUALYON90001", 90001, float(size), tolerance=1.0,
    )
    assert rule.account_id == "CT_90001"
    assert rule.enabled is True and rule.rules_verified is True
    assert rule.account_type == "challenge" and rule.drawdown == "eod"
    assert rule.starting_balance == size
    assert rule.max_loss == max_loss
    assert rule.challenge_target == target
    assert rule.max_contracts == max_contracts
    assert rule.challenge_consistency_enabled is True
    assert rule.challenge_consistency_pct == 40
    assert risk.mll_floor == floor and risk.mll_verified is True
    assert state.mll_floor == floor and state.daily_ledger_verified is True
    assert evidence["profile_contract"] == "FN_LEGACY_CHALLENGE_V1"


@pytest.mark.parametrize(
    "size,max_loss,target,max_contracts,floor",
    [
        (50_000, 2_000, 3_000, 30, 48_000),
        (100_000, 3_000, 6_000, 60, 97_000),
        (150_000, 4_500, 9_000, 90, 145_500),
    ],
)
def test_exact_current_mffu_pro_profiles(
        size, max_loss, target, max_contracts, floor):
    rule, risk, state, evidence = infer_mffu_pro_challenge(
        "MFFUEVPRO999999", 999999, float(size), tolerance=1.0,
    )
    assert rule.account_type == "challenge" and rule.drawdown == "eod"
    assert rule.starting_balance == size
    assert rule.max_loss == max_loss
    assert rule.challenge_target == target
    assert rule.max_contracts == max_contracts
    assert rule.challenge_consistency_enabled is True
    assert rule.challenge_consistency_pct == 50
    assert risk.mll_floor == floor and state.mll_floor == floor
    assert evidence["profile_contract"] == "MFFU_PRO_CHALLENGE_V1"


@pytest.mark.parametrize("name", ["FNFTFA123", "MFFUEVPRO123", "PERSONAL", ""])
def test_only_fundednext_challenge_identity_can_be_inferred(name):
    with pytest.raises(AutoOnboardingRejected, match="FundedNext challenge"):
        infer_legacy_challenge(name, 1, 50_000.0, tolerance=1.0)


@pytest.mark.parametrize("balance", [49_998.99, 50_001.01, 75_000, 150_000])
def test_nonexact_or_unsupported_balance_is_rejected(balance):
    with pytest.raises(AutoOnboardingRejected, match="starting balance not proven"):
        infer_legacy_challenge(
            "FNFTCHJOSHUALYON90001", 90001, balance, tolerance=1.0,
        )


@pytest.mark.asyncio
async def test_new_untouched_legacy_challenge_is_enabled_and_persisted(tmp_path):
    store = Store(str(tmp_path / "router.sqlite3"))
    client = FakeClient()

    result = await discover_and_onboard(_settings(), client, store, [])

    assert result["quarantined"] == []
    assert result["onboarded"] == [{
        "account_id": "CT_90001",
        "crosstrade_account": client.name,
        "starting_balance": 50_000.0,
        "status": "ONBOARDED",
    }]
    rules = store.all_auto_accounts()
    assert len(rules) == 1
    assert rules[0].enabled is True and rules[0].rules_verified is True
    assert rules[0].max_contracts == 30
    assert store.get_risk_state("CT_90001").mll_floor == 48_000
    cached = store.get_cached_account_state("CT_90001")
    assert cached.closed_cash_balance == 50_000
    assert cached.daily_ledger_verified is True
    assert store.all_auto_discovery_audit()[0]["status"] == "ONBOARDED"


@pytest.mark.asyncio
async def test_second_scan_is_idempotent_and_does_not_reinspect(tmp_path):
    store = Store(str(tmp_path / "router.sqlite3"))
    client = FakeClient()
    await discover_and_onboard(_settings(), client, store, [])
    calls = client.detail_calls

    result = await discover_and_onboard(
        _settings(), client, store, store.all_auto_accounts(),
    )

    assert result["onboarded"] == []
    assert result["existing"] == [client.name]
    assert client.detail_calls == calls
    assert len(store.all_auto_accounts()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client,reason",
    [
        (FakeClient(positions=[{"netPos": 1}]), "open position"),
        (FakeClient(orders=[{"id": "working-1"}]), "working order"),
        (FakeClient(fills=[{"id": "fill-1"}]), "historical fill"),
        (FakeClient(status="breached"), "status is breached"),
        (FakeClient(balance=49_990), "starting balance not proven"),
    ],
)
async def test_any_nonnew_evidence_quarantines_without_registering(tmp_path, client, reason):
    store = Store(str(tmp_path / "router.sqlite3"))

    result = await discover_and_onboard(_settings(), client, store, [])

    assert result["onboarded"] == []
    assert reason in result["quarantined"][0]["reason"]
    assert store.all_auto_accounts() == []
    assert store.all_auto_discovery_audit()[0]["status"] == "QUARANTINED"


@pytest.mark.asyncio
async def test_nonlegacy_default_never_guesses_rules(tmp_path):
    store = Store(str(tmp_path / "router.sqlite3"))
    client = FakeClient()

    result = await discover_and_onboard(
        _settings(FUNDEDNEXT_DEFAULT_MODEL="Rapid"), client, store, [],
    )

    assert result["onboarded"] == []
    assert "default is not Legacy" in result["quarantined"][0]["reason"]
    assert client.detail_calls == 1


@pytest.mark.asyncio
async def test_mffu_pro_challenge_auto_onboards_without_fundednext_default(tmp_path):
    store = Store(str(tmp_path / "router.sqlite3"))
    client = FakeClient(
        name="MFFUEVPRO999999", account_id=999999, balance=100_000,
    )

    result = await discover_and_onboard(
        _settings(FUNDEDNEXT_DEFAULT_MODEL="Rapid"), client, store, [],
    )

    assert result["quarantined"] == []
    assert result["onboarded"][0]["account_id"] == "CT_999999"
    rule = store.all_auto_accounts()[0]
    assert rule.max_loss == 3_000
    assert rule.max_contracts == 60
    assert rule.challenge_consistency_pct == 50


def test_unknown_named_challenge_inherits_exact_verified_cohort():
    template = AccountRule(
        account_id="CT_OLD", crosstrade_account="ACMECHALLENGE10001",
        enabled=True, rules_verified=True, account_type="challenge",
        starting_balance=75_000, max_loss=2_500, max_contracts=40,
        drawdown="live", challenge_target=4_500,
        challenge_consistency_enabled=True, challenge_consistency_pct=35,
        notes="operator-verified template",
    )

    rule, risk, state, evidence = infer_from_verified_cohort(
        "ACMECHALLENGE10002", 10002, 75_000.0, [template], tolerance=1.0,
    )

    assert rule.account_id == "CT_10002"
    assert rule.max_loss == template.max_loss
    assert rule.max_contracts == template.max_contracts
    assert rule.drawdown == template.drawdown
    assert rule.challenge_consistency_pct == 35
    assert risk.mll_floor == 72_500 and state.mll_floor == 72_500
    assert evidence["profile_contract"] == "VERIFIED_CHALLENGE_COHORT_V1"


@pytest.mark.asyncio
async def test_unknown_named_challenge_cohort_auto_onboards_end_to_end(tmp_path):
    template = AccountRule(
        account_id="CT_OLD", crosstrade_account="ACMECHALLENGE10001",
        enabled=True, rules_verified=True, account_type="challenge",
        starting_balance=75_000, max_loss=2_500, max_contracts=40,
        drawdown="live", challenge_target=4_500,
        challenge_consistency_enabled=True, challenge_consistency_pct=35,
    )
    store = Store(str(tmp_path / "router.sqlite3"))
    client = FakeClient(
        name="ACMECHALLENGE10002", account_id=10002, balance=75_000,
    )

    result = await discover_and_onboard(
        _settings(), client, store, [template],
    )

    assert result["quarantined"] == []
    assert result["onboarded"][0]["account_id"] == "CT_10002"
    enrolled = store.all_auto_accounts()[0]
    assert enrolled.max_loss == 2_500
    assert enrolled.max_contracts == 40
    assert enrolled.challenge_consistency_pct == 35
    assert store.get_risk_state("CT_10002").mll_floor == 72_500


def test_conflicting_verified_cohort_profiles_are_rejected():
    base = AccountRule(
        account_id="CT_OLD1", crosstrade_account="ACMECHALLENGE10001",
        enabled=True, rules_verified=True, account_type="challenge",
        starting_balance=75_000, max_loss=2_500, max_contracts=40,
        drawdown="eod", challenge_target=4_500,
    )
    conflicting = AccountRule.model_validate({
        **base.model_dump(), "account_id": "CT_OLD2",
        "crosstrade_account": "ACMECHALLENGE10003", "max_contracts": 20,
    })

    with pytest.raises(AutoOnboardingRejected, match="conflicting"):
        infer_from_verified_cohort(
            "ACMECHALLENGE10002", 10002, 75_000.0,
            [base, conflicting], tolerance=1.0,
        )


@pytest.mark.asyncio
async def test_active_entry_wave_defers_atomic_publish(tmp_path):
    store = Store(str(tmp_path / "router.sqlite3"))
    client = FakeClient()

    result = await discover_and_onboard(
        _settings(), client, store, [], publish_guard=lambda: False,
    )

    assert result["onboarded"] == []
    assert "entry wave active" in result["quarantined"][0]["reason"]
    assert store.all_auto_accounts() == []


def test_atomic_commit_rejects_identity_collision(tmp_path):
    store = Store(str(tmp_path / "router.sqlite3"))
    rule, risk, state, evidence = infer_legacy_challenge(
        "FNFTCHJOSHUALYON90001", 90001, 50_000.0, tolerance=1.0,
    )
    assert store.commit_auto_onboarded_account(rule, risk, state, evidence) is True

    conflicting = AccountRule.model_validate({
        **rule.model_dump(), "crosstrade_account": "FNFTCHOTHER90001",
    })
    with pytest.raises(ValueError, match="collision"):
        store.commit_auto_onboarded_account(conflicting, risk, state, evidence)
    assert store.all_auto_accounts()[0].crosstrade_account == rule.crosstrade_account


def test_main_registry_overlay_preserves_static_accounts_and_adds_dynamic(
        tmp_path, monkeypatch):
    import main

    dynamic_store = Store(str(tmp_path / "router.sqlite3"))
    auto_rule, risk, state, evidence = infer_legacy_challenge(
        "FNFTCHJOSHUALYON90001", 90001, 50_000.0, tolerance=1.0,
    )
    dynamic_store.commit_auto_onboarded_account(auto_rule, risk, state, evidence)
    static_rule = AccountRule(
        account_id="CT_STATIC", crosstrade_account="FNFTCHSTATIC",
        enabled=True, rules_verified=True, account_type="challenge",
        starting_balance=100_000, max_loss=3_000, max_contracts=50,
        drawdown="eod", challenge_target=6_000,
        challenge_consistency_enabled=True, challenge_consistency_pct=40,
    )
    monkeypatch.setattr(main, "store", dynamic_store)
    monkeypatch.setattr(main.settings, "ACCOUNT_CONFIG_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(
        main.settings, "ACCOUNT_CONFIG_JSON",
        '{"accounts":[' + static_rule.model_dump_json() + ']}'
    )

    merged = main._accounts()

    assert [rule.account_id for rule in merged] == ["CT_STATIC", "CT_90001"]


def test_explicit_static_config_wins_over_same_auto_account(tmp_path, monkeypatch):
    import main

    dynamic_store = Store(str(tmp_path / "router.sqlite3"))
    auto_rule, risk, state, evidence = infer_legacy_challenge(
        "FNFTCHJOSHUALYON90001", 90001, 50_000.0, tolerance=1.0,
    )
    dynamic_store.commit_auto_onboarded_account(auto_rule, risk, state, evidence)
    static_override = AccountRule.model_validate({
        **auto_rule.model_dump(), "profile": "aggressive",
        "notes": "explicit operator override",
    })
    monkeypatch.setattr(main, "store", dynamic_store)
    monkeypatch.setattr(main.settings, "ACCOUNT_CONFIG_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(
        main.settings, "ACCOUNT_CONFIG_JSON",
        '{"accounts":[' + static_override.model_dump_json() + ']}'
    )

    merged = main._accounts()

    assert len(merged) == 1
    assert merged[0].profile == "aggressive"
    assert merged[0].notes == "explicit operator override"


def test_health_reports_effective_zero_touch_configuration(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "store", Store(str(tmp_path / "router.sqlite3")))
    monkeypatch.setattr(main, "_accounts", lambda: [])
    monkeypatch.setattr(main.settings, "AUTO_DISCOVERY", True)
    monkeypatch.setattr(
        main.settings, "AUTO_ONBOARD_VERIFIED_CHALLENGE_COHORTS", True
    )
    monkeypatch.setattr(
        main.settings, "AUTO_ONBOARD_FUNDEDNEXT_LEGACY_CHALLENGES", True
    )
    monkeypatch.setattr(main.settings, "FUNDEDNEXT_DEFAULT_MODEL", "Legacy")

    payload = main.health()

    assert payload["auto_discovery_enabled"] is True
    assert payload["auto_onboarding_enabled"] is True
    assert payload["auto_onboarding_policy"] == (
        "verified_challenge_cohort_or_known_profile"
    )
    assert payload["fundednext_default_model"] == "Legacy"
    assert payload["auto_onboarded_accounts"] == 0


def _personal_rule(index: int) -> AccountRule:
    return AccountRule(
        account_id=f"P{index}", crosstrade_account=f"personal-{index}",
        enabled=True, rules_verified=True, account_type="personal",
        starting_balance=20_000, max_loss=0, max_contracts=50,
        personal_risk_method="fixed", personal_risk_value=250,
    )


@pytest.mark.asyncio
async def test_registry_change_never_alters_inflight_signal_but_joins_next_signal(tmp_path):
    first, newly_onboarded = _personal_rule(1), _personal_rule(2)
    store = Store(str(tmp_path / "router.sqlite3"))
    for rule in (first, newly_onboarded):
        store.save_cached_account_state(AccountState(
            account_id=rule.account_id, closed_cash_balance=20_000,
            state_timestamp=datetime.now(timezone.utc), daily_ledger_verified=True,
        ))

    class Executor:
        def __init__(self):
            self.submitted = []

        async def submit_single(self, account, allocation, custom_id, **_kwargs):
            self.submitted.append(account)
            return ExecutionReceipt(
                result={"success": True}, parent_order_id=f"parent-{account}",
                child_order_ids=[f"target-{account}", f"stop-{account}"],
                target_order_ids=[f"target-{account}"],
                stop_order_ids=[f"stop-{account}"],
            )

        place_single = submit_single

    router = LiveRouter.__new__(LiveRouter)
    router.accounts = [first]
    router.store = store
    router.entry_wave_active = asyncio.Event()
    router.executor = Executor()
    router.settings = SimpleNamespace(
        ENTRY_SIGNAL_MAX_AGE_SECONDS=8.0,
        ENTRY_PREFLIGHT_TIMEOUT_SECONDS=5.0,
        ENTRY_FANOUT_MAX_CONCURRENCY=16,
        MAX_STATE_AGE_SECONDS=20,
        ENTRY_STATE_CACHE_MAX_AGE_SECONDS=20.0,
    )
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def delayed_snapshot():
        snapshot_started.set()
        await release_snapshot.wait()
        return {
            rule.crosstrade_account: {
                "name": rule.crosstrade_account,
                "positions": [], "workingOrders": [],
            }
            for rule in (first, newly_onboarded)
        }

    router._entry_snapshot = delayed_snapshot
    first_event = parse_alert(
        "AUTOPROP_ICT_FUSION|SILVER|ENTRY|SHORT|ENTRY=100|SL=105|TP=90|B5=1"
    )
    task = asyncio.create_task(router.route_entry(
        first_event, event_key="wave-1", receipt_epoch=time.time(),
    ))
    await snapshot_started.wait()
    router.replace_accounts([first, newly_onboarded])
    release_snapshot.set()
    first_result = await task

    assert [row["account_id"] for row in first_result["results"]] == [first.account_id]
    assert router.executor.submitted == [first.crosstrade_account], (
        first_result["results"][0]["reason"]
    )

    for attempt in store.all_entry_attempts(("ACCEPTED",)):
        store.transition_entry_attempt(
            attempt.attempt_key, ("ACCEPTED",), "CLOSED",
            last_error="test closes first completed wave",
        )

    async def immediate_snapshot():
        return {
            rule.crosstrade_account: {
                "name": rule.crosstrade_account,
                "positions": [], "workingOrders": [],
            }
            for rule in (first, newly_onboarded)
        }

    router._entry_snapshot = immediate_snapshot
    second_event = parse_alert(
        "AUTOPROP_ICT_FUSION|SILVER|ENTRY|SHORT|ENTRY=101|SL=106|TP=91|B5=2"
    )
    second_result = await router.route_entry(
        second_event, event_key="wave-2", receipt_epoch=time.time(),
    )

    assert {row["account_id"] for row in second_result["results"]} == {
        first.account_id, newly_onboarded.account_id,
    }
    assert router.executor.submitted[-2:] == [
        first.crosstrade_account, newly_onboarded.crosstrade_account,
    ]
