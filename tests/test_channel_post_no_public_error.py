"""Foto di channel testi tidak boleh memicu pesan error publik.

Bug: foto yang diposting di channel testi masuk sebagai channel_post (tanpa pengirim) ke
router bukti transfer -> "'NoneType' object has no attribute 'id'", lalu error handler
mengirim "⚠️ Terjadi kesalahan..." ke channel testi sehingga terlihat publik.
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from telegram import Chat, Message, PhotoSize, Update, User  # noqa: E402

import main  # noqa: E402

_PHOTO = [PhotoSize(file_id="f", file_unique_id="u", width=10, height=10)]


def _channel_photo_update():
    chat = Chat(id=-1001234567890, type="channel", title="Testi")
    msg = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, photo=_PHOTO)
    return Update(update_id=1, channel_post=msg)


def _update_in(chat_type):
    chat = Chat(id=-100 if chat_type != "private" else 5, type=chat_type)
    user = User(id=5, first_name="Budi", is_bot=False)
    msg = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user, text="x")
    return Update(update_id=1, message=msg)


class TestRouterBuktiTransfer(unittest.IsolatedAsyncioTestCase):
    async def test_foto_channel_diabaikan_tanpa_error(self):
        self.assertIsNone(await main._route_transfer_proof(_channel_photo_update(), SimpleNamespace(user_data={})))


class TestErrorHandlerTidakPublik(unittest.IsolatedAsyncioTestCase):
    async def _run(self, update):
        ctx = SimpleNamespace(error=AttributeError("'NoneType' object has no attribute 'id'"), bot=AsyncMock())
        with patch("bot.utils.telegram_utils.notify_admins", new=AsyncMock()) as notify:
            await main.error_handler(update, ctx)
        return ctx.bot.send_message, notify

    async def test_channel_dan_grup_tidak_dikirimi_pesan_error(self):
        for update in (_channel_photo_update(), _update_in("supergroup"), _update_in("group")):
            with self.subTest(chat=update.effective_chat.type):
                send, notify = await self._run(update)
                send.assert_not_awaited()
                notify.assert_awaited_once()

    async def test_chat_pribadi_tetap_dikirimi_pesan_error(self):
        send, notify = await self._run(_update_in("private"))
        send.assert_awaited_once()
        self.assertEqual(send.await_args.kwargs["chat_id"], 5)
        notify.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
