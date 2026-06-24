"""Tests for Litetube SCHEMA_V5 shadow-table rebuild.

V5 makes `users.password_hash` nullable so Google Sign-In can create rows
without a meaningful bcrypt hash (those use the "!google" sentinel —
see auth.py:_GOOGLE_PLACEHOLDER_PASSWORD — and the actual NULL path opens
up at Этап 4 when email/password login for clients is retired).

These tests exercise:
  * V5 leaves password_hash notnull=0
  * pre-existing rows (incl. google_sub from V4) survive the rebuild
  * the partial unique index idx_users_google_sub is preserved
  * re-running init() is a no-op on a v5 DB (schema_version gate)
  * downgrade_to_v5_to_v4 blocks when NULL rows are present
  * downgrade_to_v5_to_v4 succeeds when no NULLs exist (round-trip)
"""

from __future__ import annotations

import sqlite3

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def tmp_db(tmp_path):
    """Fresh SQLite DB at v5 in tmp_path. Returns the path string so tests
    that need to run downgrade() against the file keep a reference to it.
    Auto-cleans by resetting the module-level _conn at teardown."""
    from litetube import db as db_mod
    db_path = str(tmp_path / "v5_test.db")
    prev_conn = db_mod._conn
    db_mod._conn = None
    try:
        await db_mod.init(db_path)
        yield db_path
    finally:
        db_mod._conn = prev_conn


# ---------------------------------------------------------------------------
# Schema invariants after V5
# ---------------------------------------------------------------------------

