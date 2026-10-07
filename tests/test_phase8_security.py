"""
tests/test_phase8_security.py — Comprehensive Security & Functional Test Suite (Phase 8)
========================================================================================
Covers:
1. Admin Send Balance (Lookup @username / ID, amount bounds, AuditLog, recipient notification, non-admin rejection)
2. Bot Campaign Treasury (Get, Topup, Set, Deduct flooring at 0, AuditLog, /topupbot & /saldobot commands)
3. Animated Custom Emojis (tg_emoji, coin/network emojis, campaign & transaction receipts)
4. Campaign Notification Customization & 1-Click Reset to Default
5. Transaction Integrity & Channel Testimony Anonymization
"""

import os
import sys
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "DATABASE_URL": "sqlite:///:memory:",
    "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "999,888",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from config.settings import settings
from database.connection import Base, engine, SessionLocal
from database.models import User, Order, AuditLog, Campaign, LoyaltyConfig
from database import crud
from bot.handlers.admin import (
    admin_interactive_text_router,
    topup_bot_command_handler,
    topup_qris_command_handler,
    generate_and_send_treasury_qris,
    is_admin,
    build_admin_treasury_view,
    build_admin_send_balance_amount_view,
    build_admin_send_balance_confirm_view,
)
from bot.utils.emojis import (
    tg_emoji,
    get_coin_emoji,
    get_network_emoji,
    E_GIFT,
    E_TROPHY,
    E_BANK,
    E_CHECK,
    E_MONEY_BAG,
    E_PARTY,
)
from services.testimony_service import (
    anonymize_username,
    format_testimony_message,
    post_transaction_testimony,
)
from services.campaign_service import (
    CAMPAIGN_TEMPLATES,
    get_template,
    format_custom_notification,
    execute_campaign,
    simulate_campaign,
)


