"""Hardening pra-deploy (nomor 2-7): pengirim deposit, refund Saldo Bot, hadiah/withdraw ganda,
topup atomik, anti cairkan QRIS & akun kosong, tombol Cek Ulang yang jujur."""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1,2")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from database import crud  # noqa: E402
from config.settings import settings  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import (  # noqa: E402
    AuditLog, Campaign, Order, TopupOrder, User, WithdrawRequest,
)
from bot.handlers import admin  # noqa: E402
from services.detector import DepositDetector  # noqa: E402

HOT = "0x" + "1" * 40
USER = 5005


def _order(order_id, status="pending", order_type="buy", method="BOT_BALANCE", paid=True, **kw):
    data = dict(order_id=order_id, telegram_id=USER, order_type=order_type, crypto_symbol="USDT",
                network="BSC", crypto_amount=Decimal("10"), price_per_unit=17000, nominal_idr=170000,
                fee_idr=3000, total_idr=173000, buyer_wallet="0x" + "c" * 40, payment_method=method,
                status=status, paid_at=datetime.utcnow() if paid else None, created_at=datetime.utcnow())
    data.update(kw)
    return Order(**data)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.pin = patch.object(settings, "ADMIN_CHAT_IDS", [1, 2])
        self.pin.start()
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, balance_idr=Decimal("0")))
        self.db.commit()

    def tearDown(self):
        self.pin.stop()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def balance(self):
        self.db.expire_all()
        return self.db.query(User).filter_by(telegram_id=USER).one().balance_idr

    async def press(self, handler, data, uid=1):
        query = MagicMock()
        query.data = data
        query.from_user = SimpleNamespace(id=uid)
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.message = SimpleNamespace(caption="x", text="x", text_html="x", reply_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(bot=AsyncMock(), application=AsyncMock())
        with patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()), \
             patch("bot.handlers.withdraw.safe_send_message", new=AsyncMock()):
            await handler(update, context)
        return query


# ───────────── 2. Pengirim deposit ─────────────
class SenderGuard(_Base):
    def _reason(self, verified):
        order = _order("ORD-S", "WAITING_CRYPTO_DEPOSIT", "swap", None, False,
                       crypto_amount=Decimal("10"), deposit_wallet=HOT)
        self.db.add(order)
        self.db.commit()
        return DepositDetector().user_hash_review_reason(self.db, order, verified)

    def test_pengirim_tidak_terbaca_dicek_admin(self):
        reason = self._reason({"verified": True, "amount": 10.0, "from_address": ""})
        self.assertIn("Pengirim deposit tidak dapat dibaca", reason)

    def test_pengirim_terbaca_lolos(self):
        self.assertEqual(self._reason({"verified": True, "amount": 10.0, "from_address": "0x" + "a" * 40}), "")

    def test_data_lama_tanpa_field_tetap_kompatibel(self):
        self.assertEqual(self._reason({"verified": True, "amount": 10.0}), "")

    def test_pengirim_wallet_owner_ditolak_otomatis(self):
        owner = "0x" + "b" * 40
        with patch.object(settings, "OWNER_WALLET_ADDRESSES", (owner,)):
            reason = self._reason({"verified": True, "amount": 10.0, "from_address": owner.upper().replace("0X", "0x")})
        self.assertIn("wallet owner", reason)


