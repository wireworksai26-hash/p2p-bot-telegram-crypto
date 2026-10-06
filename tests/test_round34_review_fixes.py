"""Regresi untuk temuan audit bagian 2c (gsd-code-reviewer, deep) — tiap kelas = satu temuan.

CR-01 flag wizard admin basi · CR-02 kunci wallet lewat 1-tap · CR-03 kunci rekening (format & poisoning)
WR-01 parse nominal desimal · WR-03 audit-log vs kredit · WR-05 Kas Bot atomik · WR-06 eskalasi per hash
WR-07 batas panjang pesan · WR-08 tie-break peringkat · INFO regex ID / overflow
"""
import os
import time
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

import database.models  # noqa: F401
from database import crud
from database.connection import Base, SessionLocal, engine
from database.models import AuditLog, Order, User
from services import reward_service as rs

ADMIN = 1
_seq = [0]


def order(db, tid, otype="sell", status="completed", wallet=None, total=1_000):
    _seq[0] += 1
    o = Order(order_id=f"RV-{_seq[0]}", telegram_id=tid, order_type=otype, crypto_symbol="USDT", network="BSC",
              crypto_amount=Decimal("1"), price_per_unit=1, nominal_idr=total, fee_idr=0, total_idr=total,
              status=status, buyer_wallet=wallet, created_at=datetime.utcnow())
    db.add(o)
    db.commit()
    return o


class Base_(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        for tid in (1, 2, 3, 4):
            crud.create_user(self.db, telegram_id=tid, username=f"u{tid}", full_name=f"User {tid}")

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)


class BankLockFormats(Base_):  # CR-03
    def setUp(self):
        super().setUp()
        order(self.db, 1, wallet="GOPAY | 0812.3456.7890 | Budi")
        order(self.db, 2, wallet="BCA | 1111111111 | x|5555555555|y")   # holder berisi |...| (poisoning)

    def taken(self, text, uid=3):
        return crud.is_bank_account_taken_by_other(self.db, text, uid)

    def test_format_variants_are_matched(self):
        self.assertTrue(self.taken("081234567890"))            # tersimpan bertitik, diketik polos
        self.assertTrue(self.taken("0812 3456 7890"))
        self.assertTrue(self.taken("+62 812-3456-7890"))       # +62 disamakan ke 0
        self.assertTrue(self.taken("0812.3456.7890 GOPAY Budi"))  # tanpa koma (teks bebas)

    def test_poisoning_through_pipe_in_holder_name_is_blocked(self):
        self.assertFalse(self.taken("5555555555"))             # hanya kolom nomor yang dihitung
        self.assertTrue(self.taken("1111111111"))

    def test_owner_and_junk(self):
        self.assertFalse(self.taken("081234567890", uid=1))
        self.assertFalse(self.taken("123"))
        self.assertFalse(self.taken(""))


class AmountParsing(unittest.TestCase):  # WR-01 + INFO
    def test_decimal_forms_do_not_become_100x(self):
        self.assertEqual(rs.parse_amount("75.000,00"), 75000)
        self.assertEqual(rs.parse_amount("75,000.00"), 75000)
        self.assertEqual(rs.parse_amount("50.000,50"), 50000)
        self.assertEqual(rs.parse_amount("1.250.000,00"), 1_250_000)
        self.assertEqual(rs.parse_amount("50.000"), 50000)     # tetap: ribuan
        self.assertEqual(rs.parse_amount("50,000"), 50000)

    def test_garbage_does_not_raise(self):
        for text in ("²", "９９", "9" * 400, "9" * 400 + "k", "1e5", "NaN", "inf"):
            with self.subTest(text=text[:20]):
                self.assertIsNone(rs.parse_amount(text))


class WizardFlags(Base_):  # CR-01
    def test_arm_clears_everything_else_and_expires(self):
        from bot.handlers import admin as A
        ctx = SimpleNamespace(user_data={"admin_awaiting_send_bal_user": True, "admin_awaiting_treasury_custom": True,
                                         "admin_reward_batch_id": 9})
        A._arm_reward_wizard(ctx, "admin_awaiting_milestone_excl")
        self.assertEqual({k for k in ctx.user_data if k.startswith("admin_awaiting_")}, {"admin_awaiting_milestone_excl"})
        self.assertNotIn("admin_reward_batch_id", ctx.user_data)
        A._expire_stale_reward_wizard(ctx)
        self.assertTrue(ctx.user_data["admin_awaiting_milestone_excl"])          # masih segar
        ctx.user_data["admin_wizard_ts"] = time.time() - A.WIZARD_TTL_SECONDS - 5
        A._expire_stale_reward_wizard(ctx)
        self.assertNotIn("admin_awaiting_milestone_excl", ctx.user_data)          # kedaluwarsa


