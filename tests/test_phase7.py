"""
tests/test_phase7.py — Comprehensive Unit & Integration Tests for Phase 7
========================================================================
Covers:
1. Wallet Detector (EVM, Solana, Tron, SUI, TON, Bitcoin) & validation
2. Referral Discount (10% discount on transaction fee for 10 uses)
3. Time-based Loyalty Reward (5 transactions in 5 days window)
4. Top Spender Leaderboard & Milestone Rewards (Top 10 tier rewards)
5. Random Winner Draw (Pool selection, random picking, atomic balance credit)
6. Enhanced Wallet Management (Grouped by chain, auto-detect, default wallet)
"""

import os
import sys
import asyncio
import unittest
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

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
from database.models import (
    User,
    Order,
    UserSavedWallet,
    Referral,
    ReferralDiscount,
    LoyaltyReward,
    LoyaltyConfig,
    Campaign,
    CampaignDistribution,
    AuditLog,
)
from database import crud
from services.wallet_detector import (
    detect_wallet_network,
    validate_wallet_address,
    get_chain_type_for_network,
)
from services.referral_discount_service import (
    calculate_discounted_fee,
    apply_referral_discount_if_eligible,
    activate_discount_for_referrer,
    REFERRAL_DISCOUNT_PCT,
    REFERRAL_DISCOUNT_USES,
)
from services.loyalty_service import (
    process_loyalty_after_order,
)
from services.campaign_service import (
    TOP_SPENDER_REWARDS,
    execute_top_spender_campaign,
    execute_random_winner_campaign,
)


class TestWalletDetector(unittest.TestCase):
    """Pengujian modul deteksi format alamat wallet crypto."""

    def test_detect_evm(self):
        bsc_addr = "0x71C839556CB3250b716773B3aBE329a4a796c9c6"
        res = detect_wallet_network(bsc_addr)
        self.assertIsNotNone(res)
        self.assertEqual(res["chain_type"], "EVM")
        self.assertIn("BSC", res["chains"])
        self.assertIn("ETH", res["chains"])

    def test_detect_solana(self):
        sol_addr = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
        res = detect_wallet_network(sol_addr)
        self.assertIsNotNone(res)
        self.assertEqual(res["chain_type"], "SOLANA")

    def test_detect_tron(self):
        trx_addr = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
        res = detect_wallet_network(trx_addr)
        self.assertIsNotNone(res)
        self.assertEqual(res["chain_type"], "TRON")

    def test_detect_sui(self):
        sui_addr = "0x524c7dbda9eb0fca02d68a2e1d7cf9d164dfb3a99bbbebd04353f81e354a8677"
        res = detect_wallet_network(sui_addr)
        self.assertIsNotNone(res)
        self.assertEqual(res["chain_type"], "SUI")

    def test_detect_ton(self):
        ton_addr = "EQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N"
        res = detect_wallet_network(ton_addr)
        self.assertIsNotNone(res)
        self.assertEqual(res["chain_type"], "TON")

    def test_detect_bitcoin(self):
        btc_native_segwit = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
        res = detect_wallet_network(btc_native_segwit)
        self.assertIsNotNone(res)
        self.assertEqual(res["chain_type"], "BITCOIN")

        btc_legacy = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
        res2 = detect_wallet_network(btc_legacy)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["chain_type"], "BITCOIN")

    def test_validate_wallet_address(self):
        evm = "0x71C839556CB3250b716773B3aBE329a4a796c9c6"
        self.assertTrue(validate_wallet_address(evm, "BSC"))
        self.assertTrue(validate_wallet_address(evm, "ETH"))
        self.assertTrue(validate_wallet_address(evm, "POLYGON"))
        self.assertFalse(validate_wallet_address(evm, "SOLANA"))
        self.assertFalse(validate_wallet_address(evm, "TRC20"))

        sol = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
        self.assertTrue(validate_wallet_address(sol, "SOLANA"))
        self.assertFalse(validate_wallet_address(sol, "BSC"))

    def test_get_chain_type_for_network(self):
        self.assertEqual(get_chain_type_for_network("BSC"), "EVM")
        self.assertEqual(get_chain_type_for_network("ETH"), "EVM")
        self.assertEqual(get_chain_type_for_network("SOLANA"), "SOLANA")
        self.assertEqual(get_chain_type_for_network("TRC20"), "TRON")
        self.assertEqual(get_chain_type_for_network("TON"), "TON")
        self.assertEqual(get_chain_type_for_network("SUI"), "SUI")


