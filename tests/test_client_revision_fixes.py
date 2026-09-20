"""
tests/test_client_revision_fixes.py
===================================
Unit tests for bug fixes and features from the 18 September 2026 client revision:
1. TON native deposit verification on uninitialized / skipped compute phase addresses.
2. TON bounce rejection.
3. 120-second clock drift tolerance in verify_deposit.
4. Swap flow stock check handling available is None without TypeError.
5. Buy flow stock check and MANUAL_PAYOUT_NETWORKS rejection.
"""
import asyncio
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "DATABASE_URL": "sqlite:///:memory:",
    "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "123456",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from database.connection import Base, engine, SessionLocal
from database.models import WalletBalance, Order, User
from database import crud
from types import SimpleNamespace
from services.tx_verifier import (
    _ton_native_evidence,
    verify_deposit,
)
from bot.handlers.swap import confirm_swap_order
from bot.handlers.buy import (
    handle_network_selection,
    handle_amount_input,
    handle_order_confirmation,
    finalize_gopay_buy_payment,
    SELECT_NETWORK,
    INPUT_AMOUNT,
)
from telegram import Update, User as TgUser, Message, CallbackQuery


class TestTonNativeVerification(unittest.TestCase):
    """Test TON native transfer verification for uninitialized wallets and bounce scenarios."""

    def test_ton_native_uninitialized_wallet_transfer_succeeds(self):
        """
        Transfers to an uninitialized account have compute_ph skipped (reason: no_state)
        and aborted: True. They must be recognized as valid and verified.
        """
        bot_wallet = "0:" + "1" * 64
        sender_wallet = "0:" + "2" * 64
        tx_payload = {
            "account": bot_wallet,
            "hash": "885747ea09b78e4d3a0c4f8263fecfcfb131fbcf9e102f43bbefb3fa1aef0854",
            "now": 1726640000,
            "emulated": False,
            "in_msg": {
                "source": sender_wallet,
                "destination": bot_wallet,
                "value": "1200000000",  # 1.2 TON in nanotons
                "bounced": False,
                "opcode": None,
            },
            "out_msgs": [],
            "description": {
                "aborted": True,  # TON node sets aborted: True because compute phase was skipped
                "compute_ph": {
                    "skipped": True,
                    "reason": "no_state"
                },
                "credit_ph": {
                    "credit": "1200000000"
                }
            }
        }
        result = _ton_native_evidence(tx_payload, bot_wallet)
        self.assertTrue(result["verified"], f"Expected verified=True, got reason: {result.get('reason')}")
        self.assertAlmostEqual(float(result["amount"]), 1.2)
        self.assertEqual(result["timestamp"], 1726640000)

    def test_ton_native_bounced_in_msg_rejected(self):
        """Bounced incoming messages must be rejected."""
        bot_wallet = "0:" + "1" * 64
        tx_payload = {
            "account": bot_wallet,
            "hash": "885747ea09b78e4d3a0c4f8263fecfcfb131fbcf9e102f43bbefb3fa1aef0854",
            "now": 1726640000,
            "in_msg": {
                "source": "0:" + "2" * 64,
                "destination": bot_wallet,
                "value": "1000000000",
                "bounced": True,
            },
            "out_msgs": [],
            "description": {}
        }
        result = _ton_native_evidence(tx_payload, bot_wallet)
        self.assertFalse(result["verified"])
        self.assertIn("bounced", result["reason"])

    def test_ton_native_bounced_out_msgs_rejected(self):
        """Transactions with bounce message in out_msgs must be rejected."""
        bot_wallet = "0:" + "1" * 64
        tx_payload = {
            "account": bot_wallet,
            "hash": "885747ea09b78e4d3a0c4f8263fecfcfb131fbcf9e102f43bbefb3fa1aef0854",
            "now": 1726640000,
            "in_msg": {
                "source": "0:" + "2" * 64,
                "destination": bot_wallet,
                "value": "1000000000",
                "bounced": False,
            },
            "out_msgs": [
                {
                    "bounced": True,
                    "decoded_opcode": "bounce",
                }
            ],
            "description": {
                "compute_ph": {"skipped": True, "reason": "no_state"}
            }
        }
        result = _ton_native_evidence(tx_payload, bot_wallet)
        self.assertFalse(result["verified"])
        self.assertIn("bounce", result["reason"])


