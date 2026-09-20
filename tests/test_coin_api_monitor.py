"""Offline unit tests for Coin API & RPC Early Warning Alarm Monitor.

Run: python -m unittest tests/test_coin_api_monitor.py -v
"""
import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "DATABASE_URL": "sqlite:///:memory:",
    "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "123456,789012",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

import httpx
from services.coin_api_monitor import (
    CoinAPIMonitor,
    coin_api_monitor,
    LATENCY_DEGRADED_THRESHOLD_MS,
)


class TestCoinAPIMonitor(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.monitor = CoinAPIMonitor()

    async def asyncTearDown(self):
        await self.monitor.close()

    def test_configured_endpoints_count_and_keys(self):
        endpoints = self.monitor.get_configured_endpoints()
        self.assertGreaterEqual(len(endpoints), 10)

        required_keys = {"id", "category", "name", "url", "env_var", "fallback_urls", "impact"}
        for ep in endpoints:
            for k in required_keys:
                self.assertIn(k, ep, f"Endpoint {ep.get('id')} missing key {k}")

    async def test_check_price_api_success(self):
        ep = {
            "id": "PRICE_COINGECKO",
            "category": "PRICE_API",
            "name": "CoinGecko Price API",
            "url": "https://api.coingecko.com/api/v3/simple/price",
            "env_var": "COINGECKO_API_KEY",
        }
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"tether": {"idr": 16250}}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "OK")
            self.assertIn("16,250", res["block_info"])
            self.assertEqual(res["error_code"], None)

    async def test_check_price_api_404_url_changed(self):
        ep = {
            "id": "PRICE_COINGECKO",
            "category": "PRICE_API",
            "name": "CoinGecko Price API",
            "url": "https://api.coingecko.com/api/v3/bad_route",
            "env_var": "COINGECKO_API_KEY",
        }
        mock_resp = MagicMock(status_code=404)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "DOWN")
            self.assertEqual(res["error_code"], "HTTP_404")
            self.assertIn("404 Not Found", res["error_detail"])

    async def test_check_price_api_429_rate_limited(self):
        ep = {
            "id": "PRICE_COINGECKO",
            "category": "PRICE_API",
            "name": "CoinGecko Price API",
            "url": "https://api.coingecko.com/api/v3/simple/price",
            "env_var": "COINGECKO_API_KEY",
        }
        mock_resp = MagicMock(status_code=429)

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "DOWN")
            self.assertEqual(res["error_code"], "HTTP_429")
            self.assertIn("429 Too Many Requests", res["error_detail"])

    async def test_check_evm_rpc_success(self):
        ep = {
            "id": "EVM_BSC",
            "category": "EVM_RPC",
            "name": "BNB Smart Chain (BSC)",
            "url": "https://bsc-dataseed.binance.org",
            "env_var": "BSC_RPC",
        }
        mock_resp = MagicMock(status_code=200)
        # 0x2710 in hex = 10000 in decimal
        mock_resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": "0x2710"}

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "OK")
            self.assertIn("Block #10,000", res["block_info"])

    async def test_check_evm_rpc_timeout(self):
        ep = {
            "id": "EVM_HYPEREVM",
            "category": "EVM_RPC",
            "name": "HyperEVM",
            "url": "https://rpc.hyperliquid.xyz/evm",
            "env_var": "HYPEREVM_RPC",
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.TimeoutException("Timed out")
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "DOWN")
            self.assertEqual(res["error_code"], "TIMEOUT")
            self.assertIn("Timeout", res["error_detail"])

    async def test_check_evm_rpc_connection_error(self):
        ep = {
            "id": "EVM_ROBINHOOD",
            "category": "EVM_RPC",
            "name": "Robinhood Chain",
            "url": "https://invalid-non-existent-domain.xyz",
            "env_var": "ROBINHOOD_RPC",
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.ConnectError("DNS failure")
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "DOWN")
            self.assertEqual(res["error_code"], "CONNECT_ERROR")

    async def test_check_nonevm_solana_success(self):
        ep = {
            "id": "NONEVM_SOLANA",
            "category": "NONEVM_RPC",
            "network": "SOLANA",
            "name": "Solana Mainnet",
            "url": "https://solana-rpc.publicnode.com",
            "env_var": "SOL_RPC",
        }
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"jsonrpc": "2.0", "result": 284920112, "id": 1}

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "OK")
            self.assertIn("Slot #284,920,112", res["block_info"])

    async def test_check_nonevm_tron_success(self):
        ep = {
            "id": "NONEVM_TRON",
            "category": "NONEVM_RPC",
            "network": "TRON",
            "name": "TronGrid (TRON)",
            "url": "https://api.trongrid.io",
            "env_var": "TRX_RPC",
        }
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "block_header": {"raw_data": {"number": 64239811}}
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "OK")
            self.assertIn("Block #64,239,811", res["block_info"])

    async def test_check_nonevm_ton_success(self):
        ep = {
            "id": "NONEVM_TON",
            "category": "NONEVM_RPC",
            "network": "TON",
            "name": "The Open Network (TON)",
            "url": "https://toncenter.com/api/v2/jsonRPC",
            "env_var": "TON_RPC",
        }
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "ok": True,
            "result": {"last": {"seqno": 41203991}},
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "OK")
            self.assertIn("Seqno #41,203,991", res["block_info"])

    async def test_check_nonevm_sui_success(self):
        ep = {
            "id": "NONEVM_SUI",
            "category": "NONEVM_RPC",
            "network": "SUI",
            "name": "Sui Network",
            "url": "https://sui-rpc.publicnode.com",
            "env_var": "SUI_RPC",
        }
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "result": "59201882",
            "id": 1,
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "OK")
            self.assertIn("Checkpoint #59,201,882", res["block_info"])

    async def test_primary_down_reports_degraded_when_fallback_works(self):
        ep = {
            "id": "EVM_TEST",
            "category": "EVM_RPC",
            "network": "TEST",
            "name": "Test Chain",
            "url": "https://primary.invalid",
            "env_var": "TEST_RPC",
            "fallback_urls": ["https://fallback.invalid"],
        }
        ok_resp = MagicMock(status_code=200)
        ok_resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": "0x10"}
        calls = []

        async def fake_post(url, **kwargs):
            calls.append(str(url))
            if "primary" in str(url):
                raise httpx.ConnectError("primary down")
            return ok_resp

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
            res = await self.monitor.check_single_endpoint(ep)

        self.assertEqual(res["status"], "DEGRADED")
        self.assertEqual(res["error_code"], "PRIMARY_DOWN")
        self.assertIn("fallback.invalid", res["error_detail"])
        self.assertIn("Block #16", res["block_info"])
        self.assertEqual(len(calls), 3)

    async def test_evm_half_broken_endpoint_reports_state_call_failed(self):
        ep = {
            "id": "EVM_KAIA",
            "category": "EVM_RPC",
            "network": "KAIA",
            "name": "Kaia Mainnet",
            "url": "https://half-broken.invalid",
            "env_var": "KAIA_RPC",
        }

        async def fake_post(url, **kwargs):
            method = kwargs.get("json", {}).get("method")
            resp = MagicMock(status_code=200)
            if method == "eth_blockNumber":
                resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": "0x10"}
            else:
                resp.json.return_value = {"jsonrpc": "2.0", "id": 2, "error": {"message": "Temporary"}}
            return resp

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
            res = await self.monitor.check_single_endpoint(ep)

        self.assertEqual(res["status"], "DOWN")
        self.assertEqual(res["error_code"], "STATE_CALL_FAILED")
        self.assertIn("eth_gasPrice", res["error_detail"])

    async def test_check_nonevm_aptos_success(self):
        ep = {
            "id": "NONEVM_APTOS",
            "category": "NONEVM_RPC",
            "network": "APTOS",
            "name": "Aptos Mainnet",
            "url": "https://fullnode.mainnet.aptoslabs.com/v1",
            "env_var": "APTOS_RPC",
        }
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "chain_id": 1,
            "ledger_version": "194820192",
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = await self.monitor.check_single_endpoint(ep)

            self.assertEqual(res["status"], "OK")
            self.assertIn("Ledger v194,820,192", res["block_info"])

    def test_format_alarm_message(self):
        down_res = {
            "id": "EVM_BSC",
            "name": "BNB Smart Chain (BSC)",
            "symbol": "BNB / USDT",
            "url": "https://bad-rpc.binance.org",
            "env_var": "BSC_RPC",
            "status": "DOWN",
            "latency_ms": 45,
            "error_code": "HTTP_404",
            "error_detail": "URL endpoint tidak ditemukan (404 Not Found).",
            "impact": "Transaksi koin BNB / USDT jaringan BSC tidak dapat diproses.",
            "fallback_urls": ["https://bsc-dataseed.bnbchain.org", "https://bsc-rpc.publicnode.com"],
        }

        msg = self.monitor.format_alarm_message(down_res)
        self.assertIn("ALARM DETEKSI DINI", msg)
        self.assertIn("BSC_RPC", msg)
        self.assertIn("https://bad-rpc.binance.org", msg)
        self.assertIn("HTTP_404", msg)
        self.assertIn("Transaksi koin BNB / USDT", msg)
        self.assertIn("https://bsc-dataseed.bnbchain.org", msg)

    def test_format_recovery_message(self):
        up_res = {
            "id": "PRICE_COINGECKO",
            "name": "CoinGecko Price API",
            "symbol": "ALL",
            "url": "https://api.coingecko.com/api/v3/simple/price",
            "status": "OK",
            "latency_ms": 125,
            "block_info": "USDT: Rp 16,250",
        }

        msg = self.monitor.format_recovery_message(up_res)
        self.assertIn("PEMULIHAN", msg)
        self.assertIn("CoinGecko Price API", msg)
        self.assertIn("125 ms", msg)
        self.assertIn("USDT: Rp 16,250", msg)

    def test_format_admin_dashboard(self):
        results = [
            {
                "id": "PRICE_COINGECKO",
                "category": "PRICE_API",
                "name": "CoinGecko Price API",
                "status": "OK",
                "latency_ms": 110,
                "block_info": "USDT: Rp 16,250",
            },
            {
                "id": "EVM_BSC",
                "category": "EVM_RPC",
                "name": "BNB Smart Chain (BSC)",
                "status": "DOWN",
                "latency_ms": 50,
                "error_code": "HTTP_404",
                "error_detail": "URL endpoint tidak ditemukan (404 Not Found).",
            },
            {
                "id": "NONEVM_SOLANA",
                "category": "NONEVM_RPC",
                "name": "Solana Mainnet",
                "status": "OK",
                "latency_ms": 220,
                "block_info": "Slot #280,000,000",
            },
        ]

        dashboard = self.monitor.format_admin_dashboard(results)
        self.assertIn("DIAGNOSTIK KESEHATAN URL & API KOIN", dashboard)
        self.assertIn("Total: 3", dashboard)
        self.assertIn("🟢 2 Normal", dashboard)
        self.assertIn("🔴 1 Mati", dashboard)
        self.assertIn("CoinGecko Price API", dashboard)
        self.assertIn("BNB Smart Chain (BSC)", dashboard)
        self.assertIn("Solana Mainnet", dashboard)

    async def test_alerting_workflow_debounce_and_recovery(self):
        mock_bot = AsyncMock()
        mock_sender = mock_bot

        down_res = {
            "id": "EVM_BSC",
            "category": "EVM_RPC",
            "name": "BNB Smart Chain (BSC)",
            "symbol": "BNB",
            "url": "https://bad-rpc.binance.org",
            "env_var": "BSC_RPC",
            "status": "DOWN",
            "latency_ms": 30,
            "error_code": "HTTP_404",
            "error_detail": "URL endpoint tidak ditemukan (404 Not Found).",
            "impact": "Transaksi BSC gagal.",
            "fallback_urls": [],
        }

        with patch.object(self.monitor, "check_all", new_callable=AsyncMock) as mock_check, \
             patch("services.coin_api_monitor.notify_admins", new_callable=AsyncMock) as mock_notify:

            mock_check.return_value = [down_res]

            # 1. First run -> Should notify admins
            await self.monitor.check_all_and_alert(mock_sender)
            self.assertEqual(mock_notify.call_count, 1)
            alarm_call = mock_notify.call_args[0][1]
            self.assertIn("ALARM DETEKSI DINI", alarm_call)
            mock_notify.reset_mock()

            # 2. Second run immediately -> Endpoint still DOWN, debounce prevents duplicate spam
            await self.monitor.check_all_and_alert(mock_sender)
            self.assertEqual(mock_notify.call_count, 0)

            # 3. Third run -> Endpoint recovers to OK -> Should send recovery notification
            up_res = dict(down_res)
            up_res["status"] = "OK"
            up_res["latency_ms"] = 80
            up_res["block_info"] = "Block #30,000,000"
            mock_check.return_value = [up_res]

            await self.monitor.check_all_and_alert(mock_sender)
            self.assertEqual(mock_notify.call_count, 1)
            recovery_call = mock_notify.call_args[0][1]
            self.assertIn("PEMULIHAN", recovery_call)


if __name__ == "__main__":
    unittest.main()