class TestReferralDiscount(unittest.TestCase):
    """Pengujian sistem diskon referral (10% potongan untuk 10 kali transaksi)."""

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.user = User(telegram_id=111001, username="test_referrer", balance_idr=Decimal("0"))
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_calculate_discounted_fee(self):
        # 10% discount on 5000 fee
        final_fee, discount_amt = calculate_discounted_fee(5000, 10.0)
        self.assertEqual(discount_amt, 500)
        self.assertEqual(final_fee, 4500)

        # 0 fee
        final_fee_0, disc_0 = calculate_discounted_fee(0, 10.0)
        self.assertEqual(final_fee_0, 0)
        self.assertEqual(disc_0, 0)

    def test_activate_and_consume_discount(self):
        # 1. Initially inactive
        info = crud.get_referral_discount_info(self.db, self.user.telegram_id)
        self.assertFalse(info["active"])
        self.assertEqual(info["remaining"], 0)

        # 2. Activate
        disc = crud.activate_referral_discount(self.db, self.user.telegram_id, uses=10, pct=10.0)
        self.assertEqual(disc.remaining_uses, 10)
        self.assertEqual(float(disc.discount_pct), 10.0)

        # 3. Check info
        info = crud.get_referral_discount_info(self.db, self.user.telegram_id)
        self.assertTrue(info["active"])
        self.assertEqual(info["remaining"], 10)

        # 4. Consume 1 slot
        pct = crud.consume_referral_discount(self.db, self.user.telegram_id)
        self.assertEqual(pct, 10.0)
        info_after = crud.get_referral_discount_info(self.db, self.user.telegram_id)
        self.assertEqual(info_after["remaining"], 9)

        # 5. Consume all remaining 9 slots
        for _ in range(9):
            crud.consume_referral_discount(self.db, self.user.telegram_id)

        # 6. Now exhausted
        info_exhausted = crud.get_referral_discount_info(self.db, self.user.telegram_id)
        self.assertFalse(info_exhausted["active"])
        self.assertEqual(info_exhausted["remaining"], 0)

        # 7. Consume when 0 returns None
        none_pct = crud.consume_referral_discount(self.db, self.user.telegram_id)
        self.assertIsNone(none_pct)

    def test_apply_referral_discount_service(self):
        crud.activate_referral_discount(self.db, self.user.telegram_id, uses=3, pct=10.0)

        async def _run():
            res = await apply_referral_discount_if_eligible(
                telegram_id=self.user.telegram_id,
                base_fee_idr=10000,
                db=self.db,
            )
            return res

        res = asyncio.run(_run())
        self.assertTrue(res["applied"])
        self.assertEqual(res["discount_pct"], 10.0)
        self.assertEqual(res["discount_amount"], 1000)
        self.assertEqual(res["final_fee"], 9000)
        self.assertEqual(res["remaining_after"], 2)


