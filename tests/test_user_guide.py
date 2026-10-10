"""Tombol "📖 Panduan Transaksi" (sisi user) dan /bantuan.

- Halaman < 4096 karakter dan HTML valid.
- Angka di panduan (batas, waktu) sama dengan yang dipakai kode; panduan tidak boleh basi.
- Nama tombol yang disebut di panduan benar-benar ada di bot.
- Perintah yang disebut punya handler; callback dirutekan dari menu utama.
"""
import html
import os
import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
from bot.handlers import start as start_mod
from bot.handlers.sell import SELL_QUOTE_MINUTES
from bot.keyboards.main_menu import get_main_menu_keyboard
from bot.utils.user_guide import GUIDE_INDEX_TEXT, GUIDE_TOPICS, guide_index, guide_topic
from config.settings import settings
from services.fee_service import GAS_PAIR_MIN_IDR, MAX_TRANSACTION_IDR

ALLOWED_TAGS = {"b", "i", "code"}
BOT_SRC = ROOT / "bot"


def _idr(value: int) -> str:
    return "Rp " + f"{value:,}".replace(",", ".")


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


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _bot_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in BOT_SRC.rglob("*.py")
                     if p.name not in ("user_guide.py", "admin_guide.py"))


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

    def test_topik_wajib_ada(self):
        for key in ("mulai", "beli", "jual", "convert", "hash", "tips", "biaya", "masalah"):
            self.assertIn(key, GUIDE_TOPICS)

    def test_jual_dan_convert_menegaskan_tx_hash_wajib(self):
        for key in ("jual", "convert"):
            teks = _plain(GUIDE_TOPICS[key][1])
            self.assertIn("TX Hash", teks)
            self.assertIn("wajib", teks.lower())
        self.assertIn("/txhash", _plain(GUIDE_TOPICS["hash"][1]))

    def test_perintah_yang_disebut_punya_handler(self):
        registered = _registered_commands()
        for key, text in _all_pages().items():
            for cmd in re.findall(r"<code>/([a-z0-9_]+)", text):
                with self.subTest(halaman=key, command=cmd):
                    self.assertIn(cmd, registered)

    def test_tidak_ada_istilah_admin_internal(self):
        for key, text in _all_pages().items():
            with self.subTest(halaman=key):
                self.assertNotIn("OWNER_WALLET", text)
                self.assertNotIn("DEPOSIT_AUTOSCAN", text)
                self.assertNotIn("/credit", text)


class TestAngkaSamaDenganKode(unittest.TestCase):
    """Bila batas/waktu di kode berubah, tes ini gagal dan panduan harus diperbarui."""

    def test_batas_transaksi(self):
        teks = _plain(GUIDE_TOPICS["biaya"][1])
        self.assertIn(_idr(5000), teks)
        self.assertIn(_idr(GAS_PAIR_MIN_IDR), teks)
        self.assertIn(_idr(MAX_TRANSACTION_IDR), teks)
        self.assertIn(_idr(MAX_TRANSACTION_IDR), _plain(GUIDE_TOPICS["beli"][1]))
        self.assertIn(_idr(5000), _plain(GUIDE_TOPICS["jual"][1]))

    def test_waktu_qris_quote_dan_jendela_deposit(self):
        tips = _plain(GUIDE_TOPICS["tips"][1])
        self.assertIn(f"{settings.ORDER_EXPIRE_MINUTES} menit", tips)
        self.assertIn(f"{SELL_QUOTE_MINUTES} menit", tips)
        self.assertIn(f"{settings.SELL_DEPOSIT_WINDOW_MINUTES // 60} jam", tips)
        self.assertIn(f"{settings.ORDER_EXPIRE_MINUTES} menit", _plain(GUIDE_TOPICS["beli"][1]))
        self.assertIn(f"{SELL_QUOTE_MINUTES} menit", _plain(GUIDE_TOPICS["convert"][1]))

    def test_toleransi_pergerakan_harga(self):
        from services import quote_guard
        self.assertIn("0,5%", _plain(GUIDE_TOPICS["biaya"][1]))
        self.assertIn("0,5%", quote_guard.QUOTE_MOVED_TEXT)

    def test_jam_layanan_jual(self):
        self.assertIn("08.00", _plain(GUIDE_TOPICS["jual"][1]))
        self.assertIn("23.59", _plain(GUIDE_TOPICS["jual"][1]))


