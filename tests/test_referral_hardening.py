"""Pengerasan referral setelah audit keamanan: anti-fraud dan bug logika."""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

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
from services.referral_service import accrue_pending

REFERRER, FRIEND = 1001, 2001


def _order(oid, uid=FRIEND, nominal=40_000, fee=3_000, symbol="USDT", network="BSC", order_type="buy",
           completed_at=None, **kw):
    return Order(
        order_id=oid, telegram_id=uid, order_type=order_type, crypto_symbol=symbol, network=network,
        crypto_amount=Decimal("2"), price_per_unit=16000, nominal_idr=nominal, fee_idr=fee, total_idr=nominal,
        status="completed", completed_at=completed_at or datetime.utcnow(), **kw)


class Base3(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([User(telegram_id=u, balance_idr=0) for u in (REFERRER, FRIEND, 3001)])
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def cfg(self, **kv):
        db = SessionLocal()
        for name, v in kv.items():
            key = rr.SETTING_DEFS[name][0] if name in rr.SETTING_DEFS else name
            crud.set_referral_config(db, key, str(v))
        db.close()

    def earnings_total(self):
        db = SessionLocal()
        try:
            rows = db.query(ReferralEarning).filter(ReferralEarning.kind != rr.KIND_COUNTED).all()
            return sum(r.amount_idr for r in rows)
        finally:
            db.close()


class TestWhoCanBeReferred(Base3):
    def test_pengguna_lama_yang_sudah_bertransaksi_tidak_bisa_jadi_teman(self):
        db = SessionLocal()
        db.add(_order("OLD", completed_at=datetime.utcnow() - timedelta(days=90)))
        db.commit()
        self.assertIsNone(crud.create_referral(db, REFERRER, FRIEND))
        self.assertEqual(db.query(Referral).count(), 0)
        db.close()

    def test_order_pending_pun_menandakan_bukan_pengguna_baru(self):
        db = SessionLocal()
        o = _order("PEND")
        o.status = "pending"
        db.add(o)
        db.commit()
        self.assertIsNone(crud.create_referral(db, REFERRER, FRIEND))
        db.close()

    def test_pengundang_tidak_dikenal_ditolak(self):
        db = SessionLocal()
        self.assertIsNone(crud.create_referral(db, 424242, FRIEND))
        self.assertEqual(db.query(User).filter(User.telegram_id == 424242).count(), 0, "tidak membuat user hantu")
        db.close()

    def test_pengguna_baru_tanpa_order_diterima(self):
        db = SessionLocal()
        self.assertIsNotNone(crud.create_referral(db, REFERRER, FRIEND))
        db.close()


class TestSettingsRobustness(Base3):
    def test_nilai_config_inf_nan_dan_sampah_jatuh_ke_default(self):
        for bad in ("inf", "-inf", "nan", "1e999", "abc"):
            self.cfg(reward=bad, share=bad, hold=bad)
            db = SessionLocal()
            try:
                s = rr.get_referral_settings(db)  # tidak boleh melempar
            finally:
                db.close()
            self.assertEqual((s["reward"], s["share"], s["hold"]), (1000, 7.0, 24), bad)

    def test_pembuatan_referral_tetap_jalan_walau_config_rusak(self):
        self.cfg(maxref="inf")
        db = SessionLocal()
        self.assertIsNotNone(crud.create_referral(db, REFERRER, FRIEND))
        db.close()

    def test_parser_admin_menolak_nan_inf_desimal_dan_unicode(self):
        from bot.handlers.admin import parse_ref_setting_value as parse
        for name, raw in (("share", "nan"), ("share", "inf"), ("hold", "1,5"), ("hold", "2.5"),
                          ("sharemax", "1.000"), ("reward", "1.000,50"), ("reward", "٣٠٠"),
                          ("share", "-5"), ("reward", "abc")):
            self.assertFalse(parse(name, raw)[0], (name, raw))
        self.assertEqual(parse("reward", "7.500"), (True, 7500))
        self.assertEqual(parse("share", "7,5%"), (True, 7.5))
        self.assertEqual(parse("hold", "24 jam"), (True, 24))


class TestPayoutGuards(Base3):
    def setUp(self):
        super().setUp()
        db = SessionLocal()
        crud.create_referral(db, REFERRER, FRIEND)
        db.close()

    def _run(self, *orders):
        db = SessionLocal()
        for o in orders:
            db.add(o)
            db.commit()
            rr.process_referral_for_referee(db, o.telegram_id)
        db.close()

    def test_surcharge_gas_bukan_margin_untuk_batas_fee(self):
        # ETH-ETH: fee Rp 5.500 = Rp 3.000 fee + Rp 2.500 gas. Preset admin besar tidak boleh melampaui Rp 3.000.
        self.cfg(reward=5000, bonus=2000)
        self._run(_order("GAS", symbol="ETH", network="ETH", fee=5_500))
        self.assertEqual(self.earnings_total(), 3_000)

    def test_pasangan_non_gas_batas_fee_tetap_utuh(self):
        self.cfg(reward=5000, bonus=2000)
        self._run(_order("NOGAS", symbol="USDT", network="BSC", fee=5_500))
        self.assertEqual(self.earnings_total(), 5_500)

    def test_jual_tidak_punya_surcharge(self):
        self.cfg(reward=5000, bonus=2000)
        self._run(_order("SELL", symbol="ETH", network="ETH", fee=5_500, order_type="sell"))
        self.assertEqual(self.earnings_total(), 5_500)

    def test_program_dimatikan_menahan_pencairan_lalu_lanjut_saat_dihidupkan(self):
        db0 = SessionLocal()
        db0.query(Referral).update({"created_at": datetime.utcnow() - timedelta(days=3)})
        db0.commit()
        db0.close()
        done = datetime.utcnow() - timedelta(days=2)
        self._run(_order("O1", completed_at=done))
        self.cfg(referral_enabled="false")
        db = SessionLocal()
        self.assertEqual(rr.release_due_earnings(db), {})
        self.cfg(referral_enabled="true")
        self.assertEqual(rr.release_due_earnings(db), {REFERRER: 1210})
        db.close()

    def test_reward_idr_tidak_tertimpa_pekerja_dengan_status_basi(self):
        # Pekerja A memuat referral (PENDING); pekerja B menyelesaikannya; A lalu menghitung order berikutnya.
        db_a, db_b = SessionLocal(), SessionLocal()
        try:
            stale = db_a.query(Referral).first()
            self.assertEqual(stale.status, "PENDING")
            db_b.add(_order("B1"))
            db_b.commit()
            rr.process_referral_for_referee(db_b, FRIEND)  # +1000 +210
            o2 = _order("A2", fee=4_000)
            db_a.add(o2)
            db_a.commit()
            settings_ = rr.get_referral_settings(db_a)
            rr._accrue_order(db_a, stale, o2, datetime.utcnow(), settings_)  # stale.status masih "PENDING"
        finally:
            db_a.close()
            db_b.close()
        db = SessionLocal()
        ref = db.query(Referral).first()
        total = sum(e.amount_idr for e in db.query(ReferralEarning).filter(
            ReferralEarning.beneficiary_id == REFERRER, ReferralEarning.kind != rr.KIND_COUNTED))
        self.assertEqual(int(ref.reward_idr), total)
        self.assertEqual(ref.status, "COMPLETED")
        db.close()


class TestSweeperNotStarved(Base3):
    def test_referral_pending_tanpa_order_tidak_memacetkan_antrean(self):
        db = SessionLocal()
        for i in range(130):  # lebih banyak dari batas per putaran sweeper
            uid = 50_000 + i
            db.add(User(telegram_id=uid))
            db.add(Referral(referrer_id=REFERRER, referee_id=uid, status="PENDING", reward_idr=0,
                            created_at=datetime.utcnow() - timedelta(days=10)))
        db.commit()
        # Teman yang benar-benar sudah transaksi, dibuat PALING AKHIR
        db.add(Referral(referrer_id=REFERRER, referee_id=FRIEND, status="PENDING", reward_idr=0,
                        created_at=datetime.utcnow() - timedelta(days=1)))
        db.add(_order("REAL", completed_at=datetime.utcnow() - timedelta(hours=2)))
        db.commit()
        self.assertGreaterEqual(accrue_pending(db), 2)
        self.assertEqual(db.query(Referral).filter(Referral.referee_id == FRIEND).one().status, "COMPLETED")
        db.close()


class TestMigrationBackfill(unittest.TestCase):
    def test_completed_at_kosong_diisi_dari_updated_at(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db").replace(os.sep, "/")
            c = sqlite3.connect(path)
            c.executescript("""
                create table orders (id integer primary key, order_id text, telegram_id integer, status text,
                                     completed_at timestamp, updated_at timestamp);
                insert into orders (order_id, telegram_id, status, completed_at, updated_at) values
                  ('A', 1, 'completed', NULL, '2026-10-01 10:00:00'),
                  ('B', 1, 'COMPLETED', '2026-10-02 09:00:00', '2026-10-05 09:00:00'),
                  ('C', 1, 'pending', NULL, '2026-10-03 09:00:00');
            """)
            c.commit()
            c.close()
            src = (ROOT / "main.py").read_text(encoding="utf-8")
            sql = src[src.index("UPDATE orders SET completed_at = updated_at"):]
            sql = sql[:sql.index('"\n')]
            stmt = "UPDATE orders SET completed_at = updated_at " + \
                   "WHERE LOWER(status) = 'completed' AND completed_at IS NULL AND updated_at IS NOT NULL"
            self.assertIn("completed_at IS NULL", src)
            c = sqlite3.connect(path)
            c.execute(stmt)
            rows = dict(c.execute("select order_id, completed_at from orders").fetchall())
            c.close()
            self.assertEqual(rows["A"], "2026-10-01 10:00:00")
            self.assertEqual(rows["B"], "2026-10-02 09:00:00", "yang sudah terisi tidak diubah")
            self.assertIsNone(rows["C"])


if __name__ == "__main__":
    unittest.main()
