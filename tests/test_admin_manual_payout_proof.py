"""Tombol "Kirim SS Transfer Manual": payout crypto gagal (mis. RPC error) → admin kirim manual,
unggah SS transfer, bot meneruskan ke user dengan catatan "ditransfer oleh admin" dan
menyelesaikan order.
"""
import os
import sys
import unittest
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import AuditLog, Order, User  # noqa: E402
from bot.handlers import admin as admin_mod  # noqa: E402
from bot.utils.manual_payout import manual_payout_block_reason  # noqa: E402

ADMIN = 1
USER = 4242


def _order(order_id="ORD-MP-1", status="manual_review", order_type="buy", payout_tx_hash=None):
    return Order(
        order_id=order_id, telegram_id=USER, order_type=order_type,
        crypto_symbol="USDT", network="BSC", crypto_amount=Decimal("10"),
        target_crypto_symbol="SOL" if order_type == "swap" else None,
        target_network="SOLANA" if order_type == "swap" else None,
        target_crypto_amount=Decimal("0.5") if order_type == "swap" else None,
        price_per_unit=16000, nominal_idr=160000, fee_idr=6000, total_idr=160000,
        buyer_wallet="0x" + "3" * 40, payment_method="GOPAY_QRIS",
        status=status, payout_tx_hash=payout_tx_hash,
    )


def _callback(order_id, user_id=ADMIN):
    query = AsyncMock()
    query.data = f"admin_manual_sent_{order_id}"
    query.from_user = SimpleNamespace(id=user_id)
    query.message = AsyncMock()
    ctx = SimpleNamespace(user_data={}, bot=AsyncMock())
    return SimpleNamespace(callback_query=query, effective_user=query.from_user), ctx, query


def _photo_update(user_id=ADMIN):
    msg = AsyncMock()
    msg.photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big_file_id")]
    return SimpleNamespace(message=msg, effective_user=SimpleNamespace(id=user_id)), msg


class ManualPayoutBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, username="buyer", full_name="Buyer"))
        self.db.commit()
        self._admin = patch.object(admin_mod, "is_admin", side_effect=lambda uid: uid == ADMIN)
        self._admin.start()

    def tearDown(self):
        self._admin.stop()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _reload(self, order_id="ORD-MP-1"):
        self.db.expire_all()
        return self.db.query(Order).filter(Order.order_id == order_id).first()


class BlockReason(unittest.TestCase):
    def test_hanya_buy_dan_swap_yang_manual_review(self):
        self.assertEqual(manual_payout_block_reason(_order()), "")
        self.assertEqual(manual_payout_block_reason(_order(order_type="swap")), "")
        self.assertEqual(manual_payout_block_reason(_order(status="MANUAL_REVIEW")), "")
        self.assertEqual(manual_payout_block_reason(_order(status="payout_broadcasted")), "")
        for status in ("completed", "COMPLETED", "pending", "paid", "payout_processing", "cancelled"):
            self.assertTrue(manual_payout_block_reason(_order(status=status)), status)
        self.assertTrue(manual_payout_block_reason(_order(order_type="sell")))
        self.assertTrue(manual_payout_block_reason(None))


class CallbackFlow(ManualPayoutBase):
    async def test_admin_menekan_tombol_menunggu_foto(self):
        self.db.add(_order())
        self.db.commit()
        update, ctx, query = _callback("ORD-MP-1")
        await admin_mod.admin_manual_payout_callback(update, ctx)
        self.assertEqual(ctx.user_data["admin_awaiting_payout_proof_order_id"], "ORD-MP-1")
        self.assertIn("ORD-MP-1", query.message.reply_text.call_args.args[0])

    async def test_bukan_admin_ditolak(self):
        self.db.add(_order())
        self.db.commit()
        update, ctx, query = _callback("ORD-MP-1", user_id=USER)
        await admin_mod.admin_manual_payout_callback(update, ctx)
        self.assertNotIn("admin_awaiting_payout_proof_order_id", ctx.user_data)
        self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))

    async def test_order_completed_atau_jual_ditolak(self):
        self.db.add(_order("ORD-DONE", status="completed"))
        self.db.add(_order("ORD-SELL", order_type="sell"))
        self.db.commit()
        for oid in ("ORD-DONE", "ORD-SELL", "ORD-TIDAK-ADA"):
            update, ctx, query = _callback(oid)
            await admin_mod.admin_manual_payout_callback(update, ctx)
            self.assertNotIn("admin_awaiting_payout_proof_order_id", ctx.user_data, oid)
            query.message.reply_text.assert_not_awaited()

    async def test_hash_broadcast_memunculkan_peringatan_jangan_kirim_ulang(self):
        self.db.add(_order(status="payout_broadcasted", payout_tx_hash="0x" + "ab" * 32))
        self.db.commit()
        update, ctx, query = _callback("ORD-MP-1")
        await admin_mod.admin_manual_payout_callback(update, ctx)
        self.assertIn("JANGAN kirim ulang", query.message.reply_text.call_args.args[0])


