"""Unit tests for Broadcast Enhancement (Phase 2) — segment targeting.

Covers:
- Segment parsing (--all, --active, --buyers, --balance)
- get_users_by_segment CRUD queries (all, active, buyers, balance)
- Broadcast with segment flag routes to correct users
- Empty segment flag (message after flag)
- Backward compatibility (no flag = all users)
- Segment label display
"""
import os
import sys
import unittest
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "DATABASE_URL": "sqlite:///:memory:",
    "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "999",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from database.connection import Base, engine, SessionLocal
from database.models import User, Order
from database.crud import get_users_by_segment, get_segment_count
from bot.handlers.admin import (
    _parse_broadcast_segment,
    _segment_label,
    broadcast_handler,
)


class TestBroadcastSegmentParser(unittest.TestCase):
    """Test the flag parser utility."""

    def test_no_flag_returns_all(self):
        seg, msg = _parse_broadcast_segment("Halo member")
        self.assertEqual(seg, "all")
        self.assertEqual(msg, "Halo member")

    def test_all_flag(self):
        seg, msg = _parse_broadcast_segment("--all Promo hari ini")
        self.assertEqual(seg, "all")
        self.assertEqual(msg, "Promo hari ini")

    def test_active_flag(self):
        seg, msg = _parse_broadcast_segment("--active Hanya untuk user aktif")
        self.assertEqual(seg, "active")
        self.assertEqual(msg, "Hanya untuk user aktif")

    def test_buyers_flag(self):
        seg, msg = _parse_broadcast_segment("--buyers Terima kasih sudah bertransaksi")
        self.assertEqual(seg, "buyers")
        self.assertEqual(msg, "Terima kasih sudah bertransaksi")

    def test_balance_flag(self):
        seg, msg = _parse_broadcast_segment("--balance Saldo Anda menunggu!")
        self.assertEqual(seg, "balance")
        self.assertEqual(msg, "Saldo Anda menunggu!")

    def test_flag_only_no_message(self):
        seg, msg = _parse_broadcast_segment("--buyers")
        self.assertEqual(seg, "buyers")
        self.assertEqual(msg, "")

    def test_flag_in_middle_not_parsed(self):
        """Flag must be at the start."""
        seg, msg = _parse_broadcast_segment("Hello --buyers world")
        self.assertEqual(seg, "all")
        self.assertEqual(msg, "Hello --buyers world")


class TestSegmentLabel(unittest.TestCase):
    def test_all_segments_have_labels(self):
        for seg in ("all", "active", "buyers", "balance"):
            label = _segment_label(seg)
            self.assertIsInstance(label, str)
            self.assertGreater(len(label), 3)

    def test_unknown_segment_fallback(self):
        label = _segment_label("unknown_segment")
        self.assertIn("Semua", label)


