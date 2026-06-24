"""Litube FastAPI entrypoint. Wires routes for client auth, billing, /proxy/refresh,
operator admin, plus a couple of static page endpoints. Lifespan spawns the
health-checker coroutine and the 3proxy cfg-reload worker.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, alerter, alert_loops, auth, billing, db, health_checker, proxy_3proxy

logging.basicConfig(
    level=os.environ.get("LITETUBE_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("litetube.main")


# ----------------------------------------------------------------------
# Lifespan: bootstrap DB, spawn background tasks
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = os.environ.get("LITETUBE_DB_PATH", "/srv/proxy-infra/db/litetube.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    await db.init(db_path)
    logger.info("db initialised at %s", db_path)
    proxy_3proxy.bootstrap()
    logger.info("3proxy baseline ready")
    await proxy_3proxy._ensure_async_runtime()

    health_task = asyncio.create_task(health_checker.run(), name="health")
    reload_task = asyncio.create_task(proxy_3proxy.reload_worker(), name="reload")
    alert_proxy_task = asyncio.create_task(alert_loops.proxy_down_loop(), name="alert-proxy-down")
    alert_daily_task = asyncio.create_task(alert_loops.daily_zero_loop(), name="alert-daily-zero")
    logger.info("background tasks scheduled: health, reload, alert-proxy-down, alert-daily-zero")
    try:
        yield
    finally:
        for t in (health_task, reload_task, alert_proxy_task, alert_daily_task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        logger.info("litetube shut down cleanly")


app = FastAPI(title="Litetube", version=__version__, lifespan=lifespan)
HERE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# Cache rendered HTML per (filename, version, google-flag-state). Invalidation
# happens on process restart plus on any feature-flag flip or client id change
# — Google Sign-In rendering is gated by `is_google_auth_enabled()` and the
# actual `GOOGLE_CLIENT_ID` value, so the cache MUST depend on both or a flip
# would serve stale HTML. Upper bound ~files × versions × flag_vals × client_id_vals
# (≈ 20 entries across process lifetime; flip events are rare; LRU unwarranted).
_RENDER_CACHE: dict[tuple, str] = {}


def _google_signin_block() -> str:
    """Return the HTML+JS block for the Google Identity Services button,
    conditionally populated when the feature flag is on AND a client id is
    configured. Empty string otherwise so the placeholder substitution in
    `_render()` leaves a clean gap (no leftover `{{GOOGLE_SIGNIN_BLOCK}}`
    text leaks into served HTML).
    """
    if not auth.is_google_auth_enabled():
        return ""
    cid = auth.google_client_id()
    if not cid:
        return ""
    # Defense-in-depth: cid is operator-controlled (env) but if it contains
    # a quote or angle-bracket the rendered data-client_id attribute could
    # break (and become an HTML-injection vector when combined with future
    # template logic). html.escape with quote=True is safe.
    cid_h = html.escape(cid, quote=True)
    # The script tag MUST be present: Google's GIS library only loads when
    # this script executes. Without it, the g_id_signin div renders empty
    # and no popup ever appears.
    #
    # Onerror fallback for Russia reachability: if the GIS CDN is blocked
    # the user sees a friendly "temporarily unavailable" message instead
    # of a silent broken button. Triggers unconditionally on script load
    # failure (network error, DNS block, 5xx).
    return (
        '<div class="oauth-divider mt16"><span>или войдите через Google</span></div>\n'
        '<div class="google-signin-block">\n'
        '  <div id="g_id_onload" data-client_id="' + cid_h + '"\n'
        '       data-callback="handleGoogleCredential" data-auto_prompt="false"></div>\n'
        '  <div class="g_id_signin" data-type="standard" data-size="large"\n'
        '       data-text="signin_with" data-shape="rectangular" data-theme="dark"\n'
        '       data-logo_alignment="left"></div>\n'
        '</div>\n'
        '<script src="https://accounts.google.com/gsi/client" async defer\n'
        '        onerror="var e=document.getElementById(\'google-signin-fallback\');if(e)e.style.display=\'block\'"></script>\n'
        '<div id="google-signin-fallback" style="display:none;text-align:center;font-size:12px;color:var(--text-muted);margin-top:8px;padding:6px 10px;border:1px solid var(--input-border);border-radius:8px">Google Sign-In временно недоступен — используйте email и пароль</div>\n'
        '<script>\n'
        'function handleGoogleCredential(response){\n'
        '  fetch("/api/auth/google",{method:"POST",headers:{"content-type":"application/json"},\n'
        '    body:JSON.stringify({id_token:response.credential}),credentials:"same-origin"})\n'
        '  .then(function(r){\n'
        '    if(!r.ok){r.json().catch(function(){return null;}).then(function(j){\n'
        '      alert("Google sign-in failed: "+(j&&j.detail?j.detail:r.status));\n'
        '    });return;}\n'
        '    var c=new URLSearchParams(location.search).get("code");\n'
        '    if(c){\n'
        '      fetch("/api/devices/claim/complete",{method:"POST",\n'
        '        headers:{"content-type":"application/json"},body:JSON.stringify({code:c}),\n'
        '        credentials:"same-origin"}).then(function(){\n'
        '        window.location.href="/?google_oauth=1&claimed="+encodeURIComponent(c);\n'
        '      }).catch(function(){window.location.href="/?google_oauth=1&claimed_unack="+encodeURIComponent(c);});\n'
        '      return;\n'
        '    }\n'
        '    window.location.href="/?google_oauth=1";\n'
        '  }).catch(function(e){alert("Network error during Google sign-in: "+e);});\n'
        '}\n'
        '</script>'
    )


def _render(name: str) -> str:
    """Read a static HTML file and substitute `{{VERSION}}` plus the
    conditionally-rendered `{{GOOGLE_SIGNIN_BLOCK}}` placeholder. Cache
    key includes feature-flag state so a `GOOGLE_AUTH_ENABLED` flip is
    picked up on the next request without a process restart.
    """
    cache_key = (
        name,
        __version__,
        auth.is_google_auth_enabled(),
        auth.google_client_id(),
    )
    if cache_key not in _RENDER_CACHE:
        text = (HERE / "static" / name).read_text(encoding="utf-8")
        text = text.replace("{{VERSION}}", __version__)
        text = text.replace("{{GOOGLE_SIGNIN_BLOCK}}", _google_signin_block())
        _RENDER_CACHE[cache_key] = text
    return _RENDER_CACHE[cache_key] 


# ----------------------------------------------------------------------
# Mirror uvicorn's access log into a file in the mounted /var/log/litetube
# volume so the host-side alert_daemon (scripts/alert_daemon.py) can tail
# it for /api/admin/* 5xx events even when FastAPI itself is the failure.
# ----------------------------------------------------------------------
def _install_uvicorn_access_logfile() -> None:
    try:
        path = os.environ.get("UVICORN_ACCESS_LOG_PATH", "/var/log/litetube/uvicorn.log")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            from uvicorn.logging import AccessFormatter
            formatter = AccessFormatter("%(asctime)s [%(levelname)s] %(client_addr)s - \"%(request_line)s\" %(status_code)s")
        except Exception:
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(formatter)
        access_logger = logging.getLogger("uvicorn.access")
        # Idempotent: if reloads have re-installed us, don't double-append.
        already = any(
            isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == handler.baseFilename
            for h in access_logger.handlers)
        if not already:
            access_logger.addHandler(handler)
            access_logger.info("uvicorn access logfile attached: %s", path)
    except Exception:
        logger.exception("could not install uvicorn access logfile handler")


_install_uvicorn_access_logfile()


# ----------------------------------------------------------------------
# Middleware: per-IP rate-limit
# ----------------------------------------------------------------------
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    xff = request.headers.get("x-forwarded-for")
    if xff:
        ip = xff.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"
    per_min = int(os.environ.get("RATE_LIMIT_PER_IP_PER_MIN", "60"))
    if not await auth.rate_limit_check(ip, per_min):
        return JSONResponse({"error": "rate_limited"}, status_code=429)
    return await call_next(request)


# ----------------------------------------------------------------------
# Middleware: admin-5xx capture → alerter (cooldown'd in alerter.emit).
# Detects both intentionally-raised HTTPException(status>=500) and any
# uncaught exception that propagates out of the endpoint, then routes to
# the same alert_state[admin_5xx] row the host-side alert_daemon writes,
# so the two processes cooperatively debounce.
# ----------------------------------------------------------------------
@app.middleware("http")
async def admin_5xx_capture(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception as exc:
        if request.url.path.startswith("/api/admin/"):
            try:
                await alerter.emit(
                    "admin_5xx",
                    subject=f"[LITETUBE] admin UNCAUGHT on {request.url.path}",
                    body=(f"Path: {request.url.path}\n"
                          f"Method: {request.method}\n"
                          f"Exception: {type(exc).__name__}: {exc}"),
                    value=f"path={request.url.path};method={request.method};status=uncaught")
            except Exception:
                logger.exception("admin_5xx alerter emit failed")
        raise

    if response.status_code >= 500 and request.url.path.startswith("/api/admin/"):
        try:
            await alerter.emit(
                "admin_5xx",
                subject=f"[LITETUBE] admin {response.status_code} on {request.url.path}",
                body=(f"Path: {request.url.path}\n"
                      f"Method: {request.method}\n"
                      f"Status: {response.status_code}"),
                value=f"path={request.url.path};method={request.method};status={response.status_code}")
        except Exception:
            logger.exception("admin_5xx alerter emit failed")
    return response


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _set_cookie(response: JSONResponse, name: str, token: str, hours: int = 24, *, admin: bool = False) -> None:
    response.set_cookie(
        key=name, value=token,
        max_age=hours * 3600,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("LITETUBE_ENV", "production") != "development",
        path="/",
    )


# ----------------------------------------------------------------------
# Client routes
# ----------------------------------------------------------------------
@app.post("/api/auth/signup")
async def api_signup(request: Request):
    body = await request.json()
    out = await auth.signup(body.get("email", ""), body.get("password", ""))
    response = JSONResponse({"ok": True, "redirect": "/"})
    _set_cookie(response, auth.COOKIE_CLIENT, out["token"], hours=int(os.environ.get("JWT_EXPIRY_HOURS", "24")))
    return response


@app.post("/api/auth/login")
async def api_login(request: Request):
    body = await request.json()
    out = await auth.login(body.get("email", ""), body.get("password", ""), body.get("remember_me", False))
    response = JSONResponse({"ok": True})
    _set_cookie(response, auth.COOKIE_CLIENT, out["token"], hours=out["hours"])
    return response


@app.post("/api/auth/logout")
async def api_logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE_CLIENT)
    return r


@app.post("/api/auth/google")
async def api_google_login(request: Request):
    """Google Sign-In for clients (Этап 1, feature-flagged).

    Behaviour:
      * GOOGLE_AUTH_ENABLED=0 (default) → endpoint is invisible: 404. No
        network, no schema lookup, no client id required.
      * GOOGLE_AUTH_ENABLED=1          → verifies the Google ID token,
        runs the lookup/link/create flow defined in `auth.google_login`,
        and sets the same `litetube_client` cookie as /api/auth/login.
        Cookie lifetime is 30 days (OAuth users generally expect persistent
        sessions across device restarts).

    No field other than `id_token` is trusted from the client — the entire
    identity decision is driven by Google's signed claims.

    TODO(Этап 3 / 4): When flipping GOOGLE_AUTH_ENABLED=1 in production,
    also add `client_max_body_size 32k;` to the litetube.trfnv.ru nginx
    vhost (or set Starlette `body_size_limit` in main.py) — Starlette has
    no default body cap, so a multi-MB POST hits `request.json()` without
    limit. Rate-limit middleware already protects against floods; the body
    cap is defence-in-depth against memory-pressure DoS.
    """
    if not auth.is_google_auth_enabled():
        raise HTTPException(404, "not_found")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad_json")
    # Type-coerce: a client posting `{"id_token": 12345}` (a JSON number)
    # would otherwise crash `.strip()` with AttributeError → 500. Coerce
    # non-strings to "" so the empty/oversize cases land clean at 400.
    raw = body.get("id_token", "")
    if not isinstance(raw, str):
        raw = ""
    id_token_str = raw.strip()
    # Length cap is enforced inside auth.verify_oauth2_token; the endpoint
    # stays focused on routing/cookie concerns. Both 400-from-empty and
    # 400-from-oversize are surfaced with the same JSON contract so the
    # frontend can show a single "token invalid" state.
    out = await auth.google_login(id_token_str)
    response = JSONResponse({
        "ok": True,
        "user_id": out["user_id"],
        "linked": out.get("linked", False),
        "created": out.get("created", False),
    })
    # Cookie TTL is the same as the JWT itself, taken from the canonical
    # constant in auth.py so future tuning happens in one place.
    _set_cookie(
        response, auth.COOKIE_CLIENT, out["token"], hours=auth.GOOGLE_JWT_HOURS)
    return response


@app.get("/api/me")
async def api_me(creds=Depends(auth.client_required)):
    info = await auth.get_me(creds)
    info["currency"] = "RUB"  # default for client UI
    return info


@app.get("/api/proxy/refresh")
async def api_proxy_refresh(creds=Depends(auth.client_required)):
    return await proxy_3proxy.refresh_token(creds)


@app.get("/api/proxy/pool")
async def api_proxy_pool(creds=Depends(auth.client_required)):
    """TV-client: ordered list of proxies to parallel-test. Same auth cookie
    / Bearer header used by /api/me and /api/proxy/refresh. Per-user quotas
    come from the user's allocated 3proxy row."""
    return await proxy_3proxy.proxy_pool(creds)


