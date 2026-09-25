"""Fallback harga CoinPaprika / CoinMarketCap saat CoinGecko limit + backoff anti badai retry."""
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
    "COINMARKETCAP_API_KEY": "TESTKEY",
})

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base
from config.settings import settings
from services.price_service import (
    CMC_SYMBOLS,
    COINGECKO_IDS,
    PAPRIKA_IDS,
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
        self.headers_dipakai = []

    @property
    def is_closed(self):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        self.panggilan.append(url)
        self.headers_dipakai.append(headers or {})
        for kunci, payload in self.routes.items():
            if kunci in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResp(payload)
        raise RuntimeError(f"tanpa rute {url}")

    async def aclose(self):
        pass


def rute_paprika_lengkap():
    baris = []
    for paprika_id in sorted(set(PAPRIKA_IDS.values())):
        baris.append({"id": paprika_id, "quotes": {"USD": {
            "price": ETH_USD if paprika_id == "eth-ethereum" else 10.0}}})
    return baris


def rute_cmc_lengkap():
    data = {}
    for symbol in sorted(set(CMC_SYMBOLS.values())):
        data[symbol] = {"symbol": symbol, "quote": {"USD": {
            "price": ETH_USD if symbol == "ETH" else 10.0}}}
    return {"data": data}


def rute_sukses():
    return {
        "api.coingecko.com": {"tether": {"idr": 16500.0, "usd": 1.0, "last_updated_at": int(time.time())}},
        "open.er-api.com": {"rates": {"IDR": FX}},
        "api.coinpaprika.com": rute_paprika_lengkap(),
        "pro-api.coinmarketcap.com": rute_cmc_lengkap(),
    }


class TestFallbackHarga(unittest.TestCase):
    def setUp(self):
        self._key_lama = settings.COINMARKETCAP_API_KEY
        settings.COINMARKETCAP_API_KEY = "TESTKEY"
        self.ps = PriceService()

    def tearDown(self):
        settings.COINMARKETCAP_API_KEY = self._key_lama

    def _pakai(self, routes):
        client = FakeClient(routes)
        self.ps._get_client = AsyncMock(return_value=client)
        return client

    def test_fetch_paprika_mengisi_semua_id_coingecko(self):
        self._pakai(rute_sukses())
        data = asyncio.run(self.ps._fetch_paprika(FX))
        self.assertEqual(set(data), set(COINGECKO_IDS.values()))

    def test_stable_memakai_kurs_fx(self):
        self._pakai(rute_sukses())
        data = asyncio.run(self.ps._fetch_paprika(FX))
        self.assertEqual(data["tether"]["idr"], FX)
        self.assertEqual(data["global-dollar"]["idr"], FX)
        self.assertEqual(data["usd-coin"]["usd"], 1.0)

    def test_refresh_jatuh_ke_paprika_saat_coingecko_gagal(self):
        rute = rute_sukses()
        rute["api.coingecko.com"] = RuntimeError("429")
        self._pakai(rute)
        asyncio.run(self.ps._refresh())
        self.assertEqual(self.ps._cache["_source"], "CoinPaprika+FX")
        self.assertEqual(self.ps._cache["ethereum"]["idr"], ETH_USD * FX)

    def test_refresh_jatuh_ke_cmc_saat_paprika_juga_gagal(self):
        rute = rute_sukses()
        rute["api.coingecko.com"] = RuntimeError("429")
        rute["api.coinpaprika.com"] = RuntimeError("503")
        self._pakai(rute)
        asyncio.run(self.ps._refresh())
        self.assertEqual(self.ps._cache["_source"], "CoinMarketCap+FX")
        self.assertEqual(self.ps._cache["ethereum"]["idr"], ETH_USD * FX)

    def test_cmc_membawa_header_api_key(self):
        rute = rute_sukses()
        rute["api.coingecko.com"] = RuntimeError("429")
        rute["api.coinpaprika.com"] = RuntimeError("503")
        client = self._pakai(rute)
        asyncio.run(self.ps._refresh())
        headers_cmc = [h for u, h in zip(client.panggilan, client.headers_dipakai) if "coinmarketcap" in u]
        self.assertTrue(headers_cmc)
        self.assertEqual(headers_cmc[0].get("X-CMC_PRO_API_KEY"), "TESTKEY")

    def test_refresh_sukses_coingecko_tetap_sumber_coingecko(self):
        self._pakai(rute_sukses())
        asyncio.run(self.ps._refresh())
        self.assertEqual(self.ps._cache["_source"], "CoinGecko IDR")
        self.assertIn("tether", self.ps._cache)

    def test_semua_sumber_gagal_cache_kosong_tapi_tercap(self):
        rute = {"api.coingecko.com": RuntimeError("429"), "open.er-api.com": RuntimeError("500"),
                "api.frankfurter.dev": RuntimeError("500"), "api.coinpaprika.com": RuntimeError("503"),
                "pro-api.coinmarketcap.com": RuntimeError("503")}
        self._pakai(rute)
        asyncio.run(self.ps._refresh())
        self.assertIn("_fetched_at", self.ps._cache)
        self.assertIsNone(self.ps._cache.get("tether"))

    def test_backoff_gagal_tidak_mengulang_dalam_ttl(self):
        rute = {"api.coingecko.com": RuntimeError("429"), "open.er-api.com": RuntimeError("500"),
                "api.frankfurter.dev": RuntimeError("500"), "api.coinpaprika.com": RuntimeError("503"),
                "pro-api.coinmarketcap.com": RuntimeError("503")}
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
        self.assertEqual(hasil["source"], "CoinPaprika+FX")
        self.assertAlmostEqual(hasil["market_price_idr"], ETH_USD * FX)
        self.assertAlmostEqual(hasil["usdt_idr_rate"], FX)


if __name__ == "__main__":
    unittest.main()
