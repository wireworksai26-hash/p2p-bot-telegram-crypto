"""Unit tests for Referral Program (Phase 3).

Covers:
- CRUD: create_referral, get_referral_by_referee, complete_referral
- Anti-abuse: self-referral, duplicate referee, max cap
- Referral config: get/set
- Referral stats and top referrers
- Deep-link detection in start_handler
- Referral menu handler
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
from database.models import User, Referral, ReferralConfig
from database import crud


class TestReferralCRUD(unittest.TestCase):
    """Test referral CRUD functions with in-memory DB."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([
            User(telegram_id=100, username="referrer_alice", balance_idr=Decimal("0")),
            User(telegram_id=200, username="referee_bob", balance_idr=Decimal("0")),
            User(telegram_id=300, username="referee_charlie", balance_idr=Decimal("0")),
            User(telegram_id=400, username="referee_dave", balance_idr=Decimal("0")),
        ])
        # Seed default config
        db.add_all([
            ReferralConfig(key="reward_per_referral", value="5000"),
            ReferralConfig(key="referral_enabled", value="true"),
            ReferralConfig(key="max_referrals_per_user", value="100"),
        ])
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def test_create_referral_success(self):
        db = SessionLocal()
        try:
            ref = crud.create_referral(db, referrer_id=100, referee_id=200)
            self.assertIsNotNone(ref)
            self.assertEqual(ref.referrer_id, 100)
            self.assertEqual(ref.referee_id, 200)
            self.assertEqual(ref.status, "PENDING")
            self.assertEqual(ref.reward_idr, 5000)
        finally:
            db.close()

    def test_create_referral_duplicate_rejected(self):
        """One referee can only have one referrer."""
        db = SessionLocal()
        try:
            crud.create_referral(db, referrer_id=100, referee_id=200)
            dup = crud.create_referral(db, referrer_id=300, referee_id=200)
            self.assertIsNone(dup)
        finally:
            db.close()

    def test_create_referral_disabled(self):
        """When referral_enabled is false, no referrals created."""
        db = SessionLocal()
        try:
            crud.set_referral_config(db, "referral_enabled", "false")
            ref = crud.create_referral(db, referrer_id=100, referee_id=200)
            self.assertIsNone(ref)
        finally:
            db.close()

    def test_create_referral_max_cap(self):
        """Referrer at max cap cannot create more."""
        db = SessionLocal()
        try:
            crud.set_referral_config(db, "max_referrals_per_user", "1")
            # First referral OK
            ref1 = crud.create_referral(db, referrer_id=100, referee_id=200)
            self.assertIsNotNone(ref1)
            # Second referral should be rejected (over cap)
            ref2 = crud.create_referral(db, referrer_id=100, referee_id=300)
            self.assertIsNone(ref2)
        finally:
            db.close()

    def test_get_referral_by_referee(self):
        db = SessionLocal()
        try:
            crud.create_referral(db, referrer_id=100, referee_id=200)
            ref = crud.get_referral_by_referee(db, referee_id=200)
            self.assertIsNotNone(ref)
            self.assertEqual(ref.referrer_id, 100)
        finally:
            db.close()

    def test_get_referral_by_referee_not_found(self):
        db = SessionLocal()
        try:
            ref = crud.get_referral_by_referee(db, referee_id=999)
            self.assertIsNone(ref)
        finally:
            db.close()

    def test_complete_referral_credits_reward(self):
        db = SessionLocal()
        try:
            crud.create_referral(db, referrer_id=100, referee_id=200)
            result = crud.complete_referral(db, referee_id=200)
            self.assertTrue(result)

            # Check status
            ref = crud.get_referral_by_referee(db, 200)
            self.assertEqual(ref.status, "COMPLETED")
            self.assertIsNotNone(ref.completed_at)

            # Check referrer balance credited
            bal = crud.get_user_balance(db, 100)
            self.assertEqual(bal, 5000.0)
        finally:
            db.close()

    def test_complete_referral_already_completed(self):
        """Double-complete should return False."""
        db = SessionLocal()
        try:
            crud.create_referral(db, referrer_id=100, referee_id=200)
            self.assertTrue(crud.complete_referral(db, 200))
            self.assertFalse(crud.complete_referral(db, 200))
        finally:
            db.close()

    def test_complete_referral_no_referral(self):
        db = SessionLocal()
        try:
            result = crud.complete_referral(db, referee_id=999)
            self.assertFalse(result)
        finally:
            db.close()


