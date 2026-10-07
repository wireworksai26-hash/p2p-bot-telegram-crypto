"""B9 — tombol reject admin hanya untuk transaksi yang belum dibayar/belum diproses.

Dulu tanpa guard status:
- Reject order Beli Saldo Bot (saldo sudah dipotong) -> order rejected, saldo hilang.
- Reject Convert setelah deposit terkonfirmasi -> koin user tertahan tanpa payout.
- Reject topup yang sudah SUCCESS -> berubah CANCELLED (pembukuan rusak).
"""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from database import crud  # noqa: E402
from config.settings import settings  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order, TopupOrder, User  # noqa: E402
from bot.handlers import admin  # noqa: E402

USER = 3003


def _order(order_id, status, order_type="buy", method="GOPAY_QRIS", paid_at=None):
    return Order(order_id=order_id, telegram_id=USER, order_type=order_type, crypto_symbol="USDT",
                 network="BSC", crypto_amount=Decimal("1"), price_per_unit=17915, nominal_idr=17915,
                 fee_idr=3000, total_idr=20915, buyer_wallet="0x" + "c" * 40, payment_method=method,
                 status=status, paid_at=paid_at, created_at=datetime.utcnow())


class RejectGuard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.pin = patch.object(settings, "ADMIN_CHAT_IDS", [1])
        self.pin.start()
        db = SessionLocal()
        db.add(User(telegram_id=USER, balance_idr=Decimal("0")))
        db.commit()
        db.close()

    def tearDown(self):
        self.pin.stop()
        Base.metadata.drop_all(bind=engine)

    async def _press(self, handler, data, uid=1):
        query = MagicMock()
        query.data = data
        query.from_user = SimpleNamespace(id=uid)
        query.answer = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = SimpleNamespace(caption="x", text="x")
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        with patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()):
            await handler(update, SimpleNamespace(bot=AsyncMock()))
        return query

    def _get(self, model, key, value):
        db = SessionLocal()
        try:
            return db.query(model).filter(getattr(model, key) == value).one()
        finally:
            db.close()

    def _add(self, obj):
        db = SessionLocal()
        db.add(obj)
        db.commit()
        db.close()

    async def test_reject_beli_saldo_bot_otomatis_refund_saldo(self):
        self._add(_order("ORD-BAL", "pending", method="BOT_BALANCE", paid_at=datetime.utcnow()))
        self.assertEqual(self._get(User, "telegram_id", USER).balance_idr, Decimal("0"))
        await self._press(admin.admin_reject_buy_callback, "admin_reject_buy_ORD-BAL")
        self.assertEqual(self._get(Order, "order_id", "ORD-BAL").status, "rejected")
        self.assertEqual(self._get(User, "telegram_id", USER).balance_idr, Decimal("20915"))
        # Tekan kedua kali tidak menduplikasi refund
        await self._press(admin.admin_reject_buy_callback, "admin_reject_buy_ORD-BAL")
        self.assertEqual(self._get(User, "telegram_id", USER).balance_idr, Decimal("20915"))

    async def test_reject_beli_saldo_bot_tanpa_bukti_debit_tidak_mencetak_saldo(self):
        self._add(_order("ORD-BAL-NO-DEBIT", "pending", method="BOT_BALANCE"))
        await self._press(admin.admin_reject_buy_callback, "admin_reject_buy_ORD-BAL-NO-DEBIT")
        self.assertEqual(self._get(Order, "order_id", "ORD-BAL-NO-DEBIT").status, "pending")
        self.assertEqual(self._get(User, "telegram_id", USER).balance_idr, Decimal("0"))

    def test_pembuatan_order_saldo_bot_dan_debit_satu_transaksi(self):
        db = SessionLocal()
        try:
            db.query(User).filter(User.telegram_id == USER).update({User.balance_idr: 30_000})
            db.commit()
            data = {
                "order_id": "ORD-ATOMIC-BALANCE", "telegram_id": USER, "order_type": "buy",
                "crypto_symbol": "USDT", "network": "BSC", "crypto_amount": Decimal("1"),
                "price_per_unit": 20_000, "nominal_idr": 20_000, "fee_idr": 0,
                "total_idr": 20_000, "payment_method": "BOT_BALANCE", "status": "pending",
            }
            order = crud.create_bot_balance_order(db, data)
            self.assertIsNotNone(order)
            self.assertIsNotNone(order.paid_at)
            self.assertEqual(db.query(User).filter(User.telegram_id == USER).one().balance_idr, Decimal("10000"))
        finally:
            db.close()

    def test_gagal_mencatat_audit_mengembalikan_status_dan_saldo(self):
        self._add(_order("ORD-ROLLBACK", "pending", method="BOT_BALANCE", paid_at=datetime.utcnow()))
        db = SessionLocal()
        try:
            with patch.object(db, "add", side_effect=RuntimeError("audit unavailable")):
                with self.assertRaises(RuntimeError):
                    crud.reject_and_refund_bot_balance_order(db, "ORD-ROLLBACK", 1)
        finally:
            db.close()
        self.assertEqual(self._get(Order, "order_id", "ORD-ROLLBACK").status, "pending")
        self.assertEqual(self._get(User, "telegram_id", USER).balance_idr, Decimal("0"))

    async def test_reject_beli_yang_sedang_payout_ditolak(self):
        for st in ("paid", "payout_processing", "completed"):
            self._add(_order(f"ORD-{st}", st))
            await self._press(admin.admin_reject_buy_callback, f"admin_reject_buy_ORD-{st}")
            self.assertEqual(self._get(Order, "order_id", f"ORD-{st}").status, st)

    async def test_kontrol_reject_qris_belum_bayar(self):
        self._add(_order("ORD-Q", "pending"))
        await self._press(admin.admin_reject_buy_callback, "admin_reject_buy_ORD-Q")
        self.assertEqual(self._get(Order, "order_id", "ORD-Q").status, "rejected")

    async def test_reject_convert_setelah_deposit_masuk_ditolak(self):
        for st in ("CRYPTO_CONFIRMED", "PAYOUT_QUEUED", "COMPLETED"):
            self._add(_order(f"ORD-S-{st}", st, order_type="swap", method=None))
            await self._press(admin.admin_reject_swap_callback, f"admin_reject_swap_ORD-S-{st}")
            self.assertEqual(self._get(Order, "order_id", f"ORD-S-{st}").status, st)

    async def test_kontrol_reject_convert_menunggu_deposit(self):
        self._add(_order("ORD-SW", "WAITING_CRYPTO_DEPOSIT", order_type="swap", method=None))
        await self._press(admin.admin_reject_swap_callback, "admin_reject_swap_ORD-SW")
        self.assertEqual(self._get(Order, "order_id", "ORD-SW").status, "CANCELLED")

    async def test_reject_topup_lunas_ditolak(self):
        self._add(TopupOrder(topup_id="TOPUP-OK", telegram_id=USER, amount_idr=50_137, status="SUCCESS",
                             created_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(minutes=15)))
        await self._press(admin.admin_reject_topup_callback, "admin_reject_topup_TOPUP-OK")
        self.assertEqual(self._get(TopupOrder, "topup_id", "TOPUP-OK").status, "SUCCESS")

    async def test_kontrol_reject_topup_pending(self):
        self._add(TopupOrder(topup_id="TOPUP-P", telegram_id=USER, amount_idr=50_137, status="PENDING",
                             created_at=datetime.utcnow(), expires_at=datetime.utcnow() + timedelta(minutes=15)))
        await self._press(admin.admin_reject_topup_callback, "admin_reject_topup_TOPUP-P")
        self.assertEqual(self._get(TopupOrder, "topup_id", "TOPUP-P").status, "CANCELLED")

    async def test_non_admin_tidak_bisa_reject(self):
        self._add(_order("ORD-Q", "pending"))
        await self._press(admin.admin_reject_buy_callback, "admin_reject_buy_ORD-Q", uid=USER)
        self.assertEqual(self._get(Order, "order_id", "ORD-Q").status, "pending")


if __name__ == "__main__":
    unittest.main()
