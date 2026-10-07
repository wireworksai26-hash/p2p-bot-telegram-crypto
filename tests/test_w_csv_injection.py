"""Ekspor CSV laporan tidak boleh mengeksekusi formula dari data user.

Nama Telegram / username / rekening dikendalikan user. Isi seperti
`=HYPERLINK("http://evil","klik")` atau `=cmd|' /C calc'!A0` dijalankan Excel/Sheets
saat admin membuka laporan (CSV/formula injection).
"""
import csv
import io
import os
import unittest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")

from services.report_service import generate_weekly_report_csv_buffer  # noqa: E402

EVIL = ['=HYPERLINK("http://evil.example","klik")', "+1+1", "-2+3", "@SUM(A1)", "\t=1+1", "\r=1+1"]


def _row(**over):
    row = {"order_id": "ORD-1", "created_at": "2026-10-07 10:00", "order_type": "sell",
           "status": "completed", "username": "budi", "telegram_id": 1, "full_name": "Budi",
           "nominal_idr": 10000, "crypto_amount": 1.5, "crypto_symbol": "USDT", "network": "BSC",
           "destination": "BCA | 123 | Budi", "payment_method": "-", "fee_idr": 3000,
           "tx_hash": "0xabc", "explorer_url": ""}
    row.update(over)
    return row


class CsvInjection(unittest.TestCase):
    def _cells(self, rows):
        text = generate_weekly_report_csv_buffer(rows).getvalue().decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text)))[1:]

    def test_isian_user_tidak_jadi_formula(self):
        rows = [_row(full_name=e, username=e, destination=e) for e in EVIL]
        for cells in self._cells(rows):
            for idx in (5, 7, 12):  # username, nama, tujuan
                self.assertFalse(cells[idx][:1] in ("=", "+", "-", "@", "\t", "\r"), cells[idx])

    def test_angka_tetap_angka(self):
        cells = self._cells([_row()])[0]
        self.assertEqual(cells[8], "10000")
        self.assertEqual(cells[9], "1.5")
        self.assertEqual(cells[7], "Budi")


if __name__ == "__main__":
    unittest.main()
