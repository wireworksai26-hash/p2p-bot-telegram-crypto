"""Laporan client 10 Okt: deposit Convert dengan nominal tidak sesuai.

- Nominal kurang tidak memicu alert sama sekali (hanya log), nominal lebih memicu alert yang
  hanya menyebut "USDC (BASE)" tanpa koin tujuan dan tanpa username.
- Antrean Order menunjukkan 0 karena order sudah expired, dan user melihat status "Expired"
  padahal koinnya sudah masuk.
"""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from config.settings import settings  # noqa: E402
from database import crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import AuditLog, DepositClaim, Order, User  # noqa: E402
from bot.handlers import admin  # noqa: E402
from services.detector import deposit_detector  # noqa: E402

USER = 98001
HOT = "0x" + "1" * 40
HASH = "0x" + "ab" * 32


def _swap(order_id="SWAP-T1", status="WAITING_CRYPTO_DEPOSIT", **kw):
    data = dict(order_id=order_id, telegram_id=USER, order_type="swap", crypto_symbol="USDC",
                network="BASE", crypto_amount=Decimal("5"), price_per_unit=16000, nominal_idr=80000,
                fee_idr=0, total_idr=80000, target_crypto_symbol="BNB", target_network="BSC",
                target_crypto_amount=Decimal("0.0123"), buyer_wallet="0x" + "c" * 40,
                deposit_wallet=HOT, status=status, created_at=datetime.utcnow() - timedelta(minutes=2))
    data.update(kw)
    return Order(**data)


def _msg(text="alert"):
    return SimpleNamespace(caption=None, caption_html=None, text=text, text_html=text,
                           edit_text=AsyncMock(), edit_caption=AsyncMock(), reply_text=AsyncMock())


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.pin = patch.object(settings, "ADMIN_CHAT_IDS", [1])
        self.pin.start()
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, username="husnun", full_name="Mas Husnun", balance_idr=Decimal("0")))
        self.db.commit()

    def tearDown(self):
        self.pin.stop()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _add(self, *objs):
        for obj in objs:
            self.db.add(obj)
        self.db.commit()

    def _review(self, order_id, reason="Nominal deposit kurang: diterima 4 USDC, dibutuhkan 5 USDC."):
        return AuditLog(telegram_id=USER, action="DEPOSIT_HASH_NEEDS_REVIEW", order_id=order_id,
                        from_status="WAITING_CRYPTO_DEPOSIT", to_status="WAITING_CRYPTO_DEPOSIT",
                        details=f"Hash {HASH}: {reason}")

    async def _press(self, handler, data, message=None):
        query = SimpleNamespace(data=data, message=message or _msg(), from_user=SimpleNamespace(id=1),
                                answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        with patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()) as sent:
            await handler(update, SimpleNamespace(bot=AsyncMock(), application=AsyncMock()))
        return query, sent