@app.post("/api/billing/pay")
async def api_billing_pay(request: Request, creds=Depends(auth.client_required)):
    body = await request.json() if (await request.body()) else {}
    user = await db.conn().fetch_one("SELECT email FROM users WHERE id=?", (creds["user_id"],))
    return await billing.create_payment(creds["user_id"], user["email"], body.get("currency", "RUB"))


@app.post("/api/billing/webhook")
async def api_billing_webhook(request: Request):
    """Robokassa server-to-server callback. Plaintext reply required."""
    form = {k: v for k, v in (await request.form()).items()}
    body, status = await billing.handle_webhook(form)
    return Response(content=body, media_type="text/plain", status_code=status)


# ----------------------------------------------------------------------
# Operator routes
# ----------------------------------------------------------------------
@app.post("/api/admin/login")
async def api_admin_login(request: Request):
    body = await request.json()
    out = await auth.login_admin(body.get("email", ""), body.get("password", ""))
    r = JSONResponse({"ok": True})
    _set_cookie(r, auth.COOKIE_OPERATOR, out["token"], hours=8, admin=True)
    return r


@app.post("/api/admin/logout")
async def api_admin_logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE_OPERATOR)
    return r


@app.get("/api/admin/users")
async def api_admin_users(_=Depends(auth.operator_required)):
    rows = await db.conn().fetch_all(
        "SELECT id,email,role,status,trial_started_at,paid_until,banned_reason,created_at "
        "FROM users ORDER BY id DESC LIMIT 500")
    return [dict(r) for r in rows]


