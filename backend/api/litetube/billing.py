"""Litube billing providers: MockProvider (dev) and RobokassaProvider (prod/test).

Signatures follow Robokassa's documented scheme:
  Init-payment:  MD5(MerchantLogin:OutSum:InvId[:OutSumCurrency]:MerchantPass1)
  Result URL:    MD5(OutSum:InvId[:OutSumCurrency]:MerchantPass2[:Shp_* sorted])
  Success URL:   informational only — never trust these for state changes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import HTTPException

from . import db

logger = logging.getLogger("litetube.billing")


PRICE_TABLE = {
    "RUB": float(os.environ.get("PRICE_RUB", "199")),
    "USD": float(os.environ.get("PRICE_USD", "3.99")),
    "EUR": float(os.environ.get("PRICE_EUR", "3.99")),
}


# ---- abstract + implementations ----------------------------------

class BillingProvider:
    name = "abstract"
    def make_payment_url(self, user: dict, amount: float, currency: str, payment_id: str) -> str:
        raise NotImplementedError
    def verify_webhook(self, body: dict) -> bool:
        raise NotImplementedError


class MockProvider(BillingProvider):
    """Always-accept simulator; `/api/admin/billing/simulate` triggers the same flow locally."""
    name = "MockProvider"
    def make_payment_url(self, user, amount, currency, payment_id):
        return f"/billing/success?InvId={payment_id}&Mock=1"
    def verify_webhook(self, body):
        return True


class RobokassaProvider(BillingProvider):
    name = "RobokassaProvider"
    BASE = "https://auth.robokassa.ru/Merchant/Index.aspx"

    def __init__(self):
        self.shop_id = os.environ.get("ROBOKASSA_SHOP_ID", "")
        self.p1 = os.environ.get("ROBOKASSA_PASSWORD1", "")
        self.p2 = os.environ.get("ROBOKASSA_PASSWORD2", "")
        self.is_test = os.environ.get("ROBOKASSA_TEST_MODE", "1") == "1"
        self.result_url = os.environ.get("ROBOKASSA_RESULT_URL", "")
        self.success_url = os.environ.get("ROBOKASSA_SUCCESS_URL", "")
        self.fail_url = os.environ.get("ROBOKASSA_FAIL_URL", "")
        if not all([self.shop_id, self.p1, self.p2]):
            raise ValueError("Robokassa: missing shop_id/passwords in env")

    def _sig_init(self, out_sum: str, inv_id: str, currency: str) -> str:
        # Robokassa signature scheme (corrected):
        #   * IsTest is a GET parameter only and never participates in any signature.
        #   * RUB (no OutSumCurrency): MD5(shop:sum:inv:password1)
        #   * non-RUB (OutSumCurrency present): MD5(shop:sum:inv:OutSumCurrency:password1)
        parts = [self.shop_id, out_sum, inv_id]
        if currency and currency != "RUB":
            parts.append(currency)
        parts.append(self.p1)
        raw = ":".join(parts)
        logger.debug("robokassa init sig raw: %s", raw)
        return hashlib.md5(raw.encode()).hexdigest().lower()

    def _sig_result(self, out_sum: str, inv_id: str, currency: str, shp: dict) -> str:
        parts = [out_sum, inv_id]
        if currency and currency != "RUB":
            parts.append(currency)
        suffix = ""
        if shp:
            sorted_shp = sorted(f"Shp_{k}={v}" for k, v in shp.items())
            suffix = ":" + ":".join(sorted_shp)
        raw = ":".join(parts) + f":{self.p2}" + suffix
        return hashlib.md5(raw.encode()).hexdigest().lower()

    def make_payment_url(self, user, amount, currency, payment_id):
        params = {
            "MerchantLogin": self.shop_id,
            "OutSum": f"{amount:.2f}",
            "InvId": str(payment_id),
            "Description": f"Litetube subscription for {user['email']}",
            "Culture": "ru",
        }
        if currency and currency != "RUB":
            params["OutSumCurrency"] = currency
        if self.is_test:
            params["IsTest"] = "1"
        if self.result_url:
            params["ResultURL"] = self.result_url
        if self.success_url:
            params["SuccessURL"] = self.success_url
        if self.fail_url:
            params["FailURL"] = self.fail_url
        params["SignatureValue"] = self._sig_init(params["OutSum"], payment_id, currency)
        return f"{self.BASE}?{urlencode(params)}"

    def verify_webhook(self, body):
        out_sum = body.get("OutSum", "")
        inv_id = body.get("InvId", "")
        provided = (body.get("SignatureValue") or "").lower()
        currency = body.get("OutSumCurrency", "")
        shp = {k.removeprefix("Shp_"): v for k, v in body.items() if k.startswith("Shp_")}
        expected = self._sig_result(out_sum, inv_id, currency, shp)
        return hmac.compare_digest(provided, expected) if provided else False


def get_provider() -> BillingProvider:
    which = os.environ.get("BILLING_PROVIDER", "mock").lower()
    return RobokassaProvider() if which == "robokassa" else MockProvider()


# ---- public endpoints helpers --------------------------------------------------------------

async def create_payment(user_id: int, email: str, currency: str):
    if currency not in PRICE_TABLE:
        raise HTTPException(400, "unsupported_currency")
    amount = PRICE_TABLE[currency]
    provider = get_provider()
    tx_id = secrets.token_urlsafe(8)
    now = await db.now()
    await db.conn().execute(
        "INSERT INTO payments(user_id,amount,currency,provider,tx_id,raw_request,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (user_id, amount, currency, provider.name, tx_id,
         json.dumps({"email": email}), now, now))
    url = provider.make_payment_url({"email": email}, amount, currency, tx_id)
    return {"ok": True, "url": url, "amount": amount, "currency": currency, "tx_id": tx_id}


async def handle_webhook(form: dict) -> tuple[str, int]:
    """Returns (plaintext body, http_status)."""
    provider = get_provider()
    if not provider.verify_webhook(form):
        logger.warning("webhook signature mismatch from %s", dict(form))
        return "bad_signature", 400
    tx_id = form.get("InvId") or ""
    row = await db.conn().fetch_one(
        "SELECT * FROM payments WHERE provider=? AND tx_id=?",
        (provider.name, tx_id))
    if not row:
        # Robokassa sometimes sends webhooks for amounts we never created.
        # Return OK so it stops retrying; just log.
        logger.warning("webhook for unknown tx_id=%s", tx_id)
        return "OK" + tx_id, 200
    if row["status"] == "completed":
        return "OK" + tx_id, 200  # idempotent

    now = await db.now()
    valid_until = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.conn().execute(
        "UPDATE payments SET status='completed', paid_at=?, valid_until=?, raw_request=?, updated_at=? WHERE id=?",
        (now, valid_until, json.dumps(form, sort_keys=True), now, row["id"]))
    # Extend paid_until; if user already had an active sub, add 30d on top.
    user = await db.conn().fetch_one(
        "SELECT paid_until,status FROM users WHERE id=?", (row["user_id"],))
    if user and user["paid_until"]:
        prev = datetime.fromisoformat(user["paid_until"].replace("Z", "+00:00"))
        # extend from max(prev, now)
        base = max(prev, datetime.now(timezone.utc))
        new_paid = (base + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        new_paid = valid_until
    await db.conn().execute(
        "UPDATE users SET status='active', paid_until=?, updated_at=? WHERE id=?",
        (new_paid, now, row["user_id"]))
    return "OK" + tx_id, 200


async def simulate_webhook(user_id: int, currency: str = "RUB", amount: float = None):
    """Mock-only: directly insert a 'completed' payment + activate subscription."""
    if amount is None:
        amount = PRICE_TABLE.get(currency, 199)
    tx_id = "m_" + secrets.token_urlsafe(6)
    now = await db.now()
    valid_until = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.conn().execute(
        "INSERT INTO payments(user_id,amount,currency,provider,tx_id,raw_request,status,paid_at,valid_until,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, amount, currency, "MockProvider", tx_id,
         json.dumps({"simulate": True}), "completed", now, valid_until, now, now))
    user = await db.conn().fetch_one(
        "SELECT paid_until FROM users WHERE id=?", (user_id,))
    if user and user["paid_until"]:
        prev = datetime.fromisoformat(user["paid_until"].replace("Z", "+00:00"))
        base = max(prev, datetime.now(timezone.utc))
        new_paid = (base + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        new_paid = valid_until
    await db.conn().execute(
        "UPDATE users SET status='active', paid_until=? WHERE id=?",
        (new_paid, user_id))
    return {"ok": True, "tx_id": tx_id, "paid_until": new_paid}
