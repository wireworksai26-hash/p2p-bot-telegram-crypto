"""Regresi auto-scan deposit EVM.

Kasus nyata (BSC, 21 Sep 2026): dataseed menolak eth_getLogs dengan
"limit exceeded", dan blok BSC butuh middleware PoA. Akibatnya deposit
BEP-20 tidak pernah terverifikasi walau dana sudah masuk.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime
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
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "0x" + "1" * 64,
    "ETHERSCAN_API_KEY": "TESTKEY",
})

from services import tx_verifier


class FakeLog:
    def __init__(self, tx_hash: bytes):
        self._hash = tx_hash

    def __getitem__(self, key):
        if key == "transactionHash":
            return self._hash
        raise KeyError(key)


class FakeWeb3:
    def __init__(self, chain_id=56, block_number=1000, logs=None, error=None):
        self._logs = logs or []
        self._error = error
        self.eth = SimpleNamespace(
            chain_id=chain_id, block_number=block_number, get_logs=self._get_logs)

    def _get_logs(self, query):
        if self._error:
            raise self._error
        return self._logs

    def to_checksum_address(self, address):
        return address

    def to_hex(self, value):
        return value.hex() if isinstance(value, bytes) else str(value)


class TestScanRpcRotation(unittest.TestCase):
    def _collect(self, network, symbol, wallet):
        async def run():
            out = []
            async for item in tx_verifier._scan_hashes(
                    network, symbol, wallet, 20, datetime.utcnow()):
                out.append(item)
            return out
        return asyncio.run(run())

    def test_getlogs_ditolak_rpc_pertama_pindah_ke_berikutnya(self):
        sender = SimpleNamespace(
            rpc_list=["https://rpc-satu.invalid", "https://rpc-dua.invalid"],
            config={"chain_id": 56, "native_symbol": "BNB",
                    "tokens": {"USDT": "0x55d398326f99059ff775485246999027b3197955"}},
        )
        hash_bytes = bytes.fromhex("11" * 32)
        primary = FakeWeb3()
        ditolak = FakeWeb3(error=RuntimeError("limit exceeded"))
        berhasil = FakeWeb3(logs=[FakeLog(hash_bytes)])

        with patch.object(tx_verifier.CryptoSenderFactory, "get_sender", return_value=sender), \
                patch.object(tx_verifier, "_scan_web3", side_effect=[primary, ditolak, berhasil]):
            hasil = self._collect("BSC", "USDT", "0x" + "a" * 40)

        self.assertEqual(hasil, ["11" * 32])

    def test_semua_rpc_ditolak_scan_gagal_diam(self):
        sender = SimpleNamespace(
            rpc_list=["https://rpc-satu.invalid"],
            config={"chain_id": 56, "native_symbol": "BNB",
                    "tokens": {"USDT": "0x55d398326f99059ff775485246999027b3197955"}},
        )
        primary = FakeWeb3()
        ditolak = FakeWeb3(error=RuntimeError("limit exceeded"))

        with patch.object(tx_verifier.CryptoSenderFactory, "get_sender", return_value=sender), \
                patch.object(tx_verifier, "_scan_web3", side_effect=[primary, ditolak]):
            with self.assertRaises(RuntimeError):
                self._collect("BSC", "USDT", "0x" + "a" * 40)

    def test_scan_web3_pakai_middleware_poa(self):
        from web3.middleware import ExtraDataToPOAMiddleware

        w3 = tx_verifier._scan_web3("https://rpc.invalid")
        self.assertIn(ExtraDataToPOAMiddleware, w3.middleware_onion)


class TestExplorerIncomingScan(unittest.TestCase):
    def setUp(self):
        tx_verifier._incoming_cache.clear()

    def _sender(self, tokens=None):
        return SimpleNamespace(
            rpc_list=["https://rpc.invalid"],
            config={"chain_id": 56, "native_symbol": "BNB",
                    "tokens": tokens if tokens is not None else {
                        "USDT": "0x55d398326f99059ff775485246999027b3197955"}},
        )

    def test_ambil_hash_dari_riwayat_alamat(self):
        wallet = "0x" + "a" * 40
        rows = [
            {"to": wallet, "timeStamp": "1790000000", "hash": "0x" + "b" * 64},
            {"to": "0x" + "c" * 40, "timeStamp": "1790000000", "hash": "0x" + "d" * 64},
            {"to": wallet, "timeStamp": "100", "hash": "0x" + "e" * 64},
        ]
        with patch.object(tx_verifier.settings, "ETHERSCAN_API_KEY", "TESTKEY"), \
                patch.object(tx_verifier.CryptoSenderFactory, "get_sender", return_value=self._sender()), \
                patch.object(tx_verifier, "_json", new=AsyncMock(return_value={"status": "1", "result": rows})):
            hasil = asyncio.run(tx_verifier._explorer_incoming_hashes("BSC", "USDT", wallet, 1789999000))

        self.assertEqual(hasil, ["0x" + "b" * 64])

    def test_riwayat_kosong_tidak_error(self):
        wallet = "0x" + "a" * 40
        with patch.object(tx_verifier.settings, "ETHERSCAN_API_KEY", "TESTKEY"), \
                patch.object(tx_verifier.CryptoSenderFactory, "get_sender", return_value=self._sender()), \
                patch.object(tx_verifier, "_json", new=AsyncMock(return_value={"status": "0", "result": "No transactions found"})):
            hasil = asyncio.run(tx_verifier._explorer_incoming_hashes("BSC", "USDT", wallet, 0))

        self.assertEqual(hasil, [])

    def test_tanpa_api_key_tidak_memanggil_explorer(self):
        wallet = "0x" + "a" * 40
        with patch.object(tx_verifier.settings, "ETHERSCAN_API_KEY", ""), \
                patch.object(tx_verifier, "_json", new=AsyncMock(side_effect=AssertionError("tidak boleh dipanggil"))):
            hasil = asyncio.run(tx_verifier._explorer_incoming_hashes("BSC", "USDT", wallet, 0))

        self.assertEqual(hasil, [])


if __name__ == "__main__":
    unittest.main()
