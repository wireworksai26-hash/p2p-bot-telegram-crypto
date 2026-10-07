"""Menu tombol ☰ (daftar perintah saat mengetik "/") khusus admin.

- Format sesuai aturan Telegram (nama perintah, panjang deskripsi, maks 100 perintah).
- Setiap perintah di menu benar-benar punya handler terdaftar (bukan perintah mati).
- Menu admin dipasang per chat admin dan grup admin; user biasa tetap memakai menu default.
- Gagal di satu chat tidak menghentikan chat lain.
"""
import os
import re
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

from telegram import BotCommandScopeChat
from telegram.ext import CommandHandler, ConversationHandler

import main
from main import ADMIN_COMMAND_MENU, BOT_COMMAND_MENU, admin_menu_chat_ids, set_bot_commands


def _registered_commands(application) -> set:
    names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                names |= set(handler.commands)
            elif isinstance(handler, ConversationHandler):
                for entry in handler.entry_points:
                    if isinstance(entry, CommandHandler):
                        names |= set(entry.commands)
    return names


class TestFormatMenuAdmin(unittest.TestCase):
    def test_format_sesuai_aturan_telegram(self):
        self.assertLessEqual(len(ADMIN_COMMAND_MENU), 100)
        for name, desc in ADMIN_COMMAND_MENU:
            with self.subTest(command=name):
                self.assertRegex(name, r"^[a-z0-9_]{1,32}$")
                self.assertTrue(3 <= len(desc) <= 256, f"deskripsi /{name} harus 3-256 karakter")

    def test_tanpa_duplikat(self):
        names = [n for n, _ in ADMIN_COMMAND_MENU]
        self.assertEqual(len(names), len(set(names)))

    def test_admin_di_urutan_pertama(self):
        self.assertEqual(ADMIN_COMMAND_MENU[0][0], "admin")

    def test_semua_perintah_menu_punya_handler(self):
        application = main.build_bot_application()
        registered = _registered_commands(application)
        for name, _ in ADMIN_COMMAND_MENU + BOT_COMMAND_MENU:
            with self.subTest(command=name):
                self.assertIn(name, registered, f"/{name} ada di menu tapi tidak punya handler")

    def test_menu_user_biasa_tidak_berubah(self):
        self.assertEqual([n for n, _ in BOT_COMMAND_MENU], ["start", "cancel"])


class TestPemasanganMenuAdmin(unittest.IsolatedAsyncioTestCase):
    def test_chat_id_admin_plus_grup_tanpa_duplikat(self):
        with patch.object(main.settings, "ADMIN_CHAT_IDS", [11, 22, 11]), \
             patch.object(main.settings, "ADMIN_GROUP_ID", -100123):
            self.assertEqual(admin_menu_chat_ids(), [11, 22, -100123])
        with patch.object(main.settings, "ADMIN_CHAT_IDS", [11]), \
             patch.object(main.settings, "ADMIN_GROUP_ID", None):
            self.assertEqual(admin_menu_chat_ids(), [11])

    async def test_menu_default_lalu_menu_admin_per_chat(self):
        app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()))
        with patch.object(main.settings, "ADMIN_CHAT_IDS", [11, 22]), \
             patch.object(main.settings, "ADMIN_GROUP_ID", -100123):
            await set_bot_commands(app)
        calls = app.bot.set_my_commands.await_args_list
        self.assertEqual(len(calls), 4, "1 default + 2 admin + 1 grup")
        self.assertNotIn("scope", calls[0].kwargs, "menu default tanpa scope (untuk semua user)")
        for call, chat_id in zip(calls[1:], [11, 22, -100123]):
            scope = call.kwargs["scope"]
            self.assertIsInstance(scope, BotCommandScopeChat)
            self.assertEqual(scope.chat_id, chat_id)
            self.assertEqual([(c.command, c.description) for c in call.args[0]], ADMIN_COMMAND_MENU)

    async def test_gagal_di_satu_chat_tidak_menghentikan_yang_lain(self):
        async def fake_set(commands, scope=None):
            if scope is not None and scope.chat_id == 11:
                raise RuntimeError("Bad Request: chat not found")

        app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock(side_effect=fake_set)))
        with patch.object(main.settings, "ADMIN_CHAT_IDS", [11, 22]), \
             patch.object(main.settings, "ADMIN_GROUP_ID", None):
            await set_bot_commands(app)
        scoped = [c.kwargs["scope"].chat_id for c in app.bot.set_my_commands.await_args_list if "scope" in c.kwargs]
        self.assertEqual(scoped, [11, 22], "admin 22 tetap diproses walau admin 11 gagal")

    async def test_menu_admin_tidak_dipasang_bila_menu_default_gagal_total(self):
        app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock(side_effect=RuntimeError("offline"))))
        with patch("asyncio.sleep", new=AsyncMock()):
            await set_bot_commands(app)
        self.assertEqual(app.bot.set_my_commands.await_count, 3, "3 percobaan default, lalu berhenti")


if __name__ == "__main__":
    unittest.main()
