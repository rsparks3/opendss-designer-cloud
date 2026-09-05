"""The engine-time ledger: one SQLite file, two tables.

``runs`` is the audit trail (one row per engine call); ``usage`` is the
running total per caller per period that admission reads. Sub-millisecond
writes, so calls are made inline from the event loop under a lock rather
than through a thread.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class Ledger:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY,
                    ts REAL NOT NULL,
                    client TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    path TEXT NOT NULL,
                    engine_seconds REAL NOT NULL,
                    queue_seconds REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    worker TEXT,
                    request_id TEXT
                );
                CREATE INDEX IF NOT EXISTS runs_client_ts ON runs (client, ts);
                CREATE TABLE IF NOT EXISTS usage (
                    client TEXT NOT NULL,
                    period TEXT NOT NULL,
                    engine_seconds REAL NOT NULL DEFAULT 0,
                    calls INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (client, period)
                );
            """)

    def used(self, client: str, period: str) -> float:
        with self._lock:
            row = self._db.execute(
                "SELECT engine_seconds FROM usage WHERE client=? AND period=?",
                (client, period)).fetchone()
        return float(row[0]) if row else 0.0

    def record(self, *, client: str, plan: str, period: str, path: str,
               engine_seconds: float, status: str, worker: str | None,
               request_id: str | None, queue_seconds: float = 0.0) -> None:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute(
                    "INSERT INTO runs (ts, client, plan, path, engine_seconds, queue_seconds,"
                    " status, worker, request_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (time.time(), client, plan, path, engine_seconds, queue_seconds,
                     status, worker, request_id))
                self._db.execute(
                    "INSERT INTO usage (client, period, engine_seconds, calls) VALUES (?,?,?,1)"
                    " ON CONFLICT(client, period) DO UPDATE SET"
                    " engine_seconds = engine_seconds + excluded.engine_seconds,"
                    " calls = calls + 1",
                    (client, period, engine_seconds))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            cur = self._db.execute(
                "SELECT ts, client, plan, path, engine_seconds, queue_seconds, status, worker,"
                " request_id FROM runs ORDER BY id DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()
