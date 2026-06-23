"""Litube alerter core: AlertSink ABC + SMTP + Webhook + Log(forced-on), with SQLite-backed cooldown shared by the in-process loops and the host-side alert_daemon.

State for cooldown lives in `alert_state(signal_name PK, incident_active,
last_alert_iso, last_alert_value, last_checked_iso)`. Both this module
(used inside the FastAPI process) and scripts/alert_daemon.py (host
sidecar) write it; SQLite WAL serialises writes; the UPSERT-with-WHERE
cooldown-elapsed check makes the read-then-write window atomic so a
burst of admin-5xx between processes produces exactly one alert.

Public API:
    AlertSink       — abstract base
    EmailSink       — smtplib SMTP+STARTTLS (or SMTP_SSL on port 465)
    WebhookSink     — httpx POST JSON
    LogSink         — JSONL appender (NEVER fails: counts as success)
    emit(signal, subject, body, value=None, force=False) -> bool
    resolve(signal) — mark incident cleared (no send)
    tick_checked(signal) — update last_checked_iso (no state change)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import ssl
import time
from abc import ABC, abstractmethod
from email.mime.text import MIMEText

import httpx

from . import db

logger = logging.getLogger("litetube.alerter")

# ----- tunables (env-overridable) ----------------------------------
COOLDOWN_SEC = int(os.environ.get("ALERT_COOLDOWN_SEC", "1800"))  # 30 min

SMTP_HOST = os.environ.get("ALERT_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("ALERT_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("ALERT_SMTP_USER", "")
SMTP_PASS = os.environ.get("ALERT_SMTP_PASS", "")
SMTP_FROM = os.environ.get("ALERT_SMTP_FROM", "") or SMTP_USER
SMTP_USE_TLS = os.environ.get("ALERT_SMTP_TLS", "1") == "1"
# Some providers need implicit TLS (port 465 / Outlook). Default off;
# STARTTLS on 587 is the historical Litetube setup.
SMTP_IMPLICIT_TLS = os.environ.get("ALERT_SMTP_IMPLICIT_TLS", "0") == "1"
SMTP_TIMEOUT = float(os.environ.get("ALERT_SMTP_TIMEOUT", "10"))
EMAIL_TO = [s.strip() for s in os.environ.get("ALERT_EMAIL_TO", "").split(",") if s.strip()]
WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
WEBHOOK_TIMEOUT = float(os.environ.get("ALERT_WEBHOOK_TIMEOUT", "10"))
ALERT_LOG_PATH = os.environ.get("ALERT_LOG_PATH", "/var/log/litetube/alerts.jsonl")
HOSTNAME = os.environ.get("LITETUBE_HOSTNAME", "litetube")


# ----- sinks -------------------------------------------------------

class AlertSink(ABC):
    name = "abstract"

    @abstractmethod
    async def send(self, subject: str, body: str) -> bool:
        """Return True iff this sink reports success. Must NEVER raise."""


class LogSink(AlertSink):
    """Always-on JSONL appender + critical logger line. NEVER fails."""
    name = "log"

    def __init__(self, path: str = ALERT_LOG_PATH):
        self.path = path

    async def send(self, subject: str, body: str) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            rec = {
                "iso": _now_iso(),
                "host": HOSTNAME,
                "subject": subject,
                "body": body,
            }
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            logger.critical("ALERT %s: %s", subject, body)
        except Exception as e:
            logger.error("LogSink append failed: %s", e)
        return True  # always counts as success (defense in depth)


class EmailSink(AlertSink):
    name = "smtp"
    configured: bool = bool(SMTP_HOST and SMTP_USER and SMTP_FROM and EMAIL_TO)

    async def send(self, subject: str, body: str) -> bool:
        if not self.configured:
            return False
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = ", ".join(EMAIL_TO)
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, self._sync_send, msg.as_string()),
                timeout=SMTP_TIMEOUT + 5,
            )
            return True
        except Exception as e:
            logger.error("EmailSink send failed: %s", e)
            return False

    def _sync_send(self, payload: str) -> None:
        ctx = ssl.create_default_context()
        cls = smtplib.SMTP_SSL if SMTP_IMPLICIT_TLS else smtplib.SMTP
        with cls(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as s:
            s.ehlo()
            if SMTP_USE_TLS and not SMTP_IMPLICIT_TLS:
                s.starttls(context=ctx)
                s.ehlo()
            if SMTP_PASS:
                s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, EMAIL_TO, payload)


class WebhookSink(AlertSink):
    name = "webhook"
    configured: bool = bool(WEBHOOK_URL)

    async def send(self, subject: str, body: str) -> bool:
        if not self.configured:
            return False
        payload = {
            "subject": subject, "body": body,
            "host": HOSTNAME, "iso": _now_iso(),
        }
        try:
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as c:
                r = await c.post(WEBHOOK_URL, json=payload)
                return r.is_success
        except Exception as e:
            logger.error("WebhookSink send failed: %s", e)
            return False


# ----- helpers ----------------------------------------------------

_SINKS: list[AlertSink] | None = None


def _all_sinks() -> list[AlertSink]:
    global _SINKS
    if _SINKS is None:
        _SINKS = [LogSink(), EmailSink(), WebhookSink()]
    return _SINKS


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _age_sec(iso: str) -> float:
    try:
        return max(0.0, time.time() - time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return float("inf")


# ----- public emit/resolve API -----------------------------------

async def resolve(signal: str) -> None:
    """Mark an incident cleared (no emission). Used by loops when condition normalised."""
    row = await db.conn().fetch_one(
        "SELECT incident_active, last_alert_iso, last_alert_value "
        "FROM alert_state WHERE signal_name=?", (signal,))
    if not row or not row["incident_active"]:
        return
    await db.conn().execute(
        "UPDATE alert_state SET incident_active=0 WHERE signal_name=?",
        (signal,))
    logger.info("alert %s resolved", signal)


async def tick_checked(signal: str) -> None:
    """Update last_checked_iso without changing effective cooldown state."""
    await db.conn().execute(
        "UPDATE alert_state SET last_checked_iso=? WHERE signal_name=?",
        (_now_iso(), signal))


async def emit(signal: str, subject: str, body: str, *,
               value: str | None = None, force: bool = False) -> bool:
    """Emit an alert gated by cooldown.

    - Atomic UPSERT with WHERE-on-cooldown-elapsed: the FastAPI middleware
      and the host-side alert_daemon cannot both fire for the same signal
      within a burst.
    - LogSink counts toward success: a log-only deployment still obeys
      cooldown (otherwise we'd flood the JSONL log every tick).
    - force=True bypasses cooldown (used by manual smoke tests).
    - On ALL-sinks-failed (even LogSink): roll back tentative last_alert_iso
      so the next tick retries.
    """
    proposed_iso = _now_iso()
    rowcount = await db.conn().execute(
        "INSERT INTO alert_state(signal_name, incident_active, last_alert_iso, "
        "  last_alert_value, last_checked_iso) "
        "VALUES(?, 1, ?, ?, ?) "
        "ON CONFLICT(signal_name) DO UPDATE SET "
        "  incident_active=1, "
        "  last_alert_iso=excluded.last_alert_iso, "
        "  last_alert_value=excluded.last_alert_value, "
        "  last_checked_iso=excluded.last_checked_iso "
        "WHERE alert_state.last_alert_iso IS NULL "
        "   OR (strftime('%s', excluded.last_alert_iso) - "
        "       strftime('%s', alert_state.last_alert_iso)) >= ?",
        (signal, proposed_iso, value, proposed_iso, COOLDOWN_SEC))
    advanced = (rowcount > 0) if not force else True
    if not advanced:
        logger.debug("alert %s: cooldown NOT elapsed; skipping (rowcount=0)", signal)
        return False

    any_ok = False
    any_real_ok = False  # smtp or webhook returned True
    for sink in _all_sinks():
        try:
            ok = await sink.send(subject, body)
        except Exception as e:
            logger.error("sink %s raised: %s", sink.name, e)
            ok = False
        if ok:
            any_ok = True
            if sink.name != "log":
                any_real_ok = True

    if not any_ok and not force:
        # Roll back tentative last_alert_iso so next tick retries.
        try:
            await db.conn().execute(
                "UPDATE alert_state SET last_alert_iso=NULL "
                "WHERE signal_name=? AND last_alert_iso=?",
                (signal, proposed_iso))
        except Exception:
            logger.exception("rollback of tentative alert_state failed")
        logger.warning("alert %s: all sinks failed; rolled back, will retry next tick", signal)
        return False

    if not any_real_ok and not force:
        logger.warning("alert %s: only LogSink succeeded (smtp/webhook both failed)", signal)
    else:
        logger.info("alert %s emitted (real_ok=%s)", signal, any_real_ok)
    return True
