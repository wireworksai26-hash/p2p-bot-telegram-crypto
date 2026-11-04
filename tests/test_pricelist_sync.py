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

    # Daftar resmi klien (pesan 4 Nov 2026): (rentang, fee) persis seperti tampil di bot.
    ALTCOIN = [("5k - 7k", "2,5k"), ("7k - 32k", "3k"), ("32k - 62k", "4k"), ("62k - 100k", "4,5k"),
               ("100k - 127k", "5k"), ("127k - 150k", "6,5k"), ("150k - 220k", "7,5k"), ("220k - 300k", "8,5k"),
               ("300k - 330k", "9k"), ("330k - 380k", "9,5k"), ("380k - 460k", "10k"), ("460k - 500k", "12k"),
               ("500k - 600k", "13k"), ("600k - 700k", "14k"), ("700k - 770k", "15k"), ("770k - 840k", "17k"),
               ("840k - 890k", "18,5k"), ("890k - 940k", "19,5k"), ("940k - 1.010k", "22,5k"),
               ("1.010k - 1.030k", "23,5k"), ("1.030k - 5.000k", "2,5%")]
    USD = [("5k - 10k", "2,5k"), ("10k - 36k", "3k"), ("36k - 50k", "3,5k"), ("50k - 100k", "4k"),
           ("100k - 160k", "5k"), ("160k - 200k", "5,5k"), ("200k - 245k", "6,5k"), ("245k - 300k", "7k"),
           ("300k - 360k", "7,5k"), ("360k - 405k", "8k"), ("405k - 480k", "8,5k"), ("480k - 580k", "9k"),
           ("580k - 680k", "9,5k"), ("680k - 735k", "11,5k"), ("735k - 875k", "13k"), ("875k - 950k", "15,5k"),
           ("950k - 1.010k", "17,5k"), ("1.010k - 1.035k", "19k"), ("1.035k - 3.800k", "2,3%"),
           ("3.800k - 5.000k", "2%")]
    CONVERT = [("5k - 7k", "2,5k"), ("7k - 32k", "3k"), ("32k - 62k", "4k"), ("62k - 100k", "4,5k"),
               ("100k - 127k", "5k"), ("127k - 150k", "6,5k"), ("150k - 220k", "7,5k"), ("220k - 300k", "8,5k"),
               ("300k - 350k", "9,5k"), ("350k - 410k", "10k"), ("410k - 600k", "12,5k"), ("600k - 710k", "15k"),
               ("710k - 810k", "17,5k"), ("810k - 920k", "19,5k"), ("920k - 1.010k", "22,5k"),
               ("1.010k - 1.030k", "23k"), ("1.030k - 5.000k", "2,5%")]

    def test_tier_fixed_altcoin_lengkap(self):
        for rng, fee in self.ALTCOIN:
            self.assertTrue(_row(self.text, rng, fee), (rng, fee))

    def test_tier_persen_altcoin(self):
        self.assertTrue(_row(self.text, "1.030k - 5.000k", "2,5%"))
        self.assertFalse(_row(self.text, "1.030k - 3.100k", "3%"))  # tier 3% lama sudah dihapus

    def test_tier_usd_dan_convert(self):
        for rng, fee in self.USD:
            self.assertTrue(_row(self.text, rng, fee), ("USD", rng, fee))
        for rng, fee in self.CONVERT:
            self.assertTrue(_row(self.text, rng, fee), ("CONVERT", rng, fee))

    def test_jumlah_baris_persis_daftar_klien(self):
        self.assertEqual(self.text.count("<code>"), len(self.ALTCOIN) + len(self.USD) + len(self.CONVERT))

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
