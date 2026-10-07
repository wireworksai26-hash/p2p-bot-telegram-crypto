"""Pilihan cara input jumlah di Beli, Jual dan Convert: tombol Koin dan tombol Rupiah terpisah.

- mode_row: dua tombol terpisah, mode aktif bertanda centang.
- Beli: mode koin menghitung terbalik nominal Rupiah (termasuk fee) terkecil yang cukup;
  mode Rupiah tetap seperti semula.
- Jual: layar pilih mode setelah memilih jaringan.
- Convert: parser mengikuti mode yang dipilih (angka polos tidak ditebak), $ tetap dikenali.
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
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
    "EVM_PRIVATE_KEY": "",
})

from database.connection import Base, engine, SessionLocal
from database.models import User, WalletBalance
from bot.utils.amount_mode import mode_row, mode_from_callback
from bot.handlers import buy as buy_mod
from bot.handlers import sell as sell_mod
from bot.handlers import swap as swap_mod
from bot.handlers.buy import handle_amount_input as buy_amount, INPUT_AMOUNT as BUY_INPUT_AMOUNT, INPUT_WALLET
from services.fee_service import calculate_fee_idr, get_fee_category

BUY_PRICE = 16000


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class TestModeRow(unittest.TestCase):
    def test_dua_tombol_terpisah(self):
        row = mode_row("buy")
        self.assertEqual([b.callback_data for b in row], ["buy_mode_coin", "buy_mode_idr"])
        self.assertTrue(row[0].text.endswith("Jumlah Koin"))
        self.assertTrue(row[1].text.endswith("Nominal Rupiah"))

    def test_mode_aktif_diberi_centang(self):
        self.assertTrue(mode_row("sell", "COIN")[0].text.startswith("✅"))
        self.assertFalse(mode_row("sell", "COIN")[1].text.startswith("✅"))
        self.assertTrue(mode_row("sell", "IDR")[1].text.startswith("✅"))

    def test_mode_from_callback(self):
        self.assertEqual(mode_from_callback("swap_mode_idr"), "IDR")
        self.assertEqual(mode_from_callback("swap_mode_coin"), "COIN")

    def test_layar_awal_semua_flow_menampilkan_dua_tombol(self):
        screens = {
            "buy": buy_mod._amount_prompt("USDT", "BSC", None),
            "sell": sell_mod._amount_prompt("USDT", "BSC", None),
            "swap": swap_mod._amount_prompt("USDT", "BSC", "SOL", "SOLANA", "", None),
        }
        for prefix, (text, markup) in screens.items():
            with self.subTest(flow=prefix):
                cbs = _callbacks(markup)
                self.assertIn(f"{prefix}_mode_coin", cbs)
                self.assertIn(f"{prefix}_mode_idr", cbs)
                self.assertIn("Pilih cara memasukkan jumlah", text)


class BuyCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=42, username="u", full_name="U"))
        self.db.add(WalletBalance(
            network="BSC", symbol="USDT", address="0x" + "1" * 40, balance=Decimal("100000"),
            reserved_balance=Decimal("0"), sync_status="OK",
            last_success_at=datetime.utcnow(), last_checked_at=datetime.utcnow()))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def _kirim(self, text, mode):
        message = AsyncMock()
        message.text = text
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=42))
        context = SimpleNamespace(user_data={"buy_symbol": "USDT", "buy_network": "BSC", "buy_input_mode": mode})
        price = {"symbol": "USDT", "buy_price_idr": BUY_PRICE, "sell_price_idr": BUY_PRICE - 200,
                 "source": "MOCK", "price_updated_at": int(datetime.now(timezone.utc).timestamp()),
                 "spread_pct": 0.0}
        with patch("services.price_service.PriceService.get_price", new=AsyncMock(return_value=price)), \
             patch("bot.handlers.buy.SessionLocal", side_effect=lambda: SessionLocal()):
            state = await buy_amount(update, context)
        return state, context, message


class TestBuyKoin(BuyCase):
    def _fee(self, nominal):
        return calculate_fee_idr(nominal, category=get_fee_category("USDT"), symbol="USDT", network="BSC")

    async def test_jumlah_koin_dihitung_balik_ke_nominal_terkecil(self):
        state, ctx, _ = await self._kirim("10", "COIN")
        self.assertEqual(state, INPUT_WALLET)
        nominal = ctx.user_data["buy_nominal_idr"]
        target = 10 * BUY_PRICE
        self.assertEqual(ctx.user_data["buy_crypto_amount"], 10)
        self.assertGreaterEqual(nominal - self._fee(nominal), target, "nilai koin diterima harus cukup")
        self.assertLess((nominal - 1) - self._fee(nominal - 1), target, "nominal harus yang terkecil")
        self.assertEqual(ctx.user_data["buy_total_idr"], nominal)

    async def test_koma_desimal_diterima(self):
        state, ctx, _ = await self._kirim("1,5", "COIN")
        self.assertEqual(state, INPUT_WALLET)
        self.assertEqual(ctx.user_data["buy_crypto_amount"], 1.5)

    async def test_koin_terlalu_kecil_ditolak_dengan_info_minimal(self):
        state, _, message = await self._kirim("0.1", "COIN")
        self.assertEqual(state, BUY_INPUT_AMOUNT)
        teks = message.reply_text.call_args.kwargs["text"]
        self.assertIn("Terlalu Kecil", teks)
        self.assertIn("Rp 5.000", teks)

    async def test_koin_terlalu_besar_ditolak(self):
        state, _, message = await self._kirim("1000", "COIN")  # 16 juta
        self.assertEqual(state, BUY_INPUT_AMOUNT)
        self.assertIn("Terlalu Besar", message.reply_text.call_args.kwargs["text"])

    async def test_teks_bukan_angka_ditolak(self):
        state, _, message = await self._kirim("sepuluh", "COIN")
        self.assertEqual(state, BUY_INPUT_AMOUNT)
        self.assertIn("Jumlah Koin Tidak Valid", message.reply_text.call_args.kwargs["text"])

    async def test_mode_rupiah_tetap_seperti_semula(self):
        for mode in ("IDR", None):
            with self.subTest(mode=mode):
                state, ctx, _ = await self._kirim("Rp 50.000", mode)
                self.assertEqual(state, INPUT_WALLET)
                self.assertEqual(ctx.user_data["buy_nominal_idr"], 50000)
                fee = ctx.user_data["buy_fee_idr"]
                self.assertAlmostEqual(ctx.user_data["buy_crypto_amount"], (50000 - fee) / BUY_PRICE, places=6)

    async def test_mode_rupiah_menolak_koin_desimal(self):
        state, _, message = await self._kirim("0.5", "IDR")
        self.assertEqual(state, BUY_INPUT_AMOUNT)
        self.assertIn("Nominal Tidak Valid", message.reply_text.call_args.kwargs["text"])

    async def test_toggle_mode_beli(self):
        for data, expected, label in (("buy_mode_idr", "IDR", "Mode Nominal Rupiah"),
                                      ("buy_mode_coin", "COIN", "Mode Jumlah Koin")):
            query = AsyncMock()
            query.data = data
            context = SimpleNamespace(user_data={"buy_symbol": "USDT", "buy_network": "BSC"})
            with patch.object(buy_mod, "safe_edit_message", new=AsyncMock()) as edit:
                state = await buy_mod.handle_input_mode(SimpleNamespace(callback_query=query), context)
            self.assertEqual(state, BUY_INPUT_AMOUNT)
            self.assertEqual(context.user_data["buy_input_mode"], expected)
            self.assertIn(label, edit.await_args.kwargs["text"])


class TestConvertParser(unittest.TestCase):
    PRICE = 2_000_000.0  # kurs koin non-USD
    USDT = 16000.0

    def test_mode_koin_angka_polos_bukan_rupiah(self):
        amount, nominal, mode = swap_mod.parse_convert_amount("5000", self.PRICE, self.USDT, "SOL", force="COIN")
        self.assertEqual((amount, mode), (5000.0, "CRYPTO"))
        self.assertEqual(nominal, int(5000 * self.PRICE))

    def test_mode_koin_stablecoin(self):
        amount, nominal, mode = swap_mod.parse_convert_amount("1500", self.USDT, self.USDT, "USDT", force="COIN")
        self.assertEqual((amount, nominal, mode), (1500.0, 1500 * 16000, "CRYPTO"))

    def test_mode_rupiah_angka_polos_adalah_rupiah(self):
        amount, nominal, mode = swap_mod.parse_convert_amount("5000", self.USDT, self.USDT, "USDT", force="IDR")
        self.assertEqual((nominal, mode), (5000, "IDR"))
        self.assertAlmostEqual(amount, 5000 / self.USDT)

    def test_mode_rupiah_format_lain(self):
        for text, expected in (("50k", 50000), ("Rp 100.000", 100000), ("1.5jt", 1_500_000), ("25.000", 25000)):
            with self.subTest(text=text):
                _, nominal, mode = swap_mod.parse_convert_amount(text, self.USDT, self.USDT, "USDT", force="IDR")
                self.assertEqual((nominal, mode), (expected, "IDR"))

    def test_mode_rupiah_menolak_bukan_angka(self):
        for text in ("abc", "0", "0.5"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    swap_mod.parse_convert_amount(text, self.USDT, self.USDT, "USDT", force="IDR")

    def test_dollar_tetap_dikenali_di_semua_mode(self):
        for force in (None, "COIN", "IDR"):
            with self.subTest(force=force):
                _, nominal, mode = swap_mod.parse_convert_amount("$10", self.USDT, self.USDT, "USDT", force=force)
                self.assertEqual((nominal, mode), (160000, "USD"))

    def test_tanpa_mode_perilaku_lama_tetap(self):
        _, nominal, mode = swap_mod.parse_convert_amount("50000", self.PRICE, self.USDT, "SOL")
        self.assertEqual((nominal, mode), (50000, "IDR"))
        _, _, mode = swap_mod.parse_convert_amount("0.5", self.PRICE, self.USDT, "SOL")
        self.assertEqual(mode, "CRYPTO")


class TestConvertToggle(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_menyimpan_mode_dan_menampilkan_kurs(self):
        for data, expected, label in (("swap_mode_idr", "IDR", "Mode Nominal Rupiah"),
                                      ("swap_mode_coin", "COIN", "Mode Jumlah Koin")):
            query = AsyncMock()
            query.data = data
            context = SimpleNamespace(user_data={
                "swap_src_symbol": "USDT", "swap_src_network": "BSC",
                "swap_tgt_symbol": "SOL", "swap_tgt_network": "SOLANA",
                "swap_rate_info": "• <b>Kurs USDT:</b> <code>$1.00</code>\n"})
            state = await swap_mod.handle_input_mode(SimpleNamespace(callback_query=query), context)
            self.assertEqual(state, swap_mod.INPUT_AMOUNT)
            self.assertEqual(context.user_data["swap_input_mode"], expected)
            teks = query.edit_message_text.await_args.args[0]
            self.assertIn(label, teks)
            self.assertIn("Kurs USDT", teks)


if __name__ == "__main__":
    unittest.main()
