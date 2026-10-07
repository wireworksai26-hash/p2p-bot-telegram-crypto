"""Jual/Convert hanya boleh memakai jaringan yang depositnya bisa diverifikasi on-chain.

Dulu menu Jual memakai daftar jaringan Beli yang memuat jaringan yang depositnya tidak
bisa diverifikasi (TX hash selalu "format salah") dan user terjebak. Callback
rakitan/basi (mis. `swap_src_net_FAKENET`) juga diterima begitu saja.
"""
import os
import unittest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from tests._settings_guard import e2e_setup, e2e_teardown  # noqa: E402
from tests import test_e2e_bot_flows as _e2e  # noqa: E402
from bot.keyboards.crypto_select import get_sell_network_keyboard  # noqa: E402

U = 94001


class SellNetworkKeyboard(unittest.TestCase):
    def test_menu_jual_tanpa_jaringan_yang_tidak_bisa_diverifikasi(self):
        for sym in ("USDC", "ETH"):
            data = [b.callback_data for row in get_sell_network_keyboard(sym).inline_keyboard for b in row]
            self.assertNotIn(f"sell_net_{sym}_FAKENET", data)
            self.assertIn(f"sell_net_{sym}_BASE", data)


class CraftedNetworkE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await e2e_setup(self)

    async def asyncTearDown(self):
        await e2e_teardown(self)

    _user = _e2e.BotFlowE2E._user
    _dispatch = _e2e.BotFlowE2E._dispatch
    say = _e2e.BotFlowE2E.say
    tap = _e2e.BotFlowE2E.tap

    async def test_jual_jaringan_palsu_dari_tombol_basi_ditolak(self):
        await self.say(U, "/start")
        await self.tap(U, "menu_sell", from_screen=False)
        await self.tap(U, "sell_sym_USDC", from_screen=False)
        shown = await self.tap(U, "sell_net_USDC_FAKENET", from_screen=False)
        self.assertNotIn("Berapa jumlah koin", shown)

    async def test_convert_jaringan_asal_rakitan_ditolak(self):
        await self.say(U, "/start")
        await self.tap(U, "start_swap", from_screen=False)
        await self.tap(U, "swap_src_sym_USDT", from_screen=False)
        shown = await self.tap(U, "swap_src_net_FAKENET", from_screen=False)
        self.assertNotIn("Koin Tujuan", shown)

    async def test_kontrol_jaringan_valid_tetap_jalan(self):
        await self.say(U, "/start")
        await self.tap(U, "menu_sell", from_screen=False)
        await self.tap(U, "sell_sym_USDC", from_screen=False)
        shown = await self.tap(U, "sell_net_USDC_BASE", from_screen=False)
        self.assertIn("Pilih cara memasukkan jumlah", shown)


if __name__ == "__main__":
    unittest.main()
