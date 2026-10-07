"""Ronde 4 (keputusan client):
- semua hadiah Milestone dibayar dari Kas Bot; kurang -> admin harus mengisi dulu
- volume Top Spender = akumulasi beli + jual + convert
- rekening pencairan terkunci seperti alamat wallet (setelah penjualan sukses)
- wallet/rekening otomatis tersimpan setelah transaksi sukses
- undian acak hanya untuk user yang sudah /start (tidak ada user "hantu")
"""
import os
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

import database.models  # noqa: F401
from database import crud
from database.connection import Base, SessionLocal, engine
from database.models import Campaign, Order, User, UserSavedBank, UserSavedWallet
from services import campaign_service as cs

ADMIN = 1
_seq = [0]


def make_order(db, tid, order_type, total, status="completed", wallet=None, network="BSC", **extra):
    _seq[0] += 1
    order = Order(
        order_id=f"ORD-{_seq[0]}", telegram_id=tid, order_type=order_type, crypto_symbol="USDT",
        network=network, crypto_amount=Decimal("1"), price_per_unit=1, nominal_idr=total, fee_idr=0,
        total_idr=total, status=status, buyer_wallet=wallet, created_at=datetime.utcnow(), **extra)
    db.add(order)
    db.commit()
    return order


class DB(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        for tid in (1, 11, 12, 13, 14):
            crud.create_user(self.db, telegram_id=tid, username=f"u{tid}", full_name=f"U{tid}")
        self.bot = AsyncMock()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def balance(self, tid):
        self.db.expire_all()
        return float(self.db.query(User).filter(User.telegram_id == tid).one().balance_idr or 0)


class TopSpenderVolume(DB):
    def test_volume_accumulates_buy_sell_and_convert(self):
        make_order(self.db, 11, "buy", 100_000)
        make_order(self.db, 11, "sell", 300_000)
        make_order(self.db, 11, "swap", 200_000)
        make_order(self.db, 12, "buy", 500_000)
        make_order(self.db, 12, "sell", 90_000_000, status="pending")      # belum selesai: tidak dihitung
        make_order(self.db, 13, "sell", 50_000, status="cancelled")
        top = crud.get_top_spenders(self.db, limit=10, period_days=30)
        self.assertEqual([t["telegram_id"] for t in top], [11, 12])
        self.assertEqual(top[0]["total_spent_idr"], 600_000)               # 100k + 300k + 200k
        self.assertEqual(top[0]["tx_count"], 3)
        self.assertEqual(top[1]["total_spent_idr"], 500_000)

    def test_sell_only_user_can_rank(self):
        make_order(self.db, 14, "sell", 1_000_000)
        self.assertEqual([t["telegram_id"] for t in crud.get_top_spenders(self.db)], [14])

    def test_milestone_campaign_ranking_uses_same_volume(self):
        make_order(self.db, 11, "sell", 400_000)
        make_order(self.db, 12, "buy", 300_000)
        rows = cs.get_top_users_by_milestone(self.db, "VOLUME_IDR", limit=10)
        self.assertEqual([r["telegram_id"] for r in rows], [11, 12])


class TopSpenderFromTreasury(DB):
    def _seed(self, n=3):
        for i in range(n):
            make_order(self.db, 11 + i, "buy", 1_000_000 - i * 100_000)

    def _token(self, period=30):
        return crud.issue_admin_action_token(self.db, ADMIN, "top_spender", str(period))

    async def test_short_treasury_blocks_payout_and_changes_nothing(self):
        self._seed()
        crud.topup_bot_treasury(self.db, 200_000, admin_id=ADMIN)         # butuh 300.000
        result = await cs.execute_top_spender_campaign(
            self.db, self.bot, ADMIN, period_days=30, action_token=self._token(),
        )
        self.assertIn("Kas Bot kurang Rp 100.000", result["error"])
        self.assertEqual(result["treasury_shortfall"], 100_000)
        self.assertEqual([self.balance(t) for t in (11, 12, 13)], [0, 0, 0])
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 200_000)
        self.assertEqual(self.db.query(Campaign).count(), 0)              # tidak ada campaign setengah jadi
        self.bot.send_message.assert_not_awaited()

    async def test_payout_deducts_exactly_the_rewards(self):
        self._seed()
        crud.topup_bot_treasury(self.db, 1_000_000, admin_id=ADMIN)
        result = await cs.execute_top_spender_campaign(
            self.db, self.bot, ADMIN, period_days=30, action_token=self._token(),
        )
        self.assertIsNone(result["error"])
        self.assertEqual(result["total_amount"], 300_000)                 # 150k + 100k + 50k
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 700_000)
        self.assertEqual([self.balance(t) for t in (11, 12, 13)], [150_000, 100_000, 50_000])

    async def test_failure_midway_refunds_treasury(self):
        self._seed()
        crud.topup_bot_treasury(self.db, 1_000_000, admin_id=ADMIN)
        with patch.object(database.models, "AuditLog", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                await cs.execute_top_spender_campaign(
                    self.db, self.bot, ADMIN, period_days=30, action_token=self._token(),
                )
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 1_000_000)   # utuh kembali
        self.assertEqual([self.balance(t) for t in (11, 12, 13)], [0, 0, 0])

    async def test_funded_top_spender_honours_exclusion(self):
        self._seed()
        crud.add_milestone_exclusion(self.db, 11, created_by=ADMIN)
        crud.topup_bot_treasury(self.db, 1_000_000, admin_id=ADMIN)
        result = await cs.execute_top_spender_campaign(
            self.db, self.bot, ADMIN, period_days=30, action_token=self._token(),
        )
        self.assertEqual([w["telegram_id"] for w in result["winners"]], [12, 13])
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 1_000_000 - 250_000)   # 150k + 100k