class TestNamaTombolAda(unittest.TestCase):
    LABELS = (
        "Beli Crypto", "Jual Crypto", "Convert Crypto", "Konfirmasi Jual", "Konfirmasi & Bayar",
        "Kirim TX Hash", "Masukkan TX Hash", "Cek Ulang", "Salin Alamat", "Saldo Bot", "QRIS",
        "Riwayat Transaksi", "Cek Harga", "Cek Stok", "Hubungi Owner", "Jumlah Koin", "Nominal Rupiah",
    )

    def test_label_yang_disebut_ada_di_bot(self):
        source = _bot_source()
        teks = _plain(" ".join(t for _l, t in GUIDE_TOPICS.values()))
        dipakai = [label for label in self.LABELS if label in teks]
        self.assertGreaterEqual(len(dipakai), 12, "panduan harus menyebut tombol-tombol yang dipakai user")
        for label in dipakai:
            with self.subTest(tombol=label):
                self.assertIn(label, source, f"tombol '{label}' disebut di panduan tapi tidak ada di bot")


class TestNavigasiDanRouting(unittest.IsolatedAsyncioTestCase):
    def _callbacks(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]

    def test_tombol_ada_di_menu_utama(self):
        self.assertIn("menu_guide", self._callbacks(get_main_menu_keyboard()))
        self.assertIn("menu_snk", self._callbacks(get_main_menu_keyboard()), "S&K tidak boleh hilang")

    def test_index_menautkan_semua_topik_dan_callback_aman(self):
        _text, markup = guide_index()
        cbs = self._callbacks(markup)
        for key in GUIDE_TOPICS:
            self.assertIn(f"guide_{key}", cbs)
        self.assertIn("menu_back", cbs)
        for key in GUIDE_TOPICS:
            for cb in self._callbacks(guide_topic(key)[1]):
                self.assertLessEqual(len(cb.encode()), 64, cb)

    def test_navigasi_dan_jalan_pulang(self):
        keys = list(GUIDE_TOPICS)
        first = self._callbacks(guide_topic(keys[0])[1])
        last = self._callbacks(guide_topic(keys[-1])[1])
        self.assertNotIn(f"guide_{keys[-1]}", first)
        self.assertIn(f"guide_{keys[1]}", first)
        self.assertIn(f"guide_{keys[-2]}", last)
        for cbs in (first, last):
            self.assertIn("menu_guide", cbs)
            self.assertIn("menu_back", cbs)
        self.assertIsNone(guide_topic("tidak-ada"))

    async def _route(self, data):
        query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                                from_user=SimpleNamespace(id=999), message=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=999),
                                 effective_chat=SimpleNamespace(id=999))
        await start_mod.menu_callback_handler(update, SimpleNamespace(user_data={}, bot=AsyncMock()))
        return query

    async def test_menu_guide_dirutekan(self):
        query = await self._route("menu_guide")
        kwargs = query.edit_message_text.await_args.kwargs
        self.assertEqual(kwargs["text"], GUIDE_INDEX_TEXT)
        self.assertEqual(kwargs["parse_mode"], "HTML")

    async def test_setiap_topik_dirutekan(self):
        for key, (_label, text) in GUIDE_TOPICS.items():
            with self.subTest(topik=key):
                query = await self._route(f"guide_{key}")
                self.assertEqual(query.edit_message_text.await_args.kwargs["text"], text)

    async def test_topik_tidak_dikenal(self):
        query = await self._route("guide_xyz")
        query.edit_message_text.assert_not_called()
        # Telegram hanya menampilkan jawaban PERTAMA; jawaban penutup router sesudahnya ditolak.
        self.assertTrue(query.answer.await_args_list[0].kwargs.get("show_alert"))


if __name__ == "__main__":
    unittest.main()
