"""K1 — nominal QRIS tidak boleh bentrok antar order/topup PENDING.

Pembayaran QRIS dicocokkan HANYA lewat nominal. Bila dua tagihan PENDING punya
total yang sama, pembayaran korban bisa diklaim tagihan penyerang (poller
mengecek semua tagihan paralel, yang klaim duluan menang).

Serangan yang diuji:
1. Total bentrok walau kode unik berbeda (base 50.000+300 == base 50.200+100).
2. Kode unik habis (400 tagihan pending) -> dulu kode dipakai ulang.
3. Satu user membuat ratusan topup berturut-turut untuk menghabiskan kode.
4. topup_id hanya timestamp detik -> dua topup di detik yang sama crash.
"""
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

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from database.connection import Base, engine, SessionLocal
from database.models import Order, TopupOrder, User
from database import crud
from bot.handlers import balance

KORBAN = 999
PENYERANG = 111


def _pending_order(order_id, total, code, telegram_id=KORBAN):
    return Order(
        order_id=order_id, telegram_id=telegram_id, order_type="buy",
        crypto_symbol="USDT", network="BSC", crypto_amount=Decimal("1"),
        price_per_unit=16000, nominal_idr=total - code, fee_idr=3000,
        total_idr=total, unique_code=code, payment_method="GOPAY_QRIS",
        status="pending", created_at=datetime.utcnow(),
    )


def _pending_topup(topup_id, total, code, telegram_id=PENYERANG):
    return TopupOrder(
        topup_id=topup_id, telegram_id=telegram_id, amount_idr=total,
        unique_code=code, status="PENDING", created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(minutes=15),
    )


def _pending_totals(db):
    totals = [o.total_idr for o in db.query(Order).filter(Order.status == "pending").all()]
    totals += [t.amount_idr for t in db.query(TopupOrder).filter(TopupOrder.status == "PENDING").all()]
    return totals


class Base_(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=KORBAN, username="korban"))
        self.db.add(User(telegram_id=PENYERANG, username="penyerang"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)


class TestKodeUnikTidakBentrok(Base_):
    def test_total_bentrok_beda_kode_ditolak(self):
        # Order korban: 50.000 + kode 300 = 50.300.
        self.db.add(_pending_order("ORD-K", 50_300, 300))
        self.db.commit()
        # Topup penyerang base 50.200, satu-satunya kode tersedia = 100 -> total 50.300.
        kode = crud.generate_unique_payment_code(self.db, base_amount=50_200, min_code=100, max_code=100)
        self.assertIsNone(kode, "kode yang membuat total sama dengan tagihan pending wajib ditolak")

    def test_total_bentrok_dihindari_bila_ada_kode_lain(self):
        self.db.add(_pending_order("ORD-K", 50_300, 300))
        self.db.commit()
        for _ in range(50):
            kode = crud.generate_unique_payment_code(self.db, base_amount=50_200, min_code=99, max_code=101)
            self.assertIn(kode, (99, 101))

    def test_kode_habis_ditolak_bukan_dipakai_ulang(self):
        for c in range(1, 401):
            self.db.add(_pending_topup(f"TOPUP-A{c}", 50_000 + c, c, telegram_id=10_000 + c))
        self.db.commit()
        self.assertIsNone(crud.generate_unique_payment_code(self.db, base_amount=50_000))

    def test_tanpa_tagihan_pending_selalu_dapat_kode(self):
        kode = crud.generate_unique_payment_code(self.db, base_amount=50_000)
        self.assertTrue(1 <= kode <= 400)


class TestTopupHandlerAbuse(Base_):
    def _update(self, uid):
        msg = MagicMock()
        msg.reply_text = AsyncMock(return_value=MagicMock(delete=AsyncMock(), edit_text=AsyncMock()))
        return SimpleNamespace(callback_query=None, message=msg,
                               effective_user=SimpleNamespace(id=uid, username="u", full_name="U"))

    def _context(self):
        return SimpleNamespace(user_data={}, bot=SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()))

    async def _topup(self, uid, amount):
        with patch.object(balance, "SessionLocal", side_effect=lambda: SessionLocal()), \
             patch("services.qris_generator.get_qris_image_stream", return_value=None):
            ctx = self._context()
            await balance.generate_and_send_qris(self._update(uid), ctx, amount)
            return ctx

    async def test_satu_user_tidak_bisa_spam_topup_pending(self):
        for _ in range(5):
            await self._topup(PENYERANG, 50_000)
        pending = self.db.query(TopupOrder).filter(
            TopupOrder.telegram_id == PENYERANG, TopupOrder.status == "PENDING").count()
        self.assertEqual(pending, 1, "user hanya boleh punya satu topup PENDING")

    async def test_dua_topup_di_detik_yang_sama_tidak_crash(self):
        frozen = datetime(2026, 10, 7, 1, 2, 3)

        class _DT(datetime):
            @classmethod
            def utcnow(cls):
                return frozen

        with patch.object(balance, "datetime", _DT):
            await self._topup(KORBAN, 50_000)
            await self._topup(PENYERANG, 50_000)
        self.assertEqual(self.db.query(TopupOrder).count(), 2)

    async def test_kode_habis_topup_ditolak_dan_tidak_ada_total_kembar(self):
        for c in range(1, 401):
            self.db.add(_pending_topup(f"TOPUP-A{c}", 50_000 + c, c, telegram_id=10_000 + c))
        self.db.commit()
        ctx = await self._topup(KORBAN, 50_000)
        totals = _pending_totals(self.db)
        self.assertEqual(len(totals), len(set(totals)), "tidak boleh ada dua tagihan pending bernominal sama")
        self.assertEqual(self.db.query(TopupOrder).filter(TopupOrder.telegram_id == KORBAN).count(), 0)
        ctx.bot.send_photo.assert_not_awaited()


class TestSeranganEndToEnd(Base_):
    """Pembayaran korban tidak pernah bisa dicocokkan ke tagihan penyerang."""

    async def test_pembayaran_korban_tidak_bisa_diklaim_topup_penyerang(self):
        # Penyerang (banyak akun) menguasai semua total 50.001..50.400.
        for c in range(1, 401):
            self.db.add(_pending_topup(f"TOPUP-A{c}", 50_000 + c, c, telegram_id=10_000 + c))
        self.db.commit()
        kode = crud.generate_unique_payment_code(self.db, base_amount=50_000)
        if kode is not None:
            self.db.add(_pending_order("ORD-KORBAN", 50_000 + kode, kode))
            self.db.commit()
        totals = _pending_totals(self.db)
        self.assertEqual(len(totals), len(set(totals)))


if __name__ == "__main__":
    unittest.main()
