from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS account_overrides (
                account_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_registry (
                account_id TEXT PRIMARY KEY, crosstrade_account_id INTEGER,
                account_name TEXT NOT NULL, firm TEXT NOT NULL, program TEXT NOT NULL,
                phase TEXT NOT NULL, starting_balance REAL NOT NULL, max_loss REAL NOT NULL,
                profit_target REAL NOT NULL, max_micros INTEGER NOT NULL,
                drawdown_type TEXT NOT NULL, consistency_pct REAL, enabled INTEGER NOT NULL DEFAULT 0,
                rules_verified INTEGER NOT NULL DEFAULT 0, risk_ready INTEGER NOT NULL DEFAULT 0,
                profile_source TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prop_state (
                account_id TEXT PRIMARY KEY, mll_floor REAL, peak_eod_balance REAL,
                live_high_water REAL, mll_locked INTEGER NOT NULL DEFAULT 0,
                day_start_balance REAL, day_key TEXT, largest_winning_day REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS eod_marks (
                account_id TEXT NOT NULL, trading_date TEXT NOT NULL, eod_balance REAL NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(account_id, trading_date)
            );
            CREATE TABLE IF NOT EXISTS processed_events (
                event_key TEXT PRIMARY KEY, trade_id TEXT NOT NULL, event TEXT NOT NULL,
                created_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS route_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT NOT NULL,
                created_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trade_allocations (
                trade_id TEXT NOT NULL, account_id TEXT NOT NULL, account_name TEXT NOT NULL,
                instrument TEXT NOT NULL, direction TEXT NOT NULL, management_mode TEXT NOT NULL,
                initial_qty INTEGER NOT NULL, remaining_qty INTEGER NOT NULL,
                tp1_qty INTEGER NOT NULL DEFAULT 0, runner_qty INTEGER NOT NULL DEFAULT 0,
                entry REAL, stop REAL, tp1 REAL, tp2 REAL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(trade_id, account_id)
            );
            CREATE INDEX IF NOT EXISTS idx_alloc_active ON trade_allocations(account_id,instrument,status,updated_at);
            """)

    def get_prop_state(self, account_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM prop_state WHERE account_id=?", (account_id,)).fetchone()
            return dict(row) if row else None

    def upsert_prop_state(self, account_id: str, **state):
        now = datetime.now(timezone.utc).isoformat()
        existing = self.get_prop_state(account_id) or {}
        merged = {
            "mll_floor": state.get("mll_floor", existing.get("mll_floor")),
            "peak_eod_balance": state.get("peak_eod_balance", existing.get("peak_eod_balance")),
            "live_high_water": state.get("live_high_water", existing.get("live_high_water")),
            "mll_locked": int(bool(state.get("mll_locked", existing.get("mll_locked", 0)))),
            "day_start_balance": state.get("day_start_balance", existing.get("day_start_balance")),
            "day_key": state.get("day_key", existing.get("day_key")),
            "largest_winning_day": state.get("largest_winning_day", existing.get("largest_winning_day", 0.0)),
        }
        with self._lock, self._conn() as c:
            c.execute("""
            INSERT INTO prop_state(account_id,mll_floor,peak_eod_balance,live_high_water,mll_locked,day_start_balance,day_key,largest_winning_day,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET mll_floor=excluded.mll_floor,
              peak_eod_balance=excluded.peak_eod_balance, live_high_water=excluded.live_high_water,
              mll_locked=excluded.mll_locked, day_start_balance=excluded.day_start_balance,
              day_key=excluded.day_key, largest_winning_day=excluded.largest_winning_day,
              updated_at=excluded.updated_at
            """, (account_id, merged["mll_floor"], merged["peak_eod_balance"], merged["live_high_water"],
                  merged["mll_locked"], merged["day_start_balance"], merged["day_key"],
                  merged["largest_winning_day"], now))

    def has_eod_mark(self, account_id: str, trading_date: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM eod_marks WHERE account_id=? AND trading_date=?", (account_id, trading_date)).fetchone() is not None

    def record_eod_mark(self, account_id: str, trading_date: str, eod_balance: float):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO eod_marks(account_id,trading_date,eod_balance,created_at) VALUES(?,?,?,?)",
                      (account_id, trading_date, eod_balance, datetime.now(timezone.utc).isoformat()))

    def claim_event(self, event_key: str, trade_id: str, event: str, payload: dict) -> bool:
        try:
            with self._lock, self._conn() as c:
                c.execute("INSERT INTO processed_events(event_key,trade_id,event,created_at,payload) VALUES(?,?,?,?,?)",
                          (event_key, trade_id, event, datetime.now(timezone.utc).isoformat(), json.dumps(payload, separators=(",", ":"))))
            return True
        except sqlite3.IntegrityError:
            return False

    def log_route(self, trade_id: str, payload: dict):
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO route_log(trade_id,created_at,payload) VALUES(?,?,?)",
                      (trade_id, datetime.now(timezone.utc).isoformat(), json.dumps(payload, separators=(",", ":"))))

    def upsert_allocation(self, **x):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            # One AutoProp portfolio position per account/instrument. Retire older active rows.
            c.execute("UPDATE trade_allocations SET status='superseded',updated_at=? WHERE account_id=? AND instrument=? AND status IN ('submitted','active') AND trade_id<>?",
                      (now, x["account_id"], x["instrument"], x["trade_id"]))
            c.execute("""
            INSERT INTO trade_allocations(trade_id,account_id,account_name,instrument,direction,management_mode,
              initial_qty,remaining_qty,tp1_qty,runner_qty,entry,stop,tp1,tp2,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_id,account_id) DO UPDATE SET account_name=excluded.account_name,
              instrument=excluded.instrument,direction=excluded.direction,management_mode=excluded.management_mode,
              initial_qty=excluded.initial_qty,remaining_qty=excluded.remaining_qty,tp1_qty=excluded.tp1_qty,
              runner_qty=excluded.runner_qty,entry=excluded.entry,stop=excluded.stop,tp1=excluded.tp1,
              tp2=excluded.tp2,status=excluded.status,updated_at=excluded.updated_at
            """, (x["trade_id"], x["account_id"], x["account_name"], x["instrument"], x["direction"], x["management_mode"],
                  x["initial_qty"], x["remaining_qty"], x.get("tp1_qty",0), x.get("runner_qty",0), x.get("entry"),
                  x.get("stop"), x.get("tp1"), x.get("tp2"), x.get("status","submitted"), now, now))

    def get_active_allocation(self, account_id: str, instrument: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("""SELECT * FROM trade_allocations WHERE account_id=? AND instrument=?
                              AND status IN ('submitted','active') ORDER BY updated_at DESC LIMIT 1""",
                            (account_id, instrument)).fetchone()
            return dict(row) if row else None

    def update_allocation(self, trade_id: str, account_id: str, *, remaining_qty: int | None = None, stop: float | None = None, status: str | None = None):
        now = datetime.now(timezone.utc).isoformat()
        sets, vals = ["updated_at=?"], [now]
        if remaining_qty is not None: sets += ["remaining_qty=?"]; vals += [int(remaining_qty)]
        if stop is not None: sets += ["stop=?"]; vals += [float(stop)]
        if status is not None: sets += ["status=?"]; vals += [status]
        vals += [trade_id, account_id]
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE trade_allocations SET {','.join(sets)} WHERE trade_id=? AND account_id=?", vals)

    def list_active_allocations(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM trade_allocations WHERE status IN ('submitted','active') ORDER BY updated_at DESC").fetchall()]

    def get_override(self, account_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT payload FROM account_overrides WHERE account_id=?", (account_id,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def set_override(self, account_id: str, payload: dict[str, Any]):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            c.execute("""INSERT INTO account_overrides(account_id,payload,updated_at) VALUES(?,?,?)
                         ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at""",
                      (account_id, json.dumps(payload, separators=(",", ":")), now))

    def delete_override(self, account_id: str):
        with self._lock, self._conn() as c: c.execute("DELETE FROM account_overrides WHERE account_id=?", (account_id,))

    def list_overrides(self) -> dict[str, dict[str, Any]]:
        with self._conn() as c:
            return {r["account_id"]: json.loads(r["payload"]) for r in c.execute("SELECT account_id,payload FROM account_overrides").fetchall()}

    def reset_prop_state(self, account_id: str, *, mll_floor: float | None=None, peak_eod_balance: float | None=None, live_high_water: float | None=None, mll_locked: bool=False):
        with self._lock, self._conn() as c: c.execute("DELETE FROM prop_state WHERE account_id=?", (account_id,))
        self.upsert_prop_state(account_id, mll_floor=mll_floor, peak_eod_balance=peak_eod_balance,
                               live_high_water=live_high_water, mll_locked=mll_locked)

    def upsert_registry(self, cfg):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            c.execute("""
            INSERT INTO account_registry(account_id,crosstrade_account_id,account_name,firm,program,phase,starting_balance,
              max_loss,profit_target,max_micros,drawdown_type,consistency_pct,enabled,rules_verified,risk_ready,profile_source,first_seen,last_seen)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET crosstrade_account_id=excluded.crosstrade_account_id,
              account_name=excluded.account_name,firm=excluded.firm,program=excluded.program,phase=excluded.phase,
              starting_balance=excluded.starting_balance,max_loss=excluded.max_loss,profit_target=excluded.profit_target,
              max_micros=excluded.max_micros,drawdown_type=excluded.drawdown_type,consistency_pct=excluded.consistency_pct,
              enabled=excluded.enabled,rules_verified=excluded.rules_verified,risk_ready=excluded.risk_ready,
              profile_source=excluded.profile_source,last_seen=excluded.last_seen
            """, (cfg.id,cfg.crosstrade_account_id,cfg.account_name,cfg.firm,cfg.program,cfg.phase.value,cfg.starting_balance,
                  cfg.max_loss,cfg.profit_target,cfg.max_micros,cfg.drawdown_type.value,cfg.consistency_pct,int(cfg.enabled),
                  int(cfg.rules_verified),int(cfg.risk_ready),cfg.profile_source,now,now))

    def list_registry(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM account_registry ORDER BY firm,account_name").fetchall()]