class TestLoyaltySystem(unittest.TestCase):
    """Pengujian sistem Time-based Loyalty Reward (5 transaksi dalam 5 hari)."""

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.user = User(telegram_id=222001, username="loyal_user", balance_idr=Decimal("0"))
        self.db.add(self.user)
        self.db.commit()

        # Set default loyalty config
        crud.set_loyalty_config(self.db, "loyalty_enabled", "true")
        crud.set_loyalty_config(self.db, "window_days", "5")
        crud.set_loyalty_config(self.db, "min_tx_count", "5")
        crud.set_loyalty_config(self.db, "reward_amount_idr", "25000")
        crud.set_loyalty_config(self.db, "min_tx_amount_idr", "50000")

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_loyalty_window_creation_and_increment(self):
        t_id = self.user.telegram_id

        # 1. First transaction below min nominal -> skipped from progress
        res1 = crud.increment_loyalty_tx(self.db, t_id, tx_amount_idr=10_000)
        self.assertTrue(res1.get("skipped"))
        self.assertEqual(res1["current_count"], 0)

        # 2. First valid transaction
        res2 = crud.increment_loyalty_tx(self.db, t_id, tx_amount_idr=100_000)
        self.assertFalse(res2.get("skipped", False))
        self.assertEqual(res2["current_count"], 1)
        self.assertFalse(res2["qualified"])
        self.assertEqual(res2["needed"], 4)

        # 3. Transactions 2, 3, 4
        crud.increment_loyalty_tx(self.db, t_id, tx_amount_idr=100_000)
        crud.increment_loyalty_tx(self.db, t_id, tx_amount_idr=100_000)
        res4 = crud.increment_loyalty_tx(self.db, t_id, tx_amount_idr=100_000)
        self.assertEqual(res4["current_count"], 4)
        self.assertFalse(res4["qualified"])
        self.assertEqual(res4["needed"], 1)

        # 4. 5th transaction -> Newly qualified!
        res5 = crud.increment_loyalty_tx(self.db, t_id, tx_amount_idr=100_000)
        self.assertEqual(res5["current_count"], 5)
        self.assertTrue(res5["qualified"])
        self.assertTrue(res5.get("newly_qualified"))
        self.assertEqual(res5["needed"], 0)

    def test_process_loyalty_after_order_credits_balance(self):
        t_id = self.user.telegram_id
        mock_bot = AsyncMock()

        async def _run():
            # 4 transactions
            for _ in range(4):
                await process_loyalty_after_order(t_id, 100_000, mock_bot, self.db)

            # 5th transaction -> should qualify and credit 25k
            res = await process_loyalty_after_order(t_id, 100_000, mock_bot, self.db)
            return res

        final_res = asyncio.run(_run())
        self.assertEqual(final_res["action"], "rewarded")
        self.assertTrue(final_res["qualified"])
        self.assertEqual(final_res["reward_idr"], 25_000)

        # Verify user balance updated
        user_in_db = self.db.query(User).filter(User.telegram_id == t_id).first()
        self.assertEqual(user_in_db.balance_idr, Decimal("25000"))

        # Verify 6th transaction does not double-reward
        res6 = asyncio.run(process_loyalty_after_order(t_id, 100_000, mock_bot, self.db))
        self.assertEqual(res6["action"], "already_rewarded")
        user_in_db2 = self.db.query(User).filter(User.telegram_id == t_id).first()
        self.assertEqual(user_in_db2.balance_idr, Decimal("25000"))


