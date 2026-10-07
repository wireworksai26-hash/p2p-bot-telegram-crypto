"""S2 — kredit/debit Saldo Bot harus atomik di database, bukan baca-ubah-tulis di Python.

Dulu credit/deduct membaca User lewat ORM lalu menulis saldo baru dari nilai yang
dibaca. Sesi yang sudah memuat User sebelumnya (handler, job lain, instance kedua
saat deploy overlap) memakai saldo basi: potongan dobel lolos (double-spend) dan
kredit dari proses lain tertimpa (lost update).
"""
import os
import unittest
from decimal import Decimal

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from database import crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import User  # noqa: E402

UID = 4242


class AtomicBalance(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add(User(telegram_id=UID, balance_idr=Decimal("100000")))
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def _balance(self):
        db = SessionLocal()
        try:
            return Decimal(str(db.query(User).filter(User.telegram_id == UID).one().balance_idr))
        finally:
            db.close()

    def test_tidak_bisa_double_spend_dengan_sesi_basi(self):
        a, b = SessionLocal(), SessionLocal()
        try:
            # Handler A memuat & memegang objek user (saldo 100k), seperti handler sungguhan.
            loaded = a.query(User).filter(User.telegram_id == UID).one()
            self.assertTrue(crud.deduct_user_balance(b, UID, 100000))  # proses B memakai seluruh saldo
            self.assertFalse(crud.deduct_user_balance(a, UID, 100000),
                             "saldo sudah habis — potongan kedua harus ditolak")
            self.assertIsNotNone(loaded)
        finally:
            a.close()
            b.close()
        self.assertEqual(self._balance(), Decimal("0"))

    def test_kredit_proses_lain_tidak_tertimpa(self):
        a, b = SessionLocal(), SessionLocal()
        try:
            loaded = a.query(User).filter(User.telegram_id == UID).one()
            crud.credit_user_balance(b, UID, 50000)  # topup masuk dari proses lain
            self.assertTrue(crud.deduct_user_balance(a, UID, 30000))
            self.assertIsNotNone(loaded)
        finally:
            a.close()
            b.close()
        self.assertEqual(self._balance(), Decimal("120000"))

    def test_deduct_mengembalikan_saldo_cukup(self):
        db = SessionLocal()
        try:
            self.assertFalse(crud.deduct_user_balance(db, UID, 100001))
            self.assertTrue(crud.deduct_user_balance(db, UID, 100000))
        finally:
            db.close()
        self.assertEqual(self._balance(), Decimal("0"))

    def test_credit_mengembalikan_saldo_baru_dan_membuat_user(self):
        db = SessionLocal()
        try:
            self.assertEqual(Decimal(str(crud.credit_user_balance(db, UID, 2500))), Decimal("102500"))
            self.assertEqual(Decimal(str(crud.credit_user_balance(db, 777, 1000))), Decimal("1000"))
        finally:
            db.close()

    def test_nominal_negatif_ditolak(self):
        db = SessionLocal()
        try:
            with self.assertRaises(ValueError):
                crud.deduct_user_balance(db, UID, -5000)
            with self.assertRaises(ValueError):
                crud.credit_user_balance(db, UID, -5000)
        finally:
            db.close()
        self.assertEqual(self._balance(), Decimal("100000"))


if __name__ == "__main__":
    unittest.main()
