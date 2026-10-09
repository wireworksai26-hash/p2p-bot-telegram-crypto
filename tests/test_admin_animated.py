"""Emoji animasi (3D) di panel admin: tombol ber-icon dan pesan ke chat admin.

User biasa tidak boleh terpengaruh; bila Telegram menolak pesan beranimasi, pesan dikirim ulang apa adanya.
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
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

from telegram.error import BadRequest
from telegram.ext import Application, ExtBot

from bot.utils import animated as an
from bot.utils.animated import AnimatedButton, AnimatedExtBot, animate_html, split_leading_emoji
from bot.utils.animated_map import ANIMATED_EMOJI

GIFT = ANIMATED_EMOJI["\U0001f381"]


class TestAnimateHtml(unittest.TestCase):
    def test_emoji_dalam_teks_jadi_tg_emoji(self):
        out = animate_html("\U0001f381 <b>Reward</b>")
        self.assertIn(f'<tg-emoji emoji-id="{GIFT}">\U0001f381</tg-emoji>', out)
        self.assertIn("<b>Reward</b>", out)

    def test_variation_selector_ikut_dibungkus(self):
        out = animate_html("⚠️ awas")
        self.assertIn("<tg-emoji", out)
        self.assertIn("⚠️</tg-emoji>", out)

    def test_emoji_di_dalam_code_pre_dan_tag_tidak_diubah(self):
        for src in ("<code>\U0001f381</code>", "<pre>\U0001f381</pre>", '<a href="x">\U0001f381</a>'):
            if src.startswith("<a"):
                self.assertIn("<tg-emoji", animate_html(src))  # tautan boleh berisi emoji animasi
            else:
                self.assertEqual(animate_html(src), src)

    def test_tidak_dobel_dan_batas_jumlah(self):
        once = animate_html("\U0001f381")
        self.assertEqual(animate_html(once), once)
        many = animate_html("\U0001f381" * 100, limit=5)
        self.assertEqual(many.count("<tg-emoji"), 5)

    def test_emoji_tanpa_padanan_dan_teks_biasa_tetap(self):
        self.assertEqual(animate_html("tanpa emoji <b>x</b>"), "tanpa emoji <b>x</b>")
        self.assertEqual(animate_html("\U0001f4f8 foto"), "\U0001f4f8 foto")


class TestAnimatedButton(unittest.TestCase):
    def test_emoji_di_depan_jadi_icon(self):
        b = AnimatedButton("\U0001f381 Kirim Reward", callback_data="x")
        self.assertEqual(b.text, "Kirim Reward")
        self.assertEqual(b.icon_custom_emoji_id, GIFT)
        self.assertEqual(b.callback_data, "x")

    def test_tanpa_emoji_atau_tanpa_padanan_tidak_berubah(self):
        self.assertEqual(AnimatedButton("Tanpa emoji", callback_data="x").text, "Tanpa emoji")
        b = AnimatedButton("\U0001f4f8 Foto", callback_data="x")
        self.assertEqual((b.text, b.icon_custom_emoji_id), ("\U0001f4f8 Foto", None))

    def test_label_hanya_emoji_dan_icon_eksplisit_dihormati(self):
        self.assertEqual(split_leading_emoji("\U0001f381"), (None, "\U0001f381"))
        b = AnimatedButton("\U0001f381 X", callback_data="x", icon_custom_emoji_id="123")
        self.assertEqual((b.text, b.icon_custom_emoji_id), ("\U0001f381 X", "123"))

    def test_url_button_dan_semua_tombol_admin_valid(self):
        b = AnimatedButton("\U0001f381 Buka", url="https://t.me/x")
        self.assertEqual(b.url, "https://t.me/x")
        from bot.handlers.admin import get_admin_dashboard_keyboard
        rows = get_admin_dashboard_keyboard(3).inline_keyboard
        buttons = [b for r in rows for b in r]
        self.assertGreaterEqual(sum(1 for b in buttons if b.icon_custom_emoji_id), 10)
        self.assertTrue(all(b.text.strip() for b in buttons))


class TestAnimatedBot(unittest.TestCase):
    def setUp(self):
        from config.settings import settings
        patcher = patch.object(settings, "ADMIN_CHAT_IDS", [999])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _bot(self):
        app = Application.builder().token("123456:TEST_ONLY").build()
        self.assertTrue(an.install_animated_bot(app))
        return app.bot

    def test_terpasang_di_application_dan_tetap_extbot(self):
        bot = self._bot()
        self.assertIsInstance(bot, AnimatedExtBot)
        self.assertIsInstance(bot, ExtBot)

    def test_pesan_ke_admin_beranimasi_user_biasa_tidak(self):
        bot = self._bot()
        with patch.object(ExtBot, "send_message", new=AsyncMock()) as send:
            asyncio.run(bot.send_message(chat_id=999, text="\U0001f381 hi", parse_mode="HTML"))
            asyncio.run(bot.send_message(chat_id=111, text="\U0001f381 hi", parse_mode="HTML"))
            asyncio.run(bot.send_message(chat_id=999, text="\U0001f381 polos"))
        admin_call, user_call, plain_call = send.await_args_list
        self.assertIn("<tg-emoji", admin_call.kwargs["text"])
        self.assertEqual(user_call.kwargs["text"], "\U0001f381 hi")
        self.assertEqual(plain_call.kwargs["text"], "\U0001f381 polos")

    def test_edit_pesan_admin_beranimasi(self):
        bot = self._bot()
        with patch.object(ExtBot, "edit_message_text", new=AsyncMock()) as edit:
            asyncio.run(bot.edit_message_text("\U0001f381 x", chat_id=999, message_id=5, parse_mode="HTML"))
        self.assertIn("<tg-emoji", edit.await_args.kwargs["text"])

    def test_ditolak_telegram_dikirim_ulang_tanpa_animasi(self):
        bot = self._bot()
        send = AsyncMock(side_effect=[BadRequest("Can't parse entities"), "ok"])
        with patch.object(ExtBot, "send_message", new=send):
            result = asyncio.run(bot.send_message(chat_id=999, text="\U0001f381 hi", parse_mode="HTML"))
        self.assertEqual(result, "ok")
        self.assertIn("<tg-emoji", send.await_args_list[0].kwargs["text"])
        self.assertEqual(send.await_args_list[1].kwargs["text"], "\U0001f381 hi")

    def test_not_modified_tidak_dikirim_ulang(self):
        bot = self._bot()
        edit = AsyncMock(side_effect=BadRequest("Message is not modified"))
        with patch.object(ExtBot, "edit_message_text", new=edit):
            with self.assertRaises(BadRequest):
                asyncio.run(bot.edit_message_text("\U0001f381 x", chat_id=999, message_id=5, parse_mode="HTML"))
        self.assertEqual(edit.await_count, 1)


class TestFallbackWhenTelegramRejectsCustomEmoji(unittest.TestCase):
    """Bot tanpa hak emoji animasi (pemilik bukan Premium): pesan harus tetap terkirim, tanpa emoji animasi."""

    def setUp(self):
        from config.settings import settings
        patcher = patch.object(settings, "ADMIN_CHAT_IDS", [999])
        patcher.start()
        self.addCleanup(patcher.stop)
        an._STATE.update(rejects=0, accepts=0, warned=False)
        app = Application.builder().token("123456:TEST_ONLY").build()
        an.install_animated_bot(app)
        self.bot = app.bot

    @staticmethod
    def _menu():
        from telegram import InlineKeyboardMarkup
        return InlineKeyboardMarkup([[
            AnimatedButton("🎁 Reward", callback_data="a"),
            AnimatedButton("Tanpa Emoji", callback_data="b"),
        ]])

    @staticmethod
    def _reject_when_custom(**kw):
        markup = kw.get("reply_markup")
        has_icon = any(getattr(b, "icon_custom_emoji_id", None) for r in markup.inline_keyboard for b in r) if markup else False
        if has_icon or "<tg-emoji" in (kw.get("text") or ""):
            raise BadRequest("Bad Request: DOCUMENT_INVALID")
        return "terkirim"

    def test_icon_tombol_ditolak_dikirim_ulang_dengan_emoji_di_label(self):
        send = AsyncMock(side_effect=lambda *a, **kw: self._reject_when_custom(**kw))
        with patch.object(ExtBot, "send_message", new=send):
            res = asyncio.run(self.bot.send_message(chat_id=111, text="menu", reply_markup=self._menu()))
        self.assertEqual(res, "terkirim")
        retry = send.await_args_list[-1].kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual(retry[0].text, "🎁 Reward")
        self.assertIsNone(retry[0].icon_custom_emoji_id)
        self.assertEqual(retry[0].callback_data, "a")
        self.assertEqual(retry[1].text, "Tanpa Emoji")

    def test_teks_tg_emoji_ditolak_dikirim_ulang_jadi_unicode(self):
        send = AsyncMock(side_effect=lambda *a, **kw: self._reject_when_custom(**kw))
        with patch.object(ExtBot, "send_message", new=send):
            asyncio.run(self.bot.send_message(chat_id=999, text="🎁 halo", parse_mode="HTML"))
        self.assertIn("<tg-emoji", send.await_args_list[0].kwargs["text"])
        self.assertEqual(send.await_args_list[-1].kwargs["text"], "🎁 halo")

    def test_setelah_beberapa_penolakan_langsung_tanpa_emoji(self):
        send = AsyncMock(side_effect=lambda *a, **kw: self._reject_when_custom(**kw))
        with patch.object(ExtBot, "send_message", new=send):
            for _ in range(2):
                asyncio.run(self.bot.send_message(chat_id=111, text="m", reply_markup=self._menu()))
            before = send.await_count
            asyncio.run(self.bot.send_message(chat_id=111, text="m", reply_markup=self._menu()))
        self.assertEqual(send.await_count - before, 1, "tidak ada request gagal lagi")

    def test_error_lain_tanpa_emoji_tidak_dikirim_ulang(self):
        send = AsyncMock(side_effect=BadRequest("Chat not found"))
        with patch.object(ExtBot, "send_message", new=send):
            with self.assertRaises(BadRequest):
                asyncio.run(self.bot.send_message(chat_id=111, text="polos"))
        self.assertEqual(send.await_count, 1)

    def test_error_asli_naik_bila_kirim_ulang_juga_gagal(self):
        send = AsyncMock(side_effect=BadRequest("Chat not found"))
        with patch.object(ExtBot, "send_message", new=send):
            with self.assertRaises(BadRequest):
                asyncio.run(self.bot.send_message(chat_id=111, text="x", reply_markup=self._menu()))
        self.assertEqual(send.await_count, 2)

    def test_edit_pesan_tombol_ditolak_dikirim_ulang(self):
        edit = AsyncMock(side_effect=lambda *a, **kw: self._reject_when_custom(**kw))
        with patch.object(ExtBot, "edit_message_text", new=edit):
            res = asyncio.run(self.bot.edit_message_text("menu", chat_id=111, message_id=5, reply_markup=self._menu()))
        self.assertEqual(res, "terkirim")


class TestUploadSurvivesFallback(unittest.TestCase):
    """F1: kirim ulang setelah emoji animasi ditolak tidak boleh mengirim file KOSONG (mis. foto QRIS dari BytesIO)."""

    def setUp(self):
        an._STATE.update(rejects=0, accepts=0, warned=False)
        app = Application.builder().token("123456:TEST_ONLY").build()
        an.install_animated_bot(app)
        self.bot = app.bot

    def test_bytesio_dibaca_ulang_dari_awal_pada_kirim_ulang(self):
        import io
        from telegram import InlineKeyboardMarkup
        seen = []

        async def fake_send_photo(self_, **kw):
            data = kw["photo"].read()  # meniru PTB yang membaca stream saat upload
            seen.append(len(data))
            markup = kw.get("reply_markup")
            if any(getattr(b, "icon_custom_emoji_id", None) for r in markup.inline_keyboard for b in r):
                raise BadRequest("Bad Request: DOCUMENT_INVALID")
            return "terkirim"

        stream = io.BytesIO(b"QRIS-IMAGE-BYTES")
        markup = InlineKeyboardMarkup([[AnimatedButton("🎁 Kembali", callback_data="x")]])
        with patch.object(ExtBot, "send_photo", new=fake_send_photo):
            res = asyncio.run(self.bot.send_photo(chat_id=111, photo=stream, reply_markup=markup))
        self.assertEqual(res, "terkirim")
        self.assertEqual(seen, [16, 16], "percobaan kedua harus mengirim file yang sama, bukan 0 byte")


if __name__ == "__main__":
    unittest.main()
