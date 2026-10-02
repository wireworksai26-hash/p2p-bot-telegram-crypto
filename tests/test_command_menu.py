"""Menu command Telegram (/start + /cancel di tombol Menu) — Fase A.

BOT_COMMAND_MENU didaftarkan idempotent via post_init setiap startup,
jadi buyer tidak perlu mengetik slash manual.
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

from main import BOT_COMMAND_MENU, set_bot_commands


class TestCommandMenu(unittest.IsolatedAsyncioTestCase):
    async def test_menu_hanya_start_dan_cancel(self):
        self.assertEqual(BOT_COMMAND_MENU, [
            ("start", "Memulai bot"),
            ("cancel", "Membatalkan transaksi"),
        ])

    async def test_post_init_mendaftarkan_menu(self):
        app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()))
        await set_bot_commands(app)
        cmds = app.bot.set_my_commands.await_args.args[0]
        self.assertEqual(
            [(c.command, c.description) for c in cmds],
            BOT_COMMAND_MENU,
        )

    async def test_post_init_tidak_mematikan_startup_saat_gagal(self):
        app = SimpleNamespace(bot=SimpleNamespace(
            set_my_commands=AsyncMock(side_effect=RuntimeError("offline"))))
        with patch("asyncio.sleep", new=AsyncMock()):
            await set_bot_commands(app)  # tidak raise, 3x percobaan
        self.assertEqual(app.bot.set_my_commands.await_count, 3)


if __name__ == "__main__":
    unittest.main()