class SenderExtraction(unittest.IsolatedAsyncioTestCase):
    async def test_tron_trx_mengembalikan_pengirim(self):
        from services import tx_verifier
        wallet = "T" + "W" * 33
        sender = "T" + "S" * 33
        info = {"id": "h", "blockTimeStamp": 1_700_000_000_000, "blockNumber": 1, "receipt": {"result": "SUCCESS"}}
        tx = {"ret": [{"contractRet": "SUCCESS"}], "raw_data": {"contract": [{
            "type": "TransferContract",
            "parameter": {"value": {"to_address": "to", "owner_address": "from", "amount": 5_000_000}}}]}}

        async def fake_tron(path, tx_hash):
            return info if path == "gettransactioninfobyid" else tx

        def fake_addr(value):
            return {"to": wallet, "from": sender}.get(value, value)

        with patch.object(tx_verifier, "_tron", side_effect=fake_tron), \
             patch.object(tx_verifier, "_tron_address", side_effect=fake_addr):
            res = await tx_verifier._verify_tron("TRX", "h", wallet)
        self.assertTrue(res["verified"])
        self.assertEqual(res["from_address"], sender)

    async def test_solana_mengembalikan_penanda_tangan_pertama(self):
        from services import tx_verifier
        wallet, sender = "W" * 44, "S" * 44
        result = {
            "blockTime": 1_700_000_000,
            "meta": {"err": None, "preBalances": [10, 0], "postBalances": [5, 5]},
            "transaction": {"message": {
                "accountKeys": [{"pubkey": sender}, {"pubkey": wallet}],
                "instructions": [{"program": "system", "parsed": {"type": "transfer", "info": {
                    "source": sender, "destination": wallet, "lamports": 5}}}]}},
        }
        with patch.object(tx_verifier, "_sol_rpc", new=AsyncMock(return_value=result)):
            res = await tx_verifier._verify_solana("SOL", "sig", wallet)
        self.assertTrue(res["verified"])
        self.assertEqual(res["from_address"], sender)


# ───────────── 3. Refund Saldo Bot & pemulihan ─────────────
class BalanceOrderRefund(_Base):
    async def test_stok_habis_manual_review_bisa_ditolak_dan_direfund(self):
        self.db.add(_order("ORD-STOK", "manual_review", failure_reason="Stok USDT (BSC) tidak mencukupi."))
        self.db.commit()
        q = await self.press(admin.admin_reject_buy_callback, "admin_reject_buy_ORD-STOK")
        # Langkah pertama hanya meminta konfirmasi: saldo belum berubah.
        self.assertEqual(self.balance(), 0)
        q.message.reply_text.assert_awaited_once()
        await self.press(admin.admin_reject_buy_callback, "admin_reject_buy_yes_ORD-STOK")
        self.assertEqual(self.balance(), Decimal("173000"))
        self.db.expire_all()
        self.assertEqual(self.db.query(Order).filter_by(order_id="ORD-STOK").one().status, "rejected")

    async def test_refund_tidak_dobel(self):
        self.db.add(_order("ORD-2X", "manual_review"))
        self.db.commit()
        for _ in range(2):
            await self.press(admin.admin_reject_buy_callback, "admin_reject_buy_yes_ORD-2X")
        self.assertEqual(self.balance(), Decimal("173000"))

    async def test_payout_terputus_tidak_direfund_otomatis(self):
        self.db.add(_order("ORD-CUT", "manual_review",
                           failure_reason="Payout terputus (bot restart) — cek on-chain sebelum kirim ulang"))
        self.db.commit()
        await self.press(admin.admin_reject_buy_callback, "admin_reject_buy_yes_ORD-CUT")
        self.assertEqual(self.balance(), 0)

    async def test_ada_hash_broadcast_tidak_direfund(self):
        self.db.add(_order("ORD-HASH", "manual_review", payout_tx_hash="0x" + "9" * 64))
        self.db.commit()
        await self.press(admin.admin_reject_buy_callback, "admin_reject_buy_yes_ORD-HASH")
        self.assertEqual(self.balance(), 0)

    async def test_alur_pending_lama_tetap_langsung_refund(self):
        self.db.add(_order("ORD-PEND", "pending"))
        self.db.commit()
        await self.press(admin.admin_reject_buy_callback, "admin_reject_buy_ORD-PEND")
        self.assertEqual(self.balance(), Decimal("173000"))

    def test_order_saldo_bot_terputus_ikut_dipulihkan_job(self):
        self.db.add(_order("ORD-PROC", "payout_processing"))
        self.db.add(_order("ORD-DONE", "completed", payout_tx_hash="0xabc"))
        self.db.commit()
        ids = {o.order_id for o in crud.get_gopay_resume_orders(self.db)}
        self.assertIn("ORD-PROC", ids)
        self.assertNotIn("ORD-DONE", ids)


