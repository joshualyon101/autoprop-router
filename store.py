from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from models import ActiveTrade, VerifiedRiskState


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self):
        with self.db() as c:
            c.execute("CREATE TABLE IF NOT EXISTS dedupe (event_id TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            c.execute("CREATE TABLE IF NOT EXISTS active_trades (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS org_attempts (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS risk_state (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            c.execute("""CREATE TABLE IF NOT EXISTS webhook_inbox (
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
        with self.db() as c:
            c.execute("DELETE FROM active_trades WHERE account_id=?", (account_id,))

    def save_org_attempt(self, account_id: str, payload: dict):
        with self.db() as c:
            c.execute("INSERT INTO org_attempts(account_id,payload) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload",
                      (account_id, json.dumps(payload)))

    def get_org_attempt(self, account_id: str) -> dict | None:
        with self.db() as c:
            row = c.execute("SELECT payload FROM org_attempts WHERE account_id=?", (account_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_risk_state(self, state: VerifiedRiskState):
        with self.db() as c:
            c.execute("INSERT INTO risk_state(account_id,payload) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload",
                      (state.account_id, state.model_dump_json()))

    def get_risk_state(self, account_id: str) -> VerifiedRiskState | None:
        with self.db() as c:
            row = c.execute("SELECT payload FROM risk_state WHERE account_id=?", (account_id,)).fetchone()
        return VerifiedRiskState.model_validate_json(row[0]) if row else None

    def all_risk_states(self) -> list[VerifiedRiskState]:
        with self.db() as c:
            rows = c.execute("SELECT payload FROM risk_state").fetchall()
        return [VerifiedRiskState.model_validate_json(r[0]) for r in rows]


    def enqueue_webhook(self, event_key: str, raw: str, kind: str) -> bool:
        try:
            with self.db() as c:
                c.execute(
                    "INSERT INTO webhook_inbox(event_key,raw,kind,status) VALUES(?,?,?,'PENDING')",
                    (event_key, raw, kind),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def recover_processing_webhooks(self) -> int:
        # A process restart can strand work in PROCESSING. Requeue it. Entry-level
        # per-account dedupe and stable broker custom order IDs remain the mutation guards.
        with self.db() as c:
            cur = c.execute(
                "UPDATE webhook_inbox SET status='PENDING', updated_at=CURRENT_TIMESTAMP "
                "WHERE status='PROCESSING'"
            )
            return int(cur.rowcount or 0)

    def claim_next_webhook(self) -> dict | None:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT event_key,raw,kind,attempts,received_at FROM webhook_inbox "
                "WHERE status='PENDING' ORDER BY received_at,event_key LIMIT 1"
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
