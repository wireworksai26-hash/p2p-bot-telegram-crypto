"""K3 — nominal QRIS tidak boleh ambigu antara satuan sen dan rupiah.

Gateway membagi gross_amount kelipatan 100 dengan 100. Tagihan bot yang
totalnya kelipatan 100 membuat satu pembayaran bisa dibaca dua nominal
(dulu: bayar Rp 1.000 melunasi tagihan Rp 100.000). Bot kini tidak pernah
menerbitkan total kelipatan 100, dan gateway hanya punya satu tafsiran.
"""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

import database.models  # noqa: F401
from database import crud
from database.connection import Base, SessionLocal, engine

ROOT = Path(__file__).resolve().parents[1]


class TotalBukanKelipatan100(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_kode_yang_membuat_total_bulat_ditolak(self):
        # 49.900 + 100 = 50.000 (kelipatan 100) -> ambigu.
        self.assertIsNone(crud.generate_unique_payment_code(
            self.db, base_amount=49_900, min_code=100, max_code=100))

    def test_total_tidak_pernah_kelipatan_100(self):
        for base in (49_900, 50_000, 99_800, 1_000_000, 5_000):
            for _ in range(200):
                kode = crud.generate_unique_payment_code(self.db, base_amount=base)
                self.assertNotEqual((base + kode) % 100, 0, f"base {base} kode {kode}")


@unittest.skipUnless(shutil.which("node"), "node tidak terpasang")
class GatewayAmountJs(unittest.TestCase):
    def test_gateway_amount_suite(self):
        res = subprocess.run(
            ["node", "--test", *sorted(str(f) for f in (ROOT / "gopay-gateway" / "test").glob("*.test.js"))],
            capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(res.returncode, 0, res.stdout[-3000:] + res.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
