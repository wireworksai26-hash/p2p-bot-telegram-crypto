"""Satu QRIS Beli belum dibayar per user: selesaikan atau batalkan dulu sebelum order baru.

Bug: user bisa membuat order QRIS baru walau QRIS sebelumnya belum dibayar, sehingga order
sampah menumpuk (abuse). Belum ada tombol batal untuk order QRIS.
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

from telegram.ext import ConversationHandler  # noqa: E402

import database.models  # noqa: F401,E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order, User  # noqa: E402
from bot.handlers import buy  # noqa: E402

USER = 99001


def _order(order_id="BUY-T1", status="pending", method="GOPAY_QRIS", telegram_id=USER):
    now = datetime.utcnow()
    return Order(order_id=order_id, telegram_id=telegram_id, order_type="buy", crypto_symbol="USDT",
                 network="BSC", crypto_amount=Decimal("5"), price_per_unit=17000, nominal_idr=85000,
                 fee_idr=3000, total_idr=88123, buyer_wallet="0x" + "c" * 40, payment_method=method,
                 status=status, created_at=now, expired_at=now + timedelta(minutes=10))


def _msg():
    return SimpleNamespace(caption="QRIS", caption_html="QRIS", text=None, text_html=None,
                           reply_text=AsyncMock(), edit_caption=AsyncMock(), edit_text=AsyncMock(),
                           reply_to_message=None)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add(User(telegram_id=USER, balance_idr=Decimal("0")))
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def _add(self, *orders):
        db = SessionLocal()
        for o in orders:
            db.add(o)
        db.commit()
        db.close()

    def _status(self, order_id):
        db = SessionLocal()
        try:
            return db.query(Order).filter_by(order_id=order_id).one().status
        finally:
            db.close()

    def _update(self, data=None, message=None, user_id=USER):
        query = None
        if data is not None:
            query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                                    message=message or _msg())
        return SimpleNamespace(callback_query=query, message=None if query else SimpleNamespace(reply_text=AsyncMock()),
                               effective_user=SimpleNamespace(id=user_id),
                               effective_chat=SimpleNamespace(id=user_id, type="private"))


class GerbangOrderBaru(_Base):
    async def test_tombol_beli_diblokir_bila_qris_belum_dibayar(self):
        self._add(_order())
        update = self._update(data="menu_buy")
        result = await buy.start_buy_callback(update, SimpleNamespace(user_data={}))
        self.assertEqual(result, ConversationHandler.END)
        kwargs = update.callback_query.edit_message_text.await_args.kwargs
        self.assertIn("belum dibayar", kwargs["text"])
        self.assertIn("BUY-T1", kwargs["text"])
        callbacks = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row if b.callback_data]
        self.assertIn("cancel_buy_order_BUY-T1", callbacks)
        self.assertIn("check_buy_payment_BUY-T1", callbacks)

    async def test_perintah_beli_juga_diblokir(self):
        self._add(_order())
        update = self._update()
        result = await buy.start_buy_command(update, SimpleNamespace(user_data={}))
        self.assertEqual(result, ConversationHandler.END)
        self.assertIn("belum dibayar", update.message.reply_text.await_args.args[0])

    async def test_order_lain_tidak_memblokir(self):
        self._add(_order("BUY-EXP", status="expired"), _order("BUY-CAN", status="cancelled"),
                  _order("BUY-BAL", method="BOT_BALANCE"), _order("BUY-ORANG", telegram_id=12345))
        update = self._update(data="menu_buy")
        result = await buy.start_buy_callback(update, SimpleNamespace(user_data={}))
        self.assertEqual(result, buy.SELECT_SYMBOL)

    async def test_konfirmasi_order_kedua_tidak_membuat_order(self):
        self._add(_order())
        update = self._update(data="buy_confirm")
        result = await buy.handle_order_confirmation(update, SimpleNamespace(user_data={}, bot=AsyncMock()))
        self.assertEqual(result, ConversationHandler.END)
        db = SessionLocal()
        try:
            self.assertEqual(db.query(Order).count(), 1)
        finally:
            db.close()

    def test_qris_punya_tombol_batal(self):
        callbacks = [b.callback_data for row in buy.qris_payment_keyboard("BUY-X").inline_keyboard
                     for b in row if b.callback_data]
        self.assertIn("cancel_buy_order_BUY-X", callbacks)


class BatalkanOrder(_Base):
    async def _press(self, data, payment=False, message=None, user_id=USER):
        update = self._update(data=data, message=message, user_id=user_id)
        context = SimpleNamespace(bot=AsyncMock(), user_data={})
        with patch.object(buy.gopay_service, "confirm_payment", new=AsyncMock(return_value=payment)), \
             patch.object(buy, "_run_finalize_background", new=AsyncMock()) as finalize:
            await buy.cancel_buy_order_callback(update, context)
        return update.callback_query, finalize

    async def test_minta_konfirmasi_dulu(self):
        self._add(_order())
        query, _ = await self._press("cancel_buy_order_BUY-T1")
        kwargs = query.message.reply_text.await_args.kwargs
        self.assertTrue(kwargs.get("do_quote"))
        callbacks = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(callbacks, ["cancel_buy_order_yes_BUY-T1", "cancel_buy_order_no_BUY-T1"])
        self.assertEqual(self._status("BUY-T1"), "pending")

    async def test_ya_batalkan_bila_belum_dibayar_dan_tandai_qris(self):
        self._add(_order())
        qris = _msg()
        confirm = _msg()
        confirm.reply_to_message = qris
        query, finalize = await self._press("cancel_buy_order_yes_BUY-T1", payment=False, message=confirm)
        self.assertEqual(self._status("BUY-T1"), "cancelled")
        self.assertIn("tidak berlaku lagi", qris.edit_caption.await_args.kwargs["caption"])
        self.assertIn("dibatalkan", query.edit_message_text.await_args.kwargs["text"])
        finalize.assert_not_called()

    async def test_sudah_dibayar_tidak_dibatalkan_tapi_diproses(self):
        self._add(_order())
        query, finalize = await self._press("cancel_buy_order_yes_BUY-T1", payment=True)
        self.assertEqual(self._status("BUY-T1"), "pending")
        finalize.assert_called_once()
        self.assertIn("sudah masuk", query.edit_message_text.await_args.kwargs["text"])

    async def test_gateway_tidak_pasti_pembatalan_ditahan(self):
        self._add(_order())
        query, finalize = await self._press("cancel_buy_order_yes_BUY-T1", payment=None)
        self.assertEqual(self._status("BUY-T1"), "pending")
        finalize.assert_not_called()
        self.assertIn("belum bisa dipastikan", query.edit_message_text.await_args.kwargs["text"])

    async def test_order_orang_lain_ditolak(self):
        self._add(_order(telegram_id=12345))
        query, _ = await self._press("cancel_buy_order_yes_BUY-T1")
        self.assertIn("tidak ditemukan", query.answer.await_args.args[0])
        self.assertEqual(self._status("BUY-T1"), "pending")

    async def test_tidak_jadi_batal(self):
        self._add(_order())
        query, _ = await self._press("cancel_buy_order_no_BUY-T1")
        self.assertEqual(self._status("BUY-T1"), "pending")
        self.assertIn("tetap aktif", query.edit_message_text.await_args.kwargs["text"])

    async def test_setelah_batal_bisa_order_lagi(self):
        self._add(_order())
        await self._press("cancel_buy_order_yes_BUY-T1", payment=False)
        result = await buy.start_buy_callback(self._update(data="menu_buy"), SimpleNamespace(user_data={}))
        self.assertEqual(result, buy.SELECT_SYMBOL)


if __name__ == "__main__":
    unittest.main()
