"""Tombol "📖 Panduan Admin" dan perintah /panduan.

- Tiap halaman < 4096 karakter dan HTML valid (Telegram menolak tag tidak seimbang).
- Setiap perintah yang disebut di panduan benar-benar terdaftar (panduan tidak boleh basi).
- Setiap perintah di menu ☰ admin tercakup di panduan.
- Tombol/callback berfungsi dan hanya untuk admin.
"""
import os
import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
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
    "ADMIN_CHAT_IDS": "123456",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from telegram.ext import CommandHandler, ConversationHandler

import main
from bot.handlers import admin as admin_mod
from bot.utils.admin_guide import GUIDE_INDEX_TEXT, GUIDE_TOPICS, guide_index, guide_topic
from bot.utils.command_menu import ADMIN_COMMAND_MENU

ALLOWED_TAGS = {"b", "i", "code"}
VOID_OK = set()


class _TagChecker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"tag tidak diizinkan: <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"penutup </{tag}> tidak cocok (stack={self.stack})")
        else:
            self.stack.pop()


def _all_pages():
    pages = {"index": GUIDE_INDEX_TEXT}
    pages.update({key: text for key, (_label, text) in GUIDE_TOPICS.items()})
    return pages


def _registered_commands() -> set:
    names = set()
    application = main.build_bot_application()
    for handlers in application.handlers.values():
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                names |= set(handler.commands)
            elif isinstance(handler, ConversationHandler):
                for entry in handler.entry_points:
                    if isinstance(entry, CommandHandler):
                        names |= set(entry.commands)
    return names


class TestIsiPanduan(unittest.TestCase):
    def test_panjang_di_bawah_batas_telegram(self):
        for key, text in _all_pages().items():
            with self.subTest(halaman=key):
                self.assertLess(len(text), 4096, f"halaman '{key}' {len(text)} karakter")

    def test_html_valid(self):
        for key, text in _all_pages().items():
            with self.subTest(halaman=key):
                checker = _TagChecker()
                checker.feed(text)
                checker.close()
                self.assertEqual(checker.errors, [])
                self.assertEqual(checker.stack, [], "ada tag yang belum ditutup")
                self.assertNotRegex(re.sub(r"<[^>]+>", "", text), r"[<>]", "tanda <> mentah harus &lt; &gt;")

    def test_perintah_yang_disebut_benar_benar_ada(self):
        registered = _registered_commands()
        mentioned = set()
        for text in _all_pages().values():
            mentioned |= set(re.findall(r"<code>/([a-z0-9_]+)", text))
        self.assertTrue(mentioned, "panduan harus menyebut perintah")
        missing = sorted(c for c in mentioned if c not in registered)
        self.assertEqual(missing, [], f"perintah di panduan tanpa handler: {missing}")

    def test_semua_perintah_menu_admin_tercakup(self):
        everything = " ".join(_all_pages().values())
        for name, _desc in ADMIN_COMMAND_MENU:
            if name in ("start", "cancel"):
                continue
            with self.subTest(command=name):
                self.assertIn(f"/{name}", everything, f"/{name} ada di menu ☰ tapi tidak dijelaskan di panduan")

    def test_topik_penting_ada(self):
        for key in ("jual", "convert", "beli", "saldo", "user", "reward", "harga", "testi", "notif", "cmd", "tips"):
            self.assertIn(key, GUIDE_TOPICS)


