import time
from types import SimpleNamespace

import pytest

from crosstrade import CrossTradeError
from execution import Executor, NormalizationReadbackPending
from live import LiveRouter
from models import AccountRule, Allocation, EntryAttempt
from store import Store


def _rule() -> AccountRule:
    return AccountRule(
        account_id="A1", crosstrade_account="broker-1", enabled=True,
        rules_verified=True, account_type="personal", starting_balance=20_000,
        max_loss=0, max_contracts=20, personal_risk_method="fixed",
        personal_risk_value=250,
    )


def _attempt(*, age: float = 200.0) -> EntryAttempt:
    now = time.time()
    return EntryAttempt(
        attempt_key="core-readback:A1", account_id="A1",
        crosstrade_account="broker-1", event_id="core-readback", engine="CORE",
        side="SHORT", qty=3, planned_entry=30_913.5, stop=30_940.25,
        tp1=30_889.25, tp2=30_854.5, tp1_qty=2, runner_qty=1,
        custom_order_id="AP-core-readback", state="ACCEPTED",
        parent_order_id="parent-1", accepted_at_epoch=now - age,
        submit_started_at_epoch=now - age - 0.5, created_at_epoch=now - age,
        updated_at_epoch=now - age, next_reconcile_at_epoch=0.0,
    )


def _router(store: Store, executor) -> LiveRouter:
    router = LiveRouter.__new__(LiveRouter)
    router.settings = SimpleNamespace(
        ENTRY_RECONCILE_BASE_DELAY_SECONDS=0.01,
        ENTRY_RECONCILE_MAX_DELAY_SECONDS=0.01,
    )
    router.accounts = [_rule()]
    router.store = store
    router.executor = executor
    router.execution_symbol = "MNQ1!"
    return router


class _PendingLifecycleClient:
    """One CHANGE becomes visible but its final report is deliberately delayed."""

    def __init__(self):
        self.change_calls = 0
        self.version = {
            "orderId": "s1", "orderQty": 2,
            "orderType": "Stop", "stopPrice": 95.0,
        }
        self.commands = [{
            "id": 10, "orderId": "s1", "commandType": "Modify",
            "commandStatus": "Replaced",
        }]
        self.reports = [{
            "commandId": 10, "commandStatus": "Replaced",
            "rejectReason": "Success", "ordStatus": "Working",
        }]

    async def order_lifecycle(self, account, oid):
        return {"success": True, "data": {
            "order": {"id": "s1", "ordStatus": "Working"},
            "version": dict(self.version),
            "commands": [dict(row) for row in self.commands],
            "reports": [dict(row) for row in self.reports],
        }}

    async def change(self, account, oid, payload):
        self.change_calls += 1
        self.version.update({
            "orderQty": int(payload["qty"]), "orderType": "Stop",
            "stopPrice": float(payload["stopPrice"]),
        })
        self.commands.append({
            "id": 11, "orderId": "s1", "commandType": "Modify",
            "commandStatus": "Replaced",
        })
        return {"success": True}

    def finish_change(self):
        self.reports.append({
            "commandId": 11, "commandStatus": "Replaced",
            "rejectReason": "Success", "ordStatus": "Working",
        })


@pytest.mark.asyncio
async def test_live_native_children_with_partial_lifecycle_are_readback_pending():
    class PartialLifecycleClient:
        async def orders(self, account):
            return {"success": True, "data": [
                {"id": oid, "ordStatus": "Working"}
                for oid in ("t1", "s1", "t2", "s2")
            ]}

        async def order_lifecycle(self, account, oid):
            return {
                "success": True, "partial": True, "unavailable": ["version"],
                "data": {
                    "order": {"id": oid, "ordStatus": "Working"},
                    "version": None, "commands": [], "reports": [],
                },
            }

    allocation = Allocation(
        account_id="A1", event_id="core-readback", engine="CORE",
        side="SHORT", qty=3, entry=30_913.5, stop=30_940.25,
        tp1=30_889.25, tp2=30_854.5, tp1_qty=2, runner_qty=1,
        base_risk=100, effective_risk_budget=100, risk_per_contract=53.5,
    )
    executor = Executor(
        PartialLifecycleClient(), bracket_confirm_retries=1,
        bracket_confirm_delay=0,
    )

    with pytest.raises(
        NormalizationReadbackPending, match="children are live.*readback is pending"
    ):
        await executor.finalize_core("broker-1", allocation, set())


@pytest.mark.asyncio
async def test_delayed_final_change_report_is_quarantined_and_never_resent():
    client = _PendingLifecycleClient()
    desired = {"qty": 2, "orderType": "stop", "stopPrice": 96.0}
    executor = Executor(client, change_retries=1, change_delay=0)

    with pytest.raises(NormalizationReadbackPending, match="no resend"):
        await executor._change_exact("broker-1", "s1", desired)
    with pytest.raises(NormalizationReadbackPending, match="no resend"):
        await executor._change_exact("broker-1", "s1", desired)
    assert client.change_calls == 1

    # A fresh Executor models a process restart.  The broker's visible pending Modify
    # remains the durable no-resend fence even though OrderVersion already matches.
    restarted = Executor(client, change_retries=1, change_delay=0)
    with pytest.raises(NormalizationReadbackPending, match="no resend"):
        await restarted._change_exact("broker-1", "s1", desired)
    assert client.change_calls == 1

    client.finish_change()
    confirmed = await restarted._change_exact("broker-1", "s1", desired)
    assert confirmed["stopPrice"] == 96.0
    assert client.change_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        NormalizationReadbackPending(
            "child s1 CHANGE outcome unconfirmed; no resend: final report missing"
        ),
        CrossTradeError(
            "GET /orders/s1/lifecycle network error after 4 attempts: ReadTimeout"
        ),
    ],
    ids=["missing-final-change-report", "lifecycle-get-timeout"],
)
async def test_read_uncertainty_retains_native_atm_and_opens_entry_circuit(
        tmp_path, failure):
    store = Store(str(tmp_path / "readback-pending.sqlite3"))
    attempt = _attempt()
    assert store.create_entry_attempt(attempt)

    class PendingExecutor:
        async def finalize_core(self, *args, **kwargs):
            raise failure

    class NoFlattenClient:
        async def flatten(self, *args, **kwargs):
            raise AssertionError("read uncertainty must not authorize market flatten")

    router = _router(store, PendingExecutor())
    router.client = NoFlattenClient()
    row = await router._normalize_core_attempt(attempt, timeout=1.0)

    retained = store.get_entry_attempt(attempt.attempt_key)
    assert row["status"] == "CORE_NORMALIZATION_PENDING"
    assert row["quarantined"] is True
    assert retained.state == "ACCEPTED"
    assert retained.reconcile_attempts == 1
    assert retained.next_reconcile_at_epoch > time.time()
    assert store.entry_circuit()["outcome"] == (
        "CORE_NORMALIZATION_READBACK_PENDING"
    )
