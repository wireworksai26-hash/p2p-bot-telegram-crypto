"""Testimoni transaksi Convert ke channel.

Bug: detector memanggil testimoni dengan `bot_app` (Application) -- Application tidak punya
`send_message`, jadi posting Convert gagal diam-diam. Juga link explorer Convert memakai
jaringan asal padahal hash payout milik jaringan tujuan.
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
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

from services import testimony_service as ts

TX_BASE = "0x" + "ab" * 32


def _swap_order(order_id="SWAP-T-1"):
    return SimpleNamespace(
        order_id=order_id, order_type="swap", telegram_id=555, user_username="budi_santoso",
        crypto_symbol="USDT", network="BSC", target_crypto_symbol="ETH", target_network="BASE",
        total_idr=250000, nominal_idr=250000, payout_tx_hash=TX_BASE, tx_hash=None, deposit_tx_hash=None,
    )


class FakeApplication:
    """Mirip telegram.ext.Application: tidak punya send_message, hanya atribut .bot."""

    def __init__(self):
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()
        self.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="TokoKoinID_Bot"))


class TestTestimoniConvert(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ts._scheduled_order_ids.clear()

    async def test_application_dibuka_menjadi_bot_dan_terkirim(self):
        app = FakeApplication()
        ok = await ts.post_transaction_testimony(app, _swap_order(), channel="@TokoKoinID")
        self.assertTrue(ok, "Application harus dibuka ke .bot, bukan gagal diam-diam")
        app.bot.send_message.assert_awaited_once()
        kwargs = app.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], "@TokoKoinID")
        self.assertIn("Swap", kwargs["text"])
        self.assertIn("USDT -> ETH (BASE)", kwargs["text"])

    async def test_jadwal_otomatis_dari_detector_dengan_application(self):
        app = FakeApplication()
        self.assertTrue(ts.schedule_transaction_testimony(app, _swap_order("SWAP-T-2")))
        await asyncio.sleep(0.1)
        app.bot.send_message.assert_awaited_once()

    async def test_bot_biasa_tetap_berfungsi(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(username="TokoKoinID_Bot"))
        self.assertTrue(await ts.post_transaction_testimony(bot, _swap_order(), channel="@TokoKoinID"))
        bot.send_message.assert_awaited_once()

    async def test_link_explorer_convert_memakai_jaringan_tujuan(self):
        dipakai = []

        def fake_explorer(network, tx_hash):
            dipakai.append(network)
            return f"https://explorer.example/{network}/{tx_hash}"

        with patch.object(ts, "get_explorer_url_for_tx", side_effect=fake_explorer):
            ts.format_testimony_message(
                order_type="swap", crypto_symbol="USDT", network="BSC", nominal_idr=250000,
                username="budi_santoso", telegram_id=555, tx_hash=TX_BASE,
                target_symbol="ETH", target_network="BASE")
            ts.format_testimony_message(
                order_type="buy", crypto_symbol="USDT", network="BSC", nominal_idr=250000,
                username="budi_santoso", telegram_id=555, tx_hash=TX_BASE)
        self.assertEqual(dipakai, ["BASE", "BSC"], "Convert -> jaringan tujuan; Beli -> jaringan koin")

    async def test_kegagalan_kirim_tercatat_sebagai_error(self):
        app = FakeApplication()
        app.bot.send_message = AsyncMock(side_effect=RuntimeError("chat not found"))
        with self.assertLogs("services.testimony_service", level="ERROR") as logs:
            ok = await ts.post_transaction_testimony(app, _swap_order("SWAP-T-3"), channel="@TokoKoinID")
        self.assertFalse(ok)
        self.assertIn("chat not found", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
