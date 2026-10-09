"""Perbaikan minor: menu ☰ default, jam layanan 23.59, angka koin bisa disalin (admin), testimoni agresif."""
import asyncio
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
    "ADMIN_CHAT_IDS": "999",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from telegram import BotCommand
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from database.connection import Base, SessionLocal, engine
from database.models import Order, User
from services import testimony_service as ts
from bot.utils import command_menu as cmenu


def _run(coro):
    return asyncio.run(coro)


# ── 1. Menu ☰ default ────────────────────────────────────────────────────────
class TestDefaultMenu(unittest.TestCase):
    def setUp(self):
        cmenu._default_menu_ok = False

    def _bot(self, current):
        bot = SimpleNamespace(get_my_commands=AsyncMock(return_value=current), set_my_commands=AsyncMock())
        return bot

    def test_menu_kosong_dipasang(self):
        bot = self._bot([])
        self.assertTrue(_run(cmenu.ensure_default_menu(bot)))
        sent = bot.set_my_commands.await_args.args[0]
        self.assertEqual([c.command for c in sent], ["start", "beli", "jual", "convert", "cancel"])

    def test_menu_sudah_benar_tidak_dipasang_ulang_dan_hanya_sekali_per_proses(self):
        current = [BotCommand(c, d) for c, d in cmenu.USER_COMMAND_MENU]
        bot = self._bot(current)
        self.assertTrue(_run(cmenu.ensure_default_menu(bot)))
        _run(cmenu.ensure_default_menu(bot))
        bot.set_my_commands.assert_not_awaited()
        self.assertEqual(bot.get_my_commands.await_count, 1)

    def test_gagal_tidak_melempar_dan_dicoba_lagi_lain_kali(self):
        bot = self._bot([])
        bot.get_my_commands.side_effect = NetworkError("down")
        self.assertFalse(_run(cmenu.ensure_default_menu(bot)))
        bot.get_my_commands.side_effect = None
        self.assertTrue(_run(cmenu.ensure_default_menu(bot)))

    def test_startup_dan_start_memakai_daftar_yang_sama(self):
        import main
        self.assertIs(main.BOT_COMMAND_MENU, cmenu.USER_COMMAND_MENU)
        src = (ROOT / "bot" / "handlers" / "start.py").read_text(encoding="utf-8")
        self.assertIn("ensure_default_menu(context.bot)", src)


