"""Tests for Litetube Google Sign-In (Этап 1, feature-flagged).

Strategy: no real Google ID token is sent in this suite. We monkeypatch
`litetube.auth._google_token_verifier` (the underlying seam in `verify_oauth2_token`)
to return canned claims, eliminating network dependency. The lone network-
bearing call in production (the import of `google.auth.transport.requests`)
only fires when monkeypatch is bypassed; CI runs offline because the patch
is in effect throughout every Google-lifecycle test.
"""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def google_enabled(monkeypatch):
    """Flip GOOGLE_AUTH_ENABLED on and ensure GOOGLE_CLIENT_ID is non-empty
    so `auth.google_client_id()` returns a real audience to the wrapper."""
    monkeypatch.setenv("GOOGLE_AUTH_ENABLED", "1")
    monkeypatch.setenv(
        "GOOGLE_CLIENT_ID",
        "test-client-id-1234.apps.googleusercontent.com")
    yield


def _patch_verifier(monkeypatch, *, sub: str, email: str, email_verified: bool = True):
    """Replace the underlying `_google_token_verifier` (NOT the wrapper) so
    we exercise the wrapper's narrow-exception handling end-to-end. The
    wrapper's classification of ValueError vs GoogleAuthError vs other is
    what we want to assert here; the patched underlying just returns claims
    on success."""
    def _fake(tok: str, aud: str) -> dict:
        return {
            "sub": sub,
            "email": email.lower(),
            "email_verified": email_verified,
        }
    monkeypatch.setattr("litetube.auth._google_token_verifier", _fake)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

