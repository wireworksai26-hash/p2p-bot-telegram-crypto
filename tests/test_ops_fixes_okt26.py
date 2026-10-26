"""Perbaikan operasional 26 Okt 2026:
- withdraw saldo user sampai ke admin (topik/DM) dan punya antrean di dashboard,
- referral tanpa batas 100 teman (bawaan),
- rekening/nominal Rupiah di pesan admin bisa disalin satu-satu.
"""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1,2")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from database import crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order, User, WithdrawRequest  # noqa: E402
from bot.handlers import admin  # noqa: E402
from bot.utils.admin_alert import bank_lines, idr_copy, order_detail_block  # noqa: E402

USER = 7001


class _Db(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, username="budi", full_name="Budi", balance_idr=Decimal("100000")))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def wd(self, amount=50_000, status="PENDING", minutes_ago=5, bank="BCA", number="1234567890", name="Budi S"):
        row = WithdrawRequest(telegram_id=USER, amount_idr=amount, bank_name=bank, account_number=number,
                              account_name=name, status=status,
                              created_at=datetime.utcnow() - timedelta(minutes=minutes_ago))
        self.db.add(row)
        self.db.commit()
        return row.id


# ───────────── 5. Rekening & Rupiah bisa disalin ─────────────
class CopyableAdminLines(unittest.TestCase):
    def test_nominal_rupiah_punya_angka_mentah_di_code(self):
        text = idr_copy(49503)
        self.assertIn("Rp 49.503", text)
        self.assertIn("<code>49503</code>", text)

    def test_hanya_nomor_rekening_di_code(self):
        text = bank_lines("BCA | 1234567890 | Budi Santoso")
        self.assertEqual(text.count("<code>"), 1)
        self.assertIn("<code>1234567890</code>", text)
        self.assertNotIn("<code>BCA", text)
        self.assertNotIn("Budi Santoso</code>", text)
        self.assertIn("BCA", text)
        self.assertIn("Budi Santoso", text)

    def test_karakter_html_di_nama_aman(self):
        text = bank_lines("Bank <X> | 123 | A&B")
        self.assertIn("&lt;X&gt;", text)
        self.assertIn("A&amp;B", text)

    def test_format_lama_tanpa_pemisah_tetap_tampil(self):
        self.assertIn("<code>0xabc</code>", bank_lines("0xabc"))
        self.assertIn("<code>-</code>", bank_lines(None))

    def test_blok_detail_order_jual(self):
        order = SimpleNamespace(
            order_id="ORD-1", order_type="sell", telegram_id=USER, crypto_symbol="USDT", network="BSC",
            crypto_amount=Decimal("3.1"), buyer_wallet="DANA | 081234567890 | Siti", total_idr=49503,
            payment_method=None, status="CRYPTO_CONFIRMED", created_at=None, target_crypto_symbol=None,
            target_network=None, target_crypto_amount=None)
        block = order_detail_block(order)
        self.assertIn("<code>49503</code>", block)
        self.assertIn("<code>081234567890</code>", block)
        self.assertNotIn("<code>DANA | 081234567890 | Siti</code>", block)


# ───────────── 1. Withdraw sampai ke admin ─────────────
class WithdrawNotification(_Db):
    async def test_permintaan_withdraw_lewat_jalur_topik_admin(self):
        from bot.handlers.withdraw import withdraw_confirm_handler
        req = SimpleNamespace(id=7, amount_idr=49503, bank_name="BCA", account_number="1234567890",
                              account_name="Budi S")
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(
            id=USER, username="budi", full_name="Budi"))
        context = SimpleNamespace(bot=AsyncMock(), user_data={"wd_bank_id": 1, "wd_amount": 49503})
        notif = AsyncMock()
        with patch("bot.handlers.withdraw.get_saved_bank_by_id", return_value=SimpleNamespace(id=1)), \
             patch("bot.handlers.withdraw.create_withdraw_request", return_value=req), \
             patch("bot.handlers.withdraw.get_user_balance", return_value=0), \
             patch("bot.handlers.withdraw.notify_admins", new=notif):
            await withdraw_confirm_handler(update, context)
        notif.assert_awaited_once()
        args, kwargs = notif.await_args
        self.assertEqual(kwargs["kind"], "topup")
        self.assertTrue(kwargs["butuh_tindakan"])
        text, markup = args[1], kwargs["reply_markup"]
        self.assertIn("WD-7", text)
        self.assertIn("<code>49503</code>", text)
        self.assertIn("<code>1234567890</code>", text)
        data = {b.callback_data for row in markup.inline_keyboard for b in row}
        self.assertEqual(data, {"admin_wd_take_7", "admin_wd_ok_7", "admin_wd_no_7"})

    async def test_admin_lain_diberi_tahu_lewat_jalur_yang_sama(self):
        from bot.handlers.withdraw import admin_withdraw_callback
        rid = self.wd()
        query = MagicMock()
        query.data = f"admin_wd_take_{rid}"
        query.from_user = SimpleNamespace(id=1)
        query.answer = AsyncMock()
        query.message = SimpleNamespace(text_html="x")
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(bot=AsyncMock())
        notif = AsyncMock()
        with patch("bot.handlers.admin.is_admin", return_value=True), \
             patch("bot.handlers.withdraw.notify_admins", new=notif):
            await admin_withdraw_callback(update, context)
        self.assertEqual(notif.await_args.kwargs["kind"], "topup")
        self.assertIn(f"WD-{rid}", notif.await_args.args[1])


