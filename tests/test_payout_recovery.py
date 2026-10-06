"""B2 + saldo: recovery payout tidak boleh mengirim dobel / menghanguskan saldo.

B2: proses mati di tengah payout (restart/deploy) → order 'payout_processing'
tanpa hash. Dulu poller 20 dtk mengirim ULANG setelah 120 dtk padahal koin
mungkin sudah ter-broadcast. Sekarang: manual_review + admin cek on-chain.

Saldo Bot: saldo dipotong lalu order 'pending'+paid_at. Dulu task payout bisa
hilang (edit Telegram gagal / restart) lalu order di-expire 15 menit → saldo hangus.
"""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

import database.models  # noqa: F401
from database import crud
from database.connection import Base, SessionLocal, engine
from database.models import Order


def _order(status, payment_method="GOPAY_QRIS", paid_at=None, minutes_old=10):
    return Order(
        order_id="ORD-REC-1", telegram_id=999, order_type="buy", crypto_symbol="USDT",
        network="BSC", crypto_amount=Decimal("1"), price_per_unit=16000, nominal_idr=50000,
        fee_idr=3000, total_idr=50077, unique_code=77, buyer_wallet="0x" + "3" * 40,
        payment_method=payment_method, status=status, paid_at=paid_at,
        created_at=datetime.utcnow() - timedelta(minutes=minutes_old),
        updated_at=datetime.utcnow() - timedelta(minutes=minutes_old),
    )


class InterruptedPayout(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def test_stale_payout_processing_is_escalated_not_resent(self):
        from bot.handlers import buy
        self.db.add(_order("payout_processing"))
        self.db.commit()
        send = AsyncMock(return_value={"success": True, "tx_hash": "0xNEW"})
        notify = AsyncMock()
        with patch("services.payout_service.send_order_payout", send), \
             patch.object(buy, "notify_admins", notify):
            await buy._run_finalize_background("ORD-REC-1", bot=AsyncMock(), allow_recovery=True)
        send.assert_not_awaited()
        notify.assert_awaited_once()
        self.assertIn("PAYOUT TERPUTUS", notify.await_args.args[1])
        self.db.expire_all()
        self.assertEqual(crud.get_order_by_id(self.db, "ORD-REC-1").status, "manual_review")

    async def test_fresh_payout_processing_left_alone(self):
        from bot.handlers import buy
        self.db.add(_order("payout_processing", minutes_old=0))
        self.db.commit()
        notify = AsyncMock()
        with patch.object(buy, "notify_admins", notify):
            await buy._run_finalize_background("ORD-REC-1", bot=AsyncMock(), allow_recovery=True)
        notify.assert_not_awaited()
        self.db.expire_all()
        self.assertEqual(crud.get_order_by_id(self.db, "ORD-REC-1").status, "payout_processing")


class BalancePaidOrder(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_paid_balance_order_is_not_expired_and_is_resumed(self):
        self.db.add(_order("pending", payment_method="BOT_BALANCE",
                           paid_at=datetime.utcnow() - timedelta(minutes=30), minutes_old=30))
        self.db.commit()
        crud.expire_stale_orders(self.db, minutes=15)
        self.db.expire_all()
        self.assertEqual(crud.get_order_by_id(self.db, "ORD-REC-1").status, "pending")
        self.assertIn("ORD-REC-1", [o.order_id for o in crud.get_gopay_resume_orders(self.db)])

    def test_unpaid_qris_order_still_expires(self):
        self.db.add(_order("pending", minutes_old=30))
        self.db.commit()
        self.assertEqual(crud.expire_stale_orders(self.db, minutes=15), 1)


if __name__ == "__main__":
    unittest.main()
