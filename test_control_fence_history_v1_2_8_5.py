from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from models import AccountState, VerifiedRiskState
from store import Store


def test_store_connection_factory_enforces_busy_wait_and_full_sync(tmp_path):
    store = Store(str(tmp_path / "pragmas.sqlite3"))

    connection = store._connect(timeout=5.0)
    try:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        connection.close()


def test_fresh_schema_contains_all_current_inbox_columns(tmp_path):
    store = Store(str(tmp_path / "fresh-schema.sqlite3"))

    with sqlite3.connect(store.path) as connection:
        columns = {
            str(row[1]) for row in connection.execute(
                "PRAGMA table_info(webhook_inbox)"
            ).fetchall()
        }

    assert {
        "event_key", "raw", "kind", "status", "attempts", "received_at",
        "updated_at", "last_error", "result_payload", "receipt_epoch",
        "priority", "engine", "side", "not_before_epoch",
    } <= columns


def test_concurrent_initializers_serialize_legacy_column_migration(tmp_path):
    path = tmp_path / "concurrent-migration.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE webhook_inbox (
            event_key TEXT PRIMARY KEY,
            raw TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_error TEXT,
            result_payload TEXT
        )""")
        connection.execute(
            "INSERT INTO webhook_inbox(event_key,raw,kind) VALUES(?,?,?)",
            ("preserved", "raw-control", "EXIT"),
        )

    workers = 8
    barrier = threading.Barrier(workers)

    def initialize():
        barrier.wait(timeout=5.0)
        Store(str(path))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(initialize) for _ in range(workers)]
        for future in futures:
            future.result(timeout=15.0)

    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1]) for row in connection.execute(
                "PRAGMA table_info(webhook_inbox)"
            ).fetchall()
        }
        preserved = connection.execute(
            "SELECT raw,kind,receipt_epoch FROM webhook_inbox WHERE event_key='preserved'"
        ).fetchone()
    assert {"receipt_epoch", "priority", "engine", "side", "not_before_epoch"} <= columns
    assert preserved[0:2] == ("raw-control", "EXIT")
    assert preserved[2] is not None


def test_identical_risk_seed_keeps_cache_but_changed_payload_invalidates_it(tmp_path):
    store = Store(str(tmp_path / "risk-cache.sqlite3"))
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    risk = VerifiedRiskState(
        account_id="A1", mll_floor=48_000.0, mll_verified=True,
        ledger_verified=True, verified_at=now, source="seed",
    )

    def cache_state():
        store.save_cached_account_state(AccountState(
            account_id="A1", closed_cash_balance=49_000.0,
            state_timestamp=now, daily_ledger_verified=True,
        ))

    cache_state()
    store.save_risk_state(risk)
    assert store.get_cached_account_state("A1") is None

    cache_state()
    store.save_risk_state(risk.model_copy(deep=True))
    assert store.get_cached_account_state("A1") is not None

    store.save_risk_state(risk.model_copy(update={"largest_winning_day": 123.0}))
    assert store.get_cached_account_state("A1") is None


def test_later_soft_exit_cannot_mask_hard_flat_crossed_by_attempt(tmp_path):
    store = Store(str(tmp_path / "history.sqlite3"))
    store.record_control_fence(
        "ORG", 150.0, "hard-150", kind="HARD_FLAT"
    )
    store.record_control_fence(
        "ORG", 200.0, "exit-200", kind="EXIT"
    )

    # The compatibility API remains the latest high-water event.
    assert store.latest_control_fence_info("ORG") == {
        "scope": "ORG",
        "fence_epoch": 200.0,
        "event_key": "exit-200",
        "kind": "EXIT",
    }
    # Attempt-specific lookup retains the earlier hard control that crossed PLACE.
    assert store.relevant_control_fence_info(
        "ORG", submit_started_epoch=100.0, accepted_at_epoch=175.0
    ) == {
        "scope": "ORG",
        "fence_epoch": 150.0,
        "event_key": "hard-150",
        "kind": "HARD_FLAT",
    }


def test_attempt_query_avoids_old_hard_fence_false_positive_and_finds_soft_crossing(
        tmp_path):
    store = Store(str(tmp_path / "interval.sqlite3"))
    store.record_control_fence("ORG", 150.0, "old-hard", kind="HARD_FLAT")
    store.record_control_fence("ORG", 200.0, "new-exit", kind="EXIT")

    # The hard control predates this PLACE and the soft exit follows acceptance.
    assert store.relevant_control_fence_info(
        "ORG", submit_started_epoch=160.0, accepted_at_epoch=175.0
    ) is None
    # Once the same EXIT is inside the submit/accept interval it is relevant.
    assert store.relevant_control_fence_info(
        "ORG", submit_started_epoch=160.0, accepted_at_epoch=210.0
    ) == {
        "scope": "ORG",
        "fence_epoch": 200.0,
        "event_key": "new-exit",
        "kind": "EXIT",
    }


def test_hard_control_after_acceptance_remains_relevant_for_lost_response_recovery(
        tmp_path):
    store = Store(str(tmp_path / "late-hard.sqlite3"))
    store.record_control_fence("GLOBAL", 220.0, "late-hard", kind="HARD_FLAT")

    assert store.relevant_control_fence_info(
        "CORE", submit_started_epoch=160.0, accepted_at_epoch=210.0
    ) == {
        "scope": "GLOBAL",
        "fence_epoch": 220.0,
        "event_key": "late-hard",
        "kind": "HARD_FLAT",
    }


def test_equal_epoch_lookup_deterministically_prefers_hard_control(tmp_path):
    store = Store(str(tmp_path / "equal.sqlite3"))
    store.record_control_fence("SILVER", 100.0, "soft", kind="EXIT")
    store.record_control_fence("SILVER", 100.0, "hard", kind="HARD_FLAT")

    # The strict high-watermark table keeps its first equal-epoch row, while history
    # deterministically retains and prefers the hard event for attempt reconciliation.
    assert store.latest_control_fence_info("SILVER")["event_key"] == "soft"
    assert store.relevant_control_fence_info(
        "SILVER", submit_started_epoch=90.0, accepted_at_epoch=105.0
    )["event_key"] == "hard"


def test_control_fence_replay_is_idempotent_but_conflict_is_rejected(tmp_path):
    store = Store(str(tmp_path / "replay.sqlite3"))
    store.record_control_fence("ORG", 10.0, "same-event", kind="EXIT")
    store.record_control_fence("ORG", 10.0, "same-event", kind="EXIT")

    with pytest.raises(ValueError, match="conflicting replay"):
        store.record_control_fence(
            "ORG", 20.0, "same-event", kind="HARD_FLAT"
        )

    assert store.latest_control_fence_info("ORG") == {
        "scope": "ORG", "fence_epoch": 10.0,
        "event_key": "same-event", "kind": "EXIT",
    }
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM control_fence_history"
        ).fetchone()[0] == 1


def test_attempt_query_also_sees_legacy_writer_watermark(tmp_path):
    store = Store(str(tmp_path / "mixed-version.sqlite3"))
    # Simulate an older process: it journals both controls in the inbox but retains only
    # its latest one-row compatibility watermark.
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO webhook_inbox(event_key,raw,kind,received_at) "
            "VALUES(?,?,?,?)",
            ("legacy-writer-hard", "hard", "HARD_FLAT",
             "1970-01-01 00:00:55"),
        )
        connection.execute(
            "INSERT INTO webhook_inbox(event_key,raw,kind,received_at) "
            "VALUES(?,?,?,?)",
            ("legacy-writer-exit", "exit", "EXIT",
             "1970-01-01 00:01:10"),
        )
        connection.execute(
            "INSERT INTO control_fences(scope,fence_epoch,event_key,kind) "
            "VALUES(?,?,?,?)",
            ("CORE", 70.0, "legacy-writer-exit", "EXIT"),
        )

    assert store.relevant_control_fence_info(
        "CORE", submit_started_epoch=50.0, accepted_at_epoch=60.0
    ) == {
        "scope": "GLOBAL",
        "fence_epoch": 55.0,
        "event_key": "legacy-writer-hard",
        "kind": "HARD_FLAT",
    }


@pytest.mark.parametrize("rejected_table", [
    "control_fence_history",
    "control_fences",
])
def test_control_write_failure_rolls_back_entire_enqueue(tmp_path, rejected_table):
    store = Store(str(tmp_path / f"atomic-{rejected_table}.sqlite3"))
    with sqlite3.connect(store.path) as connection:
        connection.execute(f"""
            CREATE TRIGGER reject_control_write
            BEFORE INSERT ON {rejected_table}
            WHEN NEW.event_key='blocked-control'
            BEGIN
                SELECT RAISE(ABORT, 'control write rejected');
            END
        """)

    with pytest.raises(sqlite3.IntegrityError, match="control write rejected"):
        store.enqueue_control_webhook(
            "blocked-control", "AUTOPROP_ICT_FUSION|ORG|EXIT|LONG", "EXIT",
            receipt_epoch=50.0, priority=90, engine="ORG", side="LONG",
            fence_scope="ORG",
        )

    assert store.webhook_rows() == []
    assert store.latest_control_fence_info("ORG") is None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM control_fences"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM control_fence_history"
        ).fetchone()[0] == 0


def test_online_migration_recovers_overwritten_hard_fence_from_durable_inbox(tmp_path):
    path = tmp_path / "legacy-hard.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE control_fences ("
            "scope TEXT PRIMARY KEY, fence_epoch REAL NOT NULL, event_key TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO control_fences(scope,fence_epoch,event_key) VALUES(?,?,?)",
            ("ORG", 90.0, "legacy-exit"),
        )
        connection.execute("""CREATE TABLE webhook_inbox (
            event_key TEXT PRIMARY KEY,
            raw TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_error TEXT,
            result_payload TEXT
        )""")
        connection.execute(
            "INSERT INTO webhook_inbox(event_key,raw,kind,received_at) "
            "VALUES(?,?,?,?)",
            ("legacy-hard", "AUTOPROP_ICT_FUSION|ORG|REQUIRED_FLAT_EXIT",
             "HARD_FLAT", "1970-01-01 00:01:17"),
        )
        connection.execute(
            "INSERT INTO webhook_inbox(event_key,raw,kind,received_at) "
            "VALUES(?,?,?,?)",
            ("legacy-exit", "AUTOPROP_ICT_FUSION|ORG|EXIT|LONG",
             "EXIT", "1970-01-01 00:01:30"),
        )

    store = Store(str(path))

    assert store.latest_control_fence_info("ORG") == {
        "scope": "ORG",
        "fence_epoch": 90.0,
        "event_key": "legacy-exit",
        "kind": "EXIT",
    }
    assert store.relevant_control_fence_info(
        "ORG", submit_started_epoch=70.0, accepted_at_epoch=80.0
    ) == {
        # The oldest schema had no engine column, so migration conservatively treats
        # non-current historical controls as GLOBAL rather than risking a missed flat.
        "scope": "GLOBAL",
        "fence_epoch": 77.0,
        "event_key": "legacy-hard",
        "kind": "HARD_FLAT",
    }
    # Reopening is idempotent and does not duplicate either migrated event.
    Store(str(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM control_fence_history"
        ).fetchone()[0] == 2


def test_attempt_query_tolerates_reversed_legacy_interval(tmp_path):
    store = Store(str(tmp_path / "invalid-interval.sqlite3"))
    store.record_control_fence("ORG", 15.0, "inside-reversed", kind="EXIT")

    assert store.relevant_control_fence_info(
        "ORG", submit_started_epoch=20.0, accepted_at_epoch=10.0
    )["event_key"] == "inside-reversed"
