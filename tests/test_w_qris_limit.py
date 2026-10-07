"""Total tagihan QRIS tidak boleh melewati batas QRIS BI Rp 10.000.000.

Nominal dibatasi Rp 10 jt, tetapi total = nominal + pajak QRIS 0,3% + kode unik bisa
melewati Rp 10 jt. QRIS dinamis lalu gagal dibuat dan bot jatuh ke QR statis padahal
teks bilang "nominal muncul otomatis" -> user mengetik sendiri, rawan salah nominal.
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from tests._settings_guard import e2e_setup, e2e_teardown  # noqa: E402
from tests import test_e2e_bot_flows as _e2e  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order, TopupOrder, User  # noqa: E402
from bot.handlers import balance  # noqa: E402
from services.fee_service import QRIS_MAX_TOTAL_IDR, qris_max_nominal, calculate_qris_mdr  # noqa: E402

U = 95001


class Limit(unittest.TestCase):
    def test_nominal_maksimum_selalu_muat(self):
        n = qris_max_nominal()
        self.assertLessEqual(n + calculate_qris_mdr(n) + 400, QRIS_MAX_TOTAL_IDR)
        self.assertGreater(n + 1 + calculate_qris_mdr(n + 1) + 400, QRIS_MAX_TOTAL_IDR)


class TopupLimit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add(User(telegram_id=U))
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    async def test_topup_10jt_tidak_membuat_tagihan_di_atas_batas(self):
        msg = MagicMock()
        msg.reply_text = AsyncMock(return_value=MagicMock(delete=AsyncMock(), edit_text=AsyncMock()))
        update = SimpleNamespace(callback_query=None, message=msg,
                                 effective_user=SimpleNamespace(id=U, username="u", full_name="U"))
        ctx = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()))
        with patch.object(balance, "SessionLocal", side_effect=lambda: SessionLocal()):
            await balance.generate_and_send_qris(update, ctx, 10_000_000)
        db = SessionLocal()
        try:
            for t in db.query(TopupOrder).all():
                self.assertLessEqual(t.amount_idr, QRIS_MAX_TOTAL_IDR)
        finally:
            db.close()


class BuyLimitE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await e2e_setup(self)

    async def asyncTearDown(self):
        await e2e_teardown(self)

    _user = _e2e.BotFlowE2E._user
    _dispatch = _e2e.BotFlowE2E._dispatch
    say = _e2e.BotFlowE2E.say
    tap = _e2e.BotFlowE2E.tap

    async def test_beli_qris_10jt_tidak_melewati_batas(self):
        await self.say(U, "/start")
        await self.tap(U, "menu_buy", from_screen=False)
        await self.tap(U, "buy_sym_USDT", from_screen=False)
        await self.tap(U, "buy_net_USDT_BSC", from_screen=False)
        await self.say(U, "10000000")
        await self.say(U, _e2e.BotFlowE2E.WALLET)
        await self.tap(U, "paymethod_GOPAY_QRIS", from_screen=False)
        await self.tap(U, "buy_confirm", from_screen=False)
        db = SessionLocal()
        try:
            for o in db.query(Order).filter(Order.telegram_id == U).all():
                self.assertLessEqual(o.total_idr, QRIS_MAX_TOTAL_IDR)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
