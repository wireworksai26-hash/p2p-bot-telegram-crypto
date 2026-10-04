"""
tests/test_phase8.py — Unit & Integration Tests for Phase 8
============================================================
Covers:
1. Campaign target & template refinements (tpl_split_all for buyers only)
2. Checkout confirmation copy cleanup (removal of (01-200))
3. Testimonial channel service (anonymization, message formatting, posting)
4. Post-transaction thank you footer update
5. Weekly transaction report & CSV generation
6. Referral share link formatting (single link verification)
"""

import io
import csv
import os
import sys
import asyncio
import unittest
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
from database.models import User, Order, AuditLog
from database import crud
from services.campaign_service import (
    CAMPAIGN_TEMPLATES,
    simulate_campaign,
    get_template,
)
from services.testimony_service import (
    anonymize_username,
    get_explorer_url_for_tx,
    format_testimony_message,
    post_transaction_testimony,
)
from services.report_service import (
    get_weekly_transactions_data,
    calculate_weekly_summary,
    generate_weekly_report_csv_buffer,
    format_weekly_report_telegram_message,
)


class TestCampaignTemplateRefinements(unittest.TestCase):
    """Test 8.1: Template campaign Bagi Rata & Loyalty Buyer."""

    def test_tpl_split_all_targets_buyers(self):
        tpl = get_template("tpl_split_all")
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl["target_segment"], "buyers")
        self.assertIn("transaksi 1 kali", tpl["description"])

    def test_tpl_loyalty_buyers_copy(self):
        tpl = get_template("tpl_loyalty_buyers")
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl["target_segment"], "buyers")
        self.assertIn("loyalty", tpl["description"].lower())

    def test_simulate_campaign_split_all_selects_only_buyers(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            # User 1: Beli (Completed)
            u1 = crud.create_user(db, telegram_id=81111, username="buyer1")
            # User 2: Non-buyer (No completed order)
            u2 = crud.create_user(db, telegram_id=82222, username="nonbuyer")
            db.commit()

            # Create completed order for u1
            crud.create_order(db, {
                "order_id": "ORD-P8-TPL1",
                "telegram_id": 81111,
                "order_type": "buy",
                "crypto_symbol": "USDT",
                "network": "BSC",
                "crypto_amount": Decimal("10.0"),
                "price_per_unit": 16000,
                "nominal_idr": 160000,
                "fee_idr": 2000,
                "total_idr": 160000,
                "status": "completed",
            })

            sim = simulate_campaign(
                db=db,
                mode="EQUAL_SPLIT",
                total_pool=100_000,
                target_segment="buyers",
            )
            self.assertEqual(sim["winner_count"], 1)
            self.assertEqual(sim["winners"][0]["telegram_id"], 81111)
        finally:
            db.close()


class TestCheckoutCopyCleanup(unittest.TestCase):
    """Test 8.2: Hapus range (01-200) dari konfirmasi beli."""

    def test_buy_confirmation_text_contains_no_range(self):
        # Periksa string konfirmasi di file bot/handlers/buy.py
        with open(ROOT / "bot" / "handlers" / "buy.py", "r", encoding="utf-8") as f:
            content = f.read()

        self.assertNotIn("(01-200)", content)
        self.assertIn("Kode unik akan ditambahkan ke total bayar", content)


class TestTestimonialService(unittest.TestCase):
    """Test 8.3: Service Testimoni Transaksi."""

    def test_anonymize_username(self):
        # Long username
        self.assertEqual(anonymize_username("hendra_crypto", 12345), "@he****to")
        # 4-char username
        self.assertEqual(anonymize_username("budi", 12345), "@b****i")
        # 2-char username
        self.assertEqual(anonymize_username("za", 12345), "@z****")
        # Empty / None username -> uses Telegram ID
        self.assertEqual(anonymize_username(None, 8773623977), "@User_87****77")

    def test_get_explorer_url_for_tx(self):
        bsc_url = get_explorer_url_for_tx("BSC", "0xabc1234567890abcdef1234567890abcdef123456")
        self.assertIn("bscscan.com/tx/0xabc", bsc_url)

        sol_url = get_explorer_url_for_tx("SOLANA", "5abc123456789")
        self.assertIn("solscan.io/tx/5abc", sol_url)

        ton_url = get_explorer_url_for_tx("TON", "msg:abcdef123456")
        self.assertIn("tonviewer.com/transaction/abcdef", ton_url)

    def test_format_testimony_message_buy(self):
        msg = format_testimony_message(
            order_type="buy",
            crypto_symbol="USDT",
            network="BSC",
            nominal_idr=520_000,
            username="hendra_hidayat",
            telegram_id=12345678,
            tx_hash="0x1234567890abcdef1234567890abcdef12345678",
            bot_username="TokoKoinID_Bot",
        )
        self.assertIn("Transaksi Selesai", msg)
        self.assertIn("- Jenis Transaksi : Beli", msg)
        self.assertIn("- Jenis Coin : USDT BSC", msg)
        self.assertIn("- Pengguna : @he****at", msg)
        self.assertIn("- Nominal : Rp 520.000", msg)
        self.assertIn("- Transaction Hash : <a href=", msg)
        self.assertIn("- Bot order : @TokoKoinID_Bot", msg)

    def test_format_testimony_message_swap(self):
        msg = format_testimony_message(
            order_type="swap",
            crypto_symbol="USDT",
            network="BSC",
            nominal_idr=100_000,
            username=None,
            telegram_id=8773623977,
            target_symbol="SOL",
            target_network="SOLANA",
            tx_hash=None,
            bot_username="TokoKoinID_Bot",
        )
        self.assertIn("- Jenis Transaksi : Swap", msg)
        self.assertIn("- Jenis Coin : USDT -> SOL (SOLANA)", msg)
        self.assertIn("- Pengguna : @User_87****77", msg)
        self.assertIn("- Transaction Hash : -", msg)

    def test_post_transaction_testimony_async(self):
        async def _test():
            bot_mock = AsyncMock()
            bot_mock.get_me = AsyncMock(return_value=MagicMock(username="TokoKoinID_Bot"))
            bot_mock.send_message = AsyncMock(return_value=MagicMock())

            order = MagicMock()
            order.order_id = "ORD-TEST-001"
            order.order_type = "buy"
            order.crypto_symbol = "USDT"
            order.network = "BSC"
            order.total_idr = 520000
            order.telegram_id = 999999
            order.user_username = "tester88"
            order.payout_tx_hash = "0xabcde"

            res = await post_transaction_testimony(bot_mock, order, channel="@TokoKoinID")
            self.assertTrue(res)
            bot_mock.send_message.assert_called_once()
            call_kwargs = bot_mock.send_message.call_args[1]
            self.assertEqual(call_kwargs["chat_id"], "@TokoKoinID")
            self.assertIn("Transaksi Selesai", call_kwargs["text"])

        asyncio.run(_test())


class TestPostTransactionFooter(unittest.TestCase):
    """Test 8.4: Footer Terima Kasih setelah Transaksi."""

    def test_footer_strings_in_watchdog_and_handlers(self):
        with open(ROOT / "services" / "payout_watchdog.py", "r", encoding="utf-8") as f:
            watchdog_code = f.read()
        self.assertIn("Terimakasih sudah bertransaksi di sini, Lancar selalu 🙏🙏", watchdog_code)
        self.assertIn("Testimoni : t.me/TokoKoinID", watchdog_code)
        self.assertIn("Channel : t.me/ROBHSN_STORE_SELLER", watchdog_code)

        with open(ROOT / "bot" / "handlers" / "buy.py", "r", encoding="utf-8") as f:
            buy_code = f.read()
        self.assertIn("Terimakasih sudah bertransaksi di sini, Lancar selalu 🙏🙏", buy_code)
        self.assertIn("Testimoni : t.me/TokoKoinID", buy_code)
        self.assertIn("Channel : t.me/ROBHSN_STORE_SELLER", buy_code)

        with open(ROOT / "bot" / "handlers" / "admin.py", "r", encoding="utf-8") as f:
            admin_code = f.read()
        self.assertIn("Terimakasih sudah bertransaksi di sini, Lancar selalu 🙏🙏", admin_code)
        self.assertIn("Testimoni : t.me/TokoKoinID", admin_code)
        self.assertIn("Channel : t.me/ROBHSN_STORE_SELLER", admin_code)


class TestWeeklyReportingService(unittest.TestCase):
    """Test 8.5: Service Rekap Laporan Transaksi Mingguan & Export CSV."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def test_weekly_report_data_and_summary(self):
        # Create users
        u1 = crud.create_user(self.db, telegram_id=89001, username="trader_one")
        u2 = crud.create_user(self.db, telegram_id=89002, username="trader_two")

        # Create orders in last 7 days
        crud.create_order(self.db, {
            "order_id": "ORD-W1",
            "telegram_id": 89001,
            "order_type": "buy",
            "crypto_symbol": "USDT",
            "network": "BSC",
            "crypto_amount": Decimal("100.0"),
            "price_per_unit": 16000,
            "nominal_idr": 1600000,
            "fee_idr": 10000,
            "total_idr": 1600000,
            "status": "completed",
            "buyer_wallet": "0x71C839556CB3250b716773B3aBE329a4a796c9c6",
        })

        crud.create_order(self.db, {
            "order_id": "ORD-W2",
            "telegram_id": 89002,
            "order_type": "sell",
            "crypto_symbol": "SOL",
            "network": "SOLANA",
            "crypto_amount": Decimal("2.0"),
            "price_per_unit": 2000000,
            "nominal_idr": 4000000,
            "fee_idr": 15000,
            "total_idr": 4000000,
            "status": "completed",
            "buyer_wallet": "BCA 12345678 a/n Budi",
        })

        txs = get_weekly_transactions_data(self.db, days=7)
        self.assertGreaterEqual(len(txs), 2)

        summary = calculate_weekly_summary(txs, days=7)
        self.assertGreaterEqual(summary["completed_count"], 2)
        self.assertGreaterEqual(summary["vol_beli"], 1_600_000)
        self.assertGreaterEqual(summary["vol_jual"], 4_000_000)
        self.assertGreaterEqual(summary["total_turnover"], 5_600_000)
        self.assertGreaterEqual(summary["total_fees"], 25_000)

        # Telegram text formatting
        text = format_weekly_report_telegram_message(summary)
        self.assertIn("REKAP LAPORAN TRANSAKSI (7 HARI TERAKHIR)", text)
        self.assertIn("Volume Beli (Buy)", text)
        self.assertIn("Volume Jual (Sell)", text)

        # CSV generation
        csv_buf = generate_weekly_report_csv_buffer(txs)
        self.assertIsInstance(csv_buf, io.BytesIO)
        content = csv_buf.getvalue().decode("utf-8-sig")
        self.assertIn("ID Order", content)
        self.assertIn("ORD-W1", content)
        self.assertIn("ORD-W2", content)
        self.assertIn("0x71C839556CB3250b716773B3aBE329a4a796c9c6", content)


class TestReferralShareLink(unittest.TestCase):
    """Test 8.6: Validasi URL Bagikan Referral Tidak Duplikat."""

    def test_referral_share_text_contains_no_duplicate_url(self):
        with open(ROOT / "bot" / "handlers" / "referral.py", "r", encoding="utf-8") as f:
            ref_code = f.read()

        # share_text places the message on top and the link below cleanly
        self.assertIn("share_text = f\"Yuk beli dan jual crypto mudah, cepat & terpercaya di HSN Store! Daftar lewat link ini ya:\\n{ref_link}\"", ref_code)
        self.assertIn("share_url = f\"https://t.me/share/url?url={quote(share_text)}\"", ref_code)


if __name__ == "__main__":
    unittest.main()
