"""Reject manual di Antrean Order: order yang ditolak harus hilang dari daftar.

Bug 1 (akar masalah): menu_callback_handler menjawab callback di awal. Telegram hanya menerima
satu jawaban per callback, jadi query.answer(...) di handler Reject selalu BadRequest: order sudah
rejected di DB tetapi panel tidak dimuat ulang, dan konfirmasi refund Saldo Bot tidak pernah muncul.

Bug 2: konfirmasi "✅ Ya, Refund Saldo" dikirim sebagai pesan terpisah. Setelah "Ya", hanya pesan
konfirmasi yang diedit; panel Antrean Order tidak dimuat ulang.
"""
import os
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from config.settings import settings  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order, User  # noqa: E402
from bot.handlers import admin  # noqa: E402

USER = 97001


def _order(order_id, status, method, paid=True):
    return Order(order_id=order_id, telegram_id=USER, order_type="buy", crypto_symbol="USDT",
                 network="BSC", crypto_amount=Decimal("10"), price_per_unit=17000, nominal_idr=170000,
                 fee_idr=3000, total_idr=173000, buyer_wallet="0x" + "c" * 40, payment_method=method,
                 status=status, paid_at=datetime.utcnow() if paid else None, created_at=datetime.utcnow())


def _msg(text, reply_to=None):
    return SimpleNamespace(caption=None, caption_html=None, text=text, text_html=text,
                           reply_to_message=reply_to, edit_text=AsyncMock(), edit_caption=AsyncMock(),
                           reply_text=AsyncMock())


class RejectRefreshesQueue(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.pin = patch.object(settings, "ADMIN_CHAT_IDS", [1])
        self.pin.start()
        db = SessionLocal()
        db.add(User(telegram_id=USER, balance_idr=Decimal("0")))
        db.commit()
        db.close()

    def tearDown(self):
        self.pin.stop()
        Base.metadata.drop_all(bind=engine)

    def _add(self, order):
        db = SessionLocal()
        db.add(order)
        db.commit()
        db.close()

    def _queue_message(self):
        db = SessionLocal()
        try:
            text, _ = admin.build_admin_orders_view(db)
        finally:
            db.close()
        return _msg(text)

    async def _press(self, data, message):
        query = SimpleNamespace(data=data, message=message, from_user=SimpleNamespace(id=1),
                                answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        with patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()):
            await admin.admin_reject_buy_callback(update, SimpleNamespace(bot=AsyncMock()))

    async def test_konfirmasi_refund_memuat_ulang_antrean(self):
        self._add(_order("ORD-MR", "manual_review", "BOT_BALANCE"))
        queue = self._queue_message()
        self.assertIn("ORD-MR", queue.text)

        await self._press("admin_reject_buy_ORD-MR", queue)
        queue.reply_text.assert_awaited_once()
        self.assertTrue(queue.reply_text.await_args.kwargs.get("do_quote"),
                        "konfirmasi harus membalas pesan antrean agar bisa dilacak saat 'Ya'")

        confirm = _msg("⚠️ KONFIRMASI TOLAK & REFUND", reply_to=queue)
        await self._press("admin_reject_buy_yes_ORD-MR", confirm)

        queue.edit_text.assert_awaited_once()
        refreshed = queue.edit_text.await_args.kwargs["text"]
        self.assertNotIn("ORD-MR", refreshed)
        self.assertIn("Semua antrean bersih", refreshed)
        self.assertIn("DITOLAK", confirm.edit_text.await_args.kwargs["text"])

    async def test_konfirmasi_dari_notifikasi_manual_review_menandai_notifikasi(self):
        self._add(_order("ORD-NT", "manual_review", "BOT_BALANCE"))
        notif = _msg("🚨 MANUAL REVIEW REQUIRED (Saldo Bot)\n\nOrder: ORD-NT")
        confirm = _msg("⚠️ KONFIRMASI TOLAK & REFUND", reply_to=notif)
        await self._press("admin_reject_buy_yes_ORD-NT", confirm)

        sent = notif.edit_text.await_args.kwargs
        self.assertIn("DITOLAK OLEH ADMIN (SALDO DI-REFUND)", sent["text"])
        self.assertNotIn("reply_markup", sent, "tombol lama di notifikasi harus hilang")

    async def test_reject_langsung_gopay_tetap_memuat_ulang_antrean(self):
        self._add(_order("ORD-GP", "pending", "GOPAY_QRIS", paid=False))
        queue = self._queue_message()
        self.assertIn("ORD-GP", queue.text)

        await self._press("admin_reject_buy_ORD-GP", queue)
        refreshed = queue.edit_text.await_args.kwargs["text"]
        self.assertNotIn("ORD-GP", refreshed)


class OnceQuery:
    """Callback query yang meniru Telegram: jawaban kedua ditolak BadRequest."""

    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=1)
        self.answers = []
        self.edit_message_text = AsyncMock()

    async def answer(self, text=None, show_alert=False, **_):
        if self.answers:
            raise BadRequest("Query is too old and response timeout expired or query id is invalid")
        self.answers.append(text)


class RejectLewatRouter(RejectRefreshesQueue):
    """Klik tombol sungguhan lewat catch-all router (menu_callback_handler)."""

    async def _press(self, data, message):
        from bot.handlers.start import menu_callback_handler
        query = OnceQuery(data, message)
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(bot=AsyncMock(), user_data={})
        with patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()):
            await menu_callback_handler(update, context)
        return query

    async def test_popup_hasil_reject_tampil_bukan_jawaban_kosong_router(self):
        self._add(_order("ORD-POP", "pending", "GOPAY_QRIS", paid=False))
        query = await self._press("admin_reject_buy_ORD-POP", self._queue_message())
        self.assertEqual(query.answers, ["Order berhasil ditolak."])

    async def test_tombol_tanpa_jawaban_handler_tetap_dijawab_router(self):
        query = await self._press("menu_snk", _msg("menu"))
        self.assertEqual(len(query.answers), 1)


if __name__ == "__main__":
    unittest.main()