class AlertDepositNominalSalah(_Base):
    async def _process(self, verify_result):
        order = _swap(deposit_tx_hash=HASH)
        self._add(order)
        with patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value=verify_result)), \
             patch("services.detector.notify_admins", new=AsyncMock()) as notify, \
             patch("services.detector.safe_send_message", new=AsyncMock()) as to_user:
            await deposit_detector._process_order(self.db, order, bot_app=object())
        return notify, to_user

    async def test_nominal_kurang_dikabarkan_lengkap_ke_admin_dan_user(self):
        notify, to_user = await self._process(
            {"verified": False, "reason": "Nominal deposit kurang: diterima 4 USDC, dibutuhkan 5 USDC."})
        notify.assert_awaited_once()
        alert = notify.await_args.args[1]
        for needle in ("DEPOSIT PERLU DICEK MANUAL", "CONVERT USDC (BASE) → BNB (BSC)", "@husnun",
                       "Mas Husnun", "SWAP-T1", "diterima 4 USDC, dibutuhkan 5 USDC", "BNB"):
            self.assertIn(needle, alert)
        callbacks = [b.callback_data for row in notify.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("admin_swap_manual_SWAP-T1", callbacks)
        self.assertIn("admin_reject_swap_SWAP-T1", callbacks)
        self.assertNotIn("admin_approve_swap_SWAP-T1", callbacks, "proses otomatis pasti gagal untuk nominal kurang")
        self.assertIn("Jangan kirim ulang", to_user.await_args.args[2])

    async def test_nominal_lebih_dalam_toleransi_tetap_bisa_proses_convert(self):
        notify, _ = await self._process(
            {"verified": True, "amount": 5.02, "from_address": "0x" + "d" * 40, "tx_hash": HASH,
             "timestamp": datetime.utcnow().timestamp()})
        alert = notify.await_args.args[1]
        self.assertIn("Diterima on-chain", alert)
        callbacks = [b.callback_data for row in notify.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("admin_approve_swap_SWAP-T1", callbacks)
        self.assertIn("admin_swap_manual_SWAP-T1", callbacks)


class AntreanOrder(_Base):
    def test_order_expired_yang_menunggu_cek_admin_tetap_tampil(self):
        self._add(_swap("SWAP-REV", status="expired"), self._review("SWAP-REV"), _swap("SWAP-BASI", status="expired"))
        text, markup = admin.build_admin_orders_view(self.db)
        self.assertIn("SWAP-REV", text)
        self.assertIn("PERLU DICEK ADMIN", text)
        self.assertIn("@husnun", text)
        self.assertNotIn("SWAP-BASI", text, "order expired tanpa deposit tidak masuk antrean")
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("admin_swap_manual_SWAP-REV", callbacks)
        self.assertEqual(crud.get_pending_orders_count(self.db), 1)

    def test_convert_manual_review_hanya_tombol_ss_transfer(self):
        self._add(_swap("SWAP-MR", status="manual_review"))
        _, markup = admin.build_admin_orders_view(self.db)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("admin_manual_sent_SWAP-MR", callbacks)
        self.assertNotIn("admin_reject_swap_SWAP-MR", callbacks)
        self.assertNotIn("admin_approve_swap_SWAP-MR", callbacks)


class TombolAdminConvert(_Base):
    async def test_proses_manual_memindahkan_ke_manual_review_dan_mengunci_hash(self):
        self._add(_swap("SWAP-PM", status="expired", deposit_tx_hash=HASH), self._review("SWAP-PM"))
        message = _msg()
        query, sent = await self._press(admin.admin_swap_manual_callback, "admin_swap_manual_SWAP-PM", message)
        self.db.expire_all()
        order = self.db.query(Order).filter_by(order_id="SWAP-PM").one()
        self.assertEqual(order.status, "manual_review")
        self.assertIsNotNone(self.db.query(DepositClaim).filter_by(order_id="SWAP-PM").first())
        sent.assert_awaited_once()
        markup = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "admin_manual_sent_SWAP-PM")

    async def test_proses_manual_tanpa_hash_ditolak(self):
        self._add(_swap("SWAP-NH", status="expired"))
        query, _ = await self._press(admin.admin_swap_manual_callback, "admin_swap_manual_SWAP-NH")
        self.assertIn("belum punya TX hash", query.answer.await_args.args[0])
        self.db.expire_all()
        self.assertEqual(self.db.query(Order).filter_by(order_id="SWAP-NH").one().status, "expired")

    async def test_approve_order_expired_meminta_konfirmasi_bukan_ditolak(self):
        self._add(_swap("SWAP-EX", status="expired", deposit_tx_hash=HASH))
        message = _msg()
        await self._press(admin.admin_approve_swap_callback, "admin_approve_swap_SWAP-EX", message)
        self.assertIn("KONFIRMASI KIRIM KOIN", message.reply_text.await_args.args[0])

    async def test_reject_manual_review_menjelaskan_jalan_keluar(self):
        self._add(_swap("SWAP-RJ", status="manual_review"))
        query, _ = await self._press(admin.admin_reject_swap_callback, "admin_reject_swap_SWAP-RJ")
        self.assertIn("SS Transfer Manual", query.answer.await_args.args[0])


class StatusDiAkunUser(_Base):
    async def test_riwayat_menampilkan_dicek_admin_bukan_expired(self):
        from bot.handlers.history import show_history
        self._add(_swap("SWAP-H", status="expired"), self._review("SWAP-H"))
        query = SimpleNamespace(message=SimpleNamespace(reply_chat_action=AsyncMock()),
                                edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER))
        await show_history(update, SimpleNamespace())
        text = query.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Deposit dicek admin", text)
        self.assertIn("CONVERT", text)
        self.assertNotIn("Expired", text)


if __name__ == "__main__":
    unittest.main()
