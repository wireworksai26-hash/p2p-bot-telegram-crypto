"""T3 — /confirm hanya boleh menyelesaikan order yang memang sudah dibayar.

Dulu /confirm untuk order Beli/Convert tidak memeriksa status: order QRIS yang
belum dibayar, order expired/cancelled, atau Convert yang depositnya belum masuk
langsung jadi COMPLETED dan user diberi tahu sukses (statistik, Top Spender, dan
kunci wallet ikut terhitung). Salah ketik ID order sudah cukup.
"""
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
from database.models import Order  # noqa: E402

BUYER = 91001


class ConfirmGuardE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await e2e_setup(self)
    async def asyncTearDown(self):
        await e2e_teardown(self)
    _user = _e2e.BotFlowE2E._user
    _dispatch = _e2e.BotFlowE2E._dispatch
    say = _e2e.BotFlowE2E.say
    tap = _e2e.BotFlowE2E.tap

    def _add(self, order_id, status, order_type="buy", method="GOPAY_QRIS", paid_at=None):
        db = SessionLocal()
        try:
            db.add(Order(
                order_id=order_id, telegram_id=BUYER, order_type=order_type, crypto_symbol="USDT",
                network="BSC", crypto_amount=Decimal("1"), price_per_unit=17915, nominal_idr=17915,
                fee_idr=3000, total_idr=20915, buyer_wallet="0x" + "c" * 40, payment_method=method,
                status=status, paid_at=paid_at, created_at=datetime.utcnow()))
            db.commit()
        finally:
            db.close()

    def _status(self, order_id):
        db = SessionLocal()
        try:
            return db.query(Order).filter(Order.order_id == order_id).one().status
        finally:
            db.close()

    async def _confirm(self, order_id):
        return await self.say(settings.ADMIN_CHAT_IDS[0], f"/confirm {order_id}")

    async def test_qris_belum_dibayar_tidak_bisa_dikonfirmasi(self):
        self._add("ORD-UNPAID", "pending")
        await self._confirm("ORD-UNPAID")
        self.assertEqual(self._status("ORD-UNPAID"), "pending")

    async def test_expired_dan_cancelled_tidak_bisa_dikonfirmasi(self):
        for oid, st in (("ORD-EXP", "expired"), ("ORD-CAN", "cancelled")):
            self._add(oid, st)
            await self._confirm(oid)
            self.assertEqual(self._status(oid), st)

    async def test_convert_belum_deposit_tidak_bisa_dikonfirmasi(self):
        self._add("ORD-SWAP", "WAITING_CRYPTO_DEPOSIT", order_type="swap", method=None)
        await self._confirm("ORD-SWAP")
        self.assertEqual(self._status("ORD-SWAP"), "WAITING_CRYPTO_DEPOSIT")

    async def test_user_tidak_diberi_tahu_sukses_bila_ditolak(self):
        self._add("ORD-UNPAID", "pending")
        start = len(_e2e.FakeTelegram.calls)
        await self._confirm("ORD-UNPAID")
        to_buyer = [p for e, p in _e2e.FakeTelegram.calls[start:] if int(p.get("chat_id") or 0) == BUYER]
        self.assertEqual(to_buyer, [])

    async def test_kontrol_manual_review_bisa_dikonfirmasi(self):
        self._add("ORD-MR", "manual_review", paid_at=datetime.utcnow())
        await self._confirm("ORD-MR")
        self.assertEqual(self._status("ORD-MR"), "completed")

    async def test_kontrol_saldo_bot_terpotong_bisa_dikonfirmasi(self):
        self._add("ORD-BAL", "pending", method="BOT_BALANCE", paid_at=datetime.utcnow())
        await self._confirm("ORD-BAL")
        self.assertEqual(self._status("ORD-BAL"), "completed")


if __name__ == "__main__":
    unittest.main()