class WithdrawQueue(_Db):
    def test_ringkasan_dan_urutan_terlama_dulu(self):
        a = self.wd(10_000, minutes_ago=30)
        b = self.wd(20_000, minutes_ago=5)
        self.wd(99_000, status="PAID")
        self.wd(77_000, status="REJECTED")
        self.assertEqual(crud.get_pending_withdraw_summary(self.db), (2, 30_000))
        self.assertEqual([r.id for r in crud.get_pending_withdraws(self.db)], [a, b])

    def test_tampilan_antrean_kosong(self):
        text, markup = admin.build_admin_withdraws_view(self.db)
        self.assertIn("Tidak ada withdraw", text)
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(data, ["admin_panel_withdraws", "admin_panel_main"])

    def test_tampilan_antrean_berisi_tombol_buka(self):
        a = self.wd(10_000, minutes_ago=30)
        b = self.wd(20_000, minutes_ago=2)
        text, markup = admin.build_admin_withdraws_view(self.db)
        self.assertIn(f"WD-{a}", text)
        self.assertIn("Rp 30.000", text)
        self.assertIn("30 mnt lalu", text)
        data = [b_.callback_data for row in markup.inline_keyboard for b_ in row]
        self.assertIn(f"admin_wd_open_{a}", data)
        self.assertIn(f"admin_wd_open_{b}", data)

    def test_dashboard_menunjukkan_withdraw_menunggu(self):
        self.assertIn("Withdraw Menunggu:</b> <b>0 Permintaan", admin.build_admin_dashboard_text(self.db))
        self.wd(50_000)
        text = admin.build_admin_dashboard_text(self.db)
        self.assertIn("Withdraw Menunggu:</b> <b>1 Permintaan", text)
        self.assertIn("Rp 50.000", text)
        self.assertIn("Perlu Tindakan", text)  # hanya karena ada withdraw, tanpa order pending
        labels = [b.text for row in admin.get_admin_dashboard_keyboard(0, 1).inline_keyboard for b in row]
        self.assertTrue(any("Withdraw Saldo User (1)" in label for label in labels), labels)

    def test_dashboard_tetap_tampil_bila_antrean_withdraw_gagal_dibaca(self):
        with patch.object(admin.crud, "get_pending_withdraw_summary", side_effect=RuntimeError("db")):
            self.assertIn("Withdraw Menunggu:</b> <b>0 Permintaan", admin.build_admin_dashboard_text(self.db))
            self.assertEqual(admin._pending_withdraws(self.db), (0, 0))

    async def test_buka_withdraw_mengirim_kartu_berisi_tombol(self):
        from bot.handlers.withdraw import admin_withdraw_open
        rid = self.wd(number="9988776655")
        query = MagicMock()
        query.data = f"admin_wd_open_{rid}"
        query.from_user = SimpleNamespace(id=1)
        query.answer = AsyncMock()
        query.message = SimpleNamespace(reply_text=AsyncMock())
        with patch("bot.handlers.admin.is_admin", return_value=True):
            await admin_withdraw_open(SimpleNamespace(callback_query=query), SimpleNamespace(bot=AsyncMock()))
        text = query.message.reply_text.await_args.args[0]
        self.assertIn(f"WD-{rid}", text)
        self.assertIn("<code>9988776655</code>", text)
        self.assertIn("@budi", text)
        data = {b.callback_data for row in query.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row}
        self.assertIn(f"admin_wd_ok_{rid}", data)

    async def test_buka_withdraw_yang_sudah_diproses_atau_bukan_admin_ditolak(self):
        from bot.handlers.withdraw import admin_withdraw_open
        rid = self.wd(status="PAID")
        query = MagicMock()
        query.data = f"admin_wd_open_{rid}"
        query.from_user = SimpleNamespace(id=1)
        query.answer = AsyncMock()
        query.message = SimpleNamespace(reply_text=AsyncMock())
        with patch("bot.handlers.admin.is_admin", return_value=True):
            await admin_withdraw_open(SimpleNamespace(callback_query=query), SimpleNamespace(bot=AsyncMock()))
        query.message.reply_text.assert_not_awaited()
        self.assertIn("sudah diproses", query.answer.await_args.args[0])
        with patch("bot.handlers.admin.is_admin", return_value=False):
            await admin_withdraw_open(SimpleNamespace(callback_query=query), SimpleNamespace(bot=AsyncMock()))
        query.message.reply_text.assert_not_awaited()

    def test_tombol_buka_dan_panel_withdraw_terdaftar_di_router(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "bot" / "handlers" / "start.py").read_text(encoding="utf-8")
        self.assertIn('data.startswith("admin_wd_open_")', source)
        self.assertLess(source.index('"admin_wd_open_"'), source.index('"admin_wd_ok_"'))


