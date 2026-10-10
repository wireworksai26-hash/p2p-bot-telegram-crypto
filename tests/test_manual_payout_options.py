"""Menu Selesaikan Pengiriman Manual: selain screenshot, admin bisa mengirim TX hash atau
menandai selesai tanpa bukti; user tetap mendapat notifikasi transaksi sukses."""
import os
import unittest
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
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import AuditLog, Order, User  # noqa: E402
from bot.handlers import admin as admin_mod  # noqa: E402

ADMIN, USER = 1, 4343
HASH = "0x" + "cd" * 32


def _order(order_id="SWAP-MO-1", status="manual_review", payout_tx_hash=None):
    return Order(
        order_id=order_id, telegram_id=USER, order_type="swap", crypto_symbol="USDC", network="BASE",
        crypto_amount=Decimal("5"), target_crypto_symbol="BNB", target_network="BSC",
        target_crypto_amount=Decimal("0.0123"), price_per_unit=16000, nominal_idr=80000, fee_idr=0,
        total_idr=80000, buyer_wallet="0x" + "3" * 40, status=status, payout_tx_hash=payout_tx_hash,
    )


def _msg(text="x"):
    return SimpleNamespace(caption=None, caption_html=None, text=text, text_html=text,
                           reply_text=AsyncMock(), edit_text=AsyncMock(), edit_caption=AsyncMock())


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, username="buyer", full_name="Buyer"))
        self.db.commit()
        self._admin = patch.object(admin_mod, "is_admin", side_effect=lambda uid: uid == ADMIN)
        self._admin.start()
        self._testi = patch("services.testimony_service.schedule_transaction_testimony")
        self._testi.start()

    def tearDown(self):
        self._testi.stop()
        self._admin.stop()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _reload(self, order_id="SWAP-MO-1"):
        self.db.expire_all()
        return self.db.query(Order).filter(Order.order_id == order_id).one()

    async def _press(self, handler, data, ctx=None, user_id=ADMIN, message=None):
        query = SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id), answer=AsyncMock(),
                                message=message or _msg())
        ctx = ctx or SimpleNamespace(user_data={}, bot=AsyncMock())
        await handler(SimpleNamespace(callback_query=query, effective_user=query.from_user), ctx)
        return query, ctx

    async def _text(self, text, ctx):
        message = SimpleNamespace(text=text, reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=ADMIN))
        handled = await admin_mod.admin_interactive_text_router(update, ctx)
        return handled, message


class MenuPilihan(_Base):
    async def test_tombol_utama_menampilkan_tiga_pilihan_dan_tetap_menunggu_foto(self):
        self.db.add(_order())
        self.db.commit()
        query, ctx = await self._press(admin_mod.admin_manual_payout_callback, "admin_manual_sent_SWAP-MO-1")
        self.assertEqual(ctx.user_data["admin_awaiting_payout_proof_order_id"], "SWAP-MO-1")
        markup = query.message.reply_text.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(callbacks, ["admin_manual_ss_SWAP-MO-1", "admin_manual_tx_SWAP-MO-1",
                                     "admin_manual_done_SWAP-MO-1"])

    async def test_bukan_admin_ditolak(self):
        self.db.add(_order())
        self.db.commit()
        query, ctx = await self._press(admin_mod.admin_manual_option_callback, "admin_manual_doneok_SWAP-MO-1",
                                       user_id=USER)
        self.assertIn("ditolak", query.answer.await_args.args[0])
        self.assertEqual(self._reload().status, "manual_review")