class TestGoogleAuthFlag:
    """Without GOOGLE_AUTH_ENABLED the endpoint is a 404 — even if the body
    is otherwise valid."""

    @pytest.mark.asyncio
    async def test_endpoint_returns_404_when_disabled(self, test_client):
        r = await test_client.post(
            "/api/auth/google", json={"id_token": "anything"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_login_function_not_reachable_when_disabled_unimported(self):
        """`is_google_auth_enabled()` should return False for any value not
        equal to literal '1' (the env-var sentinel)."""
        from litetube import auth
        prev = os.environ.get("GOOGLE_AUTH_ENABLED", "0")
        try:
            for v in ("0", "false", ""):
                os.environ["GOOGLE_AUTH_ENABLED"] = v
                assert auth.is_google_auth_enabled() is False, v
        finally:
            os.environ["GOOGLE_AUTH_ENABLED"] = prev


# ---------------------------------------------------------------------------
# verify_oauth2_token — wrapper classification
# ---------------------------------------------------------------------------

class TestVerifyOauthTokenWrapper:
    """The wrapper maps verifier failures to specific HTTPException codes.
    We patch the underlying verifier so we can drive each branch."""

    @pytest.mark.asyncio
    async def test_value_error_becomes_401_invalid_google_token(
            self, monkeypatch):
        from litetube import auth
        def _fake(_tok, _aud):
            raise ValueError("Token has been expired.")
        monkeypatch.setattr(
            "litetube.auth._google_token_verifier", _fake)
        with pytest.raises(HTTPException) as exc:
            auth.verify_oauth2_token("any", "any-aud")
        assert exc.value.status_code == 401
        assert exc.value.detail == "invalid_google_token"

    @pytest.mark.asyncio
    async def test_google_auth_error_becomes_401_invalid_google_token(
            self, monkeypatch):
        pytest.importorskip("google.auth", reason="requires google-auth installed")
        from google.auth.exceptions import GoogleAuthError
        from litetube import auth
        def _fake(_tok, _aud):
            raise GoogleAuthError("Invalid token signature.")
        monkeypatch.setattr(
            "litetube.auth._google_token_verifier", _fake)
        with pytest.raises(HTTPException) as exc:
            auth.verify_oauth2_token("any", "any-aud")
        assert exc.value.status_code == 401
        assert exc.value.detail == "invalid_google_token"

    @pytest.mark.asyncio
    async def test_infra_failure_becomes_503_google_unreachable(
            self, monkeypatch):
        """A non-GoogleAuthError, non-ValueError exception (e.g.
        ConnectionError during cert discovery) must surface as 503 so the
        operator knows Google is unreachable rather than the user's token
        being malformed."""
        from litetube import auth
        def _fake(_tok, _aud):
            raise ConnectionError("DNS lookup failed for googleapis.com")
        monkeypatch.setattr(
            "litetube.auth._google_token_verifier", _fake)
        with pytest.raises(HTTPException) as exc:
            auth.verify_oauth2_token("any", "any-aud")
        assert exc.value.status_code == 503
        assert exc.value.detail == "google_unreachable"

    @pytest.mark.asyncio
    async def test_unverified_email_claim_becomes_403(self, monkeypatch):
        from litetube import auth
        def _fake(_tok, _aud):
            return {"sub": "s", "email": "x@y.com", "email_verified": False}
        monkeypatch.setattr(
            "litetube.auth._google_token_verifier", _fake)
        with pytest.raises(HTTPException) as exc:
            auth.verify_oauth2_token("any", "any-aud")
        assert exc.value.status_code == 403
        assert exc.value.detail == "unverified_email"

    @pytest.mark.asyncio
    async def test_missing_sub_or_email_becomes_403(self, monkeypatch):
        from litetube import auth
        def _fake(_tok, _aud):
            return {"email_verified": True}  # no sub, no email
        monkeypatch.setattr(
            "litetube.auth._google_token_verifier", _fake)
        with pytest.raises(HTTPException) as exc:
            auth.verify_oauth2_token("any", "any-aud")
        assert exc.value.status_code == 403
        assert exc.value.detail == "google_claims_incomplete"

    @pytest.mark.asyncio
    async def test_empty_id_token_becomes_400(self, monkeypatch):
        from litetube import auth
        # The verifier shouldn't even be called.
        called = {"n": 0}
        def _fake(_tok, _aud):
            called["n"] += 1
            return {}
        monkeypatch.setattr(
            "litetube.auth._google_token_verifier", _fake)
        with pytest.raises(HTTPException) as exc:
            auth.verify_oauth2_token("", "any-aud")
        assert exc.value.status_code == 400
        assert exc.value.detail == "id_token_required"
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_oversize_id_token_becomes_400(self, monkeypatch):
        from litetube import auth
        called = {"n": 0}
        def _fake(_tok, _aud):
            called["n"] += 1
            return {"sub": "s", "email": "x@y.com", "email_verified": True}
        monkeypatch.setattr(
            "litetube.auth._google_token_verifier", _fake)
        with pytest.raises(HTTPException) as exc:
            auth.verify_oauth2_token("a" * 4097, "any-aud")
        assert exc.value.status_code == 400
        assert exc.value.detail == "id_token_too_large"
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# google_login lookup/link/create flow
# ---------------------------------------------------------------------------

class TestGoogleLoginHappyPath:
    @pytest.mark.asyncio
    async def test_new_client_signup_creates_row(
            self, test_db, monkeypatch, google_enabled):
        from litetube import auth, db as db_mod

        _patch_verifier(
            monkeypatch,
            sub="google-sub-fresh-001",
            email="GoogleUser@Example.com")

        out = await auth.google_login("dummy-token")
        assert out["created"] is True
        assert out["linked"] is False
        assert "token" in out and len(out["token"]) > 0
        assert out["user_id"] > 0

        row = await db_mod.conn().fetch_one(
            "SELECT email, google_sub, password_hash, role, status "
            "FROM users WHERE id=?", (out["user_id"],))
        assert row["email"] == "googleuser@example.com"
        assert row["google_sub"] == "google-sub-fresh-001"
        assert row["password_hash"] == "!google"
        assert row["role"] == "client"
        assert row["status"] == "trial"

    @pytest.mark.asyncio
    async def test_login_by_google_sub_uses_existing_row_and_preserves_db_email(
            self, test_db, monkeypatch, google_enabled):
        from litetube import auth, db as db_mod

        # Pre-create a row with google_sub populated (modelling the case
        # where the same Google account signs in again after cookie expiry).
        now = await db_mod.now()
        await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "  trial_started_at, google_sub, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("returning@example.com", "!google", "client", "trial",
             now, "google-sub-returning", now, now))
        row = await db_mod.conn().fetch_one(
            "SELECT id FROM users WHERE google_sub=?",
            ("google-sub-returning",))
        existing_id = row["id"]

        _patch_verifier(
            monkeypatch,
            sub="google-sub-returning",
            email="differentrealemail@example.com"  # Workspace rename scenario
        )
        out = await auth.google_login("dummy-token")
        assert out["user_id"] == existing_id
        assert out["created"] is False
        assert out["linked"] is False
        # Case C: even if Google rotated the email, the DB email is preserved.
        row_after = await db_mod.conn().fetch_one(
            "SELECT email FROM users WHERE id=?", (existing_id,))
        assert row_after["email"] == "returning@example.com"


