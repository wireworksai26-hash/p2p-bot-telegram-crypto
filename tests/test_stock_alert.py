"""Peringatan stok menipis ke admin: batas USD, anti-spam, pengingat, dan kabar pulih."""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import WalletBalance  # noqa: E402
from services import stock_alert as sa  # noqa: E402

PRICES = {"USDT": 1.0, "SOL": 100.0, "BNB": 600.0}
ADDR = "0x" + "a" * 40


class PeringatanStok(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        sa._state.clear()
        self.now_dt = datetime.utcnow()
        self._set("BSC", "USDT", "2", reserved="0")        # $2  -> kritis
        self._set("SOLANA", "SOL", "0.1")                   # $10 -> menipis
        self._set("BSC", "BNB", "1")                        # $600 -> aman

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def _set(self, net, sym, balance, reserved="0", fresh=True):
        db = SessionLocal()
        try:
            row = db.query(WalletBalance).filter_by(network=net, symbol=sym).first()
            if row is None:
                row = WalletBalance(network=net, symbol=sym, address=ADDR)
                db.add(row)
            row.balance, row.reserved_balance = Decimal(balance), Decimal(reserved)
            row.sync_status = "OK"
            row.last_success_at = self.now_dt if fresh else self.now_dt - timedelta(hours=2)
            db.commit()
        finally:
            db.close()

    async def _check(self, now=1000.0):
        with patch("bot.utils.telegram_utils.notify_admins", new=AsyncMock()) as notify:
            result = await sa.check_stock_levels(object(), prices=PRICES, now=now, now_dt=self.now_dt)
        return result, notify

    async def test_alert_pertama_lengkap_dan_tidak_spam(self):
        result, notify = await self._check()
        self.assertTrue(result["alert"])
        text = notify.await_args.args[1]
        self.assertIn("PERINGATAN STOK MENIPIS", text)
        self.assertIn("KRITIS", text)
        self.assertIn("USDT</b> (BSC)", text)
        self.assertIn("$2.00", text)
        self.assertIn("SOL</b> (SOLANA)", text)
        self.assertIn(ADDR, text, "alamat isi ulang ditampilkan")
        self.assertNotIn("BNB", text)
        self.assertLess(text.index("USDT"), text.index("SOL"), "kritis di atas menipis")
        callbacks = [b.callback_data for row in notify.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("admin_panel_sync_wallets", callbacks)
        self.assertEqual(notify.await_args.kwargs["kind"], "alarm")

        result, notify = await self._check(now=1300.0)
        self.assertFalse(result["alert"], "kondisi sama tidak dikirim ulang")
        notify.assert_not_awaited()

    async def test_makin_parah_dikirim_lagi_dan_pengingat_berkala(self):
        await self._check()
        self._set("SOLANA", "SOL", "0.01")                  # $1 -> kritis
        result, notify = await self._check(now=1300.0)
        self.assertTrue(result["alert"])
        self.assertIn("PERINGATAN STOK MENIPIS", notify.await_args.args[1])

        result, notify = await self._check(now=1300.0 + sa.REMIND_SECONDS)
        self.assertTrue(result["alert"])
        self.assertIn("PENGINGAT", notify.await_args.args[1])

    async def test_stok_diisi_admin_dikabari_aman(self):
        await self._check()
        self._set("BSC", "USDT", "500")
        result, notify = await self._check(now=1300.0)
        self.assertEqual(result["recovered"], [("BSC", "USDT")])
        self.assertIn("Stok sudah aman kembali", notify.await_args.args[1])

    async def test_stok_terkunci_payout_tidak_dihitung(self):
        self._set("BSC", "BNB", "1", reserved="0.995")       # tersedia 0.005 BNB = $3 -> menipis
        _, notify = await self._check()
        self.assertIn("BNB", notify.await_args.args[1])

    async def test_data_basi_diabaikan(self):
        self._set("BSC", "USDT", "0", fresh=False)
        _, notify = await self._check()
        self.assertNotIn("USDT", notify.await_args.args[1])

    def test_tanpa_harga_pakai_batas_jumlah_koin(self):
        self.assertEqual(sa.classify(3.0, None, "APT"), "low")
        self.assertEqual(sa.classify(0.5, None, "APT"), "critical")
        self.assertEqual(sa.classify(50.0, None, "APT"), "ok")
        self.assertEqual(sa.classify(1.0, None, "XYZ"), "ok")


if __name__ == "__main__":
    unittest.main()
