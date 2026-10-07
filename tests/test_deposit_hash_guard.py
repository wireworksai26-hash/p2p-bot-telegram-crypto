"""B3 — hash deposit yang ditempel user tidak boleh mengklaim deposit orang lain.

Hot wallet dipakai bersama. Dulu jalur hash menerima transfer apa pun yang
nominalnya >= order dan Convert langsung auto-payout — penyerang cukup
menempel hash deposit korban ke order miliknya.
"""
import os
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

import database.models  # noqa: F401
from database.connection import Base, SessionLocal, engine
from database.models import AuditLog, Order
from services.detector import DepositDetector

HOT = "0x" + "1" * 40


def _order(order_id, amount, telegram_id=1, order_type="sell"):
    return Order(
        order_id=order_id, telegram_id=telegram_id, order_type=order_type,
        crypto_symbol="USDT", network="BSC", crypto_amount=Decimal(str(amount)),
        price_per_unit=17915, nominal_idr=1, fee_idr=0, total_idr=1,
        buyer_wallet="BCA | 1 | X", deposit_wallet=HOT, status="WAITING_CRYPTO_DEPOSIT",
        created_at=datetime.utcnow(),
    )


class UserHashGuard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.det = DepositDetector()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _reason(self, order, received):
        return self.det.user_hash_review_reason(self.db, order, {"verified": True, "amount": received})

    def test_exact_deposit_for_own_order_is_auto(self):
        mine = _order("ORD-MINE", 10)
        self.db.add(mine)
        self.db.commit()
        self.assertEqual(self._reason(mine, 10.0), "")

    def test_overpay_goes_to_admin(self):
        # K2: dulu lebih bayar <= 0,5% auto — penyerang memasang order sedikit di bawah
        # transfer orang lain (isi ulang stok) lalu menempel hash-nya.
        mine = _order("ORD-MINE", 10)
        self.db.add(mine)
        self.db.commit()
        self.assertIn("tidak sesuai", self._reason(mine, 10.04))

    def test_big_victim_transfer_cannot_close_small_order(self):
        attacker = _order("ORD-ATTACKER", 1)
        self.db.add(attacker)
        self.db.commit()
        self.assertIn("tidak sesuai", self._reason(attacker, 99.0))

    def test_underpay_still_rejected(self):
        mine = _order("ORD-MINE", 10)
        self.db.add(mine)
        self.db.commit()
        self.assertIn("tidak sesuai", self._reason(mine, 9.99))

    def test_deposit_matching_another_waiting_order_is_ambiguous(self):
        victim = _order("ORD-VICTIM", 25, telegram_id=1)
        attacker = _order("ORD-ATTACKER", 25, telegram_id=2)
        self.db.add_all([victim, attacker])
        self.db.commit()
        self.assertIn("ORD-VICTIM", self._reason(attacker, 25.0))

    def test_near_amount_trick_is_ambiguous_too(self):
        # Penyerang memilih nominal sedikit di bawah deposit korban: kini selalu
        # dicek admin (nominal harus persis), apa pun alasannya.
        victim = _order("ORD-VICTIM", 25, telegram_id=1)
        attacker = _order("ORD-ATTACKER", "24.9", telegram_id=2)
        self.db.add_all([victim, attacker])
        self.db.commit()
        self.assertNotEqual(self._reason(attacker, 25.0), "")

    async def test_escalation_notifies_admin_once(self):
        order = _order("ORD-X", 1)
        self.db.add(order)
        self.db.commit()
        bot = AsyncMock()
        from unittest.mock import patch
        with patch("services.detector.notify_admins", new=AsyncMock()) as notify:
            await self.det.escalate_user_hash(self.db, order, "0xabc", "alasan", bot)
            await self.det.escalate_user_hash(self.db, order, "0xabc", "alasan", bot)
        notify.assert_awaited_once()
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "DEPOSIT_HASH_NEEDS_REVIEW").count(), 1)

    async def test_detector_does_not_confirm_ambiguous_user_hash(self):
        from unittest.mock import patch
        victim = _order("ORD-VICTIM", 25, telegram_id=1, order_type="swap")
        attacker = _order("ORD-ATTACKER", 25, telegram_id=2, order_type="swap")
        attacker.deposit_tx_hash = "0x" + "ab" * 32
        self.db.add_all([victim, attacker])
        self.db.commit()
        verified = {"verified": True, "amount": 25.0, "timestamp": datetime.utcnow().timestamp(),
                    "tx_hash": attacker.deposit_tx_hash, "reason": "OK"}
        with patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value=verified)), \
             patch("services.detector.notify_admins", new=AsyncMock()), \
             patch.object(self.det, "_confirm_order", new=AsyncMock()) as confirm:
            await self.det._process_order(self.db, attacker, AsyncMock())
        confirm.assert_not_awaited()


class LateConvertDeposit(unittest.IsolatedAsyncioTestCase):
    """B4 — deposit Convert setelah quote berakhir tidak dibayar otomatis dengan kurs lama."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.det = DepositDetector()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def _run(self, deposit_at, trusted=False):
        from datetime import timedelta
        from unittest.mock import patch
        order = _order("SWAP-1", 10, order_type="swap")
        order.created_at = datetime.utcnow() - timedelta(hours=2)
        order.quote_expires_at = order.created_at + timedelta(minutes=30)
        order.deposit_tx_hash = "0x" + "cd" * 32
        self.db.add(order)
        self.db.commit()
        verified = {"verified": True, "amount": 10.0, "timestamp": deposit_at.timestamp(),
                    "tx_hash": order.deposit_tx_hash, "reason": "OK"}
        with patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value=verified)), \
             patch("services.detector.notify_admins", new=AsyncMock()) as notify, \
             patch.object(self.det, "_confirm_order", new=AsyncMock()) as confirm:
            await self.det._process_order(self.db, order, AsyncMock(), trusted=trusted)
        return order, confirm, notify

    async def test_late_deposit_escalated_not_paid(self):
        from datetime import timedelta, timezone
        late = (datetime.utcnow() - timedelta(minutes=30)).replace(tzinfo=timezone.utc)
        _, confirm, notify = await self._run(late)
        confirm.assert_not_awaited()
        notify.assert_awaited_once()
        self.assertIn("masa quote", notify.await_args.args[1])

    async def test_in_time_deposit_confirmed(self):
        from datetime import timedelta, timezone
        on_time = (datetime.utcnow() - timedelta(hours=2) + timedelta(minutes=5)).replace(tzinfo=timezone.utc)
        _, confirm, _ = await self._run(on_time)
        confirm.assert_awaited_once()

    async def test_admin_trusted_path_can_settle_late_deposit(self):
        from datetime import timedelta, timezone
        late = (datetime.utcnow() - timedelta(minutes=30)).replace(tzinfo=timezone.utc)
        _, confirm, _ = await self._run(late, trusted=True)
        confirm.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
