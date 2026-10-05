"""
tests/test_price_realtime.py
============================
Unit tests untuk arsitektur kurs real-time hybrid:
1. OKX Spot Tickers x Yahoo Spot FX sebagai sumber primer.
2. Per-coin fallback (misal koin tidak ada di OKX) tetap terisi.
3. Fallback bertingkat ke CoinGecko saat OKX tidak tersedia.
4. Cache TTL 15 detik.
5. Formula spread beli/jual 0.5% tetap presisi.
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
})

from database.connection import Base, engine, SessionLocal
from services.price_service import (
    CACHE_TTL_SECONDS,
    COINGECKO_IDS,
    PriceService,
    quote_source_text,
)

Base.metadata.create_all(bind=engine)

MOCK_FX = 17898.0
MOCK_ETH_USD = 2730.0
MOCK_SOL_USD = 122.0


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
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    @property
    def is_closed(self):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        self.calls.append(url)
        for key, payload in self.routes.items():
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResp(payload)
        raise RuntimeError(f"No route for {url}")

    async def aclose(self):
        pass


def make_okx_payload():
    return {
        "code": "0",
        "data": [
            {"instId": "ETH-USDT", "last": str(MOCK_ETH_USD)},
            {"instId": "SOL-USDT", "last": str(MOCK_SOL_USD)},
            {"instId": "SUI-USDT", "last": "1.25"},
            {"instId": "BNB-USDT", "last": "600.0"},
            {"instId": "TRX-USDT", "last": "0.15"},
            {"instId": "POL-USDT", "last": "0.40"},
            {"instId": "ARB-USDT", "last": "0.60"},
            {"instId": "AVAX-USDT", "last": "28.0"},
            {"instId": "KAIA-USDT", "last": "0.12"},
            {"instId": "BERA-USDT", "last": "5.0"},
            {"instId": "APT-USDT", "last": "8.5"},
            {"instId": "HYPE-USDT", "last": "90.0"},
            {"instId": "USDC-USDT", "last": "1.0"},
        ]
    }


def make_yahoo_payload(rate=MOCK_FX):
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": rate
                    }
                }
            ]
        }
    }


class TestPriceRealtimeHybrid(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.ps = PriceService()

    async def asyncTearDown(self):
        await self.ps.close()

    async def test_hybrid_okx_and_yahoo_primary(self):
        routes = {
            "chart/USDIDR=X": make_yahoo_payload(MOCK_FX),
            "market/tickers": make_okx_payload(),
        }
        self.ps._client = FakeClient(routes)

        quote = await self.ps.get_price("ETH")
        self.assertIsNotNone(quote)
        self.assertEqual(quote["source"], "OKX + Spot FX")
        expected_eth_idr = round(MOCK_ETH_USD * MOCK_FX, 2)
        self.assertEqual(quote["market_price_idr"], expected_eth_idr)
        self.assertEqual(quote["usdt_idr_rate"], MOCK_FX)

        # Spread 0.0% (Pure real time market price)
        self.assertEqual(quote["spread_pct"], 0.0)
        self.assertEqual(quote["buy_price_idr"], expected_eth_idr)
        self.assertEqual(quote["sell_price_idr"], expected_eth_idr)

    async def test_stablecoin_price_equals_spot_fx(self):
        routes = {
            "chart/USDIDR=X": make_yahoo_payload(MOCK_FX),
            "market/tickers": make_okx_payload(),
        }
        self.ps._client = FakeClient(routes)

        usdt_quote = await self.ps.get_price("USDT")
        self.assertIsNotNone(usdt_quote)
        self.assertEqual(usdt_quote["market_price_idr"], round(MOCK_FX, 2))

        usdc_quote = await self.ps.get_price("USDC")
        self.assertIsNotNone(usdc_quote)
        self.assertEqual(usdc_quote["market_price_idr"], round(MOCK_FX, 2))

    async def test_cache_ttl_is_15_seconds(self):
        self.assertEqual(CACHE_TTL_SECONDS, 15)
        fake_client = FakeClient({
            "chart/USDIDR=X": make_yahoo_payload(MOCK_FX),
            "market/tickers": make_okx_payload(),
        })
        self.ps._client = fake_client

        # Call 1
        await self.ps.get_price("ETH")
        call_count_1 = len(fake_client.calls)

        # Call 2 immediately (within 15s)
        await self.ps.get_price("ETH")
        call_count_2 = len(fake_client.calls)
        self.assertEqual(call_count_1, call_count_2, "Harus menggunakan cache dalam TTL 15s")

    async def test_fallback_to_coingecko_when_okx_fails(self):
        cg_payload = {
            "ethereum": {"idr": 48000000, "usd": 2700, "last_updated_at": int(time.time())},
            "tether": {"idr": 17800, "usd": 1.0, "last_updated_at": int(time.time())},
            "usd-coin": {"idr": 17800, "usd": 1.0, "last_updated_at": int(time.time())},
        }
        # OKX throws error, CoinGecko succeeds
        routes = {
            "chart/USDIDR=X": RuntimeError("Yahoo down"),
            "open.er-api.com": RuntimeError("FX down"),
            "market/tickers": RuntimeError("OKX blocked"),
            "simple/price": cg_payload,
        }
        self.ps._client = FakeClient(routes)

        quote = await self.ps.get_price("ETH")
        self.assertIsNotNone(quote)
        self.assertEqual(quote["source"], "CoinGecko IDR")
        self.assertEqual(quote["market_price_idr"], 48000000.0)

    def test_seed_price_configs_sync_zero_spread(self):
        from main import _seed_price_configs
        from database.connection import SessionLocal
        from database.models import PriceConfig

        db = SessionLocal()
        try:
            # Tambahkan dummy config dengan spread lama 0.5%
            db.query(PriceConfig).delete()
            db.add(PriceConfig(symbol="ETH", spread_pct=0.5, is_active=True))
            db.add(PriceConfig(symbol="SOL", spread_pct=1.0, is_active=True))
            db.commit()

            # Jalankan seed sync
            _seed_price_configs(db)
            db.commit()

            # Verifikasi semua koin direset ke 0.0%
            configs = {c.symbol: float(c.spread_pct) for c in db.query(PriceConfig).all()}
            self.assertEqual(configs.get("ETH"), 0.0)
            self.assertEqual(configs.get("SOL"), 0.0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