class TestTopSpendersCampaign(unittest.TestCase):
    """Pengujian Top Spender Leaderboard dan eksekusi campaign tier milestone."""

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

        self.users = []
        for i in range(1, 6):
            u = User(telegram_id=300000 + i, username=f"spender_{i}", balance_idr=Decimal("0"))
            self.db.add(u)
            self.users.append(u)
        self.db.commit()

        # Seed completed buy orders with different amounts
        amounts = [50_000_000, 30_000_000, 20_000_000, 10_000_000, 5_000_000]
        for i, amt in enumerate(amounts):
            order = Order(
                order_id=f"TEST_BUY_{i+1}",
                telegram_id=self.users[i].telegram_id,
                order_type="buy",
                crypto_symbol="USDT",
                network="BSC",
                crypto_amount=Decimal("100"),
                price_per_unit=16000,
                nominal_idr=amt,
                fee_idr=5000,
                total_idr=amt,
                status="COMPLETED",
                created_at=datetime.utcnow() - timedelta(days=2),
            )
            self.db.add(order)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_get_top_spenders_ranking(self):
        top = crud.get_top_spenders(self.db, limit=10, period_days=30)
        self.assertEqual(len(top), 5)
        self.assertEqual(top[0]["telegram_id"], self.users[0].telegram_id)
        self.assertEqual(top[0]["rank"], 1)
        self.assertEqual(top[0]["total_spent_idr"], 50_000_000)

        self.assertEqual(top[1]["telegram_id"], self.users[1].telegram_id)
        self.assertEqual(top[1]["rank"], 2)
        self.assertEqual(top[1]["total_spent_idr"], 30_000_000)

    def test_execute_top_spender_campaign(self):
        mock_bot = AsyncMock()
        # Hadiah milestone selalu dari Kas Bot (keputusan client) — isi dulu.
        crud.topup_bot_treasury(self.db, 1_000_000, admin_id=999)

        async def _run():
            token = crud.issue_admin_action_token(self.db, 999, "top_spender", "30")
            return await execute_top_spender_campaign(
                db=self.db,
                bot=mock_bot,
                admin_id=999,
                period_days=30,
                action_token=token,
            )

        res = asyncio.run(_run())
        self.assertEqual(res["distributed_count"], 5)
        self.assertIsNone(res["error"])

        # Check balances credited according to tier rewards
        u1 = self.db.query(User).filter(User.telegram_id == self.users[0].telegram_id).first()
        u2 = self.db.query(User).filter(User.telegram_id == self.users[1].telegram_id).first()
        u3 = self.db.query(User).filter(User.telegram_id == self.users[2].telegram_id).first()
        u4 = self.db.query(User).filter(User.telegram_id == self.users[3].telegram_id).first()
        u5 = self.db.query(User).filter(User.telegram_id == self.users[4].telegram_id).first()

        self.assertEqual(u1.balance_idr, Decimal(str(TOP_SPENDER_REWARDS[1])))
        self.assertEqual(u2.balance_idr, Decimal(str(TOP_SPENDER_REWARDS[2])))
        self.assertEqual(u3.balance_idr, Decimal(str(TOP_SPENDER_REWARDS[3])))
        self.assertEqual(u4.balance_idr, Decimal(str(TOP_SPENDER_REWARDS[4])))
        self.assertEqual(u5.balance_idr, Decimal(str(TOP_SPENDER_REWARDS[5])))


