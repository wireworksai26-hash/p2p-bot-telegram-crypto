"""ID order harus praktis unik.

Dulu ORD-YYYYMMDD-XXX (3 karakter acak = 46.656 kombinasi per hari): peluang
tabrakan ~50% setelah ±250 order sehari -> IntegrityError, user melihat error.
ID juga memakai `random` (bisa ditebak), bukan `secrets`.
"""
import os
import re
import unittest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")

from bot.utils.formatter import generate_order_id  # noqa: E402


class OrderIdUnique(unittest.TestCase):
    def test_tidak_ada_tabrakan_di_volume_harian_tinggi(self):
        ids = [generate_order_id() for _ in range(20_000)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_format_tetap_dikenali(self):
        self.assertRegex(generate_order_id(), r"^ORD-\d{8}-[A-Z0-9]{8}$")
        self.assertLessEqual(len(generate_order_id()), 30)


if __name__ == "__main__":
    unittest.main()
