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


def _row(text, rng, fee):
    """True bila ada baris <code> dengan rentang `rng` dan fee `fee` (spasi bebas)."""
    import re
    return re.search(rf"<code>{re.escape(rng)}\s+{re.escape(fee)}</code>", text) is not None


class TestPricelistSync(unittest.TestCase):
    def setUp(self):
        self.text = get_official_price_list_text()

    def test_tier_fixed_altcoin_lengkap(self):
        self.assertTrue(_row(self.text, "5k - 12k", "3k"))
        self.assertTrue(_row(self.text, "12k - 18k", "3,5k"))
        self.assertTrue(_row(self.text, "55k - 105k", "5k"))
        self.assertTrue(_row(self.text, "150k - 220k", "7,5k"))
        self.assertTrue(_row(self.text, "940k - 1.010k", "20,5k"))
        self.assertTrue(_row(self.text, "1.010k - 1.030k", "23k"))

    def test_tier_persen_altcoin(self):
        self.assertTrue(_row(self.text, "1.030k - 3.100k", "3%"))
        self.assertTrue(_row(self.text, "3.100k - 5.000k", "2,5%"))

    def test_tier_usd_dan_convert(self):
        # USD
        self.assertTrue(_row(self.text, "34k - 50k", "3,5k"))
        self.assertTrue(_row(self.text, "950k - 1.010k", "15,5k"))
        self.assertTrue(_row(self.text, "1.010k - 1.035k", "18k"))
        self.assertTrue(_row(self.text, "1.035k - 3.800k", "2,3%"))
        self.assertTrue(_row(self.text, "3.800k - 5.000k", "2%"))
        # CONVERT
        self.assertTrue(_row(self.text, "5k - 10k", "3k"))
        self.assertTrue(_row(self.text, "47k - 55k", "4,5k"))
        self.assertTrue(_row(self.text, "350k - 425k", "11k"))
        self.assertTrue(_row(self.text, "920k - 1.010k", "22k"))
        self.assertTrue(_row(self.text, "1.010k - 1.030k", "25k"))
        self.assertTrue(_row(self.text, "1.030k - 3.100k", "3%"))
        self.assertTrue(_row(self.text, "3.100k - 5.000k", "2,5%"))

    def test_jumlah_baris_sama_dengan_engine(self):
        expected = (len(ALTCOIN_FEE_TIERS) + len(ALTCOIN_PERCENT_TIERS)
                    + len(USD_FEE_TIERS) + len(USD_PERCENT_TIERS)
                    + len(CONVERT_FEE_TIERS) + len(CONVERT_PERCENT_TIERS))
        self.assertEqual(self.text.count("<code>"), expected)

    def test_empat_kartu_expandable(self):
        # 3 kartu fee (Altcoin, USD, Convert) + 1 kartu Note.
        self.assertEqual(self.text.count("<blockquote expandable>"), 4)
        self.assertEqual(self.text.count("</blockquote>"), 4)

    def test_catatan_baru_ada_tanya_admin_hilang(self):
        # Tidak ada +500 lagi
        self.assertNotIn("+Rp 500", self.text)
        self.assertIn("+Rp 2.500", self.text)
        self.assertIn("7.500", self.text)
        self.assertIn("5.000", self.text)
        self.assertIn("0,3%", self.text)
        self.assertIn("rata-rata harga bursa global", self.text)
        self.assertIn("kode Unik", self.text)
        self.assertIn("5.000.000", self.text)
        self.assertIn("chat admin", self.text.lower())
        self.assertNotIn("sphread", self.text)

    def test_muat_di_satu_pesan_telegram(self):
        self.assertLessEqual(len(self.text), 4096)


if __name__ == "__main__":
    unittest.main()
