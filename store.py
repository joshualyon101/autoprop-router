from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from models import (ActiveTrade, VerifiedRiskState, AswPending, AccountState,
                    EntryAttempt, AccountRule)


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def _connect(self, timeout: float = 10.0) -> sqlite3.Connection:
        """Open one fully durable, bounded-wait SQLite connection."""
        conn = sqlite3.connect(self.path, timeout=float(timeout))
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def db(self):
        conn = self._connect(timeout=10.0)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self):
        with self.db() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            # Multiple Railway workers can boot against the same volume. Serialize the
            # schema check/ALTER sequence so two initializers cannot both observe a
            # missing column and race the same migration.
            c.execute("BEGIN EXCLUSIVE")
            c.execute("CREATE TABLE IF NOT EXISTS dedupe (event_id TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            c.execute("CREATE TABLE IF NOT EXISTS active_trades (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS org_attempts (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS risk_state (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS asw_pending (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS cached_account_state (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            c.execute("""CREATE TABLE IF NOT EXISTS auto_accounts (
                account_id TEXT PRIMARY KEY,
                crosstrade_account TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                evidence TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS auto_discovery_audit (
                crosstrade_account TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS entry_attempts (
                attempt_key TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                engine TEXT NOT NULL,
                state TEXT NOT NULL,
                payload TEXT NOT NULL
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS entry_attempts_state_idx ON entry_attempts(state)")
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS entry_attempts_one_open_account_idx "
                "ON entry_attempts(account_id) WHERE state IN "
                "('PREPARED','SUBMITTING','ACCEPTED','FLATTENING')"
            )
            c.execute("""CREATE TABLE IF NOT EXISTS control_fences (
                scope TEXT PRIMARY KEY,
                fence_epoch REAL NOT NULL,
                event_key TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'EXIT'
            )""")
            fence_cols = {str(r[1]) for r in c.execute(
                "PRAGMA table_info(control_fences)"
            ).fetchall()}
            if "kind" not in fence_cols:
                c.execute(
                    "ALTER TABLE control_fences ADD COLUMN kind TEXT NOT NULL DEFAULT 'EXIT'"
                )
            c.execute("CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            c.execute("""CREATE TABLE IF NOT EXISTS webhook_inbox (
                event_key TEXT PRIMARY KEY,
                raw TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_error TEXT,
                result_payload TEXT,
                receipt_epoch REAL,
                priority INTEGER NOT NULL DEFAULT 0,
                engine TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL DEFAULT '',
                not_before_epoch REAL NOT NULL DEFAULT 0
            )""")
            # Online migration for existing Railway volumes. SQLite ALTER preserves every
            # prior inbox row and the original second-resolution human timestamp. Re-read
            # table_info immediately before every ALTER for safe upgrades from any prefix.
            inbox_migrations = (
                ("receipt_epoch", "receipt_epoch REAL"),
                ("priority", "priority INTEGER NOT NULL DEFAULT 0"),
                ("engine", "engine TEXT NOT NULL DEFAULT ''"),
                ("side", "side TEXT NOT NULL DEFAULT ''"),
                ("not_before_epoch", "not_before_epoch REAL NOT NULL DEFAULT 0"),
            )
            for column, definition in inbox_migrations:
                current_columns = {str(r[1]) for r in c.execute(
                    "PRAGMA table_info(webhook_inbox)"
                ).fetchall()}
                if column not in current_columns:
                    c.execute(f"ALTER TABLE webhook_inbox ADD COLUMN {definition}")
            c.execute(
                "UPDATE webhook_inbox SET receipt_epoch=CAST(strftime('%s', received_at) AS REAL) "
                "WHERE receipt_epoch IS NULL"
            )
            # Keep every control event, not only the latest per scope.  The high-watermark
            # table above remains the fast admission check, while this append-only history
            # lets reconciliation prove that an earlier HARD_FLAT crossed an in-flight
            # PLACE even when a later ordinary EXIT advanced the scope watermark.
            c.execute("""CREATE TABLE IF NOT EXISTS control_fence_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                fence_epoch REAL NOT NULL,
                event_key TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'EXIT',
                UNIQUE(scope,event_key)
            )""")
            c.execute(
                "CREATE INDEX IF NOT EXISTS control_fence_history_scope_epoch_idx "
                "ON control_fence_history(scope,fence_epoch)"
            )
            # The previous release added ``kind`` to control_fences with EXIT as its
            # migration default. Recover the exact kind when its durable inbox event is
            # still present, then seed history idempotently from the old watermark rows.
            c.execute(
                "UPDATE control_fences SET kind=("
                "SELECT webhook_inbox.kind FROM webhook_inbox "
                "WHERE webhook_inbox.event_key=control_fences.event_key LIMIT 1"
                ") WHERE EXISTS (SELECT 1 FROM webhook_inbox "
                "WHERE webhook_inbox.event_key=control_fences.event_key)"
            )
            c.execute(
                "INSERT OR IGNORE INTO control_fence_history("
                "scope,fence_epoch,event_key,kind) "
                "SELECT scope,fence_epoch,event_key,kind FROM control_fences"
            )
            c.execute(
                "INSERT OR IGNORE INTO control_fence_history("
                "scope,fence_epoch,event_key,kind) "
                "SELECT CASE WHEN control_fences.scope IS NOT NULL "
                "THEN control_fences.scope "
                "WHEN UPPER(COALESCE(webhook_inbox.engine,'')) "
                "IN ('','GLOBAL','ACCOUNT') THEN 'GLOBAL' "
                "ELSE UPPER(webhook_inbox.engine) END,"
                "COALESCE(control_fences.fence_epoch,webhook_inbox.receipt_epoch),"
                "webhook_inbox.event_key,UPPER(webhook_inbox.kind) "
                "FROM webhook_inbox LEFT JOIN control_fences "
                "ON control_fences.event_key=webhook_inbox.event_key "
                "WHERE webhook_inbox.kind IN "
                "('EXIT','HARD_FLAT','ASW_CANCEL_PENDING','ASW_TIME_FLAT') "
                "AND COALESCE(control_fences.fence_epoch,webhook_inbox.receipt_epoch) "
                "IS NOT NULL"
            )

    def claim_event(self, event_id: str) -> bool:
        try:
            with self.db() as c:
                c.execute("INSERT INTO dedupe(event_id) VALUES(?)", (event_id,))
            return True
        except sqlite3.IntegrityError:
            return False

    def save_trade(self, trade: ActiveTrade):
        with self.db() as c:
            c.execute("INSERT INTO active_trades(account_id,payload) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload",
                      (trade.account_id, trade.model_dump_json()))

    def get_trade(self, account_id: str) -> ActiveTrade | None:
        with self.db() as c:
            row = c.execute("SELECT payload FROM active_trades WHERE account_id=?", (account_id,)).fetchone()
        return ActiveTrade.model_validate_json(row[0]) if row else None

    def all_trades(self) -> list[ActiveTrade]:
        with self.db() as c:
            rows = c.execute("SELECT payload FROM active_trades").fetchall()
        return [ActiveTrade.model_validate_json(r[0]) for r in rows]

    def delete_trade(self, account_id: str):
        conn = self._connect(timeout=10.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM active_trades WHERE account_id=?", (account_id,)
            ).fetchone()
            conn.execute("DELETE FROM active_trades WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM cached_account_state WHERE account_id=?", (account_id,))
            if row is not None:
                trade = ActiveTrade.model_validate_json(row[0])
                attempt_row = conn.execute(
                    "SELECT attempt_key,payload FROM entry_attempts "
                    "WHERE account_id=? AND event_id=? AND state='ACTIVE' ORDER BY rowid DESC LIMIT 1",
                    (account_id, trade.event_id),
                ).fetchone()
                if attempt_row is not None:
                    values = EntryAttempt.model_validate_json(attempt_row[1]).model_dump()
                    values.update({"state": "CLOSED", "updated_at_epoch": time.time()})
                    attempt = EntryAttempt.model_validate(values)
                    conn.execute(
                        "UPDATE entry_attempts SET state='CLOSED',payload=? "
                        "WHERE attempt_key=? AND state='ACTIVE'",
                        (attempt.model_dump_json(), attempt_row[0]),
                    )
            conn.commit()
        finally:
            conn.close()

    def save_cached_account_state(self, state: AccountState):
        conn = self._connect(timeout=10.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM cached_account_state WHERE account_id=?",
                (state.account_id,),
            ).fetchone()
            if row is not None:
                existing = AccountState.model_validate_json(row[0])
                if existing.state_timestamp >= state.state_timestamp:
                    conn.rollback()
                    return
            conn.execute(
                "INSERT INTO cached_account_state(account_id,payload) VALUES(?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload",
                (state.account_id, state.model_dump_json()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_cached_account_state(self, account_id: str) -> AccountState | None:
        with self.db() as c:
            row = c.execute(
                "SELECT payload FROM cached_account_state WHERE account_id=?", (account_id,)
            ).fetchone()
        return AccountState.model_validate_json(row[0]) if row else None

    def all_cached_account_states(self) -> list[AccountState]:
        with self.db() as c:
            rows = c.execute("SELECT payload FROM cached_account_state").fetchall()
        return [AccountState.model_validate_json(r[0]) for r in rows]

    def delete_cached_account_state(self, account_id: str):
        with self.db() as c:
            c.execute("DELETE FROM cached_account_state WHERE account_id=?", (account_id,))

    def create_entry_attempt(self, attempt: EntryAttempt) -> bool:
        try:
            with self.db() as c:
                c.execute(
                    "INSERT INTO entry_attempts(attempt_key,account_id,event_id,engine,state,payload) "
                    "VALUES(?,?,?,?,?,?)",
                    (attempt.attempt_key, attempt.account_id, attempt.event_id, attempt.engine,
                     attempt.state, attempt.model_dump_json()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def prepare_entry_attempt(self, dedupe_key: str, attempt: EntryAttempt) -> bool:
        """Atomically claim the signal and create its recoverable pre-mutation row."""
        return bool(self.prepare_entry_attempts([(dedupe_key, attempt)])[attempt.attempt_key])

    def prepare_entry_attempts(
            self, items: Iterable[tuple[str, EntryAttempt]]) -> dict[str, bool]:
        """Prepare one entry wave with a single durable commit.

        Each account retains the historical independent duplicate/open-account result,
        while unrelated accounts in the same wave may still be prepared. Expected skips
        are identified explicitly while the write lock is held. Any later constraint,
        trigger, schema, or storage failure aborts the entire batch and is surfaced.
        """
        batch: list[tuple[str, EntryAttempt, str]] = []
        attempt_keys: set[str] = set()
        dedupe_keys: set[str] = set()
        for raw_dedupe_key, attempt in items:
            dedupe_key = str(raw_dedupe_key)
            if attempt.state != 'PREPARED':
                raise ValueError('entry attempt batch may contain only PREPARED rows')
            if not dedupe_key or not attempt.attempt_key:
                raise ValueError('entry attempt batch keys must be non-empty')
            if attempt.attempt_key in attempt_keys:
                raise ValueError(f'duplicate attempt key in batch: {attempt.attempt_key}')
            if dedupe_key in dedupe_keys:
                raise ValueError(f'duplicate dedupe key in batch: {dedupe_key}')
            attempt_keys.add(attempt.attempt_key)
            dedupe_keys.add(dedupe_key)
            # Serialize before taking the write lock so validation/encoding cannot leave a
            # partially processed wave or unnecessarily extend the SQLite critical section.
            batch.append((dedupe_key, attempt, attempt.model_dump_json()))
        if not batch:
            return {}

        conn = self._connect(timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            prepared: dict[str, bool] = {}
            for dedupe_key, attempt, payload in batch:
                duplicate = conn.execute(
                    "SELECT 1 FROM dedupe WHERE event_id=?",
                    (dedupe_key,),
                ).fetchone()
                attempt_exists = conn.execute(
                    "SELECT 1 FROM entry_attempts WHERE attempt_key=?",
                    (attempt.attempt_key,),
                ).fetchone()
                account_open = conn.execute(
                    "SELECT 1 FROM entry_attempts WHERE account_id=? AND state IN "
                    "('PREPARED','SUBMITTING','ACCEPTED','FLATTENING') LIMIT 1",
                    (attempt.account_id,),
                ).fetchone()
                if duplicate is not None or attempt_exists is not None or account_open is not None:
                    prepared[attempt.attempt_key] = False
                    continue
                conn.execute("INSERT INTO dedupe(event_id) VALUES(?)", (dedupe_key,))
                conn.execute(
                    "INSERT INTO entry_attempts("
                    "attempt_key,account_id,event_id,engine,state,payload) "
                    "VALUES(?,?,?,?,?,?)",
                    (attempt.attempt_key, attempt.account_id, attempt.event_id,
                     attempt.engine, attempt.state, payload),
                )
                prepared[attempt.attempt_key] = True
            conn.commit()
            return prepared
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def begin_entry_submissions(
            self, attempt_keys: Iterable[str], *,
            submit_started_epoch: float) -> dict[str, EntryAttempt] | None:
        """Atomically claim an entire prepared wave immediately before broker PLACE.

        ``None`` means at least one requested row was absent or no longer PREPARED; no
        requested row is changed in that case. Marking the wave SUBMITTING before network
        dispatch is deliberately conservative: after a crash, reconciliation never resends
        an order whose broker outcome could be unknown.
        """
        keys = [str(key) for key in attempt_keys]
        if not keys:
            return {}
        if any(not key for key in keys):
            raise ValueError('entry submission keys must be non-empty')
        if len(set(keys)) != len(keys):
            raise ValueError('entry submission keys must be unique')
        submit_started = float(submit_started_epoch)
        if submit_started <= 0:
            raise ValueError('submit_started_epoch must be positive')

        conn = self._connect(timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT attempt_key,state,payload FROM entry_attempts "
                f"WHERE attempt_key IN ({marks})",
                tuple(keys),
            ).fetchall()
            by_key = {str(row[0]): row for row in rows}
            if (set(by_key) != set(keys)
                    or any(str(by_key[key][1]) != 'PREPARED' for key in keys)):
                conn.rollback()
                return None

            updated_at = time.time()
            claimed: dict[str, EntryAttempt] = {}
            for key in keys:
                original = EntryAttempt.model_validate_json(by_key[key][2])
                values = original.model_dump()
                values.update({
                    'state': 'SUBMITTING',
                    'submit_started_at_epoch': submit_started,
                    'updated_at_epoch': updated_at,
                })
                attempt = EntryAttempt.model_validate(values)
                cur = conn.execute(
                    "UPDATE entry_attempts SET state='SUBMITTING',payload=? "
                    "WHERE attempt_key=? AND state='PREPARED'",
                    (attempt.model_dump_json(), key),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return None
                claimed[key] = attempt
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_entry_attempt(self, attempt_key: str) -> EntryAttempt | None:
        with self.db() as c:
            row = c.execute(
                "SELECT payload FROM entry_attempts WHERE attempt_key=?", (attempt_key,)
            ).fetchone()
        return EntryAttempt.model_validate_json(row[0]) if row else None

    def all_entry_attempts(self, states: Iterable[str] | None = None) -> list[EntryAttempt]:
        with self.db() as c:
            if states is not None:
                wanted = (states,) if isinstance(states, str) else tuple(str(x) for x in states)
                if not wanted:
                    return []
                marks = ",".join("?" for _ in wanted)
                rows = c.execute(
                    f"SELECT payload FROM entry_attempts WHERE state IN ({marks}) ORDER BY rowid",
                    wanted,
                ).fetchall()
            else:
                rows = c.execute("SELECT payload FROM entry_attempts ORDER BY rowid").fetchall()
        return [EntryAttempt.model_validate_json(r[0]) for r in rows]

    def transition_entry_attempt(self, attempt_key: str, expected_states: Iterable[str],
                                 new_state: str, **updates) -> EntryAttempt | None:
        """Compare-and-swap an attempt so late verifiers cannot resurrect exposure."""
        expected = ((expected_states,) if isinstance(expected_states, str)
                    else tuple(str(x) for x in expected_states))
        if not expected:
            return None
        immutable = {
            'attempt_key', 'account_id', 'crosstrade_account', 'event_id', 'engine',
            'side', 'qty', 'planned_entry', 'stop', 'tp1', 'tp2', 'tp1_qty',
            'runner_qty', 'custom_order_id', 'created_at_epoch',
            'entry_native_bar_index', 'entry_receipt_epoch', 'inbox_event_key',
            'state', 'updated_at_epoch',
        }
        unknown = set(updates) - set(EntryAttempt.model_fields)
        forbidden = set(updates) & immutable
        if unknown or forbidden:
            raise ValueError(
                f'invalid EntryAttempt transition fields: {sorted(unknown | forbidden)}'
            )
        conn = self._connect(timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state,payload FROM entry_attempts WHERE attempt_key=?", (attempt_key,)
            ).fetchone()
            if row is None or str(row[0]) not in expected:
                conn.rollback()
                return None
            attempt = EntryAttempt.model_validate_json(row[1])
            values = attempt.model_dump()
            values.update(updates)
            values.update({"state": new_state, "updated_at_epoch": time.time()})
            attempt = EntryAttempt.model_validate(values)
            marks = ",".join("?" for _ in expected)
            cur = conn.execute(
                f"UPDATE entry_attempts SET state=?,payload=? WHERE attempt_key=? AND state IN ({marks})",
                (new_state, attempt.model_dump_json(), attempt_key, *expected),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            return attempt
        finally:
            conn.close()

    def promote_entry_attempt(self, attempt_key: str, trade: ActiveTrade) -> bool:
        """Atomically promote ACCEPTED to ACTIVE and persist the managed trade."""
        conn = self._connect(timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM entry_attempts WHERE attempt_key=? AND state='ACCEPTED'",
                (attempt_key,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            original = EntryAttempt.model_validate_json(row[0])
            if (trade.account_id != original.account_id
                    or trade.event_id != original.event_id
                    or trade.engine != original.engine
                    or trade.side != original.side
                    or trade.total_qty != original.qty):
                conn.rollback()
                return False
            existing = conn.execute(
                "SELECT payload FROM active_trades WHERE account_id=?", (trade.account_id,)
            ).fetchone()
            if existing is not None:
                active = ActiveTrade.model_validate_json(existing[0])
                if active.event_id != trade.event_id:
                    conn.rollback()
                    return False
            values = original.model_dump()
            values.update({
                "state": "ACTIVE", "actual_entry": trade.entry,
                "updated_at_epoch": time.time(), "last_error": "",
            })
            attempt = EntryAttempt.model_validate(values)
            conn.execute(
                "INSERT INTO active_trades(account_id,payload) VALUES(?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload",
                (trade.account_id, trade.model_dump_json()),
            )
            cur = conn.execute(
                "UPDATE entry_attempts SET state='ACTIVE',payload=? "
                "WHERE attempt_key=? AND state='ACCEPTED'",
                (attempt.model_dump_json(), attempt_key),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        finally:
            conn.close()

    def record_control_fence(self, scope: str, fence_epoch: float, event_key: str,
                             kind: str = 'EXIT'):
        scope = str(scope or "GLOBAL").upper()
        fence_epoch = float(fence_epoch)
        event_key = str(event_key)
        kind = str(kind or 'EXIT').upper()
        conn = self._connect(timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT fence_epoch,kind FROM control_fence_history "
                "WHERE scope=? AND event_key=?", (scope, event_key),
            ).fetchone()
            if existing is not None:
                if (float(existing[0]) != fence_epoch
                        or str(existing[1]).upper() != kind):
                    raise ValueError('conflicting replay of control fence identity')
                conn.rollback()
                return
            conn.execute(
                "INSERT INTO control_fence_history("
                "scope,fence_epoch,event_key,kind) VALUES(?,?,?,?)",
                (scope, fence_epoch, event_key, kind),
            )
            conn.execute(
                "INSERT INTO control_fences(scope,fence_epoch,event_key,kind) VALUES(?,?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET fence_epoch=excluded.fence_epoch,"
                "event_key=excluded.event_key,kind=excluded.kind "
                "WHERE excluded.fence_epoch > control_fences.fence_epoch",
                (scope, fence_epoch, event_key, kind),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def entry_aborted_after(self, engine: str, entry_epoch: float) -> bool:
        fence = self.latest_control_fence(engine)
        return bool(fence is not None and fence > float(entry_epoch))

    def latest_control_fence(self, engine: str) -> float | None:
        info = self.latest_control_fence_info(engine)
        return float(info['fence_epoch']) if info else None

    def latest_control_fence_info(self, engine: str) -> dict | None:
        scopes = ("GLOBAL", str(engine or "").upper())
        with self.db() as c:
            row = c.execute(
                "SELECT scope,fence_epoch,event_key,kind FROM control_fences "
                "WHERE scope IN (?,?) ORDER BY fence_epoch DESC,"
                "CASE WHEN UPPER(kind) IN "
                "('HARD_FLAT','ASW_TIME_FLAT','ASW_CANCEL_PENDING') "
                "THEN 1 ELSE 0 END DESC,rowid DESC LIMIT 1",
                scopes,
            ).fetchone()
        if row is None:
            return None
        return {'scope': str(row[0]), 'fence_epoch': float(row[1]),
                'event_key': str(row[2]), 'kind': str(row[3])}

    def relevant_control_fence_info(self, engine: str, *,
                                    submit_started_epoch: float,
                                    accepted_at_epoch: float) -> dict | None:
        """Return a control event that invalidates one accepted entry attempt.

        Ordinary EXIT is relevant only when it arrived from PLACE start through broker
        acceptance. Hard controls remain relevant whenever they arrived at or after PLACE
        start, including after a response was lost. History is required because a newer
        soft EXIT must not erase an earlier hard control inside that interval.
        """
        first = float(submit_started_epoch)
        second = float(accepted_at_epoch)
        # Legacy rows or a wall-clock correction can contain reversed timestamps. Use one
        # fail-closed policy here (also used by LiveRouter): widen to the sorted interval.
        submit_started = min(first, second)
        accepted_at = max(first, second)
        scopes = ("GLOBAL", str(engine or "").upper())
        with self.db() as c:
            row = c.execute(
                "SELECT scope,fence_epoch,event_key,kind FROM ("
                "SELECT scope,fence_epoch,event_key,kind,id AS event_order "
                "FROM control_fence_history UNION ALL "
                # Include the compatibility watermark so a concurrently running older
                # process that has not learned history cannot become invisible.
                "SELECT scope,fence_epoch,event_key,kind,0 AS event_order "
                "FROM control_fences UNION ALL "
                # Older workers still commit every control to the durable inbox. Reading
                # that journal closes the rolling-deploy gap where their one-row watermark
                # could otherwise lose HARD_FLAT after a later EXIT.
                "SELECT CASE WHEN UPPER(COALESCE(engine,'')) "
                "IN ('','GLOBAL','ACCOUNT') THEN 'GLOBAL' ELSE UPPER(engine) END,"
                "COALESCE(receipt_epoch,CAST(strftime('%s',received_at) AS REAL)),"
                "event_key,UPPER(kind),0 AS event_order "
                "FROM webhook_inbox WHERE kind IN "
                "('EXIT','HARD_FLAT','ASW_CANCEL_PENDING','ASW_TIME_FLAT')) "
                "WHERE scope IN (?,?) AND fence_epoch>=? AND ("
                "UPPER(kind) IN ('HARD_FLAT','ASW_TIME_FLAT','ASW_CANCEL_PENDING') "
                "OR fence_epoch<=?) "
                "ORDER BY CASE WHEN UPPER(kind) IN "
                "('HARD_FLAT','ASW_TIME_FLAT','ASW_CANCEL_PENDING') "
                "THEN 1 ELSE 0 END DESC,fence_epoch DESC,event_order DESC LIMIT 1",
                (*scopes, submit_started, accepted_at),
            ).fetchone()
        if row is None:
            return None
        return {'scope': str(row[0]), 'fence_epoch': float(row[1]),
                'event_key': str(row[2]), 'kind': str(row[3])}

    def set_runtime_state(self, key: str, payload: dict):
        with self.db() as c:
            c.execute(
                "INSERT INTO runtime_state(key,payload) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (key, json.dumps(payload, default=str)),
            )

    def get_runtime_state(self, key: str) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT payload FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def trip_entry_circuit(self, *, reason: str, event_key: str = "", outcome: str = "") -> dict:
        payload = {
            "open": True,
            "reason": str(reason)[:2000],
            "event_key": event_key,
            "outcome": outcome,
            "opened_at_epoch": time.time(),
        }
        self.set_runtime_state("entry_circuit", payload)
        return payload

    def trip_entry_circuit_for_attempt(
            self, attempt_key: str, expected_states: Iterable[str], *,
            reason: str, outcome: str = "") -> dict | None:
        """Atomically trip only while the named attempt still owns live uncertainty.

        EXIT and reconciliation run on independent workers. A verifier that started from
        an ACCEPTED snapshot must not open the circuit after EXIT already committed CLOSED.
        """
        expected = ((expected_states,) if isinstance(expected_states, str)
                    else tuple(str(x) for x in expected_states))
        if not expected:
            return None
        payload = {
            "open": True,
            "reason": str(reason)[:2000],
            "event_key": str(attempt_key),
            "outcome": str(outcome),
            "opened_at_epoch": time.time(),
        }
        conn = self._connect(timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" for _ in expected)
            row = conn.execute(
                f"SELECT 1 FROM entry_attempts WHERE attempt_key=? "
                f"AND state IN ({marks})",
                (str(attempt_key), *expected),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            conn.execute(
                "INSERT INTO runtime_state(key,payload) VALUES('entry_circuit',?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (json.dumps(payload, default=str),),
            )
            conn.commit()
            return payload
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reset_entry_circuit(self) -> dict:
        payload = {"open": False, "reason": "", "event_key": "", "outcome": "",
                   "reset_at_epoch": time.time()}
        self.set_runtime_state("entry_circuit", payload)
        return payload

    def entry_circuit(self) -> dict:
        return self.get_runtime_state("entry_circuit") or {"open": False}

    def unresolved_entry_attempt_count(self) -> int:
        with self.db() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM entry_attempts WHERE state IN ('PREPARED','SUBMITTING','ACCEPTED','FLATTENING')"
            ).fetchone()
        return int(row[0] if row else 0)

    def record_silver_stop_intent(self, stage: int, event_key: str = '') -> dict:
        """Durably coalesce Silver protection requests to the highest pending stage."""
        stage = int(stage)
        if stage not in {1, 2}:
            raise ValueError(f'unsupported Silver protection stage {stage}')
        now = time.time()
        conn = self._connect(timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM runtime_state WHERE key='silver_stop_intent'"
            ).fetchone()
            current = json.loads(row[0]) if row else {}
            was_pending = bool(current.get('pending'))
            current_stage = int(current.get('stage') or 0) if was_pending else 0
            raised = stage > current_stage or not was_pending
            payload = {
                'pending': True,
                'stage': max(stage, current_stage),
                'event_key': str(event_key or current.get('event_key') or ''),
                'recorded_at_epoch': (now if not was_pending else float(
                    current.get('recorded_at_epoch') or now
                )),
                'updated_at_epoch': now,
                'attempts': (0 if raised else int(current.get('attempts') or 0)),
                'next_attempt_at_epoch': (0.0 if raised else float(
                    current.get('next_attempt_at_epoch') or 0.0
                )),
                'last_error': ('' if raised else str(current.get('last_error') or '')),
            }
            conn.execute(
                "INSERT INTO runtime_state(key,payload) VALUES('silver_stop_intent',?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (json.dumps(payload, default=str),),
            )
            conn.commit()
            return payload
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def silver_stop_intent(self) -> dict:
        payload = self.get_runtime_state('silver_stop_intent') or {}
        return payload if payload.get('pending') else {}

    def defer_silver_stop_intent(self, stage: int, reason: str,
                                 delay_seconds: float) -> dict:
        """Back off one service pass without losing a newer coalesced stage."""
        now = time.time()
        conn = self._connect(timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM runtime_state WHERE key='silver_stop_intent'"
            ).fetchone()
            current = json.loads(row[0]) if row else {}
            if not current.get('pending'):
                conn.rollback()
                return {}
            # A stage-2 alert may arrive while stage 1 is being serviced. Preserve its
            # immediate eligibility instead of applying the older pass's backoff.
            if int(current.get('stage') or 0) > int(stage):
                conn.rollback()
                return current
            current.update({
                'attempts': int(current.get('attempts') or 0) + 1,
                'next_attempt_at_epoch': now + max(0.05, float(delay_seconds)),
                'updated_at_epoch': now,
                'last_error': str(reason)[:2000],
            })
            conn.execute(
                "UPDATE runtime_state SET payload=? WHERE key='silver_stop_intent'",
                (json.dumps(current, default=str),),
            )
            conn.commit()
            return current
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def clear_silver_stop_intent(self, serviced_stage: int) -> bool:
        """Clear only if no newer stage arrived during the service pass."""
        conn = self._connect(timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM runtime_state WHERE key='silver_stop_intent'"
            ).fetchone()
            current = json.loads(row[0]) if row else {}
            if (not current.get('pending')
                    or int(current.get('stage') or 0) > int(serviced_stage)):
                conn.rollback()
                return False
            current.update({
                'pending': False, 'completed_at_epoch': time.time(),
                'next_attempt_at_epoch': 0.0, 'last_error': '',
            })
            conn.execute(
                "UPDATE runtime_state SET payload=? WHERE key='silver_stop_intent'",
                (json.dumps(current, default=str),),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_org_attempt(self, account_id: str, payload: dict):
        with self.db() as c:
            c.execute("INSERT INTO org_attempts(account_id,payload) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload",
                      (account_id, json.dumps(payload)))

    def get_org_attempt(self, account_id: str) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT payload FROM org_attempts WHERE account_id=?", (account_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_asw_pending(self, pending: AswPending):
        with self.db() as c:
            c.execute("INSERT INTO asw_pending(account_id,payload) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload",
                      (pending.account_id, pending.model_dump_json()))

    def get_asw_pending(self, account_id: str) -> AswPending | None:
        with self.db() as c:
            row = c.execute("SELECT payload FROM asw_pending WHERE account_id=?", (account_id,)).fetchone()
        return AswPending.model_validate_json(row[0]) if row else None

    def all_asw_pending(self) -> list[AswPending]:
        with self.db() as c:
            rows = c.execute("SELECT payload FROM asw_pending").fetchall()
        return [AswPending.model_validate_json(r[0]) for r in rows]

    def delete_asw_pending(self, account_id: str):
        with self.db() as c:
            c.execute("DELETE FROM asw_pending WHERE account_id=?", (account_id,))

    def save_risk_state(self, state: VerifiedRiskState):
        with self.db() as c:
            row = c.execute(
                "SELECT payload FROM risk_state WHERE account_id=?", (state.account_id,)
            ).fetchone()
            if row is not None:
                try:
                    existing = VerifiedRiskState.model_validate_json(row[0])
                    if (existing.model_dump(mode='json')
                            == state.model_dump(mode='json')):
                        return
                except Exception:
                    # Replace an unreadable legacy row and invalidate the derived cache.
                    pass
            c.execute("INSERT INTO risk_state(account_id,payload) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload",
                      (state.account_id, state.model_dump_json()))
            c.execute("DELETE FROM cached_account_state WHERE account_id=?", (state.account_id,))

    def get_risk_state(self, account_id: str) -> VerifiedRiskState | None:
        with self.db() as c:
            row = c.execute("SELECT payload FROM risk_state WHERE account_id=?", (account_id,)).fetchone()
        return VerifiedRiskState.model_validate_json(row[0]) if row else None

    def all_risk_states(self) -> list[VerifiedRiskState]:
        with self.db() as c:
            rows = c.execute("SELECT payload FROM risk_state").fetchall()
        return [VerifiedRiskState.model_validate_json(r[0]) for r in rows]

    def commit_auto_onboarded_account(
            self, rule: AccountRule, risk: VerifiedRiskState,
            state: AccountState, evidence: dict) -> bool:
        """Durably publish one proven-new account and its complete entry state.

        The account does not become visible to routing until its rule, verified initial
        MLL, and fresh empty-ledger cache can be committed together.  Repeated discovery
        of the same CrossTrade account is idempotent; identity conflicts fail closed.
        """
        if rule.account_id != risk.account_id or rule.account_id != state.account_id:
            raise ValueError('auto-onboarding account identity mismatch')
        if not rule.enabled or not rule.rules_verified:
            raise ValueError('auto-onboarded account must be enabled and rules verified')
        if not risk.mll_verified or risk.mll_floor is None:
            raise ValueError('auto-onboarded prop account requires verified initial MLL')
        if not state.mll_verified or state.mll_floor is None:
            raise ValueError('auto-onboarded account state requires verified MLL')
        rule_payload = rule.model_dump_json()
        risk_payload = risk.model_dump_json()
        state_payload = state.model_dump_json()
        evidence_payload = json.dumps(evidence, default=str, sort_keys=True)
        conn = self._connect(timeout=10.0)
        try:
            conn.execute('BEGIN IMMEDIATE')
            by_id = conn.execute(
                'SELECT crosstrade_account,payload FROM auto_accounts WHERE account_id=?',
                (rule.account_id,),
            ).fetchone()
            by_name = conn.execute(
                'SELECT account_id,payload FROM auto_accounts WHERE crosstrade_account=?',
                (rule.crosstrade_account,),
            ).fetchone()
            if by_id is not None and str(by_id[0]) != rule.crosstrade_account:
                raise ValueError('auto-onboarding account_id collision')
            if by_name is not None and str(by_name[0]) != rule.account_id:
                raise ValueError('auto-onboarding CrossTrade-name collision')
            inserted = by_id is None and by_name is None
            conn.execute(
                'INSERT INTO risk_state(account_id,payload) VALUES(?,?) '
                'ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload',
                (risk.account_id, risk_payload),
            )
            conn.execute(
                'INSERT INTO cached_account_state(account_id,payload) VALUES(?,?) '
                'ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload',
                (state.account_id, state_payload),
            )
            conn.execute(
                'INSERT INTO auto_accounts(account_id,crosstrade_account,payload,evidence) '
                'VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET '
                'crosstrade_account=excluded.crosstrade_account,payload=excluded.payload,'
                'evidence=excluded.evidence,updated_at=CURRENT_TIMESTAMP',
                (rule.account_id, rule.crosstrade_account, rule_payload, evidence_payload),
            )
            conn.execute(
                'INSERT INTO auto_discovery_audit('
                'crosstrade_account,status,reason,payload) VALUES(?,?,?,?) '
                'ON CONFLICT(crosstrade_account) DO UPDATE SET '
                'status=excluded.status,reason=excluded.reason,payload=excluded.payload,'
                'updated_at=CURRENT_TIMESTAMP',
                (rule.crosstrade_account, 'ONBOARDED', '', evidence_payload),
            )
            conn.commit()
            return inserted
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def all_auto_accounts(self) -> list[AccountRule]:
        with self.db() as c:
            rows = c.execute(
                'SELECT payload FROM auto_accounts ORDER BY created_at,account_id'
            ).fetchall()
        return [AccountRule.model_validate_json(row[0]) for row in rows]

    def save_auto_discovery_audit(self, crosstrade_account: str, *, status: str,
                                  reason: str, payload: dict | None = None) -> None:
        with self.db() as c:
            c.execute(
                'INSERT INTO auto_discovery_audit('
                'crosstrade_account,status,reason,payload) VALUES(?,?,?,?) '
                'ON CONFLICT(crosstrade_account) DO UPDATE SET '
                'status=excluded.status,reason=excluded.reason,payload=excluded.payload,'
                'updated_at=CURRENT_TIMESTAMP',
                (str(crosstrade_account), str(status), str(reason)[:2000],
                 json.dumps(payload or {}, default=str, sort_keys=True)),
            )

    def all_auto_discovery_audit(self) -> list[dict]:
        with self.db() as c:
            rows = c.execute(
                'SELECT crosstrade_account,status,reason,payload,updated_at '
                'FROM auto_discovery_audit ORDER BY crosstrade_account'
            ).fetchall()
        out = []
        for name, status, reason, payload, updated_at in rows:
            try:
                detail = json.loads(payload)
            except Exception:
                detail = {}
            out.append({
                'crosstrade_account': str(name), 'status': str(status),
                'reason': str(reason), 'detail': detail,
                'updated_at': str(updated_at),
            })
        return out


    def enqueue_webhook(self, event_key: str, raw: str, kind: str, *,
                        receipt_epoch: float | None = None, priority: int = 0,
                        engine: str = "", side: str = "") -> bool:
        try:
            with self.db() as c:
                c.execute(
                    "INSERT INTO webhook_inbox(event_key,raw,kind,status,receipt_epoch,priority,engine,side) "
                    "VALUES(?,?,?,'PENDING',?,?,?,?)",
                    (event_key, raw, kind,
                     time.time() if receipt_epoch is None else float(receipt_epoch), int(priority),
                     str(engine or ""), str(side or "")),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def enqueue_control_webhook(self, event_key: str, raw: str, kind: str, *,
                                receipt_epoch: float, priority: int, engine: str,
                                side: str, fence_scope: str) -> bool:
        """Commit the control event and its admission fence in one transaction."""
        conn = self._connect(timeout=5.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM webhook_inbox WHERE event_key=?", (event_key,)
            ).fetchone() is not None:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO webhook_inbox(event_key,raw,kind,status,receipt_epoch,priority,engine,side) "
                "VALUES(?,?,?,'PENDING',?,?,?,?)",
                (event_key, raw, kind, float(receipt_epoch), int(priority),
                 str(engine or ''), str(side or '')),
            )
            scope = str(fence_scope or 'GLOBAL').upper()
            normalized_kind = str(kind or 'EXIT').upper()
            conn.execute(
                "INSERT OR IGNORE INTO control_fence_history("
                "scope,fence_epoch,event_key,kind) VALUES(?,?,?,?)",
                (scope, float(receipt_epoch), event_key, normalized_kind),
            )
            conn.execute(
                "INSERT INTO control_fences(scope,fence_epoch,event_key,kind) VALUES(?,?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET fence_epoch=excluded.fence_epoch,"
                "event_key=excluded.event_key,kind=excluded.kind "
                "WHERE excluded.fence_epoch > control_fences.fence_epoch",
                (scope, float(receipt_epoch), event_key, normalized_kind),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def defer_webhook(self, event_key: str, reason: str, delay_seconds: float = 0.5):
        with self.db() as c:
            c.execute(
                "UPDATE webhook_inbox SET status='PENDING', last_error=?, "
                "not_before_epoch=?, updated_at=CURRENT_TIMESTAMP WHERE event_key=?",
                (str(reason)[:2000], time.time() + max(0.05, float(delay_seconds)), event_key),
            )

    def recover_processing_webhooks(self) -> int:
        # A process restart can strand work in PROCESSING. Requeue it. Entry-level
        # per-account dedupe and stable broker custom order IDs remain the mutation guards.
        with self.db() as c:
            cur = c.execute(
                "UPDATE webhook_inbox SET status='PENDING', updated_at=CURRENT_TIMESTAMP "
                "WHERE status='PROCESSING'"
            )
            return int(cur.rowcount or 0)

    def _claim_next_webhook(self, *, control: bool | None = None) -> dict | None:
        conn = self._connect(timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            control_kinds = ("EXIT", "HARD_FLAT", "ASW_CANCEL_PENDING", "ASW_TIME_FLAT")
            marks = ",".join("?" for _ in control_kinds)
            if control is True:
                where = f"status='PENDING' AND not_before_epoch<=? AND kind IN ({marks})"
                params = (time.time(), *control_kinds)
            elif control is False:
                where = f"status='PENDING' AND not_before_epoch<=? AND kind NOT IN ({marks})"
                params = (time.time(), *control_kinds)
            else:
                where = "status='PENDING' AND not_before_epoch<=?"
                params = (time.time(),)
            row = conn.execute(
                "SELECT event_key,raw,kind,attempts,received_at,receipt_epoch,priority,engine,side "
                f"FROM webhook_inbox WHERE {where} "
                "ORDER BY priority DESC,receipt_epoch,rowid LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            cur = conn.execute(
                "UPDATE webhook_inbox SET status='PROCESSING', attempts=attempts+1, "
                "updated_at=CURRENT_TIMESTAMP, last_error=NULL WHERE event_key=? AND status='PENDING'",
                (row["event_key"],),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            out = dict(row)
            out["attempts"] = int(out.get("attempts") or 0) + 1
            return out
        finally:
            conn.close()

    def claim_next_webhook(self) -> dict | None:
        return self._claim_next_webhook(control=None)

    def claim_next_regular_webhook(self) -> dict | None:
        return self._claim_next_webhook(control=False)

    def claim_next_control_webhook(self) -> dict | None:
        return self._claim_next_webhook(control=True)

    def complete_webhook(self, event_key: str, result: dict):
        with self.db() as c:
            c.execute(
                "UPDATE webhook_inbox SET status='DONE', result_payload=?, last_error=NULL, "
                "updated_at=CURRENT_TIMESTAMP WHERE event_key=?",
                (json.dumps(result, default=str), event_key),
            )

    def fail_webhook(self, event_key: str, error: str):
        with self.db() as c:
            c.execute(
                "UPDATE webhook_inbox SET status='FAILED', last_error=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE event_key=?",
                (str(error)[:2000], event_key),
            )

    def webhook_rows(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self.db() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT event_key,kind,status,attempts,received_at,updated_at,last_error,result_payload "
                "FROM webhook_inbox ORDER BY received_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            if item.get("result_payload"):
                try:
                    item["result"] = json.loads(item.pop("result_payload"))
                except Exception:
                    pass
            return_rows = out
            return_rows.append(item)
        return out

    def prune_dedupe(self, retention_days: int = 30) -> int:
        with self.db() as c:
            cur = c.execute("DELETE FROM dedupe WHERE created_at < datetime('now', ?)", (f'-{int(retention_days)} days',))
            return int(cur.rowcount or 0)

    def table_inventory(self) -> list[dict]:
        out = []
        with self.db() as c:
            names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
            for name in names:
                if not str(name).replace('_','').isalnum():
                    continue
                try:
                    count = c.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                except sqlite3.Error:
                    count = None
                out.append({'table': name, 'rows': count})
        return out