class KirimTxHash(_Base):
    async def test_tx_hash_diteruskan_ke_user_dan_order_selesai(self):
        self.db.add(_order())
        self.db.commit()
        query, ctx = await self._press(admin_mod.admin_manual_option_callback, "admin_manual_tx_SWAP-MO-1")
        self.assertEqual(ctx.user_data["admin_awaiting_payout_txhash_order_id"], "SWAP-MO-1")
        self.assertNotIn("admin_awaiting_payout_proof_order_id", ctx.user_data)
        self.assertIn("BSC", query.message.reply_text.await_args.args[0], "jaringan koin tujuan convert")

        handled, message = await self._text(HASH, ctx)
        self.assertTrue(handled)
        order = self._reload()
        self.assertEqual(order.status, "completed")
        self.assertEqual(order.payout_tx_hash, HASH)
        sent = ctx.bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], USER)
        self.assertIn("DITRANSFER OLEH ADMIN", sent["text"])
        self.assertIn(HASH, sent["text"])
        self.assertIn("BNB", sent["text"])
        self.assertIn("TX HASH TRANSFER MANUAL DITERUSKAN", message.reply_text.await_args.args[0])
        self.assertNotIn("admin_awaiting_payout_txhash_order_id", ctx.user_data)
        log = self.db.query(AuditLog).filter_by(order_id="SWAP-MO-1", action="PAYOUT_MANUAL_PROOF").one()
        self.assertIn(HASH, log.details)

    async def test_format_salah_diminta_ulang(self):
        self.db.add(_order())
        self.db.commit()
        ctx = SimpleNamespace(user_data={"admin_awaiting_payout_txhash_order_id": "SWAP-MO-1"}, bot=AsyncMock())
        handled, message = await self._text("bukan hash", ctx)
        self.assertTrue(handled)
        self.assertIn("tidak valid", message.reply_text.await_args.args[0])
        self.assertEqual(self._reload().status, "manual_review")
        self.assertIn("admin_awaiting_payout_txhash_order_id", ctx.user_data)
        ctx.bot.send_message.assert_not_awaited()

    async def test_tx_hash_order_lain_ditolak(self):
        self.db.add(_order())
        self.db.add(_order("SWAP-LAIN", status="completed", payout_tx_hash=HASH))
        self.db.commit()
        ctx = SimpleNamespace(user_data={"admin_awaiting_payout_txhash_order_id": "SWAP-MO-1"}, bot=AsyncMock())
        handled, message = await self._text(HASH, ctx)
        self.assertIn("SWAP-LAIN", message.reply_text.await_args.args[0])
        self.assertEqual(self._reload().status, "manual_review")


class TandaiSelesaiTanpaBukti(_Base):
    async def test_minta_konfirmasi_lalu_selesai_dan_user_dapat_notif_sukses(self):
        self.db.add(_order())
        self.db.commit()
        query, ctx = await self._press(admin_mod.admin_manual_option_callback, "admin_manual_done_SWAP-MO-1")
        self.assertEqual(self._reload().status, "manual_review", "belum selesai sebelum dikonfirmasi")
        markup = query.message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "admin_manual_doneok_SWAP-MO-1")

        confirm = _msg("⚠️ TANDAI SELESAI TANPA BUKTI?")
        query, ctx = await self._press(admin_mod.admin_manual_option_callback, "admin_manual_doneok_SWAP-MO-1",
                                       message=confirm)
        self.assertEqual(self._reload().status, "completed")
        sent = ctx.bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], USER)
        self.assertIn("COMPLETED", sent["text"])
        self.assertNotIn("TX Hash", sent["text"])
        ctx.bot.send_photo.assert_not_awaited()
        self.assertIn("TANPA BUKTI", confirm.edit_text.await_args.kwargs["text"])

    async def test_order_sudah_selesai_tidak_diproses_ulang(self):
        self.db.add(_order(status="completed"))
        self.db.commit()
        query, ctx = await self._press(admin_mod.admin_manual_option_callback, "admin_manual_doneok_SWAP-MO-1")
        self.assertIn("COMPLETED", query.answer.await_args.args[0])
        ctx.bot.send_message.assert_not_awaited()

    async def test_router_mengarahkan_tombol_konfirmasi(self):
        from bot.handlers.start import menu_callback_handler
        self.db.add(_order())
        self.db.commit()
        query = SimpleNamespace(data="admin_manual_doneok_SWAP-MO-1", from_user=SimpleNamespace(id=ADMIN),
                                answer=AsyncMock(), message=_msg())
        ctx = SimpleNamespace(user_data={}, bot=AsyncMock())
        await menu_callback_handler(SimpleNamespace(callback_query=query, effective_user=query.from_user), ctx)
        self.assertEqual(self._reload().status, "completed")


if __name__ == "__main__":
    unittest.main()
