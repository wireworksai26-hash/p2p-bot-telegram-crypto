"""S4 — notifikasi error ke admin harus aman untuk HTML.

Dulu `<code>{context.error}</code>` tanpa escape: error yang memuat `<`/`&`
(mis. input user, respons RPC) membuat Telegram menolak pesan, sehingga admin
tidak pernah tahu ada error.
"""
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

import main  # noqa: E402


class ErrorHandlerEscape(unittest.IsolatedAsyncioTestCase):
    async def test_error_berisi_html_di_escape(self):
        ctx = SimpleNamespace(error=ValueError("nama <b>Ali</b> & co <3"), bot=AsyncMock())
        with patch("bot.utils.telegram_utils.notify_admins", new=AsyncMock()) as notify:
            await main.error_handler(None, ctx)
        text = notify.await_args.kwargs.get("text") or notify.await_args.args[1]
        self.assertIn("&lt;b&gt;Ali&lt;/b&gt; &amp; co &lt;3", text)
        self.assertNotIn("<b>Ali</b>", text)

    async def test_pesan_error_panjang_dipotong(self):
        ctx = SimpleNamespace(error=RuntimeError("x" * 10_000), bot=AsyncMock())
        with patch("bot.utils.telegram_utils.notify_admins", new=AsyncMock()) as notify:
            await main.error_handler(None, ctx)
        text = notify.await_args.kwargs.get("text") or notify.await_args.args[1]
        self.assertLess(len(text), 4096, "batas pesan Telegram 4096 karakter")


if __name__ == "__main__":
    unittest.main()