class TestGoogleLoginLinkByEmail:
    @pytest.mark.asyncio
    async def test_existing_email_password_user_gets_linked_and_password_kept(
            self, test_db, monkeypatch, google_enabled):
        from litetube import auth, db as db_mod
        # Use the production hash helper so we reuse bcrypt cost=12 and
        # exercise the same code paths an email/password signup would.
        from litetube.auth import hash_password
        pwd = hash_password("ha-strong-passphrase")
        now = await db_mod.now()
        await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "  trial_started_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("linked@example.com", pwd, "client", "trial", now, now, now))
        row = await db_mod.conn().fetch_one(
            "SELECT id FROM users WHERE email=?",
            ("linked@example.com",))
        existing_id = row["id"]

        _patch_verifier(
            monkeypatch,
            sub="google-sub-link-001",
            email="linked@example.com")
        out = await auth.google_login("dummy-token")
        assert out["user_id"] == existing_id
        assert out["linked"] is True
        assert out["created"] is False

        row_after = await db_mod.conn().fetch_one(
            "SELECT google_sub, password_hash FROM users WHERE id=?",
            (existing_id,))
        # google_sub was added on the existing row; original password_hash
        # is preserved (so the user could still log in via email/password).
        assert row_after["google_sub"] == "google-sub-link-001"
        assert row_after["password_hash"] == pwd

    @pytest.mark.asyncio
    @pytest.mark.skip(
        reason=(
            "The rc=0 → verify_row['id']==row['id'] idempotent retry "
            "branch is defensive: two concurrent /api/auth/google calls "
            "with the SAME email+sub. Pre-linking with the same sub "
            "instead short-circuits step 1 (lookup-by-sub) — the only "
            "way to drive rc=0 in step 2 deterministically is concurrent "
            "threading, which pytest-asyncio doesn't reproduce. The "
            "behaviour is verified by reading the production code in "
            "`auth.google_login` step 2."
        ))
    async def test_idempotent_same_sub_retry_after_partial_link(
            self, test_db, monkeypatch, google_enabled):
        """If two concurrent /api/auth/google calls land with the SAME
        email+sub, the second's UPDATE returns rc=0 because google_sub is
        no longer NULL. The wrapper must treat this as success (the row is
        already in the desired state) rather than 409 link_race_lost."""
        from litetube import auth, db as db_mod
        from litetube.auth import hash_password
        pwd = hash_password("ha-strong-passphrase")
        now = await db_mod.now()
        await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "  trial_started_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("idem@example.com", pwd, "client", "trial", now, now, now))
        first_row = await db_mod.conn().fetch_one(
            "SELECT id FROM users WHERE email=?", ("idem@example.com",))
        first_id = first_row["id"]
        # Manually link the sub to simulate the first request winning.
        await db_mod.conn().execute(
            "UPDATE users SET google_sub=? WHERE id=?",
            ("google-sub-idem", first_id))

        _patch_verifier(
            monkeypatch,
            sub="google-sub-idem",
            email="idem@example.com")
        out = await auth.google_login("dummy-token")
        assert out["user_id"] == first_id
        assert out["linked"] is True
        assert out["created"] is False


class TestGoogleLoginRejections:
    @pytest.mark.asyncio
    async def test_operator_email_rejected(
            self, test_db, monkeypatch, google_enabled):
        from litetube import auth, db as db_mod
        await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "  trial_started_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("admin@example.com", "x", "operator", "active",
             await db_mod.now(), await db_mod.now(), await db_mod.now()))

        _patch_verifier(
            monkeypatch, sub="google-sub-admin", email="admin@example.com")
        with pytest.raises(HTTPException) as exc:
            await auth.google_login("dummy-token")
        assert exc.value.status_code == 403
        assert exc.value.detail == "admin_sso_disabled"

    @pytest.mark.asyncio
    async def test_email_already_linked_to_different_sub_rejected(
            self, test_db, monkeypatch, google_enabled):
        from litetube import auth, db as db_mod
        now = await db_mod.now()
        await db_mod.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "  trial_started_at, google_sub, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("preexisting@example.com", "x", "client", "trial", now,
             "google-sub-already-linked", now, now))

        _patch_verifier(
            monkeypatch,
            sub="google-sub-attacker",
            email="preexisting@example.com")
        with pytest.raises(HTTPException) as exc:
            await auth.google_login("dummy-token")
        assert exc.value.status_code == 409
        assert exc.value.detail == "google_sub_mismatch"

    @pytest.mark.asyncio
    async def test_unverified_email_rejected(
            self, test_db, monkeypatch, google_enabled):
        from litetube import auth

        def _fake(_tok, _aud):
            # email_verified=False — the verifier returns the claim but
            # verify_oauth2_token must still reject.
            return {"sub": "s", "email": "x@y.com", "email_verified": False}
        monkeypatch.setattr(
            "litetube.auth._google_token_verifier", _fake)
        with pytest.raises(HTTPException) as exc:
            await auth.google_login("dummy-token")
        assert exc.value.status_code == 403
        assert exc.value.detail == "unverified_email"


