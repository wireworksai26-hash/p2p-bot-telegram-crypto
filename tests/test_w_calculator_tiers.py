"""Kalkulator fee harus sama dengan fee yang dikenakan saat transaksi.

Dulu nominal > Rp 1,01 jt menampilkan "N/A (di atas batas)", padahal Beli/Jual/Convert
memakai tier persen (3% / 2,5% / 2,3% / 2%) untuk nominal hingga Rp 5.000.000.
Di atas Rp 5.000.000, sistem menolak dan mengarahkan chat admin.
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")

from bot.handlers.calculator import process_nominal  # noqa: E402
from bot.utils.formatter import format_idr  # noqa: E402
from services.fee_service import calculate_fee_idr  # noqa: E402


class CalculatorTiers(unittest.IsolatedAsyncioTestCase):
    async def _calc(self, text):
        msg = MagicMock()
        msg.text = text
        msg.reply_text = AsyncMock()
        await process_nominal(SimpleNamespace(message=msg), SimpleNamespace(user_data={}))
        return msg.reply_text.await_args.kwargs["text"]

    async def test_nominal_besar_memakai_tier_persen(self):
        for nominal in (1_500_000, 3_000_000, 5_000_000):
            with self.subTest(nominal=nominal):
                shown = await self._calc(str(nominal))
                self.assertNotIn("N/A", shown)
                for cat in ("USD", "ALTCOIN", "CONVERT"):
                    self.assertIn(format_idr(calculate_fee_idr(nominal, cat)), shown)

    async def test_kontrol_nominal_kecil(self):
        shown = await self._calc("50000")
        self.assertIn(format_idr(calculate_fee_idr(50_000, "ALTCOIN")), shown)

    async def test_nominal_diatas_5_juta_ditolak(self):
        shown = await self._calc("6000000")
        self.assertIn("Nominal Melebihi Batas!", shown)
        self.assertIn("Rp 5.000.000", shown)


if __name__ == "__main__":
    unittest.main()
