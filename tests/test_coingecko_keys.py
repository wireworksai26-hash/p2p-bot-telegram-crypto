"""Rotasi multi-key CoinGecko (kuota 10k kredit/bln per akun, terpisah).

Regresi:
- parse env COINGECKO_API_KEYS (koma/spasi, dedupe, fallback key lama)
- round-robin rata antar key
- failover 401/403/429 ke key berikut, semua habis -> None (fallback Paprika)
- refresh_all_prices TIDAK memaksa melewati cache (dulu force=True = 1 call/tick
  scheduler 30 dtk = kuota habis ~3 hari)
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
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
    "COINGECKO_API_KEYS": "",
    "COINGECKO_API_KEY": "",
})

from config.settings import parse_coingecko_keys, settings
from services import price_service
from services.price_service import PriceService, next_coingecko_key

PAYLOAD = {"tether": {"idr": 16300.0, "usd": 1.0, "last_updated_at": 1758000000}}


def _resp(status, payload=None):
    r = SimpleNamespace(status_code=status)
    r.json = lambda: payload if payload is not None else {}

    def _raise():
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")

    r.raise_for_status = _raise
    return r


class TestParseKey(unittest.TestCase):
    def test_gabung_koma_spasi_dan_dedupe(self):
        hasil = parse_coingecko_keys("CG-a,CG-b  CG-c", "CG-d, CG-a")
        self.assertEqual(hasil, ("CG-a", "CG-b", "CG-c", "CG-d"))

    def test_kosong_jadi_tuple_kosong(self):
        self.assertEqual(parse_coingecko_keys("", "  "), ())

    def test_fallback_key_lama(self):
        # Env COINGECKO_API_KEY lama tetap dipakai bila COINGECKO_API_KEYS absen
        self.assertEqual(
            settings.COINGECKO_API_KEYS,
            parse_coingecko_keys(
                os.getenv("COINGECKO_API_KEYS") or "",
                os.getenv("COINGECKO_API_KEY") or "",
            ),
        )


class TestRotasi(unittest.TestCase):
    def setUp(self):
        price_service._key_seq = 0

    def tearDown(self):
        price_service._key_seq = 0

    def test_round_robin_rata(self):
        with patch.object(settings, "COINGECKO_API_KEYS", tuple(f"k{i}" for i in range(6))):
            hasil = [next_coingecko_key() for _ in range(12)]
        for i in range(6):
            self.assertEqual(hasil.count(f"k{i}"), 2, f"key k{i} harus dipakai 2x")

    def test_tanpa_key_kembalikan_none(self):
        with patch.object(settings, "COINGECKO_API_KEYS", ()):
            self.assertIsNone(next_coingecko_key())


class TestFetchCoingecko(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        price_service._key_seq = 0
        self.svc = PriceService()

    def tearDown(self):
        price_service._key_seq = 0

    async def _dengan_client(self, client, coro):
        with patch.object(self.svc, "_get_client", new=AsyncMock(return_value=client)):
            return await coro()

    async def test_failover_429_ke_key_berikut(self):
        client = AsyncMock()
        client.get.side_effect = [_resp(429), _resp(200, PAYLOAD)]
        with patch.object(settings, "COINGECKO_API_KEYS", ("kA", "kB")):
            data = await self._dengan_client(client, self.svc._fetch_coingecko)
        self.assertEqual(data, PAYLOAD)
        self.assertEqual(client.get.call_count, 2)
        awal = client.get.call_args_list[0].kwargs["headers"]
        kedua = client.get.call_args_list[1].kwargs["headers"]
        self.assertEqual(awal["x-cg-demo-api-key"], "kA")
        self.assertEqual(kedua["x-cg-demo-api-key"], "kB")

    async def test_failover_401_dan_403(self):
        for status in (401, 403):
            with self.subTest(status=status):
                price_service._key_seq = 0
                client = AsyncMock()
                client.get.side_effect = [_resp(status), _resp(200, PAYLOAD)]
                with patch.object(settings, "COINGECKO_API_KEYS", ("kA", "kB")):
                    data = await self._dengan_client(client, self.svc._fetch_coingecko)
                self.assertEqual(data, PAYLOAD)

    async def test_semua_key_limit_kembalikan_none(self):
        client = AsyncMock()
        client.get.side_effect = [_resp(429), _resp(429), _resp(429)]
        with patch.object(settings, "COINGECKO_API_KEYS", ("kA", "kB", "kC")):
            data = await self._dengan_client(client, self.svc._fetch_coingecko)
        self.assertIsNone(data)
        self.assertEqual(client.get.call_count, 3)

    async def test_tanpa_key_tanpa_header(self):
        client = AsyncMock()
        client.get.return_value = _resp(200, PAYLOAD)
        with patch.object(settings, "COINGECKO_API_KEYS", ()):
            data = await self._dengan_client(client, self.svc._fetch_coingecko)
        self.assertEqual(data, PAYLOAD)
        headers = client.get.call_args.kwargs.get("headers") or {}
        self.assertNotIn("x-cg-demo-api-key", headers)

    async def test_refresh_all_prices_hormati_cache(self):
        # Dulu force=True: tiap tick scheduler (30 dtk) = 1 call API nyata.
        client = AsyncMock()
        client.get.return_value = _resp(200, PAYLOAD)
        with patch.object(settings, "COINGECKO_API_KEYS", ("kA",)):
            await self._dengan_client(client, self.svc.refresh_all_prices)
            await self._dengan_client(client, self.svc.refresh_all_prices)
        self.assertEqual(client.get.call_count, 1, "panggilan kedua harus cache-hit")


if __name__ == "__main__":
    unittest.main()
