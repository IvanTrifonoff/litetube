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

# Litube V5 — Drop NOT NULL on users.password_hash (Этап 2).
# Drops the constraint via SQLite's shadow-table rebuild pattern: SQLite
# has no ALTER TABLE DROP NOT NULL, so we build a parallel `users_new`,
# copy each row by explicit column list, swap names, and re-create the
# V4 partial unique index (the DROP TABLE nukes it). PRAGMA foreign_keys
# is OFF'd around the rebuild so child rows (`proxies`, `bans`, `payments`,
# `device_claims`) referencing `users(id)` aren't cascaded by the DROP.
#
# Atomicity caveat: this runs via `sconn.executescript(...)` which auto-
# commits between statement groups, so a mid-script crash could leave the
# DB in `users_new` state. schema_version is stamped AFTER the script
# succeeds, so re-running init() is safe. Worst-case recovery is the
# explicit `downgrade_to_v4()` function below + a manual cutover.
#
# Statement ordering rationale:
#   1. PRAGMA foreign_keys=OFF first (defensive — child rows referencing
#      users shouldn't be required for V1-V4 layout, but FK pragma can
#      flip between processes).
#   2. CREATE TABLE users_new with the new shape (password_hash TEXT,
#      no NOT NULL).
#   3. INSERT INTO users_new SELECT (explicit columns so a future
#      migration adding/removing columns doesn't silently break layout).
#   4. DROP TABLE users (also drops idx_users_google_sub from V4).
#   5. ALTER TABLE users_new RENAME TO users (re-attaches the canonical
#      name; child FK re-resolve to the new table).
#   6. CREATE UNIQUE INDEX IF NOT EXISTS — idempotent, recreates the V4
#      partial index on the freshly-renamed table.
#   7. PRAGMA foreign_keys=ON resumes enforcement for callers.
SCHEMA_V5 = """
-- Re-runnable against a half-applied previous attempt: a "ghost" users_new
-- from an earlier crash is purged here so we can CREATE users_new without
-- collision. This handles the EARLY-window failure mode (crash before
-- DROP TABLE users).
--
-- LATE-window failure mode (crash AFTER DROP TABLE users but BEFORE
-- ALTER TABLE users_new RENAME TO users) is NOT auto-recoverable from
-- SQL alone: the source-of-truth `users` is gone and `users_new` is
-- partial. The mitigation is operator-driven: after a V5 crash, run
--   sqlite3 /srv/proxy-infra/db/litetube.db
--   .schema users_new   # inspect partial contents
-- then either
--   a) re-populate users_new from a backup + ALTER TABLE users_new
--      RENAME TO users, or
--   b) downgrade_to_v5_to_v4(...) (if partial copy completed) + git revert.
-- Documented in HOMELAB_DEPENDENCIES.md / HOMELAB_FIX_PLAN.md as part
-- of incident response. Do NOT add silent recovery loops that could
-- mask data drift.
DROP TABLE IF EXISTS users_new;

PRAGMA foreign_keys=OFF;

CREATE TABLE users_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    email            TEXT UNIQUE NOT NULL,
    password_hash    TEXT,
    role             TEXT NOT NULL DEFAULT 'client',
    status           TEXT NOT NULL DEFAULT 'trial',
    trial_started_at TEXT NOT NULL,
    paid_until       TEXT,
    banned_reason    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    google_sub       TEXT
);

INSERT INTO users_new (
    id, email, password_hash, role, status, trial_started_at,
    paid_until, banned_reason, created_at, updated_at, google_sub
)
SELECT
    id, email, password_hash, role, status, trial_started_at,
    paid_until, banned_reason, created_at, updated_at, google_sub
FROM users;

DROP TABLE users;
ALTER TABLE users_new RENAME TO users;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub
    ON users(google_sub) WHERE google_sub IS NOT NULL;

PRAGMA foreign_keys=ON;
"""

ALL_MIGRATIONS = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5]  # Append-only — never modify past versions.


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


# ---- manual rollback (V5 → V4) ----------------------------------------
# Not part of the normal migration runner. Call explicitly from a shell if
# a bad V5 deployment needs to be undone mid-incident.
#
#   python3 -c "import sys; sys.path.insert(0, '/srv/proxy-infra/backend/api'); \
#               from litetube import db; db.downgrade_to_v5_to_v4('/srv/proxy-infra/db/litetube.db')"
#
# Refuses to run if any user has a NULL password_hash (which would mean
# Google-only users — rebuilding with NOT NULL loses their login path).
# Operator rows are unaffected: init_operator.py writes a bcrypt hash on
# every insert/update, so they always pass the constraint.
def downgrade_to_v5_to_v4(path: str) -> None:
    """Emergency V5 → V4 downgrade: restores NOT NULL on users.password_hash.

    Refuses to run if any current user has password_hash IS NULL
    (Google-only users created via /api/auth/google). Backfill those
    rows with bcrypt-hashed placeholder passwords before downgrading.
    """
    sconn = sqlite3.connect(path, timeout=10.0)
    try:
        current = sconn.execute(
            "SELECT MAX(version) FROM schema_version").fetchone()
        cur_v = current[0] if current and current[0] else 0
        if cur_v != 5:
            raise RuntimeError(
                f"downgrade_to_v5_to_v4: expected schema at v5, found v{cur_v}; "
                "nothing to do.")
        null_count = sconn.execute(
            "SELECT COUNT(*) FROM users WHERE password_hash IS NULL").fetchone()[0]
        if null_count > 0:
            raise ValueError(
                f"downgrade_to_v5_to_v4: refused — {null_count} rows have "
                "NULL password_hash (Google-only users). Backfill or delete "
                "those rows before downgrading. Operator accounts "
                "(`role='operator'`) are not affected.")
        # Cleanup any ghost users_old from a half-applied previous run.
        sconn.executescript("""
            DROP TABLE IF EXISTS users_old;
            PRAGMA foreign_keys=OFF;
            CREATE TABLE users_old (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                email            TEXT UNIQUE NOT NULL,
                password_hash    TEXT NOT NULL,
                role             TEXT NOT NULL DEFAULT 'client',
                status           TEXT NOT NULL DEFAULT 'trial',
                trial_started_at TEXT NOT NULL,
                paid_until       TEXT,
                banned_reason    TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                google_sub       TEXT
            );
            INSERT INTO users_old (
                id, email, password_hash, role, status, trial_started_at,
                paid_until, banned_reason, created_at, updated_at, google_sub
            )
            SELECT
                id, email, password_hash, role, status, trial_started_at,
                paid_until, banned_reason, created_at, updated_at, google_sub
            FROM users;
            DROP TABLE users;
            ALTER TABLE users_old RENAME TO users;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub
                ON users(google_sub) WHERE google_sub IS NOT NULL;
            DELETE FROM schema_version WHERE version=5;
            PRAGMA foreign_keys=ON;
        """)
        sconn.commit()
        logger.warning(
            "downgrade_to_v5_to_v4: rolled back to v4 — Google-only users "
            "(if any were NULL before this call) are preserved; verify "
            "current password_hash column is NOT NULL via "
            "`PRAGMA table_info(users)`.")
    finally:
        sconn.close()
