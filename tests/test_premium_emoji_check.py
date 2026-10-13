"""Emoji premium: pemutus tidak lagi permanen sampai restart, dan /cekemoji menampilkan jawaban Telegram."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from telegram.error import BadRequest  # noqa: E402

from bot.utils import animated as an  # noqa: E402


class PemutusEmoji(unittest.TestCase):
    def setUp(self):
        an._STATE.update(rejects=0, accepts=0, warned=False, streak=0, last_reject=0.0)

    def test_dicoba_lagi_setelah_jeda_dan_pulih_saat_diterima(self):
        an.record_custom_emoji_result(False, now=1000.0)
        self.assertFalse(an._skip_custom_emoji(1001.0), "satu penolakan belum memutus")
        an.record_custom_emoji_result(False, now=1002.0)
        self.assertTrue(an._skip_custom_emoji(1003.0))
        self.assertFalse(an._skip_custom_emoji(1002.0 + an.PROBE_SECONDS), "dicoba lagi setelah jeda")
        an.record_custom_emoji_result(True)
        self.assertFalse(an._skip_custom_emoji(1004.0), "diterima sekali = langsung pulih")


class PerintahCekEmoji(unittest.IsolatedAsyncioTestCase):
    async def _run(self, send):
        from bot.handlers import admin_emoji_check as mod
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1),
                                 effective_chat=SimpleNamespace(id=1))
        with patch("bot.handlers.admin.is_admin", return_value=True), \
             patch.object(mod.ExtBot, "send_message", new=send):
            await mod.emoji_check_handler(update, SimpleNamespace(bot=object()))
        return message.reply_text.await_args.args[0]

    async def test_ditolak_menjelaskan_penyebab_dan_solusi(self):
        text = await self._run(AsyncMock(side_effect=BadRequest("Custom emoji entities can't be used")))
        self.assertIn("MENOLAK", text)
        self.assertIn("Custom emoji entities", text)
        self.assertIn("Telegram Premium", text)
        self.assertIn("Transfer Ownership", text)

    async def test_diterima(self):
        send = AsyncMock()
        text = await self._run(send)
        self.assertIn("menerima emoji premium", text)
        self.assertIn("<tg-emoji", send.await_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
