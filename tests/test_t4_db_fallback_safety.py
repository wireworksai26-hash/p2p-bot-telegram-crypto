"""T4/B11 — fallback SQLite tidak boleh membocorkan password atau menelan transaksi.

Dulu: koneksi Postgres gagal sekali -> log mencetak DATABASE_URL lengkap (password)
lalu bot diam-diam memakai SQLite kosong. Order, topup, dan saldo yang dibuat di
jendela itu hilang saat restart berikutnya.
"""
import logging
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from config.settings import settings  # noqa: E402
from database import connection  # noqa: E402
from tests._settings_guard import e2e_setup, e2e_teardown  # noqa: E402
from tests import test_e2e_bot_flows as _e2e  # noqa: E402
from database.connection import SessionLocal  # noqa: E402
from database.models import Order, TopupOrder  # noqa: E402

BAD_URL = "postgresql://postgres:SuperSecretPw123@postgres.railway.internal:5432/railway"


class FallbackEngine(unittest.TestCase):
    def setUp(self):
        self._url = settings.DATABASE_URL
        settings.DATABASE_URL = BAD_URL
        self._degraded = getattr(connection, "DB_DEGRADED", False)

    def tearDown(self):
        settings.DATABASE_URL = self._url
        connection.DB_DEGRADED = self._degraded

    def test_password_tidak_masuk_log(self):
        with self.assertLogs("database.connection", level=logging.WARNING) as logs, \
                patch.object(connection, "RETRY_DELAY_SECONDS", 0, create=True), \
                patch.object(connection, "CONNECT_ATTEMPTS", 2, create=True):
            connection._build_database_engine()
        self.assertNotIn("SuperSecretPw123", "\n".join(logs.output))

    def test_retry_sebelum_fallback(self):
        real = connection.create_engine
        attempts = []

        def flaky(url, **kw):
            attempts.append(url)
            if len(attempts) < 3:
                raise RuntimeError("could not translate host name")
            return real("sqlite:///:memory:")

        with patch.object(connection, "create_engine", side_effect=flaky), \
                patch.object(connection, "RETRY_DELAY_SECONDS", 0, create=True):
            eng = connection._build_database_engine()
        self.assertEqual(len(attempts), 3, "DNS yang telat siap harus dicoba ulang, bukan langsung fallback")
        self.assertFalse(connection.DB_DEGRADED)
        self.assertEqual(eng.url.drivername, "sqlite")  # engine 'Postgres' tiruan kita

    def test_fallback_menandai_mode_darurat(self):
        with patch.object(connection, "RETRY_DELAY_SECONDS", 0, create=True), \
                patch.object(connection, "CONNECT_ATTEMPTS", 2, create=True):
            eng = connection._build_database_engine()
        self.assertEqual(eng.url.drivername, "sqlite")
        self.assertTrue(connection.DB_DEGRADED)


class DegradedModeBlocksMoney(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await e2e_setup(self)
    _user = _e2e.BotFlowE2E._user
    _dispatch = _e2e.BotFlowE2E._dispatch
    say = _e2e.BotFlowE2E.say
    tap = _e2e.BotFlowE2E.tap
    open_buy_amount_step = _e2e.BotFlowE2E.open_buy_amount_step
    WALLET = _e2e.BotFlowE2E.WALLET
    USER = 92001

    async def asyncTearDown(self):
        connection.DB_DEGRADED = False
        await e2e_teardown(self)

    def _count(self, model):
        db = SessionLocal()
        try:
            return db.query(model).filter(model.telegram_id == self.USER).count()
        finally:
            db.close()

    async def test_mode_darurat_menolak_order_dan_topup(self):
        connection.DB_DEGRADED = True
        shown = await self.say(self.USER, "/start")
        self.assertIn("pemeliharaan", shown.lower(), "/start tetap dijawab dengan info pemeliharaan")
        await self.tap(self.USER, "menu_buy", from_screen=False)
        await self.tap(self.USER, "buy_sym_USDT", from_screen=False)
        await self.tap(self.USER, "buy_net_USDT_BSC", from_screen=False)
        await self.say(self.USER, "50000")
        await self.say(self.USER, self.WALLET)
        await self.tap(self.USER, "paymethod_GOPAY_QRIS", from_screen=False)
        await self.tap(self.USER, "buy_confirm", from_screen=False)
        await self.tap(self.USER, "menu_balance", from_screen=False)
        await self.tap(self.USER, "start_topup_qris", from_screen=False)
        await self.tap(self.USER, "topup_nom_50000", from_screen=False)
        self.assertEqual(self._count(Order), 0)
        self.assertEqual(self._count(TopupOrder), 0)

    async def test_kontrol_mode_normal_bisa_order(self):
        connection.DB_DEGRADED = False
        await self.open_buy_amount_step(self.USER)
        await self.say(self.USER, self.WALLET)
        await self.tap(self.USER, "paymethod_GOPAY_QRIS")
        await self.tap(self.USER, "buy_confirm")
        self.assertEqual(self._count(Order), 1)


if __name__ == "__main__":
    unittest.main()