class TestSchemaV5Invariants:
    """After a v1-v5 init(), the users table should accept NULL password_hash
    while continuing to enforce the v4 invariants (UNIQUE email, NOT NULL on
    other role/status columns, partial unique google_sub index)."""

    @pytest.mark.asyncio
    async def test_password_hash_column_is_nullable(self, tmp_db):
        from litetube import db as db_mod
        cols = await db_mod.conn().fetch_all("PRAGMA table_info(users)")
        by_name = {c["name"]: c for c in cols}
        assert "password_hash" in by_name
        # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
        assert by_name["password_hash"]["notnull"] == 0
        # Email is still UNIQUE NOT NULL — that contract didn't change.
        assert by_name["email"]["notnull"] == 1
        # role and status stayed NOT NULL.
        assert by_name["role"]["notnull"] == 1
        assert by_name["status"]["notnull"] == 1

    @pytest.mark.asyncio
    async def test_partial_unique_google_sub_index_survives_rebuild(self, tmp_db):
        from litetube import db as db_mod
        idx = await db_mod.conn().fetch_one(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_users_google_sub'")
        assert idx is not None
        assert "google_sub IS NOT NULL" in (idx["sql"] or "")

    @pytest.mark.asyncio
    async def test_init_records_all_five_migrations(self, tmp_db):
        from litetube import db as db_mod
        rows = await db_mod.conn().fetch_all(
            "SELECT version FROM schema_version ORDER BY version")
        assert [r["version"] for r in rows] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# Data preservation through shadow-rebuild
# ---------------------------------------------------------------------------

class TestV5DataPreservation:
    """Apply V1-V4 manually with a legacy row, then run init() so V5 fires.
    Confirm all columns (incl. google_sub and google_sub from V4) survive."""

    @pytest.mark.asyncio
    async def test_legacy_row_round_trips_through_v5(self, tmp_path):
        from litetube import db as db_mod
        db_path = str(tmp_path / "legacy_v4.db")
        sconn = sqlite3.connect(db_path, timeout=10.0)
        sconn.execute("PRAGMA journal_mode=WAL;")
        try:
            # Build the pre-existing schema exactly as the migration runner would.
            sconn.executescript(db_mod.SCHEMA_V1)
            sconn.executescript(db_mod.SCHEMA_V2)
            sconn.executescript(db_mod.SCHEMA_V3)
            sconn.executescript(db_mod.SCHEMA_V4)
            # Stamp v4 (V1..V4 applied). Then insert a legacy row.
            for v in (1, 2, 3, 4):
                sconn.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES (?,?)",
                    (v, "2026-06-01T00:00:00Z"))
            sconn.execute(
                "INSERT INTO users(email, password_hash, role, status, "
                "trial_started_at, google_sub, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("legacy@example.com", "$2b$12$pre-existing-bcrypt-hash",
                 "client", "trial", "2026-01-01T00:00:00Z",
                 "google-sub-legacy-001",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            sconn.commit()
        finally:
            sconn.close()

        prev_conn = db_mod._conn
        db_mod._conn = None
        try:
            await db_mod.init(db_path)
            row = await db_mod.conn().fetch_one(
                "SELECT email, password_hash, role, status, "
                "trial_started_at, google_sub, updated_at, created_at "
                "FROM users WHERE email=?", ("legacy@example.com",))
            assert row is not None
            assert row["email"] == "legacy@example.com"
            assert row["password_hash"] == "$2b$12$pre-existing-bcrypt-hash"
            assert row["role"] == "client"
            assert row["status"] == "trial"
            assert row["google_sub"] == "google-sub-legacy-001"
            assert row["trial_started_at"] == "2026-01-01T00:00:00Z"
            v = await db_mod.conn().fetch_one(
                "SELECT MAX(version) AS v FROM schema_version")
            assert v["v"] == 5
        finally:
            db_mod._conn = prev_conn

    @pytest.mark.asyncio
    async def test_v5_accepts_null_password_hash(self, tmp_db):
        """After V5, INSERT with NULL password_hash succeeds."""
        from litetube import db as db_mod
        now = await db_mod.now()
        rc = await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "trial_started_at, created_at, updated_at) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?)",
            ("null-pw@example.com", "client", "trial", now, now, now))
        assert rc == 1
        row = await db_mod.conn().fetch_one(
            "SELECT password_hash FROM users WHERE email=?",
            ("null-pw@example.com",))
        assert row["password_hash"] is None

    @pytest.mark.asyncio
    async def test_v5_accepts_google_sentinel_password_hash(self, tmp_db):
        """After V5, the "!google" sentinel for Google-only users still works.
        Login via /api/auth/login for that row returns 401 invalid_credentials
        (verify_password catches bcrypt rejection)."""
        from litetube import db as db_mod
        now = await db_mod.now()
        rc = await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "trial_started_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("g-only@example.com", "!google", "client", "trial",
             now, now, now))
        assert rc == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestV5Idempotency:
    """Re-running init() on a v5 DB must NOT re-apply V5 (schema_version gate).
    Re-creates _conn because the second init() requires it to be None."""

    @pytest.mark.asyncio
    async def test_reinit_on_v5_does_not_duplicate_work(self, tmp_db):
        from litetube import db as db_mod
        # Sanity: we're at v5.
        v_before = await db_mod.conn().fetch_one(
            "SELECT MAX(version) AS v FROM schema_version")
        assert v_before["v"] == 5

        # Reset the global _conn so the second init() opens a fresh one
        # (init() refuses to initialise when _conn is already set).
        db_mod._conn = None
        await db_mod.init(tmp_db)
        rows = await db_mod.conn().fetch_all(
            "SELECT version, COUNT(*) AS times "
            "FROM schema_version GROUP BY version ORDER BY version")
        versions = {r["version"]: r["times"] for r in rows}
        # Each version stamped exactly once.
        assert versions == {1: 1, 2: 1, 3: 1, 4: 1, 5: 1}

    @pytest.mark.asyncio
    async def test_v5_recovers_from_orphan_users_new_table(self, tmp_path):
        """Early-window failure mode recovery: a previous crashed V5 run
        left a `users_new` table on disk with junk data. Re-running init()
        must purge that ghost via the `DROP TABLE IF EXISTS users_new`
        prefix at the top of SCHEMA_V5, then proceed cleanly to v5.
        """
        from litetube import db as db_mod
        db_path = str(tmp_path / "v5_ghost.db")
        sconn = sqlite3.connect(db_path, timeout=10.0)
        try:
            # Apply V1-V4 cleanly.
            sconn.executescript(db_mod.SCHEMA_V1)
            sconn.executescript(db_mod.SCHEMA_V2)
            sconn.executescript(db_mod.SCHEMA_V3)
            sconn.executescript(db_mod.SCHEMA_V4)
            for v in (1, 2, 3, 4):
                sconn.execute(
                    "INSERT INTO schema_version(version, applied_at) "
                    "VALUES (?,?)", (v, "2026-06-01T00:00:00Z"))
            # Simulate the early-window ghost: an orphan `users_new`
            # with stale junk rows that aren't the right schema.
            sconn.executescript("""
                CREATE TABLE users_new (
                    id INTEGER PRIMARY KEY,
                    junk TEXT
                );
                INSERT INTO users_new(id, junk) VALUES (1, 'garbage');
            """)
            sconn.commit()
        finally:
            sconn.close()

        prev_conn = db_mod._conn
        db_mod._conn = None
        try:
            await db_mod.init(db_path)
            # SCHEMA_V5's DROP IF EXISTS users_new must have wiped the
            # ghost before recreating it with the proper schema.
            cols = await db_mod.conn().fetch_all(
                "PRAGMA table_info(users)")
            by_name = {c["name"]: c for c in cols}
            assert "password_hash" in by_name  # proper v5 schema
            assert by_name["password_hash"]["notnull"] == 0
            # Status landed at v5.
            v = await db_mod.conn().fetch_one(
                "SELECT MAX(version) AS v FROM schema_version")
            assert v["v"] == 5
            # The ghost is gone, replaced by the canonical rename.
            counts = await db_mod.conn().fetch_one(
                "SELECT COUNT(*) AS c FROM sqlite_master "
                "WHERE type='table' AND name='users_new'")
            assert counts["c"] == 0
        finally:
            db_mod._conn = prev_conn


