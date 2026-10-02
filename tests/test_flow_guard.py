"""Guard pindah transaksi (cancel dulu sebelum ganti alur) — Fase D.

Aturan: flow lain sedang aktif -> entry baru diblokir + peringatan
(ala kompetitor), flow lama tetap berjalan. Flow sama -> restart seperti biasa.
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

from telegram.ext import CallbackQueryHandler
from bot.utils.flow_guard import active_flow_names, block_if_busy
from bot.handlers.buy import (
    buy_conversation_handler, start_buy_callback, start_buy_command,
)
from bot.handlers.sell import (
    sell_conversation_handler, start_sell_callback, start_sell_command,
)
from bot.handlers.swap import swap_conv_handler, start_swap
from bot.handlers.balance import topup_conversation_handler, start_topup_callback
from bot.handlers.calculator import (
    calculator_conversation_handler, start_calculator_callback,
    start_calculator_command,
)

USER_ID = 424242
CHAT_ID = 424242
CONV_KEY = (CHAT_ID, USER_ID)


def _update():
    message = AsyncMock()
    query = AsyncMock()
    query.data = "menu_sell"
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=USER_ID),
        effective_chat=SimpleNamespace(id=CHAT_ID),
        effective_message=message,
    )


class TestFlowGuard(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        for handler in (buy_conversation_handler, sell_conversation_handler,
                        swap_conv_handler, topup_conversation_handler,
                        calculator_conversation_handler):
            convs = getattr(handler, "_conversations", None)
            if isinstance(convs, dict):
                convs.pop(CONV_KEY, None)

    async def test_tidak_aktif_tidak_diblokir(self):
        update = _update()
        self.assertEqual(active_flow_names(update), [])
        self.assertFalse(await block_if_busy("sell", update, SimpleNamespace()))

    async def test_flow_lain_aktif_diblokir_dengan_peringatan(self):
        buy_conversation_handler._conversations[CONV_KEY] = 1
        update = _update()
        self.assertEqual(active_flow_names(update, exclude="sell"), ["buy"])
        blocked = await block_if_busy("sell", update, SimpleNamespace())
        self.assertTrue(blocked)
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("Beli Crypto", text)
        self.assertIn("/cancel", text)
        # Flow lama tidak tersentuh guard.
        self.assertIn(CONV_KEY, buy_conversation_handler._conversations)

    async def test_flow_sama_boleh_restart(self):
        buy_conversation_handler._conversations[CONV_KEY] = 1
        update = _update()
        self.assertFalse(await block_if_busy("buy", update, SimpleNamespace()))
        update.effective_message.reply_text.assert_not_awaited()

    async def test_entry_terblokir_return_none(self):
        buy_conversation_handler._conversations[CONV_KEY] = 1
        update = _update()
        context = SimpleNamespace()
        with patch("bot.handlers.sell.block_if_busy",
                   side_effect=lambda *a, **k: block_if_busy(*a, **k)):
            result = await start_sell_callback(update, context)
        self.assertIsNone(result)
        self.assertNotIn(CONV_KEY, sell_conversation_handler._conversations)

    async def test_semua_entry_memanggil_guard(self):
        cases = [
            ("bot.handlers.buy", start_buy_callback),
            ("bot.handlers.buy", start_buy_command),
            ("bot.handlers.sell", start_sell_callback),
            ("bot.handlers.sell", start_sell_command),
            ("bot.handlers.swap", start_swap),
            ("bot.handlers.balance", start_topup_callback),
            ("bot.handlers.calculator", start_calculator_callback),
            ("bot.handlers.calculator", start_calculator_command),
        ]
        for module, entry in cases:
            with self.subTest(entry=f"{module}.{entry.__name__}"):
                with patch(f"{module}.block_if_busy",
                           new=AsyncMock(return_value=True)) as guard:
                    result = await entry(SimpleNamespace(), SimpleNamespace())
                self.assertIsNone(result)
                guard.assert_awaited_once()

    def test_fallback_tidak_lagi_cancel_diam_diam(self):
        pairs = [
            (buy_conversation_handler, "menu_sell"),
            (sell_conversation_handler, "menu_buy"),
            (swap_conv_handler, "menu_buy"),
            (topup_conversation_handler, "menu_sell"),
            (calculator_conversation_handler, "menu_buy"),
        ]
        for handler, foreign_menu in pairs:
            for fb in handler.fallbacks:
                if isinstance(fb, CallbackQueryHandler) and fb.pattern is not None:
                    patterns = fb.pattern if isinstance(fb.pattern, list) else [fb.pattern]
                    for pat in patterns:
                        text = getattr(pat, "pattern", str(pat))
                        self.assertNotIn(
                            foreign_menu, text,
                            f"fallback {handler} masih meng-cancel {foreign_menu} diam-diam",
                        )


if __name__ == "__main__":
    unittest.main()
