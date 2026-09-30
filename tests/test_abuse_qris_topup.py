"""Uji abuse alur TOPUP QRIS/GoBiz — hasil audit keamanan 26 Sep 2026.

Skenario penyerangan yang diuji:
1. Tombol "Batalkan Topup" tanpa cek pemilik: user lain (atau siapa pun yang
   menerima pesan forward) bisa membatalkan invoice milik orang lain.
2. Tanpa cek status: topup yang SUDAH lunas (SUCCESS) bisa di-flip jadi CANCELLED.
3. claim_topup_success harus atomic → kredit saldo tidak boleh dobel.
4. Tombol "Cek Ulang" untuk topup SUCCESS harus membalas lunas tanpa kredit ulang.

Konvensi: XFAIL = bukti RED dari bug yang belum diperbaiki.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
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
from database.models import TopupOrder, User
from database import crud
from bot.handlers.balance import cancel_topup_manual, check_topup_payment_manual

PEMILIK = 999
PENYERANG = 111


def _topup(status="PENDING", amount=50077, telegram_id=PEMILIK, topup_id="TOPUP-ABUSE-1"):
    return TopupOrder(
        topup_id=topup_id,
        telegram_id=telegram_id,
        amount_idr=amount,
        unique_code=77,
        status=status,
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(minutes=30),
    )


class TestCancelTopupAbuse(unittest.IsolatedAsyncioTestCase):
    """cancel_topup_manual: tanpa cek pemilik & tanpa cek status."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=PEMILIK, username="korban", full_name="Korban"))
        self.db.add(User(telegram_id=PENYERANG, username="penyerang", full_name="Penyerang"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _status_topup(self):
        db = SessionLocal()
        try:
            return db.query(TopupOrder).filter(TopupOrder.topup_id == "TOPUP-ABUSE-1").first().status
        finally:
            db.close()

    @unittest.expectedFailure
    async def test_user_lain_tidak_bisa_membatalkan_topup(self):
        """Pesan invoice bisa di-forward; penekan tombol != pemilik invoice."""
        self.db.add(_topup(status="PENDING"))
        self.db.commit()
        query = AsyncMock()
        query.data = "cancel_topup_TOPUP-ABUSE-1"
        query.from_user = SimpleNamespace(id=PENYERANG)
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        with patch("bot.handlers.balance.SessionLocal", side_effect=lambda: SessionLocal()):
            await cancel_topup_manual(update, SimpleNamespace())
        self.assertNotEqual(
            self._status_topup(), "CANCELLED",
            "user lain tidak boleh membatalkan topup yang bukan miliknya",
        )

    @unittest.expectedFailure
    async def test_topup_success_tidak_bisa_dibatalkan(self):
        """Topup SUCCESS (saldo sudah masuk) di-flip ke CANCELLED = korupsi accounting."""
        self.db.add(_topup(status="SUCCESS"))
        self.db.commit()
        query = AsyncMock()
        query.data = "cancel_topup_TOPUP-ABUSE-1"
        query.from_user = SimpleNamespace(id=PEMILIK)
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        with patch("bot.handlers.balance.SessionLocal", side_effect=lambda: SessionLocal()):
            await cancel_topup_manual(update, SimpleNamespace())
        self.assertNotEqual(
            self._status_topup(), "CANCELLED",
            "topup yang sudah SUCCESS tidak boleh dibatalkan",
        )


class TestClaimTopupAtomic(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=PEMILIK, username="u", full_name="U"))
        self.db.add(_topup())
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_claim_success_hanya_sekali(self):
        self.assertTrue(crud.claim_topup_success(self.db, "TOPUP-ABUSE-1"))
        self.assertFalse(crud.claim_topup_success(self.db, "TOPUP-ABUSE-1"))


class TestCheckTopupDoubleCredit(unittest.IsolatedAsyncioTestCase):
    """Klik 'Cek Ulang' berkali-kali tidak boleh mengkredit saldo dua kali."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=PEMILIK, username="u", full_name="U"))
        self.db.add(_topup(status="PENDING"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _saldo(self):
        db = SessionLocal()
        try:
            return float(db.query(User).filter(User.telegram_id == PEMILIK).first().balance_idr or 0)
        finally:
            db.close()

    async def _klik_cek(self, query_extra=None):
        query = AsyncMock()
        query.data = "check_topup_TOPUP-ABUSE-1"
        query.from_user = SimpleNamespace(id=PEMILIK)
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        gopay = SimpleNamespace(check_payment=AsyncMock(
            return_value={"paid": True, "transaction": {"transaction_id": "TX-1"}}))
        with patch("bot.handlers.balance.SessionLocal", side_effect=lambda: SessionLocal()), \
             patch("bot.handlers.balance.gopay_service", gopay):
            await check_topup_payment_manual(update, SimpleNamespace())
        return query

    async def test_klik_ulang_tidak_double_credit(self):
        await self._klik_cek()
        self.assertAlmostEqual(self._saldo(), 50077.0, places=2)

        query2 = await self._klik_cek()
        self.assertAlmostEqual(self._saldo(), 50077.0, places=2, msg="double credit!")
        balasan = [str(c.args[0]) if c.args else "" for c in query2.answer.call_args_list]
        self.assertTrue(
            any("lunas" in b.lower() for b in balasan),
            f"klik ulang setelah SUCCESS harus membalas 'sudah lunas', balasan: {balasan}",
        )


if __name__ == "__main__":
    unittest.main()