# ───────────── 4. Hadiah & withdraw ganda ─────────────
class TopSpenderOncePerPeriod(_Base):
    async def test_periode_sama_tidak_dibayar_dua_kali(self):
        from services import campaign_service as cs
        self.db.add(Campaign(
            campaign_code="TOP_SPENDER_OLD", title="🏆 Top Spender Milestone (30D)",
            template_type="tpl_top_spenders", mode="MILESTONE", target_segment="BUYERS", total_pool=1,
            max_winners=1, status="COMPLETED", created_by=1,
            created_at=datetime.utcnow() - timedelta(days=3)))
        self.db.commit()
        with patch.object(crud, "get_top_spenders",
                          return_value=[{"rank": 1, "telegram_id": USER, "username": "u", "total_spent_idr": 1}]):
            res = await cs.execute_top_spender_campaign(self.db, None, 1, period_days=30, action_token="t")
        self.assertEqual(res["distributed_count"], 0)
        self.assertIn("sudah dibayar", res["error"])

    async def test_periode_berbeda_tidak_terblokir_guard_ini(self):
        from services import campaign_service as cs
        self.db.add(Campaign(
            campaign_code="TOP_SPENDER_OLD7", title="🏆 Top Spender Milestone (7D)",
            template_type="tpl_top_spenders", mode="MILESTONE", target_segment="BUYERS", total_pool=1,
            max_winners=1, status="COMPLETED", created_by=1,
            created_at=datetime.utcnow() - timedelta(days=3)))
        self.db.commit()
        with patch.object(crud, "get_top_spenders",
                          return_value=[{"rank": 1, "telegram_id": USER, "username": "u", "total_spent_idr": 1}]):
            res = await cs.execute_top_spender_campaign(self.db, None, 1, period_days=30, action_token="t")
        # Lolos guard periode; berhenti di langkah berikutnya (Kas Bot kosong), bukan "sudah dibayar".
        self.assertNotIn("sudah dibayar", res.get("error") or "")


class AdminTaskClaim(_Base):
    def test_admin_kedua_diblokir(self):
        self.assertEqual(crud.claim_admin_task(self.db, "withdraw:1", 1), (True, 1))
        self.assertEqual(crud.claim_admin_task(self.db, "withdraw:1", 1), (True, 1))
        self.assertEqual(crud.claim_admin_task(self.db, "withdraw:1", 2), (False, 1))

    def test_pemegang_hilang_30_menit_bisa_diambil_alih(self):
        from database.models import AdminTaskClaim as Claim
        crud.claim_admin_task(self.db, "withdraw:9", 1)
        self.db.query(Claim).update({Claim.claimed_at: datetime.utcnow() - timedelta(minutes=31)})
        self.db.commit()
        self.assertEqual(crud.claim_admin_task(self.db, "withdraw:9", 2), (True, 2))

    async def test_withdraw_admin_lain_tidak_bisa_tolak_setelah_dipegang(self):
        from bot.handlers.withdraw import admin_withdraw_callback
        self.db.add(WithdrawRequest(telegram_id=USER, amount_idr=50000, bank_name="BCA",
                                    account_number="1", account_name="X", status="PENDING"))
        self.db.commit()
        rid = self.db.query(WithdrawRequest).one().id
        with patch("bot.handlers.admin.is_admin", return_value=True):
            await self.press(admin_withdraw_callback, f"admin_wd_take_{rid}", uid=1)
            q = await self.press(admin_withdraw_callback, f"admin_wd_no_{rid}", uid=2)
            self.db.expire_all()
            self.assertEqual(self.db.query(WithdrawRequest).one().status, "PENDING")
            self.assertIn("sedang ditangani", q.answer.await_args.args[0])
            await self.press(admin_withdraw_callback, f"admin_wd_ok_{rid}", uid=1)
        self.db.expire_all()
        self.assertEqual(self.db.query(WithdrawRequest).one().status, "PAID")


