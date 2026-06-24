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
import sqlite3
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

# Google Sign-In (Этап 1, hard-gated by env flag). Read at call time so a
# dev flipping GOOGLE_AUTH_ENABLED doesn't require a Python-level reset.
# When GOOGLE_AUTH_ENABLED=0 the endpoint returns 404 before any of these
# helpers run, so the underlying google-auth library is never required.
def is_google_auth_enabled() -> bool:
    return os.environ.get("GOOGLE_AUTH_ENABLED", "0") == "1"

def google_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "") or ""

# Fail-fast at import: enabled flag without a client id is a footgun.
if is_google_auth_enabled() and not google_client_id():
    raise RuntimeError(
        "Litube: GOOGLE_AUTH_ENABLED=1 requires GOOGLE_CLIENT_ID env. "
        "Either set the OAuth 2.0 Web Client ID from console.cloud.google.com, "
        "or set GOOGLE_AUTH_ENABLED=0 (default).")

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


# ---- Google Sign-In (Этап 1, hard-gated by GOOGLE_AUTH_ENABLED) ---------
#
# Hard contract:
#   * No existing route (/api/auth/signup, /api/auth/login, /api/admin/login)
#     is touched — email/password continues to work unchanged.
#   * /api/auth/google returns 404 when GOOGLE_AUTH_ENABLED=0. The helpers
#     below are called only when the flag is on.
#   * `_google_token_verifier` is the SINGLE network/seam point. Tests
#     monkeypatch `litetube.auth._google_token_verifier` (NOT this wrapper)
#     to drive success/failure paths without network.
_GOOGLE_PLACEHOLDER_PASSWORD = "!google"
# Public: main.py's /api/auth/google endpoint reads this to set cookie TTL,
# so JWT-cookie expiry and JWT-token expiry stay in sync.
GOOGLE_JWT_HOURS = 24 * 30
_GOOGLE_ID_TOKEN_MAX_LEN = 4096  # real Google tokens are <2 KB; cap is DoS defence.


def _default_google_token_verifier(id_token_str: str, audience: str) -> dict:
    """Lazy-import google-auth and run `verify_oauth2_token`. This is the
    default value of the module-level `_google_token_verifier`. Tests
    substitute it via monkeypatch to bypass the network.

    When GOOGLE_PROXY env is set (e.g. socks5h://127.0.0.1:1080), outgoing
    HTTPS to googleapis.com is routed through that proxy. Required on
    servers that cannot reach Google directly (Russia, China, etc.).
    `requests` and `PySocks` must both be installed in the environment."""
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests

    proxy_url = os.environ.get("GOOGLE_PROXY", "") or os.environ.get("HTTPS_PROXY", "")
    if proxy_url:
        import requests as http_requests
        session = http_requests.Session()
        session.proxies = {"https": proxy_url, "http": proxy_url}
        transport = google_requests.Request(session=session)
    else:
        transport = google_requests.Request()

    return id_token.verify_oauth2_token(
        id_token_str, transport, audience=audience)


# Module-level seam for tests (and for ops to swap to a dry-run verifier
# during incident response — eg. if Google's cert store is misbehaving
# the verifier can be monkey-patched at process start).
_google_token_verifier = _default_google_token_verifier


def verify_oauth2_token(id_token_str: str, audience: str) -> dict:
    """Verify a Google ID token and return sanitized claims.

    Returns: {"sub": str, "email": str, "claims": <raw dict>}

    Failure-classification rules (don't lump them together):
      * wrong audience / expired / malformed / signature mismatch
        → ValueError → 401 invalid_google_token
      * google.auth.exceptions.GoogleAuthError → also 401 invalid_google_token
        (lazy-imported; won't crash auth.py if google-auth is missing).
      * NOT GoogleAuthError, NOT ValueError — i.e. ConnectionError,
        DNS failure, timeouts, requests.RequestException — anything from
        Google infra being unhealthy → 503 google_unreachable (operator
        reads this as "Google is down", user gets a meaningful error).
      * email_verified missing/false                                       → 403 unverified_email
      * sub or email claim missing                                         → 403 google_claims_incomplete
    """
    if not id_token_str:
        raise HTTPException(400, "id_token_required")
    if len(id_token_str) > _GOOGLE_ID_TOKEN_MAX_LEN:
        # Standard Google ID tokens are <2 KB; anything beyond 4 KB is
        # almost certainly a DoS attempt or a misconfigured client.
        raise HTTPException(400, "id_token_too_large")
    try:
        claims = _google_token_verifier(id_token_str, audience)
    except HTTPException:
        raise
    except Exception as exc:
        # Lazy-imported so this file still loads when google-auth is absent.
        try:
            from google.auth.exceptions import GoogleAuthError
            if isinstance(exc, GoogleAuthError):
                logger.warning(
                    "google token verify failed: GoogleAuthError(%s)", type(exc).__name__)
                raise HTTPException(401, "invalid_google_token") from exc
        except ImportError:
            pass
        if isinstance(exc, ValueError):
            logger.warning("google token verify failed: ValueError")
            raise HTTPException(401, "invalid_google_token") from exc
        # Network / DNS / timeout / unexpected library bugs all land here.
        logger.error(
            "google auth infrastructure error: %s", type(exc).__name__)
        raise HTTPException(503, "google_unreachable") from exc
    if claims.get("email_verified") is not True:
        raise HTTPException(403, "unverified_email")
    sub = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    if not sub or not email:
        raise HTTPException(403, "google_claims_incomplete")
    return {"sub": sub, "email": email, "claims": claims}


