"""Order Jual/Convert/Beli berlaku 10 menit; deposit Jual yang telat tidak dibayar dengan harga terkunci."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

import database.models  # noqa: F401
from database.connection import Base, SessionLocal, engine
from database.models import AuditLog, Order
from services import quote_guard
from services.detector import DepositDetector

HOT = "0x" + "1" * 40


class WaktuOrder(unittest.TestCase):
    def test_semua_order_berlaku_10_menit(self):
        from config.settings import settings
        from bot.handlers.sell import SELL_QUOTE_MINUTES
        self.assertEqual(quote_guard.QUOTE_MINUTES, 10)
        self.assertEqual(SELL_QUOTE_MINUTES, 10)
        self.assertEqual(settings.ORDER_EXPIRE_MINUTES, 10)

    def test_estimasi_admin_transfer_20_menit(self):
        self.assertEqual(quote_guard.SELL_PAYOUT_ETA_MINUTES, 20)


class LateSellDeposit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.det = DepositDetector()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def _run(self, deposit_after_minutes, trusted=False, price_now=17000):
        created = datetime.utcnow() - timedelta(hours=1)
        order = Order(
            order_id="SELL-LATE", telegram_id=7, order_type="sell", crypto_symbol="USDT",
            network="BSC", crypto_amount=Decimal("10"), price_per_unit=18000,
            nominal_idr=180000, fee_idr=3000, total_idr=177000, buyer_wallet="BCA | 1 | X",
            deposit_wallet=HOT, status="WAITING_CRYPTO_DEPOSIT", created_at=created,
            expired_at=created + timedelta(minutes=quote_guard.QUOTE_MINUTES),
            deposit_tx_hash="0x" + "ef" * 32,
        )
        self.db.add(order)
        self.db.commit()
        deposit_at = (created + timedelta(minutes=deposit_after_minutes)).replace(tzinfo=timezone.utc)
        verified = {"verified": True, "amount": 10.0, "timestamp": deposit_at.timestamp(),
                    "tx_hash": order.deposit_tx_hash, "reason": "OK"}
        bot = AsyncMock()
        with patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value=verified)), \
             patch("services.price_service.price_service.get_price",
                   new=AsyncMock(return_value={"sell_price_idr": price_now})), \
             patch("services.detector.notify_admins", new=AsyncMock()) as notify, \
             patch("services.detector.safe_send_message", new=AsyncMock()) as to_user, \
             patch.object(self.det, "_confirm_order", new=AsyncMock()) as confirm:
            await self.det._process_order(self.db, order, bot, trusted=trusted)
        return confirm, notify, to_user

    async def test_deposit_tepat_waktu_dikonfirmasi(self):
        confirm, notify, _ = await self._run(deposit_after_minutes=5)
        confirm.assert_awaited_once()
        notify.assert_not_awaited()

    async def test_deposit_telat_tidak_dibayar_otomatis_dan_admin_dikabari(self):
        confirm, notify, to_user = await self._run(deposit_after_minutes=40)
        confirm.assert_not_awaited()
        notify.assert_awaited_once()
        text = notify.await_args.args[1]
        self.assertIn("masa berlaku order Jual", text)
        self.assertIn("harga terkini", text)
        self.assertIn("-5.56%", text)  # 18.000 -> 17.000
        self.assertEqual(self.db.query(AuditLog).filter(
            AuditLog.action == "DEPOSIT_HASH_NEEDS_REVIEW").count(), 1)
        user_text = to_user.await_args.args[2]
        self.assertIn("harga terkini", user_text)

    async def test_toleransi_blok_dua_menit(self):
        confirm, notify, _ = await self._run(deposit_after_minutes=11)
        confirm.assert_awaited_once()
        notify.assert_not_awaited()

    async def test_harga_tidak_tersedia_tetap_ke_admin(self):
        created = datetime.utcnow() - timedelta(hours=1)
        order = Order(
            order_id="SELL-NOPRICE", telegram_id=7, order_type="sell", crypto_symbol="USDT",
            network="BSC", crypto_amount=Decimal("10"), price_per_unit=18000,
            nominal_idr=180000, fee_idr=3000, total_idr=177000, buyer_wallet="BCA | 1 | X",
            deposit_wallet=HOT, status="WAITING_CRYPTO_DEPOSIT", created_at=created,
            expired_at=created + timedelta(minutes=10), deposit_tx_hash="0x" + "aa" * 32,
        )
        self.db.add(order)
        self.db.commit()
        verified = {"verified": True, "amount": 10.0, "tx_hash": order.deposit_tx_hash, "reason": "OK",
                    "timestamp": (created + timedelta(minutes=50)).replace(tzinfo=timezone.utc).timestamp()}
        with patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value=verified)), \
             patch("services.price_service.price_service.get_price", new=AsyncMock(side_effect=RuntimeError("down"))), \
             patch("services.detector.notify_admins", new=AsyncMock()) as notify, \
             patch("services.detector.safe_send_message", new=AsyncMock()), \
             patch.object(self.det, "_confirm_order", new=AsyncMock()) as confirm:
            await self.det._process_order(self.db, order, AsyncMock())
        confirm.assert_not_awaited()
        notify.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
