"""Input rekening Jual: aman untuk HTML, format tanpa koma dipahami, pemilik rekening tidak ditebak.

Dulu:
- "Budi <3" / "A & B" dimasukkan mentah ke ringkasan HTML -> Telegram menolak pesan.
- "BCA 1234567890 Budi" (tanpa koma) disimpan sebagai bank "Bank Lokal" dengan
  atas nama = nama Telegram user -> admin bisa mentransfer ke nama yang salah, dan
  cek kunci rekening (rekening milik user lain) dilewati.
"""
import json
import os
import unittest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from tests._settings_guard import e2e_setup, e2e_teardown  # noqa: E402
from tests import test_e2e_bot_flows as _e2e  # noqa: E402
from database.connection import SessionLocal  # noqa: E402
from database.models import Order  # noqa: E402
from bot.handlers.sell import parse_bank_text  # noqa: E402

SELLER, OWNER = 93001, 93002


class ParseBank(unittest.TestCase):
    def test_berbagai_format(self):
        cases = {
            "BCA, 1234567890, Budi Santoso": ("BCA", "1234567890", "Budi Santoso"),
            "BCA 1234567890 Budi Santoso": ("BCA", "1234567890", "Budi Santoso"),
            "Bank Mandiri - 123-456-7890 - Joko": ("Bank Mandiri", "123-456-7890", "Joko"),
            "DANA 0812 3456 7890 a/n Siti": ("DANA", "0812 3456 7890", "Siti"),
            "BRI\n0012345678901\nAni": ("BRI", "0012345678901", "Ani"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_bank_text(text), expected)

    def test_tanpa_nomor_atau_tanpa_nama_ditolak(self):
        for text in ("BCA Budi Santoso", "BCA 1234567890", "1234567890 Budi"):
            with self.subTest(text=text):
                self.assertIsNone(parse_bank_text(text))


class SellBankE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await e2e_setup(self)

    async def asyncTearDown(self):
        await e2e_teardown(self)

    _user = _e2e.BotFlowE2E._user
    _dispatch = _e2e.BotFlowE2E._dispatch
    say = _e2e.BotFlowE2E.say
    tap = _e2e.BotFlowE2E.tap
    _sell_until_bank_step = _e2e.BotFlowE2E._sell_until_bank_step

    def _raw_last_texts(self, start):
        return [p.get("text") or "" for e, p in _e2e.FakeTelegram.calls[start:]
                if e in ("sendMessage", "editMessageText")]

    async def test_nama_dengan_karakter_html_di_escape(self):
        await self._sell_until_bank_step(SELLER)
        start = len(_e2e.FakeTelegram.calls)
        await self.say(SELLER, "BCA, 1234567890, Budi <3 & Co")
        raw = "\n".join(self._raw_last_texts(start))
        self.assertIn("RINGKASAN ORDER PENJUALAN", raw)
        self.assertIn("Budi &lt;3 &amp; Co", raw)
        self.assertNotIn("Budi <3", raw)

    async def test_format_tanpa_koma_tidak_jadi_bank_lokal(self):
        await self._sell_until_bank_step(SELLER)
        await self.say(SELLER, "BCA 1234567890 Budi Santoso")
        await self.tap(SELLER, "sell_confirm", from_screen=False)
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.telegram_id == SELLER).one()
        finally:
            db.close()
        self.assertEqual(order.buyer_wallet, "BCA | 1234567890 | Budi Santoso")

    async def test_tanpa_nama_pemilik_diminta_ulang(self):
        await self._sell_until_bank_step(SELLER)
        shown = await self.say(SELLER, "BCA 1234567890")
        self.assertNotIn("RINGKASAN ORDER PENJUALAN", shown)
        self.assertIn("Atas Nama", shown)

    async def test_rekening_terkunci_tetap_dicek_tanpa_koma(self):
        db = SessionLocal()
        try:
            db.add(Order(order_id="ORD-OWN", telegram_id=OWNER, order_type="sell", crypto_symbol="USDT",
                         network="BSC", crypto_amount=1, price_per_unit=1, nominal_idr=1, fee_idr=0,
                         total_idr=1, buyer_wallet="BCA | 1234567890 | Owner", status="completed"))
            db.commit()
        finally:
            db.close()
        await self._sell_until_bank_step(SELLER)
        shown = await self.say(SELLER, "BCA 1234567890 Pencuri")
        self.assertIn("Duplikat Rekening", shown)


if __name__ == "__main__":
    unittest.main()