@app.get("/api/admin/users/{user_id}")
async def api_admin_user(user_id: int, _=Depends(auth.operator_required)):
    row = await db.conn().fetch_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(404, "not_found")
    return dict(row)


@app.post("/api/admin/users/{user_id}/ban")
async def api_admin_user_ban(user_id: int, request: Request, _=Depends(auth.operator_required)):
    body = await request.json() if (await request.body()) else {}
    await auth.ban_user(user_id, body.get("reason", "manual"))
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/unban")
async def api_admin_user_unban(user_id: int, _=Depends(auth.operator_required)):
    await auth.unban_user(user_id)
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/extend_trial")
async def api_admin_user_extend_trial(user_id: int, request: Request, _=Depends(auth.operator_required)):
    body = await request.json() if (await request.body()) else {}
    await auth.extend_user_trial(user_id, body.get("days", 7))
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reset_password")
async def api_admin_user_reset(user_id: int, _=Depends(auth.operator_required)):
    pw = await auth.reset_user_password(user_id)
    return {"ok": True, "new_password": pw}


@app.get("/api/admin/proxies")
async def api_admin_proxies(_=Depends(auth.operator_required)):
    return await proxy_3proxy.list_all_proxies()


@app.post("/api/admin/proxies/{uid}/retest")
async def api_admin_proxy_retest(uid: str, _=Depends(auth.operator_required)):
    alive, latency = await health_checker.probe_one(uid)
    return {"uid": uid, "alive": alive, "latency_ms": latency}


