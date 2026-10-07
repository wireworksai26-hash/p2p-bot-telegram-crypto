"""Kirim Reward ke user pilihan admin + pengecualian Top Milestone."""
import json
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
from database import crud
from database.connection import Base, SessionLocal, engine
from database.models import AuditLog, Order, RewardBatch, User
from services import reward_service as rs

ADMIN = 1


class ParseAmount(unittest.TestCase):
    def test_formats(self):
        for text, expected in [("50000", 50000), ("50.000", 50000), ("50,000", 50000), ("Rp 50.000", 50000),
                               ("50k", 50000), ("50rb", 50000), ("1,5jt", 1_500_000), ("2.5k", 2500),
                               ("1jt", 1_000_000), ("1 juta", 1_000_000)]:
            with self.subTest(text=text):
                self.assertEqual(rs.parse_amount(text), expected)

    def test_garbage(self):
        for text in ("", "abc", "5o000", "k", "1.2.3k", "-500"):
            with self.subTest(text=text):
                self.assertIsNone(rs.parse_amount(text))


class ParseLines(unittest.TestCase):
    def test_mixed_list(self):
        rows, errors = rs.parse_reward_lines(
            "@budi 50000\n"
            "123456789 25k\n"
            "\n"
            "@admin_chan 100.000 | Makasih ya kak, <3 {nama}!\n"
            "@x 10\n"                  # nominal terlalu kecil
            "@budi duapuluh\n"         # nominal tak terbaca
            "ngawur\n"                 # format salah
        )
        self.assertEqual([r["target"] for r in rows], ["@budi", "123456789", "@admin_chan"])
        self.assertEqual([r["amount"] for r in rows], [50000, 25000, 100000])
        self.assertEqual(rows[2]["message"], "Makasih ya kak, <3 {nama}!")
        self.assertEqual([e["line"] for e in errors], [5, 6, 7])

    def test_amount_limits(self):
        rows, errors = rs.parse_reward_lines("@aaa 999\n@bbb 10000001\n@ccc 1000\n@ddd 10000000")
        self.assertEqual([r["target"] for r in rows], ["@ccc", "@ddd"])
        self.assertEqual(len(errors), 2)


