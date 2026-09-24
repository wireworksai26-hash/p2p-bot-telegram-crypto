"""Pagar regresi menu jaringan: simbol yang dijual wajib punya pilihan jaringan sendiri.

Kasus nyata 23 Sep 2026: USDG (Robinhood) ditambahkan ke daftar simbol tapi
tidak ke NETWORKS_BY_SYMBOL, sehingga menu jaringan hanya menawarkan BSC
(default) padahal USDG hanya ada di jaringan Robinhood.
"""
import os
import sys
import unittest
from pathlib import Path

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
    "EVM_PRIVATE_KEY": "0x" + "1" * 64,
})

from bot.handlers.swap import NETWORKS_BY_SYMBOL, SUPPORTED_ASSETS
from bot.keyboards.crypto_select import BUY_NETWORKS_BY_SYMBOL


class TestMenuJaringan(unittest.TestCase):
    def test_simbol_convert_punya_pilihan_jaringan(self):
        for simbol in SUPPORTED_ASSETS:
            jaringan = NETWORKS_BY_SYMBOL.get(simbol)
            self.assertTrue(jaringan, f"{simbol} tidak punya pilihan jaringan (jatuh ke default BSC)")
            self.assertTrue(all(isinstance(n, str) and n for n in jaringan), simbol)

    def test_simbol_beli_jual_punya_pilihan_jaringan(self):
        for simbol in BUY_NETWORKS_BY_SYMBOL:
            jaringan = NETWORKS_BY_SYMBOL.get(simbol) or BUY_NETWORKS_BY_SYMBOL.get(simbol)
            self.assertTrue(jaringan, f"{simbol} tidak punya pilihan jaringan")

    def test_usdg_hanya_di_jaringan_robinhood(self):
        self.assertEqual(NETWORKS_BY_SYMBOL["USDG"], ["ROBINHOOD"])
        self.assertEqual(BUY_NETWORKS_BY_SYMBOL["USDG"], ["ROBINHOOD"])

    def test_usdg_tidak_diklaim_ada_di_bsc(self):
        for peta in (NETWORKS_BY_SYMBOL, BUY_NETWORKS_BY_SYMBOL):
            self.assertNotIn("BSC", peta.get("USDG", []))


if __name__ == "__main__":
    unittest.main()
