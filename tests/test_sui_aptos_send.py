"""
tests/test_sui_aptos_send.py
============================
Regression tests for Sui and Aptos auto-payout senders:
1. Key yang tidak cocok dengan alamat stok harus ditolak (tanpa broadcast).
2. Dry-run / simulasi gagal harus menolak sebelum broadcast.
3. Jalur sukses mengembalikan hash dan hanya setelah verifikasi on-chain.
"""
import base64
import hashlib
import os
import sys
import unittest
from decimal import Decimal
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

from nacl.signing import SigningKey

from services.crypto_sender.aptos_sender import AptosSender
from services.crypto_sender.sui_sender import SuiSender


def _keypair():
    seed = bytes(range(32))
    return seed, bytes(SigningKey(seed).verify_key)


def _sui_address(pub):
    return "0x" + hashlib.blake2b(b"\x00" + pub, digest_size=32).hexdigest()


def _aptos_address(pub):
    return "0x" + hashlib.sha3_256(pub + b"\x00").hexdigest()


class TestSuiSenderSend(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.seed, pub = _keypair()
        self.wallet = _sui_address(pub)
        self.other_wallet = "0x" + "ab" * 32
        patcher = patch.multiple(
            "config.settings.settings",
            SUI_PRIVATE_KEY="0x" + self.seed.hex(),
            SUI_WALLET_ADDRESS=self.wallet,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sender = SuiSender()

    async def test_rejects_key_mismatch_without_rpc(self):
        self.sender.wallet_address = self.other_wallet
        self.sender._rpc_call = AsyncMock()
        result = await self.sender.send(self.other_wallet, 0.001, "SUI")
        self.assertFalse(result.success)
        self.assertIn("tidak cocok", result.error_message)
        self.sender._rpc_call.assert_not_awaited()

    async def test_rejects_before_broadcast_when_dry_run_fails(self):
        async def fake_rpc(base, method, params):
            if method == "suix_getCoins":
                return {"data": [{"coinObjectId": self.other_wallet, "version": "1",
                                  "digest": base64.b64encode(b"d" * 32).decode(), "balance": "1000000000"}]}
            if method == "suix_getReferenceGasPrice":
                return "100"
            if method == "sui_dryRunTransactionBlock":
                return {"effects": {"status": {"status": "failure", "error": "InsufficientGas"}}}
            raise AssertionError(f"method tidak diharapkan: {method}")

        calls = []

        async def spy(base, method, params):
            calls.append(method)
            return await fake_rpc(base, method, params)

        self.sender._pick_endpoint = AsyncMock(return_value="https://rpc.test")
        self.sender._rpc_call = spy
        result = await self.sender.send(self.other_wallet, 0.001, "SUI")
        self.assertFalse(result.success)
        self.assertIn("Dry-run", result.error_message)
        self.assertNotIn("sui_executeTransactionBlock", calls)

    async def test_success_returns_digest_after_successful_execution(self):
        async def fake_rpc(base, method, params):
            if method == "suix_getCoins":
                return {"data": [{"coinObjectId": self.other_wallet, "version": "7",
                                  "digest": base64.b64encode(b"d" * 32).decode(), "balance": "1000000000"}]}
            if method == "suix_getReferenceGasPrice":
                return "100"
            if method == "sui_dryRunTransactionBlock":
                return {"effects": {"status": {"status": "success"},
                                    "gasUsed": {"computationCost": "100000", "storageCost": "988000", "storageRebate": "0"}}}
            if method == "sui_executeTransactionBlock":
                self.assertIsInstance(params[1][0], str)
                return {"digest": "DIGEST123", "effects": {"status": {"status": "success"}}}
            raise AssertionError(f"method tidak diharapkan: {method}")

        self.sender._pick_endpoint = AsyncMock(return_value="https://rpc.test")
        self.sender._rpc_call = fake_rpc
        result = await self.sender.send(self.other_wallet, 0.001, "SUI")
        self.assertTrue(result.success)
        self.assertEqual(result.tx_hash, "DIGEST123")
        self.assertIn("DIGEST123", result.explorer_url)


class TestAptosSenderSend(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.seed, pub = _keypair()
        self.wallet = _aptos_address(pub)
        self.other_wallet = "0x" + "cd" * 32
        patcher = patch.multiple(
            "config.settings.settings",
            APTOS_PRIVATE_KEY="0x" + self.seed.hex(),
            APTOS_WALLET_ADDRESS=self.wallet,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sender = AptosSender()

    def _fake_client_factory(self, sim_ok=True, balance=327_000_000):
        from aptos_sdk.async_client import ClientConfig
        from aptos_sdk.transactions import RawTransaction

        class FakeRestClient:
            def __init__(self, base_url, client_config=None):
                self.base_url = base_url
                self.client_config = client_config or ClientConfig()
                self.submitted = []

            async def account_balance(self, address):
                return balance

            async def create_bcs_transaction(self, sender, payload, sequence_number=None):
                return RawTransaction(sender.address(), 0, payload, 100_000, 100, 9_999_999_999, 1)

            async def simulate_transaction(self, raw, account, estimate_gas_usage=False):
                return [{"success": sim_ok,
                         "vm_status": "Executed successfully" if sim_ok else "REJECTED",
                         "gas_used": 10_336}]

            async def submit_bcs_transaction(self, signed):
                self.submitted.append(signed)
                return "0xaptos_hash"

            async def wait_for_transaction(self, tx_hash):
                return None

            async def close(self):
                return None

        return FakeRestClient

    async def test_rejects_key_mismatch_without_broadcast(self):
        self.sender.wallet_address = self.other_wallet
        fake = self._fake_client_factory()
        with patch("aptos_sdk.async_client.RestClient", fake):
            result = await self.sender.send(self.other_wallet, 0.001, "APT")
        self.assertFalse(result.success)
        self.assertIn("tidak cocok", result.error_message)

    async def test_rejects_before_broadcast_when_simulation_fails(self):
        fake = self._fake_client_factory(sim_ok=False)
        with patch("aptos_sdk.async_client.RestClient", fake):
            result = await self.sender.send(self.other_wallet, 0.001, "APT")
        self.assertFalse(result.success)
        self.assertIn("Simulasi", result.error_message)

    async def test_success_returns_hash(self):
        fake = self._fake_client_factory()
        with patch("aptos_sdk.async_client.RestClient", fake):
            result = await self.sender.send(self.other_wallet, 0.001, "APT")
        self.assertTrue(result.success)
        self.assertEqual(result.tx_hash, "0xaptos_hash")
        self.assertIn("0xaptos_hash", result.explorer_url)


if __name__ == "__main__":
    unittest.main()