class Base_(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        crud.create_user(self.db, telegram_id=ADMIN, username="admin", full_name="Admin")
        crud.create_user(self.db, telegram_id=11, username="budi", full_name="Budi S")
        crud.create_user(self.db, telegram_id=22, username=None, full_name="Citra")
        crud.create_user(self.db, telegram_id=33, username="banned_guy", full_name="Bad")
        self.db.query(User).filter(User.telegram_id == 33).update({User.is_banned: True})
        self.db.commit()
        self.bot = AsyncMock()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def balance(self, tid):
        self.db.expire_all()
        return float(self.db.query(User).filter(User.telegram_id == tid).one().balance_idr or 0)

    def make_batch(self, text="@budi 50000\n22 25000 | halo {nama}"):
        rows, _ = rs.parse_reward_lines(text)
        items, _ = rs.resolve_recipients(self.db, rows)
        return rs.create_batch(self.db, ADMIN, items)


class Resolve(Base_):
    def test_unknown_banned_duplicate_and_cap(self):
        rows, _ = rs.parse_reward_lines("@budi 1000\n11 2000\n@ghost 3000\n@banned_guy 4000\n99999 5000")
        items, skipped = rs.resolve_recipients(self.db, rows)
        self.assertEqual([i["telegram_id"] for i in items], [11])
        reasons = " | ".join(s["reason"] for s in skipped)
        self.assertIn("duplikat", reasons)
        self.assertIn("belum terdaftar", reasons)
        self.assertIn("diblokir", reasons)
        self.assertEqual(len(skipped), 4)

    def test_label_prefers_username_then_name(self):
        rows, _ = rs.parse_reward_lines("@budi 1000\n22 1000")
        items, _ = rs.resolve_recipients(self.db, rows)
        self.assertEqual([i["label"] for i in items], ["@budi", "Citra"])


class Execute(Base_):
    async def test_pays_from_treasury_and_notifies(self):
        crud.topup_bot_treasury(self.db, 200_000, admin_id=ADMIN)
        batch = self.make_batch()
        rs.set_default_message(self.db, batch.id, ADMIN, "pesan default")
        result = await rs.execute_batch(self.db, self.bot, batch.id, ADMIN)

        self.assertTrue(result["ok"])
        self.assertEqual(self.balance(11), 50000)
        self.assertEqual(self.balance(22), 25000)
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 125_000)
        self.assertEqual((result["treasury_before"], result["treasury_after"]), (200_000, 125_000))
        self.db.expire_all()
        stored = self.db.query(RewardBatch).one()
        self.assertEqual((stored.status, stored.total_amount), ("COMPLETED", 75000))
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.action == "ADMIN_REWARD").count(), 2)

        sent = {c.kwargs["chat_id"]: c.kwargs["text"] for c in self.bot.send_message.await_args_list}
        self.assertIn("halo Citra", sent[22])            # pesan khusus + {nama}
        self.assertIn("pesan default", sent[11])         # pesan default
        self.assertIn("+Rp 50.000", sent[11])            # info saldo otomatis
        self.assertIn("Saldo sekarang", sent[11])

    async def test_insufficient_treasury_pays_nobody_and_keeps_draft(self):
        crud.topup_bot_treasury(self.db, 60_000, admin_id=ADMIN)   # butuh 75.000
        batch = self.make_batch()
        result = await rs.execute_batch(self.db, self.bot, batch.id, ADMIN)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "treasury")
        self.assertEqual(result["shortfall"], 15_000)
        self.assertEqual((self.balance(11), self.balance(22)), (0, 0))
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 60_000)
        self.bot.send_message.assert_not_awaited()
        self.db.expire_all()
        self.assertEqual(self.db.query(RewardBatch).one().status, "DRAFT")   # bisa dilanjutkan setelah top-up
        # Setelah Kas Bot diisi, batch yang sama bisa dikirim.
        crud.topup_bot_treasury(self.db, 15_000, admin_id=ADMIN)
        again = await rs.execute_batch(self.db, self.bot, batch.id, ADMIN)
        self.assertTrue(again["ok"])
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 0)

    async def test_double_execute_pays_once(self):
        crud.topup_bot_treasury(self.db, 500_000, admin_id=ADMIN)
        batch = self.make_batch()
        first = await rs.execute_batch(self.db, self.bot, batch.id, ADMIN)
        second = await rs.execute_batch(self.db, self.bot, batch.id, ADMIN)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_code"], "claimed")
        self.assertEqual(self.balance(11), 50000)                       # tidak 100000
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 425_000)

    async def test_other_admin_cannot_execute_my_batch(self):
        crud.topup_bot_treasury(self.db, 500_000, admin_id=ADMIN)
        batch = self.make_batch()
        result = await rs.execute_batch(self.db, self.bot, batch.id, admin_id=2)
        self.assertEqual(result["error_code"], "claimed")
        self.assertEqual(self.balance(11), 0)

    async def test_user_banned_after_preview_is_dropped_not_paid(self):
        crud.topup_bot_treasury(self.db, 500_000, admin_id=ADMIN)
        batch = self.make_batch()
        self.db.query(User).filter(User.telegram_id == 11).update({User.is_banned: True})
        self.db.commit()
        result = await rs.execute_batch(self.db, self.bot, batch.id, ADMIN)
        self.assertTrue(result["ok"])
        self.assertEqual(self.balance(11), 0)
        self.assertEqual(self.balance(22), 25000)
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 475_000)   # hanya yang dibayar yang dipotong
        self.assertEqual([d["telegram_id"] for d in result["dropped"]], [11])

    async def test_notification_failure_does_not_undo_reward(self):
        crud.topup_bot_treasury(self.db, 500_000, admin_id=ADMIN)
        batch = self.make_batch()
        self.bot.send_message.side_effect = RuntimeError("Forbidden: bot was blocked by the user")
        result = await rs.execute_batch(self.db, self.bot, batch.id, ADMIN)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["notif_fail"]), 2)
        self.assertEqual(self.balance(11), 50000)

    async def test_failed_credit_is_refunded_to_treasury(self):
        from unittest.mock import patch
        crud.topup_bot_treasury(self.db, 100_000, admin_id=ADMIN)
        batch = self.make_batch()
        real = crud.credit_user_balance

        def flaky(db, tid, amt):
            if tid == 22:
                raise RuntimeError("db hiccup")
            return real(db, tid, amt)

        with patch.object(crud, "credit_user_balance", side_effect=flaky):
            result = await rs.execute_batch(self.db, self.bot, batch.id, ADMIN)
        self.assertTrue(result["ok"])
        self.assertEqual([r["telegram_id"] for r in result["failed"]], [22])
        self.assertEqual(self.balance(11), 50000)
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 50_000)    # 100k - 75k + 25k refund

    def test_render_message_escapes_html_in_custom_text(self):
        text = rs.render_message("<b>hai</b> {nama} & co, dapat {nominal}", name="A<script>", amount=50000, new_balance=75000)
        self.assertIn("&lt;b&gt;hai&lt;/b&gt;", text)
        self.assertIn("A&lt;script&gt;", text)
        self.assertIn("Rp 50.000", text)


