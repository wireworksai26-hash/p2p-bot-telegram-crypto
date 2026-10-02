"""Pricelist bot harus selalu sinkron dengan fee engine — Fase B.

Angka ekspektasi ditulis manual (bukan dari helper price.py) agar test
benar-benar menangkap drift antara teks dan engine.
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
    "EVM_PRIVATE_KEY": "",
})

from bot.handlers.price import get_official_price_list_text
from services.fee_service import (
    ALTCOIN_FEE_TIERS,
    ALTCOIN_PERCENT_TIERS,
    CONVERT_FEE_TIERS,
    CONVERT_PERCENT_TIERS,
    USD_FEE_TIERS,
    USD_PERCENT_TIERS,
)


class TestPricelistSync(unittest.TestCase):
    def setUp(self):
        self.text = get_official_price_list_text()

    def test_tier_fixed_altcoin_lengkap(self):
        self.assertIn("Jual/Beli 5k-10k = fee 3k IDR", self.text)
        self.assertIn("Jual/Beli 10.001-15k = fee 3,5k IDR", self.text)
        self.assertIn("Jual/Beli 940.001-1.010.000 = fee 19k IDR", self.text)

    def test_tier_persen_altcoin(self):
        self.assertIn("Jual/Beli 1.010.001-2.000.000 = fee 3%", self.text)
        self.assertIn("Jual/Beli 2.000.001-3.500.000 = fee 2,5%", self.text)
        self.assertIn("Jual/Beli 3.500.001-8.500.000 = fee 2%", self.text)
        self.assertIn("Jual/Beli di atas 8.500.000 = fee 1,5%", self.text)

    def test_tier_usd_dan_convert(self):
        self.assertIn("Jual/Beli 5k-10k = fee 3k IDR", self.text)
        self.assertIn("Jual/Beli 1.015.001-3.600.000 = fee 2%", self.text)
        self.assertIn("Jual/Beli di atas 3.600.000 = fee 1,5%", self.text)
        self.assertIn("Convert 6k-10k = fee 3,5k IDR", self.text)
        self.assertIn("Convert di atas 8.500.000 = fee 1,5%", self.text)

    def test_jumlah_baris_sama_dengan_engine(self):
        expected = (len(ALTCOIN_FEE_TIERS) + len(ALTCOIN_PERCENT_TIERS)
                    + len(USD_FEE_TIERS) + len(USD_PERCENT_TIERS)
                    + len(CONVERT_FEE_TIERS) + len(CONVERT_PERCENT_TIERS))
        self.assertEqual(self.text.count("➡️"), expected)

    def test_catatan_baru_ada_tanya_admin_hilang(self):
        self.assertIn("+Rp 500", self.text)
        self.assertIn("2.500", self.text)
        self.assertIn("7.500", self.text)
        self.assertIn("0,3%", self.text)
        self.assertIn("0,5%", self.text)
        self.assertIn("01-200", self.text)
        self.assertNotIn("tanya admin", self.text)
        self.assertNotIn("sphread", self.text)

    def test_muat_di_satu_pesan_telegram(self):
        self.assertLessEqual(len(self.text), 4096)


if __name__ == "__main__":
    unittest.main()