class MilestoneCampaignFromTreasury(DB):
    def _draft(self, mode="MILESTONE", pool=300_000, winners=2):
        camp = Campaign(campaign_code=f"T{mode}{pool}{datetime.utcnow().timestamp()}", title="Uji",
                        template_type="tpl_top_spenders", mode=mode, target_segment="buyers",
                        total_pool=pool, max_winners=winners, milestone_metric="VOLUME_IDR",
                        status="DRAFT", created_by=ADMIN)
        self.db.add(camp)
        self.db.commit()
        return camp

    def test_milestone_campaign_requires_funded_treasury(self):
        make_order(self.db, 11, "buy", 500_000)
        make_order(self.db, 12, "buy", 200_000)
        camp = self._draft()
        crud.topup_bot_treasury(self.db, 100_000, admin_id=ADMIN)
        with self.assertRaises(cs.TreasuryInsufficient) as ctx:
            cs.execute_campaign(self.db, camp.id, ADMIN)
        self.assertEqual((ctx.exception.needed, ctx.exception.shortfall), (300_000, 200_000))
        self.db.expire_all()
        self.assertEqual(self.db.query(Campaign).one().status, "DRAFT")   # bisa dilanjutkan setelah top-up
        self.assertEqual((self.balance(11), self.balance(12)), (0, 0))
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 100_000)

        crud.topup_bot_treasury(self.db, 200_000, admin_id=ADMIN)         # admin mengisi Kas Bot
        result = cs.execute_campaign(self.db, camp.id, ADMIN)
        self.assertEqual(result["distributed_amount"], 300_000)
        self.assertEqual((self.balance(11), self.balance(12)), (150_000, 150_000))
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 0)

    def test_non_milestone_campaign_unchanged(self):
        # Sesuai instruksi client: yang diubah hanya hadiah Milestone — bagi rata/undian tak berubah.
        make_order(self.db, 11, "buy", 500_000)
        make_order(self.db, 12, "buy", 200_000)
        camp = self._draft(mode="EQUAL_SPLIT", pool=100_000, winners=None)
        result = cs.execute_campaign(self.db, camp.id, ADMIN)
        self.assertEqual(result["distributed_count"], 2)
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 0)


