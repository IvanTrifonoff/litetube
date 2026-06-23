"""Tests for Litetube auth module: JWT, bcrypt, signup, login, token validation."""

import os
import time

import bcrypt
import pytest
from fastapi import HTTPException


class TestJWT:
    """JWT sign/verify round-trips."""

    def test_sign_and_verify(self, jwt_secret):
        from litetube.auth import jwt_sign, jwt_verify

        payload = {"user_id": 1, "role": "client", "exp": int(time.time()) + 3600}
        token = jwt_sign(payload, jwt_secret)
        result = jwt_verify(token, jwt_secret)
        assert result is not None
        assert result["user_id"] == 1
        assert result["role"] == "client"

    def test_verify_tampered_token(self, jwt_secret):
        from litetube.auth import jwt_sign, jwt_verify

        token = jwt_sign({"user_id": 1, "role": "client"}, jwt_secret)
        # Append garbage
        assert jwt_verify(token + "x", jwt_secret) is None

    def test_verify_wrong_secret(self, jwt_secret):
        from litetube.auth import jwt_sign, jwt_verify

        token = jwt_sign({"user_id": 1, "role": "client"}, jwt_secret)
        assert jwt_verify(token, "b" * 64) is None

    def test_verify_expired_token(self, jwt_secret):
        from litetube.auth import jwt_sign, jwt_verify

        payload = {"user_id": 1, "role": "client", "exp": int(time.time()) - 3600}
        token = jwt_sign(payload, jwt_secret)
        result = jwt_verify(token, jwt_secret)
        # JWT verify doesn't check exp — that's up to the caller.
        # It just verifies the signature.
        assert result is not None
        assert result["exp"] < int(time.time())

    def test_different_roles_produce_different_payloads(self, jwt_secret):
        from litetube.auth import jwt_sign, jwt_verify

        client_tok = jwt_sign({"user_id": 1, "role": "client"}, jwt_secret)
        op_tok = jwt_sign({"user_id": 1, "role": "operator"}, jwt_secret)
        c = jwt_verify(client_tok, jwt_secret)
        o = jwt_verify(op_tok, jwt_secret)
        assert c["role"] == "client"
        assert o["role"] == "operator"
        assert client_tok != op_tok


class TestBcrypt:
    """Password hashing and verification."""

    def test_hash_and_verify_match(self):
        from litetube.auth import hash_password, verify_password

        h = hash_password("correct-battery-horse-staple")
        assert verify_password("correct-battery-horse-staple", h)

    def test_verify_wrong_password(self):
        from litetube.auth import hash_password, verify_password

        h = hash_password("password-one")
        assert not verify_password("password-two", h)

    def test_hash_is_different_each_time(self):
        from litetube.auth import hash_password

        h1 = hash_password("same-password")
        h2 = hash_password("same-password")
        assert h1 != h2  # different salts

    def test_verify_broken_hash_returns_false(self):
        from litetube.auth import verify_password

        assert not verify_password("anything", "not-a-valid-bcrypt-hash")


class TestSignup:
    """Signup flow: creates user, proxies, issues token."""

    @pytest.mark.asyncio
    async def test_signup_creates_user(self, test_db):
        from litetube.auth import signup
        from litetube import db as db_mod

        result = await signup("test@example.com", "password123")
        assert "token" in result
        assert result["user_id"] > 0

        row = await db_mod.conn().fetch_one(
            "SELECT * FROM users WHERE email=?", ("test@example.com",))
        assert row is not None
        assert row["status"] == "trial"
        assert row["role"] == "client"

    @pytest.mark.asyncio
    async def test_signup_duplicate_email(self, test_db):
        from litetube.auth import signup

        await signup("dup@example.com", "password123")
        with pytest.raises(HTTPException) as exc:
            await signup("dup@example.com", "password456")
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_signup_invalid_email(self, test_db):
        from litetube.auth import signup

        with pytest.raises(HTTPException) as exc:
            await signup("not-an-email", "password123")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_signup_short_password(self, test_db):
        from litetube.auth import signup

        with pytest.raises(HTTPException) as exc:
            await signup("x@y.com", "short")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_signup_empty_fields(self, test_db):
        from litetube.auth import signup

        with pytest.raises(HTTPException) as exc:
            await signup("", "")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_signup_lowercases_email(self, test_db):
        from litetube.auth import signup
        from litetube import db as db_mod

        result = await signup("MixedCase@Example.COM", "password123")
        row = await db_mod.conn().fetch_one(
            "SELECT email FROM users WHERE id=?", (result["user_id"],))
        assert row["email"] == "mixedcase@example.com"

    @pytest.mark.asyncio
    async def test_signup_token_is_valid_jwt(self, test_db, jwt_secret):
        from litetube.auth import signup, jwt_verify

        result = await signup("jwt@example.com", "password123")
        payload = jwt_verify(result["token"], jwt_secret)
        assert payload is not None
        assert payload["role"] == "client"
        assert payload["user_id"] == result["user_id"]


class TestLogin:
    """Login flow."""

    @pytest.mark.asyncio
    async def test_login_success(self, test_db):
        from litetube.auth import signup, login

        await signup("login@example.com", "password123")
        result = await login("login@example.com", "password123")
        assert "token" in result
        assert result["hours"] == 24  # default: 1 day

    @pytest.mark.asyncio
    async def test_login_remember_me(self, test_db):
        from litetube.auth import signup, login

        await signup("remember@example.com", "password123")
        result = await login("remember@example.com", "password123", remember=True)
        assert result["hours"] == 24 * 30  # 30 days

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, test_db):
        from litetube.auth import signup, login

        await signup("wrong@example.com", "password123")
        with pytest.raises(HTTPException) as exc:
            await login("wrong@example.com", "wrongpass")
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_login_nonexistent_user(self, test_db):
        from litetube.auth import login

        with pytest.raises(HTTPException) as exc:
            await login("nobody@example.com", "password123")
        assert exc.value.status_code == 401


class TestRateLimit:
    """Per-IP rate limiting."""

    @pytest.mark.asyncio
    async def test_rate_limit_allows_under_limit(self):
        from litetube.auth import rate_limit_check

        for _ in range(5):
            assert await rate_limit_check("192.168.1.1", 10)

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_over_limit(self):
        from litetube.auth import rate_limit_check

        for _ in range(3):
            assert await rate_limit_check("192.168.1.2", 3)
        assert not await rate_limit_check("192.168.1.2", 3)

    @pytest.mark.asyncio
    async def test_rate_limit_per_ip_isolation(self):
        from litetube.auth import rate_limit_check

        assert await rate_limit_check("10.0.0.1", 1)
        assert not await rate_limit_check("10.0.0.1", 1)
        assert await rate_limit_check("10.0.0.2", 1)  # different IP