class ProofFlow(ManualPayoutBase):
    async def test_foto_diteruskan_ke_user_dan_order_completed(self):
        self.db.add(_order())
        self.db.commit()
        update, msg = _photo_update()
        ctx = SimpleNamespace(user_data={"admin_awaiting_payout_proof_order_id": "ORD-MP-1"}, bot=AsyncMock())
        with patch("services.testimony_service.schedule_transaction_testimony") as testimony:
            await admin_mod.handle_admin_payout_proof(update, ctx)

        send = ctx.bot.send_photo
        send.assert_awaited_once()
        self.assertEqual(send.call_args.kwargs["chat_id"], USER)
        self.assertEqual(send.call_args.kwargs["photo"], "big_file_id")
        caption = send.call_args.kwargs["caption"]
        self.assertIn("DITRANSFER OLEH ADMIN", caption)
        self.assertIn("ORD-MP-1", caption)
        self.assertIn("COMPLETED", caption)
        self.assertNotIn("admin_awaiting_payout_proof_order_id", ctx.user_data)

        order = self._reload()
        self.assertEqual(order.status, "completed")
        self.assertIsNotNone(order.completed_at)
        log = self.db.query(AuditLog).filter(AuditLog.order_id == "ORD-MP-1",
                                             AuditLog.action == "PAYOUT_MANUAL_PROOF").one()
        self.assertEqual((log.from_status, log.to_status), ("manual_review", "completed"))
        testimony.assert_called_once()
        self.assertIn("Sukses", msg.reply_text.call_args.args[0])

    async def test_swap_memakai_koin_dan_jaringan_tujuan(self):
        self.db.add(_order(order_type="swap"))
        self.db.commit()
        update, _ = _photo_update()
        ctx = SimpleNamespace(user_data={"admin_awaiting_payout_proof_order_id": "ORD-MP-1"}, bot=AsyncMock())
        with patch("services.testimony_service.schedule_transaction_testimony"):
            await admin_mod.handle_admin_payout_proof(update, ctx)
        caption = ctx.bot.send_photo.call_args.kwargs["caption"]
        self.assertIn("SOLANA", caption)
        self.assertIn("SOL", caption)
        self.assertEqual(self._reload().status, "completed")

    async def test_order_sudah_completed_tidak_diteruskan(self):
        self.db.add(_order(status="completed"))
        self.db.commit()
        update, msg = _photo_update()
        ctx = SimpleNamespace(user_data={"admin_awaiting_payout_proof_order_id": "ORD-MP-1"}, bot=AsyncMock())
        await admin_mod.handle_admin_payout_proof(update, ctx)
        ctx.bot.send_photo.assert_not_awaited()
        self.assertIn("tidak diteruskan", msg.reply_text.call_args.args[0])

    async def test_tanpa_state_atau_bukan_admin_diabaikan(self):
        self.db.add(_order())
        self.db.commit()
        update, msg = _photo_update()
        ctx = SimpleNamespace(user_data={}, bot=AsyncMock())
        await admin_mod.handle_admin_payout_proof(update, ctx)
        update, msg = _photo_update(user_id=USER)
        ctx = SimpleNamespace(user_data={"admin_awaiting_payout_proof_order_id": "ORD-MP-1"}, bot=AsyncMock())
        await admin_mod.handle_admin_payout_proof(update, ctx)
        ctx.bot.send_photo.assert_not_awaited()
        self.assertEqual(self._reload().status, "manual_review")

    async def test_gagal_kirim_ke_user_tetap_menyelesaikan_order_dan_lapor_admin(self):
        self.db.add(_order())
        self.db.commit()
        update, msg = _photo_update()
        bot = AsyncMock()
        bot.send_photo.side_effect = RuntimeError("blocked")
        ctx = SimpleNamespace(user_data={"admin_awaiting_payout_proof_order_id": "ORD-MP-1"}, bot=bot)
        with patch("services.testimony_service.schedule_transaction_testimony"):
            await admin_mod.handle_admin_payout_proof(update, ctx)
        self.assertEqual(self._reload().status, "completed")
        self.assertIn("Gagal", msg.reply_text.call_args.args[0])


class Wiring(ManualPayoutBase):
    async def test_notifikasi_payout_gagal_memuat_tombol(self):
        from bot.handlers.buy import finalize_gopay_buy_payment
        self.db.add(_order("ORD-FIN", status="paid"))
        self.db.commit()
        order = self.db.query(Order).filter(Order.order_id == "ORD-FIN").first()
        result = {"success": False, "tx_hash": "", "explorer_url": "",
                  "error_message": "RPC timeout semua endpoint"}
        with patch("bot.handlers.buy.reserve_order_inventory", return_value=True), \
             patch("services.payout_service.send_order_payout", new=AsyncMock(return_value=result)), \
             patch("bot.handlers.buy.safe_send_message", new=AsyncMock()), \
             patch("bot.handlers.buy.notify_admins", new=AsyncMock()) as na:
            await finalize_gopay_buy_payment(self.db, order, bot=AsyncMock())
        markup = na.call_args.kwargs["reply_markup"]
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(data, ["admin_manual_sent_ORD-FIN"])

    def test_antrean_admin_menampilkan_tombol_untuk_manual_review(self):
        self.db.add(_order("ORD-Q1", status="manual_review"))
        self.db.add(_order("ORD-Q2", status="paid"))
        self.db.add(_order("ORD-Q3", status="manual_review", order_type="swap"))
        self.db.commit()
        _, markup = admin_mod.build_admin_orders_view(self.db)
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("admin_manual_sent_ORD-Q1", data)
        self.assertIn("admin_manual_sent_ORD-Q3", data)
        self.assertNotIn("admin_manual_sent_ORD-Q2", data)


if __name__ == "__main__":
    unittest.main()