class TestAdminSendBalanceSecurity(unittest.IsolatedAsyncioTestCase):
    """Pengujian keamanan dan fungsionalitas transfer saldo admin ke user."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self._orig_admins = list(getattr(settings, "ADMIN_CHAT_IDS", []))
        settings.ADMIN_CHAT_IDS = [999, 888]
        # Seed users
        self.db.add_all([
            User(telegram_id=101, username="satoshi", full_name="Satoshi N", balance_idr=Decimal("50000"), total_orders=3),
            User(telegram_id=102, username="vitalik_eth", full_name="Vitalik B", balance_idr=Decimal("0"), total_orders=1),
            User(telegram_id=103, username=None, full_name="Anonymous User", balance_idr=Decimal("10000"), total_orders=0),
        ])
        self.db.commit()

    def tearDown(self):
        settings.ADMIN_CHAT_IDS = self._orig_admins
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_user_lookup_by_username_and_id(self):
        """Lookup user mendukung @username case-insensitive dan numerical telegram_id."""
        # 1. By username with @
        u1 = crud.get_user_by_identifier(self.db, "@satoshi")
        self.assertIsNotNone(u1)
        self.assertEqual(u1.telegram_id, 101)

        # 2. By username without @, mixed case
        u2 = crud.get_user_by_identifier(self.db, "ViTaLiK_eTh")
        self.assertIsNotNone(u2)
        self.assertEqual(u2.telegram_id, 102)

        # 3. By numeric ID
        u3 = crud.get_user_by_identifier(self.db, "103")
        self.assertIsNotNone(u3)
        self.assertIsNone(u3.username)

        # 4. Non-existent user
        u4 = crud.get_user_by_identifier(self.db, "@non_existent_trader")
        self.assertIsNone(u4)

    async def test_non_admin_cannot_send_balance(self):
        """User biasa (non-admin) tidak dapat memicu router interaktif admin."""
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=777),  # Non-admin
            message=AsyncMock(text="@satoshi"),
        )
        context = SimpleNamespace(user_data={"admin_awaiting_send_bal_user": True})

        handled = await admin_interactive_text_router(update, context)
        self.assertFalse(handled)

    async def test_admin_send_balance_lookup_interactive(self):
        """Admin memasukkan @username sasaran, sistem menemukan user dan merender opsi nominal."""
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),  # Admin
            message=AsyncMock(text="@satoshi"),
        )
        context = SimpleNamespace(user_data={"admin_awaiting_send_bal_user": True})

        handled = await admin_interactive_text_router(update, context)
        self.assertTrue(handled)
        self.assertEqual(context.user_data.get("admin_send_bal_target_id"), 101)
        self.assertNotIn("admin_awaiting_send_bal_user", context.user_data)
        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertIn("satoshi", args[0])

    async def test_admin_send_balance_custom_amount_validation(self):
        """Validasi batas nominal transfer kustom (min 1.000, max 10.000.000)."""
        # Terlalu kecil (< 1.000)
        update_low = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=AsyncMock(text="500"),
        )
        context_low = SimpleNamespace(user_data={
            "admin_awaiting_send_bal_custom_amt": True,
            "admin_send_bal_target_id": 101,
        })
        handled = await admin_interactive_text_router(update_low, context_low)
        self.assertTrue(handled)
        update_low.message.reply_text.assert_called_with(
            "❌ Nominal harus antara <b>Rp 1.000</b> sampai <b>Rp 10.000.000</b>.\nSilakan ketik angka kembali:",
            parse_mode="HTML"
        )

        # Valid amount (Rp 150.000)
        update_valid = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=AsyncMock(text="150000"),
        )
        context_valid = SimpleNamespace(user_data={
            "admin_awaiting_send_bal_custom_amt": True,
            "admin_send_bal_target_id": 101,
        })
        handled = await admin_interactive_text_router(update_valid, context_valid)
        self.assertTrue(handled)
        self.assertNotIn("admin_awaiting_send_bal_custom_amt", context_valid.user_data)

    def test_credit_user_balance_and_audit_log(self):
        """Credit user balance menambah saldo secara atomic dan mencatat AuditLog."""
        old_bal = float(crud.get_user(self.db, 101).balance_idr)
        new_bal = crud.credit_user_balance(self.db, 101, 75000)

        self.assertEqual(new_bal, old_bal + 75000)
        u = crud.get_user(self.db, 101)
        self.assertEqual(float(u.balance_idr), 125000.0)


class TestBotCampaignTreasurySecurity(unittest.IsolatedAsyncioTestCase):
    """Pengujian keamanan kas & dompet bot internal (Bot Campaign Treasury)."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self._orig_admins = list(getattr(settings, "ADMIN_CHAT_IDS", []))
        settings.ADMIN_CHAT_IDS = [999, 888]

    def tearDown(self):
        settings.ADMIN_CHAT_IDS = self._orig_admins
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_bot_treasury_crud_lifecycle(self):
        """Siklus lengkap get, topup, set, dan deduct bot treasury."""
        # Initial treasury default is 0
        bal0 = crud.get_bot_treasury_balance(self.db)
        self.assertEqual(bal0, 0)

        # Top up +500.000
        bal1 = crud.topup_bot_treasury(self.db, 500000, admin_id=999, note="Seed event pool")
        self.assertEqual(bal1, 500000)
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 500000)

        # Audit log verification
        audit1 = self.db.query(AuditLog).filter(AuditLog.action == "TOPUP_BOT_TREASURY").first()
        self.assertIsNotNone(audit1)
        self.assertIn("500,000", audit1.details)

        # Top up lagi +250.000
        bal2 = crud.topup_bot_treasury(self.db, 250000, admin_id=999)
        self.assertEqual(bal2, 750000)

        # Deduct 300.000 (Campaign execution)
        bal3 = crud.deduct_bot_treasury(self.db, 300000, admin_id=999, note="Giveaway 20 winner")
        self.assertEqual(bal3, 450000)

        # Deduct over balance -> Safe flooring at 0
        bal4 = crud.deduct_bot_treasury(self.db, 1000000, admin_id=999)
        self.assertEqual(bal4, 0)

        # Set manual to 2.000.000
        bal5 = crud.set_bot_treasury_balance(self.db, 2000000, admin_id=999, note="Refill manual")
        self.assertEqual(bal5, 2000000)
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 2000000)

    def test_bot_treasury_rejects_negative(self):
        """Topup & Set menolak nominal negatif."""
        with self.assertRaises(ValueError):
            crud.topup_bot_treasury(self.db, -10000, admin_id=999)

        with self.assertRaises(ValueError):
            crud.set_bot_treasury_balance(self.db, -50000, admin_id=999)

    async def test_topup_bot_command_security(self):
        """Command /topupbot dan /saldobot hanya bisa dipanggil admin."""
        # Non-admin
        update_non_admin = SimpleNamespace(
            effective_user=SimpleNamespace(id=777),
            message=AsyncMock(),
        )
        context = SimpleNamespace(args=["500000"])
        await topup_bot_command_handler(update_non_admin, context)
        update_non_admin.message.reply_text.assert_not_called()

        # Admin without args -> Show treasury status
        update_admin_view = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=AsyncMock(),
        )
        context_empty = SimpleNamespace(args=[])
        await topup_bot_command_handler(update_admin_view, context_empty)
        update_admin_view.message.reply_text.assert_called_once()
        args, kwargs = update_admin_view.message.reply_text.call_args
        self.assertIn("KAS & DOMPET CAMPAIGN BOT", args[0])

        # Admin with amount -> Top up success
        update_admin_topup = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=AsyncMock(),
        )
        context_topup = SimpleNamespace(args=["1000000", "Topup", "Event", "Maret"])
        await topup_bot_command_handler(update_admin_topup, context_topup)
        update_admin_topup.message.reply_text.assert_called_once()
        args, kwargs = update_admin_topup.message.reply_text.call_args
        self.assertIn("TOP UP KAS BOT BERHASIL", args[0])
        self.assertIn("1.000.000", args[0])

    async def test_topup_qris_command_security_and_execution(self):
        """Command /topupqris hanya untuk admin dan dapat menerima argumen nominal atau menampilkan menu."""
        # Non-admin diblokir
        update_non_admin = SimpleNamespace(
            effective_user=SimpleNamespace(id=777),
            message=AsyncMock(),
        )
        context = SimpleNamespace(args=["50000"])
        await topup_qris_command_handler(update_non_admin, context)
        update_non_admin.message.reply_text.assert_not_called()

        # Admin tanpa argumen -> tampilkan menu pilihan nominal
        update_menu = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            effective_chat=SimpleNamespace(id=-100123456, type="supergroup"),
            message=AsyncMock(message_thread_id=55),
        )
        context_empty = SimpleNamespace(args=[], user_data={})
        await topup_qris_command_handler(update_menu, context_empty)
        update_menu.message.reply_text.assert_called_once()
        args, kwargs = update_menu.message.reply_text.call_args
        self.assertIn("TOP-UP KAS BOT VIA QRIS", kwargs["text"])
        self.assertEqual(kwargs["message_thread_id"], 55)
        self.assertEqual(context_empty.user_data.get("admin_treasury_qris_chat_id"), -100123456)
        self.assertEqual(context_empty.user_data.get("admin_treasury_qris_thread_id"), 55)

        # Admin dengan nominal valid -> generate QRIS
        update_exec = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            effective_chat=SimpleNamespace(id=-100123456, type="supergroup"),
            effective_message=SimpleNamespace(message_thread_id=55),
            message=AsyncMock(message_thread_id=55),
            callback_query=None,
        )
        bot_mock = AsyncMock()
        context_exec = SimpleNamespace(
            args=["75000"],
            user_data={},
            bot=bot_mock,
        )
        await topup_qris_command_handler(update_exec, context_exec)
        # Verify photo dikirim ke chat grup dan thread topik yang benar
        bot_mock.send_photo.assert_called_once()
        _, photo_kwargs = bot_mock.send_photo.call_args
        self.assertEqual(photo_kwargs["chat_id"], -100123456)
        self.assertEqual(photo_kwargs["message_thread_id"], 55)
        self.assertIn("INVOICE TOP-UP KAS BOT", photo_kwargs["caption"])
        self.assertIn("75.000", photo_kwargs["caption"])

    async def test_admin_interactive_text_router_treasury_qris_custom(self):
        """Router teks admin memproses input angka nominal kustom QRIS dari grup/topik."""
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            effective_chat=SimpleNamespace(id=-100123456, type="supergroup"),
            effective_message=SimpleNamespace(message_thread_id=55),
            message=AsyncMock(text="5000", message_thread_id=55),
            callback_query=None,
        )
        bot_mock = AsyncMock()
        context = SimpleNamespace(
            user_data={
                "admin_awaiting_treasury_qris_custom": True,
                "admin_treasury_qris_chat_id": -100123456,
                "admin_treasury_qris_thread_id": 55,
            },
            bot=bot_mock,
        )
        handled = await admin_interactive_text_router(update, context)
        self.assertTrue(handled)
        self.assertNotIn("admin_awaiting_treasury_qris_custom", context.user_data)
        bot_mock.send_photo.assert_called_once()
        _, photo_kwargs = bot_mock.send_photo.call_args
        self.assertEqual(photo_kwargs["chat_id"], -100123456)
        self.assertEqual(photo_kwargs["message_thread_id"], 55)
        self.assertIn("5.000", photo_kwargs["caption"])


