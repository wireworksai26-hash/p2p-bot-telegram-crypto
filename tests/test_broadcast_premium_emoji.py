"""/broadcast: emoji premium & format Telegram ikut terkirim, laporan gagal dirinci.

Bug: client mengirim emoji premium ☃️ (manusia salju), tetapi user menerima 👏. Broadcast hanya
mengambil teks polos, sehingga emoji premium berubah menjadi emoji standar pasangannya.
Laporan juga hanya menulis "Gagal (blocked)" untuk semua kegagalan.
"""
import os
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from telegram import Chat, Message, MessageEntity  # noqa: E402
from telegram.error import BadRequest, Forbidden  # noqa: E402

import database.models  # noqa: F401,E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import User  # noqa: E402
import bot.handlers.admin as admin_mod  # noqa: E402

SNOWMAN_ID = "5440660757194744323"


def _premium_message(text="/broadcast Selamat pagi ☃ semua"):
    """Pesan admin asli: ☃ adalah emoji premium (custom_emoji) dan 'semua' dicetak tebal."""
    offset = text.index("☃")
    entities = [MessageEntity("bot_command", 0, 10),
                MessageEntity("custom_emoji", offset, 1, custom_emoji_id=SNOWMAN_ID),
                MessageEntity("bold", offset + 2, 5)]
    return Message(1, datetime.now(), Chat(1, "private"), text=text, entities=entities)


def _as_handler_message(real):
    """Message asli tidak bisa diberi reply_text palsu; salin field yang dibaca handler."""
    return SimpleNamespace(text=real.text, entities=real.entities, text_html=real.text_html, caption=None,
                           photo=None, document=None, reply_to_message=None, reply_text=AsyncMock())


class SumberTeks(unittest.TestCase):
    def test_emoji_premium_dan_format_dipertahankan(self):
        html = admin_mod._broadcast_source(_premium_message())
        self.assertIn(f'<tg-emoji emoji-id="{SNOWMAN_ID}">☃</tg-emoji>', html)
        self.assertIn("<b>semua</b>", html)

    def test_tanpa_format_teks_apa_adanya(self):
        msg = Message(1, datetime.now(), Chat(1, "private"), text="/broadcast <b>Promo</b> hari ini",
                      entities=[MessageEntity("bot_command", 0, 10)])
        self.assertEqual(admin_mod._broadcast_source(msg), "/broadcast <b>Promo</b> hari ini")


class _DB(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([User(telegram_id=i, username=f"u{i}", is_banned=False, balance_idr=Decimal("0"))
                    for i in (21, 22, 23)])
        db.commit()
        db.close()
        admin_mod._broadcast_task = None
        self._admin = patch.object(admin_mod, "is_admin", return_value=True)
        self._admin.start()
        self._sleep = patch("bot.handlers.admin.asyncio.sleep", new=AsyncMock())
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        self._admin.stop()
        Base.metadata.drop_all(bind=engine)

    async def _broadcast(self, message, bot):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=message)
        await admin_mod.broadcast_handler(update, SimpleNamespace(bot=bot, args=[]))
        await admin_mod._broadcast_task


class BroadcastEmojiPremium(_DB):
    async def test_user_menerima_emoji_premium_yang_sama(self):
        bot = AsyncMock()
        await self._broadcast(_as_handler_message(_premium_message()), bot)
        self.assertEqual(bot.send_message.await_count, 3)
        kwargs = bot.send_message.await_args.kwargs
        self.assertIn(f'<tg-emoji emoji-id="{SNOWMAN_ID}">☃</tg-emoji>', kwargs["text"])
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertNotIn("/broadcast", kwargs["text"])

    async def test_emoji_premium_ditolak_tetap_terkirim_tanpa_emoji_premium(self):
        async def send_message(**kwargs):
            if "<tg-emoji" in kwargs["text"]:
                raise BadRequest("Bad Request: custom emoji entities can't be used")
            return True

        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=send_message)
        message = _as_handler_message(_premium_message())
        await self._broadcast(message, bot)
        last = bot.send_message.await_args.kwargs
        self.assertEqual(last["parse_mode"], "HTML")
        self.assertIn("☃", last["text"])
        self.assertIn("<b>semua</b>", last["text"])
        self.assertIn("Terkirim        : <code>3 user</code>", message.reply_text.await_args.args[0])


class LaporanGagalDirinci(_DB):
    async def test_alasan_gagal_dipisah(self):
        async def send_message(chat_id, **kwargs):
            if chat_id == 22:
                raise BadRequest("Chat not found")
            if chat_id == 23:
                raise Forbidden("Forbidden: bot was blocked by the user")
            return True

        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=send_message)
        message = SimpleNamespace(text="/broadcast Halo", entities=None, caption=None, photo=None, document=None,
                                  reply_to_message=None, reply_text=AsyncMock())
        await self._broadcast(message, bot)
        report = message.reply_text.await_args.args[0]
        self.assertIn("Terkirim        : <code>1 user</code>", report)
        self.assertIn("Tidak terkirim  : <code>2 user</code>", report)
        self.assertIn("Belum pernah /start bot ini / akun dihapus: <code>1</code>", report)
        self.assertIn("Memblokir bot: <code>1</code>", report)
        self.assertIn("Gagal lain: <code>0</code>", report)


if __name__ == "__main__":
    unittest.main()