@app.get("/api/admin/payments")
async def api_admin_payments(_=Depends(auth.operator_required)):
    rows = await db.conn().fetch_all(
        "SELECT * FROM payments ORDER BY id DESC LIMIT 200")
    return [dict(r) for r in rows]


@app.post("/api/admin/billing/simulate")
async def api_admin_simulate(request: Request, _=Depends(auth.operator_required)):
    body = await request.json()
    return await billing.simulate_webhook(
        user_id=body["user_id"],
        currency=body.get("currency", "RUB"),
        amount=body.get("amount"),
    )


@app.get("/api/admin/me")
async def api_admin_me(creds=Depends(auth.operator_required)):
    row = await db.conn().fetch_one(
        "SELECT email FROM users WHERE id=? AND role='operator'", (creds["user_id"],))
    return {"email": row["email"]} if row else {"email": None}


@app.get("/api/admin/stats")
async def api_admin_stats(_=Depends(auth.operator_required)):
    counts = {}
    for s in ("trial", "active", "banned", "expired", "operator"):
        row = await db.conn().fetch_one("SELECT COUNT(*) AS c FROM users WHERE status=?", (s,))
        counts[s] = (row["c"] if row else 0)
    proxy_row = await db.conn().fetch_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN is_alive=1 THEN 1 ELSE 0 END) AS alive FROM proxies")
    return {"user_counts": counts, "proxies": dict(proxy_row) if proxy_row else {}}


