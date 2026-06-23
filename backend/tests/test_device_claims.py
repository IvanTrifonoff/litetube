"""Tests for Litetube device activation: code gen, start/poll/claim flow, expiry."""

import asyncio
import os
import time

import pytest


class TestDeviceActivation:
    """Full TV activation round-trip: start → poll → claim."""

    @pytest.mark.asyncio
    async def test_start_creates_code(self, test_db):
        from litetube import db as db_mod

        # Simulate a /start call
        now = await db_mod.now()
        code = "742195"  # use a known code
        await db_mod.conn().execute(
            "INSERT INTO device_claims(code, created_at, expires_at_iso) VALUES (?,?,?)",
            (code, now, now))

        row = await db_mod.conn().fetch_one(
            "SELECT * FROM device_claims WHERE code=?", (code,))
        assert row is not None
        assert row["code"] == "742195"

    @pytest.mark.asyncio
    async def test_code_uniqueness_enforced(self, test_db):
        from litetube import db as db_mod

        now = await db_mod.now()
        await db_mod.conn().execute(
            "INSERT INTO device_claims(code, created_at, expires_at_iso) VALUES (?,?,?)",
            ("111222", now, now))

        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            await db_mod.conn().execute(
                "INSERT INTO device_claims(code, created_at, expires_at_iso) VALUES (?,?,?)",
                ("111222", now, now))

    @pytest.mark.asyncio
    async def test_poll_returns_claimed_after_claim(self, test_db):
        from litetube import db as db_mod

        now = await db_mod.now()
        code = "333444"
        await db_mod.conn().execute(
            "INSERT INTO device_claims(code, created_at, expires_at_iso) VALUES (?,?,?)",
            (code, now, "2099-01-01T00:00:00Z"))

        # Simulate claim: set claimed_jwt (user_id=1 is the seed user from conftest)
        await db_mod.conn().execute(
            "UPDATE device_claims SET user_id=1, claimed_jwt='test.jwt.token', "
            "claimed_at=? WHERE code=?", (now, code))

        # Poll should see the claimed_jwt
        row = await db_mod.conn().fetch_one(
            "SELECT claimed_jwt, claimed_at FROM device_claims WHERE code=?", (code,))
        assert row["claimed_jwt"] == "test.jwt.token"

    @pytest.mark.asyncio
    async def test_poll_returns_expired_for_missing_code(self, test_db):
        from litetube import db as db_mod

        row = await db_mod.conn().fetch_one(
            "SELECT * FROM device_claims WHERE code=?", ("999999",))
        assert row is None

    @pytest.mark.asyncio
    async def test_poll_returns_expired_for_expired_code(self, test_db):
        from litetube import db as db_mod

        now = await db_mod.now()
        past = "2020-01-01T00:00:00Z"
        await db_mod.conn().execute(
            "INSERT INTO device_claims(code, created_at, expires_at_iso) VALUES (?,?,?)",
            ("555666", now, past))

        row = await db_mod.conn().fetch_one(
            "SELECT * FROM device_claims WHERE code=? AND expires_at_iso < ?",
            ("555666", await db_mod.now()))
        assert row is not None  # expired

    @pytest.mark.asyncio
    async def test_claim_binds_code_to_user(self, test_db):
        from litetube import db as db_mod

        now = await db_mod.now()
        code = "777888"
        await db_mod.conn().execute(
            "INSERT INTO device_claims(code, created_at, expires_at_iso) VALUES (?,?,?)",
            (code, now, "2099-01-01T00:00:00Z"))

        # Simulate claim_complete atomic UPDATE (user_id=1 is seed user)
        rc = await db_mod.conn().execute(
            "UPDATE device_claims SET user_id=?, claimed_jwt=?, claimed_ip=?, "
            "claimed_ua=?, claimed_at=? WHERE code=? AND user_id IS NULL",
            (1, "new.jwt.token", "127.0.0.1", "test-agent", now, code))
        assert rc > 0

        row = await db_mod.conn().fetch_one(
            "SELECT * FROM device_claims WHERE code=?", (code,))
        assert row["user_id"] == 1
        assert row["claimed_jwt"] == "new.jwt.token"

    @pytest.mark.asyncio
    async def test_claim_cannot_override_existing(self, test_db):
        from litetube import db as db_mod

        now = await db_mod.now()
        code = "999000"
        # Create a second user so FK to user_id=2 works
        await db_mod.conn().execute(
            "INSERT INTO users(email,password_hash,role,status,trial_started_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("second@test.local", "$2b$12$...", "client", "trial",
             now, now, now))
        await db_mod.conn().execute(
            "INSERT INTO device_claims(code, created_at, expires_at_iso) VALUES (?,?,?)",
            (code, now, "2099-01-01T00:00:00Z"))

        # First claim (user_id=1 is seed user)
        rc1 = await db_mod.conn().execute(
            "UPDATE device_claims SET user_id=?, claimed_jwt=?, claimed_ip=?, "
            "claimed_ua=?, claimed_at=? WHERE code=? AND user_id IS NULL",
            (1, "jwt.one", "127.0.0.1", "a", now, code))
        assert rc1 > 0

        # Second claim — should fail (user_id IS NOT NULL)
        rc2 = await db_mod.conn().execute(
            "UPDATE device_claims SET user_id=?, claimed_jwt=?, claimed_ip=?, "
            "claimed_ua=?, claimed_at=? WHERE code=? AND user_id IS NULL",
            (2, "jwt.two", "127.0.0.2", "b", now, code))
        assert rc2 == 0  # no rows updated

    @pytest.mark.asyncio
    async def test_jwt_one_shot_consumer(self, test_db):
        """After first poll reads claimed_jwt, it's cleared (one-shot)."""
        from litetube import db as db_mod

        now = await db_mod.now()
        code = "111333"
        # user_id=1 is seed user from conftest
        await db_mod.conn().execute(
            "INSERT INTO device_claims(code, created_at, expires_at_iso, "
            "user_id, claimed_jwt, claimed_at) VALUES (?,?,?,?,?,?)",
            (code, now, "2099-01-01T00:00:00Z", 1, "secret.jwt", now))

        # First read — JWT present
        row1 = await db_mod.conn().fetch_one(
            "SELECT claimed_jwt FROM device_claims WHERE code=?", (code,))
        assert row1["claimed_jwt"] == "secret.jwt"

        # Clear the JWT (simulating one-shot consumer)
        await db_mod.conn().execute(
            "UPDATE device_claims SET claimed_jwt=NULL WHERE code=?", (code,))

        # Second read — JWT null
        row2 = await db_mod.conn().fetch_one(
            "SELECT claimed_jwt FROM device_claims WHERE code=?", (code,))
        assert row2["claimed_jwt"] is None

    @pytest.mark.asyncio
    async def test_code_expires_after_ttl(self, test_db):
        """Code should be considered expired after TTL passes."""
        from litetube import db as db_mod
        from datetime import datetime, timedelta, timezone

        # Set expiry 2 seconds in the future (db.now() has 1s resolution)
        expires = datetime.now(timezone.utc) + timedelta(seconds=2)
        code = "222444"
        await db_mod.conn().execute(
            "INSERT INTO device_claims(code, created_at, expires_at_iso) VALUES (?,?,?)",
            (code, await db_mod.now(), expires.strftime("%Y-%m-%dT%H:%M:%SZ")))

        # Before expiry
        row = await db_mod.conn().fetch_one(
            "SELECT * FROM device_claims WHERE code=? AND expires_at_iso > ?",
            (code, await db_mod.now()))
        assert row is not None

        # Wait past the 2-second TTL
        await asyncio.sleep(2.2)

        # After expiry
        row2 = await db_mod.conn().fetch_one(
            "SELECT * FROM device_claims WHERE code=? AND expires_at_iso > ?",
            (code, await db_mod.now()))
        assert row2 is None  # expired, no longer found