class BankAccountLock(DB):
    def test_locked_only_after_completed_sell(self):
        make_order(self.db, 11, "sell", 100_000, wallet="BCA | 1234567890 | Budi")
        self.assertTrue(crud.is_bank_account_taken_by_other(self.db, "1234567890", 12))
        self.assertTrue(crud.is_bank_account_taken_by_other(self.db, "1234 5678-90", 12))   # format bebas
        self.assertFalse(crud.is_bank_account_taken_by_other(self.db, "1234567890", 11))    # pemilik sendiri
        self.assertFalse(crud.is_bank_account_taken_by_other(self.db, "9999999999", 12))

    def test_unfinished_or_non_sell_orders_do_not_lock(self):
        make_order(self.db, 11, "sell", 1, status="pending", wallet="BCA | 5550001111 | A")
        make_order(self.db, 11, "sell", 1, status="cancelled", wallet="BCA | 5550002222 | A")
        make_order(self.db, 11, "buy", 1, wallet="BCA | 5550003333 | A")   # bukan penjualan
        for acc in ("5550001111", "5550002222", "5550003333"):
            self.assertFalse(crud.is_bank_account_taken_by_other(self.db, acc, 12), acc)

    def test_saving_in_profile_alone_does_not_lock(self):
        crud.save_user_bank(self.db, 11, "BCA", "7770001111", "Budi")
        self.assertFalse(crud.is_bank_account_taken_by_other(self.db, "7770001111", 12))

    def test_ewallet_number_lock(self):
        make_order(self.db, 11, "sell", 1, wallet="GOPAY | 081234567890 | Budi")
        self.assertTrue(crud.is_bank_account_taken_by_other(self.db, "0812-3456-7890", 12))


class AutoSaveAfterSuccess(DB):
    def test_buy_saves_wallet(self):
        order = make_order(self.db, 11, "buy", 1, status="paid", wallet="0x" + "ab" * 20, network="BSC")
        self.assertEqual(crud.get_user_saved_wallets(self.db, 11), [])
        crud.update_order_status(self.db, order.order_id, "completed")
        saved = crud.get_user_saved_wallets(self.db, 11)
        self.assertEqual([(w.wallet_address, w.network) for w in saved], [("0x" + "ab" * 20, "BSC")])

    def test_convert_saves_target_wallet_with_target_network(self):
        order = make_order(self.db, 11, "swap", 1, status="PAYOUT_QUEUED", wallet="0x" + "cd" * 20,
                           target_network="BASE", target_crypto_symbol="ETH")
        crud.update_order_status(self.db, order.order_id, "COMPLETED")
        saved = crud.get_user_saved_wallets(self.db, 11)
        self.assertEqual([(w.wallet_address, w.network) for w in saved], [("0x" + "cd" * 20, "BASE")])

    def test_sell_saves_bank_account(self):
        order = make_order(self.db, 11, "sell", 1, status="CRYPTO_CONFIRMED", wallet="BCA | 1234567890 | Budi Santoso")
        crud.update_order_status(self.db, order.order_id, "completed")
        banks = self.db.query(UserSavedBank).filter(UserSavedBank.telegram_id == 11).all()
        self.assertEqual([(b.bank_name, b.account_number, b.account_name) for b in banks],
                         [("BCA", "1234567890", "BUDI SANTOSO")])

    def test_idempotent_and_never_breaks_completion(self):
        order = make_order(self.db, 11, "buy", 1, status="paid", wallet="0x" + "ef" * 20)
        crud.update_order_status(self.db, order.order_id, "completed")
        crud.auto_save_order_accounts(self.db, order)                       # panggil ulang
        self.assertEqual(len(crud.get_user_saved_wallets(self.db, 11)), 1)

        order2 = make_order(self.db, 12, "buy", 1, status="paid", wallet="0x" + "12" * 20)
        with patch.object(crud, "save_user_wallet", side_effect=RuntimeError("db hiccup")):
            result = crud.update_order_status(self.db, order2.order_id, "completed")
        self.assertEqual(result.status, "completed")                        # order tetap selesai


class RandomPoolOnlyStartedUsers(DB):
    def test_pool_comes_only_from_registered_users_and_never_creates_ghosts(self):
        before = self.db.query(User).count()
        winners = crud.get_random_winners(self.db, "ALL", 50)
        self.assertLessEqual(len(winners), before)
        self.assertEqual({w["telegram_id"] for w in winners} - {1, 11, 12, 13, 14}, set())
        self.assertEqual(self.db.query(User).count(), before)

    def test_admin_reward_refuses_unregistered_user(self):
        from services import reward_service as rs
        rows, _ = rs.parse_reward_lines("987654321 5000")
        items, skipped = rs.resolve_recipients(self.db, rows)
        self.assertEqual(items, [])
        self.assertIn("belum terdaftar", skipped[0]["reason"])
        self.assertEqual(self.db.query(User).filter(User.telegram_id == 987654321).count(), 0)


if __name__ == "__main__":
    unittest.main()
