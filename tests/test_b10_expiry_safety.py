"""B10: hasil gateway tak diketahui tidak boleh menyebabkan QRIS kedaluwarsa."""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

import database.models  # noqa: F401,E402
from database import crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order, TopupOrder, User  # noqa: E402
from services.gopay_service import GopayGatewayService  # noqa: E402


class GatewayAvailability(unittest.IsolatedAsyncioTestCase):
    async def test_non_200_gateway_result_is_unknown_not_unpaid(self):
        class FailedClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return SimpleNamespace(status_code=401)

        service = GopayGatewayService()
        with patch("services.gopay_service.httpx.AsyncClient", return_value=FailedClient()):
            result = await service.check_payment(50000, "TOPUP-X")
        self.assertFalse(result["available"])
        with patch.object(service, "check_payment", new=AsyncMock(return_value=result)):
            self.assertIsNone(await service.confirm_payment(
                db=None, amount=50000, ref_id="TOPUP-X", kind="topup", created_at=datetime.utcnow(),
            ))


class TopupExpiryRace(unittest.IsolatedAsyncioTestCase):
    USER_ID = 98101

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add(User(telegram_id=self.USER_ID, balance_idr=Decimal("0")))
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def _make_topup(self, topup_id, expired_seconds=300):
        now = datetime.utcnow()
        db = SessionLocal()
        db.add(TopupOrder(
            topup_id=topup_id, telegram_id=self.USER_ID, amount_idr=50_000,
            mdr_idr=0, status="PENDING", created_at=now - timedelta(minutes=30),
            expires_at=now - timedelta(seconds=expired_seconds),
        ))
        db.commit()
        db.close()

    def _topup_state(self, topup_id):
        db = SessionLocal()
        try:
            order = db.query(TopupOrder).filter_by(topup_id=topup_id).one()
            balance = db.query(User).filter_by(telegram_id=self.USER_ID).one().balance_idr
            return order.status, balance
        finally:
            db.close()

    async def _run_poller(self, payment_result):
        import main
        main._topup_last_transactions_fetch = 0
        with patch("services.gopay_service.gopay_service.confirm_payment", new=AsyncMock(return_value=payment_result)), \
                patch("services.gopay_service.gopay_service.get_recent_transactions", new=AsyncMock(return_value=[])):
            await main._job_check_pending_topups()

    async def test_gateway_down_keeps_expired_topup_pending(self):
        self._make_topup("TOPUP-DOWN", expired_seconds=900)
        await self._run_poller(None)
        self.assertEqual(self._topup_state("TOPUP-DOWN"), ("PENDING", Decimal("0.00")))

    async def test_confirmed_unpaid_waits_grace_then_expires(self):
        self._make_topup("TOPUP-GRACE", expired_seconds=30)
        await self._run_poller(False)
        self.assertEqual(self._topup_state("TOPUP-GRACE")[0], "PENDING")

        db = SessionLocal()
        try:
            db.query(TopupOrder).filter_by(topup_id="TOPUP-GRACE").update(
                {TopupOrder.expires_at: datetime.utcnow() - timedelta(minutes=5)},
                synchronize_session=False,
            )
            db.commit()
        finally:
            db.close()
        await self._run_poller(False)
        self.assertEqual(self._topup_state("TOPUP-GRACE")[0], "EXPIRED")

    async def test_gateway_paid_recovers_if_expiry_wins_race(self):
        self._make_topup("TOPUP-RACE", expired_seconds=300)
        import main

        async def expire_then_report_paid(db, **kwargs):
            self.assertTrue(crud.expire_topup_if_pending(db, "TOPUP-RACE"))
            return True

        main._topup_last_transactions_fetch = 0
        with patch("services.gopay_service.gopay_service.confirm_payment", side_effect=expire_then_report_paid), \
                patch("services.gopay_service.gopay_service.get_recent_transactions", new=AsyncMock(return_value=[])), \
                patch("services.bot_runtime.bot_app", None):
            await main._job_check_pending_topups()
        status, balance = self._topup_state("TOPUP-RACE")
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(balance, Decimal("50000.00"))

    def test_atomic_expire_cannot_overwrite_success(self):
        self._make_topup("TOPUP-ATOMIC", expired_seconds=300)
        stale_session = SessionLocal()
        stale_session.query(TopupOrder).filter_by(topup_id="TOPUP-ATOMIC").one()
        paying_session = SessionLocal()
        try:
            self.assertTrue(crud.claim_topup_success(paying_session, "TOPUP-ATOMIC"))
            self.assertFalse(crud.expire_topup_if_pending(stale_session, "TOPUP-ATOMIC"))
        finally:
            stale_session.close()
            paying_session.close()
        self.assertEqual(self._topup_state("TOPUP-ATOMIC")[0], "SUCCESS")


class BuyExpiryProtection(unittest.IsolatedAsyncioTestCase):
    USER_ID = 98102

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add(User(telegram_id=self.USER_ID, balance_idr=Decimal("0")))
        db.add(Order(
            order_id="ORD-GW-UNKNOWN", telegram_id=self.USER_ID, order_type="buy",
            crypto_symbol="USDT", network="BSC", crypto_amount=Decimal("1"),
            price_per_unit=50_000, nominal_idr=50_000, fee_idr=0, total_idr=50_000,
            payment_method="GOPAY_QRIS", status="pending",
            created_at=datetime.utcnow() - timedelta(hours=1),
        ))
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def test_expiry_batch_excludes_gateway_unknown_order(self):
        db = SessionLocal()
        try:
            count = crud.expire_stale_orders(db, minutes=10, exclude_order_ids={"ORD-GW-UNKNOWN"})
        finally:
            db.close()
        self.assertEqual(count, 0)
        db = SessionLocal()
        try:
            self.assertEqual(db.query(Order).filter_by(order_id="ORD-GW-UNKNOWN").one().status, "pending")
        finally:
            db.close()

    async def test_expiry_job_defers_unknown_and_graces_recent_unpaid(self):
        import main

        with patch("services.gopay_service.gopay_service.confirm_payment", new=AsyncMock(return_value=None)):
            await main._job_expire_orders()
        db = SessionLocal()
        try:
            order = db.query(Order).filter_by(order_id="ORD-GW-UNKNOWN").one()
            self.assertEqual(order.status, "pending")
            db.query(Order).filter_by(order_id="ORD-GW-UNKNOWN").update(
                {Order.created_at: datetime.utcnow() - timedelta(minutes=11)},
                synchronize_session=False,
            )
            db.commit()
        finally:
            db.close()

        with patch("services.gopay_service.gopay_service.confirm_payment", new=AsyncMock(return_value=False)):
            await main._job_expire_orders()
        db = SessionLocal()
        try:
            self.assertEqual(db.query(Order).filter_by(order_id="ORD-GW-UNKNOWN").one().status, "pending")
            db.query(Order).filter_by(order_id="ORD-GW-UNKNOWN").update(
                {Order.created_at: datetime.utcnow() - timedelta(minutes=20)},
                synchronize_session=False,
            )
            db.commit()
        finally:
            db.close()

        with patch("services.gopay_service.gopay_service.confirm_payment", new=AsyncMock(return_value=False)):
            await main._job_expire_orders()
        db = SessionLocal()
        try:
            self.assertEqual(db.query(Order).filter_by(order_id="ORD-GW-UNKNOWN").one().status, "expired")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