class TestRandomWinnerCampaign(unittest.TestCase):
    """Pengujian undian pemenang acak (Flash Giveaway)."""

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

        from database.models import Order
        for i in range(1, 21):
            u = User(telegram_id=400000 + i, username=f"random_user_{i}", balance_idr=Decimal("0"))
            self.db.add(u)
        self.db.commit()
        # Pool undian hanya user yang pernah menyelesaikan transaksi (anti akun kosong).
        for i in range(1, 21):
            self.db.add(Order(
                order_id=f"ORD-RW-{i}", telegram_id=400000 + i, order_type="buy", crypto_symbol="USDT",
                network="BSC", crypto_amount=Decimal("1"), price_per_unit=16000, nominal_idr=16000,
                fee_idr=0, total_idr=16000, buyer_wallet="0x" + "1" * 40, status="completed"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_akun_kosong_tidak_ikut_undian(self):
        for i in range(21, 31):  # 10 akun yang cuma /start
            self.db.add(User(telegram_id=400000 + i, username=f"kosong_{i}", balance_idr=Decimal("0")))
        self.db.commit()
        winners = crud.get_random_winners(self.db, pool_segment="ALL", count=100)
        ids = {w["telegram_id"] for w in winners}
        self.assertEqual(len(ids), 20)
        self.assertTrue(all(400000 < t <= 400020 for t in ids))

    def test_transaksi_di_bawah_minimum_tidak_ikut(self):
        from database.models import Order
        self.db.add(User(telegram_id=499999, username="receh", balance_idr=Decimal("0")))
        self.db.add(Order(
            order_id="ORD-RW-RECEH", telegram_id=499999, order_type="buy", crypto_symbol="USDT",
            network="BSC", crypto_amount=Decimal("0.1"), price_per_unit=16000, nominal_idr=1600,
            fee_idr=0, total_idr=1600, buyer_wallet="0x" + "1" * 40, status="completed"))
        self.db.commit()
        winners = crud.get_random_winners(self.db, pool_segment="ALL", count=100)
        self.assertNotIn(499999, {w["telegram_id"] for w in winners})

    def test_get_random_winners(self):
        winners = crud.get_random_winners(self.db, pool_segment="ALL", count=5)
        self.assertEqual(len(winners), 5)
        # Ensure all IDs are unique
        ids = [w["telegram_id"] for w in winners]
        self.assertEqual(len(ids), len(set(ids)))

    def test_execute_random_winner_campaign(self):
        mock_bot = AsyncMock()

        async def _run():
            token = crud.issue_admin_action_token(self.db, 999, "random_draw", "ALL|5|50000")
            return await execute_random_winner_campaign(
                db=self.db,
                bot=mock_bot,
                admin_id=999,
                pool_segment="ALL",
                winner_count=5,
                reward_per_winner=50_000,
                action_token=token,
            )

        res = asyncio.run(_run())
        self.assertEqual(res["distributed_count"], 5)
        self.assertEqual(res["total_amount"], 250_000)

        # Check that winners received 50_000
        for w in res["winners"]:
            u = self.db.query(User).filter(User.telegram_id == w["telegram_id"]).first()
            self.assertEqual(u.balance_idr, Decimal("50000"))


class TestEnhancedWalletManagement(unittest.TestCase):
    """Pengujian manajemen wallet per jaringan, auto-detect, dan default wallet."""

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.user = User(telegram_id=500001, username="wallet_user", balance_idr=Decimal("0"))
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_save_and_group_wallets(self):
        t_id = self.user.telegram_id

        # Save 2 EVM wallets
        w1 = crud.save_user_wallet_v2(
            self.db, t_id,
            wallet_address="0x71C839556CB3250b716773B3aBE329a4a796c9c6",
            network="BSC", chain_type="EVM", label="Metamask BSC", auto_detected=True
        )
        w2 = crud.save_user_wallet_v2(
            self.db, t_id,
            wallet_address="0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
            network="ETH", chain_type="EVM", label="TrustWallet ETH", auto_detected=True
        )

        # Save 1 Solana wallet
        w3 = crud.save_user_wallet_v2(
            self.db, t_id,
            wallet_address="9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
            network="SOLANA", chain_type="SOLANA", label="Phantom SOL", auto_detected=True
        )

        # Save 1 Tron wallet
        w4 = crud.save_user_wallet_v2(
            self.db, t_id,
            wallet_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            network="TRC20", chain_type="TRON", label="TronLink USDT", auto_detected=True
        )

        # Test grouped view
        grouped = crud.get_saved_wallets_grouped(self.db, t_id)
        self.assertIn("EVM", grouped)
        self.assertIn("SOLANA", grouped)
        self.assertIn("TRON", grouped)
        self.assertEqual(len(grouped["EVM"]), 2)
        self.assertEqual(len(grouped["SOLANA"]), 1)
        self.assertEqual(len(grouped["TRON"]), 1)

    def test_set_default_wallet(self):
        t_id = self.user.telegram_id

        w1 = crud.save_user_wallet_v2(
            self.db, t_id,
            wallet_address="0x71C839556CB3250b716773B3aBE329a4a796c9c6",
            network="BSC", chain_type="EVM"
        )
        w2 = crud.save_user_wallet_v2(
            self.db, t_id,
            wallet_address="0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
            network="ETH", chain_type="EVM"
        )

        # Set w1 as default
        crud.set_default_wallet(self.db, w1.id, t_id)
        self.db.refresh(w1)
        self.db.refresh(w2)
        self.assertTrue(w1.is_default)
        self.assertFalse(w2.is_default)

        # Now set w2 as default -> w1 must become False
        crud.set_default_wallet(self.db, w2.id, t_id)
        self.db.refresh(w1)
        self.db.refresh(w2)
        self.assertFalse(w1.is_default)
        self.assertTrue(w2.is_default)


if __name__ == "__main__":
    unittest.main()
