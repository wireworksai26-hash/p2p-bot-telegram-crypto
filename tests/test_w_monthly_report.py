"""Laporan bulanan: bulan yang dilaporkan harus lengkap, dan fee tidak dihitung dua kali.

Dulu cron "hari terakhir 09:00" memakai zona waktu container (UTC = 16:00 WIB) dan
melaporkan bulan BERJALAN -> transaksi 8+ jam terakhir bulan tidak pernah tercatat
(guard anti-ganda mencegah laporan diperbarui). TOTAL MASUK = volume + fee + topup,
padahal volume (total_idr order) sudah mengandung fee.
"""
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import main  # noqa: E402
from database import crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order  # noqa: E402


class MonthlyReport(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def test_periode_yang_dilaporkan_adalah_bulan_lalu_dalam_wib(self):
        # 31 Okt 2026 17:00 UTC = 1 Nov 00:00 WIB -> laporkan Oktober yang sudah lengkap.
        self.assertEqual(main._monthly_report_period(datetime(2026, 10, 31, 17, 5, tzinfo=timezone.utc)),
                         (2026, 10))
        self.assertEqual(main._monthly_report_period(datetime(2027, 1, 1, 0, 30, tzinfo=timezone.utc)),
                         (2026, 12))

    def test_jadwal_cron_tanggal_1_wib(self):
        trig = main._monthly_report_trigger()
        fields = {f.name: str(f) for f in trig.fields}
        self.assertEqual(fields["day"], "1")
        self.assertEqual(str(trig.timezone), "Asia/Jakarta")

    def test_transaksi_jam_terakhir_bulan_ikut_terhitung(self):
        db = SessionLocal()
        try:
            # 31 Okt 23:30 WIB = 16:30 UTC
            db.add(Order(order_id="ORD-LATE", telegram_id=1, order_type="buy", crypto_symbol="USDT",
                         network="BSC", crypto_amount=Decimal("1"), price_per_unit=1, nominal_idr=97_000,
                         fee_idr=3_000, total_idr=100_000, status="completed",
                         created_at=datetime(2026, 10, 31, 16, 30)))
            db.commit()
            report = crud.build_monthly_report(db, 2026, 10)
        finally:
            db.close()
        self.assertEqual(report.order_count, 1)
        self.assertEqual(report.fee_idr, 3_000)
        self.assertEqual(report.total_idr, 100_000, "fee sudah termasuk total order — jangan dihitung ulang")


if __name__ == "__main__":
    unittest.main()
