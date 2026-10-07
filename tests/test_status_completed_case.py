"""Status order "completed" vs "COMPLETED".

Beli/Jual menyimpan `completed` (huruf kecil) lewat crud.update_order_status, sedangkan Convert
(detector) menyimpan `COMPLETED`. Query laporan yang mencari `completed` persis melewatkan
Convert: /postlasttesti tidak memposting Convert dan statistik admin tidak menghitungnya.
"""
import asyncio
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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

from config.settings import settings
from database import crud
from database.connection import Base, SessionLocal, engine
from database.models import Order
from services import testimony_service as ts


def _order(order_id, order_type, status, **extra):
    return Order(
        order_id=order_id, telegram_id=999, order_type=order_type, crypto_symbol="USDT", network="BSC",
        crypto_amount=Decimal("5"), price_per_unit=16000, nominal_idr=80000, fee_idr=2000, total_idr=80000,
        status=status, payout_tx_hash="0x" + "ab" * 32, **extra)


class TestStatusCompletedCase(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        ts._scheduled_order_ids.clear()
        db = SessionLocal()
        db.add(_order("ORD-BUY", "buy", "completed"))
        db.add(_order("ORD-SELL", "sell", "completed"))
        db.add(_order("ORD-SWAP", "swap", "COMPLETED", target_crypto_symbol="ETH", target_network="BASE"))
        db.add(_order("ORD-WAIT", "buy", "pending"))
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def test_hitungan_order_selesai_menyertakan_convert(self):
        db = SessionLocal()
        try:
            self.assertEqual(crud.get_completed_order_count(db), 3)
        finally:
            db.close()

    def test_statistik_admin_tidak_error_dan_menghitung_convert(self):
        from bot.handlers.admin import build_admin_stats_text
        db = SessionLocal()
        try:
            teks = build_admin_stats_text(db)
        finally:
            db.close()
        self.assertIsInstance(teks, str)
        self.assertIn("Convert", teks)

    def test_postlasttesti_memposting_convert_juga(self):
        from bot.handlers.admin import resend_recent_testimonies_command_handler

        async def _run():
            bot = AsyncMock()
            bot.get_me = AsyncMock(return_value=MagicMock(username="TokoKoinID_Bot"))
            bot.send_message = AsyncMock()
            update = SimpleNamespace(effective_user=SimpleNamespace(id=999), message=AsyncMock())
            context = SimpleNamespace(bot=bot, args=["10"])
            orig = list(getattr(settings, "ADMIN_CHAT_IDS", []))
            settings.ADMIN_CHAT_IDS = [999]
            try:
                await resend_recent_testimonies_command_handler(update, context)
            finally:
                settings.ADMIN_CHAT_IDS = orig
            return bot

        bot = asyncio.run(_run())
        posted = " ".join(c.kwargs["text"] for c in bot.send_message.await_args_list)
        self.assertEqual(bot.send_message.await_count, 3, "buy + sell + convert; order pending tidak ikut")
        self.assertIn("Swap", posted)
        self.assertIn("Beli", posted)
        self.assertIn("Jual", posted)


if __name__ == "__main__":
    unittest.main()
