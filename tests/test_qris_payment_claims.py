"""B1 — satu pembayaran QRIS hanya boleh melunasi SATU order/topup.

Dulu: pencocokan per nominal, 3 daftar "tx terpakai" terpisah di memori
(hilang saat restart), /check-payment tanpa startTime (lookback 24 jam),
offset zona waktu dibuang, refund ikut dihitung.
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

import database.models  # noqa: F401
from database import crud
from database.connection import Base, SessionLocal, engine
from main import _match_transaction
from services.gopay_service import GopayGatewayService, payment_window_start

CREATED = datetime(2026, 10, 6, 3, 0, 0)  # UTC naive, seperti Order.created_at


class ConfirmPayment(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.gopay = GopayGatewayService()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _gateway_returns(self, tx_id):
        tx = {"transaction_id": tx_id} if tx_id else {}
        self.gopay.check_payment = AsyncMock(return_value={"paid": True, "transaction": tx})

    async def test_one_payment_cannot_pay_buy_and_topup(self):
        self._gateway_returns("TX-77")
        self.assertTrue(await self.gopay.confirm_payment(
            self.db, amount=50077, ref_id="ORD-1", kind="buy", created_at=CREATED))
        self.assertFalse(await self.gopay.confirm_payment(
            self.db, amount=50077, ref_id="TOPUP-1", kind="topup", created_at=CREATED))
        # Klik ulang "Saya Sudah Transfer" di order yang sama tetap lunas.
        self.assertTrue(await self.gopay.confirm_payment(
            self.db, amount=50077, ref_id="ORD-1", kind="buy", created_at=CREATED))

    async def test_window_starts_at_order_creation(self):
        self._gateway_returns("TX-1")
        await self.gopay.confirm_payment(self.db, amount=1, ref_id="ORD-1", kind="buy", created_at=CREATED)
        start = self.gopay.check_payment.await_args.kwargs["start_time"]
        self.assertEqual(start, CREATED - timedelta(seconds=60))

    async def test_payment_without_transaction_id_is_not_accepted(self):
        self._gateway_returns(None)
        self.assertIsNone(await self.gopay.confirm_payment(
            self.db, amount=1, ref_id="ORD-1", kind="buy", created_at=CREATED))

    async def test_gateway_error_returns_unknown(self):
        self.gopay.check_payment = AsyncMock(return_value={
            "paid": False, "transaction": None, "available": False,
        })
        self.assertIsNone(await self.gopay.confirm_payment(
            self.db, amount=1, ref_id="ORD-1", kind="buy", created_at=CREATED))

    async def test_unpaid(self):
        self.gopay.check_payment = AsyncMock(return_value={"paid": False, "transaction": None})
        self.assertFalse(await self.gopay.confirm_payment(
            self.db, amount=1, ref_id="ORD-1", kind="buy", created_at=CREATED))


class MatchTransaction(unittest.TestCase):
    def test_timezone_offset_is_converted_not_dropped(self):
        # 09:30 WIB = 02:30 UTC → 30 menit SEBELUM order (03:00 UTC). Dulu offset
        # dibuang sehingga dianggap 09:30 UTC (sesudah order) dan cocok.
        txn = {"transaction_id": "TX", "amount": 50077, "time": "2026-10-06T09:30:00+07:00"}
        self.assertFalse(_match_transaction(txn, 50077, CREATED, set()))

    def test_after_order_matches(self):
        txn = {"transaction_id": "TX", "amount": 50077, "time": "2026-10-06T03:00:30Z"}
        self.assertTrue(_match_transaction(txn, 50077, CREATED, set()))

    def test_refund_never_matches(self):
        txn = {"transaction_id": "TX", "amount": 50077, "status": "refund",
               "time": "2026-10-06T03:05:00Z"}
        self.assertFalse(_match_transaction(txn, 50077, CREATED, set()))

    def test_missing_timestamp_rejected(self):
        self.assertFalse(_match_transaction({"transaction_id": "TX", "amount": 50077}, 50077, CREATED, set()))

    def test_window_helper(self):
        self.assertEqual(payment_window_start(CREATED), CREATED - timedelta(seconds=60))


if __name__ == "__main__":
    unittest.main()
