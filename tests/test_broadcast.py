"""Unit tests for Broadcast Enhancement (Phase 2) — segment targeting.

Covers:
- Segment parsing (--all, --active, --buyers, --balance)
- get_users_by_segment CRUD queries (all, active, buyers, balance)
- Broadcast with segment flag routes to correct users
- Empty segment flag (message after flag)
- Backward compatibility (no flag = all users)
- Segment label display
"""
import os
import sys
import unittest
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timedelta
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

from database.connection import Base, engine, SessionLocal
from database.models import User, Order
from database.crud import get_users_by_segment, get_segment_count
from bot.handlers.admin import (
    _parse_broadcast_segment,
    _segment_label,
    broadcast_handler,
)


class TestBroadcastSegmentParser(unittest.TestCase):
    """Test the flag parser utility."""

    def test_no_flag_returns_all(self):
        seg, msg = _parse_broadcast_segment("Halo member")
        self.assertEqual(seg, "all")
        self.assertEqual(msg, "Halo member")

    def test_all_flag(self):
        seg, msg = _parse_broadcast_segment("--all Promo hari ini")
        self.assertEqual(seg, "all")
        self.assertEqual(msg, "Promo hari ini")

    def test_active_flag(self):
        seg, msg = _parse_broadcast_segment("--active Hanya untuk user aktif")
        self.assertEqual(seg, "active")
        self.assertEqual(msg, "Hanya untuk user aktif")

    def test_buyers_flag(self):
        seg, msg = _parse_broadcast_segment("--buyers Terima kasih sudah bertransaksi")
        self.assertEqual(seg, "buyers")
        self.assertEqual(msg, "Terima kasih sudah bertransaksi")

    def test_balance_flag(self):
        seg, msg = _parse_broadcast_segment("--balance Saldo Anda menunggu!")
        self.assertEqual(seg, "balance")
        self.assertEqual(msg, "Saldo Anda menunggu!")

    def test_flag_only_no_message(self):
        seg, msg = _parse_broadcast_segment("--buyers")
        self.assertEqual(seg, "buyers")
        self.assertEqual(msg, "")

    def test_flag_in_middle_not_parsed(self):
        """Flag must be at the start."""
        seg, msg = _parse_broadcast_segment("Hello --buyers world")
        self.assertEqual(seg, "all")
        self.assertEqual(msg, "Hello --buyers world")


class TestSegmentLabel(unittest.TestCase):
    def test_all_segments_have_labels(self):
        for seg in ("all", "active", "buyers", "balance"):
            label = _segment_label(seg)
            self.assertIsInstance(label, str)
            self.assertGreater(len(label), 3)

    def test_unknown_segment_fallback(self):
        label = _segment_label("unknown_segment")
        self.assertIn("Semua", label)


