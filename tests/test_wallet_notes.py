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
    def test_catatan_kirim_teks_sesuai_permintaan(self):
        text = _plain(exchange_send_note())
        for fragment in (
            "Jangan kirim dari akun Exchange seperti",
            "Gate.io", "Bybit", "Bitget", "Cwallet", "dan sejenisnya",
            "Jumlah yang sampai bisa kurang dan dikhawatirkan tidak terdeteksi oleh bot",
            "Kirim dari Alamat Wallet Web3 Pribadi",
        ):
            self.assertIn(fragment, text)

    def test_catatan_kirim_logo_exchange_tampil_bila_id_terdaftar(self):
        from unittest.mock import patch
        from bot.utils import wallet_notes
        ids = {"EXCH_GATE": "111", "EXCH_BYBIT": "222", "EXCH_BITGET": "333", "EXCH_CWALLET": "444"}
        with patch.dict(wallet_notes.CUSTOM_EMOJI_IDS, ids):
            note = exchange_send_note()
        for emoji_id, name in (("111", "Gate.io"), ("222", "Bybit"), ("333", "Bitget"), ("444", "Cwallet")):
            self.assertRegex(note, rf'<tg-emoji emoji-id="{emoji_id}">[^<]*</tg-emoji> {name}')

    def test_catatan_kirim_tanpa_id_logo_hanya_nama_tanpa_emoji_kosong(self):
        from unittest.mock import patch
        from bot.utils import wallet_notes
        tanpa_logo = {k: v for k, v in wallet_notes.CUSTOM_EMOJI_IDS.items() if not k.startswith("EXCH_")}
        with patch.object(wallet_notes, "CUSTOM_EMOJI_IDS", tanpa_logo):
            note = exchange_send_note()
        self.assertNotIn("<tg-emoji", note)
        self.assertIn("Gate.io, Bybit, Bitget, Cwallet", note)

    def test_logo_exchange_sudah_terdaftar_secara_bawaan(self):
        note = exchange_send_note()
        self.assertEqual(note.count("<tg-emoji"), 4)
        from bot.utils.emojis import CUSTOM_EMOJI_IDS
        for key in ("EXCH_GATE", "EXCH_BYBIT", "EXCH_BITGET", "EXCH_CWALLET"):
            self.assertRegex(CUSTOM_EMOJI_IDS[key], r"^\d{15,}$")

    def test_catatan_terima_menyebut_exchange_dan_cwallet(self):
        text = _plain(exchange_receive_note())
        self.assertIn("Cwallet", text)
        self.assertIn("exchange", text.lower())

    def test_catatan_terima_menyebut_exchange_dan_cwallet(self):
        text = _plain(exchange_receive_note())
        self.assertIn("Cwallet", text)
        self.assertIn("exchange", text.lower())

    def test_catatan_terima_ikon_bsc_ada_sebelum_kata_cwallet(self):
        before = exchange_receive_note().split("Cwallet")[0]
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
