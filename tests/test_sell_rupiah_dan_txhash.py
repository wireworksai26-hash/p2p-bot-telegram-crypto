"""Jual dengan nominal Rupiah + penerimaan TX hash wajib (Jual/Convert).

1. parse_idr_amount / looks_like_idr: format Rupiah yang diterima.
2. Mode Rupiah di alur Jual: koin = Rupiah / kurs jual, dibulatkan KE ATAS ke presisi koin,
   tanpa kode unik; mode koin tetap dibulatkan ke bawah.
3. submit_deposit_hash (dipakai Jual, Convert dan /txhash): hash salah/sudah dipakai/ditolak
   on-chain tidak disimpan dan admin tidak dikabari; hash menunggu konfirmasi disimpan.
4. /txhash: tanpa order -> info, satu order -> langsung diminta hash.
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
from database.models import DepositClaim, Order, User
from bot.utils.validator import parse_idr_amount, looks_like_idr
from bot.handlers import sell as sell_mod
from bot.handlers.sell import (
    handle_amount_input, handle_input_mode, INPUT_AMOUNT, INPUT_SENDER,
)
from bot.handlers.deposit_hash import (
    submit_deposit_hash, txhash_command, txhash_receive, WAIT_HASH,
)
from telegram.ext import ConversationHandler

HOT = "0x" + "1" * 40
HASH_1 = "0x" + "ab" * 32


def _price(sell=16000.0):
    return {
        "symbol": "USDT", "market_price_idr": sell, "buy_price_idr": sell, "sell_price_idr": sell,
        "spread_pct": 0.0, "usdt_idr_rate": sell, "source": "TEST",
        "price_updated_at": int(datetime.now(timezone.utc).timestamp()),
    }


class TestParseIdr(unittest.TestCase):
    def test_format_yang_diterima(self):
        cases = {
            "5000": 5000, "5.000": 5000, "5,000": 5000, "Rp5000": 5000, "Rp 5.000": 5000,
            "IDR 5000": 5000, "5k": 5000, "5rb": 5000, "50 ribu": 50000,
            "1.5jt": 1_500_000, "2 juta": 2_000_000, "1.000.000": 1_000_000,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_idr_amount(text), expected)

    def test_format_yang_ditolak(self):
        for text in ("", "abc", "0", "-5000", "0.5", "5000x", "1e3", "nan"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_idr_amount(text)

    def test_looks_like_idr_hanya_untuk_penanda_jelas(self):
        for text in ("Rp5000", "rp 5.000", "5k", "5rb", "1.5jt", "IDR 2000"):
            self.assertTrue(looks_like_idr(text), text)
        for text in ("5000", "0.5", "10", "5.000"):
            self.assertFalse(looks_like_idr(text), text)


class SellAmountCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=42, username="u", full_name="U"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def _kirim(self, text, mode="COIN", price=16000.0, symbol="USDT", network="BSC"):
        update = SimpleNamespace(message=AsyncMock(text=text), effective_user=SimpleNamespace(id=42, first_name="T"))
        context = SimpleNamespace(
            user_data={"sell_symbol": symbol, "sell_network": network, "sell_input_mode": mode}, bot=AsyncMock())
        with patch("services.price_service.price_service.get_price", new=AsyncMock(return_value=_price(price))):
            state = await handle_amount_input(update, context)
        return state, context, update


class TestSellRupiah(SellAmountCase):
    async def test_rupiah_pas_dibagi_kurs(self):
        state, ctx, _ = await self._kirim("5000", mode="IDR", price=16000.0)
        self.assertEqual(state, INPUT_SENDER)
        # Nominal Rupiah = yang masuk rekening; fee (3.000) ditambahkan ke koin yang disetor.
        self.assertEqual(ctx.user_data["sell_crypto_amount"], 0.5)
        self.assertEqual(ctx.user_data["sell_gross_nominal_idr"], 8000)
        self.assertEqual(ctx.user_data["sell_fee_idr"], 3000)
        self.assertEqual(ctx.user_data["sell_net_idr"], 5000)

    async def test_rupiah_dibulatkan_ke_atas_agar_tidak_di_bawah_nominal(self):
        price = 17915.0
        state, ctx, _ = await self._kirim("Rp 5.000", mode="IDR", price=price)
        self.assertEqual(state, INPUT_SENDER)
        coin = Decimal(str(ctx.user_data["sell_crypto_amount"]))
        self.assertEqual(coin, coin.quantize(Decimal("0.0001")), "presisi USDT = 4 desimal")
        gross = ctx.user_data["sell_gross_nominal_idr"]
        net = ctx.user_data["sell_net_idr"]
        self.assertEqual(net, gross - ctx.user_data["sell_fee_idr"])
        self.assertGreaterEqual(net, 5000, "yang diterima tidak boleh di bawah nominal yang diminta")
        self.assertLess(net, 5000 + price * 0.0001 + 1, "pembulatan maksimal 1 satuan terkecil koin")

    async def test_rupiah_bersih_persis_untuk_berbagai_nominal_dan_koin(self):
        # Kasus client: ketik 50.000 -> yang masuk rekening 50.000 (fee ditambah di atasnya).
        for symbol, network, price in (("USDT", "BSC", 16000.0), ("SOL", "SOLANA", 2_000_000.0),
                                       ("BTC", "BTC", 1_500_000_000.0), ("ETH", "ETH", 40_000_000.0)):
            for nominal in (50_000, 100_000, 1_000_000, 4_000_000):
                with self.subTest(symbol=symbol, nominal=nominal):
                    state, ctx, _ = await self._kirim(
                        str(nominal), mode="IDR", price=price, symbol=symbol, network=network)
                    self.assertEqual(state, INPUT_SENDER)
                    net = ctx.user_data["sell_net_idr"]
                    self.assertGreaterEqual(net, nominal)
                    self.assertLess(net - nominal, price * 0.0001 + 2000 if symbol == "BTC" else 200)
                    self.assertEqual(
                        net, ctx.user_data["sell_gross_nominal_idr"] - ctx.user_data["sell_fee_idr"])

    async def test_penanda_rupiah_dikenali_walau_mode_koin(self):
        state, ctx, _ = await self._kirim("50k", mode="COIN", price=16000.0)
        self.assertEqual(state, INPUT_SENDER)
        self.assertEqual(ctx.user_data["sell_net_idr"], 50000)
        self.assertEqual(ctx.user_data["sell_gross_nominal_idr"], 54000)
        self.assertEqual(ctx.user_data["sell_crypto_amount"], 3.375)

    async def test_rupiah_di_bawah_minimum_ditolak(self):
        state, ctx, update = await self._kirim("Rp 1000", mode="IDR")
        self.assertEqual(state, INPUT_AMOUNT)
        self.assertIn("Minimum transaksi", update.message.reply_text.call_args.args[0])

    async def test_mode_rupiah_menolak_teks_bukan_angka(self):
        state, _, update = await self._kirim("sepuluh ribu", mode="IDR")
        self.assertEqual(state, INPUT_AMOUNT)
        self.assertIn("Nominal Rupiah Tidak Valid", update.message.reply_text.call_args.kwargs["text"])

    async def test_mode_koin_dibulatkan_ke_bawah_tanpa_kode_unik(self):
        state, ctx, _ = await self._kirim("0.50009", mode="COIN")
        self.assertEqual(state, INPUT_SENDER)
        self.assertEqual(ctx.user_data["sell_crypto_amount"], 0.5)

    async def test_toggle_mode(self):
        for data, expected in (("sell_mode_idr", "IDR"), ("sell_mode_coin", "COIN")):
            query = AsyncMock()
            query.data = data
            update = SimpleNamespace(callback_query=query)
            context = SimpleNamespace(user_data={"sell_symbol": "USDT", "sell_network": "BSC"})
            with patch.object(sell_mod, "safe_edit_message", new=AsyncMock()) as edit:
                state = await handle_input_mode(update, context)
            self.assertEqual(state, INPUT_AMOUNT)
            self.assertEqual(context.user_data["sell_input_mode"], expected)
            teks = edit.await_args.kwargs["text"]
            self.assertIn("Mode Nominal Rupiah" if expected == "IDR" else "Mode Jumlah Koin", teks)


def _order(order_id="ORD-H-1", telegram_id=42, order_type="sell", status="WAITING_CRYPTO_DEPOSIT", amount="10"):
    return Order(
        order_id=order_id, telegram_id=telegram_id, order_type=order_type,
        crypto_symbol="USDT", network="BSC", crypto_amount=Decimal(amount),
        price_per_unit=16000, nominal_idr=160000, fee_idr=6000, total_idr=154000,
        buyer_wallet="BCA | 1 | X", deposit_wallet=HOT, status=status,
    )


class HashCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=42, username="u", full_name="U"))
        self.db.add(_order())
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _update(self, text, user_id=42):
        message = AsyncMock()
        message.text = text
        return SimpleNamespace(message=message, effective_message=message,
                               effective_user=SimpleNamespace(id=user_id), callback_query=None)

    def _stored_hash(self, order_id="ORD-H-1"):
        self.db.expire_all()
        return self.db.query(Order).filter(Order.order_id == order_id).one().deposit_tx_hash

    async def _submit(self, text, verify_result, order_id="ORD-H-1", user_id=42):
        update = self._update(text, user_id)
        context = SimpleNamespace(application=object(), user_data={})
        with patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value=verify_result)), \
             patch("services.detector.deposit_detector.verifikasi_cepat", new=AsyncMock()) as fast, \
             patch("services.detector.notify_admins", new=AsyncMock()) as notify, \
             patch("services.detector.safe_send_message", new=AsyncMock()):
            result = await submit_deposit_hash(update, context, order_id, text)
        return result, update, fast, notify


class TestSubmitDepositHash(HashCase):
    async def test_format_salah_diminta_ulang_dan_tidak_disimpan(self):
        result, update, _, notify = await self._submit("bukan-hash", {"verified": False, "reason": "x"})
        self.assertEqual(result, "retry")
        self.assertIsNone(self._stored_hash())
        notify.assert_not_called()
        self.assertIn("Format TX Hash Salah", update.message.reply_text.call_args.args[0])

    async def test_hash_ditolak_onchain_tidak_disimpan_dan_admin_tidak_dikabari(self):
        result, update, fast, notify = await self._submit(
            HASH_1, {"verified": False, "amount": 0.0, "from_address": "",
                     "reason": "Nominal deposit kurang: diterima 9 USDT, dibutuhkan 10 USDT."})
        self.assertEqual(result, "retry")
        self.assertIsNone(self._stored_hash())
        notify.assert_not_called()
        fast.assert_not_called()
        teks = update.message.reply_text.call_args.args[0]
        self.assertIn("Deposit Belum Bisa Diverifikasi", teks)
        self.assertIn("10.0000", teks, "user diberi tahu nominal tepat yang harus dikirim")

    async def test_hash_menunggu_konfirmasi_disimpan_dan_dipantau(self):
        result, _, fast, notify = await self._submit(
            HASH_1, {"verified": False, "amount": 0.0, "from_address": "", "reason": "Menunggu konfirmasi jaringan"})
        self.assertEqual(result, "done")
        self.assertEqual(self._stored_hash(), HASH_1)
        fast.assert_called_once()
        notify.assert_not_called()

    async def test_hash_sudah_dipakai_order_lain_ditolak(self):
        self.db.add(_order("ORD-LAIN", telegram_id=43))
        self.db.add(DepositClaim(network="BSC", tx_hash=HASH_1, order_id="ORD-LAIN"))
        self.db.commit()
        result, update, _, notify = await self._submit(HASH_1, {"verified": True, "amount": 10.0})
        self.assertEqual(result, "retry")
        self.assertIsNone(self._stored_hash())
        notify.assert_not_called()
        self.assertIn("sudah dipakai order lain", update.message.reply_text.call_args.args[0])

    async def test_order_milik_user_lain_ditolak(self):
        result, update, _, _ = await self._submit(HASH_1, {"verified": True, "amount": 10.0}, user_id=999)
        self.assertEqual(result, "done")
        self.assertIsNone(self._stored_hash())
        self.assertIn("tidak ditemukan", update.message.reply_text.call_args.args[0])

    async def test_order_sudah_tidak_menunggu_deposit(self):
        self.db.query(Order).filter(Order.order_id == "ORD-H-1").update({Order.status: "COMPLETED"})
        self.db.commit()
        result, update, _, _ = await self._submit(HASH_1, {"verified": True, "amount": 10.0})
        self.assertEqual(result, "done")
        self.assertIn("sudah tidak menunggu deposit", update.message.reply_text.call_args.args[0])

    async def test_hash_valid_mengonfirmasi_dan_admin_dikabari_sekali(self):
        from services import tx_verifier
        order = self.db.query(Order).filter(Order.order_id == "ORD-H-1").one()
        verified = {"verified": True, "amount": 10.0, "reason": "OK", "tx_hash": HASH_1,
                    "from_address": "0x" + "7" * 40,
                    "timestamp": tx_verifier._timestamp(order.created_at) + 5}
        result, _, _, notify = await self._submit(HASH_1, verified)
        self.assertEqual(result, "done")
        self.db.expire_all()
        self.assertEqual(self.db.query(Order).filter(Order.order_id == "ORD-H-1").one().status, "CRYPTO_CONFIRMED")
        notify.assert_awaited_once()
        self.assertIn("0x" + "7" * 40, notify.await_args.args[1])

    async def test_convert_juga_diterima_lewat_fungsi_yang_sama(self):
        self.db.add(_order("ORD-SWAP-1", order_type="swap"))
        self.db.commit()
        result, _, fast, _ = await self._submit(
            HASH_1, {"verified": False, "amount": 0.0, "from_address": "", "reason": "Menunggu konfirmasi jaringan"},
            order_id="ORD-SWAP-1")
        self.assertEqual(result, "done")
        self.assertEqual(self._stored_hash("ORD-SWAP-1"), HASH_1)
        fast.assert_called_once()


class TestTxhashCommand(HashCase):
    async def test_tanpa_order_menunggu(self):
        self.db.query(Order).delete()
        self.db.commit()
        update = self._update("/txhash")
        context = SimpleNamespace(user_data={}, application=object())
        state = await txhash_command(update, context)
        self.assertEqual(state, ConversationHandler.END)
        self.assertIn("Tidak ada order", update.message.reply_text.call_args.args[0])

    async def test_satu_order_langsung_diminta_hash(self):
        update = self._update("/txhash")
        context = SimpleNamespace(user_data={}, application=object())
        state = await txhash_command(update, context)
        self.assertEqual(state, WAIT_HASH)
        self.assertEqual(context.user_data["txhash_order_id"], "ORD-H-1")
        self.assertIn("ORD-H-1", update.message.reply_text.call_args.args[0])

    async def test_hash_salah_tetap_di_state_dan_benar_mengakhiri(self):
        context = SimpleNamespace(user_data={"txhash_order_id": "ORD-H-1"}, application=object())
        with patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value={
                "verified": False, "amount": 0.0, "from_address": "", "reason": "Menunggu konfirmasi jaringan"})), \
             patch("services.detector.deposit_detector.verifikasi_cepat", new=AsyncMock()):
            salah = await txhash_receive(self._update("zzz"), context)
            self.assertEqual(salah, WAIT_HASH)
            benar = await txhash_receive(self._update(HASH_1), context)
        self.assertEqual(benar, ConversationHandler.END)
        self.assertEqual(self._stored_hash(), HASH_1)


if __name__ == "__main__":
    unittest.main()