class TreasuryAtomic(Base_):  # WR-05
    def test_conditional_deduct(self):
        self.assertIsNone(crud.try_deduct_bot_treasury(self.db, 1, ADMIN))        # baris belum ada = saldo 0
        crud.topup_bot_treasury(self.db, 100_000, ADMIN)
        self.assertEqual(crud.try_deduct_bot_treasury(self.db, 60_000, ADMIN), 40_000)
        self.assertIsNone(crud.try_deduct_bot_treasury(self.db, 60_000, ADMIN))   # kurang -> tidak berubah
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 40_000)
        self.assertEqual(crud.try_deduct_bot_treasury(self.db, 40_000, ADMIN), 0)  # pas = boleh
        self.assertIsNone(crud.try_deduct_bot_treasury(self.db, 1, ADMIN))

    def test_topup_is_incremental_and_audited(self):
        self.assertEqual(crud.topup_bot_treasury(self.db, 5_000, ADMIN), 5_000)    # membuat baris
        self.assertEqual(crud.topup_bot_treasury(self.db, 7_000, ADMIN), 12_000)   # menambah atomik
        actions = [a.action for a in self.db.query(AuditLog).all()]
        self.assertEqual(actions.count("TOPUP_BOT_TREASURY"), 2)


class RewardAuditVsCredit(Base_):  # WR-03
    async def test_audit_failure_after_credit_does_not_refund_or_mark_failed(self):
        crud.topup_bot_treasury(self.db, 100_000, ADMIN)
        rows, _ = rs.parse_reward_lines("2 50000")
        items, _ = rs.resolve_recipients(self.db, rows)
        batch = rs.create_batch(self.db, ADMIN, items)
        with patch.object(rs, "AuditLog", side_effect=RuntimeError("audit down")):
            result = await rs.execute_batch(self.db, AsyncMock(), batch.id, ADMIN)
        self.assertTrue(result["ok"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(crud.get_bot_treasury_balance(self.db), 50_000)           # tidak ada refund palsu
        self.db.expire_all()
        self.assertEqual(float(self.db.query(User).filter(User.telegram_id == 2).one().balance_idr), 50_000)


class EscalationPerHash(Base_):  # WR-06
    async def test_second_hash_reaches_admin_but_repeat_does_not(self):
        from services.detector import DepositDetector
        o = order(self.db, 2, otype="sell", status="WAITING_CRYPTO_DEPOSIT", wallet="BCA | 1 | x")
        det = DepositDetector()
        with patch("services.detector.notify_admins", new=AsyncMock()) as notify:
            await det.escalate_user_hash(self.db, o, "0xAAA", "r", AsyncMock())
            await det.escalate_user_hash(self.db, o, "0xAAA", "r", AsyncMock())    # sama -> diam
            await det.escalate_user_hash(self.db, o, "0xBBB", "r", AsyncMock())    # hash baru -> tetap dikirim
        self.assertEqual(notify.await_count, 2)


class MessageLength(Base_):  # WR-07
    def test_preview_and_result_stay_under_telegram_limit(self):
        from bot.handlers import admin as A
        items = [{"telegram_id": 1000 + i, "label": "N" * 300, "full_name": "N" * 300, "amount": 1_000_000,
                  "message": "m"} for i in range(30)]
        batch = rs.create_batch(self.db, ADMIN, items)
        rs.set_default_message(self.db, batch.id, ADMIN, "p" * 700)
        skipped = A._fmt_skipped([], [{"line": i, "raw": "r", "reason": "x" * 5000} for i in range(20)])
        text, _ = A.build_reward_preview_view(self.db, batch, skipped)
        self.assertLessEqual(len(text), 4096)
        self.assertIn("Total:", text)
        long_names = ["N" * 300] * 30
        result = {"batch_id": 1, "paid_count": 30, "paid_total": 1, "treasury_before": 1, "treasury_after": 0,
                  "notif_ok": [], "notif_fail": long_names, "failed": [{"label": "F" * 300}] * 30,
                  "dropped": [{"label": "D" * 300}] * 30}
        self.assertLessEqual(len(A.build_reward_result_text(result)), 4096)

    def test_history_shows_running_batches(self):
        from bot.handlers import admin as A
        batch = rs.create_batch(self.db, ADMIN, [{"telegram_id": 2, "label": "x", "amount": 1000, "message": None}])
        self.db.query(type(batch)).update({"status": "RUNNING"})
        self.db.commit()
        text, _ = A.build_reward_history_view(self.db, ADMIN)
        self.assertIn("Berjalan", text)


class RankingTieBreak(Base_):  # WR-08
    def test_equal_volume_is_ordered_by_user_id(self):
        for tid in (4, 2, 3):
            order(self.db, tid, otype="buy", total=500_000)
        self.assertEqual([t["telegram_id"] for t in crud.get_top_spenders(self.db, limit=2)], [2, 3])
        from services.campaign_service import get_top_users_by_milestone
        self.assertEqual([r["telegram_id"] for r in get_top_users_by_milestone(self.db, "VOLUME_IDR", limit=2)], [2, 3])


if __name__ == "__main__":
    unittest.main()
