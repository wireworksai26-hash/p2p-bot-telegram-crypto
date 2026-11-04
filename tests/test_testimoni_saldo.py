"""Testimoni channel: metode bayar pada Beli, tips Saldo Bot, topup saldo, dan ticker GRAM untuk TON."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1", "DATABASE_URL": "sqlite:///:memory:", "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "1", "EVM_WALLET_ADDRESS": "0x" + "1" * 40, "EVM_PRIVATE_KEY": "",
})

from services import testimony_service as ts  # noqa: E402

HASH = "0x" + "ab" * 32


def beli(payment, symbol="TON", network="TON"):
    return ts.format_testimony_message("buy", symbol, network, 105_339, "xk_user12", 1, HASH,
                                       "TokoKoinID_bot", payment_method=payment)


class TestBeli(unittest.TestCase):
    def test_saldo_bot_ada_pembayaran_dan_tips(self):
        text = beli("BOT_BALANCE")
        self.assertIn("- Jenis Coin : GRAM TON", text)
        self.assertIn("- Pembayaran : Saldo Bot", text)
        self.assertTrue(text.endswith("*Tips : Top up sekali, gunakan saldo untuk pembelian berikutnya"))
        self.assertLess(text.index("- Pembayaran"), text.index("- Bot order"))

    def test_qris_ada_pembayaran_tanpa_tips(self):
        text = beli("GOPAY_QRIS")
        self.assertIn("- Pembayaran : Qris", text)
        self.assertNotIn("Tips", text)
        self.assertTrue(text.endswith("- Bot order : @TokoKoinID_bot"))

    def test_tanpa_metode_bayar_tidak_ada_baris_pembayaran(self):
        self.assertNotIn("Pembayaran", beli(None))

    def test_jual_dan_convert_tetap_sama_tanpa_pembayaran(self):
        jual = ts.format_testimony_message("sell", "USDT", "BSC", 50_000, "xk_user12", 1, HASH, "TokoKoinID_bot",
                                           payment_method="BOT_BALANCE")
        self.assertNotIn("Pembayaran", jual)
        self.assertNotIn("Tips", jual)
        self.assertIn("- Jenis Coin : USDT BSC", jual)
        swap = ts.format_testimony_message("swap", "TON", "TON", 50_000, "xk_user12", 1, HASH, "TokoKoinID_bot",
                                           target_symbol="USDT", target_network="BSC")
        self.assertIn("GRAM (TON) -> USDT (BSC)", swap)
        self.assertNotIn("Pembayaran", swap)

    def test_usdt_di_jaringan_ton_tetap_usdt(self):
        self.assertIn("- Jenis Coin : USDT TON", beli("GOPAY_QRIS", symbol="USDT"))


class TestTopup(unittest.IsolatedAsyncioTestCase):
    def test_format_topup(self):
        text = ts.format_topup_testimony("xk_user12", 1, 102_212, "TokoKoinID_bot")
        self.assertEqual(text.splitlines(), [
            "\u2705 <b>Top Up Saldo Berhasil</b>",
            "- Jenis Transaksi : Top Up Saldo Bot",
            "- Nominal Masuk : Rp 102.212",
            "- Pengguna : @xk****12",
            "- Pembayaran : Qris",
            "- Bot order : @TokoKoinID_bot",
            "*Saldo top up digunakan untuk membeli Coin di bot",
        ])

    async def test_post_topup_ke_channel_tanpa_melempar(self):
        bot = SimpleNamespace(send_message=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(username="TokoKoinID_bot")))
        topup = SimpleNamespace(topup_id="TOPUP-1", telegram_id=7)
        ok = await ts.post_topup_testimony(bot, topup, 50_000, db=None, channel="@uji")
        self.assertTrue(ok)
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], "@uji")
        self.assertIn("Rp 50.000", kwargs["text"])
        bot.send_message = AsyncMock(side_effect=RuntimeError("channel salah"))
        self.assertFalse(await ts.post_topup_testimony(bot, topup, 50_000, channel="@uji"))

    async def test_topup_user_dipublikasikan_topup_kas_bot_tidak(self):
        from unittest.mock import patch
        from bot.utils import saldo_log
        topup = SimpleNamespace(topup_id="T", telegram_id=7, amount_idr=1, mdr_idr=0)
        post = AsyncMock()
        with patch.object(saldo_log, "kirim_ke_topik", new=AsyncMock()), \
             patch("services.testimony_service.post_topup_testimony", new=post):
            await saldo_log.log_topup_success(AsyncMock(), topup, 1, 1, is_treasury=False)
            self.assertEqual(post.await_count, 1)
            await saldo_log.log_topup_success(AsyncMock(), topup, 1, 1, is_treasury=True)
            self.assertEqual(post.await_count, 1)


class TestPaymentMethodTerbawa(unittest.TestCase):
    def test_snapshot_membawa_payment_method(self):
        self.assertIn("payment_method", ts._TESTIMONY_FIELDS)
        order = SimpleNamespace(**{f: ("BOT_BALANCE" if f == "payment_method" else None) for f in ts._TESTIMONY_FIELDS})
        self.assertEqual(ts._snapshot_order(order).payment_method, "BOT_BALANCE")


if __name__ == "__main__":
    unittest.main()
