"""The engine-time ledger: a view over the shared store.

``runs`` is the audit trail (one row per engine call); ``usage`` is the
running total per caller per period that admission reads.
"""
from __future__ import annotations

import time

from .store import Store


class Ledger:
    def __init__(self, store: Store):
        self._s = store

    def used(self, client: str, period: str) -> float:
        with self._s.lock:
            row = self._s.db.execute(
                "SELECT engine_seconds FROM usage WHERE client=? AND period=?",
                (client, period)).fetchone()
        return float(row[0]) if row else 0.0

    def record(self, *, client: str, plan: str, period: str, path: str,
               engine_seconds: float, status: str, worker: str | None,
               request_id: str | None, queue_seconds: float = 0.0) -> None:
        with self._s.lock:
            db = self._s.db
            db.execute("BEGIN")
            try:
                db.execute(
                    "INSERT INTO runs (ts, client, plan, path, engine_seconds, queue_seconds,"
                    " status, worker, request_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (time.time(), client, plan, path, engine_seconds, queue_seconds,
                     status, worker, request_id))
                db.execute(
                    "INSERT INTO usage (client, period, engine_seconds, calls) VALUES (?,?,?,1)"
                    " ON CONFLICT(client, period) DO UPDATE SET"
                    " engine_seconds = engine_seconds + excluded.engine_seconds,"
                    " calls = calls + 1",
                    (client, period, engine_seconds))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def recent(self, limit: int = 50, client: str | None = None) -> list[dict]:
        with self._s.lock:
            if client is None:
                cur = self._s.db.execute(
                    "SELECT ts, client, plan, path, engine_seconds, queue_seconds, status,"
                    " worker, request_id FROM runs ORDER BY id DESC LIMIT ?", (limit,))
            else:
                cur = self._s.db.execute(
                    "SELECT ts, client, plan, path, engine_seconds, queue_seconds, status,"
                    " worker, request_id FROM runs WHERE client=? ORDER BY id DESC LIMIT ?",
                    (client, limit))
            return [dict(row) for row in cur.fetchall()]