class TestClockDriftTolerance(unittest.IsolatedAsyncioTestCase):
    """Test 120s clock drift tolerance on verify_deposit."""

    async def test_clock_drift_within_120s_accepted(self):
        """A transaction timestamp 60 seconds before order creation must be accepted."""
        from services.tx_verifier import _timestamp
        order_time = datetime(2026, 9, 18, 12, 0, 0)
        tx_stamp = int(_timestamp(order_time)) - 60  # 60 seconds earlier (mined block timestamp)
        valid_hash = "a" * 64

        with patch("services.tx_verifier._verify_tron", new=AsyncMock(return_value={
            "verified": True,
            "amount": Decimal("100"),
            "timestamp": tx_stamp,
            "tx_hash": valid_hash,
            "sender_address": "sender123",
        })):
            result = await verify_deposit(
                network="TRON",
                symbol="TRX",
                tx_hash=valid_hash,
                expected_wallet="TW4x...target",
                expected_amount=Decimal("100"),
                not_before=order_time,
            )
            self.assertTrue(result["verified"], f"Deposit should be accepted within 120s drift. Got: {result.get('reason')}")

    async def test_clock_drift_exceeding_120s_rejected(self):
        """A transaction timestamp 150 seconds before order creation must be rejected."""
        from services.tx_verifier import _timestamp
        order_time = datetime(2026, 9, 18, 12, 0, 0)
        tx_stamp = int(_timestamp(order_time)) - 150  # 150 seconds earlier
        valid_hash = "a" * 64

        with patch("services.tx_verifier._verify_tron", new=AsyncMock(return_value={
            "verified": True,
            "amount": Decimal("100"),
            "timestamp": tx_stamp,
            "tx_hash": valid_hash,
            "sender_address": "sender123",
        })):
            result = await verify_deposit(
                network="TRON",
                symbol="TRX",
                tx_hash=valid_hash,
                expected_wallet="TW4x...target",
                expected_amount=Decimal("100"),
                not_before=order_time,
            )
            self.assertFalse(result["verified"])
            self.assertIn("mendahului", result["reason"])


class TestSwapStockNullSafety(unittest.IsolatedAsyncioTestCase):
    """Test swap flow handling when get_available_inventory returns None."""

    async def test_swap_available_none_does_not_raise_typeerror(self):
        """When available inventory is None, swap handler must reject gracefully, not crash."""
        query = AsyncMock()
        query.data = "swap_confirm"
        query.from_user = SimpleNamespace(id=999, username="testuser", full_name="Test User")
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)

        context = SimpleNamespace(
            user_data={
                "swap_src_symbol": "SOL",
                "swap_src_network": "SOLANA",
                "swap_src_amount": 1.0,
                "swap_tgt_symbol": "POL",
                "swap_tgt_network": "POLYGON",
                "swap_tgt_amount": 50.0,
                "swap_nominal_idr": 100000,
                "swap_fee_idr": 3000,
                "swap_target_addr": "0x" + "2" * 40,
                "swap_seller_deposit_wallet": "SoL_wallet_123",
            },
            bot=AsyncMock(),
        )

        with patch("services.wallet_sync.sync_wallet_balances", new=AsyncMock()), \
             patch("database.crud.get_available_inventory", return_value=None):
            # This should complete gracefully without TypeError
            res = await confirm_swap_order(update, context)
            self.assertEqual(res, -1)  # ConversationHandler.END is -1
            query.edit_message_text.assert_called_once()
            call_text = query.edit_message_text.call_args[0][0]
            self.assertIn("Stok tujuan tidak cukup atau belum terverifikasi", call_text)


class TestSolanaMainnetGuard(unittest.TestCase):
    """Genesis hash terpotong akan menolak semua payout Solana di preflight."""

    def test_genesis_hash_is_full_44_characters(self):
        from services.crypto_sender.solana_sender import SOLANA_MAINNET_GENESIS_HASH
        self.assertEqual(SOLANA_MAINNET_GENESIS_HASH, "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d")
        self.assertEqual(len(SOLANA_MAINNET_GENESIS_HASH), 44)


