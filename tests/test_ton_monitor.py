"""Monitor TON: fallback harus provider berbeda, sehingga timeout toncenter v2 (tanpa API key sering
lambat/timeout) tidak langsung menjadi alarm DOWN selama jalur lain hidup."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    "EVM_PRIVATE_KEY": "",
})

import httpx  # noqa: E402
from services.coin_api_monitor import CoinAPIMonitor  # noqa: E402

V2 = "https://toncenter.com/api/v2/jsonRPC"
V3 = "https://toncenter.com/api/v3/masterchainInfo"
TONAPI = "https://tonapi.io/v2/blockchain/masterchain-head"
TONHUB = "https://mainnet-v4.tonhubapi.com/block/latest"


def _resp(body, status=200):
    resp = MagicMock(status_code=status)
    resp.json.return_value = body
    return resp


class FakeClient:
    """Balasan per URL; nilai Exception akan dilempar."""

    def __init__(self, by_url):
        self.by_url = by_url
        self.calls = []

    async def _answer(self, url):
        self.calls.append(url)
        value = self.by_url[url]
        if isinstance(value, Exception):
            raise value
        return value

    async def post(self, url, json=None, headers=None):
        return await self._answer(url)

    async def get(self, url, headers=None):
        return await self._answer(url)


class TestTonMonitor(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.monitor = CoinAPIMonitor()
        self.ep = next(e for e in self.monitor.get_configured_endpoints() if e["id"] == "NONEVM_TON")
        self.ep["url"] = V2  # lingkungan uji: bebas dari env

    async def asyncTearDown(self):
        await self.monitor.close()

    def test_fallback_provider_berbeda_dan_bukan_saran_isi_env(self):
        self.assertNotIn(V2, self.ep["fallback_urls"])
        self.assertEqual(set(self.ep["fallback_urls"]), {V3, TONAPI, TONHUB})
        self.assertEqual(self.ep["hint_urls"], [])  # bentuk API-nya bukan JSON-RPC: bukan isi TON_RPC

    async def _probe(self, url, body):
        client = FakeClient({url: _resp(body)})
        return await self.monitor._probe(client, url, self.ep)

    async def test_probe_tiap_bentuk_api_ton(self):
        cases = [
            (V2, {"ok": True, "result": {"last": {"seqno": 103047369}}}),
            (V3, {"last": {"seqno": 103047375}}),
            (TONAPI, {"seqno": 103047380}),
            (TONHUB, {"last": {"seqno": 103047405}}),
        ]
        for url, body in cases:
            with self.subTest(url=url):
                res = await self._probe(url, body)
                self.assertIn(res["status"], ("OK", "DEGRADED"))
                self.assertIn("Seqno #", res["block_info"])

    async def test_balasan_tanpa_seqno_dianggap_error_bukan_sehat(self):
        for url, body in ((V3, {"error": "x"}), (TONAPI, {"error": "x"}), (TONHUB, {}), (V2, {"error": "x"})):
            with self.subTest(url=url):
                res = await self._probe(url, body)
                self.assertEqual(res["status"], "DOWN")
                self.assertEqual(res["error_code"], "RPC_ERROR")

    async def test_toncenter_v2_timeout_tapi_fallback_hidup_maka_bukan_down(self):
        client = FakeClient({
            V2: httpx.ReadTimeout("timeout"),
            V3: _resp({"last": {"seqno": 103047375}}),
        })
        with patch.object(self.monitor, "_get_client", new=AsyncMock(return_value=client)):
            res = await self.monitor.check_single_endpoint(self.ep)
        self.assertEqual(res["status"], "DEGRADED")  # tidak memicu alarm DOWN
        self.assertEqual(res["error_code"], "PRIMARY_DOWN")
        self.assertEqual(client.calls, [V2, V3])

    async def test_semua_provider_mati_baru_down(self):
        client = FakeClient({V2: httpx.ReadTimeout("t"), V3: httpx.ReadTimeout("t"),
                             TONAPI: httpx.ReadTimeout("t"), TONHUB: httpx.ReadTimeout("t")})
        with patch.object(self.monitor, "_get_client", new=AsyncMock(return_value=client)):
            res = await self.monitor.check_single_endpoint(self.ep)
        self.assertEqual(res["status"], "DOWN")

    async def test_pesan_alarm_ton_tidak_menyarankan_url_non_json_rpc(self):
        res = {"id": "NONEVM_TON", "name": "TON", "symbol": "GRAM / USDT", "url": V2, "env_var": "TON_RPC",
               "fallback_urls": self.ep["fallback_urls"], "hint_urls": self.ep["hint_urls"], "latency_ms": 6000,
               "error_code": "TIMEOUT", "error_detail": "Timeout", "impact": "x"}
        text = self.monitor.format_alarm_message(res)
        self.assertNotIn("Rekomendasi URL Alternatif", text)
        self.assertNotIn("tonapi.io", text)


if __name__ == "__main__":
    unittest.main()
