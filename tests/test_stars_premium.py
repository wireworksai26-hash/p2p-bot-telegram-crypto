"""Telegram Stars & Premium: katalog harga, data pesanan (klaim atomik), alur user, pembayaran QRIS, sisi admin."""
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
from config.settings import settings  # noqa: E402
from database import stars_crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import AuditLog, Order, StarsOrder, TopupOrder, User  # noqa: E402
from bot.handlers import stars as H  # noqa: E402
from services import stars_catalog as C  # noqa: E402

USER = 9001


class TestCatalog(unittest.TestCase):
    def test_harga_persis_daftar_klien(self):
        self.assertEqual(C.STARS_PACKAGES, {
            50: 16_500, 75: 24_000, 100: 31_000, 150: 45_000, 250: 74_000, 350: 103_000,
            500: 143_000, 750: 215_000, 1000: 278_500, 1500: 414_000, 2500: 690_000, 5000: 1_361_000})
        self.assertEqual(C.PREMIUM_PACKAGES, {3: 228_000, 6: 305_400, 12: 539_500})

    def test_label_dan_harga(self):
        self.assertEqual(C.package_label("stars", 1000), "1.000 Stars")
        self.assertEqual(C.package_label("premium", 3), "3 Bulan")
        self.assertEqual(C.package_price("stars", 250), 74_000)
        self.assertEqual(C.package_price("stars", 999), 0)
        self.assertEqual(C.package_price("lain", 3), 0)
        self.assertEqual(C.package_price("premium", "abc"), 0)

    def test_parse_username(self):
        for raw, want in (("@Budi_99", "Budi_99"), ("budi_99", "budi_99"), ("t.me/budi_99", "budi_99"),
                          ("https://t.me/Budi_99/", "Budi_99"), ("  @abcde  ", "abcde")):
            self.assertEqual(C.parse_username(raw), want, raw)
        for bad in ("abc", "@1abcde", "a b c d e", "", None, "@" + "a" * 33, "@ab-cd"):
            self.assertIsNone(C.parse_username(bad), bad)

    def test_teks_pricelist_memuat_semua_paket(self):
        text = C.pricelist_text()
        for price in ("Rp 16.500", "Rp 278.500", "Rp 1.361.000", "Rp 228.000", "Rp 305.400", "Rp 539.500"):
            self.assertIn(price, text)
        self.assertIn("akun teman", text)


class _Db(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, username="budi", full_name="Budi", balance_idr=Decimal("1000000")))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def balance(self, uid=USER):
        self.db.expire_all()
        return int(self.db.query(User).filter_by(telegram_id=uid).one().balance_idr)


