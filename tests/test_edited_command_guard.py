"""Perintah slash yang diedit diabaikan (bukan error, bukan dijalankan ulang).

Bug: CommandHandler bawaan PTB menerima `edited_message`; di update itu `update.message` None
sehingga handler error "'NoneType' object has no attribute 'reply_text'" (terlihat di
/postlasttesti), dan perintah admin berbahaya (/credit, /broadcast) bisa terjalankan ulang.
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone

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

from telegram import Chat, Message, Update, User
from telegram.ext import ApplicationHandlerStop, TypeHandler

from bot.utils.edit_guard import edited_command_gate


_CHAT = Chat(id=1, type="private")
_USER = User(id=1, first_name="Admin", is_bot=False)


def _message(text=None, caption=None):
    return Message(message_id=1, date=datetime.now(timezone.utc), chat=_CHAT, from_user=_USER,
                   text=text, caption=caption)


def FakeUpdate(message=None, edited_message=None):
    """Update asli dengan Message asli (field dibaca gerbang: edited_message, text, caption)."""
    return Update(update_id=1, message=message, edited_message=edited_message)


def _msg(text=None, caption=None):
    return _message(text=text, caption=caption)


class TestEditedCommandGate(unittest.IsolatedAsyncioTestCase):
    async def test_perintah_yang_diedit_dihentikan(self):
        for text in ("/postlasttesti", "/postlasttesti 3", "  /credit 123 5000", "/broadcast halo"):
            with self.subTest(text=text):
                with self.assertRaises(ApplicationHandlerStop):
                    await edited_command_gate(FakeUpdate(edited_message=_msg(text)), None)

    async def test_perintah_dengan_caption_yang_diedit_dihentikan(self):
        with self.assertRaises(ApplicationHandlerStop):
            await edited_command_gate(FakeUpdate(edited_message=_msg(text=None, caption="/broadcast promo")), None)

    async def test_perintah_baru_tidak_disentuh(self):
        self.assertIsNone(await edited_command_gate(FakeUpdate(message=_msg("/postlasttesti")), None))

    async def test_edit_teks_biasa_tidak_disentuh(self):
        for text in ("0.5", "BCA, 123456, Budi", "halo", ""):
            with self.subTest(text=text):
                self.assertIsNone(await edited_command_gate(FakeUpdate(edited_message=_msg(text)), None))

    async def test_bukan_update_diabaikan(self):
        self.assertIsNone(await edited_command_gate(object(), None))


class TestGerbangTerpasang(unittest.TestCase):
    def test_terdaftar_paling_awal(self):
        import main
        application = main.build_bot_application()
        groups = application.handlers
        self.assertIn(-3, groups, "gerbang perintah-edit harus di group -3 (sebelum maintenance & ban gate)")
        gate = [h for h in groups[-3] if isinstance(h, TypeHandler) and h.callback is edited_command_gate]
        self.assertEqual(len(gate), 1)
        self.assertLess(min(groups), -2)
        self.assertEqual(min(groups), -3)


if __name__ == "__main__":
    unittest.main()