class TestReferralStats(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([
            User(telegram_id=100, username="referrer", balance_idr=Decimal("0")),
            User(telegram_id=200, username="ref1", balance_idr=Decimal("0")),
            User(telegram_id=300, username="ref2", balance_idr=Decimal("0")),
            User(telegram_id=400, username="ref3", balance_idr=Decimal("0")),
        ])
        db.add(ReferralConfig(key="reward_per_referral", value="5000"))
        db.commit()

        # Create 3 referrals, complete 2
        crud.create_referral(db, 100, 200)
        crud.create_referral(db, 100, 300)
        crud.create_referral(db, 100, 400)
        crud.complete_referral(db, 200)
        crud.complete_referral(db, 300)
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def test_referral_stats(self):
        db = SessionLocal()
        try:
            stats = crud.get_referral_stats(db, 100)
            self.assertEqual(stats["total"], 3)
            self.assertEqual(stats["completed"], 2)
            self.assertEqual(stats["pending"], 1)
            self.assertEqual(stats["total_reward"], 10000)
        finally:
            db.close()

    def test_top_referrers(self):
        db = SessionLocal()
        try:
            top = crud.get_top_referrers(db, limit=10)
            self.assertEqual(len(top), 1)
            self.assertEqual(top[0].referrer_id, 100)
            self.assertEqual(top[0].total, 2)  # 2 completed
        finally:
            db.close()


class TestReferralConfig(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def test_set_and_get(self):
        db = SessionLocal()
        try:
            crud.set_referral_config(db, "reward_per_referral", "10000")
            val = crud.get_referral_config(db, "reward_per_referral")
            self.assertEqual(val, "10000")
        finally:
            db.close()

    def test_update_existing(self):
        db = SessionLocal()
        try:
            crud.set_referral_config(db, "test_key", "value1")
            crud.set_referral_config(db, "test_key", "value2")
            val = crud.get_referral_config(db, "test_key")
            self.assertEqual(val, "value2")
        finally:
            db.close()

    def test_get_nonexistent(self):
        db = SessionLocal()
        try:
            val = crud.get_referral_config(db, "does_not_exist")
            self.assertIsNone(val)
        finally:
            db.close()


class TestStartDeepLink(unittest.IsolatedAsyncioTestCase):
    """Test referral deep-link detection in start_handler."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([
            User(telegram_id=100, username="referrer"),
            ReferralConfig(key="reward_per_referral", value="5000"),
            ReferralConfig(key="referral_enabled", value="true"),
        ])
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    async def test_deep_link_creates_referral(self):
        from bot.handlers.start import start_handler

        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=200, username="newuser", first_name="New", full_name="New User"
            ),
            effective_chat=SimpleNamespace(id=200),
            message=AsyncMock(),
            callback_query=None,
        )
        context = SimpleNamespace(
            bot=AsyncMock(),
            args=["ref_100"],
            user_data={},
        )

        # Mock get_me for referral menu
        context.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="Hsnpro_bot"))

        with patch("bot.handlers.start.SessionLocal", side_effect=lambda: SessionLocal()):
            with patch("bot.handlers.start.send_main_menu", new=AsyncMock()):
                await start_handler(update, context)

        # Check referral was created
        db = SessionLocal()
        try:
            ref = crud.get_referral_by_referee(db, 200)
            self.assertIsNotNone(ref)
            self.assertEqual(ref.referrer_id, 100)
            self.assertEqual(ref.status, "PENDING")
        finally:
            db.close()

        # Check referrer was notified
        context.bot.send_message.assert_awaited()

    async def test_self_referral_ignored(self):
        from bot.handlers.start import start_handler

        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=100, username="selfref", first_name="Self", full_name="Self Ref"
            ),
            effective_chat=SimpleNamespace(id=100),
            message=AsyncMock(),
            callback_query=None,
        )
        context = SimpleNamespace(
            bot=AsyncMock(),
            args=["ref_100"],  # Referring self
            user_data={},
        )

        with patch("bot.handlers.start.SessionLocal", side_effect=lambda: SessionLocal()):
            with patch("bot.handlers.start.send_main_menu", new=AsyncMock()):
                await start_handler(update, context)

        db = SessionLocal()
        try:
            ref = crud.get_referral_by_referee(db, 100)
            self.assertIsNone(ref)  # Self-referral should NOT be created
        finally:
            db.close()

    async def test_duplicate_referral_ignored(self):
        from bot.handlers.start import start_handler

        # Create initial referral
        db = SessionLocal()
        try:
            db.add(User(telegram_id=200, username="existinguser"))
            db.commit()
            crud.create_referral(db, 100, 200)
        finally:
            db.close()

        update = SimpleNamespace(
            effective_user=SimpleNamespace(
                id=200, username="existinguser", first_name="Existing", full_name="Existing User"
            ),
            effective_chat=SimpleNamespace(id=200),
            message=AsyncMock(),
            callback_query=None,
        )
        context = SimpleNamespace(
            bot=AsyncMock(),
            args=["ref_100"],
            user_data={},
        )

        with patch("bot.handlers.start.SessionLocal", side_effect=lambda: SessionLocal()):
            with patch("bot.handlers.start.send_main_menu", new=AsyncMock()):
                await start_handler(update, context)

        # Should not send notification for duplicate
        context.bot.send_message.assert_not_awaited()


class TestSetReferralHandler(unittest.IsolatedAsyncioTestCase):
    """Test /setreferral admin command."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    async def test_set_reward(self):
        from bot.handlers.admin import setreferral_handler

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=AsyncMock(),
        )
        context = SimpleNamespace(args=["reward", "10000"])

        with patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()):
            with patch("bot.handlers.admin.is_admin", return_value=True):
                await setreferral_handler(update, context)

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("diperbarui", text)

        db = SessionLocal()
        try:
            val = crud.get_referral_config(db, "reward_per_referral")
            self.assertEqual(val, "10000")
        finally:
            db.close()

    async def test_set_enabled(self):
        from bot.handlers.admin import setreferral_handler

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=AsyncMock(),
        )
        context = SimpleNamespace(args=["enabled", "false"])

        with patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()):
            with patch("bot.handlers.admin.is_admin", return_value=True):
                await setreferral_handler(update, context)

        db = SessionLocal()
        try:
            val = crud.get_referral_config(db, "referral_enabled")
            self.assertEqual(val, "false")
        finally:
            db.close()

    async def test_invalid_key_rejected(self):
        from bot.handlers.admin import setreferral_handler

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=AsyncMock(),
        )
        context = SimpleNamespace(args=["badkey", "123"])

        with patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()):
            with patch("bot.handlers.admin.is_admin", return_value=True):
                await setreferral_handler(update, context)

        text = update.message.reply_text.call_args.args[0]
        self.assertIn("tidak valid", text)

    async def test_non_admin_rejected(self):
        from bot.handlers.admin import setreferral_handler

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=111),
            message=AsyncMock(),
        )
        context = SimpleNamespace(args=["reward", "10000"])

        with patch("bot.handlers.admin.is_admin", return_value=False):
            await setreferral_handler(update, context)

        update.message.reply_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
