"""Litube auth: JWT cookie + bcrypt + trial-banning + per-IP rate-limit + admin actions.

Tokens are HS256 JWTs with payload {user_id, role, exp}. Cookies are
httpOnly+secure+samesite=lax. Operator login uses a separate cookie
(litetube_operator) so a client-side compromise doesn't promote to operator.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from fastapi import Cookie, Header, HTTPException

from . import db

logger = logging.getLogger("litetube.auth")

TRIAL_DAYS = int(os.environ.get("TRIAL_DURATION_DAYS", "3"))
COOKIE_CLIENT = "litetube_client"
COOKIE_OPERATOR = "litetube_operator"

# Fail-closed at startup if JWT_SECRET is missing or too short. Without this,
# an unset secret defaults to "" and anyone can forge valid auth tokens.
JWT_SECRET = os.environ.get("JWT_SECRET", "")
if not JWT_SECRET or len(JWT_SECRET) < 32:
    raise RuntimeError(
        "Litube: JWT_SECRET env must be set to a string of >=32 chars. "
        "Generate via `openssl rand -hex 32` and put it in /srv/proxy-infra/.env.")

# Same-process rate-limit + failed-auth tracking. Per-IP, in-memory.
_rate: dict[str, list[float]] = {}
_rate_lock = asyncio.Lock()
_failed: dict[str, int] = {}
_failed_lock = asyncio.Lock()


# ---- JWT ------------------------------------------------------------

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def jwt_sign(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode("utf-8"),
                   f"{h_b64}.{p_b64}".encode("ascii"),
                   hashlib.sha256).digest()
    s_b64 = _b64url(sig)
    return f"{h_b64}.{p_b64}.{s_b64}"


def jwt_verify(token: str, secret: str) -> dict[str, Any] | None:
    try:
        h_b64, p_b64, s_b64 = token.split(".")
        sig = base64.urlsafe_b64decode(s_b64 + "===")
        expected = hmac.new(secret.encode("utf-8"),
                            f"{h_b64}.{p_b64}".encode("ascii"),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        pad = "=" * (-len(p_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(p_b64 + pad))
    except Exception:
        return None


def hash_password(plain: str) -> str:
    # cost=12 is the 2026-bcrypt baseline; rounds=10 is on the edge of
    # acceptably strong given contemporary GPU compute.
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashv: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashv.encode())
    except Exception:
        return False


# ---- rate-limit and auth-failure tracking ---------------------------

async def rate_limit_check(ip: str, per_minute: int) -> bool:
    now = time.time()
    async with _rate_lock:
        bucket = _rate.setdefault(ip, [])
        bucket[:] = [t for t in bucket if now - t < 60.0]
        if len(bucket) >= per_minute:
            return False
        bucket.append(now)
        return True


async def auth_failure_inc(ip: str) -> int:
    async with _failed_lock:
        _failed[ip] = _failed.get(ip, 0) + 1
        return _failed[ip]


# ---- trial logic ----------------------------------------------------

async def is_user_active(row) -> tuple[bool, str]:
    """Returns (active, reason). Auto-bans expired-trial users."""
    if row["role"] == "operator":
        return True, ""
    if row["status"] == "banned":
        return False, "banned"
    now = datetime.now(timezone.utc)
    if row["status"] == "trial":
        started = datetime.fromisoformat(row["trial_started_at"].replace("Z", "+00:00"))
        if (now - started).days > TRIAL_DAYS:
            await db.conn().execute(
                "UPDATE users SET status='expired', updated_at=? WHERE id=?",
                (await db.now(), row["id"]))
            # Idempotent ban insertion — protect against concurrent expired-trial
            # requests for the same user creating duplicate ban rows.
            existing = await db.conn().fetch_one(
                "SELECT 1 FROM bans WHERE user_id=? AND reason='trial_expired' "
                "AND unbanned_at IS NULL LIMIT 1", (row["id"],))
            if not existing:
                await db.conn().execute(
                    "INSERT INTO bans(user_id, reason, banned_at) VALUES (?,?,?)",
                    (row["id"], "trial_expired", await db.now()))
            return False, "trial_expired"
    if row["status"] == "active" and row["paid_until"]:
        valid = datetime.fromisoformat(row["paid_until"].replace("Z", "+00:00"))
        if valid < now:
            await db.conn().execute(
                "UPDATE users SET status='expired', updated_at=? WHERE id=?",
                (await db.now(), row["id"]))
            return False, "subscription_expired"
    return True, ""


# ---- dependencies ---------------------------------------------------

async def _decode(cookie_val: str | None, auth_header: str | None) -> dict[str, Any]:
    if not cookie_val and not auth_header:
        raise HTTPException(401, "auth_required")
    token = cookie_val or (auth_header.removeprefix("Bearer ") if auth_header else None)
    if not token:
        raise HTTPException(401, "auth_required")
    payload = jwt_verify(token, JWT_SECRET)
    if not payload:
        raise HTTPException(401, "invalid_token")
    return payload


async def client_required(
    litetube_client: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    creds = await _decode(litetube_client, authorization)
    if creds.get("role") != "client":
        raise HTTPException(403, "client_only")
    row = await db.conn().fetch_one("SELECT * FROM users WHERE id=?", (creds["user_id"],))
    if not row:
        raise HTTPException(401, "user_not_found")
    active, reason = await is_user_active(row)
    if not active:
        raise HTTPException(403, f"user_{reason}")
    return creds


async def operator_required(
    litetube_operator: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    creds = await _decode(litetube_operator, authorization)
    if creds.get("role") != "operator":
        raise HTTPException(403, "operator_only")
    return creds


# ---- signup/login/admin -------------------------

def _issue_token(user_id: int, role: str, hours: int) -> str:
    return jwt_sign(
        {"user_id": user_id, "role": role, "exp": int(time.time()) + hours * 3600},
        JWT_SECRET,
    )


async def signup(email: str, password: str):
    if not email or not password:
        raise HTTPException(400, "email_and_password_required")
    if len(password) < 8:
        raise HTTPException(400, "password_too_short")
    if "@" not in email:
        raise HTTPException(400, "invalid_email")
    h = hash_password(password)
    now = await db.now()
    try:
        await db.conn().execute(
            "INSERT INTO users(email,password_hash,role,status,trial_started_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (email.lower(), h, "client", "trial", now, now, now))
    except Exception:
        raise HTTPException(409, "email_already_used")
    row = await db.conn().fetch_one("SELECT id FROM users WHERE email=?", (email.lower(),))
    user_id = row["id"]
    from . import proxy_3proxy
    await proxy_3proxy.allocate_proxy_for_user(user_id)
    token = _issue_token(user_id, "client", int(os.environ.get("JWT_EXPIRY_HOURS", "24")))
    return {"token": token, "user_id": user_id}


async def login(email: str, password: str, remember: bool = False):
    row = await db.conn().fetch_one(
        "SELECT * FROM users WHERE email=? AND role='client'", (email.lower(),))
    if not row or not verify_password(password, row["password_hash"]):
        raise HTTPException(401, "invalid_credentials")
    hours = 24 * (30 if remember else 1)
    token = _issue_token(row["id"], "client", hours)
    return {"token": token, "hours": hours}


async def login_admin(email: str, password: str):
    row = await db.conn().fetch_one(
        "SELECT * FROM users WHERE email=? AND role='operator'", (email.lower(),))
    if not row or not verify_password(password, row["password_hash"]):
        raise HTTPException(401, "invalid_credentials")
    return {"token": _issue_token(row["id"], "operator", 8)}


async def reset_user_password(user_id: int) -> str:
    newpw = secrets.token_urlsafe(16)
    await db.conn().execute(
        "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
        (hash_password(newpw), await db.now(), user_id))
    return newpw


async def extend_user_trial(user_id: int, days: int) -> None:
    target_started = datetime.now(timezone.utc) - timedelta(days=max(0, TRIAL_DAYS - days))
    new_iso = target_started.strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.conn().execute(
        "UPDATE users SET status='trial', trial_started_at=?, updated_at=? WHERE id=?",
        (new_iso, await db.now(), user_id))


async def ban_user(user_id: int, reason: str) -> None:
    now = await db.now()
    await db.conn().execute(
        "UPDATE users SET status='banned', banned_reason=?, updated_at=? WHERE id=?",
        (reason, now, user_id))
    await db.conn().execute(
        "INSERT INTO bans(user_id,reason,banned_at) VALUES (?,?,?)",
        (user_id, reason, now))
    from . import proxy_3proxy
    await proxy_3proxy.revoke_user_proxy(user_id)


async def unban_user(user_id: int) -> None:
    now = await db.now()
    await db.conn().execute(
        "UPDATE users SET status='trial', banned_reason=NULL, trial_started_at=?, updated_at=? WHERE id=?",
        (now, now, user_id))
    await db.conn().execute(
        "UPDATE bans SET unbanned_at=? WHERE user_id=? AND unbanned_at IS NULL",
        (now, user_id))


# ---- helpers for /me -------------------

async def get_me(creds: dict) -> dict:
    row = await db.conn().fetch_one(
        "SELECT id,email,role,status,trial_started_at,paid_until,banned_reason,created_at "
        "FROM users WHERE id=?", (creds["user_id"],))
    out = dict(row)
    started = datetime.fromisoformat(out["trial_started_at"].replace("Z", "+00:00"))
    out["trial_days_left"] = max(0, TRIAL_DAYS - (datetime.now(timezone.utc) - started).days)
    return out
