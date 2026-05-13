"""SQLite-based domain stats storage for Sci-Hub domain rotation."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_local = threading.local()

_DB_FILENAME = "domain_stats.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS domain_stats (
    domain      TEXT PRIMARY KEY,
    success     INTEGER NOT NULL DEFAULT 0,
    fail        INTEGER NOT NULL DEFAULT 0,
    last_fail   REAL    NOT NULL DEFAULT 0,
    fail_streak INTEGER NOT NULL DEFAULT 0,
    avg_latency REAL,
    reachable   INTEGER,
    updated_at  REAL    NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS probe_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _get_db_path(config: dict[str, Any]) -> Path:
    cache_dir = Path(config.get("cache_dir", str(Path.home() / ".scansci-pdf" / "cache")))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _DB_FILENAME


def _get_conn(config: dict[str, Any]) -> sqlite3.Connection:
    db_path = _get_db_path(config)
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        _local.conn = conn
    return conn


def load_stats(config: dict[str, Any]) -> dict[str, dict[str, Any]]:

    conn = _get_conn(config)
    rows = conn.execute("SELECT * FROM domain_stats").fetchall()
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        stats[row["domain"]] = {
            "success": row["success"],
            "fail": row["fail"],
            "last_fail_time": row["last_fail"],
            "fail_streak": row["fail_streak"],
            "avg_latency_ms": row["avg_latency"],
            "reachable": bool(row["reachable"]) if row["reachable"] is not None else None,
        }
    # Also load probe metadata
    meta = conn.execute("SELECT key, value FROM probe_meta").fetchall()
    for m in meta:
        if m["key"] == "_last_probe":
            try:
                stats["_last_probe"] = int(m["value"])
            except (ValueError, TypeError):
                stats["_last_probe"] = 0
    return stats


def record_result(domain: str, ok: bool, config: dict[str, Any]) -> None:

    conn = _get_conn(config)
    now = time.time()
    conn.execute("""
        INSERT INTO domain_stats (domain, success, fail, last_fail, fail_streak, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            success     = success + excluded.success,
            fail        = fail + excluded.fail,
            last_fail   = CASE WHEN excluded.fail > 0 THEN excluded.last_fail ELSE last_fail END,
            fail_streak = CASE WHEN excluded.fail > 0 THEN fail_streak + 1 ELSE 0 END,
            updated_at  = excluded.updated_at
    """, (domain, 1 if ok else 0, 0 if ok else 1, now if not ok else 0, 1 if not ok else 0, now))
    conn.commit()


def update_probe(domain: str, reachable: bool, latency_ms: float, config: dict[str, Any]) -> None:

    conn = _get_conn(config)
    conn.execute("""
        INSERT INTO domain_stats (domain, success, fail, reachable, avg_latency, updated_at)
        VALUES (?, 0, 0, ?, ?, ?)
        ON CONFLICT(domain) DO UPDATE SET
            reachable  = excluded.reachable,
            avg_latency = excluded.avg_latency,
            updated_at  = excluded.updated_at
    """, (domain, 1 if reachable else 0, latency_ms, time.time()))
    conn.commit()


def set_probe_timestamp(config: dict[str, Any], timestamp: float | None = None) -> None:
    ts = int(timestamp) if timestamp is not None else int(time.time())
    conn = _get_conn(config)
    conn.execute("""
        INSERT INTO probe_meta (key, value) VALUES ('_last_probe', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (str(ts),))
    conn.commit()


def get_probe_timestamp(config: dict[str, Any]) -> int:

    conn = _get_conn(config)
    row = conn.execute("SELECT value FROM probe_meta WHERE key = '_last_probe'").fetchone()
    if row:
        try:
            return int(row["value"])
        except (ValueError, TypeError):
            return 0
    return 0


def close_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