# ---------------------------------------------------------------------------
# Downgrade (V5 → V4) rollback path
# ---------------------------------------------------------------------------

class TestDowngradeV5ToV4:
    """`db.downgrade_to_v5_to_v4(path)` rolls back the shadow-table rebuild
    by restoring NOT NULL password_hash. Refuses if any user has NULL hash."""

    @pytest.mark.asyncio
    async def test_downgrade_refuses_when_null_pw_present(self, tmp_db):
        from litetube import db as db_mod
        now = await db_mod.now()
        await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "trial_started_at, created_at, updated_at) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?)",
            ("block@example.com", "client", "trial", now, now, now))
        with pytest.raises(ValueError) as exc:
            db_mod.downgrade_to_v5_to_v4(tmp_db)
        assert "NULL password_hash" in str(exc.value)
        # Schema stays at v5 — downgrade did NOT half-apply.
        v = await db_mod.conn().fetch_one(
            "SELECT MAX(version) AS v FROM schema_version")
        assert v["v"] == 5

    @pytest.mark.asyncio
    async def test_downgrade_roundtrip_when_no_nulls(self, tmp_db):
        """Pre-populate bcrypt rows (operator + client), then downgrade.
        Schema closes back to v4 with password_hash NOT NULL."""
        from litetube import db as db_mod
        from litetube.auth import hash_password
        bcrypt_pwd = hash_password("test-passphrase-123")
        now = await db_mod.now()
        await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "trial_started_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("op@example.com", bcrypt_pwd, "operator", "active",
             now, now, now))
        await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "trial_started_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("client@example.com", bcrypt_pwd, "client", "trial",
             now, now, now))
        await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "trial_started_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("linked@example.com", bcrypt_pwd, "client", "trial",
             now, now, now))

        # Run downgrade — should succeed, no NULLs present.
        db_mod.downgrade_to_v5_to_v4(tmp_db)
        # _conn still points to file, but we just rebuilt — re-open to be safe.
        await db_mod.conn().close() if False else None  # noop, file is fine

        sconn = sqlite3.connect(tmp_db, timeout=10.0)
        try:
            # v4 envelope: password_hash is NOT NULL again.
            cols = sconn.execute("PRAGMA table_info(users)").fetchall()
            pwd_col = next(c for c in cols if c[1] == "password_hash")
            assert pwd_col[3] == 1, "downgrade failed to re-add NOT NULL on password_hash"
            # schema_version capped at 4.
            v = sconn.execute(
                "SELECT MAX(version) FROM schema_version").fetchone()[0]
            assert v == 4
            # All three pre-existing rows are intact with their bcrypt hashes.
            for email in ("op@example.com", "client@example.com",
                          "linked@example.com"):
                row = sconn.execute(
                    "SELECT password_hash FROM users WHERE email=?",
                    (email,)).fetchone()
                assert row is not None
                assert row[0] == bcrypt_pwd
            # Partial unique index still present (idx_users_google_sub).
            idx = sconn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_users_google_sub'").fetchone()
            assert idx is not None
            assert "google_sub IS NOT NULL" in (idx[0] or "")
        finally:
            sconn.close()

    def test_downgrade_refuses_when_current_schema_not_v5(self, tmp_db):
        """If the DB is at v4 (already), downgrade errors out cleanly
        rather than silently re-rebuilding. We remove the v5 row from
        schema_version so the lookup returns v4, simulating the case of
        a half-applied migration that stamped up to v4 in PyPI-shipped
        code but didn't manage to apply V5 itself.
        """
        from litetube import db as db_mod
        sconn = sqlite3.connect(tmp_db, timeout=10.0)
        try:
            # Stamping schema_version lower than v5 simulates a state where
            # V5 hasn't been applied (or was reverted). v3 already exists
            # from v1-v3 → cannot UPDATE to v3 in place, so DELETE the
            # v5 row to land at v4.
            sconn.execute("DELETE FROM schema_version WHERE version=5")
            sconn.commit()
            v_after = sconn.execute(
                "SELECT MAX(version) FROM schema_version").fetchone()[0]
            assert v_after == 4
        finally:
            sconn.close()
        with pytest.raises(RuntimeError) as exc:
            db_mod.downgrade_to_v5_to_v4(tmp_db)
        assert "expected schema at v5" in str(exc.value)
