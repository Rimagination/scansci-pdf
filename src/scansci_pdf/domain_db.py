"""SQLite-based domain stats storage for Sci-Hub domain rotation.

Also the single persistent home for verification-wall state (ALTCHA /
interactive gates) — scihub.py's pacing helpers read/write the wall_state
table here. Do NOT build parallel in-memory or JSON health stores: three
generations of exactly that caused constant rework (see docs/PLAYBOOK.md).
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
CREATE TABLE IF NOT EXISTS wall_state (
    domain         TEXT PRIMARY KEY,
    last_solve     REAL    NOT NULL DEFAULT 0,
    walls          INTEGER NOT NULL DEFAULT 0,
    cooldown_until REAL    NOT NULL DEFAULT 0
);
"""


def _get_db_path(config: dict[str, Any]) -> Path:
    cache_dir = Path(config.get("cache_dir", str(Path.home() / ".scansci-pdf" / "cache")))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _DB_FILENAME


@contextmanager
def _conn(config: dict[str, Any]):
    """Open, initialize, and close the stats DB per operation.

    The database is tiny and operations are short; holding connections open
    across calls kept domain_stats.db locked on Windows and broke callers'
    temp-dir cleanup. Per-operation open/close makes the module leak-free by
    construction (WAL keeps concurrent access cheap).
    """
    db_path = _get_db_path(config)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        yield conn
    finally:
        conn.close()


def load_stats(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with _conn(config) as conn:
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
    with _conn(config) as conn:
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
    with _conn(config) as conn:
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
    with _conn(config) as conn:
        conn.execute("""
            INSERT INTO probe_meta (key, value) VALUES ('_last_probe', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (str(ts),))
        conn.commit()


def get_probe_timestamp(config: dict[str, Any]) -> int:
    with _conn(config) as conn:
        row = conn.execute("SELECT value FROM probe_meta WHERE key = '_last_probe'").fetchone()
        if row:
            try:
                return int(row["value"])
            except (ValueError, TypeError):
                return 0
    return 0


def close_connection() -> None:
    """Backward-compat no-op: connections are closed after every operation."""
    return None


# ---------------------------------------------------------------------------
# Verification-wall state (ALTCHA/interactive gates) — the single persistent
# home for mirror health cooldowns. scihub.py's pacing helpers read/write this
# table; do NOT build parallel in-memory or JSON health stores (that split
# caused three generations of rework).
# ---------------------------------------------------------------------------

_WALL_DEFAULTS = {"last_solve": 0.0, "walls": 0, "cooldown_until": 0.0}


def get_wall_state(domain: str, config: dict[str, Any]) -> dict[str, float]:
    """Latest wall state for a domain (defaults when never seen)."""
    with _conn(config) as conn:
        row = conn.execute(
            "SELECT last_solve, walls, cooldown_until FROM wall_state WHERE domain = ?", (domain,)
        ).fetchone()
    if not row:
        return dict(_WALL_DEFAULTS)
    return {
        "last_solve": float(row["last_solve"]),
        "walls": int(row["walls"]),
        "cooldown_until": float(row["cooldown_until"]),
    }


def set_wall_state(
    domain: str,
    config: dict[str, Any],
    *,
    last_solve: float,
    walls: int,
    cooldown_until: float,
) -> None:
    with _conn(config) as conn:
        conn.execute(
            """INSERT INTO wall_state (domain, last_solve, walls, cooldown_until)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(domain) DO UPDATE SET last_solve=excluded.last_solve,
                 walls=excluded.walls, cooldown_until=excluded.cooldown_until""",
            (domain, float(last_solve), int(walls), float(cooldown_until)),
        )
        # Opportunistic pruning: rows untouched for a day are dead weight (the
        # mirror recovered or was abandoned long ago). NOTE: cooldown_until=0
        # is a VALID healthy state — pruning on it would delete just-reset
        # rows.
        conn.execute("DELETE FROM wall_state WHERE last_solve < ?", (time.time() - 86400,))
        conn.commit()


def reset_wall_state(config: dict[str, Any]) -> None:
    """Forget all wall cooldowns (used by tests and manual overrides)."""
    with _conn(config) as conn:
        conn.execute("DELETE FROM wall_state")
        conn.commit()
