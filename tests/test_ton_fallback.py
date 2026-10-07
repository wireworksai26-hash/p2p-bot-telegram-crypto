"""Verifikasi deposit TON: toncenter (utama) + tonapi (cadangan, kuota terpisah).

Cadangan hanya dipakai saat toncenter error/kuota habis atau hasilnya "belum ketemu".
Penolakan pasti (bounce, gagal, penerima salah) tidak boleh dilonggarkan oleh cadangan.
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
    "EVM_PRIVATE_KEY": "",
})

from services import tx_verifier as tv  # noqa: E402

WALLET = "UQCg-y22rJA4RvW__pxDCQopi9l5TBkoBPVpZoBvOfsJQxDt"
WALLET_RAW = "0:a0fb2db6ac903846f5bffe9c43090a298bd9794c192804f56966806f39fb0943"
SENDER_RAW = "0:" + "ab" * 32
TX_HASH = "11" * 32
OK_RESULT = {"verified": True, "amount": 1.5, "timestamp": 1.0, "tx_hash": TX_HASH,
             "from_address": SENDER_RAW, "reason": "OK"}
NOT_FOUND = tv._fail("Transfer TON masuk belum ditemukan.")
BOUNCED = tv._fail("Transaksi TON di-bounce balik ke pengirim.")


def _incoming_tx(**over):
    tx = {
        "hash": TX_HASH, "utime": 1_700_000_000, "success": True, "aborted": False,
        "account": {"address": WALLET_RAW},
        "compute_phase": {"skipped": False, "success": True},
        "in_msg": {"source": {"address": SENDER_RAW}, "destination": {"address": WALLET_RAW},
                   "value": 1_500_000_000, "bounced": False, "hash": "22" * 32},
        "out_msgs": [],
    }
    tx.update(over)
    return tx


class TestTonFallback(unittest.IsolatedAsyncioTestCase):
    async def test_toncenter_error_lalu_tonapi_dipakai(self):
        with patch.object(tv, "_verify_ton_indexer", new=AsyncMock(side_effect=RuntimeError("429"))), \
             patch.object(tv, "_verify_ton_tonapi", new=AsyncMock(return_value=OK_RESULT)) as fb:
            result = await tv._verify_ton("TON", TX_HASH, WALLET)
        self.assertTrue(result["verified"])
        fb.assert_awaited_once()

    async def test_belum_ketemu_di_toncenter_dicoba_di_tonapi(self):
        with patch.object(tv, "_verify_ton_indexer", new=AsyncMock(return_value=NOT_FOUND)), \
             patch.object(tv, "_verify_ton_tonapi", new=AsyncMock(return_value=OK_RESULT)):
            result = await tv._verify_ton("TON", TX_HASH, WALLET)
        self.assertTrue(result["verified"])

    async def test_penolakan_pasti_tidak_dilonggarkan_cadangan(self):
        with patch.object(tv, "_verify_ton_indexer", new=AsyncMock(return_value=BOUNCED)), \
             patch.object(tv, "_verify_ton_tonapi", new=AsyncMock(return_value=OK_RESULT)) as fb:
            result = await tv._verify_ton("TON", TX_HASH, WALLET)
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], BOUNCED["reason"])
        fb.assert_not_awaited()

    async def test_cadangan_error_mengembalikan_hasil_toncenter(self):
        with patch.object(tv, "_verify_ton_indexer", new=AsyncMock(return_value=NOT_FOUND)), \
             patch.object(tv, "_verify_ton_tonapi", new=AsyncMock(side_effect=RuntimeError("down"))):
            result = await tv._verify_ton("TON", TX_HASH, WALLET)
        self.assertEqual(result["reason"], NOT_FOUND["reason"])

    async def test_dua_provider_error_jadi_pending_bukan_ditolak(self):
        with patch.object(tv, "_verify_ton_indexer", new=AsyncMock(side_effect=RuntimeError("429"))), \
             patch.object(tv, "_verify_ton_tonapi", new=AsyncMock(side_effect=RuntimeError("down"))):
            result = await tv.verify_deposit("TON", "TON", TX_HASH, WALLET, 1.5)
        self.assertFalse(result["verified"])
        self.assertIn("belum dapat diverifikasi", result["reason"])


class TestTonapiTrace(unittest.IsolatedAsyncioTestCase):
    async def test_native_masuk_terverifikasi_dari_trace(self):
        trace = {"transaction": {"hash": "99" * 32, "utime": 1, "success": True, "aborted": False,
                                 "account": {"address": SENDER_RAW}, "in_msg": {}, "out_msgs": []},
                 "children": [{"transaction": _incoming_tx(), "children": []}]}
        with patch.object(tv, "_tonapi_get", new=AsyncMock(return_value=trace)):
            result = await tv._verify_ton_tonapi("TON", "99" * 32, WALLET)
        self.assertTrue(result["verified"], result["reason"])
        self.assertEqual(result["amount"], 1.5)
        self.assertEqual(result["tx_hash"], TX_HASH)

    async def test_native_bounced_ditolak(self):
        bad = _incoming_tx(in_msg={"source": {"address": SENDER_RAW}, "destination": {"address": WALLET_RAW},
                                   "value": 1_500_000_000, "bounced": True})
        with patch.object(tv, "_tonapi_get", new=AsyncMock(return_value={"transaction": bad, "children": []})):
            result = await tv._verify_ton_tonapi("TON", TX_HASH, WALLET)
        self.assertFalse(result["verified"])

    async def test_native_gagal_compute_ditolak(self):
        bad = _incoming_tx(compute_phase={"skipped": False, "success": False}, aborted=True)
        with patch.object(tv, "_tonapi_get", new=AsyncMock(return_value={"transaction": bad, "children": []})):
            result = await tv._verify_ton_tonapi("TON", TX_HASH, WALLET)
        self.assertFalse(result["verified"])

    async def test_penerima_bukan_wallet_kita_ditolak(self):
        other = "0:" + "cd" * 32
        bad = _incoming_tx(account={"address": other},
                           in_msg={"source": {"address": SENDER_RAW}, "destination": {"address": other},
                                   "value": 1_500_000_000, "bounced": False})
        with patch.object(tv, "_tonapi_get", new=AsyncMock(return_value={"transaction": bad, "children": []})):
            result = await tv._verify_ton_tonapi("TON", TX_HASH, WALLET)
        self.assertFalse(result["verified"])

    async def test_hash_tidak_ada_di_tonapi(self):
        with patch.object(tv, "_tonapi_get", new=AsyncMock(side_effect=FileNotFoundError("404"))):
            result = await tv._verify_ton_tonapi("TON", TX_HASH, WALLET)
        self.assertFalse(result["verified"])
        self.assertTrue(result["reason"].startswith(tv._TON_RETRY_REASONS))

    async def test_trace_emulated_ditolak(self):
        with patch.object(tv, "_tonapi_get",
                          new=AsyncMock(return_value={"transaction": _incoming_tx(), "children": [], "emulated": True})):
            result = await tv._verify_ton_tonapi("TON", TX_HASH, WALLET)
        self.assertFalse(result["verified"])


if __name__ == "__main__":
    unittest.main()