class MilestoneExclusion(Base_):
    _seq = 0

    def _completed_buy(self, tid, total, n=1):
        for _ in range(n):
            MilestoneExclusion._seq += 1
            self.db.add(Order(order_id=f"ORD-{tid}-{MilestoneExclusion._seq}", telegram_id=tid, order_type="buy", crypto_symbol="USDT",
                              network="BSC", crypto_amount=Decimal("1"), price_per_unit=1, nominal_idr=total,
                              fee_idr=0, total_idr=total, status="completed", created_at=datetime.utcnow()))
        self.db.commit()

    def _seed_top(self):
        # 12 user nyata dengan volume menurun: 1000..12000 -> pemilik volume terbesar = 100
        for n in range(12):
            tid = 100 + n
            crud.create_user(self.db, telegram_id=tid, username=f"u{tid}", full_name=f"U{tid}")
            self._completed_buy(tid, 1_000_000 - n * 10_000)

    def test_excluded_user_skipped_and_ranking_stays_top_10(self):
        self._seed_top()
        self.assertEqual(crud.get_top_spenders(self.db, limit=10)[0]["telegram_id"], 100)
        self.assertTrue(crud.add_milestone_exclusion(self.db, 100, note="admin channel airdrop", created_by=ADMIN))
        self.assertTrue(crud.add_milestone_exclusion(self.db, 101, created_by=ADMIN))
        top = crud.get_top_spenders(self.db, limit=10)
        ids = [t["telegram_id"] for t in top]
        self.assertEqual(len(top), 10)                       # tetap Top 10
        self.assertNotIn(100, ids)
        self.assertNotIn(101, ids)
        self.assertEqual(ids[0], 102)                        # yang berikutnya naik
        self.assertEqual([t["rank"] for t in top], list(range(1, 11)))
        self.assertEqual(ids[-1], 111)                       # urutan 11 & 12 ikut masuk

    def test_excluded_user_still_transacts_normally(self):
        self._seed_top()
        crud.add_milestone_exclusion(self.db, 100, created_by=ADMIN)
        user = self.db.query(User).filter(User.telegram_id == 100).one()
        self.assertFalse(user.is_banned)                      # bukan diblokir
        self._completed_buy(100, 5_000)                       # order baru tetap sah
        self.assertEqual(self.db.query(Order).filter(Order.telegram_id == 100, Order.status == "completed").count(), 2)

    def test_only_users_with_completed_transaction_qualify(self):
        self._seed_top()
        crud.create_user(self.db, telegram_id=500, username="nopurchase", full_name="N")
        self.db.add(Order(order_id="ORD-PEND", telegram_id=500, order_type="buy", crypto_symbol="USDT", network="BSC",
                          crypto_amount=Decimal("1"), price_per_unit=1, nominal_idr=9_999_999, fee_idr=0,
                          total_idr=9_999_999, status="pending", created_at=datetime.utcnow()))
        self.db.commit()
        ids = [t["telegram_id"] for t in crud.get_top_spenders(self.db, limit=20)]
        self.assertNotIn(500, ids)                           # pending / belum transaksi tidak masuk
        self.assertEqual(len(ids), 12)

    def test_milestone_campaign_ranking_also_skips_excluded(self):
        from services.campaign_service import get_top_users_by_milestone
        self._seed_top()
        crud.add_milestone_exclusion(self.db, 100, created_by=ADMIN)
        ids = [r["telegram_id"] for r in get_top_users_by_milestone(self.db, "VOLUME_IDR", limit=10)]
        self.assertNotIn(100, ids)
        self.assertEqual(len(ids), 10)

    def test_remove_restores_ranking_and_audit(self):
        self._seed_top()
        crud.add_milestone_exclusion(self.db, 100, created_by=ADMIN)
        self.assertFalse(crud.add_milestone_exclusion(self.db, 100, note="update", created_by=ADMIN))  # sudah ada
        self.assertTrue(crud.remove_milestone_exclusion(self.db, 100, removed_by=ADMIN))
        self.assertFalse(crud.remove_milestone_exclusion(self.db, 100, removed_by=ADMIN))
        self.assertEqual(crud.get_top_spenders(self.db, limit=10)[0]["telegram_id"], 100)
        actions = [a.action for a in self.db.query(AuditLog).all()]
        self.assertIn("MILESTONE_EXCLUDE_ADD", actions)
        self.assertIn("MILESTONE_EXCLUDE_REMOVE", actions)

    async def test_top_spender_payout_skips_excluded(self):
        from services.campaign_service import execute_top_spender_campaign
        self._seed_top()
        crud.topup_bot_treasury(self.db, 5_000_000, admin_id=ADMIN)
        crud.add_milestone_exclusion(self.db, 100, created_by=ADMIN)
        token = crud.issue_admin_action_token(self.db, ADMIN, "top_spender", "30")
        result = await execute_top_spender_campaign(
            self.db, self.bot, ADMIN, period_days=30, action_token=token,
        )
        paid = [w["telegram_id"] for w in result["winners"]]
        self.assertNotIn(100, paid)
        self.assertEqual(len(paid), 10)
        self.assertEqual(self.balance(100), 0)
        self.assertEqual(self.balance(101), 150_000)          # rank 1 sekarang user 101 -> Rp 150.000
        self.assertEqual(self.balance(102), 100_000)          # rank 2

    async def test_top_spender_double_tap_pays_once(self):
        from services.campaign_service import execute_top_spender_campaign
        self._seed_top()
        crud.topup_bot_treasury(self.db, 5_000_000, admin_id=ADMIN)
        token = crud.issue_admin_action_token(self.db, ADMIN, "top_spender", "30")
        first = await execute_top_spender_campaign(
            self.db, self.bot, ADMIN, period_days=30, action_token=token,
        )
        second = await execute_top_spender_campaign(self.db, self.bot, ADMIN, period_days=30)
        self.assertIsNone(first["error"])
        self.assertIn("sudah dibagikan", second["error"])
        self.assertEqual(self.balance(100), 150_000)          # bukan 300.000


if __name__ == "__main__":
    unittest.main()
