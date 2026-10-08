"""Testimoni channel selalu terkirim + kolom waktu selesai (WIB) di rekap CSV.

Akar masalah testimoni telat: detector/watchdog/buy meneruskan telegram Application
(services.bot_runtime.bot_app), yang tidak punya send_message, sehingga posting gagal diam-diam
dan baru muncul setelah admin menjalankan /postlasttesti (yang memakai context.bot).
"""
import asyncio
import csv
import io
import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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

from database.connection import Base, SessionLocal, engine
from database.models import Order, User
from services import testimony_service as ts
from services.report_service import generate_weekly_report_csv_buffer, get_weekly_transactions_data


class _FakeApplication:
    """Meniru telegram.ext.Application: tidak punya send_message/get_me, hanya `.bot`."""

    def __init__(self, bot):
        self.bot = bot


def _bot():
    bot = AsyncMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="TokoKoinID_Bot"))
    bot.send_message = AsyncMock()
    return bot


def _order(order_id, status="completed", **extra):
    return Order(
        order_id=order_id, telegram_id=999, order_type="buy", crypto_symbol="USDT", network="BSC",
        crypto_amount=Decimal("5"), price_per_unit=16000, nominal_idr=80000, fee_idr=2000,
        total_idr=80000, status=status, payout_tx_hash="0x" + "ab" * 32, **extra)


class TestTestimonyDelivery(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        ts._scheduled_order_ids.clear()
        ts._sweep_attempts.clear()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def test_application_diteruskan_tetap_terkirim(self):
        bot = _bot()
        ok = asyncio.run(ts.post_transaction_testimony(_FakeApplication(bot), _order("ORD-APP")))
        self.assertTrue(ok)
        bot.send_message.assert_awaited_once()

    def test_objek_tanpa_bot_ditolak_tanpa_error(self):
        self.assertFalse(asyncio.run(ts.post_transaction_testimony(object(), _order("ORD-X"))))

    def test_sweeper_memposting_order_tertunda_sekali_saja(self):
        db = SessionLocal()
        now = datetime.utcnow()
        db.add(_order("ORD-PENDING", completed_at=now))
        db.add(_order("ORD-SUDAH", completed_at=now, testimony_posted_at=now))
        db.add(_order("ORD-LAMA", completed_at=now - timedelta(days=30)))
        db.add(_order("ORD-BELUM", status="pending"))
        db.commit()
        db.close()

        bot = _bot()
        app = _FakeApplication(bot)
        self.assertEqual(asyncio.run(ts.sweep_unposted_testimonies(app)), 1)
        self.assertEqual(bot.send_message.await_count, 1)

        db = SessionLocal()
        try:
            posted = db.query(Order).filter(Order.order_id == "ORD-PENDING").one().testimony_posted_at
        finally:
            db.close()
        self.assertIsNotNone(posted)

        ts._scheduled_order_ids.clear()  # simulasi proses restart
        self.assertEqual(asyncio.run(ts.sweep_unposted_testimonies(app)), 0)
        self.assertEqual(bot.send_message.await_count, 1, "tidak boleh posting dobel")

    def test_sweeper_mencoba_ulang_setelah_gagal_kirim(self):
        db = SessionLocal()
        db.add(_order("ORD-RETRY", completed_at=datetime.utcnow()))
        db.commit()
        db.close()

        bot = _bot()
        bot.send_message = AsyncMock(side_effect=[RuntimeError("flood"), None])
        self.assertEqual(asyncio.run(ts.sweep_unposted_testimonies(bot)), 0)
        self.assertEqual(asyncio.run(ts.sweep_unposted_testimonies(bot)), 1)


class TestReportCompletedTime(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add(User(telegram_id=999, username="trader"))
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def test_csv_memuat_waktu_selesai_wib_sebagai_teks(self):
        db = SessionLocal()
        try:
            done = datetime.utcnow().replace(hour=10, minute=30, second=5, microsecond=0)
            db.add(_order("ORD-DONE", completed_at=done))
            db.add(_order("ORD-OPEN", status="pending"))
            db.commit()

            rows = list(csv.reader(io.StringIO(
                generate_weekly_report_csv_buffer(get_weekly_transactions_data(db, 7)).getvalue().decode("utf-8-sig")
            )))
        finally:
            db.close()

        header = rows[0]
        i_done = header.index("Waktu Transaksi Selesai (WIB)")
        by_id = {r[header.index("ID Order")]: r for r in rows[1:]}
        self.assertEqual(by_id["ORD-DONE"][i_done], done.replace(hour=17).strftime("%Y-%m-%d %H:%M:%S") + " WIB")
        self.assertEqual(by_id["ORD-OPEN"][i_done], "-")
        self.assertTrue(by_id["ORD-DONE"][header.index("Waktu Order Dibuat (WIB)")].endswith(" WIB"))


class TestReportButtonSendsFile(unittest.TestCase):
    def test_klik_rekap_langsung_mengirim_file(self):
        from bot.handlers import admin

        async def _run():
            bot = AsyncMock()
            query = SimpleNamespace(
                data="admin_panel_weekly_report",
                from_user=SimpleNamespace(id=999),
                message=SimpleNamespace(chat_id=999),
                answer=AsyncMock(),
                edit_message_text=AsyncMock(),
            )
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=999))
            context = SimpleNamespace(bot=bot, user_data={}, args=[])
            from config.settings import settings
            orig = list(getattr(settings, "ADMIN_CHAT_IDS", []))
            settings.ADMIN_CHAT_IDS = [999]
            Base.metadata.create_all(bind=engine)
            try:
                await admin.admin_panel_callback(update, context)
            finally:
                settings.ADMIN_CHAT_IDS = orig
                Base.metadata.drop_all(bind=engine)
            return bot

        bot = asyncio.run(_run())
        bot.send_document.assert_awaited_once()
        self.assertEqual(bot.send_document.await_args.kwargs["chat_id"], 999)


if __name__ == "__main__":
    unittest.main()