class TestOrderData(_Db):
    def test_saldo_bot_memotong_dan_langsung_paid(self):
        order = stars_crud.create_balance_order(self.db, USER, "stars", 100, "teman_ku")
        self.assertEqual((order.status, order.total_idr, order.payment_method), ("paid", 31_000, "BOT_BALANCE"))
        self.assertEqual(order.recipient_username, "teman_ku")
        self.assertEqual(self.balance(), 1_000_000 - 31_000)
        self.assertEqual(self.db.query(AuditLog).filter_by(action="STARS_ORDER_PAID").count(), 1)

    def test_saldo_kurang_tidak_membuat_pesanan(self):
        self.db.query(User).update({"balance_idr": Decimal("10000")})
        self.db.commit()
        self.assertIsNone(stars_crud.create_balance_order(self.db, USER, "stars", 100, "teman_ku"))
        self.assertEqual(self.balance(), 10_000)
        self.assertEqual(self.db.query(StarsOrder).count(), 0)

    def test_paket_tidak_dikenal_ditolak(self):
        self.assertIsNone(stars_crud.create_balance_order(self.db, USER, "stars", 7, "teman_ku"))
        self.assertIsNone(stars_crud.create_qris_order(self.db, USER, "premium", 4, "teman_ku", 10))

    def test_qris_tagihan_harga_plus_kode_unik(self):
        order = stars_crud.create_qris_order(self.db, USER, "premium", 3, "teman_ku", 10)
        self.assertEqual(order.status, "pending")
        self.assertTrue(1 <= order.unique_code <= 400)
        self.assertEqual(order.total_idr, 228_000 + order.unique_code)
        self.assertNotEqual(order.total_idr % 100, 0)  # kelipatan 100 ambigu di gateway
        self.assertEqual(self.balance(), 1_000_000)  # QRIS tidak menyentuh saldo
        self.assertEqual(stars_crud.get_open_qris_order(self.db, USER).order_id, order.order_id)

    def test_kode_unik_tidak_kembar_dengan_pesanan_stars_lain_maupun_topup(self):
        a = stars_crud.create_qris_order(self.db, USER, "stars", 100, "a_user", 10)
        self.db.add(User(telegram_id=USER + 1, balance_idr=Decimal("0")))
        self.db.add(TopupOrder(topup_id="TOPUP-X", telegram_id=USER + 1, amount_idr=31_000 + 7, unique_code=7,
                               status="PENDING", created_at=datetime.utcnow()))
        self.db.commit()
        for _ in range(30):
            b = stars_crud.create_qris_order(self.db, USER + 1, "stars", 100, "b_user", 10)
            self.assertNotEqual(b.unique_code, a.unique_code)
            self.assertNotEqual(b.unique_code, 7)
            self.assertNotEqual(b.total_idr, a.total_idr)
            self.db.query(StarsOrder).filter_by(id=b.id).delete()
            self.db.commit()

    def test_mark_paid_sekali_dan_telat_ditandai(self):
        order = stars_crud.create_qris_order(self.db, USER, "stars", 50, "a_user", 10)
        paid = stars_crud.mark_paid(self.db, order.order_id)
        self.assertEqual((paid.status, paid.late_payment), ("paid", False))
        self.assertIsNone(stars_crud.mark_paid(self.db, order.order_id))  # klaim kedua gagal

        late = stars_crud.create_qris_order(self.db, USER, "stars", 75, "a_user", 10)
        self.db.query(StarsOrder).filter_by(id=late.id).update(
            {"status": "expired", "expired_at": datetime.utcnow() - timedelta(minutes=5)})
        self.db.commit()
        self.assertIn(late.order_id, [o.order_id for o in stars_crud.get_pending_qris(self.db)])
        self.assertTrue(stars_crud.mark_paid(self.db, late.order_id).late_payment)

    def test_kedaluwarsa_dan_jendela_bayar_telat(self):
        order = stars_crud.create_qris_order(self.db, USER, "stars", 50, "a_user", 10)
        self.db.query(StarsOrder).filter_by(id=order.id).update({"expired_at": datetime.utcnow() - timedelta(minutes=1)})
        self.db.commit()
        self.assertEqual(stars_crud.expire_pending(self.db), 1)
        self.assertIsNone(stars_crud.get_open_qris_order(self.db, USER))
        # lewat 2 jam sejak kedaluwarsa: tidak lagi dipantau poller
        self.db.query(StarsOrder).filter_by(id=order.id).update({"expired_at": datetime.utcnow() - timedelta(hours=3)})
        self.db.commit()
        self.assertEqual(stars_crud.get_pending_qris(self.db), [])

    def test_batal_hanya_pemilik_dan_hanya_pending(self):
        order = stars_crud.create_qris_order(self.db, USER, "stars", 50, "a_user", 10)
        self.assertFalse(stars_crud.cancel_pending(self.db, order.order_id, USER + 5))
        self.assertTrue(stars_crud.cancel_pending(self.db, order.order_id, USER))
        self.assertFalse(stars_crud.cancel_pending(self.db, order.order_id, USER))

    def test_selesai_sekali_dan_refund_sekali(self):
        a = stars_crud.create_balance_order(self.db, USER, "stars", 100, "a_user")
        self.assertEqual(stars_crud.complete(self.db, a.order_id, 1).status, "completed")
        self.assertIsNone(stars_crud.complete(self.db, a.order_id, 1))
        self.assertIsNone(stars_crud.reject_and_refund(self.db, a.order_id, 1))  # sudah selesai: tak bisa refund
        self.assertEqual(self.balance(), 1_000_000 - 31_000)

        b = stars_crud.create_balance_order(self.db, USER, "stars", 150, "a_user")
        self.assertEqual(self.balance(), 1_000_000 - 31_000 - 45_000)
        self.assertEqual(stars_crud.reject_and_refund(self.db, b.order_id, 2).status, "rejected")
        self.assertIsNone(stars_crud.reject_and_refund(self.db, b.order_id, 2))
        self.assertIsNone(stars_crud.complete(self.db, b.order_id, 2))
        self.assertEqual(self.balance(), 1_000_000 - 31_000)  # refund tepat sekali

    def test_refund_pesanan_qris_mengembalikan_total_yang_dibayar(self):
        order = stars_crud.create_qris_order(self.db, USER, "stars", 100, "a_user", 10)
        stars_crud.mark_paid(self.db, order.order_id)
        stars_crud.reject_and_refund(self.db, order.order_id, 1)
        self.assertEqual(self.balance(), 1_000_000 + int(order.total_idr))

    def test_antrean_dan_ringkasan(self):
        a = stars_crud.create_balance_order(self.db, USER, "stars", 100, "a_user")
        b = stars_crud.create_balance_order(self.db, USER, "premium", 3, "b_user")
        stars_crud.complete(self.db, a.order_id, 1)
        self.assertEqual(stars_crud.paid_summary(self.db), (1, 228_000))
        self.assertEqual([o.order_id for o in stars_crud.get_paid_orders(self.db)], [b.order_id])
        self.assertEqual(len(stars_crud.get_user_orders(self.db, USER)), 2)


