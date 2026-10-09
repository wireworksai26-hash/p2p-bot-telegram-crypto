"""Catatan "jangan pakai alamat exchange (mis. Cwallet)" di layar Beli / Jual / Convert."""
import html
import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

from bot.utils.wallet_notes import exchange_receive_note, exchange_send_note  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(text))


class IsiCatatan(unittest.TestCase):
    def test_catatan_kirim_menyebut_exchange_dan_cwallet(self):
        text = _plain(exchange_send_note())
        self.assertIn("Cwallet", text)
        self.assertIn("exchange", text.lower())

    def test_catatan_terima_menyebut_exchange_dan_cwallet(self):
        text = _plain(exchange_receive_note())
        self.assertIn("Cwallet", text)
        self.assertIn("exchange", text.lower())

    def test_ikon_jaringan_bsc_ada_sebelum_kata_cwallet(self):
        for note in (exchange_send_note(), exchange_receive_note()):
            before = note.split("Cwallet")[0]
            self.assertTrue(
                "<tg-emoji" in before or "🟡" in before,
                "harus ada ikon BSC (emoji kustom atau 🟡) tepat sebelum kata Cwallet",
            )

    def test_tulisan_tidak_menyalin_kompetitor(self):
        for note in (exchange_send_note(), exchange_receive_note()):
            self.assertNotIn("addres exchange", note.lower())
            self.assertNotIn("cwallet / dll", note.lower())

    def test_html_seimbang(self):
        for note in (exchange_send_note(), exchange_receive_note()):
            for tag in ("b", "i", "tg-emoji"):
                self.assertEqual(note.count(f"<{tag}"), note.count(f"</{tag}>"), tag)


class TerpasangDiLayar(unittest.TestCase):
    def _src(self, rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_layar_setoran_jual_dan_convert_memakai_catatan_kirim(self):
        self.assertIn("exchange_send_note()", self._src("bot/handlers/sell.py"))
        self.assertIn("exchange_send_note()", self._src("bot/handlers/swap.py"))

    def test_input_alamat_beli_dan_convert_memakai_catatan_terima(self):
        self.assertIn("exchange_receive_note()", self._src("bot/handlers/buy.py"))
        self.assertIn("exchange_receive_note()", self._src("bot/handlers/swap.py"))

    def test_caption_foto_jual_tetap_di_bawah_batas_telegram(self):
        # Layar order Jual dikirim sebagai caption foto QR: maksimal 1024 karakter setelah HTML diurai.
        src = self._src("bot/handlers/sell.py")
        i = src.index("waiting_text = (")
        j = src.index("keyboard = [", i)
        from services import quote_guard
        ns = dict(
            order_id="ORD-20261909-867TVQV5", deposit_str="<code>0.00016000</code> ETH",
            network="ETHEREUM", hot_wallet="0x" + "C" * 40, SELL_QUOTE_MINUTES=10, symbol="USDT",
            quote_guard=quote_guard, exchange_send_note=exchange_send_note,
        )
        exec(src[i:j], ns)
        self.assertLessEqual(len(_plain(ns["waiting_text"])), 1024)
        self.assertIn("Cwallet", ns["waiting_text"])


if __name__ == "__main__":
    unittest.main()
