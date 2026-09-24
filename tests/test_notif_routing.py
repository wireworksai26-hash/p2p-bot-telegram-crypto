"""Regresi routing notifikasi per topik fitur + salinan konfirmasi ke DM admin."""
import asyncio
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "DATABASE_URL": "sqlite:///:memory:",
    "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "6440006997,8710049667",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "0x" + "1" * 64,
})

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, NotificationTarget
from bot.utils.telegram_utils import (
    admin_notification_targets,
    normalisasi_kind,
    notify_admins,
)

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
TestingSession = sessionmaker(bind=engine)

db = TestingSession()
db.add(NotificationTarget(kind="jual", chat_id="-100123", thread_id="55",
                          title="Monitoring / thread 55"))
db.commit()
db.close()


class BotPalsu:
    def __init__(self):
        self.terkirim = []

    async def send_message(self, chat_id=None, text=None, parse_mode=None,
                           reply_markup=None, message_thread_id=None, **kw):
        self.terkirim.append((str(chat_id), message_thread_id, bool(reply_markup)))
        return True


class TestRoutingNotif(unittest.TestCase):
    def setUp(self):
        from config.settings import settings
        self._lama = settings.ADMIN_CHAT_IDS
        settings.ADMIN_CHAT_IDS = [6440006997, 8710049667]

    def tearDown(self):
        from config.settings import settings
        settings.ADMIN_CHAT_IDS = self._lama

    def test_normalisasi_jenis(self):
        self.assertEqual(normalisasi_kind("buy"), "beli")
        self.assertEqual(normalisasi_kind("sell"), "jual")
        self.assertEqual(normalisasi_kind("swap"), "convert")
        self.assertEqual(normalisasi_kind(None), "ops")

    def test_target_jual_mengarah_ke_thread(self):
        with patch("database.connection.SessionLocal", TestingSession):
            t = admin_notification_targets("jual")
        self.assertEqual(t, [("-100123", 55)])

    def test_target_belum_dipasang_fallback_dm_admin(self):
        with patch("database.connection.SessionLocal", TestingSession):
            t = admin_notification_targets("beli")
        self.assertEqual([c for c, _ in t], ["6440006997", "8710049667"])

    def test_notif_masuk_ke_thread_yang_benar(self):
        bot = BotPalsu()
        with patch("database.connection.SessionLocal", TestingSession):
            asyncio.run(notify_admins(bot, "halo", kind="jual"))
        self.assertIn(("-100123", 55, False), bot.terkirim)

    def test_pesan_ber_tombol_disalin_ke_dm_admin(self):
        bot = BotPalsu()
        with patch("database.connection.SessionLocal", TestingSession):
            asyncio.run(notify_admins(bot, "konfirmasi", kind="jual", reply_markup="KB"))
        chat = [c for c, _, _ in bot.terkirim]
        self.assertIn("-100123", chat)
        self.assertIn("6440006997", chat)
        self.assertIn("8710049667", chat)

    def test_tanpa_target_tidak_duplikat_dm(self):
        bot = BotPalsu()
        with patch("database.connection.SessionLocal", TestingSession):
            asyncio.run(notify_admins(bot, "info", kind="beli", reply_markup="KB"))
        chat = [c for c, _, _ in bot.terkirim]
        self.assertEqual(sorted(set(chat)), ["6440006997", "8710049667"])


if __name__ == "__main__":
    unittest.main()