# ── 2. Jam layanan ───────────────────────────────────────────────────────────
class TestServiceHours(unittest.TestCase):
    def test_semua_teks_jam_layanan_23_59(self):
        for rel in ("bot/handlers/sell.py", "bot/utils/messages.py", "bot/utils/user_guide.py",
                    "bot/utils/admin_guide.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("23.59 WIB", text, rel)
            self.assertNotIn("22.00 WIB", text, rel)
            self.assertNotIn("22.00", text.replace("22.000", ""), rel)


# ── 3. Angka koin bisa disalin ───────────────────────────────────────────────
class TestCopyableAmounts(unittest.TestCase):
    def test_helper_hanya_angka_di_dalam_code(self):
        from bot.utils.formatter import format_crypto_copy
        self.assertEqual(format_crypto_copy(5, "USDT"), "<code>5.0000</code> USDT")
        self.assertEqual(format_crypto_copy(0.00123456, "ETH"), "<code>0.00123456</code> ETH")

    def test_exact_membuang_desimal_mentah_decimal_db(self):
        from bot.utils.formatter import format_crypto_copy
        self.assertEqual(format_crypto_copy(Decimal("5.000000000000000000"), "USDT", exact=True),
                         "<code>5.0000</code> USDT")
        self.assertEqual(format_crypto_copy("1.5", "SOL", exact=True).count("<code>"), 1)

    def test_nilai_aneh_tidak_melempar(self):
        from bot.utils.formatter import format_crypto_copy
        self.assertIn("<code>", format_crypto_copy(None, "TON"))
        self.assertIn("<code>", format_crypto_copy("abc", "TON", exact=True))

    def test_dashboard_jual_admin_memuat_angka_tersalin(self):
        from bot.handlers import admin
        Base.metadata.create_all(bind=engine)
        try:
            db = SessionLocal()
            db.add(User(telegram_id=111))
            db.add(Order(order_id="ORD-S1", telegram_id=111, order_type="sell", crypto_symbol="USDT", network="BSC",
                         crypto_amount=Decimal("12.345600000000000000"), price_per_unit=16000, nominal_idr=200000,
                         fee_idr=4000, total_idr=196000, status="CRYPTO_CONFIRMED", deposit_wallet="0xabc"))
            db.commit()
            db.close()
            q = SimpleNamespace(data="admin_sellorders_0", from_user=SimpleNamespace(id=999), answer=AsyncMock(),
                                edit_message_text=AsyncMock())
            upd = SimpleNamespace(callback_query=q, message=None, effective_user=SimpleNamespace(id=999))
            with patch("bot.handlers.admin.is_admin", return_value=True):
                _run(admin.sellorders_handler(upd, SimpleNamespace(user_data={})))
            text = q.edit_message_text.call_args.args[0] if q.edit_message_text.call_args.args \
                else q.edit_message_text.call_args.kwargs["text"]
            self.assertIn("<code>12.3456</code> USDT", text)
        finally:
            Base.metadata.drop_all(bind=engine)

    def test_pesan_admin_jual_dan_convert_memakai_helper(self):
        for rel, needles in (
            ("bot/handlers/sell.py", ("format_crypto_copy(order.crypto_amount, order.crypto_symbol, exact=True)",)),
            ("bot/handlers/swap.py", ("format_crypto_copy(order.crypto_amount, order.crypto_symbol, exact=True)",
                                      "format_crypto_copy(order.target_crypto_amount, order.target_crypto_symbol, exact=True)")),
            ("services/detector.py", ("format_crypto_copy(verified.get('amount')",
                                      "format_crypto_copy(order.target_crypto_amount or 0, order.target_crypto_symbol, exact=True)")),
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            for n in needles:
                self.assertIn(n, text, rel)


# ── 5. Menu default tidak menahan /start ─────────────────────────────────────
class TestMenuDoesNotStallStart(unittest.TestCase):
    def test_api_lambat_dibatasi_waktu_dan_tidak_melempar(self):
        cmenu._default_menu_ok = False

        async def slow(*a, **k):
            await asyncio.sleep(30)

        bot = SimpleNamespace(get_my_commands=slow, set_my_commands=AsyncMock())
        with patch("bot.utils.command_menu.asyncio.wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            self.assertFalse(_run(cmenu.ensure_default_menu(bot)))
        cmenu._default_menu_ok = False


# ── 4. Testimoni agresif ─────────────────────────────────────────────────────
def _snap(oid="ORD-T1"):
    return SimpleNamespace(order_id=oid, order_type="buy", crypto_symbol="USDT", network="BSC",
                           target_crypto_symbol=None, target_network=None, total_idr=80000, nominal_idr=80000,
                           payout_tx_hash="0x" + "ab" * 32, tx_hash=None, deposit_tx_hash=None,
                           telegram_id=999, user_username="tester88")


class TestAggressiveTestimony(unittest.TestCase):
    def setUp(self):
        ts._bot_username_cache = None
        ts._scheduled_order_ids.clear()
        ts._sweep_attempts.clear()
        ts._last_error.clear()
        ts._alerted_orders.clear()
        patcher = patch("services.testimony_service.asyncio.sleep", new=AsyncMock())
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def _bot(self, **kw):
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="TokoKoinID_bot")),
                              send_message=AsyncMock(**kw))
        return bot

    def test_flood_limit_menunggu_lalu_berhasil(self):
        bot = self._bot(side_effect=[RetryAfter(3), None])
        self.assertTrue(_run(ts.post_transaction_testimony(bot, _snap())))
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(self.sleep.await_args.args[0], 4)  # retry_after + 1

    def test_gangguan_jaringan_dicoba_ulang_hingga_3x(self):
        bot = self._bot(side_effect=[NetworkError("x"), NetworkError("y"), None])
        self.assertTrue(_run(ts.post_transaction_testimony(bot, _snap())))
        self.assertEqual(bot.send_message.await_count, 3)

    def test_timeout_tidak_dikirim_ulang_agar_tidak_dobel_di_channel(self):
        bot = self._bot(side_effect=[TimedOut("read timeout"), None])
        self.assertFalse(_run(ts.post_transaction_testimony(bot, _snap("ORD-TO"))))
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertIn("TimedOut", ts._last_error["ORD-TO"])

    def test_username_gagal_diambil_tidak_disimpan_selamanya(self):
        bot = self._bot()
        bot.get_me = AsyncMock(side_effect=[NetworkError("down"), SimpleNamespace(username="NamaAsli_bot")])
        _run(ts.post_transaction_testimony(bot, _snap("A")))
        self.assertIsNone(ts._bot_username_cache)
        _run(ts.post_transaction_testimony(bot, _snap("B")))
        self.assertEqual(ts._bot_username_cache, "NamaAsli_bot")

    def test_error_permanen_tidak_dicoba_ulang_dan_penyebab_tercatat(self):
        bot = self._bot(side_effect=BadRequest("Chat not found"))
        self.assertFalse(_run(ts.post_transaction_testimony(bot, _snap("ORD-P"))))
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertIn("Chat not found", ts._last_error["ORD-P"])

    def test_username_bot_di_cache_tidak_get_me_tiap_posting(self):
        bot = self._bot()
        _run(ts.post_transaction_testimony(bot, _snap("A")))
        _run(ts.post_transaction_testimony(bot, _snap("B")))
        self.assertEqual(bot.get_me.await_count, 1)
        self.assertIn("@TokoKoinID_bot", bot.send_message.await_args.kwargs["text"])

    def test_alarm_admin_sekali_setelah_batas_percobaan(self):
        Base.metadata.create_all(bind=engine)
        try:
            db = SessionLocal()
            db.add(User(telegram_id=999))
            db.add(Order(order_id="ORD-STUCK", telegram_id=999, order_type="buy", crypto_symbol="USDT", network="BSC",
                         crypto_amount=Decimal("5"), price_per_unit=16000, nominal_idr=80000, fee_idr=3000,
                         total_idr=80000, status="completed", completed_at=datetime.utcnow()))
            db.commit()
            db.close()
            bot = self._bot(side_effect=BadRequest("Forbidden: bot is not a member of the channel"))
            alert = AsyncMock()
            with patch("bot.utils.telegram_utils.notify_admins", new=alert):
                for _ in range(ts._SWEEP_MAX_ATTEMPTS + 3):
                    _run(ts.sweep_unposted_testimonies(bot))
            self.assertEqual(alert.await_count, 1)
            text = alert.await_args.args[1]
            self.assertIn("ORD-STUCK", text)
            self.assertIn("not a member", text)
            self.assertIn("/posttesti ORD-STUCK", text)
        finally:
            Base.metadata.drop_all(bind=engine)

    def test_sweeper_dijadwalkan_tiap_10_detik(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        i = src.index("_job_sweep_testimonies,")
        self.assertIn("seconds=10", src[i:i + 120])


class TestSecretsAndHardening(unittest.TestCase):
    def test_httpx_tidak_menulis_url_request_ke_log(self):
        import logging
        import main  # noqa: F401  (mengatur level logger saat impor)
        self.assertGreaterEqual(logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").getEffectiveLevel(), logging.WARNING)

    def test_log_rpc_hanya_host(self):
        from services.tx_verifier import _host_only
        self.assertEqual(_host_only("https://mainnet.infura.io/v3/SECRETKEY123"), "mainnet.infura.io")
        self.assertEqual(_host_only(None), "?")

    def test_failure_reason_dipotong_agar_muat_kolom_postgres(self):
        from database import crud
        Base.metadata.create_all(bind=engine)
        try:
            db = SessionLocal()
            db.add(User(telegram_id=111))
            db.add(Order(order_id="ORD-LONG", telegram_id=111, order_type="buy", crypto_symbol="USDT", network="BSC",
                         crypto_amount=Decimal("1"), price_per_unit=1, nominal_idr=1, fee_idr=0, total_idr=1,
                         status="payout_processing"))
            db.commit()
            crud.update_order_status(db, "ORD-LONG", "manual_review", failure_reason="x" * 2000)
            self.assertEqual(len(db.query(Order).filter(Order.order_id == "ORD-LONG").one().failure_reason), 480)
            db.close()
        finally:
            Base.metadata.drop_all(bind=engine)

    def test_hint_kontrak_token_jual_tidak_lagi_nameerror(self):
        src = (ROOT / "bot" / "handlers" / "sell.py").read_text(encoding="utf-8")
        i = src.index("token_hint = \"\"")
        self.assertIn("from services.crypto_sender import CryptoSenderFactory", src[i:i + 200])


if __name__ == "__main__":
    unittest.main()
