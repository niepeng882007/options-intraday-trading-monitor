"""Telegram message archive — logs all outgoing messages to SQLite.

Module-level singleton with independent SQLite connection (WAL mode).
Shares `data/monitor.db` with SQLiteStore.
"""

from __future__ import annotations

import sqlite3
import time
from threading import Lock

from src.utils.logger import setup_logger

logger = setup_logger("message_archive")

_conn: sqlite3.Connection | None = None
_lock = Lock()

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS telegram_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    trigger    TEXT NOT NULL,
    content    TEXT NOT NULL,
    market     TEXT NOT NULL,
    timestamp  REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_tg_msg_market_ts
ON telegram_messages(market, timestamp)
"""


def init(db_path: str = "data/monitor.db") -> None:
    """Initialize the archive connection and create the table if needed."""
    global _conn
    with _lock:
        if _conn is not None:
            return
        _conn = sqlite3.connect(db_path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(_CREATE_TABLE)
        _conn.execute(_CREATE_INDEX)
        _conn.commit()
        logger.info("Message archive initialized (db=%s)", db_path)


def log(source: str, trigger: str, content: str, market: str) -> None:
    """Write one message record. No-op if not initialized."""
    if _conn is None:
        return
    try:
        with _lock:
            _conn.execute(
                "INSERT INTO telegram_messages (source, trigger, content, market, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (source, trigger, content, market, time.time()),
            )
            _conn.commit()
    except Exception:
        logger.warning("Failed to log message", exc_info=True)


def query(
    start_ts: float,
    end_ts: float,
    market: str | None = None,
) -> list[dict]:
    """Query messages within a time range. Returns list of dicts."""
    if _conn is None:
        return []
    try:
        with _lock:
            if market:
                cur = _conn.execute(
                    "SELECT source, trigger, content, market, timestamp "
                    "FROM telegram_messages "
                    "WHERE market = ? AND timestamp >= ? AND timestamp <= ? "
                    "ORDER BY timestamp",
                    (market, start_ts, end_ts),
                )
            else:
                cur = _conn.execute(
                    "SELECT source, trigger, content, market, timestamp "
                    "FROM telegram_messages "
                    "WHERE timestamp >= ? AND timestamp <= ? "
                    "ORDER BY timestamp",
                    (start_ts, end_ts),
                )
            rows = cur.fetchall()
    except Exception:
        logger.warning("Failed to query messages", exc_info=True)
        return []

    return [
        {
            "source": r[0],
            "trigger": r[1],
            "content": r[2],
            "market": r[3],
            "timestamp": r[4],
        }
        for r in rows
    ]


def close() -> None:
    """Close the archive connection."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