class TestBuyFlowStockValidation(unittest.IsolatedAsyncioTestCase):
    """Test buy flow pre-payment stock checks and manual network checks."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def test_buy_manual_payout_network_blocked(self):
        """Selecting a network in MANUAL_PAYOUT_NETWORKS (e.g. SUI / APTOS) must block auto-purchase."""
        query = AsyncMock()
        query.data = "buy_net_SUI_SUI"
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(user_data={})

        state = await handle_network_selection(update, context)
        self.assertEqual(state, SELECT_NETWORK)
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args.kwargs.get("text", "")
        self.assertIn("Pengiriman Otomatis Belum Tersedia", text)

    async def test_buy_amount_input_warns_if_stock_insufficient(self):
        """Entering an amount that exceeds available stock must reject early at INPUT_AMOUNT."""
        # Insert a wallet balance for HYPE with very low balance
        wb = WalletBalance(
            network="HYPEREVM",
            symbol="HYPE",
            address="0x" + "1" * 40,
            balance=Decimal("0.03197"),
            reserved_balance=Decimal("0"),
            sync_status="OK",
            last_success_at=datetime.utcnow(),
            last_checked_at=datetime.utcnow(),
        )
        self.db.add(wb)
        self.db.commit()

        message = AsyncMock()
        message.text = "500000"  # Rp 500.000 buys ~3.5 HYPE, but stock is only 0.03197
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=123))
        context = SimpleNamespace(
            user_data={
                "buy_symbol": "HYPE",
                "buy_network": "HYPEREVM",
            }
        )

        with patch("services.price_service.PriceService.get_price", new=AsyncMock(return_value={
            "symbol": "HYPE",
            "buy_price_idr": 140000,
            "sell_price_idr": 135000,
            "source": "MOCK",
            "price_updated_at": int(datetime.utcnow().timestamp()),
            "spread_pct": 1.5,
        })), patch("bot.handlers.buy.SessionLocal", side_effect=lambda: SessionLocal()):
            state = await handle_amount_input(update, context)
            self.assertEqual(state, INPUT_AMOUNT)
            message.reply_text.assert_called_once()
            call_text = message.reply_text.call_args.kwargs.get("text", "")
            self.assertIn("Stok HYPE (HYPEREVM) Tidak Mencukupi", call_text)

    async def test_paid_order_refreshes_stale_stock_before_manual_review(self):
        """Pesanan yang sudah dibayar tidak boleh ke manual review hanya karena baris stok basi."""
        import os
        import tempfile
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        stock_engine = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'stock.db')}")
        Base.metadata.create_all(bind=stock_engine)
        StockSession = sessionmaker(bind=stock_engine, autoflush=False, expire_on_commit=False)

        seed = StockSession()
        seed.add(Order(
            order_id="BUY-STALE-1",
            telegram_id=777,
            order_type="buy",
            crypto_symbol="HYPE",
            network="HYPEREVM",
            crypto_amount=Decimal("0.01"),
            price_per_unit=140000,
            nominal_idr=50000,
            fee_idr=3000,
            total_idr=50000,
            buyer_wallet="0x" + "3" * 40,
            payment_method="GOPAY_QRIS",
            status="paid",
        ))
        seed.add(WalletBalance(
            network="HYPEREVM",
            symbol="HYPE",
            address="0x" + "1" * 40,
            balance=Decimal("0.03197"),
            reserved_balance=Decimal("0"),
            sync_status="ERROR",
            last_error="Pembacaan saldo HYPE/HYPEREVM gagal (RuntimeError).",
            last_success_at=datetime.utcnow() - timedelta(hours=2),
            last_checked_at=datetime.utcnow() - timedelta(hours=2),
        ))
        seed.commit()
        seed.close()

        async def fake_sync(assets=None):
            other = StockSession()
            try:
                row = other.query(WalletBalance).filter_by(network="HYPEREVM", symbol="HYPE").first()
                row.balance = Decimal("1")
                row.sync_status = "OK"
                row.last_error = None
                row.last_success_at = datetime.utcnow()
                other.commit()
            finally:
                other.close()
            return {"success": ["HYPE (HYPEREVM)"], "failed": []}

        db = StockSession()
        try:
            order = db.query(Order).filter_by(order_id="BUY-STALE-1").first()
            payout = AsyncMock(return_value={
                "success": True, "tx_hash": "0xfeed", "explorer_url": "", "error_message": "",
            })
            with patch("services.wallet_sync.sync_wallet_balances", new=AsyncMock(side_effect=fake_sync)), \
                 patch("services.payout_service.send_order_payout", new=payout), \
                 patch("bot.handlers.buy.safe_send_message", new=AsyncMock()), \
                 patch("bot.handlers.buy.notify_admins", new=AsyncMock()):
                await finalize_gopay_buy_payment(db, order, bot=AsyncMock())

            self.assertEqual(order.status, "completed")
            self.assertEqual(order.payout_tx_hash, "0xfeed")
            payout.assert_awaited_once()
        finally:
            db.close()

    async def test_buy_order_confirmation_rejects_if_stock_unavailable(self):
        """Confirmation step must reject order creation if stock is insufficient."""
        query = AsyncMock()
        query.from_user = SimpleNamespace(id=123, username="buyer", full_name="Buyer")
        query.message = AsyncMock()
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(
            user_data={
                "buy_order_id": "BUY_TEST_123",
                "buy_symbol": "HYPE",
                "buy_network": "HYPEREVM",
                "buy_crypto_amount": 5.0,
                "buy_price_per_unit": 140000,
                "buy_nominal_idr": 700000,
                "buy_fee_idr": 3000,
                "buy_received_idr": 697000,
                "buy_total_idr": 700000,
                "buy_wallet": "0x" + "3" * 40,
                "buy_pay_method": "GOPAY_QRIS",
            },
            bot=AsyncMock(),
        )

        with patch("bot.handlers.buy.get_available_inventory", return_value=Decimal("0.03197")), \
             patch("database.crud.get_available_inventory", return_value=Decimal("0.03197")), \
             patch("bot.handlers.buy.SessionLocal", side_effect=lambda: SessionLocal()):
            state = await handle_order_confirmation(update, context)
            self.assertEqual(state, -1)  # ConversationHandler.END
            query.edit_message_text.assert_called_once()
            call_text = query.edit_message_text.call_args.kwargs.get("text", "")
            self.assertIn("Stok HYPE (HYPEREVM) tidak mencukupi", call_text)


if __name__ == "__main__":
    from types import SimpleNamespace
    unittest.main()