# ───────────── 5. Topup atomik ─────────────
class TopupAtomic(_Base):
    def _topup(self, topup_id="TOPUP-1", status="PENDING", amount=100_000, mdr=300):
        self.db.add(TopupOrder(topup_id=topup_id, telegram_id=USER, amount_idr=amount, mdr_idr=mdr,
                               status=status, created_at=datetime.utcnow(),
                               expires_at=datetime.utcnow() + timedelta(minutes=10)))
        self.db.commit()

    def test_claim_dan_kredit_sekali(self):
        self._topup()
        first = crud.claim_and_credit_topup(self.db, "TOPUP-1")
        second = crud.claim_and_credit_topup(self.db, "TOPUP-1")
        self.assertEqual(first[:2], (False, 99_700))
        self.assertEqual(float(first[2]), 99_700.0)
        self.assertIsNone(second)
        self.assertEqual(self.balance(), Decimal("99700"))
        self.assertEqual(self.db.query(AuditLog).filter_by(action="TOPUP_CREDITED").count(), 1)

    def test_gagal_di_tengah_tidak_meninggalkan_status_lunas_tanpa_saldo(self):
        self._topup("TOPUP-2")
        real_add = self.db.add

        def boom(obj, *a, **k):
            if isinstance(obj, AuditLog):
                raise RuntimeError("koneksi putus")
            return real_add(obj, *a, **k)

        with patch.object(self.db, "add", side_effect=boom):
            with self.assertRaises(RuntimeError):
                crud.claim_and_credit_topup(self.db, "TOPUP-2")
        self.db.expire_all()
        self.assertEqual(self.db.query(TopupOrder).filter_by(topup_id="TOPUP-2").one().status, "PENDING")
        self.assertEqual(self.balance(), 0)
        # Percobaan ulang (pemindai berikutnya) berhasil penuh.
        self.assertIsNotNone(crud.claim_and_credit_topup(self.db, "TOPUP-2"))
        self.assertEqual(self.balance(), Decimal("99700"))

    def test_topup_expired_hanya_lewat_jalur_admin(self):
        self._topup("TOPUP-EXP", status="EXPIRED")
        self.assertIsNone(crud.claim_and_credit_topup(self.db, "TOPUP-EXP"))
        self.assertIsNotNone(crud.claim_and_credit_topup(self.db, "TOPUP-EXP", allow_expired=True))

    def test_topup_kas_bot_masuk_kas_bukan_saldo_user(self):
        self._topup("TOPUP-TREASURY-1", amount=50_000, mdr=0)
        before = crud.get_bot_treasury_balance(self.db)
        res = crud.claim_and_credit_topup(self.db, "TOPUP-TREASURY-1")
        self.assertTrue(res[0])
        self.assertEqual(crud.get_bot_treasury_balance(self.db) - before, 50_000)
        self.assertEqual(self.balance(), 0)

    def test_topup_tidak_ada(self):
        self.assertIsNone(crud.claim_and_credit_topup(self.db, "NOPE"))


# ───────────── 6. Anti cairkan QRIS & akun kosong ─────────────
class TopupWithdrawLock(_Base):
    def _fund(self, topup=100_000, extra=0):
        self.db.add(TopupOrder(topup_id="TOPUP-W", telegram_id=USER, amount_idr=topup, mdr_idr=0,
                               status="SUCCESS", created_at=datetime.utcnow(), paid_at=datetime.utcnow()))
        self.db.query(User).filter_by(telegram_id=USER).update({User.balance_idr: topup + extra})
        self.db.commit()

    def test_saldo_topup_murni_tidak_bisa_ditarik(self):
        self._fund()
        self.assertEqual(crud.get_withdrawable_balance(self.db, USER), 0)
        bank = SimpleNamespace(bank_name="BCA", account_number="1", account_name="X")
        self.assertIsNone(crud.create_withdraw_request(self.db, USER, bank, 50_000))
        self.assertEqual(self.balance(), Decimal("100000"))

    def test_reward_di_atas_topup_boleh_ditarik(self):
        self._fund(extra=30_000)
        self.assertEqual(crud.get_withdrawable_balance(self.db, USER), 30_000)
        bank = SimpleNamespace(bank_name="BCA", account_number="1", account_name="X")
        self.assertIsNotNone(crud.create_withdraw_request(self.db, USER, bank, 20_000))
        # Sisa reward 10k < minimum, dan 100k topup tetap terkunci (tidak "terbuka" karena withdraw reward).
        self.assertEqual(crud.get_withdrawable_balance(self.db, USER), 10_000)
        self.assertIsNone(crud.create_withdraw_request(self.db, USER, bank, 15_000))

    def test_topup_yang_dibelanjakan_tidak_lagi_terkunci(self):
        self._fund()
        self.db.add(_order("ORD-SPENT", "completed", total_idr=100_000))
        self.db.query(User).filter_by(telegram_id=USER).update({User.balance_idr: 40_000})
        self.db.commit()
        self.assertEqual(crud.topup_locked_amount(self.db, USER), 0)
        self.assertEqual(crud.get_withdrawable_balance(self.db, USER), 40_000)

    def test_order_ditolak_dan_direfund_tidak_dihitung_belanja(self):
        self._fund()
        self.db.add(_order("ORD-REF", "rejected", total_idr=100_000))
        self.db.commit()
        self.assertEqual(crud.topup_locked_amount(self.db, USER), 100_000)

    def test_user_tanpa_topup_tidak_terpengaruh(self):
        self.db.query(User).filter_by(telegram_id=USER).update({User.balance_idr: 75_000})
        self.db.commit()
        self.assertEqual(crud.get_withdrawable_balance(self.db, USER), 75_000)

    def test_topup_sebelum_tanggal_aturan_tidak_dikunci(self):
        self.db.add(TopupOrder(topup_id="TOPUP-OLD", telegram_id=USER, amount_idr=100_000, mdr_idr=0,
                               status="SUCCESS", created_at=datetime(2026, 1, 1), paid_at=datetime(2026, 1, 1)))
        self.db.query(User).filter_by(telegram_id=USER).update({User.balance_idr: 100_000})
        self.db.commit()
        self.assertEqual(crud.get_withdrawable_balance(self.db, USER), 100_000)