# ───────────── 2. Referral tanpa batas 100 ─────────────
class ReferralNoCap(_Db):
    def _friends(self, n):
        for i in range(n):
            self.db.add(User(telegram_id=900_000 + i, username=f"f{i}", balance_idr=Decimal("0")))
        self.db.commit()

    def test_bawaan_tanpa_batas_teman_ke_101_tetap_tercatat(self):
        from services.referral_rewards import get_referral_settings
        self.assertEqual(get_referral_settings(self.db)["maxref"], 0)
        self._friends(105)
        made = [crud.create_referral(self.db, USER, 900_000 + i) for i in range(105)]
        self.assertTrue(all(made))
        from database.models import Referral
        self.assertEqual(self.db.query(Referral).filter_by(referrer_id=USER).count(), 105)

    def test_batas_yang_diatur_admin_tetap_berlaku(self):
        crud.set_referral_config(self.db, "max_referrals_per_user", "2")
        self._friends(3)
        self.assertIsNotNone(crud.create_referral(self.db, USER, 900_000))
        self.assertIsNotNone(crud.create_referral(self.db, USER, 900_001))
        self.assertIsNone(crud.create_referral(self.db, USER, 900_002))

    def test_panel_admin_menerima_nol_sebagai_tanpa_batas(self):
        ok, value = admin.parse_ref_setting_value("maxref", "0")
        self.assertEqual((ok, value), (True, 0))
        self.assertIn("tanpa batas", admin.format_ref_setting("maxref", 0))
        self.assertIn(0, admin.REFERRAL_ADMIN_SETTINGS["maxref"]["presets"])
        self.assertEqual(admin.format_ref_setting("maxref", 250), "250 teman")


if __name__ == "__main__":
    unittest.main()


# ───────────── Riwayat topup & withdraw ke topik "Topup & Withdraw Saldo" ─────────────
class SaldoHistory(_Db):
    async def test_riwayat_topup_masuk_topik_topup(self):
        from bot.utils import saldo_log
        topup = SimpleNamespace(topup_id="TOPUP-1", telegram_id=USER, amount_idr=100_300, mdr_idr=300)
        send = AsyncMock(return_value=True)
        with patch.object(saldo_log, "kirim_ke_topik", new=send):
            await saldo_log.log_topup_success(AsyncMock(), topup, 100_000, 150_000, False, "auto", self.db)
        self.assertEqual(send.await_args.kwargs["kind"], "topup")
        text = send.await_args.kwargs["text"]
        for part in ("TOPUP-1", "Rp 100.000", "Rp 150.000", "pajak QRIS Rp 300", "@budi", "otomatis"):
            self.assertIn(part, text)

    async def test_riwayat_withdraw_selesai_dan_ditolak(self):
        from bot.utils import saldo_log
        send = AsyncMock(return_value=True)
        with patch.object(saldo_log, "kirim_ke_topik", new=send):
            await saldo_log.log_withdraw_event(AsyncMock(), 9, USER, 49_503, "BCA", "123", "Budi", "PAID", 1, self.db)
            await saldo_log.log_withdraw_event(AsyncMock(), 9, USER, 49_503, "BCA", "123", "Budi", "REJECTED", 2, self.db)
        paid, rejected = (c.kwargs["text"] for c in send.await_args_list)
        self.assertIn("SUDAH DITRANSFER", paid)
        self.assertIn("<code>49503</code>", paid)
        self.assertIn("DITOLAK", rejected)

    async def test_gagal_kirim_riwayat_tidak_melempar(self):
        from bot.utils import saldo_log
        with patch.object(saldo_log, "kirim_ke_topik", new=AsyncMock(side_effect=RuntimeError("x"))):
            await saldo_log.log_withdraw_event(AsyncMock(), 1, USER, 1, "B", "1", "N", "PAID")
            await saldo_log.log_topup_success(AsyncMock(), SimpleNamespace(topup_id="T", telegram_id=USER,
                                              amount_idr=1, mdr_idr=0), 1, 1)

    async def test_withdraw_diselesaikan_admin_dicatat_ke_riwayat(self):
        from bot.handlers.withdraw import admin_withdraw_callback
        rid = self.wd()
        query = MagicMock()
        query.data = f"admin_wd_ok_{rid}"
        query.from_user = SimpleNamespace(id=1)
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = SimpleNamespace(text_html="x")
        context = SimpleNamespace(bot=AsyncMock())
        log = AsyncMock()
        with patch("bot.handlers.admin.is_admin", return_value=True), \
             patch("bot.handlers.withdraw.safe_send_message", new=AsyncMock()), \
             patch("bot.utils.saldo_log.log_withdraw_event", new=log):
            await admin_withdraw_callback(SimpleNamespace(callback_query=query), context)
        self.assertEqual(log.await_args.args[7], "PAID")
        self.assertEqual(log.await_args.args[1], rid)

    def test_alias_withdraw_menuju_topik_topup(self):
        from bot.utils.telegram_utils import normalisasi_kind
        self.assertEqual(normalisasi_kind("withdraw"), "topup")
        self.assertEqual(normalisasi_kind("topup"), "topup")
