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
            CREATE TABLE IF NOT EXISTS prop_state (
                account_id TEXT PRIMARY KEY,
                mll_floor REAL,
                peak_eod_balance REAL,
                live_high_water REAL,
                mll_locked INTEGER NOT NULL DEFAULT 0,
                day_start_balance REAL,
                day_key TEXT,
                largest_winning_day REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS eod_marks (
                account_id TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                eod_balance REAL NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(account_id, trading_date)
            );
            CREATE TABLE IF NOT EXISTS processed_signals (
                trade_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS route_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
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
            INSERT INTO prop_state
            (account_id,mll_floor,peak_eod_balance,live_high_water,mll_locked,day_start_balance,day_key,largest_winning_day,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
              mll_floor=excluded.mll_floor,
              peak_eod_balance=excluded.peak_eod_balance,
              live_high_water=excluded.live_high_water,
              mll_locked=excluded.mll_locked,
              day_start_balance=excluded.day_start_balance,
              day_key=excluded.day_key,
              largest_winning_day=excluded.largest_winning_day,
              updated_at=excluded.updated_at
            """, (
                account_id, merged["mll_floor"], merged["peak_eod_balance"], merged["live_high_water"],
                merged["mll_locked"], merged["day_start_balance"], merged["day_key"],
                merged["largest_winning_day"], now
            ))


    def has_eod_mark(self, account_id: str, trading_date: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM eod_marks WHERE account_id=? AND trading_date=?",
                (account_id, trading_date)
            ).fetchone()
            return row is not None

    def record_eod_mark(self, account_id: str, trading_date: str, eod_balance: float):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO eod_marks(account_id,trading_date,eod_balance,created_at) VALUES(?,?,?,?)",
                (account_id, trading_date, eod_balance, datetime.now(timezone.utc).isoformat())
            )

    def claim_signal(self, trade_id: str, payload: dict) -> bool:
        """Atomic dedupe. True only for the first copy of a trade_id."""
        try:
            with self._lock, self._conn() as c:
                c.execute(
                    "INSERT INTO processed_signals(trade_id,created_at,payload) VALUES(?,?,?)",
                    (trade_id, datetime.now(timezone.utc).isoformat(), json.dumps(payload, separators=(",", ":")))
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def log_route(self, trade_id: str, payload: dict):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO route_log(trade_id,created_at,payload) VALUES(?,?,?)",
                (trade_id, datetime.now(timezone.utc).isoformat(), json.dumps(payload, separators=(",", ":")))
            )
