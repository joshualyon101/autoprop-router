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