# ───────────── 7. Tombol Cek Ulang yang jujur ─────────────
class HonestRecheck(_Base):
    def _swap(self):
        self.db.add(_order("SWAP-1", "WAITING_CRYPTO_DEPOSIT", "swap", None, False,
                           target_crypto_symbol="SOL", target_network="SOLANA",
                           target_crypto_amount=Decimal("0.5")))
        self.db.commit()

    async def test_tombol_cek_ulang_memakai_pengecekan_standar(self):
        self._swap()
        with patch("bot.handlers.admin.is_admin", return_value=True), \
             patch("services.detector.deposit_detector._process_order", new=AsyncMock()) as proc:
            await self.press(admin.admin_recheck_swap_callback, "admin_recheck_swap_SWAP-1")
        self.assertEqual(proc.await_args.kwargs.get("trusted"), False)

    async def test_setuju_paksa_butuh_konfirmasi_dan_belum_mengirim(self):
        self._swap()
        with patch("bot.handlers.admin.is_admin", return_value=True), \
             patch("services.detector.deposit_detector._process_order", new=AsyncMock()) as proc:
            q = await self.press(admin.admin_approve_swap_callback, "admin_approve_swap_SWAP-1")
        proc.assert_not_awaited()
        prompt = q.message.reply_text.await_args.args[0]
        self.assertIn("bukan milik pihak ketiga", prompt)

    async def test_konfirmasi_ya_baru_mengeksekusi_jalur_terpercaya(self):
        self._swap()
        with patch("bot.handlers.admin.is_admin", return_value=True), \
             patch("services.detector.deposit_detector._process_order", new=AsyncMock()) as proc:
            await self.press(admin.admin_approve_swap_callback, "admin_approve_swap_yes_SWAP-1")
        self.assertEqual(proc.await_args.kwargs.get("trusted"), True)

    async def test_batal_tidak_mengirim(self):
        self._swap()
        with patch("bot.handlers.admin.is_admin", return_value=True), \
             patch("services.detector.deposit_detector._process_order", new=AsyncMock()) as proc:
            await self.press(admin.admin_approve_swap_callback, "admin_approve_swap_no_SWAP-1")
        proc.assert_not_awaited()

    def test_label_tombol_di_notifikasi_menuju_jalur_aman(self):
        import inspect
        from bot.handlers import swap
        src = inspect.getsource(swap._notify_admin_deposit_pending)
        self.assertIn("admin_recheck_swap_", src)
        self.assertNotIn('"Cek Ulang Deposit", callback_data=f"admin_approve_swap_', src)


if __name__ == "__main__":
    unittest.main()