class TestNavigasi(unittest.TestCase):
    def _callbacks(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_callback_data_maks_64_byte(self):
        _text, markup = guide_index()
        pages = [markup] + [guide_topic(k)[1] for k in GUIDE_TOPICS]
        for m in pages:
            for cb in self._callbacks(m):
                self.assertLessEqual(len(cb.encode()), 64, cb)

    def test_index_menautkan_semua_topik(self):
        _text, markup = guide_index()
        cbs = self._callbacks(markup)
        for key in GUIDE_TOPICS:
            self.assertIn(f"admin_panel_guide_{key}", cbs)
        self.assertIn("admin_panel_main", cbs)

    def test_topik_punya_navigasi_dan_jalan_pulang(self):
        keys = list(GUIDE_TOPICS)
        first = self._callbacks(guide_topic(keys[0])[1])
        last = self._callbacks(guide_topic(keys[-1])[1])
        self.assertNotIn(f"admin_panel_guide_{keys[-1]}", first, "halaman pertama tanpa tombol sebelumnya")
        self.assertIn(f"admin_panel_guide_{keys[1]}", first)
        self.assertIn(f"admin_panel_guide_{keys[-2]}", last)
        for cbs in (first, last):
            self.assertIn("admin_panel_guide", cbs)
            self.assertIn("admin_panel_main", cbs)

    def test_topik_tidak_dikenal(self):
        self.assertIsNone(guide_topic("tidak-ada"))

    def test_tombol_ada_di_dashboard(self):
        markup = admin_mod.get_admin_dashboard_keyboard(0)
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("admin_panel_guide", cbs)
        self.assertIn("admin_panel_close", cbs)


class TestCallbackDanPerintah(unittest.IsolatedAsyncioTestCase):
    def _query(self, data, user_id=123456):
        return SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(), edit_message_text=AsyncMock(), message=AsyncMock())

    async def _run(self, data, user_id=123456):
        query = self._query(data, user_id)
        update = SimpleNamespace(callback_query=query)
        with patch.object(main.settings, "ADMIN_CHAT_IDS", [123456]), \
             patch.object(admin_mod, "SessionLocal", return_value=SimpleNamespace(close=lambda: None)):
            await admin_mod.admin_panel_callback(update, SimpleNamespace(user_data={}))
        return query

    async def test_buka_daftar_panduan(self):
        query = await self._run("admin_panel_guide")
        kwargs = query.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["text"], GUIDE_INDEX_TEXT)
        self.assertEqual(kwargs["parse_mode"], "HTML")

    async def test_buka_satu_topik(self):
        query = await self._run("admin_panel_guide_jual")
        self.assertEqual(query.edit_message_text.await_args.kwargs["text"], GUIDE_TOPICS["jual"][1])

    async def test_topik_tidak_dikenal_memberi_peringatan(self):
        query = await self._run("admin_panel_guide_xyz")
        query.edit_message_text.assert_not_called()
        self.assertTrue(query.answer.await_args.kwargs.get("show_alert"))

    async def test_bukan_admin_ditolak(self):
        query = await self._run("admin_panel_guide", user_id=999)
        query.edit_message_text.assert_not_called()
        self.assertIn("Akses ditolak", query.answer.await_args.args[0])

    async def test_panduan_admin_untuk_admin_dan_panduan_user_untuk_lainnya(self):
        from bot.utils import user_guide
        message = AsyncMock()
        update = lambda uid: SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_message=message)
        with patch.object(main.settings, "ADMIN_CHAT_IDS", [123456]):
            await admin_mod.guide_command_handler(update(123456), None)
            self.assertEqual(message.reply_text.await_args.args[0], GUIDE_INDEX_TEXT)
            await admin_mod.guide_command_handler(update(999), None)
        self.assertEqual(message.reply_text.await_args.args[0], user_guide.GUIDE_INDEX_TEXT,
                         "user biasa tidak boleh melihat panduan admin")
        self.assertNotEqual(user_guide.GUIDE_INDEX_TEXT, GUIDE_INDEX_TEXT)

    async def test_bantuan_untuk_semua_orang(self):
        from bot.utils import user_guide
        message = AsyncMock()
        for uid in (123456, 999):
            await admin_mod.user_guide_command_handler(
                SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_message=message), None)
            self.assertEqual(message.reply_text.await_args.args[0], user_guide.GUIDE_INDEX_TEXT)


if __name__ == "__main__":
    unittest.main()
