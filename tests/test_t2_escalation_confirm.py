"""T2 — tombol eskalasi "Konfirmasi Deposit (sudah dicek)" tidak boleh menyelesaikan order.

Dulu tombol itu memanggil admin_force_sell -> _finish_sell_order: order langsung
COMPLETED, user menerima pesan "Rupiah sudah ditransfer", dan pesan admin diberi
tanda "RUPIAH SUDAH DITRANSFER" padahal admin baru mengecek deposit. Rupiah bisa
tidak pernah dibayar.
"""
import json
import os
import unittest
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from tests._settings_guard import e2e_setup, e2e_teardown  # noqa: E402
from tests import test_e2e_bot_flows as _e2e  # noqa: E402
from config.settings import settings  # noqa: E402
from database.connection import SessionLocal  # noqa: E402
from database.models import DepositClaim, Order  # noqa: E402
from services.detector import DepositDetector  # noqa: E402

SELLER = 90001
HASH = "0x" + "ab" * 32


class EscalationButtonE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await e2e_setup(self)
    async def asyncTearDown(self):
        await e2e_teardown(self)
    _user = _e2e.BotFlowE2E._user
    _dispatch = _e2e.BotFlowE2E._dispatch
    say = _e2e.BotFlowE2E.say
    tap = _e2e.BotFlowE2E.tap

    async def _escalated_button(self):
        await self.say(SELLER, "/start")
        db = SessionLocal()
        try:
            order = Order(
                order_id="ORD-ESC", telegram_id=SELLER, order_type="sell", crypto_symbol="USDT",
                network="BSC", crypto_amount=Decimal("10.0037"), price_per_unit=17915,
                nominal_idr=179_000, fee_idr=5_000, total_idr=174_000,
                buyer_wallet="BCA | 1234567890 | Budi", deposit_wallet=os.environ["EVM_WALLET_ADDRESS"],
                deposit_tx_hash=HASH, status="WAITING_CRYPTO_DEPOSIT", created_at=datetime.utcnow())
            db.add(order)
            db.commit()
            start = len(_e2e.FakeTelegram.calls)
            await DepositDetector().escalate_user_hash(db, order, HASH, "nominal tidak sesuai", self.app)
        finally:
            db.close()
        for endpoint, params in _e2e.FakeTelegram.calls[start:]:
            markup = params.get("reply_markup")
            markup = json.loads(markup) if isinstance(markup, str) else markup
            for row in (markup or {}).get("inline_keyboard", []):
                for b in row:
                    if "ORD-ESC" in (b.get("callback_data") or ""):
                        return b["callback_data"]
        self.fail("tombol eskalasi tidak ditemukan")

    def _order(self):
        db = SessionLocal()
        try:
            return db.query(Order).filter(Order.order_id == "ORD-ESC").one()
        finally:
            db.close()

    async def test_konfirmasi_deposit_tidak_menyelesaikan_order(self):
        data = await self._escalated_button()
        start = len(_e2e.FakeTelegram.calls)
        admin = settings.ADMIN_CHAT_IDS[0]
        await self.tap(admin, data, from_screen=False)
        order = self._order()
        self.assertEqual(order.status, "CRYPTO_CONFIRMED", "deposit dikonfirmasi, Rupiah BELUM ditransfer")
        sent = _e2e.FakeTelegram.calls[start:]
        to_seller = " ".join(str(p.get("text") or "") for e, p in sent
                             if int(p.get("chat_id") or 0) == SELLER)
        self.assertNotIn("berhasil", to_seller.lower(), "user tidak boleh diberi tahu dana sudah dikirim")
        everything = json.dumps([p for _, p in sent])
        self.assertNotIn("RUPIAH SUDAH DITRANSFER", everything)
        self.assertIn("admin_confirm_sell_ORD-ESC", everything,
                      "admin harus mendapat tombol 'Sudah Ditransfer' untuk langkah berikutnya")

    async def test_hash_dikunci_agar_tidak_dipakai_order_lain(self):
        data = await self._escalated_button()
        await self.tap(settings.ADMIN_CHAT_IDS[0], data, from_screen=False)
        db = SessionLocal()
        try:
            self.assertTrue(db.query(DepositClaim).filter(DepositClaim.order_id == "ORD-ESC").first())
        finally:
            db.close()

    async def test_bukan_admin_ditolak(self):
        data = await self._escalated_button()
        await self.tap(SELLER, data, from_screen=False)
        self.assertEqual(self._order().status, "WAITING_CRYPTO_DEPOSIT")

    async def test_tombol_dua_kali_tidak_mengubah_order_selesai(self):
        data = await self._escalated_button()
        admin = settings.ADMIN_CHAT_IDS[0]
        await self.tap(admin, data, from_screen=False)
        db = SessionLocal()
        try:
            db.query(Order).filter(Order.order_id == "ORD-ESC").update({Order.status: "completed"})
            db.commit()
        finally:
            db.close()
        await self.tap(admin, data, from_screen=False)
        self.assertEqual(self._order().status, "completed")


if __name__ == "__main__":
    unittest.main()
