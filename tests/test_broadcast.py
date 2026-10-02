"""Broadcast admin: teks + foto, pacing, RetryAfter — Fase E.

Penerima = semua User non-banned (pernah /start maupun transaksi,
karena /start mendaftarkan user).
"""
import os
import sys
import unittest
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
    "ADMIN_CHAT_IDS": "123456",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from telegram.error import RetryAfter
from database.connection import Base, engine, SessionLocal
from database.models import User
from bot.handlers.admin import (
    _retry_after_seconds,
    _send_broadcast_to_user,
    broadcast_handler,
)


def _message(text="/broadcast Halo member", photo=None, caption=None, reply_photo=None):
    msg = AsyncMock()
    msg.text = text
    msg.photo = photo or []
    msg.caption = caption
    msg.reply_to_message = None
    if reply_photo:
        msg.reply_to_message = SimpleNamespace(photo=[SimpleNamespace(file_id=reply_photo)])
        msg.text = text
    return msg


class TestBroadcast(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add_all([
            User(telegram_id=11, is_banned=False),
            User(telegram_id=22, is_banned=False),
            User(telegram_id=33, is_banned=False),
            User(telegram_id=44, is_banned=True),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _run(self, message, admin=True, bot=None):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1), message=message,
        )
        context = SimpleNamespace(bot=bot or AsyncMock(), args=[])
        patches = [patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()),
                    patch("bot.handlers.admin.is_admin", return_value=admin),
                    patch("bot.handlers.admin.asyncio.sleep", new=AsyncMock())]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return update, context

    async def test_non_admin_ditolak(self):
        bot = AsyncMock()
        update, context = self._run(_message(), admin=False, bot=bot)
        await broadcast_handler(update, context)
        bot.send_message.assert_not_awaited()

    async def test_broadcast_teks_hitung_sukses_gagal(self):
        bot = AsyncMock()
        bot.send_message.side_effect = [None, RuntimeError("blokir"), None]
        update, context = self._run(_message(), bot=bot)
        await broadcast_handler(update, context)
        self.assertEqual(bot.send_message.await_count, 3)  # banned terexclude
        teks = bot.send_message.call_args_list[0].kwargs.get("text", "")
        self.assertIn("PENGUMUMAN DARI OWNER", teks)
        self.assertIn("Halo member", teks)
        laporan = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("Sukses terkirim: <code>2 user</code>", laporan)
        self.assertIn("Gagal/Blokir bot: <code>1 user</code>", laporan)

    async def test_broadcast_foto_caption(self):
        bot = AsyncMock()
        msg = _message(text=None, photo=[SimpleNamespace(file_id="FID-POSTER")],
                       caption="/broadcast USDC ARC READY!")
        update, context = self._run(msg, bot=bot)
        await broadcast_handler(update, context)
        self.assertEqual(bot.send_photo.await_count, 3)
        call = bot.send_photo.call_args_list[0]
        self.assertEqual(call.kwargs.get("photo"), "FID-POSTER")
        self.assertIn("USDC ARC READY!", call.kwargs.get("caption", ""))
        bot.send_message.assert_not_awaited()

    async def test_broadcast_foto_reply(self):
        bot = AsyncMock()
        msg = _message(text="/broadcast Campaign spesial", reply_photo="FID-REPLY")
        update, context = self._run(msg, bot=bot)
        await broadcast_handler(update, context)
        self.assertEqual(bot.send_photo.await_count, 3)
        self.assertEqual(bot.send_photo.call_args_list[0].kwargs.get("photo"), "FID-REPLY")

    async def test_format_salah_tanpa_isi(self):
        bot = AsyncMock()
        update, context = self._run(_message(text="/broadcast   "), bot=bot)
        await broadcast_handler(update, context)
        bot.send_message.assert_not_awaited()
        bot.send_photo.assert_not_awaited()
        self.assertIn("Format salah", update.message.reply_text.call_args.args[0])

    async def test_retryafter_lalu_sukses(self):
        bot = AsyncMock()
        with patch("bot.handlers.admin.asyncio.sleep", new=AsyncMock()) as sleeper:
            ok = await _send_broadcast_to_user(bot, 11, "halo")
        self.assertTrue(ok)

        bot2 = AsyncMock()
        bot2.send_message.side_effect = [RetryAfter(2), None]
        with patch("bot.handlers.admin.asyncio.sleep", new=AsyncMock()) as sleeper2:
            ok2 = await _send_broadcast_to_user(bot2, 11, "halo")
        self.assertTrue(ok2)
        self.assertEqual(bot2.send_message.await_count, 2)
        sleeper2.assert_awaited_once()
        self.assertGreaterEqual(_retry_after_seconds(RetryAfter(2)), 2)


if __name__ == "__main__":
    unittest.main()
