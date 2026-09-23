"""Regresi gerbang kesegaran harga (kasus 'Harga X tidak ditemukan', 22-23 Sep 2026).

Sumber bug: umur data CoinGecko (`last_updated_at`) dinilai terlalu ketat (180s)
sehingga satu keterlambatan update CoinGecko membuat SEMUA harga jadi None,
tanpa log alasan apa pun.
"""
import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base
from services.price_service import (
    MAX_CLOCK_SKEW_SECONDS,
    MAX_PRICE_AGE_SECONDS,
    PriceService,
)

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
TestingSession = sessionmaker(bind=engine)

NOW = time.time()


def baris(updated_age, idr=17000.0, usd=1.0):
    return {"idr": idr, "usd": usd, "last_updated_at": NOW - updated_age}


class TestPriceFreshness(unittest.TestCase):
    def setUp(self):
        self.ps = PriceService()

    def _isi(self, tether_age=10, fetch_age=0):
        self.ps._cache = {"tether": baris(tether_age), "_fetched_at": NOW - fetch_age}

    def test_data_coingecko_300_detik_tetap_dipakai(self):
        self._isi(tether_age=300)
        self.assertIsNotNone(self.ps._valid("tether"))

    def test_data_lebih_dari_batas_ditolak(self):
        self._isi(tether_age=MAX_PRICE_AGE_SECONDS + 1)
        self.assertIsNone(self.ps._valid("tether"))
        self.assertIn("berumur", self.ps._invalid_reason("tether"))

    def test_timestamp_masa_depan_kecil_ditoleransi(self):
        self.ps._cache = {"tether": baris(-30), "_fetched_at": NOW}
        self.assertIsNotNone(self.ps._valid("tether"))

    def test_timestamp_masa_depan_besar_ditolak(self):
        self.ps._cache = {"tether": baris(-MAX_CLOCK_SKEW_SECONDS - 30), "_fetched_at": NOW}
        self.assertIsNone(self.ps._valid("tether"))
        self.assertIn("masa depan", self.ps._invalid_reason("tether"))

    def test_fetch_lama_ditolak_walau_baris_masih_fresh(self):
        self._isi(tether_age=5, fetch_age=MAX_PRICE_AGE_SECONDS + 5)
        self.assertIsNone(self.ps._valid("tether"))
        self.assertIn("gagal diperbarui", self.ps._invalid_reason("tether"))

    def test_nilai_harga_nol_ditolak(self):
        self.ps._cache = {"tether": baris(10, idr=0.0), "_fetched_at": NOW}
        self.assertIsNone(self.ps._valid("tether"))

    def test_get_price_berhasil(self):
        self._isi()
        db = TestingSession()
        with patch.object(self.ps, "_refresh", new=AsyncMock()):
            hasil = asyncio.run(self.ps.get_price("USDT", db))
        db.close()
        self.assertIsNotNone(hasil)
        self.assertAlmostEqual(hasil["market_price_idr"], 17000.0)

    def test_get_price_gagal_mencatat_alasan(self):
        self.ps._cache = {"_fetched_at": NOW}
        db = TestingSession()
        with patch.object(self.ps, "_refresh", new=AsyncMock()):
            with self.assertLogs("services.price_service", level="WARNING") as cm:
                hasil = asyncio.run(self.ps.get_price("USDT", db))
        db.close()
        self.assertIsNone(hasil)
        self.assertTrue(any("USDT" in line for line in cm.output), cm.output)

    def test_get_price_simbol_tanpa_id_coingecko(self):
        db = TestingSession()
        with patch.object(self.ps, "_refresh", new=AsyncMock()):
            with self.assertLogs("services.price_service", level="WARNING") as cm:
                hasil = asyncio.run(self.ps.get_price("TOKENNGACO", db))
        db.close()
        self.assertIsNone(hasil)
        self.assertTrue(any("CoinGecko" in line for line in cm.output), cm.output)


if __name__ == "__main__":
    unittest.main()
