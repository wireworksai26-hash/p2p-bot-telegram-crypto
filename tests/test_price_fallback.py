"""Fallback harga Binance+FX saat CoinGecko limit + backoff anti badai retry."""
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
    BINANCE_SYMBOLS,
    COINGECKO_IDS,
    PriceService,
)

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
TestingSession = sessionmaker(bind=engine)

FX = 17000.0
ETH_USD = 2000.0


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeClient:
    """httpx tiruan: rute berdasarkan substring URL."""

    def __init__(self, routes):
        self.routes = routes
        self.panggilan = []

    @property
    def is_closed(self):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        self.panggilan.append(url)
        for kunci, payload in self.routes.items():
            if kunci in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResp(payload)
        raise RuntimeError(f"tanpa rute {url}")

    async def aclose(self):
        pass


def rute_binance_lengkap():
    harga = [{"symbol": s, "price": str(ETH_USD if s == "ETHUSDT" else 10.0)}
             for s in sorted(set(BINANCE_SYMBOLS.values()))]
    return harga


def rute_sukses():
    return {
        "api.coingecko.com": {"tether": {"idr": 16500.0, "usd": 1.0, "last_updated_at": int(time.time())}},
        "open.er-api.com": {"rates": {"IDR": FX}},
        "api.binance.com": rute_binance_lengkap(),
    }


class TestFallbackBinance(unittest.TestCase):
    def setUp(self):
        self.ps = PriceService()

    def _pakai(self, routes):
        client = FakeClient(routes)
        self.ps._get_client = AsyncMock(return_value=client)
        return client

    def test_fetch_binance_mengisi_semua_id_coingecko(self):
        self._pakai(rute_sukses())
        data = asyncio.run(self.ps._fetch_binance())
        self.assertEqual(set(data), set(COINGECKO_IDS.values()))

    def test_stable_memakai_kurs_fx(self):
        self._pakai(rute_sukses())
        data = asyncio.run(self.ps._fetch_binance())
        self.assertEqual(data["tether"]["idr"], FX)
        self.assertEqual(data["global-dollar"]["idr"], FX)
        self.assertEqual(data["usd-coin"]["usd"], 1.0)

    def test_refresh_jatuh_ke_binance_saat_coingecko_gagal(self):
        rute = rute_sukses()
        rute["api.coingecko.com"] = RuntimeError("429")
        self._pakai(rute)
        asyncio.run(self.ps._refresh())
        self.assertEqual(self.ps._cache["_source"], "Binance+FX")
        self.assertEqual(self.ps._cache["ethereum"]["idr"], ETH_USD * FX)

    def test_refresh_sukses_coingecko_tetap_sumber_coingecko(self):
        self._pakai(rute_sukses())
        asyncio.run(self.ps._refresh())
        self.assertEqual(self.ps._cache["_source"], "CoinGecko IDR")
        self.assertIn("tether", self.ps._cache)

    def test_kedua_sumber_gagal_cache_kosong_tapi_tercap(self):
        rute = {"api.coingecko.com": RuntimeError("429"), "open.er-api.com": RuntimeError("500"),
                "api.frankfurter.app": RuntimeError("500"), "api.binance.com": RuntimeError("503")}
        self._pakai(rute)
        asyncio.run(self.ps._refresh())
        self.assertIn("_fetched_at", self.ps._cache)
        self.assertIsNone(self.ps._cache.get("tether"))

    def test_backoff_gagal_tidak_mengulang_dalam_ttl(self):
        rute = {"api.coingecko.com": RuntimeError("429"), "open.er-api.com": RuntimeError("500"),
                "api.frankfurter.app": RuntimeError("500"), "api.binance.com": RuntimeError("503")}
        client = self._pakai(rute)
        asyncio.run(self.ps._refresh())
        n_setelah_pertama = len(client.panggilan)
        self.assertGreater(n_setelah_pertama, 0)
        asyncio.run(self.ps._refresh())
        self.assertEqual(len(client.panggilan), n_setelah_pertama)

    def test_get_price_memakai_sumber_fallback(self):
        rute = rute_sukses()
        rute["api.coingecko.com"] = RuntimeError("429")
        self._pakai(rute)
        asyncio.run(self.ps._refresh())
        db = TestingSession()
        try:
            hasil = asyncio.run(self.ps.get_price("ETH", db))
        finally:
            db.close()
        self.assertIsNotNone(hasil)
        self.assertEqual(hasil["source"], "Binance+FX")
        self.assertAlmostEqual(hasil["market_price_idr"], ETH_USD * FX)
        self.assertAlmostEqual(hasil["usdt_idr_rate"], FX)


if __name__ == "__main__":
    unittest.main()
