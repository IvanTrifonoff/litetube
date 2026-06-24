"""Litube SQLite layer: WAL mode, in-process serialization, retry-loop, versioned migrations.

Schema is small and stable, kept inside this module rather than a separate
migration framework. Future-proof: if/when this grows beyond ~3 versions,
move to Alembic or gateway/sqitch.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any, Iterable

import aiosqlite

logger = logging.getLogger("litetube.db")

# Schema versions. Append-only — never modify past versions.
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'client',
    status          TEXT NOT NULL DEFAULT 'trial',
    trial_started_at TEXT NOT NULL,
    paid_until      TEXT,
    banned_reason   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proxies (
    uid             TEXT PRIMARY KEY,
    host            TEXT NOT NULL,
    port            INTEGER NOT NULL,
    type            TEXT NOT NULL DEFAULT 'socks5http',
    auth_token      TEXT UNIQUE NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    owner_user_id   INTEGER REFERENCES users(id),
    is_alive        INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    last_check_at   TEXT,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS proxies_owner_idx ON proxies(owner_user_id);
CREATE INDEX IF NOT EXISTS proxies_alive_idx  ON proxies(is_alive);

CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    amount          REAL NOT NULL,
    currency        TEXT NOT NULL,
    provider        TEXT NOT NULL,
    tx_id           TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    paid_at         TEXT,
    valid_until     TEXT,
    raw_request     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(provider, tx_id)
);
CREATE INDEX IF NOT EXISTS payments_user_idx ON payments(user_id);

CREATE TABLE IF NOT EXISTS bans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    reason          TEXT,
    unbanned_at     TEXT,
    banned_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    token           TEXT UNIQUE NOT NULL,
    expires_at      TEXT NOT NULL,
    revoked_at      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS api_tokens_user_idx ON api_tokens(user_id);

CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER PRIMARY KEY,
    applied_at      TEXT NOT NULL
);
"""

# Litetube alerter state: one row per cooldown-domain signal. Written by
# both the in-process alert_loops and the host-side alert_daemon; SQLite
# WAL + UPSERT-with-WHERE cooldown check gives honest cross-process debounce.
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS alert_state (
    signal_name      TEXT PRIMARY KEY,
    incident_active  INTEGER NOT NULL DEFAULT 0,
    last_alert_iso   TEXT,
    last_alert_value TEXT,
    last_checked_iso TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_state_active_idx ON alert_state(incident_active);
"""

# Litetube activation: short-lived "code -> JWT" rows claimed by
# /api/devices/claim/complete, long-polled by /api/devices/poll. 6-digit
# numeric string PK; row is fully populated at claim time (user_id,
# claimed_jwt, claimed_ip, claimed_ua, claimed_at). expires_at_iso is the
# wall-clock cap regardless of claim activity. Indexes are tuned for the
# two hot queries: (a) cleanup sweep on expires_at_iso, (b) per-user lookup.
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS device_claims (
    code             TEXT PRIMARY KEY,
    user_id          INTEGER REFERENCES users(id),
    claimed_jwt      TEXT,
    claimed_ip       TEXT,
    claimed_ua       TEXT,
    created_at       TEXT NOT NULL,
    expires_at_iso   TEXT NOT NULL,
    claimed_at       TEXT
);
CREATE INDEX IF NOT EXISTS device_claims_expires_idx ON device_claims(expires_at_iso);
CREATE INDEX IF NOT EXISTS device_claims_user_idx    ON device_claims(user_id);
"""

# Litube V4 — Google Sign-In support (Этап 1, behind GOOGLE_AUTH_ENABLED flag).
# Nullable column + partial unique index gives us:
#   * existing email/password users continue working untouched (google_sub=NULL)
#   * first-Google-login linkages are atomic (UNIQUE check catches conflicts)
#   * no shadow-table rebuild in this micro-migration; the heavier
#     password_hash→nullable rebuild is deferred to a later stage.
# The ALTER TABLE ADD COLUMN is not idempotent at the SQL level, but the
# migration runner is gated by schema_version.version, so it runs exactly
# once per fresh DB. `WHERE google_sub IS NOT NULL` makes the index partial
# so legacy rows with NULL don't all collide on the index.
SCHEMA_V4 = """
ALTER TABLE users ADD COLUMN google_sub TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub
    ON users(google_sub) WHERE google_sub IS NOT NULL;
"""

ALL_MIGRATIONS = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4]  # Append-only — never modify past versions.


