"""Regresi QRIS: template rusak tidak boleh lagi menghasilkan QR (kasus 23 Sep 2026).

Template QRIS baru sempat kehilangan 1 karakter di 2 field saat copy-paste:
TLV bergeser, CRC salah, semua e-wallet menolak dengan 'QR sedang tidak tersedia',
dan generator diam-diam menerbitkan QR dari payload rusak.
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

from services.qris_generator import (
    crc16_ccitt,
    generate_dynamic_qris_string,
    get_qris_image_stream,
    validasi_qris_payload,
)

RUSAK = ("00020101021126680016ID.CO.GOPAY.WWW01189360001438922870000215ID10265038922870303UKE5"
         "1440014ID.CO.QRIS.WWW0215ID10265038922870303UKE5204729953033605802ID5936"
         "TOKO DIGITAL HSN, DIGITAL & KREATIF6011DKI JAKARTA61051212162070703A01630453D8")

VALID = ("00020101021126670015ID.CO.GOPAY.WWW01189360001438922870000222ID10265038922870303UKE"
         "51440014ID.CO.QRIS.WWW0222ID10265038922870303UKE5204729953033605802ID5935"
         "TOKO DIGITAL HSN, DIGITAL & KREATIF6011DKI JAKARTA61051212162070703A016304B55D")


def walk_habis(payload):
    tags, i = __import__("services.qris_generator", fromlist=["_parse_tlv"])._parse_tlv(payload)
    return i == len(payload), tags


class TestValidasiQris(unittest.TestCase):
    def test_template_rusak_ditolak_dengan_alasan_crc(self):
        with self.assertRaises(ValueError) as cm:
            validasi_qris_payload(RUSAK)
        self.assertIn("CRC", str(cm.exception))

    def test_template_rusak_tidak_menghasilkan_qr(self):
        with self.assertRaises(ValueError):
            generate_dynamic_qris_string(RUSAK, 5136)

    def test_template_valid_lulus(self):
        validasi_qris_payload(VALID)

    def test_dinamis_menyisipkan_nominal_dengan_crc_benar(self):
        for amount in (5136, 10000, 136513, 10000000):
            hasil = generate_dynamic_qris_string(VALID, amount)
            habis, tags = walk_habis(hasil)
            self.assertTrue(habis)
            self.assertEqual(hasil[-4:], crc16_ccitt(hasil[:-4]))
            self.assertIn(("54", str(amount)), tags)
            self.assertIn(("01", "12"), tags)

    def test_stream_tidak_mengembalikan_qr_dari_payload_rusak(self):
        os.environ["QRIS_STATIC"] = RUSAK
        try:
            from config.settings import settings
            settings.QRIS_STATIC = RUSAK
            buf = get_qris_image_stream(5136)
        finally:
            os.environ.pop("QRIS_STATIC", None)
            settings.QRIS_STATIC = ""
        # fallback gambar statis repo adalah pointer LFS (131 byte) -> dilewati,
        # dan payload rusak tidak boleh dijadikan QR -> None
        self.assertIsNone(buf)


if __name__ == "__main__":
    unittest.main()
