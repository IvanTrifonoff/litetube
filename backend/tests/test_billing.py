"""Tests for Litetube billing: Robokassa signatures, MockProvider, webhooks, payments."""

import hashlib
import hmac
import os
from unittest.mock import patch

import pytest


class TestRobokassaSignatures:
    """Robokassa init and result signature correctness."""

    def setup_method(self):
        self._saved = {}
        for k, v in {"BILLING_PROVIDER": "robokassa",
                      "ROBOKASSA_SHOP_ID": "testshop",
                      "ROBOKASSA_PASSWORD1": "pass1",
                      "ROBOKASSA_PASSWORD2": "pass2",
                      "ROBOKASSA_TEST_MODE": "1"}.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v

    def teardown_method(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_init_sig_rub(self):
        """MD5(shop:sum:inv:pass1) for RUB."""
        from litetube.billing import RobokassaProvider

        r = RobokassaProvider()
        sig = r._sig_init("199.00", "inv123", "RUB")
        expected = hashlib.md5(b"testshop:199.00:inv123:pass1").hexdigest().lower()
        assert sig == expected

    def test_init_sig_usd(self):
        """MD5(shop:sum:inv:currency:pass1) for non-RUB."""
        from litetube.billing import RobokassaProvider

        r = RobokassaProvider()
        sig = r._sig_init("3.99", "inv456", "USD")
        expected = hashlib.md5(b"testshop:3.99:inv456:USD:pass1").hexdigest().lower()
        assert sig == expected

    def test_result_sig_no_shp(self):
        """MD5(sum:inv:pass2) when no Shp_* params."""
        from litetube.billing import RobokassaProvider

        r = RobokassaProvider()
        sig = r._sig_result("199.00", "inv123", "RUB", {})
        expected = hashlib.md5(b"199.00:inv123:pass2").hexdigest().lower()
        assert sig == expected

    def test_result_sig_with_shp(self):
        """MD5(sum:inv:pass2:Shp_a=1:Shp_b=2) with sorted Shp_* params."""
        from litetube.billing import RobokassaProvider

        r = RobokassaProvider()
        sig = r._sig_result("199.00", "inv123", "RUB",
                            {"b": "2", "a": "1"})
        expected = hashlib.md5(b"199.00:inv123:pass2:Shp_a=1:Shp_b=2").hexdigest().lower()
        assert sig == expected

    def test_result_sig_with_currency(self):
        """Non-RUB result includes OutSumCurrency."""
        from litetube.billing import RobokassaProvider

        r = RobokassaProvider()
        sig = r._sig_result("3.99", "inv456", "USD", {})
        expected = hashlib.md5(b"3.99:inv456:USD:pass2").hexdigest().lower()
        assert sig == expected

    def test_verify_webhook_correct_sig(self):
        from litetube.billing import RobokassaProvider

        r = RobokassaProvider()
        expected = hashlib.md5(b"199.00:inv789:pass2").hexdigest().lower()
        body = {"OutSum": "199.00", "InvId": "inv789",
                "SignatureValue": expected}
        assert r.verify_webhook(body)

    def test_verify_webhook_wrong_sig(self):
        from litetube.billing import RobokassaProvider

        r = RobokassaProvider()
        body = {"OutSum": "199.00", "InvId": "inv789",
                "SignatureValue": "deadbeef"}
        assert not r.verify_webhook(body)

    def test_verify_webhook_missing_sig(self):
        from litetube.billing import RobokassaProvider

        r = RobokassaProvider()
        body = {"OutSum": "199.00", "InvId": "inv789"}
        assert not r.verify_webhook(body)

    def test_make_payment_url_contains_params(self):
        from litetube.billing import RobokassaProvider
        from urllib.parse import parse_qs, urlparse

        r = RobokassaProvider()
        url = r.make_payment_url({"email": "u@x.com"}, 199.0, "RUB", "tx001")
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        assert qs["MerchantLogin"][0] == "testshop"
        assert qs["OutSum"][0] == "199.00"
        assert qs["InvId"][0] == "tx001"
        assert "SignatureValue" in qs
        assert "IsTest" in qs


class TestMockProvider:
    """Mock provider for development."""

    def setup_method(self):
        os.environ["BILLING_PROVIDER"] = "mock"

    def test_make_payment_url(self):
        from litetube.billing import MockProvider

        m = MockProvider()
        url = m.make_payment_url({"email": "x@y"}, 199, "RUB", "tx42")
        assert "/billing/success" in url
        assert "InvId=tx42" in url

    def test_verify_webhook_always_true(self):
        from litetube.billing import MockProvider

        m = MockProvider()
        assert m.verify_webhook({})


class TestCreatePayment:
    """Payment creation + webhook handling."""

    @pytest.mark.asyncio
    async def test_create_payment_mock(self, test_db):
        from litetube.billing import create_payment, PRICE_TABLE
        from litetube.auth import signup
        from litetube import db as db_mod

        # Create a user first — payments have FK to users
        await signup("pay@example.com", "password123")

        result = await create_payment(1, "pay@example.com", "RUB")
        assert result["ok"] is True
        assert result["amount"] == PRICE_TABLE["RUB"]
        assert result["currency"] == "RUB"
        assert "url" in result
        assert "tx_id" in result

        row = await db_mod.conn().fetch_one(
            "SELECT * FROM payments WHERE tx_id=?", (result["tx_id"],))
        assert row is not None
        assert row["status"] == "pending"
        assert row["user_id"] == 1

    @pytest.mark.asyncio
    async def test_create_payment_unsupported_currency(self, test_db):
        from litetube.billing import create_payment
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await create_payment(1, "x@y.com", "GBP")
        assert exc.value.status_code == 400


class TestWebhookHandling:
    """Webhook processing: idempotency, user activation."""

    @pytest.mark.asyncio
    async def test_webhook_activates_user(self, test_db):
        from litetube.billing import create_payment, handle_webhook, MockProvider
        from litetube import db as db_mod
        from litetube.auth import signup

        await signup("webhook@example.com", "password123")
        payment = await create_payment(1, "webhook@example.com", "RUB")

        body, status = await handle_webhook({
            "OutSum": str(payment["amount"]),
            "InvId": payment["tx_id"],
        })
        assert status == 200
        assert "OK" in body

        # User should now be active
        user = await db_mod.conn().fetch_one("SELECT * FROM users WHERE id=1")
        assert user["status"] == "active"
        assert user["paid_until"] is not None

        # Payment marked completed
        pay = await db_mod.conn().fetch_one(
            "SELECT * FROM payments WHERE tx_id=?", (payment["tx_id"],))
        assert pay["status"] == "completed"

    @pytest.mark.asyncio
    async def test_webhook_idempotent(self, test_db):
        from litetube.billing import create_payment, handle_webhook
        from litetube.auth import signup

        await signup("idem@example.com", "password123")
        payment = await create_payment(1, "idem@example.com", "RUB")

        # First webhook
        body1, status1 = await handle_webhook({
            "OutSum": str(payment["amount"]),
            "InvId": payment["tx_id"],
        })
        assert status1 == 200

        # Second webhook — idempotent, same result
        body2, status2 = await handle_webhook({
            "OutSum": str(payment["amount"]),
            "InvId": payment["tx_id"],
        })
        assert status2 == 200
        assert "OK" in body2

    @pytest.mark.asyncio
    async def test_webhook_unknown_tx_id(self, test_db):
        from litetube.billing import handle_webhook

        body, status = await handle_webhook({
            "OutSum": "199.00",
            "InvId": "unknown-tx-id-123",
        })
        # Should return OK so Robokassa stops retrying
        assert status == 200
        assert "OK" in body
