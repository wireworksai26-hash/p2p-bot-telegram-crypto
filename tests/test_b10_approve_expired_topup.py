"""B10 — admin bisa menyetujui topup yang sudah EXPIRED (user bayar di menit terakhir).

Dulu claim_topup_success hanya menerima PENDING, jadi tombol Approve admin membalas
"sudah diproses sistem" dan uang user tidak pernah masuk saldo.
Tetap kebal tekan ganda: kredit hanya sekali.
"""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from config.settings import settings  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import TopupOrder, User  # noqa: E402
from bot.handlers import admin  # noqa: E402

USER = 96001


class ApproveExpired(unittest.IsolatedAsyncioTestCase):
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

    def _topup(self, status):
        db = SessionLocal()
        db.add(TopupOrder(topup_id="TOPUP-X", telegram_id=USER, amount_idr=50_137, mdr_idr=0, status=status,
                          created_at=datetime.utcnow() - timedelta(minutes=20),
                          expires_at=datetime.utcnow() - timedelta(minutes=5)))
        db.commit()
        db.close()

    async def _approve(self):
        query = MagicMock()
        query.data = "admin_approve_topup_TOPUP-X"
        query.from_user = SimpleNamespace(id=1)
        query.answer = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.message = SimpleNamespace(caption="x")
        with patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()):
            await admin.admin_approve_topup_callback(
                SimpleNamespace(callback_query=query, effective_user=query.from_user),
                SimpleNamespace(bot=AsyncMock()))

    def _state(self):
        db = SessionLocal()
        try:
            return (db.query(TopupOrder).one().status,
                    Decimal(str(db.query(User).filter(User.telegram_id == USER).one().balance_idr)))
        finally:
            db.close()

    async def test_topup_expired_bisa_diapprove_sekali(self):
        self._topup("EXPIRED")
        await self._approve()
        await self._approve()
        self.assertEqual(self._state(), ("SUCCESS", Decimal("50137")))

    async def test_topup_dibatalkan_tidak_bisa_diapprove(self):
        self._topup("CANCELLED")
        await self._approve()
        self.assertEqual(self._state(), ("CANCELLED", Decimal("0")))


if __name__ == "__main__":
    unittest.main()