class TestAnimatedCustomEmojisAndTestimony(unittest.TestCase):
    """Pengujian integrasi Telegram animated custom emojis dan posting testimoni publik."""

    def test_animated_emoji_helper_tags(self):
        """Helper tg_emoji merender tag <tg-emoji> jika emoji-id valid atau fallback Unicode."""
        # Standard custom emojis
        gift_tag = E_GIFT()
        self.assertIn("<tg-emoji", gift_tag)
        self.assertIn("5438647000851543789", gift_tag)

        trophy_tag = E_TROPHY()
        self.assertIn("<tg-emoji", trophy_tag)
        self.assertIn("5465465407748972580", trophy_tag)

        bank_tag = E_BANK()
        self.assertIn("<tg-emoji", bank_tag)

        # Coin and network emojis
        usdt_tag = get_coin_emoji("USDT")
        self.assertIn("<tg-emoji", usdt_tag)
        self.assertIn("6172744164795490758", usdt_tag)

        bsc_tag = get_network_emoji("BSC")
        self.assertIn("<tg-emoji", bsc_tag)

    def test_anonymize_username_privacy(self):
        """Username pengguna disensor secara aman untuk channel publik."""
        self.assertEqual(anonymize_username("hendra_crypto", 123456), "@he****to")
        self.assertEqual(anonymize_username("budi", 123456), "@b****i")
        self.assertEqual(anonymize_username("al", 123456), "@a****")
        # Tanpa username
        anon = anonymize_username(None, 87654321)
        self.assertEqual(anon, "@User_87****21")

    def test_testimony_message_format_and_emojis(self):
        """Format pesan posting testimoni memuat tag custom emoji, anonymized username, dan link hash."""
        msg = format_testimony_message(
            order_type="BUY",
            crypto_symbol="USDT",
            network="BSC",
            nominal_idr=520000,
            username="hendra_crypto",
            telegram_id=12345678,
            tx_hash="0xabcdef1234567890abcdef1234567890abcdef12",
            bot_username="TokoKoinID_Bot",
        )
        self.assertIn("Transaksi Selesai", msg)
        self.assertIn("- Jenis Transaksi : Beli", msg)
        self.assertIn("- Jenis Coin : USDT BSC", msg)
        self.assertIn("- Pengguna : @he****to", msg)
        self.assertIn("- Nominal : Rp 520.000", msg)
        self.assertIn("- Transaction Hash :", msg)
        self.assertIn("https://bscscan.com/tx/0xabcdef", msg)
        self.assertIn("- Bot order : @TokoKoinID_Bot", msg)


