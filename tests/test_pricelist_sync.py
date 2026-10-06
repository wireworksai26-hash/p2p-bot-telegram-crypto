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
        self.assertTrue(_row(self.text, "5k - 10k", "3k"))
        self.assertTrue(_row(self.text, "10k - 15k", "3,5k"))
        self.assertTrue(_row(self.text, "940k - 1.010k", "19k"))

    def test_tier_persen_altcoin(self):
        self.assertTrue(_row(self.text, "1.010k - 2.000k", "3%"))
        self.assertTrue(_row(self.text, "2.000k - 3.500k", "2,5%"))
        self.assertTrue(_row(self.text, "3.500k - 8.500k", "2%"))
        self.assertTrue(_row(self.text, "> 8.500k", "1,5%"))

    def test_tier_usd_dan_convert(self):
        self.assertTrue(_row(self.text, "950k - 1.015k", "14,5k"))
        self.assertTrue(_row(self.text, "1.015k - 3.600k", "2%"))
        self.assertTrue(_row(self.text, "> 3.600k", "1,5%"))
        self.assertTrue(_row(self.text, "6k - 10k", "3,5k"))
        self.assertTrue(_row(self.text, "390k - 425k", "10,5k"))

    def test_jumlah_baris_sama_dengan_engine(self):
        expected = (len(ALTCOIN_FEE_TIERS) + len(ALTCOIN_PERCENT_TIERS)
                    + len(USD_FEE_TIERS) + len(USD_PERCENT_TIERS)
                    + len(CONVERT_FEE_TIERS) + len(CONVERT_PERCENT_TIERS))
        self.assertEqual(self.text.count("<code>"), expected)

    def test_empat_kartu_expandable(self):
        # 3 kartu fee (Altcoin, USD, Convert) + 1 kartu Note.
        self.assertEqual(self.text.count("<blockquote expandable>"), 4)
        self.assertEqual(self.text.count("</blockquote>"), 4)

    def test_nominal_konsisten_k(self):
        self.assertNotIn("1.010.000", self.text)
        self.assertNotIn("8.500.000", self.text)

    def test_catatan_baru_ada_tanya_admin_hilang(self):
        self.assertIn("+Rp 500", self.text)
        self.assertIn("2.500", self.text)
        self.assertIn("7.500", self.text)
        self.assertIn("0,3%", self.text)
        self.assertIn("tanpa spread tersembunyi", self.text)
        self.assertIn("kode Unik", self.text)
        self.assertNotIn("tanya admin", self.text)
        self.assertNotIn("sphread", self.text)

    def test_muat_di_satu_pesan_telegram(self):
        self.assertLessEqual(len(self.text), 4096)


if __name__ == "__main__":
    unittest.main()