class TestSegmentQuery(unittest.IsolatedAsyncioTestCase):
    """Test get_users_by_segment CRUD with real in-memory DB."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        # Users
        db.add_all([
            User(telegram_id=10, username="active_buyer", is_banned=False,
                 balance_idr=Decimal("5000")),
            User(telegram_id=20, username="active_no_order", is_banned=False,
                 balance_idr=Decimal("0")),
            User(telegram_id=30, username="old_buyer", is_banned=False,
                 balance_idr=Decimal("0")),
            User(telegram_id=40, username="banned_user", is_banned=True,
                 balance_idr=Decimal("1000")),
        ])
        db.commit()

        # Orders
        now = datetime.utcnow()
        db.add_all([
            # Active order (recent) + completed → active_buyer is in active AND buyers
            Order(
                order_id="ORD-001", telegram_id=10, order_type="buy",
                crypto_symbol="USDT", network="BSC",
                crypto_amount=Decimal("1"), price_per_unit=16000,
                nominal_idr=5000, fee_idr=3000, total_idr=8000,
                status="completed", created_at=now - timedelta(days=5),
            ),
            # Old order (60 days ago) + completed → old_buyer is in buyers but NOT active
            Order(
                order_id="ORD-002", telegram_id=30, order_type="buy",
                crypto_symbol="USDT", network="BSC",
                crypto_amount=Decimal("1"), price_per_unit=16000,
                nominal_idr=5000, fee_idr=3000, total_idr=8000,
                status="completed", created_at=now - timedelta(days=60),
            ),
        ])
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def test_segment_all(self):
        db = SessionLocal()
        try:
            users = get_users_by_segment(db, "all")
            ids = {u.telegram_id for u in users}
            self.assertEqual(ids, {10, 20, 30})  # banned excluded
        finally:
            db.close()

    def test_segment_active(self):
        db = SessionLocal()
        try:
            users = get_users_by_segment(db, "active")
            ids = {u.telegram_id for u in users}
            self.assertEqual(ids, {10})  # Only recent order
        finally:
            db.close()

    def test_segment_buyers(self):
        db = SessionLocal()
        try:
            users = get_users_by_segment(db, "buyers")
            ids = {u.telegram_id for u in users}
            self.assertEqual(ids, {10, 30})  # Both completed orders
        finally:
            db.close()

    def test_segment_balance(self):
        db = SessionLocal()
        try:
            users = get_users_by_segment(db, "balance")
            ids = {u.telegram_id for u in users}
            self.assertEqual(ids, {10})  # Has balance + not banned
        finally:
            db.close()

    def test_segment_count(self):
        db = SessionLocal()
        try:
            self.assertEqual(get_segment_count(db, "all"), 3)
            self.assertEqual(get_segment_count(db, "active"), 1)
            self.assertEqual(get_segment_count(db, "buyers"), 2)
            self.assertEqual(get_segment_count(db, "balance"), 1)
        finally:
            db.close()


class TestBroadcastWithSegment(unittest.IsolatedAsyncioTestCase):
    """Integration tests for broadcast_handler with segment flags."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([
            User(telegram_id=10, username="alice", is_banned=False, balance_idr=Decimal("5000")),
            User(telegram_id=20, username="bob", is_banned=False, balance_idr=Decimal("0")),
            User(telegram_id=30, username="banned", is_banned=True, balance_idr=Decimal("0")),
        ])
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def _msg(self, text="/broadcast Halo", photo=None, caption=None):
        msg = AsyncMock()
        msg.text = text
        msg.photo = photo or []
        msg.caption = caption
        msg.reply_to_message = None
        msg.document = None
        return msg

    def _run(self, message, admin=True, bot=None):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            message=message,
        )
        context = SimpleNamespace(bot=bot or AsyncMock(), args=[])
        patches = [
            patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()),
            patch("bot.handlers.admin.is_admin", return_value=admin),
            patch("bot.handlers.admin.asyncio.sleep", new=AsyncMock()),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return update, context

    async def test_broadcast_all_default(self):
        """No segment flag → all non-banned users."""
        bot = AsyncMock()
        update, ctx = self._run(self._msg(), bot=bot)
        await broadcast_handler(update, ctx)
        # Should send to 2 non-banned users
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_broadcast_balance_segment(self):
        """--balance flag → only users with saldo > 0."""
        bot = AsyncMock()
        update, ctx = self._run(
            self._msg(text="/broadcast --balance Saldo menunggu!"),
            bot=bot,
        )
        await broadcast_handler(update, ctx)
        self.assertEqual(bot.send_message.await_count, 1)  # Only alice

    async def test_broadcast_report_includes_segment(self):
        """Report should mention the segment."""
        bot = AsyncMock()
        update, ctx = self._run(
            self._msg(text="/broadcast --balance Test"),
            bot=bot,
        )
        await broadcast_handler(update, ctx)
        report = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("LAPORAN BROADCAST", report)
        self.assertIn("Bersaldo", report)

    async def test_broadcast_empty_segment_rejected(self):
        """Flag only without message → error."""
        bot = AsyncMock()
        update, ctx = self._run(
            self._msg(text="/broadcast --buyers"),
            bot=bot,
        )
        await broadcast_handler(update, ctx)
        bot.send_message.assert_not_awaited()
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("kosong", text)

    async def test_non_admin_ditolak(self):
        bot = AsyncMock()
        update, ctx = self._run(self._msg(), admin=False, bot=bot)
        await broadcast_handler(update, ctx)
        bot.send_message.assert_not_awaited()

    async def test_broadcast_photo_direct(self):
        """Direct photo upload with caption /broadcast Promo Foto."""
        bot = AsyncMock()
        photo_obj = SimpleNamespace(file_id="photo_xyz_123")
        msg = self._msg(text=None, photo=[photo_obj], caption="/broadcast Promo Foto Langsung")
        msg.document = None
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        self.assertEqual(bot.send_photo.await_count, 2)
        call_kwargs = bot.send_photo.call_args_list[0].kwargs
        self.assertEqual(call_kwargs["photo"], "photo_xyz_123")
        self.assertIn("Promo Foto Langsung", call_kwargs["caption"])
        self.assertEqual(call_kwargs["parse_mode"], "HTML")

    async def test_broadcast_photo_reply(self):
        """Reply to a photo message with /broadcast Teks Reply."""
        bot = AsyncMock()
        replied_photo = SimpleNamespace(file_id="replied_photo_456")
        replied_msg = SimpleNamespace(photo=[replied_photo], caption="Foto Original", document=None)
        
        msg = self._msg(text="/broadcast Promo Teks Baru")
        msg.document = None
        msg.reply_to_message = replied_msg
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        self.assertEqual(bot.send_photo.await_count, 2)
        call_kwargs = bot.send_photo.call_args_list[0].kwargs
        self.assertEqual(call_kwargs["photo"], "replied_photo_456")
        self.assertIn("Promo Teks Baru", call_kwargs["caption"])

    async def test_broadcast_photo_reply_inherits_caption(self):
        """Reply /broadcast (no text) to a photo that has a caption."""
        bot = AsyncMock()
        replied_photo = SimpleNamespace(file_id="replied_photo_789")
        replied_msg = SimpleNamespace(photo=[replied_photo], caption="Caption Asli dari Poster", document=None)
        
        msg = self._msg(text="/broadcast")
        msg.document = None
        msg.reply_to_message = replied_msg
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        self.assertEqual(bot.send_photo.await_count, 2)
        call_kwargs = bot.send_photo.call_args_list[0].kwargs
        self.assertIn("Caption Asli dari Poster", call_kwargs["caption"])

    async def test_broadcast_photo_with_segment(self):
        """Photo broadcast targeting specific segment (--balance)."""
        bot = AsyncMock()
        photo_obj = SimpleNamespace(file_id="photo_seg_999")
        msg = self._msg(text=None, photo=[photo_obj], caption="/broadcast --balance Khusus yang punya saldo")
        msg.document = None
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        # Only alice has balance > 0
        self.assertEqual(bot.send_photo.await_count, 1)
        call_kwargs = bot.send_photo.call_args_list[0].kwargs
        self.assertEqual(call_kwargs["chat_id"], 10)
        self.assertIn("Khusus yang punya saldo", call_kwargs["caption"])

    async def test_broadcast_document_image(self):
        """Image sent as document (uncompressed image/png)."""
        bot = AsyncMock()
        doc_obj = SimpleNamespace(file_id="doc_img_123", mime_type="image/png")
        msg = self._msg(text=None, photo=[], caption="/broadcast Promo Dokumen Gambar")
        msg.document = doc_obj
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        self.assertEqual(bot.send_document.await_count, 2)
        call_kwargs = bot.send_document.call_args_list[0].kwargs
        self.assertEqual(call_kwargs["document"], "doc_img_123")
        self.assertIn("Promo Dokumen Gambar", call_kwargs["caption"])

    async def test_broadcast_photo_long_caption(self):
        """Photo with caption > 1024 characters splits into photo + text message."""
        bot = AsyncMock()
        photo_obj = SimpleNamespace(file_id="photo_long_123")
        long_text = "A" * 1200
        msg = self._msg(text=None, photo=[photo_obj], caption=f"/broadcast {long_text}")
        msg.document = None
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        # 2 users -> 2 send_photo (without full caption) + 2 send_message (with full text)
        self.assertEqual(bot.send_photo.await_count, 2)
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_broadcast_photo_html_parse_error_fallback(self):
        """Photo broadcast with broken HTML tag falls back to plain text."""
        from telegram.error import BadRequest
        bot = AsyncMock()
        photo_obj = SimpleNamespace(file_id="photo_fallback_123")
        msg = self._msg(text=None, photo=[photo_obj], caption="/broadcast Beli <USDT> sekarang")
        msg.document = None

        # First call with parse_mode='HTML' raises BadRequest, second call with parse_mode=None succeeds
        async def mock_send_photo(*args, **kwargs):
            if kwargs.get("parse_mode") == "HTML":
                raise BadRequest("Can't parse entities in message text")
            return True

        bot.send_photo = AsyncMock(side_effect=mock_send_photo)
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        # It should have called send_photo twice per user (HTML then fallback plain text)
        self.assertEqual(bot.send_photo.await_count, 4)
        report = update.message.reply_text.call_args_list[-1].args[0]
        self.assertIn("Terkirim        : <code>2 user</code>", report)

    async def test_broadcast_ready_morph(self):
        """/broadcast --ready Morph automatically generates the formatted coin announcement without owner header."""
        bot = AsyncMock()
        msg = self._msg(text="/broadcast --ready Morph")
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        self.assertEqual(bot.send_message.await_count, 2)
        sent_text = bot.send_message.call_args_list[0].kwargs["text"]
        self.assertNotIn("PENGUMUMAN DARI OWNER", sent_text)
        self.assertIn("Morph Ready For Now🪙", sent_text)
        self.assertIn("USDC", sent_text)
        self.assertIn("ETH", sent_text)
        self.assertIn("Silakan /start bot untuk Beli/Jual/Swap token.", sent_text)

    async def test_broadcast_ready_with_photo(self):
        """Photo with caption /broadcast --ready Morph sets the auto-generated text as caption."""
        bot = AsyncMock()
        photo_obj = SimpleNamespace(file_id="photo_morph_123")
        msg = self._msg(text=None, photo=[photo_obj], caption="/broadcast --ready Morph")
        msg.document = None
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        self.assertEqual(bot.send_photo.await_count, 2)
        caption = bot.send_photo.call_args_list[0].kwargs["caption"]
        self.assertNotIn("PENGUMUMAN DARI OWNER", caption)
        self.assertIn("Morph Ready For Now🪙", caption)
        self.assertIn("USDC", caption)
        self.assertIn("ETH", caption)

    async def test_broadcast_no_header_in_plain_message(self):
        """Plain broadcast does not prepend PENGUMUMAN DARI OWNER anymore."""
        bot = AsyncMock()
        msg = self._msg(text="/broadcast Koin Ready Boskyuh")
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        self.assertEqual(bot.send_message.await_count, 2)
        sent_text = bot.send_message.call_args_list[0].kwargs["text"]
        self.assertEqual(sent_text, "Koin Ready Boskyuh")
        self.assertNotIn("PENGUMUMAN DARI OWNER", sent_text)

    async def test_broadcast_ready_no_args_shows_guide(self):
        """/broadcast --ready without network parameter returns helpful network guide."""
        bot = AsyncMock()
        msg = self._msg(text="/broadcast --ready")
        update, ctx = self._run(msg, bot=bot)
        await broadcast_handler(update, ctx)

        bot.send_message.assert_not_awaited()
        guide = update.message.reply_text.call_args.args[0]
        self.assertIn("Format Siaran Koin Ready", guide)
        self.assertIn("Morph", guide)
        self.assertIn("Base", guide)


if __name__ == "__main__":
    unittest.main()