class TestCampaignNotificationCustomization(unittest.TestCase):
    """Pengujian kustomisasi pesan notifikasi pemenang campaign dan template formatting."""

    def test_campaign_templates_include_animated_emojis(self):
        """Semua template bawaan campaign memuat placeholder dan emoji."""
        tpl_split = get_template("tpl_split_all")
        self.assertIsNotNone(tpl_split)
        self.assertIn("🎁", tpl_split["default_notif"])
        self.assertIn("{name}", tpl_split["default_notif"])
        self.assertIn("{reward}", tpl_split["default_notif"])
        self.assertIn("{new_balance}", tpl_split["default_notif"])

        tpl_loyalty = get_template("tpl_loyalty_buyers")
        self.assertIsNotNone(tpl_loyalty)
        self.assertIn("🎉", tpl_loyalty["default_notif"])

        tpl_top = get_template("tpl_top_spenders")
        self.assertIsNotNone(tpl_top)
        self.assertIn("{rank}", tpl_top["default_notif"])

    def test_custom_notification_formatting_placeholders(self):
        """Format template kustom mengganti seluruh placeholder dengan data pemenang."""
        custom_tpl = (
            "🎉 Halo <b>{name}</b>! Kamu dapat <b>{reward}</b> dari <b>{campaign_name}</b>. "
            "Saldo barumu: <b>{new_balance}</b> (Rank: #{rank}) via @{bot_username}"
        )
        rendered = format_custom_notification(
            template=custom_tpl,
            name="@alice",
            reward_amount=25000,
            new_balance=Decimal("75000"),
            campaign_name="Loyalty Flash Party",
            rank=1,
            bot_username="TokoKoinID_Bot",
        )
        self.assertIn("@alice", rendered)
        self.assertIn("25.000", rendered)
        self.assertIn("75.000", rendered)
        self.assertIn("Loyalty Flash Party", rendered)
        self.assertIn("#1", rendered)
        self.assertIn("@TokoKoinID_Bot", rendered)


if __name__ == "__main__":
    unittest.main()
