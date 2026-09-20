"""
tests/test_ton_w5_send.py
=========================
Regression tests untuk pengiriman GRAM (TON) lewat wallet v5r1 (W5):
1. Deteksi versi wallet dari privkey harus cocok dengan TON_WALLET_ADDRESS.
2. External message yang dibangun harus benar: tujuan wallet, opcode W5,
   wallet_id, seqno, aksi transfer ke penerima, dan tanda tangan ed25519 valid.
3. Broadcast yang ditolak / tidak pasti tidak boleh dilaporkan sebagai sukses.
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

from nacl.signing import VerifyKey
from pytoniq_core.boc import Builder, Cell as PyCell
from pytoniq_core.boc.address import Address
from pytoniq_core.tlb.transaction import MessageAny

from services.crypto_sender import SendResult
from services.crypto_sender.ton_sender import (
    TonBroadcastRejected,
    TonBroadcastUncertain,
    TonSender,
)
from services.tx_verifier import ton_address

# Kunci uji (bukan milik siapa pun): seed = 00..1f
TEST_SEED_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
TEST_W5_ADDRESS = "EQDygBQZqqGzcEoNULWIjcy1UR7W4xkbmdFEoE6p6jkMUpdb"
TEST_WALLET_ID = 2147483409
RECIPIENT = Address((0, bytes(32))).to_str(is_user_friendly=True, is_bounceable=False)


class TestTonW5Send(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.multiple(
            "config.settings.settings",
            TON_PRIVATE_KEY=TEST_SEED_HEX,
            TON_WALLET_ADDRESS=TEST_W5_ADDRESS,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sender = TonSender()

    def test_resolve_wallet_detects_v5r1(self):
        wallet = self.sender._resolve_wallet()
        self.assertEqual(wallet["version"], "v5r1")
        self.assertEqual(wallet["wallet_id"], TEST_WALLET_ID)
        self.assertEqual(ton_address(wallet["address"]), ton_address(TEST_W5_ADDRESS))
        self.assertIsNotNone(wallet["state_init"])

    async def test_external_message_is_signed_and_targets_recipient(self):
        wallet = self.sender._resolve_wallet()
        self.sender.get_balance = AsyncMock(return_value=10.0)
        self.sender._get_seqno = AsyncMock(return_value=1)
        self.sender._account_status = AsyncMock(return_value="uninit")
        captured = {}

        async def fake_broadcast(boc_bytes):
            captured["boc"] = boc_bytes
            return "tonapi"

        async def fake_delivery(recipient, amount, symbol, since_ts, reference, jetton_wallet=""):
            captured["recipient"] = recipient
            captured["amount"] = amount
            captured["reference"] = reference
            return SendResult(True, "test-tx-hash")

        self.sender._broadcast = fake_broadcast
        self.sender._await_delivery = fake_delivery

        result = await self.sender.send(RECIPIENT, 0.5, "TON")
        self.assertTrue(result.success)
        self.assertEqual(result.tx_hash, "test-tx-hash")
        self.assertEqual(ton_address(captured["recipient"]), ton_address(RECIPIENT))
        self.assertEqual(captured["amount"], 0.5)
        self.assertTrue(captured["reference"].startswith("msg:"))

        message = MessageAny.deserialize(PyCell.one_from_boc(captured["boc"]).begin_parse())
        self.assertEqual(
            ton_address(message.info.dest.to_str(is_user_friendly=True, is_bounceable=True)),
            ton_address(TEST_W5_ADDRESS),
        )
        # Wallet belum aktif -> external message harus membawa StateInit (deploy).
        self.assertIsNotNone(message.init)

        # Body W5 dari pytoniq: [op, wallet_id, valid_until, seqno] + actions inline + 512-bit signature.
        body = message.body
        body_slice = body.begin_parse()
        op = body_slice.load_uint(32)
        wallet_id = body_slice.load_uint(32)
        valid_until = body_slice.load_uint(32)
        seqno = body_slice.load_uint(32)
        self.assertEqual(op, 0x7369676E)
        self.assertEqual(wallet_id, TEST_WALLET_ID)
        self.assertEqual(seqno, 1)

        actions_bit_a = body_slice.load_bit()
        chain_ref = body_slice.load_ref()
        actions_bit_b = body_slice.load_bit()
        signature = body_slice.load_bytes(64)

        actions_cell = (
            Builder().store_bit(actions_bit_a).store_ref(chain_ref).store_bit(actions_bit_b).end_cell()
        )
        signing_cell = (
            Builder()
            .store_uint(op, 32)
            .store_uint(wallet_id, 32)
            .store_uint(valid_until, 32)
            .store_uint(seqno, 32)
            .store_cell(actions_cell)
            .end_cell()
        )
        VerifyKey(wallet["pub"]).verify(signing_cell.hash, signature)

        chain = chain_ref.begin_parse()
        self.assertEqual(chain.load_uint(32), 0x0EC3C86D)
        self.assertEqual(chain.load_uint(8), 3)
        # refs[0] = rantai sebelumnya, refs[1] = pesan internal yang dikirim
        transfer = MessageAny.deserialize(chain_ref.refs[1].begin_parse())
        self.assertEqual(
            ton_address(transfer.info.dest.to_str(is_user_friendly=True, is_bounceable=True)),
            ton_address(RECIPIENT),
        )
        self.assertEqual(transfer.info.value.grams, int(0.5 * 10**9))

    async def test_rejected_broadcast_is_not_reported_as_success(self):
        self.sender.get_balance = AsyncMock(return_value=10.0)
        self.sender._get_seqno = AsyncMock(return_value=1)
        self.sender._account_status = AsyncMock(return_value="active")
        self.sender._broadcast = AsyncMock(
            side_effect=TonBroadcastRejected("Broadcast TON ditolak. tonapi HTTP 406")
        )
        result = await self.sender.send(RECIPIENT, 0.5, "TON")
        self.assertFalse(result.success)
        self.assertEqual(result.tx_hash, "")
        self.assertTrue(result.error_message.startswith("MANUAL_REVIEW:"))

    async def test_uncertain_broadcast_returns_reference_not_success(self):
        self.sender.get_balance = AsyncMock(return_value=10.0)
        self.sender._get_seqno = AsyncMock(return_value=1)
        self.sender._account_status = AsyncMock(return_value="active")
        self.sender._broadcast = AsyncMock(
            side_effect=TonBroadcastUncertain("status broadcast belum pasti")
        )
        result = await self.sender.send(RECIPIENT, 0.5, "TON")
        self.assertFalse(result.success)
        self.assertTrue(result.tx_hash.startswith("msg:"))
        self.assertTrue(result.error_message.startswith("MANUAL_REVIEW:"))


if __name__ == "__main__":
    unittest.main()