def _update(data=None, text=None, uid=USER, username="budi_ku"):
    query = None
    if data is not None:
        query = MagicMock()
        query.data = data
        query.from_user = SimpleNamespace(id=uid)
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.message = SimpleNamespace(photo=None, text_html="kartu", delete=AsyncMock(), reply_text=AsyncMock(),
                                        reply_markup=None)
    message = None
    if text is not None:
        message = SimpleNamespace(text=text, reply_text=AsyncMock())
    user = SimpleNamespace(id=uid, username=username, full_name="Budi")
    return SimpleNamespace(callback_query=query, message=message, effective_user=user, effective_chat=SimpleNamespace(id=uid))


def _ctx(**ud):
    return SimpleNamespace(user_data=dict(ud), bot=AsyncMock())


class TestFlow(_Db):
    async def test_menu_produk_paket(self):
        up, ctx = _update("menu_stars"), _ctx()
        with patch.object(H, "block_if_busy", new=AsyncMock(return_value=False)):
            self.assertEqual(await H.start_stars(up, ctx), H.ST_MENU)
        self.assertIn("TELEGRAM STARS", up.callback_query.edit_message_text.await_args.args[0])

        up = _update("stp_prod_stars")
        self.assertEqual(await H.choose_product(up, ctx), H.ST_PACKAGE)
        buttons = [b for row in up.callback_query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        pkg = [b for b in buttons if b.callback_data.startswith("stp_pkg_")]
        self.assertEqual(len(pkg), 12)
        self.assertIn("50 Stars · Rp 16.500", [b.text for b in pkg])

        up = _update("stp_prod_premium")
        await H.choose_product(up, ctx)
        buttons = [b for row in up.callback_query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(len([b for b in buttons if b.callback_data.startswith("stp_pkg_")]), 3)

    async def test_paket_lalu_username_valid_dan_tidak_valid(self):
        ctx = _ctx(stp_product="stars")
        up = _update("stp_pkg_100")
        self.assertEqual(await H.choose_package(up, ctx), H.ST_USERNAME)
        self.assertEqual(ctx.user_data["stp_package"], 100)
        data = [b.callback_data for row in up.callback_query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("stp_self", data)  # user punya username

        bad = _update(text="ab")
        self.assertEqual(await H.receive_username(bad, ctx), H.ST_USERNAME)
        self.assertIn("tidak valid", bad.message.reply_text.await_args.args[0])

        good = _update(text="@Teman_Ku")
        self.assertEqual(await H.receive_username(good, ctx), H.ST_PAYMENT)
        self.assertEqual(ctx.user_data["stp_recipient"], "Teman_Ku")
        text = good.message.reply_text.await_args.args[0]
        self.assertIn("@Teman_Ku", text)
        self.assertIn("100 Stars", text)
        data = [b.callback_data for row in good.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("stp_pay_BOT_BALANCE", data)
        self.assertIn("stp_pay_GOPAY_QRIS", data)

    async def test_saldo_kurang_tidak_ada_tombol_saldo_bot(self):
        self.db.query(User).update({"balance_idr": Decimal("1000")})
        self.db.commit()
        ctx = _ctx(stp_product="stars", stp_package=100)
        up = _update(text="@teman_ku")
        await H.receive_username(up, ctx)
        data = [b.callback_data for row in up.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertNotIn("stp_pay_BOT_BALANCE", data)
        self.assertIn("stp_pay_GOPAY_QRIS", data)

    async def test_pakai_akun_sendiri(self):
        ctx = _ctx(stp_product="premium", stp_package=3)
        up = _update("stp_self")
        self.assertEqual(await H.use_own_username(up, ctx), H.ST_PAYMENT)
        self.assertEqual(ctx.user_data["stp_recipient"], "budi_ku")
        up = _update("stp_self", username=None)
        self.assertEqual(await H.use_own_username(up, ctx), H.ST_USERNAME)

    async def test_konfirmasi_saldo_bot_membuat_pesanan_dan_mengabari_admin(self):
        ctx = _ctx(stp_product="stars", stp_package=100, stp_recipient="teman_ku", stp_method="BOT_BALANCE")
        up = _update("stp_confirm")
        notif = AsyncMock()
        with patch.object(H, "notify_admins", new=notif):
            from telegram.ext import ConversationHandler
            self.assertEqual(await H.confirm_order(up, ctx), ConversationHandler.END)
        order = self.db.query(StarsOrder).one()
        self.assertEqual((order.status, order.payment_method, order.recipient_username), ("paid", "BOT_BALANCE", "teman_ku"))
        self.assertEqual(self.balance(), 1_000_000 - 31_000)
        text = notif.await_args.args[1]
        self.assertIn("SIAP DIKIRIM", text)
        self.assertIn("<code>@teman_ku</code>", text)
        self.assertIn("100 Stars", text)
        self.assertEqual(notif.await_args.kwargs["kind"], "stars")
        data = {b.callback_data for row in notif.await_args.kwargs["reply_markup"].inline_keyboard for b in row}
        self.assertEqual(data, {f"admin_stp_take_{order.order_id}", f"admin_stp_ok_{order.order_id}", f"admin_stp_no_{order.order_id}"})

    async def test_konfirmasi_qris_mengirim_gambar_total_dan_memblokir_pesanan_kedua(self):
        import io
        ctx = _ctx(stp_product="premium", stp_package=3, stp_recipient="teman_ku", stp_method="GOPAY_QRIS")
        up = _update("stp_confirm")
        seen = {}

        def fake_qris(amount):
            seen["amount"] = amount
            return io.BytesIO(b"qr")

        with patch("services.qris_generator.get_qris_image_stream", side_effect=fake_qris):
            await H.confirm_order(up, ctx)
        order = self.db.query(StarsOrder).one()
        self.assertEqual((order.status, order.payment_method), ("pending", "GOPAY_QRIS"))
        self.assertEqual(seen["amount"], order.total_idr)
        caption = ctx.bot.send_photo.await_args.kwargs["caption"]
        self.assertIn(f"Rp {int(order.total_idr):,}".replace(",", "."), caption)
        self.assertIn("3 Bulan", caption)
        data = [b.callback_data for row in ctx.bot.send_photo.await_args.kwargs["reply_markup"].inline_keyboard for b in row if b.callback_data]
        self.assertIn(f"stp_check_{order.order_id}", data)
        self.assertIn(f"stp_cancel_{order.order_id}", data)

        # QRIS kedua selama yang pertama belum dibayar: ditolak
        up2 = _update("stp_confirm")
        await H.confirm_order(up2, _ctx(stp_product="stars", stp_package=50, stp_recipient="x_user", stp_method="GOPAY_QRIS"))
        self.assertEqual(self.db.query(StarsOrder).count(), 1)
        self.assertIn("belum dibayar", up2.callback_query.edit_message_text.await_args.args[0])

    async def test_sesi_habis_tidak_membuat_pesanan(self):
        up = _update("stp_confirm")
        with patch.object(H, "block_if_busy", new=AsyncMock(return_value=False)):
            await H.confirm_order(up, _ctx())
        self.assertEqual(self.db.query(StarsOrder).count(), 0)
        self.assertTrue(up.callback_query.answer.await_args_list[0].kwargs.get("show_alert"))  # peringatan sesi habis

    async def test_pesanan_saya(self):
        stars_crud.create_balance_order(self.db, USER, "stars", 100, "teman_ku")
        up = _update("stp_mine")
        await H.show_my_orders(up, _ctx())
        text = up.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("100 Stars", text)
        self.assertIn("@teman_ku", text)
        self.assertIn("diproses admin", text)


class TestQrisPayment(_Db):
    def _qris(self):
        return stars_crud.create_qris_order(self.db, USER, "stars", 100, "teman_ku", 10)

    async def test_cek_pembayaran_terdeteksi_meneruskan_sekali(self):
        order = self._qris()
        announce = AsyncMock()
        with patch("services.gopay_service.gopay_service.confirm_payment", new=AsyncMock(return_value=True)), \
             patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()), \
             patch.object(H, "announce_paid", new=announce):
            await H.check_payment(_update(f"stp_check_{order.order_id}"), _ctx())
            await H.check_payment(_update(f"stp_check_{order.order_id}"), _ctx())  # klik kedua
        self.db.expire_all()
        self.assertEqual(self.db.query(StarsOrder).one().status, "paid")
        self.assertEqual(announce.await_count, 1)

    async def test_cek_pembayaran_belum_masuk_dan_bukan_pemilik(self):
        order = self._qris()
        up = _update(f"stp_check_{order.order_id}")
        up.callback_query.message.reply_markup = InlineKeyboardMarkupStub()
        with patch("services.gopay_service.gopay_service.confirm_payment", new=AsyncMock(return_value=False)), \
             patch.object(H, "refresh_message_status", new=AsyncMock(return_value=True)):
            await H.check_payment(up, _ctx())
        self.assertEqual(stars_crud.get_order(self.db, order.order_id).status, "pending")
        other = _update(f"stp_check_{order.order_id}", uid=USER + 7)
        await H.check_payment(other, _ctx())
        self.assertIn("tidak ditemukan", other.callback_query.answer.await_args.args[0])

    async def test_batal_saat_belum_bayar_dan_saat_ternyata_sudah_bayar(self):
        order = self._qris()
        with patch("services.gopay_service.gopay_service.confirm_payment", new=AsyncMock(return_value=False)):
            await H.cancel_order(_update(f"stp_cancel_{order.order_id}"), _ctx())
        self.db.expire_all()
        self.assertEqual(stars_crud.get_order(self.db, order.order_id).status, "cancelled")

        second = self._qris()
        announce = AsyncMock()
        with patch("services.gopay_service.gopay_service.confirm_payment", new=AsyncMock(return_value=True)), \
             patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()), \
             patch.object(H, "announce_paid", new=announce):
            await H.cancel_order(_update(f"stp_cancel_{second.order_id}"), _ctx())
        self.db.expire_all()
        self.assertEqual(stars_crud.get_order(self.db, second.order_id).status, "paid")  # tidak dibatalkan
        self.assertEqual(announce.await_count, 1)

    async def test_batal_ditahan_bila_gateway_tidak_bisa_memastikan(self):
        order = self._qris()
        up = _update(f"stp_cancel_{order.order_id}")
        with patch("services.gopay_service.gopay_service.confirm_payment", new=AsyncMock(return_value=None)):
            await H.cancel_order(up, _ctx())
        self.db.expire_all()
        self.assertEqual(stars_crud.get_order(self.db, order.order_id).status, "pending")

    async def test_poller_mencocokkan_nominal_dan_mengabari(self):
        import main
        order = self._qris()
        txns = [
            {"transaction_id": "TX-SALAH", "amount": int(order.total_idr) + 1, "time": datetime.utcnow().isoformat() + "Z"},
            {"transaction_id": "TX-BENAR", "amount": int(order.total_idr), "time": datetime.utcnow().isoformat() + "Z"},
        ]
        bot_app = SimpleNamespace(bot=AsyncMock())
        announce = AsyncMock()
        main._stars_matched_tx_ids.clear()
        with patch("services.gopay_service.gopay_service.get_recent_transactions", new=AsyncMock(return_value=txns)), \
             patch("services.bot_runtime.bot_app", bot_app), \
             patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()), \
             patch.object(H, "announce_paid", new=announce):
            await main._job_check_pending_stars_payments()
            await main._job_check_pending_stars_payments()  # putaran kedua tidak mengabari lagi
        self.db.expire_all()
        self.assertEqual(stars_crud.get_order(self.db, order.order_id).status, "paid")
        self.assertEqual(announce.await_count, 1)

    async def test_poller_tidak_mengambil_pembayaran_lama_atau_nominal_lain(self):
        import main
        order = self._qris()
        old = (datetime.utcnow() - timedelta(hours=1)).isoformat() + "Z"
        txns = [{"transaction_id": "TX-LAMA", "amount": int(order.total_idr), "time": old}]
        main._stars_matched_tx_ids.clear()
        with patch("services.gopay_service.gopay_service.get_recent_transactions", new=AsyncMock(return_value=txns)), \
             patch("services.bot_runtime.bot_app", SimpleNamespace(bot=AsyncMock())):
            await main._job_check_pending_stars_payments()
        self.db.expire_all()
        self.assertEqual(stars_crud.get_order(self.db, order.order_id).status, "pending")

    async def test_poller_mengedaluwarsakan_dan_tetap_mengenali_bayar_telat(self):
        import main
        order = self._qris()
        self.db.query(StarsOrder).filter_by(id=order.id).update({"expired_at": datetime.utcnow() - timedelta(minutes=1)})
        self.db.commit()
        txns = [{"transaction_id": "TX-TELAT", "amount": int(order.total_idr), "time": datetime.utcnow().isoformat() + "Z"}]
        main._stars_matched_tx_ids.clear()
        announce = AsyncMock()
        with patch("services.gopay_service.gopay_service.get_recent_transactions", new=AsyncMock(return_value=txns)), \
             patch("services.bot_runtime.bot_app", SimpleNamespace(bot=AsyncMock())), \
             patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()), \
             patch.object(H, "announce_paid", new=announce):
            await main._job_check_pending_stars_payments()
        self.db.expire_all()
        row = stars_crud.get_order(self.db, order.order_id)
        self.assertEqual((row.status, row.late_payment), ("paid", True))
        self.assertEqual(announce.await_count, 1)


class InlineKeyboardMarkupStub:
    inline_keyboard = []


class TestAdmin(_Db):
    async def _press(self, data, uid=1, admin=True):
        up = _update(data, uid=uid)
        ctx = _ctx()
        with patch("bot.handlers.admin.is_admin", return_value=admin), \
             patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()) as send, \
             patch.object(H, "notify_admins", new=AsyncMock()):
            await H.admin_stars_callback(up, ctx)
        return up, send

    async def test_sudah_dikirim_menyelesaikan_dan_mengabari_user(self):
        order = stars_crud.create_balance_order(self.db, USER, "stars", 100, "teman_ku")
        up, send = await self._press(f"admin_stp_ok_{order.order_id}")
        self.db.expire_all()
        self.assertEqual(stars_crud.get_order(self.db, order.order_id).status, "completed")
        self.assertEqual(send.await_args.args[1], USER)
        self.assertIn("sudah dikirim", send.await_args.args[2])
        self.assertIn("@teman_ku", send.await_args.args[2])

    async def test_tolak_mengembalikan_dana(self):
        order = stars_crud.create_balance_order(self.db, USER, "premium", 3, "teman_ku")
        before = self.balance()
        up, send = await self._press(f"admin_stp_no_{order.order_id}")
        self.assertEqual(self.balance(), before + 228_000)
        self.assertIn("dikembalikan", send.await_args.args[2])

    async def test_satu_pesanan_satu_admin(self):
        order = stars_crud.create_balance_order(self.db, USER, "stars", 100, "teman_ku")
        await self._press(f"admin_stp_take_{order.order_id}", uid=1)
        up, _ = await self._press(f"admin_stp_no_{order.order_id}", uid=2)
        self.assertIn("sedang ditangani", up.callback_query.answer.await_args.args[0])
        self.db.expire_all()
        self.assertEqual(stars_crud.get_order(self.db, order.order_id).status, "paid")

    async def test_bukan_admin_ditolak_dan_klik_ganda_aman(self):
        order = stars_crud.create_balance_order(self.db, USER, "stars", 100, "teman_ku")
        up, _ = await self._press(f"admin_stp_ok_{order.order_id}", uid=99, admin=False)
        self.assertIn("ditolak", up.callback_query.answer.await_args.args[0].lower())
        self.assertEqual(stars_crud.get_order(self.db, order.order_id).status, "paid")
        await self._press(f"admin_stp_ok_{order.order_id}")
        up, _ = await self._press(f"admin_stp_ok_{order.order_id}")
        self.assertIn("sudah diproses", up.callback_query.answer.await_args.args[0])

    async def test_buka_pesanan_dari_antrean(self):
        order = stars_crud.create_balance_order(self.db, USER, "stars", 100, "teman_ku")
        up, _ = await self._press(f"admin_stp_open_{order.order_id}")
        self.assertIn("SIAP DIKIRIM", up.callback_query.message.reply_text.await_args.args[0])

    def test_tampilan_antrean_dan_dashboard(self):
        from bot.handlers import admin
        text, markup = H.build_admin_stars_view(self.db)
        self.assertIn("Tidak ada pesanan", text)
        order = stars_crud.create_balance_order(self.db, USER, "stars", 100, "teman_ku")
        text, markup = H.build_admin_stars_view(self.db)
        self.assertIn("100 Stars", text)
        self.assertIn("@teman_ku", text)
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn(f"admin_stp_open_{order.order_id}", data)
        dash = admin.build_admin_dashboard_text(self.db)
        self.assertIn("Stars/Premium Menunggu:</b> <b>1 Pesanan", dash)
        self.assertIn("Perlu Tindakan", dash)
        labels = [b.text for row in admin.get_admin_dashboard_keyboard(0, 0, 1).inline_keyboard for b in row]
        self.assertTrue(any("Stars & Premium (1)" in label for label in labels), labels)


