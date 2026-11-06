"""Teks bot yang diminimalkan: ringkasan Beli berdropdown, QRIS ringkas (koin, bukan Rupiah), tanpa baris TX."""
import io
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1", "DATABASE_URL": "sqlite:///:memory:", "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "1,2", "EVM_WALLET_ADDRESS": "0x" + "1" * 40, "EVM_PRIVATE_KEY": "",
})

import database.models  # noqa: E402,F401
from bot.handlers import buy  # noqa: E402
from bot.utils.messages import BUY_SUMMARY_CONFIRM_HINT, WALLET_LOCK_NOTE, collapsible  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order, User  # noqa: E402

USER = 9400
HASH = "0x" + "ab" * 32


class TestCollapsible(unittest.TestCase):
    def test_satu_blok_expandable(self):
        text = collapsible("baris satu", "\nbaris dua\n", "", None)
        self.assertEqual(text, "<blockquote expandable>baris satu\nbaris dua</blockquote>")
        self.assertEqual(collapsible("", None), "")


class _Db(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, username="budi_ku", balance_idr=Decimal("0")))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)


def _ud(nominal=100_000, fee=4_000, method="GOPAY_QRIS"):
    received = nominal - fee
    return {
        "buy_order_id": "ORD-TEKS", "buy_symbol": "USDT", "buy_network": "BSC", "buy_price_per_unit": 16_000,
        "buy_nominal_idr": nominal, "buy_total_idr": nominal, "buy_base_fee_idr": fee, "buy_fee_idr": fee,
        "buy_discount_amount": 0, "buy_discount_applied": False, "buy_discount_pct": None,
        "buy_received_idr": received, "buy_crypto_amount": received / 16_000, "buy_wallet": "0x" + "c" * 40,
        "buy_pay_method": method, "buy_mdr_idr": 0,
    }


class TestRingkasanBeli(_Db):
    async def test_catatan_dilipat_dalam_satu_dropdown(self):
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        update = SimpleNamespace(callback_query=query, message=None)
        await buy._render_buy_summary(update, SimpleNamespace(user_data=_ud()))
        text = query.edit_message_text.await_args.kwargs["text"]
        self.assertEqual(text.count("<blockquote expandable>"), 1)
        inside = text.split("<blockquote expandable>")[1]
        self.assertIn(BUY_SUMMARY_CONFIRM_HINT, inside)
        self.assertIn("Kode unik", inside)
        self.assertIn(WALLET_LOCK_NOTE, inside)
        outside = text.split("<blockquote expandable>")[0]
        self.assertNotIn("Silakan klik tombol", outside)
        self.assertNotIn("Penting", outside)
        # info inti tetap terlihat di luar dropdown
        for part in ("Metode Pembayaran", "Wallet Penerima", "Nominal Bayar", "Fee Layanan"):
            self.assertIn(part, outside)
        self.assertLess(text.index("Wallet Penerima"), text.index("<blockquote expandable>"))


class TestQrisRingkas(_Db):
    async def test_caption_qris_tanpa_fee_koin_dalam_koin_dan_cara_bayar_dropdown(self):
        ud = _ud(nominal=80_000, fee=3_500)
        ud["buy_received_idr"] = 76_500
        ud["buy_crypto_amount"] = 76_500 / 16_000
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = SimpleNamespace(delete=AsyncMock(), reply_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=USER, name="Budi"))
        ctx = SimpleNamespace(user_data=ud, bot=AsyncMock())
        with patch("bot.handlers.buy.SessionLocal", side_effect=lambda: SessionLocal()), \
             patch("bot.handlers.buy.get_available_inventory", return_value=Decimal("999999")), \
             patch("bot.handlers.buy.notify_admins", new=AsyncMock()), \
             patch("services.qris_generator.get_qris_image_stream", return_value=io.BytesIO(b"qr")):
            await buy.handle_order_confirmation(update, ctx)
        caption = ctx.bot.send_photo.await_args.kwargs["caption"]
        self.assertNotIn("Fee Layanan", caption)
        self.assertNotIn("Nilai Koin Diterima", caption)
        self.assertIn("Kamu dapat", caption)
        self.assertIn("4.7812 USDT (BSC)", caption)
        self.assertNotIn("Rp 76.500", caption)
        self.assertEqual(caption.count("<blockquote expandable>"), 1)
        block = caption.split("<blockquote expandable>")[1]
        for part in ("Cara Bayar", "Scan QRIS", "Kode Unik pembayaran", "Catatan"):
            self.assertIn(part, block)
        self.assertLessEqual(len(caption), 1024)  # batas caption foto Telegram


class TestPesanSelesai(_Db):
    def _order(self):
        row = Order(order_id="ORD-DONE", telegram_id=USER, order_type="buy", crypto_symbol="USDT", network="BSC",
                    crypto_amount=Decimal("4.7813"), price_per_unit=16000, nominal_idr=80000, fee_idr=3500,
                    total_idr=80000, buyer_wallet="0x" + "c" * 40, payment_method="BOT_BALANCE", status="paid",
                    paid_at=datetime.utcnow())
        self.db.add(row)
        self.db.commit()
        return row

    async def _finalize(self, explorer_url):
        order = self._order()
        sent = AsyncMock()
        result = {"success": True, "tx_hash": HASH, "explorer_url": explorer_url}
        with patch("bot.handlers.buy.reserve_order_inventory", return_value=True), \
             patch("services.payout_service.send_order_payout", new=AsyncMock(return_value=result)), \
             patch("bot.handlers.buy.safe_send_message", new=sent), \
             patch("services.testimony_service.schedule_transaction_testimony", new=MagicMock()):
            await buy.finalize_gopay_buy_payment(self.db, order, bot=AsyncMock())
        for call in sent.await_args_list:
            text = call.args[2] if len(call.args) > 2 else call.kwargs.get("text", "")
            if "Crypto Terkirim" in text:
                return text
        self.fail("pesan Crypto Terkirim tidak terkirim")

    async def test_tanpa_baris_tx_bila_ada_tautan_explorer(self):
        text = await self._finalize("https://bscscan.com/tx/" + HASH)
        self.assertNotIn("TX:", text)
        self.assertNotIn(f"<code>{HASH}</code>", text)  # hash cukup lewat tautan explorer
        self.assertIn("Lihat di Explorer", text)

    async def test_hash_tetap_tampil_bila_tidak_ada_tautan(self):
        text = await self._finalize("")
        self.assertIn("TX:", text)
        self.assertIn(HASH, text)

    async def test_watchdog_sama(self):
        from services import payout_watchdog as W
        order = SimpleNamespace(order_type="buy", order_id="ORD-W", crypto_amount=Decimal("1"), crypto_symbol="USDT",
                                network="BSC", payout_tx_hash=HASH, telegram_id=USER)
        sent = AsyncMock()
        with patch.object(W, "safe_send_message", new=sent):
            await W._notify_user(AsyncMock(), order, "https://bscscan.com/tx/" + HASH)
            with_link = sent.await_args.args[2]
            await W._notify_user(AsyncMock(), order, "")
            without = sent.await_args.args[2]
        self.assertNotIn("TX:", with_link)
        self.assertIn("Lihat di Explorer", with_link)
        self.assertIn("TX:", without)
        self.assertIn(HASH, without)


if __name__ == "__main__":
    unittest.main()
