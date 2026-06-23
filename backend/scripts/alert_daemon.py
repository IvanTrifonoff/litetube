#!/usr/bin/env python3
"""Litetube external alerter daemon — runs on the HOST (systemd).

Tail-follows nginx access logs and the uvicorn access log tee'd by
docker-compose. Detects 5xx responses on /api/admin/* paths and emits
alerts via the same cooldown-aware sinks the in-process alerter uses.

State (cooldown) is read/written to the same SQLite `alert_state` table
as the FastAPI in-process loops — they cooperatively debounce so a burst
of admin-5xx triggers exactly one email, not 50.

Startup: seeks to EOF on each file so a backlog of pre-boot 5xx isn't
replayed (avoid spam on the first run). All exceptions caught and
logged; the daemon never crashes the FastAPI process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import traceback
from email.mime.text import MIMEText
from pathlib import Path

# ---- config ------------------------------------------------------

NGINX_LOG_PATHS = [
    "/var/log/nginx/litetube.trfnv.ru.access.log",
    "/var/log/nginx/admin.litetube.trfnv.ru.access.log",
    "/var/log/nginx/api.litetube.trfnv.ru.access.log",
]
UVICORN_LOG_PATH = "/var/log/litetube/uvicorn.log"
DB_PATH = os.environ.get("LITETUBE_DB_PATH", "/srv/proxy-infra/db/litetube.db")
COOLDOWN_SEC = int(os.environ.get("ALERT_COOLDOWN_SEC", "1800"))
ALERT_LOG_PATH = os.environ.get("ALERT_LOG_PATH", "/var/log/litetube/alerts.jsonl")
SMTP_HOST = os.environ.get("ALERT_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("ALERT_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("ALERT_SMTP_USER", "")
SMTP_PASS = os.environ.get("ALERT_SMTP_PASS", "")
SMTP_FROM = os.environ.get("ALERT_SMTP_FROM", "") or SMTP_USER
SMTP_USE_TLS = os.environ.get("ALERT_SMTP_TLS", "1") == "1"
SMTP_TIMEOUT = float(os.environ.get("ALERT_SMTP_TIMEOUT", "10"))
EMAIL_TO = [s.strip() for s in os.environ.get("ALERT_EMAIL_TO", "").split(",") if s.strip()]
WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
WEBHOOK_TIMEOUT = float(os.environ.get("ALERT_WEBHOOK_TIMEOUT", "10"))
HOSTNAME = os.environ.get("LITETUBE_HOSTNAME", "litetube")

# Default nginx combined-format regex.
NGINX_RE = re.compile(
    r'^(?P<ip>\S+) - (?P<user>\S+) \[(?P<time>[^\]]+)\] '
    r'"(?P<method>\S+)\s+(?P<path>\S+)\s+(?P<protocol>[^"]+)" '
    r'(?P<status>\d{3}) (?P<bytes>\d+) '
    r'"(?P<referer>[^"]*)" "(?P<ua>[^"]*)"'
)

# uvicorn access log default format:
#   '127.0.0.1:9090 - "POST /api/admin/login HTTP/1.1" 200 OK'
# (no `bytes` count, no referer/ua fields, captured here for fallback only).
UVICORN_RE = re.compile(
    r'^(?P<ip>\S+) - "(?P<method>\S+)\s+(?P<path>\S+)\s+(?P<protocol>[^"]+)"\s+(?P<status>\d{3})'
)

logging.basicConfig(
    level=os.environ.get("LITETUBE_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("litetube.alert_daemon")


# ---- state DB helpers --------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _age_sec(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        return max(0.0, time.time() - time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return float("inf")


def _ensure_row(conn, signal: str) -> None:
    """Best-effort: make sure the row exists (created by FastAPI at boot,
    but we may have started before docker bring-up)."""
    try:
        conn.execute(
            "INSERT OR IGNORE INTO alert_state(signal_name, incident_active, "
            "  last_alert_iso, last_alert_value, last_checked_iso) "
            "VALUES (?, 0, NULL, NULL, ?)",
            (signal, _now_iso()))
    except sqlite3.OperationalError as e:
        # alert_state table not yet created (FastAPI hasn't booted).
        logger.warning("ensure_row(%s): degraded mode (%s); retrying next tick", signal, e)


def _emit_5xx(conn, signal: str, source_path: str, m: re.Match, raw: str) -> bool:
    """Cooldown-gated emit for signal 'admin_5xx' with sink broadcast.

    Atomic UPSERT pattern mirrors alerter.py:emit() so the in-proc FastAPI
    middleware and this daemon cannot both fire for the same signal in a
    burst. Returns True if the alert was emitted; False if cooldown-skipped.
    """
    status = int(m["status"])
    method = m["method"]
    path = m["path"]
    if status < 500:
        return False
    # Only fire on /api/admin/* \u2014 alerts for /api/auth/* or /api/billing/*
    # aren't relevant to operator health.
    if not path.startswith("/api/admin/"):
        return False

    proposed_iso = _now_iso()
    value = f"path={path};status={status};method={method}"
    cur = conn.execute(
        "INSERT INTO alert_state(signal_name, incident_active, last_alert_iso, "
        "  last_alert_value, last_checked_iso) VALUES(?, 1, ?, ?, ?) "
        "ON CONFLICT(signal_name) DO UPDATE SET "
        "  incident_active=1, last_alert_iso=excluded.last_alert_iso, "
        "  last_alert_value=excluded.last_alert_value, "
        "  last_checked_iso=excluded.last_checked_iso "
        "WHERE alert_state.last_alert_iso IS NULL "
        "   OR (strftime('%s', excluded.last_alert_iso) - "
        "       strftime('%s', alert_state.last_alert_iso)) >= ?",
        (signal, proposed_iso, value, proposed_iso, COOLDOWN_SEC))
    advanced = (cur.rowcount or 0) > 0
    if not advanced:
        logger.debug("admin_5xx cooldown not elapsed; skipping (rowcount=0)")
        return False

    ua = m["ua"][:200] if "ua" in m.groupdict() else ""
    ip = m["ip"] if "ip" in m.groupdict() else ""
    subject = f"[{HOSTNAME}] admin {status} on {path} ({method})"
    body = (f"Source: {source_path}\n"
            f"Log line: {raw[:400]}\n"
            f"Method: {method}\nPath: {path}\nStatus: {status}\n"
            f"Client IP: {ip}\nUser-agent: {ua}")

    _log_append(subject, body)
    any_real_ok = False
    if WEBHOOK_URL:
        if _send_webhook(subject, body):
            any_real_ok = True
    if SMTP_HOST and SMTP_USER and EMAIL_TO:
        if _send_email(subject, body):
            any_real_ok = True

    if not any_real_ok:
        # All real sinks failed; roll back tentative write so daemon's next
        # tail-loop iteration can retry, mirroring alerter.emit() behavior.
        try:
            conn.execute(
                "UPDATE alert_state SET last_alert_iso=NULL, incident_active=1 "
                "WHERE signal_name=? AND last_alert_iso=?",
                (signal, proposed_iso))
            conn.commit()
        except Exception:
            logger.exception("rollback of tentative alert_state failed")
        logger.warning("admin_5xx NO REAL SINK OK; rolled back (smtp=%s webhook=%s)",
                       bool(SMTP_HOST), bool(WEBHOOK_URL))
        return False
    conn.commit()
    logger.info("admin_5xx emitted: %s %s", method, path)
    return True


# ---- sinks (mirror alerter.py) -----------------------------------

def _log_append(subject: str, body: str) -> None:
    try:
        Path(ALERT_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        rec = {"iso": _now_iso(), "host": HOSTNAME, "subject": subject, "body": body}
        with open(ALERT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.critical("ALERT %s: %s", subject, body[:300])
    except Exception as e:
        logger.error("log_append failed: %s", e)


def _send_email(subject: str, body: str) -> bool:
    import smtplib, ssl
    if not (SMTP_HOST and SMTP_USER and SMTP_FROM and EMAIL_TO and SMTP_PASS):
        return False
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as s:
            s.ehlo()
            if SMTP_USE_TLS:
                s.starttls(context=ctx)
                s.ehlo()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, EMAIL_TO, msg.as_string())
        return True
    except Exception as e:
        logger.error("email send failed: %s", e)
        return False


def _send_webhook(subject: str, body: str) -> bool:
    if not WEBHOOK_URL:
        return False
    try:
        import httpx
        with httpx.Client(timeout=WEBHOOK_TIMEOUT) as c:
            r = c.post(WEBHOOK_URL, json={
                "subject": subject, "body": body, "host": HOSTNAME, "iso": _now_iso(),
            })
            return r.is_success
    except Exception as e:
        logger.error("webhook send failed: %s", e)
        return False


# ---- file tailer --------------------------------------------------

class Tailer:
    """Read-only seek-to-EOF follower. Reopens on inode change (logrotate)."""
    def __init__(self, path: str):
        self.path = path
        self._fh = None
        self._inode: int | None = None

    def open_eof(self) -> None:
        try:
            self._fh = open(self.path, "r", encoding="utf-8", errors="replace")
            self._fh.seek(0, 2)  # CRITICAL: skip backlog
            try:
                self._inode = os.stat(self.path).st_ino
            except OSError:
                self._inode = None
            logger.info("tailing %s (seek=EOF)", self.path)
        except FileNotFoundError:
            logger.warning("log file not found yet: %s", self.path)
            self._fh = None
            self._inode = None

    def maybe_reopen(self) -> None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
                self._inode = None
            return
        if self._inode is not None and st.st_ino != self._inode:
            self._fh.close()
            self.open_eof()
        elif self._fh is None:
            self.open_eof()

    def read_line(self) -> str | None:
        """Return new line, or '' if no data, or None if file missing."""
        if self._fh is None:
            return None
        line = self._fh.readline()
        return line.rstrip("\n") if line else ""


# ---- per-file polling -------------------------------------------

async def tail_one(name: str, path: str, regex: re.Pattern, conn) -> None:
    """Forever: poll file, parse, emit. Catches all exceptions to keep alive."""
    t = Tailer(path)
    t.open_eof()
    signal_name = "admin_5xx"
    while True:
        try:
            t.maybe_reopen()
            line = await asyncio.get_running_loop().run_in_executor(None, t.read_line)
            if line is None:
                # file missing; sleep + retry
                await asyncio.sleep(5)
                continue
            if not line:
                await asyncio.sleep(0.5)
                continue
            m = regex.match(line)
            if not m:
                continue
            await asyncio.get_running_loop().run_in_executor(
                None, _emit_5xx, conn, signal_name, path, m, line)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("tail_one(%s) error: %s", name, traceback.format_exc()[:160])
            await asyncio.sleep(2)


# ---- main ---------------------------------------------------------

async def main() -> None:
    Path("/var/log/litetube").mkdir(parents=True, exist_ok=True)

    # DB connect — keep open across loops; commits per emit.
    conn = sqlite3.connect(DB_PATH, timeout=15.0, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 15000;")

    # Ensure signal rows exist (best-effort).
    for sig in ("admin_5xx", "proxy_down_5min", "daily_signup_paid_zero"):
        _ensure_row(conn, sig)
    conn.commit()

    tasks = []
    for p in NGINX_LOG_PATHS:
        tasks.append(asyncio.create_task(
            tail_one(f"nginx:{Path(p).name}", p, NGINX_RE, conn),
            name=f"tail-nginx:{Path(p).name}"))
    if os.path.exists(os.path.dirname(UVICORN_LOG_PATH)) or True:
        tasks.append(asyncio.create_task(
            tail_one("uvicorn", UVICORN_LOG_PATH, UVICORN_RE, conn),
            name="tail-uvicorn"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    logger.info("alert_daemon started; tailing %d nginx + uvicorn access log; signals: admin_5xx (also written by in-proc loops)",
                len(NGINX_LOG_PATHS))
    await stop.wait()

    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    conn.close()
    logger.info("alert_daemon stopped")


if __name__ == "__main__":
    asyncio.run(main())
