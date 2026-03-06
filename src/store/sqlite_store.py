from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from src.utils.logger import setup_logger

logger = setup_logger("sqlite_store")

ET = timezone(timedelta(hours=-5))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   TEXT NOT NULL UNIQUE,
    strategy_id TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    detail      TEXT,
    timestamp   REAL NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS strategy_states (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    state_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(strategy_id, symbol)
);

CREATE TABLE IF NOT EXISTS indicator_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    indicators  TEXT NOT NULL,
    timestamp   REAL NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_id);
CREATE INDEX IF NOT EXISTS idx_indicator_history_symbol ON indicator_history(symbol, timestamp);
"""


class SQLiteStore:
    """Persistent storage for signals, strategy states, and indicator history."""

    def __init__(self, db_path: str = "data/monitor.db") -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        logger.info("SQLite connected: %s", self._db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    # ── Signals ──

    def save_signal(
        self,
        signal_id: str,
        strategy_id: str,
        strategy_name: str,
        signal_type: str,
        symbol: str,
        detail: dict | str = "",
        timestamp: float | None = None,
    ) -> None:
        conn = self._ensure_conn()
        detail_str = json.dumps(detail) if isinstance(detail, dict) else detail
        conn.execute(
            """INSERT OR REPLACE INTO signals
               (signal_id, strategy_id, strategy_name, signal_type, symbol, detail, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (signal_id, strategy_id, strategy_name, signal_type, symbol, detail_str, timestamp or time.time()),
        )
        conn.commit()

    def get_today_signals(self) -> list[dict[str, Any]]:
        conn = self._ensure_conn()
        today_start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts = today_start.timestamp()
        rows = conn.execute(
            "SELECT * FROM signals WHERE timestamp >= ? ORDER BY timestamp",
            (start_ts,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_signals_by_strategy(self, strategy_id: str, limit: int = 50) -> list[dict]:
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT * FROM signals WHERE strategy_id = ? ORDER BY timestamp DESC LIMIT ?",
            (strategy_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Strategy states ──

    def save_strategy_states(self, states: list[dict]) -> None:
        conn = self._ensure_conn()
        for state in states:
            conn.execute(
                """INSERT OR REPLACE INTO strategy_states
                   (strategy_id, symbol, state_json, updated_at)
                   VALUES (?, ?, ?, datetime('now'))""",
                (state["strategy_id"], state["symbol"], json.dumps(state)),
            )
        conn.commit()

    def load_strategy_states(self) -> list[dict]:
        conn = self._ensure_conn()
        rows = conn.execute("SELECT state_json FROM strategy_states").fetchall()
        results: list[dict] = []
        for row in rows:
            try:
                results.append(json.loads(row["state_json"]))
            except (json.JSONDecodeError, KeyError):
                continue
        return results

    # ── Prev values (for crosses_*/turns_* continuity across restarts) ──

    def save_prev_values(self, data: dict[str, dict[str, float | None]]) -> None:
        conn = self._ensure_conn()
        conn.execute(
            """INSERT OR REPLACE INTO strategy_states
               (strategy_id, symbol, state_json, updated_at)
               VALUES ('__prev_values__', '__prev_values__', ?, datetime('now'))""",
            (json.dumps(data),),
        )
        conn.commit()

    def load_prev_values(self) -> dict[str, dict[str, float | None]]:
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT state_json FROM strategy_states WHERE strategy_id = '__prev_values__' AND symbol = '__prev_values__'"
        ).fetchone()
        if row:
            try:
                return json.loads(row["state_json"])
            except (json.JSONDecodeError, KeyError):
                pass
        return {}

    # ── Indicator history ──

    def save_indicators(
        self,
        symbol: str,
        timeframe: str,
        indicators: dict,
        timestamp: float | None = None,
    ) -> None:
        conn = self._ensure_conn()
        conn.execute(
            """INSERT INTO indicator_history (symbol, timeframe, indicators, timestamp)
               VALUES (?, ?, ?, ?)""",
            (symbol, timeframe, json.dumps(indicators), timestamp or time.time()),
        )
        conn.commit()

    def get_indicator_history(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 100,
    ) -> list[dict]:
        conn = self._ensure_conn()
        rows = conn.execute(
            """SELECT * FROM indicator_history
               WHERE symbol = ? AND timeframe = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (symbol, timeframe, limit),
        ).fetchall()
        results = []
        for row in rows:
            entry = dict(row)
            entry["indicators"] = json.loads(entry["indicators"])
            results.append(entry)
        return list(reversed(results))

    # ── Cleanup ──

    def cleanup_old_data(self, days: int = 30) -> int:
        conn = self._ensure_conn()
        cutoff = time.time() - days * 86400
        cursor = conn.execute(
            "DELETE FROM indicator_history WHERE timestamp < ?", (cutoff,)
        )
        deleted = cursor.rowcount
        conn.commit()
        if deleted:
            logger.info("Cleaned up %d old indicator records", deleted)
        return deleted
