"""Voucher potongan fee: aturan layanan, harga di Beli/Jual/Convert, alur layar, pencatatan, perintah admin."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1", "DATABASE_URL": "sqlite:///:memory:", "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "1,2", "EVM_WALLET_ADDRESS": "0x" + "1" * 40, "EVM_PRIVATE_KEY": "",
})

import database.models  # noqa: E402,F401
from bot.handlers import buy, sell, swap  # noqa: E402
from bot.utils import voucher_flow as VF  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order, User, Voucher, VoucherRedemption  # noqa: E402
from services import voucher_service as VS  # noqa: E402

USER = 9300


class _Db(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, username="budi_ku", balance_idr=Decimal("1000000")))
        self.db.commit()
        VS.ensure_default_vouchers(self.db)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def order(self, order_id, status="pending", user=USER):
        row = Order(order_id=order_id, telegram_id=user, order_type="buy", crypto_symbol="USDT", network="BSC",
                    crypto_amount=Decimal("1"), price_per_unit=16000, nominal_idr=50000, fee_idr=3000,
                    total_idr=50000, status=status)
        self.db.add(row)
        self.db.commit()
        return row


class TestService(_Db):
    def test_voucher_bawaan_terpasang_sekali(self):
        v = VS.get_voucher(self.db, "TokoKoin1111")
        self.assertEqual((v.code, v.discount_idr, v.max_per_user, v.active), ("tokokoin1111", 500, 1, True))
        # dibatasi sejak awal: kuota total dan masa berlaku (mencegah penyalahgunaan)
        self.assertEqual(v.max_total, 200)
        remaining = v.valid_until - datetime.utcnow()
        self.assertTrue(timedelta(days=29) < remaining <= timedelta(days=30))
        self.assertEqual(VS.ensure_default_vouchers(self.db), 0)
        self.assertEqual(self.db.query(Voucher).count(), 1)

    def test_kode_tanpa_peduli_huruf_dan_spasi(self):
        for raw in ("tokokoin1111", "TOKOKOIN1111", "  toko koin 1111 "):
            voucher, error = VS.check_voucher(self.db, raw, USER, "buy")
            self.assertIsNone(error, raw)
            self.assertEqual(voucher.code, "tokokoin1111")

    def test_penolakan_dengan_alasan(self):
        self.assertIn("kosong", VS.check_voucher(self.db, "  ", USER, "buy")[1])
        self.assertIn("tidak ditemukan", VS.check_voucher(self.db, "ngasal", USER, "buy")[1])
        VS.set_active(self.db, "tokokoin1111", False)
        self.assertIn("tidak aktif", VS.check_voucher(self.db, "tokokoin1111", USER, "buy")[1])
        VS.set_active(self.db, "tokokoin1111", True)
        self.db.query(Voucher).update({"valid_until": datetime.utcnow() - timedelta(days=1)})
        self.db.commit()
        self.assertIn("kedaluwarsa", VS.check_voucher(self.db, "tokokoin1111", USER, "buy")[1])
        self.db.query(Voucher).update({"valid_until": None, "applies_to": "buy"})
        self.db.commit()
        self.assertIn("Beli", VS.check_voucher(self.db, "tokokoin1111", USER, "sell")[1])
        self.assertIsNone(VS.check_voucher(self.db, "tokokoin1111", USER, "buy")[1])

    def test_batas_per_user_dan_jatah_kembali_bila_order_mati(self):
        self.order("ORD-1")
        VS.redeem(self.db, "tokokoin1111", USER, "ORD-1", 500)
        self.assertIn("sudah kamu pakai", VS.check_voucher(self.db, "tokokoin1111", USER, "buy")[1])
        # user lain tidak terpengaruh
        self.db.add(User(telegram_id=USER + 1, balance_idr=Decimal("0")))
        self.db.commit()
        self.assertIsNone(VS.check_voucher(self.db, "tokokoin1111", USER + 1, "buy")[1])
        # order kedaluwarsa mengembalikan jatah; order selesai tidak
        self.db.query(Order).update({"status": "EXPIRED"})
        self.db.commit()
        self.assertIsNone(VS.check_voucher(self.db, "tokokoin1111", USER, "buy")[1])
        self.db.query(Order).update({"status": "completed"})
        self.db.commit()
        self.assertIsNotNone(VS.check_voucher(self.db, "tokokoin1111", USER, "buy")[1])

    def test_kuota_total(self):
        VS.upsert_voucher(self.db, "promo", 300, max_per_user=0, max_total=2)
        for i in range(2):
            self.order(f"ORD-T{i}")
            VS.redeem(self.db, "promo", USER, f"ORD-T{i}", 300)
        self.assertIn("Kuota voucher", VS.check_voucher(self.db, "promo", USER, "buy")[1])

    def test_potongan_tidak_melebihi_fee(self):
        self.assertEqual(VS.applied_discount(2500, 500), 500)
        self.assertEqual(VS.applied_discount(300, 500), 300)
        self.assertEqual(VS.applied_discount(0, 500), 0)

    def test_kelola_admin(self):
        v = VS.upsert_voucher(self.db, "Promo 500", 500, 2, 10)  # spasi dan huruf besar dinormalkan
        self.assertEqual((v.code, v.max_per_user, v.max_total), ("promo500", 2, 10))
        VS.upsert_voucher(self.db, "promo500", 700, 1, 0)
        self.assertEqual(VS.get_voucher(self.db, "promo500").discount_idr, 700)
        self.assertEqual(self.db.query(Voucher).count(), 2)
        self.assertFalse(VS.set_active(self.db, "tidakada", False))
        self.assertEqual([c.code for c, _ in VS.list_vouchers(self.db)], ["tokokoin1111", "promo500"])


def _buy_ud(nominal=100_000, fee=4_000, coin=None):
    received = nominal - fee
    price = 16_000
    return {
        "buy_symbol": "USDT", "buy_network": "BSC", "buy_price_per_unit": price,
        "buy_nominal_idr": nominal, "buy_total_idr": nominal, "buy_base_fee_idr": fee, "buy_fee_idr": fee,
        "buy_discount_amount": 0, "buy_discount_applied": False, "buy_discount_pct": None,
        "buy_received_idr": received, "buy_crypto_amount": received / price,
        "buy_coin_target": coin, "buy_fee_category": "USD", "buy_disc_pct_active": 0.0,
        "buy_pay_method": "BOT_BALANCE",
    }


class TestHargaBeli(unittest.TestCase):
    def test_potongan_mengurangi_fee_dan_menambah_koin(self):
        ud = _buy_ud()
        self.assertIsNone(buy.reprice_buy(ud, 500))
        self.assertEqual((ud["buy_fee_idr"], ud["buy_voucher_applied"], ud["buy_received_idr"]), (3_500, 500, 96_500))
        self.assertAlmostEqual(ud["buy_crypto_amount"], 96_500 / 16_000)
        self.assertEqual(ud["buy_total_idr"], 100_000)  # yang dibayar tidak berubah

    def test_lepas_voucher_kembali_persis_dan_tidak_menumpuk(self):
        ud = _buy_ud()
        base_crypto = ud["buy_crypto_amount"]
        buy.reprice_buy(ud, 500)
        buy.reprice_buy(ud, 500)
        self.assertEqual(ud["buy_fee_idr"], 3_500)
        buy.reprice_buy(ud, 0)
        self.assertEqual((ud["buy_fee_idr"], ud["buy_voucher_applied"], ud["buy_received_idr"]), (4_000, 0, 96_000))
        self.assertAlmostEqual(ud["buy_crypto_amount"], base_crypto)
        self.assertNotIn("buy_voucher_discount", ud)

    def test_potongan_dibatasi_fee(self):
        ud = _buy_ud(fee=300)
        buy.reprice_buy(ud, 500)
        self.assertEqual((ud["buy_fee_idr"], ud["buy_voucher_applied"]), (0, 300))

    def test_bersama_pajak_qris(self):
        ud = _buy_ud(nominal=850_000, fee=13_000)
        ud["buy_pay_method"] = "GOPAY_QRIS"
        buy.reprice_buy(ud, 500)
        self.assertEqual(ud["buy_mdr_idr"], 2_550)
        self.assertEqual(ud["buy_received_idr"], 850_000 - 12_500 - 2_550)
        self.assertEqual(ud["buy_total_idr"], 850_000)

    def test_ganti_metode_bayar_tetap_membawa_voucher(self):
        ud = _buy_ud(nominal=850_000, fee=13_000)
        buy.reprice_buy(ud, 500)
        buy.apply_qris_pricing(ud, "GOPAY_QRIS")
        self.assertEqual(ud["buy_received_idr"], 850_000 - 12_500 - 2_550)
        buy.apply_qris_pricing(ud, "BOT_BALANCE")
        self.assertEqual(ud["buy_received_idr"], 850_000 - 12_500)

    def test_mode_koin_nominal_turun_koin_tetap(self):
        from services.fee_service import calculate_fee_idr
        coin = "5"  # 5 USDT x 16.000 = 80.000
        ud = _buy_ud(nominal=84_000, fee=calculate_fee_idr(84_000, "USD", "USDT", "BSC"), coin=coin)
        ud["buy_crypto_amount"] = 5.0
        base_nominal = ud["buy_nominal_idr"]
        buy.reprice_buy(ud, 500)
        self.assertLess(ud["buy_nominal_idr"], base_nominal)  # bayar lebih sedikit
        self.assertEqual(ud["buy_voucher_applied"], 500)
        self.assertGreaterEqual(ud["buy_received_idr"], 80_000)
        self.assertEqual(ud["buy_crypto_amount"], 5.0)


class TestHargaJualConvert(unittest.TestCase):
    def test_jual_fee_turun_bersih_naik_koin_tetap(self):
        ud = {"sell_gross_nominal_idr": 200_000, "sell_fee_idr": 6_000, "sell_net_idr": 194_000, "sell_crypto_amount": 12.5}
        sell.reprice_sell(ud, 500)
        self.assertEqual((ud["sell_fee_idr"], ud["sell_net_idr"], ud["sell_voucher_applied"]), (5_500, 194_500, 500))
        self.assertEqual(ud["sell_crypto_amount"], 12.5)
        sell.reprice_sell(ud, 500)  # tidak menumpuk
        self.assertEqual(ud["sell_net_idr"], 194_500)
        sell.reprice_sell(ud, 0)
        self.assertEqual((ud["sell_fee_idr"], ud["sell_net_idr"]), (6_000, 194_000))

    def test_convert_fee_turun_koin_tujuan_naik(self):
        ud = {"swap_nominal_idr": 100_000, "swap_fee_idr": 5_000, "swap_tgt_amount": (100_000 - 5_000) / 2_000,
              "swap_tgt_price": 2_000}
        swap.reprice_swap(ud, 500)
        self.assertEqual((ud["swap_fee_idr"], ud["swap_voucher_applied"]), (4_500, 500))
        self.assertAlmostEqual(ud["swap_tgt_amount"], 95_500 / 2_000)
        swap.reprice_swap(ud, 500)
        self.assertAlmostEqual(ud["swap_tgt_amount"], 95_500 / 2_000)
        swap.reprice_swap(ud, 0)
        self.assertAlmostEqual(ud["swap_tgt_amount"], 95_000 / 2_000)
        self.assertEqual(ud["swap_fee_idr"], 5_000)


def _msg_update(text=None, data=None, uid=USER):
    query = None
    if data is not None:
        query = MagicMock()
        query.data = data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = SimpleNamespace(reply_text=AsyncMock())
    message = SimpleNamespace(text=text, reply_text=AsyncMock()) if text is not None else None
    return SimpleNamespace(callback_query=query, message=message, effective_user=SimpleNamespace(id=uid, username="budi_ku"))


class TestAlurLayarBeli(_Db):
    def ctx(self):
        ud = _buy_ud(nominal=100_000, fee=4_000)
        ud.update({"buy_order_id": "ORD-V1", "buy_wallet": "0x" + "c" * 40})
        return SimpleNamespace(user_data=ud, bot=AsyncMock())

    async def test_tombol_punya_voucher_di_ringkasan_lalu_kode_benar(self):
        ctx = self.ctx()
        up = _msg_update(data="paymethod_BOT_BALANCE")
        self.assertEqual(await buy._render_buy_summary(up, ctx), buy.CONFIRM_ORDER)
        markup = up.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        data = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
        self.assertIn("buy_voucher", data)
        text = up.callback_query.edit_message_text.await_args.kwargs["text"]
        self.assertNotIn("Voucher", text)
        self.assertIn("<blockquote expandable>", text)  # catatan dilipat jadi dropdown

        prompt = _msg_update(data="buy_voucher")
        self.assertEqual(await buy._buy_voucher["prompt"](prompt, ctx), buy.BUY_VOUCHER)
        self.assertIn("PUNYA VOUCHER", prompt.callback_query.edit_message_text.await_args.args[0])

        typed = _msg_update(text="TokoKoin1111")
        self.assertEqual(await buy._buy_voucher["text_input"](typed, ctx), buy.CONFIRM_ORDER)
        self.assertEqual(ctx.user_data["buy_voucher_code"], "tokokoin1111")
        self.assertEqual(ctx.user_data["buy_fee_idr"], 3_500)
        self.assertIn("dipakai", typed.message.reply_text.await_args_list[0].args[0])
        summary = typed.message.reply_text.await_args_list[1].kwargs["text"]
        self.assertIn("Voucher TOKOKOIN1111", summary)
        self.assertIn("fee -Rp 500", summary)
        data = [b.callback_data for row in typed.message.reply_text.await_args_list[1].kwargs["reply_markup"].inline_keyboard for b in row if b.callback_data]
        self.assertIn("buy_voucher_off", data)
        self.assertNotIn("buy_voucher", data)

    async def test_kode_salah_tetap_di_layar_voucher(self):
        ctx = self.ctx()
        typed = _msg_update(text="ngasal")
        self.assertEqual(await buy._buy_voucher["text_input"](typed, ctx), buy.BUY_VOUCHER)
        self.assertIn("tidak ditemukan", typed.message.reply_text.await_args.args[0])
        self.assertNotIn("buy_voucher_code", ctx.user_data)
        self.assertEqual(ctx.user_data["buy_fee_idr"], 4_000)

    async def test_lepas_dan_kembali(self):
        ctx = self.ctx()
        await buy._buy_voucher["text_input"](_msg_update(text="tokokoin1111"), ctx)
        off = _msg_update(data="buy_voucher_off")
        self.assertEqual(await buy._buy_voucher["remove"](off, ctx), buy.CONFIRM_ORDER)
        self.assertEqual((ctx.user_data["buy_fee_idr"], ctx.user_data.get("buy_voucher_code")), (4_000, None))
        back = _msg_update(data="buy_voucher_back")
        self.assertEqual(await buy._buy_voucher["back"](back, ctx), buy.CONFIRM_ORDER)

    async def test_voucher_hangus_saat_konfirmasi_dilepas_dan_harga_dihitung_ulang(self):
        ctx = self.ctx()
        await buy._buy_voucher["text_input"](_msg_update(text="tokokoin1111"), ctx)
        VS.set_active(self.db, "tokokoin1111", False)  # admin menonaktifkan di tengah jalan
        up = _msg_update(data="buy_confirm")
        ok = await VF.guard_at_confirm(up, ctx, prefix="buy", flow="buy", reprice=buy.reprice_buy, render=buy._render_buy_summary)
        self.assertFalse(ok)
        self.assertEqual((ctx.user_data["buy_fee_idr"], ctx.user_data.get("buy_voucher_code")), (4_000, None))
        self.assertIn("Voucher dilepas", up.callback_query.message.reply_text.await_args.args[0])

    async def test_konfirmasi_saldo_bot_mencatat_pemakaian_dan_fee_berkurang(self):
        ctx = self.ctx()
        ctx.user_data["buy_pay_method"] = "BOT_BALANCE"
        await buy._buy_voucher["text_input"](_msg_update(text="tokokoin1111"), ctx)
        up = _msg_update(data="buy_confirm")
        with patch("bot.handlers.buy.SessionLocal", side_effect=lambda: SessionLocal()), \
             patch("bot.handlers.buy.get_available_inventory", return_value=Decimal("999999")), \
             patch("bot.handlers.buy._run_finalize_background", new=AsyncMock()), \
             patch("bot.handlers.buy.notify_admins", new=AsyncMock()):
            await buy.handle_order_confirmation(up, ctx)
        self.db.expire_all()
        order = self.db.query(Order).filter_by(order_id="ORD-V1").one()
        self.assertEqual((order.fee_idr, order.total_idr), (3_500, 100_000))
        redemption = self.db.query(VoucherRedemption).one()
        self.assertEqual((redemption.voucher_code, redemption.order_id, redemption.discount_idr, redemption.telegram_id),
                         ("tokokoin1111", "ORD-V1", 500, USER))
        # pemakaian kedua oleh user yang sama ditolak (maks 1x per user)
        self.assertIn("sudah kamu pakai", VS.check_voucher(self.db, "tokokoin1111", USER, "buy")[1])


class TestMasaBerlaku(_Db):
    NOW = datetime(2026, 11, 6, 10, 0, 0)

    def test_parse_durasi_dan_tanggal(self):
        self.assertEqual(VS.parse_validity("12h", self.NOW), self.NOW + timedelta(hours=12))
        self.assertEqual(VS.parse_validity("7d", self.NOW), self.NOW + timedelta(days=7))
        self.assertEqual(VS.parse_validity("2w", self.NOW), self.NOW + timedelta(weeks=2))
        self.assertEqual(VS.parse_validity("2026-12-31", self.NOW), datetime(2026, 12, 31, 16, 59, 59))  # 23:59:59 WIB
        for none in ("0", "-", "tanpa"):
            self.assertIsNone(VS.parse_validity(none, self.NOW))
        for bad in ("abc", "0d", "7x", "", "2026-13-40", "7 d"):
            with self.assertRaises(ValueError, msg=bad):
                VS.parse_validity(bad, self.NOW)

    def test_format(self):
        self.assertEqual(VS.format_validity(None), "tanpa batas waktu")
        self.assertEqual(VS.format_validity(datetime(2026, 12, 31, 16, 59, 59)), "s/d 31-12-2026 23:59 WIB")

    def test_voucher_kedaluwarsa_ditolak_dan_belum_kedaluwarsa_diterima(self):
        VS.upsert_voucher(self.db, "singkat", 500, 0, 0, valid_until=datetime.utcnow() + timedelta(hours=1))
        self.assertIsNone(VS.check_voucher(self.db, "singkat", USER, "buy")[1])
        self.db.query(Voucher).filter_by(code="singkat").update({"valid_until": datetime.utcnow() - timedelta(seconds=1)})
        self.db.commit()
        self.assertIn("kedaluwarsa", VS.check_voucher(self.db, "singkat", USER, "buy")[1])

    def test_upsert_menyetel_ulang_masa_berlaku(self):
        VS.upsert_voucher(self.db, "promo", 500, valid_until=self.NOW)
        self.assertEqual(VS.get_voucher(self.db, "promo").valid_until, self.NOW)
        VS.upsert_voucher(self.db, "promo", 500)  # tanpa durasi = tanpa batas waktu
        self.assertIsNone(VS.get_voucher(self.db, "promo").valid_until)

    async def test_voucher_hangus_di_tengah_jalan_dilepas_saat_konfirmasi(self):
        ud = _buy_ud(nominal=100_000, fee=4_000)
        ud.update({"buy_order_id": "ORD-EXP", "buy_wallet": "0x" + "c" * 40})
        ctx = SimpleNamespace(user_data=ud, bot=AsyncMock())
        VS.upsert_voucher(self.db, "sebentar", 500, 0, 0, valid_until=datetime.utcnow() + timedelta(minutes=5))
        await buy._buy_voucher["text_input"](_msg_update(text="sebentar"), ctx)
        self.assertEqual(ud["buy_fee_idr"], 3_500)
        self.db.query(Voucher).filter_by(code="sebentar").update({"valid_until": datetime.utcnow() - timedelta(seconds=1)})
        self.db.commit()
        up = _msg_update(data="buy_confirm")
        ok = await VF.guard_at_confirm(up, ctx, prefix="buy", flow="buy", reprice=buy.reprice_buy, render=buy._render_buy_summary)
        self.assertFalse(ok)
        self.assertEqual(ud["buy_fee_idr"], 4_000)
        self.assertIn("kedaluwarsa", up.callback_query.message.reply_text.await_args.args[0])


class TestAlurLayarJualConvert(_Db):
    async def test_jual_ringkasan_dengan_dan_tanpa_voucher(self):
        ud = {"sell_order_id": "ORD-S1", "sell_crypto_amount": 12.5, "sell_symbol": "USDT", "sell_network": "BSC",
              "sell_price_per_unit": 16_000, "sell_gross_nominal_idr": 200_000, "sell_fee_idr": 6_000,
              "sell_net_idr": 194_000, "sell_bank_name": "BCA", "sell_bank_acc": "123", "sell_bank_holder": "Budi",
              "sell_sender_wallet": "0x" + "a" * 40}
        ctx = SimpleNamespace(user_data=ud, bot=AsyncMock())
        up = _msg_update(data="x")
        self.assertEqual(await sell._render_sell_summary(up, ctx), sell.CONFIRM_ORDER)
        data = [b.callback_data for row in up.callback_query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row if b.callback_data]
        self.assertIn("sell_voucher", data)

        typed = _msg_update(text="tokokoin1111")
        self.assertEqual(await sell._sell_voucher["text_input"](typed, ctx), sell.CONFIRM_ORDER)
        text = typed.message.reply_text.await_args_list[1].kwargs["text"]
        self.assertIn("Voucher TOKOKOIN1111", text)
        self.assertIn("Rp 194.500", text)  # nominal bersih bertambah
        self.assertLess(text.index("Nominal Bersih"), text.index("Voucher TOKOKOIN1111"))
        self.assertNotIn("Fee Layanan", text)  # fee & kurs sudah tampil di simulasi sebelumnya

    async def test_convert_ringkasan_dengan_voucher(self):
        ud = {"swap_target_addr": "0x" + "d" * 40, "swap_sender_wallet": "0x" + "a" * 40, "swap_tgt_network": "BSC",
              "swap_src_symbol": "ETH", "swap_src_network": "BASE", "swap_src_amount": 0.05, "swap_tgt_symbol": "USDT",
              "swap_tgt_amount": 95.0, "swap_nominal_idr": 100_000, "swap_fee_idr": 5_000, "swap_tgt_price": 1_000.0}
        ctx = SimpleNamespace(user_data=ud, bot=AsyncMock())
        with patch("bot.handlers.swap.CryptoSenderFactory.get_sender", side_effect=RuntimeError("x")), \
             patch("config.assets.get_wallet_address", return_value="0x" + "e" * 40):
            typed = _msg_update(text="tokokoin1111")
            self.assertEqual(await swap._swap_voucher["text_input"](typed, ctx), swap.CONFIRM_SWAP)
        text = typed.message.reply_text.await_args_list[1].args[0]
        self.assertIn("Voucher TOKOKOIN1111", text)
        self.assertIn("Rp 4,500", text)
        self.assertIn("~95.500000 USDT", text)


class TestPerintahAdmin(_Db):
    async def run_cmd(self, *args, uid=1, admin=True):
        up = _msg_update(text="/voucher", uid=uid)
        with patch("bot.handlers.admin.is_admin", return_value=admin):
            await VF.voucher_command(up, SimpleNamespace(args=list(args)))
        return up.message.reply_text.await_args.args[0]

    async def test_daftar_tambah_nonaktifkan(self):
        self.assertIn("tokokoin1111", await self.run_cmd())
        self.assertIn("tersimpan", await self.run_cmd("add", "Promo500", "500", "2", "50"))
        listing = await self.run_cmd()
        self.assertIn("promo500", listing)
        self.assertIn("2x/user", listing)
        self.assertIn("dinonaktifkan", await self.run_cmd("off", "promo500"))
        self.assertIn("⚪", await self.run_cmd())
        self.assertIn("diaktifkan", await self.run_cmd("on", "promo500"))
        self.assertIn("tidak ditemukan", await self.run_cmd("off", "tidakada"))

    async def test_tambah_dengan_masa_berlaku_dan_tampil_di_daftar(self):
        out = await self.run_cmd("add", "promo7", "500", "1", "100", "7d")
        self.assertIn("s/d", out)
        self.assertNotIn("rawan", out)  # waktu dan total terisi: tanpa peringatan
        v = VS.get_voucher(self.db, "promo7")
        self.assertTrue(timedelta(days=6) < v.valid_until - datetime.utcnow() <= timedelta(days=7))
        listing = await self.run_cmd()
        self.assertIn("promo7", listing)
        self.assertIn("s/d", listing)
        out = await self.run_cmd("add", "tanpabatas", "500")
        self.assertIn("tanpa batas waktu", out)
        self.assertIn("rawan disalahgunakan", out)  # peringatan bila waktu/total tak dibatasi
        self.db.query(Voucher).filter_by(code="promo7").update({"valid_until": datetime.utcnow() - timedelta(days=1)})
        self.db.commit()
        listing = await self.run_cmd()
        self.assertIn("KEDALUWARSA", listing)
        self.assertIn("⌛", listing)

    async def test_masa_berlaku_tidak_valid_ditolak(self):
        for bad in ("abc", "0d", "2026-99-99"):
            self.assertIn("Masa berlaku tidak valid", await self.run_cmd("add", "promo", "500", "1", "0", bad), bad)
        self.assertIsNone(VS.get_voucher(self.db, "promo"))
        self.assertIn("berlaku", await self.run_cmd("add"))  # bantuan memuat opsi berlaku

    async def test_input_tidak_valid_dan_bukan_admin(self):
        for bad in (("add", "ab", "500"), ("add", "promo", "0"), ("add", "promo", "9999999"), ("add", "promo", "x"),
                    ("add", "pro mo!", "500")):
            self.assertIn("❌", await self.run_cmd(*bad), bad)
        self.assertEqual(self.db.query(Voucher).count(), 1)
        self.assertIn("tidak memiliki akses", await self.run_cmd(uid=99, admin=False))


class TestWiring(unittest.TestCase):
    def test_state_voucher_terdaftar_di_tiga_alur(self):
        self.assertIn(buy.BUY_VOUCHER, buy.buy_conversation_handler.states)
        self.assertIn(sell.SELL_VOUCHER, sell.sell_conversation_handler.states)
        self.assertIn(swap.SWAP_VOUCHER, swap.swap_conv_handler.states)

    def test_teks_dan_perintah(self):
        self.assertIn("potongan fee transaksi", VF.PROMPT_TEXT)
        self.assertIn("tokokoin1111", VF.PROMPT_TEXT)
        from bot.utils.command_menu import ADMIN_COMMAND_MENU
        self.assertIn("voucher", [name for name, _ in ADMIN_COMMAND_MENU])

    def test_voucher_bawaan_dipasang_saat_start(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("ensure_default_vouchers(db)", src)
        self.assertIn('CommandHandler("voucher", voucher_command)', src)


if __name__ == "__main__":
    unittest.main()
