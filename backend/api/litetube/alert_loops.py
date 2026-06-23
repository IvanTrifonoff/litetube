"""Litube alert polling loops: condition (a) proxy health outage >5 min,
condition (c) daily signup/paid zero-count.

State + dedup lives in `alert_state` (see alerter.py). Loops poll on a
slow cadence (default 60s and 10min respectively) so the cooldown logic
in alerter.emit() handles the burst-suppression.

Startup guard: on boot, an in-progress outage will not be raised until
the loop's first iteration completes. A subsequent tick flips to alert.
This is intentional — we don't want the daemon to fire while the FastAPI
process is just spinning up.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from . import alerter, db

logger = logging.getLogger("litetube.alert_loops")

# 10 health_checker ticks * default INTERVAL=30s = 5 min outage threshold.
PROXY_OUTAGE_THRESHOLD = int(os.environ.get("ALERT_PROXY_FAILURE_TICKS", "10"))
PROXY_OUTAGE_SCAN_SEC = int(os.environ.get("ALERT_PROXY_OUTAGE_SCAN_SEC", "60"))
DAILY_ZERO_HOUR_UTC = int(os.environ.get("ALERT_DAILY_ZERO_HOUR_UTC", "1"))
DAILY_ZERO_SCAN_SEC = int(os.environ.get("ALERT_DAILY_ZERO_SCAN_SEC", "600"))

# Cooldown-domain signal keys
SIGNAL_PROXY_DOWN = "proxy_down_5min"
SIGNAL_DAILY_ZERO = "daily_signup_paid_zero"


# ----- (a) proxy health outage >=5 min -----------------------------

async def _proxy_total_active() -> int:
    row = await db.conn().fetch_one(
        "SELECT COUNT(*) AS c FROM proxies WHERE is_active=1")
    return (row["c"] if row else 0)


async def _proxy_outage_count() -> int:
    row = await db.conn().fetch_one(
        "SELECT COUNT(*) AS c FROM proxies "
        "WHERE is_active=1 AND is_alive=0 AND failed_count>=?",
        (PROXY_OUTAGE_THRESHOLD,))
    return (row["c"] if row else 0)


async def proxy_down_loop():
    """Fire when ALL active proxies have failed_count >= threshold (5 min).

    Resolution: when not all-down, mark incident cleared. next outage
    re-fires (subject to cooldown in alerter.emit()).
    """
    while True:
        try:
            total = await _proxy_total_active()
            down = await _proxy_outage_count()

            if total > 0 and down == total:
                await alerter.emit(
                    SIGNAL_PROXY_DOWN,
                    subject=f"[LITETUBE] all proxies down ({down}/{total} >=5 min)",
                    body=(f"Host: {os.environ.get('LITETUBE_HOSTNAME','litetube')}\n"
                          f"All {total} active proxies report is_alive=0 with "
                          f"failed_count>={PROXY_OUTAGE_THRESHOLD}\n"
                          f"(>= 5 min since last successful probe).\n"
                          f"Detected at: {datetime.now(timezone.utc).isoformat()}\n"
                          f"Likely cause: 3proxy container down, network unreachable, "
                          f"or health_checker exception loop. Check:\n"
                          f"  docker logs litetube-3proxy\n"
                          f"  docker logs litetube-api | grep -E 'health|exception'"),
                    value=f"down={down};total={total};threshold={PROXY_OUTAGE_THRESHOLD}")
            else:
                await alerter.resolve(SIGNAL_PROXY_DOWN)
            await alerter.tick_checked(SIGNAL_PROXY_DOWN)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("proxy_down_loop tick failed")
        await asyncio.sleep(PROXY_OUTAGE_SCAN_SEC)


# ----- (c) daily signup/paid zero count ---------------------------

async def _yesterday_counts() -> tuple[int, int]:
    yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    new_users = await db.conn().fetch_one(
        "SELECT COUNT(*) AS c FROM users "
        "WHERE role='client' AND substr(created_at,1,10)=?",
        (yesterday_iso,))
    paid_payments = await db.conn().fetch_one(
        "SELECT COUNT(*) AS c FROM payments "
        "WHERE status='completed' AND substr(paid_at,1,10)=?",
        (yesterday_iso,))
    nu = (new_users["c"] if new_users else 0)
    pp = (paid_payments["c"] if paid_payments else 0)
    return nu, pp, yesterday_iso


async def daily_zero_loop():
    """Around DAILY_ZERO_HOUR_UTC each day, alert if yesterday had 0 signups AND 0 paid payments.

    Uses a local process cache `last_alert_day` to ensure we don't double-fire
    if the loop iterates multiple times within the same hour window.
    """
    last_alert_day: str | None = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour >= DAILY_ZERO_HOUR_UTC and last_alert_day != _today_key(now):
                nu, pp, yesterday_iso = await _yesterday_counts()
                if nu == 0 and pp == 0:
                    await alerter.emit(
                        SIGNAL_DAILY_ZERO,
                        subject=f"[LITETUBE] zero signups & payments on {yesterday_iso}",
                        body=(f"Host: {os.environ.get('LITETUBE_HOSTNAME','litetube')}\n"
                              f"Day {yesterday_iso} UTC:\n"
                              f"  - new users:       0\n"
                              f"  - paid payments:   0\n"
                              f"Likely causes: payment webhook broken, signup endpoint\n"
                              f"down, fierce rate-limiter on /api/auth/*, or simply\n"
                              f"zero traffic. Check:\n"
                              f"  docker logs litetube-api | grep -E 'webhook|signup'\n"
                              f"  curl https://litetube.trfnv.ru/api/me"),
                        value=f"day={yesterday_iso};signups=0;paid=0")
                else:
                    await alerter.resolve(SIGNAL_DAILY_ZERO)
                last_alert_day = _today_key(now)
            await alerter.tick_checked(SIGNAL_DAILY_ZERO)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("daily_zero_loop tick failed")
        await asyncio.sleep(DAILY_ZERO_SCAN_SEC)


def _today_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")