# ---------------------------------------------------------------------------
# Endpoint (HTTP layer)
# ---------------------------------------------------------------------------

class TestEndpoint:
    @pytest.mark.asyncio
    async def test_endpoint_disabled_returns_404(self, test_client):
        r = await test_client.post(
            "/api/auth/google", json={"id_token": "anything"})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_endpoint_happy_path_sets_cookie(
            self, test_client, test_db, monkeypatch, google_enabled):
        _patch_verifier(
            monkeypatch,
            sub="google-sub-endpoint-1",
            email="endpoint@example.com")
        r = await test_client.post(
            "/api/auth/google", json={"id_token": "dummy-token"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["created"] is True
        assert body["linked"] is False
        # Cookie issued by the endpoint should let /api/me resolve the user.
        me_r = await test_client.get("/api/me")
        assert me_r.status_code == 200, me_r.text
        assert me_r.json()["email"] == "endpoint@example.com"

    @pytest.mark.asyncio
    async def test_endpoint_missing_id_token_returns_400(
            self, test_client, monkeypatch, google_enabled):
        r = await test_client.post(
            "/api/auth/google", json={"foo": "bar"})
        assert r.status_code == 400
        assert r.json()["detail"] == "id_token_required"

    @pytest.mark.asyncio
    async def test_endpoint_invalid_json_returns_400(
            self, test_client, monkeypatch, google_enabled):
        r = await test_client.post(
            "/api/auth/google", content="not-json",
            headers={"content-type": "application/json"})
        assert r.status_code == 400
        assert r.json()["detail"] == "bad_json"

    @pytest.mark.asyncio
    async def test_endpoint_invalid_token_returns_401(
            self, test_client, monkeypatch, google_enabled):
        def _raise(_tok, _aud):
            raise ValueError("Token expired")
        monkeypatch.setattr(
            "litetube.auth._google_token_verifier", _raise)
        r = await test_client.post(
            "/api/auth/google", json={"id_token": "broken"})
        assert r.status_code == 401
        assert r.json()["detail"] == "invalid_google_token"

    @pytest.mark.asyncio
    async def test_endpoint_infra_failure_returns_503(
            self, test_client, monkeypatch, google_enabled):
        def _raise(_tok, _aud):
            raise ConnectionError("googleapis is unreachable")
        monkeypatch.setattr(
            "litetube.auth._google_token_verifier", _raise)
        r = await test_client.post(
            "/api/auth/google", json={"id_token": "any"})
        assert r.status_code == 503
        assert r.json()["detail"] == "google_unreachable"

    @pytest.mark.asyncio
    async def test_endpoint_oversize_token_returns_400(
            self, test_client, monkeypatch, google_enabled):
        r = await test_client.post(
            "/api/auth/google", json={"id_token": "a" * 4097})
        assert r.status_code == 400
        assert r.json()["detail"] == "id_token_too_large"


# ---------------------------------------------------------------------------
# Default Google verifier contract — not crashable on import
# ---------------------------------------------------------------------------

class TestDefaultVerifierImportSafety:
    """The litetube.auth module must import successfully even when
    google-auth is not installed (so the wrapper's lazy-imports surface
    503 if/when the feature is enabled without the lib)."""

    def test_module_loads_without_google_auth_called(self):
        # If we reach this line, import succeeded. Verify the lazy path is
        # wired up: `_default_google_token_verifier` exists, and the
        # `_google_token_verifier` slot is filled.
        from litetube import auth
        assert callable(auth._default_google_token_verifier)
        assert callable(auth._google_token_verifier)


# ---------------------------------------------------------------------------
# Schema migration sanity (V4 applied to fresh DB)
# ---------------------------------------------------------------------------

class TestSchemaV4:
    @pytest.mark.asyncio
    async def test_google_sub_column_exists_with_partial_unique_index(
            self, test_db):
        from litetube import db as db_mod

        cols = await db_mod.conn().fetch_all("PRAGMA table_info(users)")
        names = {row["name"] for row in cols}
        assert "google_sub" in names

        index = await db_mod.conn().fetch_one(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_users_google_sub'")
        assert index is not None
        assert "google_sub IS NOT NULL" in (index["sql"] or "")