class TestWiring(unittest.TestCase):
    def test_menu_utama_punya_tombol(self):
        from bot.keyboards.main_menu import get_main_menu_keyboard
        data = {b.callback_data for row in get_main_menu_keyboard().inline_keyboard for b in row}
        self.assertIn("menu_stars", data)

    def test_flow_guard_mengenal_stars(self):
        from bot.utils import flow_guard
        self.assertIn("stars", flow_guard._handlers())
        self.assertIn("stars", flow_guard.FLOW_LABELS)

    def test_pause_menjeda_pesanan_baru(self):
        from bot.utils import maintenance_guard as mg
        self.assertIn("menu_stars", mg.PAUSE_BLOCKED_CALLBACKS)
        self.assertIn("stp_confirm", mg.PAUSE_BLOCKED_CALLBACKS)
        self.assertIn("stars", mg.PAUSE_BLOCKED_COMMANDS)

    def test_router_dan_scheduler(self):
        start_src = (ROOT / "bot" / "handlers" / "start.py").read_text(encoding="utf-8")
        for token in ('data.startswith("stp_check_")', 'data.startswith("stp_cancel_")', 'data.startswith("admin_stp_")'):
            self.assertIn(token, start_src)
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("_job_check_pending_stars_payments,", main_src)
        self.assertIn("stars_conversation_handler", main_src)


if __name__ == "__main__":
    unittest.main()
