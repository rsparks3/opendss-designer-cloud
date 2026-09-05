"""One SQLite file for everything the gateway remembers.

``Store`` owns the connection and the schema; ``Ledger`` (engine time) and
``Users`` (accounts) are thin views over it. Every write is a sub-millisecond
statement under one lock, so calls are made inline from the event loop.

What is *not* here, on purpose: circuits, results, shapes. The gateway
forwards them and forgets them.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
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
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    plan TEXT NOT NULL DEFAULT 'free',
    created REAL NOT NULL,
    last_seen REAL NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS identities (
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    email TEXT,
    created REAL NOT NULL,
    PRIMARY KEY (provider, subject)
);
CREATE INDEX IF NOT EXISTS identities_user ON identities (user_id);
CREATE TABLE IF NOT EXISTS magic_tokens (
    nonce TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    created REAL NOT NULL,
    used REAL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    customer_id TEXT NOT NULL,
    subscription_id TEXT,
    status TEXT NOT NULL DEFAULT 'none',
    current_period_end REAL,
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
    updated REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_customer ON subscriptions (customer_id);
CREATE TABLE IF NOT EXISTS stripe_events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    received REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.db.close()


# --- users -----------------------------------------------------------------

@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str | None
    plan: str
    created: float
    last_seen: float
    session_epoch: int
    disabled: bool

    @property
    def client_key(self) -> str:
        return f"user:{self.id}"


@dataclass(frozen=True)
class Identity:
    provider: str
    subject: str
    email: str | None
    created: float


def _user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(id=row["id"], email=row["email"], name=row["name"], plan=row["plan"],
                created=row["created"], last_seen=row["last_seen"],
                session_epoch=row["session_epoch"], disabled=bool(row["disabled"]))


class Users:
    def __init__(self, store: Store):
        self._s = store

    def get(self, user_id: int) -> User | None:
        with self._s.lock:
            return _user(self._s.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def by_email(self, email: str) -> User | None:
        with self._s.lock:
            return _user(self._s.db.execute(
                "SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone())

    def sign_in(self, provider: str, subject: str, email: str, name: str | None = None) -> User:
        """Find-or-create by identity, else by verified email (linking the new
        identity to the existing account), else a fresh account."""
        email = email.strip().lower()
        now = time.time()
        with self._s.lock:
            db = self._s.db
            db.execute("BEGIN")
            try:
                row = db.execute(
                    "SELECT u.* FROM identities i JOIN users u ON u.id = i.user_id"
                    " WHERE i.provider=? AND i.subject=?", (provider, subject)).fetchone()
                if row is None:
                    row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                    if row is None:
                        db.execute(
                            "INSERT INTO users (email, name, created, last_seen) VALUES (?,?,?,?)",
                            (email, name, now, now))
                        row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                    db.execute(
                        "INSERT OR IGNORE INTO identities (provider, subject, user_id, email, created)"
                        " VALUES (?,?,?,?,?)", (provider, subject, row["id"], email, now))
                db.execute("UPDATE users SET last_seen=?, name=COALESCE(name, ?) WHERE id=?",
                           (now, name, row["id"]))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return self.get(row["id"])  # type: ignore[return-value]

    def identities(self, user_id: int) -> list[Identity]:
        with self._s.lock:
            rows = self._s.db.execute(
                "SELECT provider, subject, email, created FROM identities WHERE user_id=?"
                " ORDER BY created", (user_id,)).fetchall()
        return [Identity(r["provider"], r["subject"], r["email"], r["created"]) for r in rows]

    def touch(self, user_id: int) -> None:
        with self._s.lock:
            self._s.db.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), user_id))

    def bump_epoch(self, user_id: int) -> None:
        """Invalidate every session cookie this user holds."""
        with self._s.lock:
            self._s.db.execute(
                "UPDATE users SET session_epoch = session_epoch + 1 WHERE id=?", (user_id,))

    def set_plan(self, user_id: int, plan: str) -> None:
        with self._s.lock:
            self._s.db.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))

    def count(self) -> int:
        with self._s.lock:
            return int(self._s.db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    # -- magic links --------------------------------------------------------

    def issue_magic(self, nonce: str, email: str) -> None:
        with self._s.lock:
            self._s.db.execute(
                "INSERT INTO magic_tokens (nonce, email, created) VALUES (?,?,?)",
                (nonce, email.lower(), time.time()))
            # Keep the table small: anything older than a day is long expired.
            self._s.db.execute("DELETE FROM magic_tokens WHERE created < ?", (time.time() - 86400,))

    def consume_magic(self, nonce: str, email: str) -> bool:
        """True exactly once per nonce, and only for the email it was issued to."""
        with self._s.lock:
            cur = self._s.db.execute(
                "UPDATE magic_tokens SET used=? WHERE nonce=? AND email=? AND used IS NULL",
                (time.time(), nonce, email.lower()))
            return cur.rowcount == 1


@dataclass(frozen=True)
class Subscription:
    user_id: int
    customer_id: str
    subscription_id: str | None
    status: str
    current_period_end: float | None
    cancel_at_period_end: bool
    updated: float


class Subscriptions:
    """One row per user who has ever reached Checkout; the plan is derived
    from `status` (see billing.plan_for)."""

    def __init__(self, store: Store):
        self._s = store

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Subscription | None:
        if row is None:
            return None
        return Subscription(row["user_id"], row["customer_id"], row["subscription_id"],
                            row["status"], row["current_period_end"],
                            bool(row["cancel_at_period_end"]), row["updated"])

    def get(self, user_id: int) -> Subscription | None:
        with self._s.lock:
            return self._row(self._s.db.execute(
                "SELECT * FROM subscriptions WHERE user_id=?", (user_id,)).fetchone())

    def by_customer(self, customer_id: str) -> Subscription | None:
        with self._s.lock:
            return self._row(self._s.db.execute(
                "SELECT * FROM subscriptions WHERE customer_id=?", (customer_id,)).fetchone())

    def upsert(self, *, user_id: int, customer_id: str, subscription_id: str | None,
               status: str, current_period_end: float | None, cancel_at_period_end: bool) -> None:
        with self._s.lock:
            self._s.db.execute(
                "INSERT INTO subscriptions (user_id, customer_id, subscription_id, status,"
                " current_period_end, cancel_at_period_end, updated) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET customer_id=excluded.customer_id,"
                " subscription_id=excluded.subscription_id, status=excluded.status,"
                " current_period_end=excluded.current_period_end,"
                " cancel_at_period_end=excluded.cancel_at_period_end, updated=excluded.updated",
                (user_id, customer_id, subscription_id, status, current_period_end,
                 int(cancel_at_period_end), time.time()))

    def record_event(self, event_id: str, kind: str) -> bool:
        """True the first time an event id is seen; False on a redelivery."""
        with self._s.lock:
            cur = self._s.db.execute(
                "INSERT OR IGNORE INTO stripe_events (id, type, received) VALUES (?,?,?)",
                (event_id, kind, time.time()))
            return cur.rowcount == 1