class TestSegmentQuery(unittest.IsolatedAsyncioTestCase):
    """Test get_users_by_segment CRUD with real in-memory DB."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        # Users
        db.add_all([
            User(telegram_id=10, username="active_buyer", is_banned=False,
                 balance_idr=Decimal("5000")),
            User(telegram_id=20, username="active_no_order", is_banned=False,
                 balance_idr=Decimal("0")),
            User(telegram_id=30, username="old_buyer", is_banned=False,
                 balance_idr=Decimal("0")),
            User(telegram_id=40, username="banned_user", is_banned=True,
                 balance_idr=Decimal("1000")),
        ])
        db.commit()

        # Orders
        now = datetime.utcnow()
        db.add_all([
            # Active order (recent) + completed → active_buyer is in active AND buyers
            Order(
                order_id="ORD-001", telegram_id=10, order_type="buy",
                crypto_symbol="USDT", network="BSC",
                crypto_amount=Decimal("1"), price_per_unit=16000,
                nominal_idr=5000, fee_idr=3000, total_idr=8000,
                status="completed", created_at=now - timedelta(days=5),
            ),
            # Old order (60 days ago) + completed → old_buyer is in buyers but NOT active
            Order(
                order_id="ORD-002", telegram_id=30, order_type="buy",
                crypto_symbol="USDT", network="BSC",
                crypto_amount=Decimal("1"), price_per_unit=16000,
                nominal_idr=5000, fee_idr=3000, total_idr=8000,
                status="completed", created_at=now - timedelta(days=60),
            ),
        ])
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def test_segment_all(self):
        db = SessionLocal()
        try:
            users = get_users_by_segment(db, "all")
            ids = {u.telegram_id for u in users}
            self.assertEqual(ids, {10, 20, 30})  # banned excluded
        finally:
            db.close()

    def test_segment_active(self):
        db = SessionLocal()
        try:
            users = get_users_by_segment(db, "active")
            ids = {u.telegram_id for u in users}
            self.assertEqual(ids, {10})  # Only recent order
        finally:
            db.close()

    def test_segment_buyers(self):
        db = SessionLocal()
        try:
            users = get_users_by_segment(db, "buyers")
            ids = {u.telegram_id for u in users}
            self.assertEqual(ids, {10, 30})  # Both completed orders
        finally:
            db.close()

    def test_segment_balance(self):
        db = SessionLocal()
        try:
            users = get_users_by_segment(db, "balance")
            ids = {u.telegram_id for u in users}
            self.assertEqual(ids, {10})  # Has balance + not banned
        finally:
            db.close()

    def test_segment_count(self):
        db = SessionLocal()
        try:
            self.assertEqual(get_segment_count(db, "all"), 3)
            self.assertEqual(get_segment_count(db, "active"), 1)
            self.assertEqual(get_segment_count(db, "buyers"), 2)
            self.assertEqual(get_segment_count(db, "balance"), 1)
        finally:
            db.close()


class TestBroadcastWithSegment(unittest.IsolatedAsyncioTestCase):
    """Integration tests for broadcast_handler with segment flags."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([
            User(telegram_id=10, username="alice", is_banned=False, balance_idr=Decimal("5000")),
            User(telegram_id=20, username="bob", is_banned=False, balance_idr=Decimal("0")),
            User(telegram_id=30, username="banned", is_banned=True, balance_idr=Decimal("0")),
        ])
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def _msg(self, text="/broadcast Halo", photo=None, caption=None):
        msg = AsyncMock()
        msg.text = text
        msg.photo = photo or []
        msg.caption = caption
        msg.reply_to_message = None
        return msg

    def _run(self, message, admin=True, bot=None):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=message,
        )
        context = SimpleNamespace(bot=bot or AsyncMock(), args=[])
        patches = [
            patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()),
            patch("bot.handlers.admin.is_admin", return_value=admin),
            patch("bot.handlers.admin.asyncio.sleep", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return update, context

    async def test_broadcast_all_default(self):
        """No segment flag → all non-banned users."""
        bot = AsyncMock()
        update, ctx = self._run(self._msg(), bot=bot)
        await broadcast_handler(update, ctx)
        # Should send to 2 non-banned users
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_broadcast_balance_segment(self):
        """--balance flag → only users with saldo > 0."""
        bot = AsyncMock()
        update, ctx = self._run(
            self._msg(text="/broadcast --balance Saldo menunggu!"),
            bot=bot,
        )
        await broadcast_handler(update, ctx)
        self.assertEqual(bot.send_message.await_count, 1)  # Only alice

    async def test_broadcast_report_includes_segment(self):
        """Report should mention the segment."""
        bot = AsyncMock()
        update, ctx = self._run(
            self._msg(text="/broadcast --balance Test"),
            bot=bot,
        )
        await broadcast_handler(update, ctx)
        report = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("LAPORAN BROADCAST", report)
        self.assertIn("Bersaldo", report)

    async def test_broadcast_empty_segment_rejected(self):
        """Flag only without message → error."""
        bot = AsyncMock()
        update, ctx = self._run(
            self._msg(text="/broadcast --buyers"),
            bot=bot,
        )
        await broadcast_handler(update, ctx)
        bot.send_message.assert_not_awaited()
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("kosong", text)

    async def test_non_admin_ditolak(self):
        bot = AsyncMock()
        update, ctx = self._run(self._msg(), admin=False, bot=bot)
        await broadcast_handler(update, ctx)
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
