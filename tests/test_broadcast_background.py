"""/broadcast tidak boleh menahan bot.

Bug produksi: loop pengiriman di-await di dalam handler. Bot memproses update satu per satu,
jadi selama broadcast ke ribuan user (±0,6 detik per user) bot tidak melayani siapa pun dan
terlihat mati.
"""
import asyncio
import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import User  # noqa: E402
import bot.handlers.admin as admin_mod  # noqa: E402


def _message(text="/broadcast Halo semua"):
    msg = AsyncMock()
    msg.text = text
    msg.photo = []
    msg.caption = None
    msg.reply_to_message = None
    msg.document = None
    return msg


class BroadcastLatarBelakang(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([User(telegram_id=i, username=f"u{i}", is_banned=False, balance_idr=Decimal("0"))
                    for i in (11, 12, 13)])
        db.commit()
        db.close()
        admin_mod._broadcast_task = None
        self._admin = patch.object(admin_mod, "is_admin", return_value=True)
        self._admin.start()

    def tearDown(self):
        self._admin.stop()
        Base.metadata.drop_all(bind=engine)

    async def test_handler_selesai_sebelum_broadcast_selesai(self):
        release = asyncio.Event()
        sent = []

        async def slow_send(bot, telegram_id, *args, **kwargs):
            await release.wait()
            sent.append(telegram_id)
            return True

        message = _message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=message)
        with patch.object(admin_mod, "_send_broadcast_to_user", new=slow_send):
            await asyncio.wait_for(admin_mod.broadcast_handler(update, SimpleNamespace(bot=AsyncMock(), args=[])),
                                   timeout=2)
            self.assertEqual(sent, [], "handler tidak boleh menunggu pengiriman")
            self.assertIn("latar belakang", message.reply_text.await_args.args[0])

            # Broadcast kedua saat yang pertama masih jalan ditolak.
            second = _message("/broadcast Lagi")
            await admin_mod.broadcast_handler(SimpleNamespace(effective_user=SimpleNamespace(id=1), message=second),
                                              SimpleNamespace(bot=AsyncMock(), args=[]))
            self.assertIn("masih berjalan", second.reply_text.await_args.args[0])

            release.set()
            await admin_mod._broadcast_task
        self.assertEqual(sorted(sent), [11, 12, 13])
        self.assertIn("LAPORAN BROADCAST SELESAI", message.reply_text.await_args.args[0])

    async def test_pakai_create_task_application_bila_tersedia(self):
        created = []

        def create_task(coro):
            task = asyncio.ensure_future(coro)
            created.append(task)
            return task

        message = _message()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=message)
        context = SimpleNamespace(bot=AsyncMock(), args=[], application=SimpleNamespace(create_task=create_task))
        with patch.object(admin_mod, "_send_broadcast_to_user", new=AsyncMock(return_value=True)):
            await admin_mod.broadcast_handler(update, context)
            await created[0]
        self.assertEqual(len(created), 1)
        self.assertIn("LAPORAN BROADCAST SELESAI", message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
