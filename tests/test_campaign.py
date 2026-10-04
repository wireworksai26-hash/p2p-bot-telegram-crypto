"""
tests/test_campaign.py — Unit Tests for Campaign & Giveaway Engine
===================================================================
Covers:
- Preset templates lookup
- Equal split calculation with remainder safety
- Random giveaway selection
- Milestone top spender ranking
- Budget cap protection (cannot exceed total pool)
- Atomic balance credit and audit trail
- Anti-double claim idempotency
- Custom notification placeholder formatting
"""

import os
import sys
import unittest
from decimal import Decimal
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "DATABASE_URL": "sqlite:///:memory:",
    "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "999",
})

from database.connection import Base, engine, SessionLocal
from database.models import User, Order, Campaign, CampaignDistribution, AuditLog
from services.campaign_service import (
    CAMPAIGN_TEMPLATES,
    get_template,
    simulate_campaign,
    execute_campaign,
    format_custom_notification,
)


class TestCampaignSimulation(unittest.TestCase):
    """Pengujian logika simulasi dan seleksi campaign."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

        # Seed 5 users: 4 normal, 1 banned
        self.u1 = User(telegram_id=101, username="alice", balance_idr=Decimal("0"), is_banned=False)
        self.u2 = User(telegram_id=102, username="bob", balance_idr=Decimal("5000"), is_banned=False)
        self.u3 = User(telegram_id=103, username="charlie", balance_idr=Decimal("10000"), is_banned=False)
        self.u4 = User(telegram_id=104, username="david", balance_idr=Decimal("0"), is_banned=False)
        self.u_banned = User(telegram_id=999, username="badguy", balance_idr=Decimal("0"), is_banned=True)
        self.db.add_all([self.u1, self.u2, self.u3, self.u4, self.u_banned])
        self.db.commit()

        # Seed orders: alice (Rp 500k), bob (Rp 200k), charlie (Rp 100k)
        o1 = Order(
            order_id="ORD-1", telegram_id=101, order_type="buy",
            crypto_symbol="USDT", network="BSC", crypto_amount=Decimal("30"),
            price_per_unit=16000, nominal_idr=480000, fee_idr=20000, total_idr=500000,
            status="completed"
        )
        o2 = Order(
            order_id="ORD-2", telegram_id=102, order_type="buy",
            crypto_symbol="USDT", network="BSC", crypto_amount=Decimal("12"),
            price_per_unit=16000, nominal_idr=192000, fee_idr=8000, total_idr=200000,
            status="completed"
        )
        o3 = Order(
            order_id="ORD-3", telegram_id=103, order_type="buy",
            crypto_symbol="USDC", network="BASE", crypto_amount=Decimal("6"),
            price_per_unit=16000, nominal_idr=96000, fee_idr=4000, total_idr=100000,
            status="completed"
        )
        self.db.add_all([o1, o2, o3])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_template_lookup(self):
        tpl = get_template("tpl_split_all")
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl["mode"], "EQUAL_SPLIT")
        self.assertIn("Bagi Rata", tpl["title"])

    def test_equal_split_calculation_and_cap(self):
        """Bagi rata 4 non-banned users dengan budget Rp 100.000."""
        sim = simulate_campaign(
            db=self.db,
            mode="EQUAL_SPLIT",
            total_pool=100_000,
            target_segment="all",
        )
        self.assertIsNone(sim["error"])
        self.assertEqual(sim["winner_count"], 4)
        self.assertEqual(sim["reward_per_winner"], 25_000)
        self.assertEqual(sim["total_distributed"], 100_000)
        self.assertEqual(sim["remaining_pool"], 0)
        # Banned user 999 must NOT be in winners
        winner_ids = [w["telegram_id"] for w in sim["winners"]]
        self.assertNotIn(999, winner_ids)
        self.assertIn(101, winner_ids)

    def test_equal_split_remainder_safety(self):
        """Rp 100.000 dibagi ke 3 user -> masing-masing Rp 33.333, sisa Rp 1 (total <= pool)."""
        sim = simulate_campaign(
            db=self.db,
            mode="EQUAL_SPLIT",
            total_pool=100_000,
            target_segment="buyers",  # alice, bob, charlie (3 buyers)
        )
        self.assertEqual(sim["winner_count"], 3)
        self.assertEqual(sim["reward_per_winner"], 33_333)
        self.assertEqual(sim["total_distributed"], 99_999)
        self.assertEqual(sim["remaining_pool"], 1)
        self.assertLessEqual(sim["total_distributed"], 100_000)

    def test_milestone_top_spenders_ranking(self):
        """Milestone: ranking berdasarkan volume transaksi (alice #1, bob #2)."""
        sim = simulate_campaign(
            db=self.db,
            mode="MILESTONE",
            total_pool=300_000,
            max_winners=2,
            milestone_metric="VOLUME_IDR",
        )
        self.assertEqual(sim["winner_count"], 2)
        winners = sim["winners"]
        # Alice had 500k, Bob had 200k
        self.assertEqual(winners[0]["telegram_id"], 101)
        self.assertEqual(winners[0]["rank"], 1)
        self.assertEqual(winners[0]["metric_value"], 500_000)
        self.assertEqual(winners[1]["telegram_id"], 102)
        self.assertEqual(winners[1]["rank"], 2)
        self.assertEqual(winners[1]["metric_value"], 200_000)
        # Rp 300.000 / 2 = Rp 150.000 per user
        self.assertEqual(sim["reward_per_winner"], 150_000)

    def test_random_giveaway_quota(self):
        """Random mode memilih tepat kuota yang diminta."""
        sim = simulate_campaign(
            db=self.db,
            mode="RANDOM",
            total_pool=200_000,
            target_segment="all",
            max_winners=2,
        )
        self.assertEqual(sim["winner_count"], 2)
        self.assertEqual(sim["reward_per_winner"], 100_000)
        self.assertEqual(sim["total_distributed"], 200_000)


class TestCampaignExecution(unittest.TestCase):
    """Pengujian eksekusi database, saldo, audit log, dan proteksi klaim ganda."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.u1 = User(telegram_id=201, username="winner1", balance_idr=Decimal("10000"), is_banned=False)
        self.u2 = User(telegram_id=202, username="winner2", balance_idr=Decimal("0"), is_banned=False)
        self.db.add_all([self.u1, self.u2])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_execute_campaign_success(self):
        """Eksekusi membagikan saldo dan mencatat audit log serta distribusi."""
        camp = Campaign(
            campaign_code="CMP-TEST-001",
            title="Giveaway Test",
            template_type="tpl_split_all",
            mode="EQUAL_SPLIT",
            target_segment="all",
            total_pool=50_000,
            status="DRAFT",
            created_by=999,
        )
        self.db.add(camp)
        self.db.commit()

        mock_bot = AsyncMock()
        result = execute_campaign(
            db=self.db,
            campaign_id=camp.id,
            admin_id=999,
            bot=mock_bot,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["distributed_count"], 2)
        self.assertEqual(result["distributed_amount"], 50_000)

        # Cek saldo user bertambah: Rp 50.000 / 2 = +Rp 25.000 per user
        u1_fresh = self.db.query(User).filter_by(telegram_id=201).first()
        u2_fresh = self.db.query(User).filter_by(telegram_id=202).first()
        self.assertEqual(u1_fresh.balance_idr, Decimal("35000"))
        self.assertEqual(u2_fresh.balance_idr, Decimal("25000"))

        # Cek tabel CampaignDistribution
        dists = self.db.query(CampaignDistribution).filter_by(campaign_id=camp.id).all()
        self.assertEqual(len(dists), 2)
        self.assertEqual(dists[0].amount_idr, 25_000)

        # Cek AuditLog
        audits = self.db.query(AuditLog).filter_by(action="CAMPAIGN_REWARD").all()
        self.assertEqual(len(audits), 2)

    def test_campaign_double_execution_rejected(self):
        """Campaign yang sudah COMPLETED tidak boleh dieksekusi ulang."""
        camp = Campaign(
            campaign_code="CMP-TEST-002",
            title="Completed Campaign",
            template_type="tpl_split_all",
            mode="EQUAL_SPLIT",
            target_segment="all",
            total_pool=50_000,
            status="COMPLETED",
            created_by=999,
        )
        self.db.add(camp)
        self.db.commit()

        with self.assertRaises(ValueError) as ctx:
            execute_campaign(self.db, camp.id, admin_id=999)
        self.assertIn("tidak dalam status DRAFT", str(ctx.exception))


class TestCustomNotificationFormatting(unittest.TestCase):
    """Pengujian penggantian placeholder custom notification."""

    def test_custom_notification_placeholders(self):
        tpl = "Halo {name}, selamat dapat {reward}! Saldo baru: {new_balance}. Event: {campaign_name} (Rank: {rank})"
        out = format_custom_notification(
            template=tpl,
            name="Alice",
            reward_amount=25000,
            new_balance=Decimal("50000"),
            campaign_name="Promo Awal Bulan",
            rank=1,
        )
        self.assertIn("Halo Alice", out)
        self.assertIn("25.000", out)
        self.assertIn("50.000", out)
        self.assertIn("Promo Awal Bulan", out)
        self.assertIn("Rank: 1", out)


class TestCampaignHandlers(unittest.IsolatedAsyncioTestCase):
    """Pengujian interaktif handler admin: command, callback, dan text input."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add_all([
            User(telegram_id=301, username="alpha", balance_idr=Decimal("0"), is_banned=False),
            User(telegram_id=302, username="beta", balance_idr=Decimal("0"), is_banned=False),
            Order(
                order_id="ORD-CMP-301",
                telegram_id=301,
                order_type="buy",
                crypto_symbol="USDT",
                network="BSC",
                crypto_amount=Decimal("10.0"),
                price_per_unit=16000,
                nominal_idr=160000,
                fee_idr=2000,
                total_idr=160000,
                status="completed",
            ),
            Order(
                order_id="ORD-CMP-302",
                telegram_id=302,
                order_type="buy",
                crypto_symbol="USDT",
                network="BSC",
                crypto_amount=Decimal("10.0"),
                price_per_unit=16000,
                nominal_idr=160000,
                fee_idr=2000,
                total_idr=160000,
                status="completed",
            ),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def test_campaign_command_handler(self):
        from bot.handlers.admin_campaign import campaign_command_handler
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            callback_query=None,
            message=AsyncMock(),
        )
        context = SimpleNamespace(bot=AsyncMock())

        await campaign_command_handler(update, context)
        self.assertTrue(update.message.reply_text.called)
        call_text = update.message.reply_text.call_args[0][0]
        self.assertIn("PUSAT CAMPAIGN & GIVEAWAY", call_text)

    async def test_campaign_callback_flow_preview_and_execute(self):
        from bot.handlers.admin_campaign import campaign_callback_handler
        mock_query = AsyncMock()
        mock_query.data = "camp_sim_tpl_split_all_100000"
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            callback_query=mock_query,
        )
        context = SimpleNamespace(bot=AsyncMock(), user_data={})

        # 1. Pilih template & nominal -> Preview muncul
        await campaign_callback_handler(update, context)
        self.assertTrue(mock_query.edit_message_text.called)
        preview_text = mock_query.edit_message_text.call_args[0][0]
        self.assertIn("PREVIEW CAMPAIGN", preview_text)
        self.assertIn("Rp 100.000", preview_text)

        # Cek ada Campaign DRAFT di DB
        camp = self.db.query(Campaign).filter_by(status="DRAFT").first()
        self.assertIsNotNone(camp)

        # 2. Konfirmasi eksekusi
        mock_query.reset_mock()
        mock_query.data = f"camp_confirm_exec_{camp.id}"
        await campaign_callback_handler(update, context)

        # Verifikasi campaign status COMPLETED dan user menerima saldo
        self.db.refresh(camp)
        self.assertEqual(camp.status, "COMPLETED")
        self.assertEqual(camp.distributed_count, 2)
        u1 = self.db.query(User).filter_by(telegram_id=301).first()
        self.assertEqual(u1.balance_idr, Decimal("50000"))

    async def test_campaign_text_input_custom_budget(self):
        from bot.handlers.admin_campaign import campaign_text_input_handler
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=AsyncMock(text="200.000"),
        )
        context = SimpleNamespace(
            bot=AsyncMock(),
            user_data={"awaiting_campaign_custom_budget": "tpl_split_all"},
        )

        handled = await campaign_text_input_handler(update, context)
        self.assertTrue(handled)
        self.assertTrue(update.message.reply_text.called)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("PREVIEW CAMPAIGN", reply)
        self.assertIn("Rp 200.000", reply)

    async def test_campaign_text_input_custom_notification(self):
        from bot.handlers.admin_campaign import campaign_text_input_handler
        # Buat draft campaign dulu
        camp = Campaign(
            campaign_code="CMP-EDIT-TEST",
            title="Custom Msg Test",
            template_type="tpl_split_all",
            mode="EQUAL_SPLIT",
            target_segment="all",
            total_pool=100_000,
            status="DRAFT",
            created_by=999,
        )
        self.db.add(camp)
        self.db.commit()

        custom_text = "🎉 Selamat {name}! Kamu memenangkan giveaway {reward} dari bot!"
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=AsyncMock(text=custom_text),
        )
        context = SimpleNamespace(
            bot=AsyncMock(),
            user_data={"awaiting_campaign_custom_msg_id": camp.id},
        )

        handled = await campaign_text_input_handler(update, context)
        self.assertTrue(handled)
        self.db.refresh(camp)
        self.assertEqual(camp.custom_message, custom_text)


if __name__ == "__main__":
    unittest.main()
