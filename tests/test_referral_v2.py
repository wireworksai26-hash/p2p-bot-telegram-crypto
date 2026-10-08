"""Program referral v2: reward bertingkat, bagi hasil fee, masa tahan 24 jam, diskon teman.

Ketentuan (default, semua bisa diubah admin):
  Pengundang: Rp 1.000 transaksi ke-1 teman, Rp 500 transaksi ke-2, 7% dari fee tiap
              transaksi teman (maks. 10 transaksi per teman), masuk saldo setelah 24 jam.
  Teman     : diskon fee Rp 1.000 di transaksi pertama.
Juga menjaga bug lama: referral tidak boleh nyangkut PENDING walau teman sudah transaksi.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
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
    "ADMIN_CHAT_IDS": "999",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from database import crud
from database.connection import Base, SessionLocal, engine
from database.models import Order, Referral, ReferralEarning, User
from services import referral_rewards as rr
from services.referral_service import process_referral_rewards

REFERRER, FRIEND = 1001, 2001


def _order(oid, uid=FRIEND, nominal=40_000, fee=3_000, status="completed", completed_at=None):
    return Order(
        order_id=oid, telegram_id=uid, order_type="buy", crypto_symbol="USDT", network="BSC",
        crypto_amount=Decimal("2"), price_per_unit=16000, nominal_idr=nominal, fee_idr=fee,
        total_idr=nominal, status=status, completed_at=completed_at or datetime.utcnow())


class _App:
    """Meniru telegram Application: tidak punya send_message, hanya `.bot`."""

    def __init__(self):
        self.bot = AsyncMock()
        self.bot.send_message = AsyncMock()

    def texts(self, user_id=None):
        return " ".join(
            c.kwargs.get("text", "") for c in self.bot.send_message.await_args_list
            if user_id is None or c.kwargs.get("chat_id") == user_id
        )


class Base2(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([User(telegram_id=u, balance_idr=0) for u in (REFERRER, FRIEND, 2002)])
        db.commit()
        crud.create_referral(db, REFERRER, FRIEND)
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def run_orders(self, *orders):
        db = SessionLocal()
        try:
            for o in orders:
                db.add(o)
                db.commit()
                rr.process_referral_for_referee(db, o.telegram_id)
        finally:
            db.close()

    def balance(self, uid):
        db = SessionLocal()
        try:
            return Decimal(str(db.query(User).filter(User.telegram_id == uid).one().balance_idr or 0))
        finally:
            db.close()

    def earnings(self, kind=None):
        db = SessionLocal()
        try:
            q = db.query(ReferralEarning).filter(ReferralEarning.kind != rr.KIND_COUNTED)
            if kind:
                q = q.filter(ReferralEarning.kind == kind)
            return q.order_by(ReferralEarning.id).all()
        finally:
            db.close()

    def referral(self):
        db = SessionLocal()
        try:
            r = db.query(Referral).filter(Referral.referee_id == FRIEND).one()
            db.expunge(r)
            return r
        finally:
            db.close()

    def set_cfg(self, **kv):
        db = SessionLocal()
        try:
            for name, v in kv.items():
                key = rr.SETTING_DEFS[name][0] if name in rr.SETTING_DEFS else name
                crud.set_referral_config(db, key, str(v))
        finally:
            db.close()

    def cfg(self, key):
        db = SessionLocal()
        try:
            return crud.get_referral_config(db, key)
        finally:
            db.close()


class TestTieredRewards(Base2):
    def test_transaksi_pertama_dan_kedua(self):
        self.run_orders(_order("O1"))
        first = self.earnings(rr.KIND_FIRST)
        self.assertEqual([(e.amount_idr, e.status, e.beneficiary_id) for e in first], [(1000, "HELD", REFERRER)])
        self.assertEqual([e.amount_idr for e in self.earnings(rr.KIND_SHARE)], [210])  # 7% x Rp 3.000
        self.assertEqual(self.referral().status, "COMPLETED")
        self.assertEqual(self.referral().reward_idr, 1210)
        self.assertEqual(self.balance(REFERRER), Decimal("0"), "ditahan 24 jam, belum masuk saldo")

        self.run_orders(_order("O2", fee=4_000))
        self.assertEqual([e.amount_idr for e in self.earnings(rr.KIND_SECOND)], [500])
        self.assertEqual([e.amount_idr for e in self.earnings(rr.KIND_SHARE)], [210, 280])
        self.assertEqual(self.referral().reward_idr, 1210 + 500 + 280)

        self.run_orders(_order("O3"))
        self.assertEqual(len(self.earnings(rr.KIND_FIRST)), 1)
        self.assertEqual(len(self.earnings(rr.KIND_SECOND)), 1)
        self.assertEqual(len(self.earnings(rr.KIND_SHARE)), 3)

    def test_diskon_teman_masuk_saldo_sekali_di_transaksi_pertama(self):
        self.run_orders(_order("O1"))
        self.assertEqual(self.balance(FRIEND), Decimal("1000"))
        self.run_orders(_order("O2"))
        self.assertEqual(self.balance(FRIEND), Decimal("1000"))
        bonus = self.earnings(rr.KIND_BONUS)
        self.assertEqual([(e.amount_idr, e.status, e.beneficiary_id) for e in bonus], [(1000, "RELEASED", FRIEND)])

    def test_bagi_hasil_dibatasi_10_transaksi_per_teman(self):
        self.run_orders(*[_order(f"O{i}") for i in range(1, 13)])
        self.assertEqual(len(self.earnings(rr.KIND_SHARE)), 10)

    def test_idempotent_diproses_berulang(self):
        self.run_orders(_order("O1"))
        db = SessionLocal()
        try:
            for _ in range(3):
                rr.process_referral_for_referee(db, FRIEND)
        finally:
            db.close()
        self.assertEqual(len(self.earnings()), 3)  # bonus, reward ke-1, bagi hasil
        self.assertEqual(self.balance(FRIEND), Decimal("1000"))

    def test_order_yang_sama_tidak_dihitung_dua_proses(self):
        self.run_orders(_order("O1"))
        db1, db2 = SessionLocal(), SessionLocal()
        try:
            o = db1.query(Order).filter(Order.order_id == "O1").one()
            ref = db1.query(Referral).one()
            n = rr._accrue_order(db2, ref, o, datetime.utcnow(), rr.get_referral_settings(db2))
        finally:
            db1.close()
            db2.close()
        self.assertEqual(n, 0)
        self.assertEqual(self.referral().tx_count, 1)

    def test_pengaturan_admin_dipakai(self):
        self.set_cfg(reward=2500, reward2=700, bonus=0, share=10, sharemax=1)
        self.run_orders(_order("O1"), _order("O2"))
        self.assertEqual([e.amount_idr for e in self.earnings(rr.KIND_FIRST)], [2500])
        self.assertEqual([e.amount_idr for e in self.earnings(rr.KIND_SECOND)], [700])
        self.assertEqual([e.amount_idr for e in self.earnings(rr.KIND_SHARE)], [300])
        self.assertEqual(self.earnings(rr.KIND_BONUS), [])

    def test_tanpa_setup_admin_default_sesuai_ketentuan(self):
        db = SessionLocal()
        try:
            cfg = rr.get_referral_settings(db)
        finally:
            db.close()
        self.assertEqual(
            (cfg["reward"], cfg["reward2"], cfg["bonus"], cfg["share"], cfg["sharemax"], cfg["hold"]),
            (1000, 500, 1000, 7.0, 10, 24))

    def test_min_trade_transaksi_kecil_tidak_dihitung(self):
        self.set_cfg(min_trade=50_000)
        self.run_orders(_order("SMALL", nominal=20_000))
        self.assertEqual((self.referral().status, self.referral().tx_count), ("PENDING", 0))
        self.run_orders(_order("BIG", nominal=60_000))
        self.assertEqual(len(self.earnings(rr.KIND_FIRST)), 1, "transaksi besar jadi transaksi ke-1")

    def test_program_dinonaktifkan(self):
        self.set_cfg(referral_enabled="false")
        self.run_orders(_order("O1"))
        self.assertEqual(self.earnings(), [])
        self.assertEqual(self.referral().status, "PENDING")

    def test_batas_fee_total_payout_tidak_melebihi_fee(self):
        self.set_cfg(reward=5000)
        self.run_orders(_order("O1", fee=3_000))
        total = sum(e.amount_idr for e in self.earnings())
        self.assertEqual(total, 3_000, "bonus 1.000 + reward dipotong 2.000 + bagi hasil 0")

    def test_batas_fee_bisa_dimatikan(self):
        self.set_cfg(reward=5000, referral_fee_guard="false")
        self.run_orders(_order("O1", fee=3_000))
        self.assertEqual([e.amount_idr for e in self.earnings(rr.KIND_FIRST)], [5000])


class TestHoldAndRelease(Base2):
    def test_reward_masuk_saldo_setelah_masa_tahan(self):
        done = datetime.utcnow()
        self.run_orders(_order("O1", completed_at=done))
        db = SessionLocal()
        try:
            self.assertEqual(rr.release_due_earnings(db, now=done + timedelta(hours=23)), {})
            self.assertEqual(self.balance(REFERRER), Decimal("0"))
            self.assertEqual(rr.release_due_earnings(db, now=done + timedelta(hours=25)), {REFERRER: 1210})
            self.assertEqual(rr.release_due_earnings(db, now=done + timedelta(hours=26)), {}, "tidak boleh dobel")
            self.assertEqual(crud.get_referral_stats(db, REFERRER)["held_reward"], 0)
        finally:
            db.close()
        self.assertEqual(self.balance(REFERRER), Decimal("1210"))

    def test_masa_tahan_dapat_diatur(self):
        self.set_cfg(hold=0)
        self.run_orders(_order("O1"))
        db = SessionLocal()
        try:
            self.assertEqual(rr.release_due_earnings(db), {REFERRER: 1210})
        finally:
            db.close()


class TestHookAndSweeper(Base2):
    def test_hook_update_order_status_mencatat_reward(self):
        db = SessionLocal()
        db.add(_order("O1", status="paid"))
        db.commit()
        crud.update_order_status(db, "O1", "completed", completed_at=datetime.utcnow())
        db.close()
        self.assertEqual(len(self.earnings(rr.KIND_FIRST)), 1)

    def test_sweeper_menyelesaikan_referral_lama_yang_nyangkut_dan_memberi_tahu(self):
        # Referral dibuat 3 hari lalu, teman transaksi 2 hari lalu tetapi referral masih PENDING.
        db = SessionLocal()
        db.query(Referral).filter(Referral.referee_id == FRIEND).update(
            {"created_at": datetime.utcnow() - timedelta(days=3)}
        )
        db.add(_order("OLD", completed_at=datetime.utcnow() - timedelta(days=2)))
        db.commit()
        db.close()
        self.assertEqual(self.referral().status, "PENDING")

        app = _App()
        hasil = asyncio.run(process_referral_rewards(app))
        self.assertEqual(hasil["accrued"], 3)
        self.assertEqual(self.referral().status, "COMPLETED")
        self.assertEqual(self.balance(REFERRER), Decimal("1210"), "masa tahan sudah lewat -> langsung cair")
        self.assertEqual(self.balance(FRIEND), Decimal("1000"))
        self.assertIn("Referral Reward", app.texts(REFERRER))
        self.assertIn("Masuk Saldo", app.texts(REFERRER))
        self.assertIn("Diskon fee referral", app.texts(FRIEND))

        sent = app.bot.send_message.await_count
        again = asyncio.run(process_referral_rewards(app))
        self.assertEqual((again["accrued"], again["released"]), (0, 0))
        self.assertEqual(app.bot.send_message.await_count, sent, "tidak ada notifikasi/pembayaran dobel")

    def test_transaksi_sebelum_referral_dibuat_tidak_dihitung(self):
        """User lama yang sudah transaksi tidak menghasilkan reward bila baru klik referral."""
        # Order teman selesai 2 hari lalu SEBELUM referral dibuat
        db = SessionLocal()
        db.add(_order("PREV", completed_at=datetime.utcnow() - timedelta(days=2)))
        # Referral dibuat SEKARANG (created_at = now)
        db.commit()
        db.close()

        # Sweeper dijalankan
        app = _App()
        hasil = asyncio.run(process_referral_rewards(app))
        self.assertEqual(hasil["accrued"], 0, "transaksi sebelum referral dibuat tidak boleh dihitung")
        self.assertEqual(self.referral().status, "PENDING")
        self.assertEqual(self.referral().reward_idr, 0)
        self.assertEqual(self.balance(REFERRER), Decimal("0"))

    def test_self_referral_ditolak_di_crud(self):
        db = SessionLocal()
        try:
            self.assertIsNone(crud.create_referral(db, REFERRER, REFERRER))
        finally:
            db.close()

    def test_referral_lama_sudah_dibayar_skema_lama_tidak_dihitung_ulang(self):
        cut = datetime.utcnow() - timedelta(hours=1)
        db = SessionLocal()
        db.query(Referral).update({
            "status": "COMPLETED", "legacy_until": cut, "tx_count": 1, "reward_idr": 5000,
            "created_at": cut - timedelta(days=2),
        })
        db.add(_order("PRE", completed_at=cut - timedelta(days=1)))
        db.add(_order("POST", completed_at=datetime.utcnow()))
        db.commit()
        rr.process_referral_for_referee(db, FRIEND)
        db.close()
        self.assertEqual(self.earnings(rr.KIND_FIRST), [])
        self.assertEqual(self.earnings(rr.KIND_BONUS), [])
        self.assertEqual([e.amount_idr for e in self.earnings(rr.KIND_SECOND)], [500])
        self.assertEqual(self.referral().reward_idr, 5000 + 500 + 210)


class TestUserText(Base2):
    def _text(self):
        from bot.handlers.referral import build_referral_rules_text
        db = SessionLocal()
        try:
            return build_referral_rules_text(rr.get_referral_settings(db))
        finally:
            db.close()

    def test_teks_menu_user_sesuai_ketentuan(self):
        text = self._text()
        for line in (
            "Rp 1.000 saat teman menyelesaikan transaksi pertama (beli/jual/convert)",
            "Rp 500 saat teman menyelesaikan transaksi kedua",
            "Diskon fee Rp 1.000 di transaksi pertama",
            "7% dari fee setiap transaksi temanmu masuk ke saldo bot kamu (maks. 10 transaksi teman)",
            "ditarik ke rekening/e-wallet (min. Rp 10.000)",
            "Reward masuk setelah transaksi selesai dan melewati masa tahan 24 jam",
        ):
            self.assertIn(line, text)
        self.assertNotIn(chr(92) + "n", text)

    def test_teks_user_mengikuti_pengaturan_admin(self):
        self.set_cfg(reward=1500, reward2=0, share=5, sharemax=3, hold=48, bonus=2000)
        text = self._text()
        self.assertIn("Rp 1.500 saat teman menyelesaikan transaksi pertama", text)
        self.assertIn("5% dari fee", text)
        self.assertIn("maks. 3 transaksi teman", text)
        self.assertIn("masa tahan 48 jam", text)
        self.assertIn("Diskon fee Rp 2.000", text)


class TestAdminPanel(Base2):
    def _cb(self, data):
        from bot.handlers.admin import admin_panel_callback
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=999), data=data, message=SimpleNamespace(chat_id=999),
            edit_message_text=AsyncMock(), answer=AsyncMock())
        ctx = SimpleNamespace(user_data={}, bot=AsyncMock())

        async def run():
            with patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()), \
                 patch("bot.handlers.admin.is_admin", return_value=True):
                await admin_panel_callback(SimpleNamespace(callback_query=query), ctx)
        asyncio.run(run())
        return query, ctx

    def test_semua_ketentuan_punya_tombol_pilih_dan_set(self):
        from bot.handlers.admin import REFERRAL_ADMIN_SETTINGS
        for name, meta in REFERRAL_ADMIN_SETTINGS.items():
            q, _ = self._cb(f"admin_ref_pick_{name}")
            self.assertIn(meta["title"].upper(), q.edit_message_text.call_args.kwargs["text"])
            value = meta["presets"][-1]
            self._cb(f"admin_ref_set_{name}_{value}")
            self.assertEqual(self.cfg(rr.SETTING_DEFS[name][0]), str(value), name)

    def test_panel_utama_memuat_tombol_semua_ketentuan(self):
        q, _ = self._cb("admin_panel_referral")
        markup = q.edit_message_text.call_args.kwargs["reply_markup"]
        data = {b.callback_data for row in markup.inline_keyboard for b in row}
        for name in ("reward", "reward2", "bonus", "share", "sharemax", "hold", "min_trade", "maxref"):
            self.assertIn(f"admin_ref_pick_{name}", data)
        text = q.edit_message_text.call_args.kwargs["text"]
        self.assertIn("Transaksi ke-1 teman", text)
        self.assertIn("Masa tahan reward", text)

    def test_tombol_kustom_menunggu_input(self):
        _, ctx = self._cb("admin_ref_custom_share")
        self.assertEqual(ctx.user_data["admin_awaiting_ref_cfg"], "share")

    def test_custom_persen_desimal_dan_validasi(self):
        from bot.handlers.admin import admin_referral_text_handler

        def send(text):
            msg = SimpleNamespace(text=text, reply_text=AsyncMock())
            ctx = SimpleNamespace(user_data={"admin_awaiting_ref_cfg": "share"})

            async def run():
                with patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()), \
                     patch("bot.handlers.admin.is_admin", return_value=True):
                    return await admin_referral_text_handler(
                        SimpleNamespace(effective_user=SimpleNamespace(id=999), message=msg), ctx)
            asyncio.run(run())
            return ctx

        ctx = send("7,5%")
        self.assertEqual(self.cfg("fee_share_pct"), "7.5")
        self.assertNotIn("admin_awaiting_ref_cfg", ctx.user_data)

        ctx = send("150")
        self.assertEqual(self.cfg("fee_share_pct"), "7.5", "di luar batas ditolak")
        self.assertEqual(ctx.user_data.get("admin_awaiting_ref_cfg"), "share", "tetap menunggu input")

    def test_perintah_setreferral_nama_baru(self):
        from bot.handlers.admin import setreferral_handler

        def run_cmd(*args):
            msg = SimpleNamespace(reply_text=AsyncMock())

            async def run():
                with patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()), \
                     patch("bot.handlers.admin.is_admin", return_value=True):
                    await setreferral_handler(
                        SimpleNamespace(effective_user=SimpleNamespace(id=999), message=msg),
                        SimpleNamespace(args=list(args)))
            asyncio.run(run())
            return msg.reply_text.call_args.args[0]

        run_cmd("reward2", "750")
        run_cmd("share", "8")
        run_cmd("hold", "12")
        run_cmd("sharemax", "5")
        self.assertEqual(
            (self.cfg("reward_second_tx_idr"), self.cfg("fee_share_pct"), self.cfg("reward_hold_hours"),
             self.cfg("fee_share_max_tx")), ("750", "8", "12", "5"))
        self.assertIn("tidak valid", run_cmd("nope", "1"))
        self.assertIn("antara", run_cmd("share", "500"))


if __name__ == "__main__":
    unittest.main()
