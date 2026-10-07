"""WR-04 (review Core): payout TON tidak boleh dilaporkan sukses karena transfer milik orang lain.

Dulu:
- Native: tx keluar lama (<= 60 dtk) ke alamat & nominal sama dipakai ulang untuk order berikutnya
  (dua order bernominal sama ke alamat sama -> order B "terverifikasi" dengan hash order A).
- Pesan yang di-bounce (dana kembali) tetap dihitung sukses karena pesan keluarnya ada.
- USDT: saldo jetton penerima naik oleh transfer pihak lain (mis. deposit exchange) dianggap
  bukti pengiriman kita, walau tidak ada tx keluar kita ke jetton wallet.
"""
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from services.crypto_sender import ton_sender  # noqa: E402
from services.crypto_sender.ton_sender import TonSender  # noqa: E402

RECIPIENT = "0:" + "ab" * 32
JETTON_WALLET = "0:" + "cd" * 32
NANO = 10**9


def out_tx(tx_hash, dest, value, utime):
    return {"success": True, "hash": tx_hash, "utime": utime,
            "out_msgs": [{"destination": {"address": dest}, "value": value}], "in_msg": {}}


def bounce_tx(tx_hash, source, value, utime):
    return {"success": True, "hash": tx_hash, "utime": utime, "out_msgs": [],
            "in_msg": {"bounced": True, "source": {"address": source}, "value": value}}


class TonDelivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sender = TonSender()
        self.sender.wallet_address = "0:" + "11" * 32
        TonSender._claimed_hashes.clear()
        self.sleep = patch.object(ton_sender.asyncio, "sleep", new=AsyncMock())
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        TonSender._claimed_hashes.clear()

    def _chain(self, txs):
        self.sender._tonapi_get = AsyncMock(return_value={"transactions": txs})

    async def test_native_sukses_dengan_tx_baru(self):
        now = int(time.time())
        self._chain([out_tx("H1", RECIPIENT, 5 * NANO, now + 2)])
        res = await self.sender._await_delivery(RECIPIENT, 5, "TON", now, "ref")
        self.assertTrue(res.success)
        self.assertEqual(res.tx_hash, "H1")

    async def test_native_hash_tidak_dipakai_dua_order(self):
        now = int(time.time())
        self._chain([out_tx("H1", RECIPIENT, 5 * NANO, now + 2)])
        first = await self.sender._await_delivery(RECIPIENT, 5, "TON", now, "ref-a")
        second = await self.sender._await_delivery(RECIPIENT, 5, "TON", now + 5, "ref-b")
        self.assertTrue(first.success)
        self.assertFalse(second.success, "order kedua tidak boleh memakai hash order pertama")

    async def test_native_tx_lama_sebelum_broadcast_diabaikan(self):
        now = int(time.time())
        self._chain([out_tx("OLD", RECIPIENT, 5 * NANO, now - 45)])
        res = await self.sender._await_delivery(RECIPIENT, 5, "TON", now, "ref")
        self.assertFalse(res.success)

    async def test_native_bounce_bukan_sukses(self):
        now = int(time.time())
        self._chain([bounce_tx("B1", RECIPIENT, 5 * NANO - 1000, now + 6),
                     out_tx("H1", RECIPIENT, 5 * NANO, now + 2)])
        res = await self.sender._await_delivery(RECIPIENT, 5, "TON", now, "ref")
        self.assertFalse(res.success)
        self.assertIn("bounce", res.error_message.lower())

    async def test_usdt_saldo_naik_karena_pihak_lain_bukan_sukses(self):
        now = int(time.time())
        self._chain([])  # tidak ada tx keluar kita ke jetton wallet
        self.sender._jetton_balance_of = AsyncMock(side_effect=[100.0, 100.0, 125.0, 125.0, 125.0])
        res = await self.sender._await_delivery(RECIPIENT, 20, "USDT", now, "ref", jetton_wallet=JETTON_WALLET)
        self.assertFalse(res.success)

    async def test_usdt_sukses_butuh_saldo_naik_dan_tx_keluar_kita(self):
        now = int(time.time())
        self._chain([out_tx("J1", JETTON_WALLET, 50_000_000, now + 3)])
        self.sender._jetton_balance_of = AsyncMock(side_effect=[100.0, 120.0, 120.0])
        res = await self.sender._await_delivery(RECIPIENT, 20, "USDT", now, "ref", jetton_wallet=JETTON_WALLET)
        self.assertTrue(res.success)
        self.assertEqual(res.tx_hash, "J1")


if __name__ == "__main__":
    unittest.main()