class TestAPIDevicesStart:
    """FastAPI /api/devices/start endpoint."""

    @pytest.mark.asyncio
    async def test_start_returns_code_and_qr(self, test_client):
        resp = await test_client.post("/api/devices/start")
        assert resp.status_code == 200
        data = resp.json()
        assert "code" in data
        assert len(data["code"]) == 6
        assert data["code"].isdigit()
        assert "qr_url" in data
        assert "?code=" + data["code"] in data["qr_url"]
        assert data["expires_in"] == 600

    @pytest.mark.asyncio
    async def test_start_rate_limits(self, test_client):
        codes = set()
        for _ in range(20):
            resp = await test_client.post("/api/devices/start")
            assert resp.status_code == 200
            codes.add(resp.json()["code"])
        # With 10^6 space and 20 tries, duplicates are extremely unlikely
        assert len(codes) >= 19


class TestAPIDevicesClaimComplete:
    """FastAPI /api/devices/claim/complete endpoint."""

    @pytest.mark.asyncio
    async def test_requires_auth(self, test_client):
        resp = await test_client.post("/api/devices/claim/complete",
                                json={"code": "123456"})
        assert resp.status_code == 401  # no auth cookie

    @pytest.mark.asyncio
    async def test_rejects_bad_code_format(self, test_client):
        await test_client.post("/api/auth/signup",
                         json={"email": "claim@test.com", "password": "testtest"})
        resp = await test_client.post("/api/devices/claim/complete",
                                json={"code": "abc"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_code(self, test_client):
        await test_client.post("/api/auth/signup",
                         json={"email": "claim2@test.com", "password": "testtest"})
        resp = await test_client.post("/api/devices/claim/complete",
                                json={"code": "999999"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_successful_claim(self, test_client):
        # Signup → login cookie set
        await test_client.post("/api/auth/signup",
                         json={"email": "claim3@test.com", "password": "testtest"})
        # Start a code
        start = await test_client.post("/api/devices/start")
        code = start.json()["code"]

        # Claim it
        resp = await test_client.post("/api/devices/claim/complete",
                                json={"code": code})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_cannot_claim_same_code_twice(self, test_client):
        await test_client.post("/api/auth/signup",
                         json={"email": "claim4@test.com", "password": "testtest"})

        start = await test_client.post("/api/devices/start")
        code = start.json()["code"]

        resp1 = await test_client.post("/api/devices/claim/complete",
                                 json={"code": code})
        assert resp1.status_code == 200

        resp2 = await test_client.post("/api/devices/claim/complete",
                                 json={"code": code})
        assert resp2.status_code == 409  # already claimed
