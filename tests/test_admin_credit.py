"""Unit tests for Admin Credit Balance (Phase 1) — /credit and /bulkcredit.

Covers:
- Non-admin rejection
- Valid single credit with audit log + user notification
- Unknown user rejection
- Invalid amount (too low, too high, non-numeric)
- Bulk credit to multiple users (mix of success + not-found)
- Audit log creation verification
"""
import os
import sys
import unittest
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

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
from database.models import User, AuditLog
from database import crud
from bot.handlers.admin import credit_balance_handler, bulkcredit_handler


def _update(admin_id=999, args=None):
    """Build mock Update + Context for admin commands."""
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=admin_id),
        message=AsyncMock(),
    )
    context = SimpleNamespace(
        bot=AsyncMock(),
        args=args or [],
    )
    return update, context


class TestCreditBalance(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add_all([
            User(telegram_id=100, username="alice", balance_idr=Decimal("0")),
            User(telegram_id=200, username="bob", balance_idr=Decimal("5000")),
            User(telegram_id=300, username=None, balance_idr=Decimal("0")),
        ])
        self.db.commit()
        self._patches = [
            patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()),
            patch("bot.handlers.admin.is_admin", side_effect=lambda uid: uid == 999),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    # ── Non-admin rejected ──
    async def test_non_admin_rejected(self):
        update, ctx = _update(admin_id=111, args=["100", "10000"])
        await credit_balance_handler(update, ctx)
        update.message.reply_text.assert_not_awaited()

    # ── No args → usage help ──
    async def test_no_args_shows_usage(self):
        update, ctx = _update(args=[])
        await credit_balance_handler(update, ctx)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("/credit", text)

    # ── Valid credit ──
    async def test_credit_valid_user(self):
        update, ctx = _update(args=["100", "10000", "Giveaway"])
        await credit_balance_handler(update, ctx)
        # Check reply
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("Berhasil Isi Saldo", text)
        self.assertIn("@alice", text)
        # Check DB balance
        db2 = SessionLocal()
        try:
            user = db2.query(User).filter(User.telegram_id == 100).first()
            self.assertEqual(float(user.balance_idr), 10000.0)
        finally:
            db2.close()
        # Check notification was sent to user
        ctx.bot.send_message.assert_awaited()
        notif_text = ctx.bot.send_message.call_args.kwargs.get("text", "")
        self.assertIn("Saldo Anda Bertambah", notif_text)

    # ── Credit adds to existing balance ──
    async def test_credit_adds_to_existing(self):
        update, ctx = _update(args=["200", "5000"])
        await credit_balance_handler(update, ctx)
        db2 = SessionLocal()
        try:
            user = db2.query(User).filter(User.telegram_id == 200).first()
            self.assertEqual(float(user.balance_idr), 10000.0)
        finally:
            db2.close()

    # ── Unknown user ──
    async def test_credit_unknown_user(self):
        update, ctx = _update(args=["999999", "10000"])
        await credit_balance_handler(update, ctx)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("tidak ditemukan", text)

    # ── Amount too low ──
    async def test_credit_amount_too_low(self):
        update, ctx = _update(args=["100", "500"])
        await credit_balance_handler(update, ctx)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("Minimum", text)

    # ── Amount too high ──
    async def test_credit_amount_too_high(self):
        update, ctx = _update(args=["100", "99999999"])
        await credit_balance_handler(update, ctx)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("Maksimum", text)

    # ── Non-numeric telegram_id ──
    async def test_credit_invalid_telegram_id(self):
        update, ctx = _update(args=["abc", "10000"])
        await credit_balance_handler(update, ctx)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("angka", text)

    # ── Non-numeric amount ──
    async def test_credit_invalid_amount(self):
        update, ctx = _update(args=["100", "sepuluhribu"])
        await credit_balance_handler(update, ctx)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("angka", text)

    # ── Audit log created ──
    async def test_audit_log_created(self):
        update, ctx = _update(args=["100", "5000", "Test", "Audit"])
        await credit_balance_handler(update, ctx)
        db2 = SessionLocal()
        try:
            log = db2.query(AuditLog).filter(
                AuditLog.action == "ADMIN_CREDIT_BALANCE",
                AuditLog.telegram_id == 100,
            ).first()
            self.assertIsNotNone(log)
            self.assertIn("5,000", log.details)
            self.assertIn("Test Audit", log.details)
        finally:
            db2.close()


class TestBulkCredit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add_all([
            User(telegram_id=100, username="alice", balance_idr=Decimal("0")),
            User(telegram_id=200, username="bob", balance_idr=Decimal("0")),
            User(telegram_id=300, username="charlie", balance_idr=Decimal("0")),
        ])
        self.db.commit()
        self._patches = [
            patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()),
            patch("bot.handlers.admin.is_admin", side_effect=lambda uid: uid == 999),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def test_non_admin_rejected(self):
        update, ctx = _update(admin_id=111, args=["10000", "100"])
        await bulkcredit_handler(update, ctx)
        update.message.reply_text.assert_not_awaited()

    async def test_bulk_credit_success(self):
        update, ctx = _update(args=["10000", "100", "200", "300"])
        await bulkcredit_handler(update, ctx)
        # Last reply should be the report
        calls = update.message.reply_text.call_args_list
        report = calls[-1].args[0]
        self.assertIn("3/3", report)
        self.assertIn("LAPORAN BULK CREDIT", report)
        # Check balances
        db2 = SessionLocal()
        try:
            for tid in [100, 200, 300]:
                user = db2.query(User).filter(User.telegram_id == tid).first()
                self.assertEqual(float(user.balance_idr), 10000.0)
        finally:
            db2.close()

    async def test_bulk_credit_mixed(self):
        """Some users exist, some don't."""
        update, ctx = _update(args=["5000", "100", "999999", "200"])
        await bulkcredit_handler(update, ctx)
        calls = update.message.reply_text.call_args_list
        report = calls[-1].args[0]
        self.assertIn("2/3", report)  # 2 sukses
        self.assertIn("1/3", report)  # 1 gagal

    async def test_bulk_no_args_shows_usage(self):
        update, ctx = _update(args=[])
        await bulkcredit_handler(update, ctx)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("/bulkcredit", text)

    async def test_bulk_invalid_id(self):
        update, ctx = _update(args=["10000", "abc"])
        await bulkcredit_handler(update, ctx)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("bukan angka valid", text)

    async def test_bulk_amount_too_low(self):
        update, ctx = _update(args=["100", "100"])
        await bulkcredit_handler(update, ctx)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("Minimum", text)


if __name__ == "__main__":
    unittest.main()
