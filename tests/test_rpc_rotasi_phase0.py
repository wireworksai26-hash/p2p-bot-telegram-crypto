"""Regression tests Phase 0: RPC mati/paruh-rusak tidak boleh dipakai lagi.

1. Daftar RPC EVM bersih dari endpoint mati (cloudflare-eth, polygon-rpc, meowrpc, klaytn.drpc).
2. Token USDT AVAX/GRAVITY palsu sudah dihapus.
3. Rotasi RPC Solana memakai endpoint berikutnya saat endpoint pertama gagal.
"""
import asyncio
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
    "EVM_PRIVATE_KEY": "0x" + "1" * 64,
    "SOLANA_PRIVATE_KEY": "",
})

import httpx

from services.crypto_sender.evm_sender import EVMSender
from services.crypto_sender.solana_sender import SolanaSender

DEAD_HOSTS = ("cloudflare-eth.com", "polygon-rpc.com", "meowrpc", "klaytn.drpc.org")


class TestRpcListsPhase0(unittest.TestCase):
    def test_daftar_rpc_bebas_endpoint_mati(self):
        for chain, config in EVMSender.EVM_CHAINS.items():
            for rpc in config["rpc_list"]:
                for dead in DEAD_HOSTS:
                    self.assertNotIn(dead, rpc, f"{chain} masih memakai {dead}")

    def test_kaia_rpc_utama_bukan_drpc(self):
        kaia = EVMSender.EVM_CHAINS["KAIA"]["rpc_list"]
        self.assertIn("https://public-en.node.kaia.io", kaia)
        self.assertNotIn("https://klaytn.drpc.org", kaia)

    def test_robinhood_publicnode_didahulukan(self):
        robinhood = EVMSender.EVM_CHAINS["ROBINHOOD"]["rpc_list"]
        self.assertLess(
            robinhood.index("https://robinhood-rpc.publicnode.com"),
            robinhood.index("https://rpc.mainnet.chain.robinhood.com"),
        )

    def test_token_usdt_avax_gravity_dihapus(self):
        self.assertEqual(EVMSender.EVM_CHAINS["AVAX"]["tokens"], {})
        self.assertEqual(EVMSender.EVM_CHAINS["GRAVITY"]["tokens"], {})


class TestSolanaRpcRotation(unittest.TestCase):
    def test_endpoint_pertama_gagal_pindah_ke_kedua(self):
        sender = SolanaSender.__new__(SolanaSender)
        sender.rpc_list = ["https://rpc-satu.invalid", "https://rpc-dua.invalid"]
        sender.private_key = ""
        calls = []

        ok_response = httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": 12345},
            request=httpx.Request("POST", "https://rpc-dua.invalid"),
        )

        async def fake_post(url, **kwargs):
            calls.append(str(url))
            if len(calls) == 1:
                raise httpx.ConnectError("endpoint pertama mati")
            return ok_response

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)):
            result = asyncio.run(sender._rpc("getSlot", []))

        self.assertEqual(result, 12345)
        self.assertEqual(calls, ["https://rpc-satu.invalid", "https://rpc-dua.invalid"])


if __name__ == "__main__":
    unittest.main()