async def google_login(id_token_str: str) -> dict:
    """Verify a Google ID token and run the lookup/link/create flow.

    Lookup priority:
      1. By `google_sub` — if a user row carries this sub, log in.
         The row's stored email is intentionally NOT overwritten: Google's
         email claim can rotate (Workspace rename), but `sub` is immutable.
      2. By `email` — link Google identity onto an existing client row:
         * role='operator' → reject (no admin SSO via third party).
         * google_sub already set to a different sub → reject (someone is
           claiming an email that's already anchored to a different Google
           account).
         * else → set google_sub, log in. Idempotent on retry: if rc=0
           because another concurrent request linked the SAME sub, treat
           as success rather than `link_race_lost`.
      3. None of the above → create a brand-new client (trial) row with
         google_sub, password_hash=_GOOGLE_PLACEHOLDER_PASSWORD, allocate
         a 3proxy slot.

    Returns {"token": jwt, "user_id": int, "linked": bool, "created": bool}.
    """
    info = verify_oauth2_token(id_token_str, google_client_id())
    sub = info["sub"]
    email = info["email"]
    now = await db.now()

    # 1. Lookup by google_sub (the canonical, immutable identity).
    row = await db.conn().fetch_one(
        "SELECT id, role, email FROM users WHERE google_sub=?", (sub,))
    if row is not None:
        return {
            "token": _issue_token(row["id"], "client", GOOGLE_JWT_HOURS),
            "user_id": row["id"],
            "linked": False,
            "created": False,
        }

    # 2. Lookup by email — link onto existing client, reject the rest.
    row = await db.conn().fetch_one(
        "SELECT id, role, google_sub FROM users WHERE email=?", (email,))
    if row is not None:
        if row["role"] == "operator":
            # Case A from security review: never promote an operator via OAuth.
            raise HTTPException(403, "admin_sso_disabled")
        if row["google_sub"] is not None and row["google_sub"] != sub:
            # Case B: somebody recycled a Google account to land on an
            # email already linked to a different Google account. Refuse
            # rather than overwrite the anchor.
            raise HTTPException(409, "google_sub_mismatch")
        # Link google_sub onto the existing client. The
        #   `WHERE id=? AND google_sub IS NULL`
        # guard makes the UPDATE lose to a concurrent /api/auth/google
        # call that just wrote a (different) sub — only the first writer
        # produces rowcount=1.
        rc = await db.conn().execute(
            "UPDATE users SET google_sub=?, updated_at=? "
            "WHERE id=? AND google_sub IS NULL",
            (sub, now, row["id"]))
        if rc == 0:
            # Distinguish two scenarios that both produce rc=0:
            #   (a) Concurrent request linked OUR SAME sub — idempotent
            #       retry; user-action-wise nothing went wrong, just
            #       confirm and log in.
            #   (b) Concurrent request linked a DIFFERENT sub — real
            #       race; surface as 409 so the client can retry.
            verify_row = await db.conn().fetch_one(
                "SELECT id FROM users WHERE google_sub=?", (sub,))
            if verify_row is not None and verify_row["id"] == row["id"]:
                # (a) — idempotent retry, fall through to log in.
                return {
                    "token": _issue_token(row["id"], "client", GOOGLE_JWT_HOURS),
                    "user_id": row["id"],
                    "linked": True,
                    "created": False,
                }
            # (b) — surface the race for the user to retry.
            raise HTTPException(409, "google_sub_mismatch")
        return {
            "token": _issue_token(row["id"], "client", GOOGLE_JWT_HOURS),
            "user_id": row["id"],
            "linked": True,
            "created": False,
        }

    # 3. Fresh signup.
    try:
        await db.conn().execute(
            "INSERT INTO users(email, password_hash, role, status, "
            "  trial_started_at, google_sub, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (email.lower(), _GOOGLE_PLACEHOLDER_PASSWORD, "client", "trial",
             now, sub, now, now))
    except sqlite3.IntegrityError as exc:
        # Narrow catch: only UNIQUE/PK conflicts map to 409. Other exceptions
        # (locked DB, schema mismatch, programmer errors) bubble up as 500
        # so the operator noticed rather than blaming the user with a
        # misleading "email_conflict".
        logger.warning("google_login: email already taken for %s", email)
        raise HTTPException(409, "email_conflict") from exc
    new_row = await db.conn().fetch_one(
        "SELECT id FROM users WHERE email=?", (email,))
    user_id = new_row["id"]
    # Allocate a 3proxy slot, but never block sign-in on pool flakiness
    # — the existing email/password signup does the same best-effort thing.
    try:
        from . import proxy_3proxy
        await proxy_3proxy.allocate_proxy_for_user(user_id)
    except Exception:
        logger.exception("google_login: proxy allocation failed for user %d", user_id)
    return {
        "token": _issue_token(user_id, "client", GOOGLE_JWT_HOURS),
        "user_id": user_id,
        "linked": False,
        "created": True,
    }


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
