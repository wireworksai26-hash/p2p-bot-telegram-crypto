"""S1 — reward referral tidak boleh dobel dan tidak boleh lebih besar dari fee yang diterima bot.

1. complete_referral dipanggil detector, payout_watchdog, dan update_order_status.
   Dulu: baca PENDING -> kredit -> baru tandai COMPLETED. Dua pemanggil yang
   berjalan bersamaan sama-sama melihat PENDING -> reward dobel.
2. Farming: reward Rp 5.000 > fee transaksi minimum (Rp 3.000) dan syarat minimal
   transaksi default 0, jadi akun Telegram palsu yang belanja Rp 5.000 menghasilkan
   untung bersih bagi pengundang.
"""
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from database import crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Referral, User  # noqa: E402

REFERRER, REFEREE = 1001, 2002


class ReferralCase(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([User(telegram_id=REFERRER, balance_idr=0), User(telegram_id=REFEREE, balance_idr=0)])
        db.commit()
        crud.create_referral(db, REFERRER, REFEREE)  # reward default Rp 5.000
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def _balance(self, uid):
        db = SessionLocal()
        try:
            return Decimal(str(db.query(User).filter(User.telegram_id == uid).one().balance_idr or 0))
        finally:
            db.close()

    def _status(self):
        db = SessionLocal()
        try:
            return db.query(Referral).filter(Referral.referee_id == REFEREE).one().status
        finally:
            db.close()

    def test_dua_pemanggil_bersamaan_tidak_membayar_dobel(self):
        real_credit = crud.credit_user_balance
        state = {"reentered": False}

        def credit_then_other_worker(db, uid, amount):
            # Saat worker pertama sedang mengkredit, worker kedua (sesi lain) ikut memproses.
            if not state["reentered"]:
                state["reentered"] = True
                other = SessionLocal()
                try:
                    crud.complete_referral(other, REFEREE, trade_amount_idr=200_000, fee_idr=8_000)
                finally:
                    other.close()
            return real_credit(db, uid, amount)

        db = SessionLocal()
        try:
            with patch.object(crud, "credit_user_balance", side_effect=credit_then_other_worker):
                crud.complete_referral(db, REFEREE, trade_amount_idr=200_000, fee_idr=8_000)
        finally:
            db.close()
        self.assertEqual(self._balance(REFERRER), Decimal("5000"), "reward referral dibayar lebih dari sekali")
        self.assertEqual(self._status(), "COMPLETED")

    def test_fee_lebih_kecil_dari_reward_tidak_membayar(self):
        db = SessionLocal()
        try:
            self.assertFalse(crud.complete_referral(db, REFEREE, trade_amount_idr=5_000, fee_idr=3_000))
        finally:
            db.close()
        self.assertEqual(self._balance(REFERRER), Decimal("0"))
        self.assertEqual(self._status(), "PENDING", "transaksi berikutnya yang cukup besar tetap bisa memicu")

    def test_kontrol_fee_cukup_membayar_sekali(self):
        db = SessionLocal()
        try:
            self.assertTrue(crud.complete_referral(db, REFEREE, trade_amount_idr=100_000, fee_idr=5_000))
            self.assertFalse(crud.complete_referral(db, REFEREE, trade_amount_idr=100_000, fee_idr=5_000))
        finally:
            db.close()
        self.assertEqual(self._balance(REFERRER), Decimal("5000"))

    def test_bonus_referee_ikut_dihitung(self):
        db = SessionLocal()
        try:
            crud.set_referral_config(db, "referee_discount_idr", "3000")
            # reward 5.000 + bonus 3.000 = 8.000 > fee 6.000 -> tunda
            self.assertFalse(crud.complete_referral(db, REFEREE, trade_amount_idr=150_000, fee_idr=6_000))
            self.assertTrue(crud.complete_referral(db, REFEREE, trade_amount_idr=300_000, fee_idr=9_000))
        finally:
            db.close()
        self.assertEqual(self._balance(REFEREE), Decimal("3000"))

    def test_admin_bisa_mematikan_guard_fee(self):
        db = SessionLocal()
        try:
            crud.set_referral_config(db, "referral_fee_guard", "false")
            self.assertTrue(crud.complete_referral(db, REFEREE, trade_amount_idr=5_000, fee_idr=3_000))
        finally:
            db.close()
        self.assertEqual(self._balance(REFERRER), Decimal("5000"))


if __name__ == "__main__":
    unittest.main()
