"""K4 — user yang di-/ban tidak boleh bertransaksi apa pun.

Dulu `is_banned` hanya dipakai campaign/reward: user banned tetap bisa Beli,
Jual, Convert, Topup, dan memakai Saldo Bot. Tes ini menggerakkan bot seperti
Telegram (Update asli -> Application asli), dengan kontrol user tidak-banned
pada langkah yang sama agar tes tidak lulus karena alur memang rusak.
"""
import os
import unittest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from tests._settings_guard import e2e_setup, e2e_teardown  # noqa: E402
from tests import test_e2e_bot_flows as _e2e  # noqa: E402  (modul, bukan kelas: hindari tes dobel)
from database.connection import SessionLocal  # noqa: E402
from database.models import Order, TopupOrder, User  # noqa: E402
from config.settings import settings  # noqa: E402


class BanE2E(unittest.IsolatedAsyncioTestCase):
    BANNED, NORMAL = 80001, 80002
    WALLET = _e2e.BotFlowE2E.WALLET

    async def asyncSetUp(self):
        await e2e_setup(self)
    async def asyncTearDown(self):
        await e2e_teardown(self)
    _user = _e2e.BotFlowE2E._user
    _dispatch = _e2e.BotFlowE2E._dispatch
    say = _e2e.BotFlowE2E.say
    tap = _e2e.BotFlowE2E.tap
    open_buy_amount_step = _e2e.BotFlowE2E.open_buy_amount_step

    async def _register_and_ban(self):
        await self.say(self.BANNED, "/start")
        await self.say(self.NORMAL, "/start")
        db = SessionLocal()
        try:
            db.query(User).filter(User.telegram_id == self.BANNED).update({User.is_banned: True})
            db.commit()
        finally:
            db.close()

    def _count(self, model, uid):
        db = SessionLocal()
        try:
            return db.query(model).filter(model.telegram_id == uid).count()
        finally:
            db.close()

    async def _buy_qris(self, uid):
        await self.say(uid, "/start")
        await self.tap(uid, "menu_buy", from_screen=False)
        await self.tap(uid, "buy_sym_USDT", from_screen=False)
        await self.tap(uid, "buy_net_USDT_BSC", from_screen=False)
        await self.say(uid, "50000")
        await self.say(uid, self.WALLET)
        await self.tap(uid, "paymethod_GOPAY_QRIS", from_screen=False)
        return await self.tap(uid, "buy_confirm", from_screen=False)

    async def test_banned_tidak_bisa_beli_kontrol_normal_bisa(self):
        await self._register_and_ban()
        await self._buy_qris(self.NORMAL)
        self.assertEqual(self._count(Order, self.NORMAL), 1, "kontrol: user normal harus bisa membuat order")
        shown = await self._buy_qris(self.BANNED)
        self.assertEqual(self._count(Order, self.BANNED), 0, "user banned tidak boleh membuat order beli")
        self.assertIn("diblokir", shown.lower())

    async def test_banned_tidak_bisa_topup(self):
        await self._register_and_ban()
        for uid in (self.NORMAL, self.BANNED):
            await self.say(uid, "/start")
            await self.tap(uid, "menu_balance", from_screen=False)
            await self.tap(uid, "start_topup_qris", from_screen=False)
            await self.tap(uid, "topup_nom_50000", from_screen=False)
        self.assertEqual(self._count(TopupOrder, self.NORMAL), 1, "kontrol: user normal harus bisa topup")
        self.assertEqual(self._count(TopupOrder, self.BANNED), 0, "user banned tidak boleh topup")

    async def test_banned_tidak_bisa_jual(self):
        await self._register_and_ban()
        shown = await self.tap(self.BANNED, "menu_sell", from_screen=False)
        self.assertIn("diblokir", shown.lower())
        self.assertNotIn("pilih koin crypto yang ingin anda jual", shown.lower())

    async def test_banned_tidak_bisa_pakai_saldo_bot(self):
        await self._register_and_ban()
        db = SessionLocal()
        try:
            db.query(User).filter(User.telegram_id == self.BANNED).update({User.balance_idr: 1_000_000})
            db.commit()
        finally:
            db.close()
        await self.say(self.BANNED, "/start")
        await self.tap(self.BANNED, "menu_buy", from_screen=False)
        await self.tap(self.BANNED, "buy_sym_USDT", from_screen=False)
        await self.tap(self.BANNED, "buy_net_USDT_BSC", from_screen=False)
        await self.say(self.BANNED, "50000")
        await self.say(self.BANNED, self.WALLET)
        await self.tap(self.BANNED, "paymethod_BOT_BALANCE", from_screen=False)
        await self.tap(self.BANNED, "buy_confirm", from_screen=False)
        self.assertEqual(self._count(Order, self.BANNED), 0)
        db = SessionLocal()
        try:
            self.assertEqual(float(db.query(User).get(self.BANNED).balance_idr), 1_000_000)
        finally:
            db.close()

    async def test_admin_tidak_terkena_ban(self):
        admin = settings.ADMIN_CHAT_IDS[0]
        await self.say(admin, "/start")
        db = SessionLocal()
        try:
            db.query(User).filter(User.telegram_id == admin).update({User.is_banned: True})
            db.commit()
        finally:
            db.close()
        shown = await self.say(admin, "/admin")
        self.assertNotIn("diblokir", shown.lower())

    async def test_unban_memulihkan_akses(self):
        await self._register_and_ban()
        db = SessionLocal()
        try:
            db.query(User).filter(User.telegram_id == self.BANNED).update({User.is_banned: False})
            db.commit()
        finally:
            db.close()
        await self._buy_qris(self.BANNED)
        self.assertEqual(self._count(Order, self.BANNED), 1)


if __name__ == "__main__":
    unittest.main()
