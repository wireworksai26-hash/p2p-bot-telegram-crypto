"""Pajak QRIS GoPay/GoBiz 0,3% untuk nominal > Rp 500.000 — Fase C.

Aturan terkunci: dibebankan ke customer (total = nominal + MDR + kode unik),
basis = nominal, scope = bayar via QRIS (beli + topup), pembulatan ke atas.
"""
import io
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "DATABASE_URL": "sqlite:///:memory:",
    "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "123456",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from services.fee_service import calculate_qris_mdr, qris_mdr_note
from database.connection import Base, engine, SessionLocal
from database.models import Order, TopupOrder
from database.crud import create_order
from bot.handlers.buy import handle_payment_selection, handle_order_confirmation


class TestMdrEngine(unittest.TestCase):
    def test_batas_500rb(self):
        self.assertEqual(calculate_qris_mdr(500_000), 0)
        self.assertEqual(calculate_qris_mdr(100), 0)

    def test_contoh_client_501rb(self):
        self.assertEqual(calculate_qris_mdr(501_000), 1_503)  # 501.000 + 0,3%

    def test_ceil_ke_atas(self):
        self.assertEqual(calculate_qris_mdr(500_001), 1_501)  # 1500,003 -> 1501
        self.assertEqual(calculate_qris_mdr(1_000_000), 3_000)

    def test_note(self):
        self.assertEqual(qris_mdr_note(500_000), "")
        note = qris_mdr_note(501_000)
        self.assertIn("0,3%", note)
        self.assertIn("1.503", note)


class TestMdrDbRoundtrip(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_order_menyimpan_mdr(self):
        order = create_order(self.db, {
            "order_id": "ORD-MDR-1", "telegram_id": 1, "crypto_symbol": "USDT",
            "network": "BSC", "crypto_amount": Decimal("30"), "price_per_unit": 16000,
            "nominal_idr": 501_000, "fee_idr": 5_500, "mdr_idr": 1_503,
            "unique_code": 87, "total_idr": 501_000 + 1_503 + 87,
        })
        self.assertEqual(order.mdr_idr, 1_503)
        self.assertEqual(order.total_idr, 502_590)

    def test_default_mdr_nol(self):
        order = Order(order_id="O2", telegram_id=1, crypto_symbol="X", network="Y",
                      crypto_amount=Decimal("1"), price_per_unit=1,
                      nominal_idr=1, fee_idr=1, total_idr=1)
        topup = TopupOrder(topup_id="T2", telegram_id=1, amount_idr=1)
        self.db.add_all([order, topup])
        self.db.commit()
        self.db.refresh(order)
        self.db.refresh(topup)
        self.assertEqual(order.mdr_idr, 0)
        self.assertEqual(topup.mdr_idr, 0)


class TestMdrBuyQris(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _user_data(self, nominal):
        return {
            "buy_order_id": "ORD-MDR-BUY", "buy_symbol": "USDT", "buy_network": "BSC",
            "buy_crypto_amount": 30.0, "buy_price_per_unit": 16000,
            "buy_nominal_idr": nominal, "buy_fee_idr": 5_500,
            "buy_received_idr": nominal - 5_500, "buy_total_idr": nominal,
            "buy_wallet": "0x" + "2" * 40,
        }

    async def test_qris_di_atas_500rb_total_termasuk_mdr(self):
        context, seen = await self._async_confirm(501_000, "GOPAY_QRIS")
        order = self.db.query(Order).filter_by(order_id="ORD-MDR-BUY").first()
        self.assertIsNotNone(order)
        self.assertEqual(order.mdr_idr, 1_503)
        # Invarian: total tersimpan == nominal + MDR + kode unik == amount QRIS.
        self.assertEqual(order.total_idr, 501_000 + 1_503 + order.unique_code)
        self.assertEqual(seen["amount"], order.total_idr)
        caption = context.bot.send_photo.call_args.kwargs.get("caption", "")
        self.assertIn("Pajak QRIS 0,3%", caption)

    async def test_instruksi_qris_menjelaskan_kode_unik_di_langkah_2(self):
        """User tidak boleh bingung dengan angka kode unik di belakang nominal QRIS."""
        context, _ = await self._async_confirm(50_000, "GOPAY_QRIS")
        caption = context.bot.send_photo.call_args.kwargs.get("caption", "")
        kalimat = "Kode Unik pembayaran digunakan untuk biaya pengecekan transaksi QRIS otomatis."
        self.assertIn(kalimat, caption)
        # Penjelasan berada tepat di bawah langkah 2 dan sebelum langkah 3.
        self.assertLess(caption.index("2. Nominal"), caption.index(kalimat))
        self.assertLess(caption.index(kalimat), caption.index("3. Selesaikan"))
        self.assertEqual(caption.count(kalimat), 1)

    async def _async_confirm(self, nominal, method):
        query = AsyncMock()
        query.data = "buy_confirm"
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=777, name="Pembeli"),
        )
        user_data = self._user_data(nominal)
        user_data["buy_pay_method"] = method
        user_data["buy_mdr_idr"] = calculate_qris_mdr(nominal) if method == "GOPAY_QRIS" else 0
        context = SimpleNamespace(user_data=user_data, bot=AsyncMock())
        seen = {}
        def fake_qris(amount):
            seen["amount"] = amount
            return io.BytesIO(b"qr")
        with patch("bot.handlers.buy.SessionLocal", side_effect=lambda: SessionLocal()), \
             patch("bot.handlers.buy.get_available_inventory", return_value=Decimal("999999")), \
             patch("bot.handlers.buy.notify_admins", new=AsyncMock()), \
             patch("services.qris_generator.get_qris_image_stream", side_effect=fake_qris):
            await handle_order_confirmation(update, context)
        return context, seen

    async def test_qris_di_bawah_500rb_tanpa_mdr(self):
        context, seen = await self._async_confirm(50_000, "GOPAY_QRIS")
        order = self.db.query(Order).filter_by(order_id="ORD-MDR-BUY").first()
        self.assertEqual(order.mdr_idr, 0)
        self.assertEqual(order.total_idr, 50_000 + order.unique_code)
        self.assertEqual(seen["amount"], order.total_idr)
        caption = context.bot.send_photo.call_args.kwargs.get("caption", "")
        self.assertNotIn("Pajak QRIS", caption)


if __name__ == "__main__":
    unittest.main()