# Tunables
LOCK_RETRY_ATTEMPTS = 12
LOCK_RETRY_SLEEP_MS = 100


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---- async wrapper ----------------------------------------------------

class AsyncConn:
    """Serial async wrapper around SQLite with retry on transient locks."""
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()

    async def fetch_one(self, sql: str, args: Iterable[Any] = ()) -> Any | None:
        async with self._lock:
            for i in range(LOCK_RETRY_ATTEMPTS):
                try:
                    async with aiosqlite.connect(self.path, timeout=10.0) as c:
                        await c.execute("PRAGMA journal_mode=WAL;")
                        await c.execute("PRAGMA foreign_keys=ON;")
                        c.row_factory = aiosqlite.Row
                        async with c.execute(sql, tuple(args)) as cur:
                            row = await cur.fetchone()
                        await c.commit()
                        return row
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e) or i >= LOCK_RETRY_ATTEMPTS - 1:
                        raise
                    await asyncio.sleep(LOCK_RETRY_SLEEP_MS / 1000.0)
        return None

    async def fetch_all(self, sql: str, args: Iterable[Any] = ()) -> list:
        async with self._lock:
            for i in range(LOCK_RETRY_ATTEMPTS):
                try:
                    async with aiosqlite.connect(self.path, timeout=10.0) as c:
                        await c.execute("PRAGMA journal_mode=WAL;")
                        await c.execute("PRAGMA foreign_keys=ON;")
                        c.row_factory = aiosqlite.Row
                        async with c.execute(sql, tuple(args)) as cur:
                            rows = await cur.fetchall()
                        await c.commit()
                        return rows
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e) or i >= LOCK_RETRY_ATTEMPTS - 1:
                        raise
                    await asyncio.sleep(LOCK_RETRY_SLEEP_MS / 1000.0)
        return []

    async def execute(self, sql: str, args: Iterable[Any] = ()) -> int:
        """Execute a single SQL statement. Returns cursor.rowcount (0 if unknown).

        Callers that don't care about rowcount can still discard the int —
        this is purely additive vs the previous None return.
        """
        async with self._lock:
            for i in range(LOCK_RETRY_ATTEMPTS):
                try:
                    async with aiosqlite.connect(self.path, timeout=10.0) as c:
                        await c.execute("PRAGMA journal_mode=WAL;")
                        await c.execute("PRAGMA foreign_keys=ON;")
                        cur = await c.execute(sql, tuple(args))
                        count = cur.rowcount or 0
                        await c.commit()
                        return count
                except sqlite3.OperationalError as e:
                    if "locked" not in str(e) or i >= LOCK_RETRY_ATTEMPTS - 1:
                        raise
                    await asyncio.sleep(LOCK_RETRY_SLEEP_MS / 1000.0)


# ---- init + lifecycle -------------------------------------------------

_conn: AsyncConn | None = None


def _connect_sync(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


async def init(path: str) -> None:
    """Run pending migrations synchronously (one writer at boot is fine), then open async conn."""
    global _conn
    import os
    if path != ":memory:":
        os.makedirs(os.path.dirname(path), exist_ok=True)

    sconn = _connect_sync(path)
    try:
        table = sconn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        current = 0
        if table:
            row = sconn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] if row and row[0] else 0

        for version, sql in enumerate(ALL_MIGRATIONS, start=1):
            if version <= current:
                continue
            logger.info("applying schema migration v%d", version)
            sconn.executescript(sql)
            sconn.execute("INSERT INTO schema_version(version, applied_at) VALUES (?,?)",
                          (version, _now()))
            sconn.commit()
        logger.info("schema at v%d (after init: v%d)", current, len(ALL_MIGRATIONS))
    finally:
        sconn.close()

    _conn = AsyncConn(path)


def conn() -> AsyncConn:
    if _conn is None:
        raise RuntimeError("db not initialised — call init() first")
    return _conn


async def now() -> str:
    return _now()