# ----------------------------------------------------------------------
# HTML pages
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def page_root():
    return _render("client.html")


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
async def page_admin():
    return _render("admin.html")


@app.get("/billing/success", response_class=HTMLResponse)
async def page_billing_success():
    return "<h1>💚 Готово</h1><p>Платёж принят. Подписка активируется в течение нескольких секунд. Откройте главную страницу для обновления статуса.</p>"


@app.get("/billing/fail", response_class=HTMLResponse)
async def page_billing_fail():
    return "<h1>⚠️ Не получилось</h1><p>Платёж не был завершён. Попробуйте ещё раз.</p>"


@app.get("/health")
async def health():
    return {"status": "ok", "service": "litetube-api", "version": __version__}


@app.get("/activate", response_class=HTMLResponse)
async def page_activate():
    """Web activation page — phone pairs the TV code to the logged-in account."""
    return _render("activate.html")


@app.get("/app", response_class=HTMLResponse)
async def page_app():
    """APK download page — lists available APK builds."""
    app_dir = HERE / "static" / "app"
    _v = __version__
    apks = sorted(
        [f.name for f in app_dir.glob("*.apk") if f.is_file()],
        reverse=True
    ) if app_dir.is_dir() else []
    apk_rows = "\n".join(
        f'<li><a href="/app/{name}">{name}</a></li>'
        for name in apks
    ) if apks else '<li>Сборка ещё не загружена — напишите в поддержку.</li>'
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark">
<title>Litetube — скачать APK</title>
<style>
  body{{background:#0a0a14;color:#e0e0f0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;
    min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:16px;
    background-image:radial-gradient(ellipse at 50% 0%,rgba(124,92,252,0.08) 0%,transparent 60%)}}
  .card{{background:#13132a;border:1px solid #1e1e3a;border-radius:12px;padding:32px 24px;max-width:440px;width:100%}}
  h1{{font-size:20px;text-align:center;margin-bottom:4px}}
  .sub{{font-size:13px;color:#8888aa;text-align:center;margin-bottom:20px}}
  ol{{font-size:14px;line-height:1.8;padding-left:20px;margin-bottom:20px;color:#c0c0e0}}
  a.btn{{display:block;width:100%;padding:12px 20px;border:none;border-radius:8px;font-size:15px;font-weight:600;
    cursor:pointer;text-align:center;background:#7c5cfc;color:#fff;text-decoration:none;
    transition:background .15s}}
  a.btn:hover{{background:#9b7fff}}
  .links{{margin-top:12px;font-size:13px}}
  .links a{{color:#7c5cfc;text-decoration:none}}
  .links a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<div class="card">
<h1>Установите Litetube на ТВ</h1>
<p class="sub">Android TV 5.0+ (API 21)</p>
<ol>
  <li>Скачайте APK-файл по ссылке ниже</li>
  <li>Перенесите APK на телевизор (флешка, Send Files to TV, облако)</li>
  <li>Откройте файл на ТВ → установите (разрешите неизвестные источники)</li>
  <li>Запустите Litetube → на экране появится 6-значный код</li>
  <li>Вернитесь на эту страницу на телефоне и введите код → <a href="/activate">страница активации</a></li>
</ol>
<ul style="list-style:none;padding:0;font-size:14px">{apk_rows}</ul>
<div class="links center" style="margin-top:16px;text-align:center">
  <a href="/">На главную</a> &middot; <a href="/activate">Активация</a>
</div>
</div>
<div style="text-align:center;font-size:11px;color:#888;margin-top:24px">Litetube v{_v}</div>
</body>
</html>"""
# /app page rendered above injects its own footer link; version appears there.
#
#  POST /api/devices/start              — TV app: get a 6-digit code + QR url
#  GET  /api/devices/poll?code=XXXXXX   — TV app: long-poll until claimed
#  POST /api/devices/claim/complete     — phone-web: bind code → user (auth)
#
# Codes are stored in device_claims (see SCHEMA_V3). Collision retries up to
# 3 times (10^6 numeric space ⇒ 0.13% at ~50 active codes). Long-poll holds
# up to DEVICE_POLL_MAX_SEC and returns 'pending' (HTTP 202) on timeout, so
# the TV client can reconnect without polluting logs.
# ----------------------------------------------------------------------
_DEVICE_POLL_MAX_SEC = float(os.environ.get("DEVICE_POLL_MAX_SEC", "30"))
_DEVICE_POLL_TICK_MS = int(os.environ.get("DEVICE_POLL_TICK_MS", "400"))
_DEVICE_START_PER_IP_PER_MIN = int(os.environ.get("DEVICE_START_PER_IP_PER_MIN", "12"))
_DEVICE_CLAIM_TTL_SEC = int(os.environ.get("DEVICE_CLAIM_TTL_SEC", "600"))
_DEVICE_ACTIVATE_BASE = os.environ.get("DEVICE_ACTIVATE_BASE_URL", "https://litetube.trfnv.ru/activate").rstrip("/")


async def _make_unique_code() -> str:
    """Generate a fresh 6-digit numeric code, retry on PK collision (max 3)."""
    for _ in range(3):
        code = f"{random.SystemRandom().randint(100000, 999999)}"
        exists = await db.conn().fetch_one("SELECT 1 FROM device_claims WHERE code=?", (code,))
        if not exists:
            return code
    raise HTTPException(503, "code_exhausted")


@app.post("/api/devices/start")
async def api_devices_start(request: Request):
    """TV-side: mint a short-lived code. Anonymous, IP-throttled."""
    xff = request.headers.get("x-forwarded-for")
    ip = (xff.split(",")[0].strip() if xff else request.client.host if request.client else "unknown")
    if not await auth.rate_limit_check(f"devices_start:{ip}", _DEVICE_START_PER_IP_PER_MIN):
        return JSONResponse({"error": "rate_limited"}, status_code=429)

    now = await db.now()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=_DEVICE_CLAIM_TTL_SEC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    code = await _make_unique_code()
    await db.conn().execute(
        "INSERT INTO device_claims(code, created_at, expires_at_iso) VALUES (?,?,?)",
        (code, now, expires))
    qr_url = f"{_DEVICE_ACTIVATE_BASE}?code={code}"
    return {"code": code, "expires_in": _DEVICE_CLAIM_TTL_SEC, "qr_url": qr_url}


@app.get("/api/devices/poll")
async def api_devices_poll(request: Request, code: str):
    """TV-side: long-poll until the code is bound to a user (or expires)."""
    if not code or not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "bad_code")
    started = time.monotonic()
    while time.monotonic() - started < _DEVICE_POLL_MAX_SEC:
        row = await db.conn().fetch_one(
            "SELECT claimed_jwt, claimed_at, expires_at_iso FROM device_claims WHERE code=?",
            (code,))
        if row is None:
            return JSONResponse({"status": "expired"}, status_code=410)
        if row["claimed_jwt"]:
            # One-shot consumer: clear JWT after first read so re-polls can't
            # reuse the secret from a leaked history.
            await db.conn().execute(
                "UPDATE device_claims SET claimed_jwt=NULL WHERE code=?", (code,))
            return {"status": "claimed", "jwt": row["claimed_jwt"], "claimed_at": row["claimed_at"]}
        if row["expires_at_iso"] and row["expires_at_iso"] < await db.now():
            return JSONResponse({"status": "expired"}, status_code=410)
        await asyncio.sleep(_DEVICE_POLL_TICK_MS / 1000.0)
    return JSONResponse({"status": "pending"}, status_code=202)


@app.post("/api/devices/claim/complete")
async def api_devices_claim_complete(request: Request, creds=Depends(auth.client_required)):
    """Phone-web side: bind the code to the currently logged-in user + mint a JWT."""
    body = await request.json() if (await request.body()) else {}
    code = (body.get("code") or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "bad_code")

    xff = request.headers.get("x-forwarded-for")
    ip = (xff.split(",")[0].strip() if xff else request.client.host if request.client else "unknown")
    if not await auth.rate_limit_check(f"claim_complete:{ip}", 10):
        return JSONResponse({"error": "rate_limited"}, status_code=429)

    row = await db.conn().fetch_one(
        "SELECT user_id, expires_at_iso FROM device_claims WHERE code=?", (code,))
    if row is None:
        raise HTTPException(404, "code_not_found")
    if row["expires_at_iso"] and row["expires_at_iso"] < await db.now():
        raise HTTPException(410, "code_expired")
    if row["user_id"]:
        raise HTTPException(409, "code_already_claimed")

    new_jwt = auth._issue_token(creds["user_id"], "client", int(os.environ.get("JWT_EXPIRY_HOURS", "24")))
    ua = request.headers.get("user-agent", "")[:200]
    now = await db.now()
    rc = await db.conn().execute(
        "UPDATE device_claims SET user_id=?, claimed_jwt=?, claimed_ip=?, claimed_ua=?, claimed_at=? "
        "WHERE code=? AND user_id IS NULL",
        (creds["user_id"], new_jwt, ip, ua, now, code))
    if rc == 0:
        raise HTTPException(409, "race_lost")
    return {"ok": True}























































































































































































