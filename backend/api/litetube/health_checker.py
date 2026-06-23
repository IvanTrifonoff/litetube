"""Litube health-checker: every N seconds, probe each active proxy and update SQLite.

We do TCP-connect + latency measurement rather than going through the proxy
to fetch a probe URL — cheaper, avoids storing credentials in the
healthcheck process, and detects 'proxy alive but auth broken' separately
via the 3proxy auth-failure counter (see `/api/admin/stats`).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from . import db

logger = logging.getLogger("litetube.healthcheck")

INTERVAL = int(os.environ.get("HEALTH_CHECK_INTERVAL_SEC", "30"))
TIMEOUT = int(os.environ.get("HEALTH_CHECK_TIMEOUT_SEC", "8"))
DEAD_THRESHOLD = 3  # failed_count to mark 'persistent dead'

_exec = ThreadPoolExecutor(max_workers=8)


def _tcp(host: str, port: int) -> tuple[bool, int]:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True, int((time.monotonic() - t0) * 1000)
    except Exception:
        return False, 0


async def _probe(host: str, port: int) -> tuple[bool, int]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_exec, _tcp, host, port)


async def _tick():
    rows = await db.conn().fetch_all("SELECT uid, host, port, failed_count FROM proxies WHERE is_active=1")
    if not rows:
        return
    upd = 0
    for r in rows:
        alive, latency = await _probe(r["host"], r["port"])
        failed = 0 if alive else (r["failed_count"] or 0) + 1
        await db.conn().execute(
            "UPDATE proxies SET is_alive=?, latency_ms=?, last_check_at=?, failed_count=? WHERE uid=?",
            (1 if alive else 0, latency, await db.now(), failed, r["uid"]))
        upd += 1
    logger.debug("health tick: %d proxies probed", upd)


async def run():
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("health_checker tick failed")
        await asyncio.sleep(INTERVAL)


async def probe_one(uid: str) -> tuple[bool, int]:
    row = await db.conn().fetch_one("SELECT host, port FROM proxies WHERE uid=?", (uid,))
    if not row:
        return False, 0
    alive, latency = await _probe(row["host"], row["port"])
    await db.conn().execute(
        "UPDATE proxies SET is_alive=?, latency_ms=?, last_check_at=? WHERE uid=?",
        (1 if alive else 0, latency, await db.now(), uid))
    return alive, latency
