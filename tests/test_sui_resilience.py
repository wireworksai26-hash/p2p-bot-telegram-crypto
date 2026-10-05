"""Resiliensi RPC SUI + anti-flap alarm — insiden 429 blockvision 3 Okt 2026.

- Sender memutar RPC: env -> publicnode -> blockvision (429 di satu URL
  tidak boleh mematikan SUI).
- Monitor SUI punya fallback beneran (alarm memberi alternatif hidup).
- Alarm DOWN / pulih butuh konfirmasi 2x beruntun (flap = diam).
"""
import os
import sys
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
    "EVM_PRIVATE_KEY": "",
})

from services.coin_api_monitor import CoinAPIMonitor
from services.crypto_sender.sui_sender import SuiSender


def _res(status, ep_id="NONEVM_SUI"):
    return {
        "id": ep_id, "category": "NONEVM_RPC", "name": "Sui Network",
        "symbol": "SUI", "url": "https://x", "env_var": "SUI_RPC",
        "status": status, "latency_ms": 100, "error_code": "-",
        "error_detail": "-", "impact": "-", "fallback_urls": [],
    }


class TestSuiRpcRotation(unittest.TestCase):
    def test_daftar_rpc_berlapis(self):
        sender = SuiSender()
        self.assertGreaterEqual(len(sender.rpc_list), 2)
        self.assertIn("https://sui-rpc.publicnode.com", sender.rpc_list)
        self.assertTrue(all(r.startswith("https://") for r in sender.rpc_list))
        self.assertEqual(len(sender.rpc_list), len(set(sender.rpc_list)))

    def test_429_di_url_pertama_otomatis_pindah(self):
        sender = SuiSender()
        sender.wallet_address = "0x" + "ab" * 32
        sender.rpc_list = ["https://mati.invalid", "https://hidup.invalid"]

        calls = []

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": {"totalBalance": "2500000000"}}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                calls.append(url)
                if "mati" in url:
                    raise RuntimeError("HTTP 429")
                return FakeResp()

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            import asyncio
            saldo = asyncio.run(sender.get_balance("SUI"))
        self.assertEqual(saldo, 2.5)
        self.assertEqual(calls, ["https://mati.invalid", "https://hidup.invalid"])

    def test_semua_rpc_mati_raise(self):
        sender = SuiSender()
        sender.wallet_address = "0x" + "ab" * 32
        sender.rpc_list = ["https://mati1.invalid"]

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                raise RuntimeError("HTTP 429")

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            import asyncio
            with self.assertRaises(RuntimeError):
                asyncio.run(sender.get_balance("SUI"))


class TestSuiMonitorFallback(unittest.TestCase):
    def test_fallback_sui_bukan_url_mati_itu_sendiri(self):
        eps = {e["id"]: e for e in CoinAPIMonitor().get_configured_endpoints()}
        fb = eps["NONEVM_SUI"]["fallback_urls"]
        self.assertGreaterEqual(len(fb), 2)
        self.assertIn("https://sui-rpc.publicnode.com", fb)


class TestAlarmAntiFlap(unittest.IsolatedAsyncioTestCase):
    async def _jalan(self, monitor, statuses):
        with patch.object(monitor, "check_all", new_callable=AsyncMock) as mock_check, \
             patch("services.coin_api_monitor.notify_admins", new_callable=AsyncMock) as mock_notify:
            for st in statuses:
                mock_check.return_value = [_res(st)]
                await monitor.check_all_and_alert(AsyncMock())
            return mock_notify.call_count

    async def test_flap_down_ok_down_ok_diam(self):
        monitor = CoinAPIMonitor()
        try:
            n = await self._jalan(monitor, ["DOWN", "OK", "DOWN", "OK"])
            self.assertEqual(n, 0)
        finally:
            await monitor.close()

    async def test_down_beruntun_baru_alarm(self):
        monitor = CoinAPIMonitor()
        try:
            n = await self._jalan(monitor, ["DOWN", "DOWN"])
            self.assertEqual(n, 1)
        finally:
            await monitor.close()

    async def test_pulih_butuh_dua_kali_sehat(self):
        monitor = CoinAPIMonitor()
        try:
            with patch.object(monitor, "check_all", new_callable=AsyncMock) as mock_check, \
                 patch("services.coin_api_monitor.notify_admins", new_callable=AsyncMock) as mock_notify:
                for st in ["DOWN", "DOWN", "OK"]:
                    mock_check.return_value = [_res(st)]
                    await monitor.check_all_and_alert(AsyncMock())
                self.assertEqual(mock_notify.call_count, 1)  # hanya alarm DOWN
                mock_check.return_value = [_res("OK")]
                await monitor.check_all_and_alert(AsyncMock())
                self.assertEqual(mock_notify.call_count, 2)  # + pemulihan
        finally:
            await monitor.close()


if __name__ == "__main__":
    unittest.main()
