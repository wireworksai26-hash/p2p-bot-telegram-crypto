"""Notifikasi Stars & Premium: topik grup "stars" (fallback topik "beli") dan testimoni ke channel setelah selesai."""
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1", "DATABASE_URL": "sqlite:///:memory:", "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "1,2", "EVM_WALLET_ADDRESS": "0x" + "1" * 40, "EVM_PRIVATE_KEY": "",
})

import database.models  # noqa: E402,F401
from database import stars_crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import NotificationTarget, User  # noqa: E402
from bot.utils import telegram_utils as TU  # noqa: E402
from services import testimony_service as T  # noqa: E402

USER = 9300


class _Db(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, username="budi_santoso", full_name="Budi", balance_idr=Decimal("1000000")))
        self.db.commit()
        T._bot_username_cache = None

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)


class TestJenisNotifikasi(_Db):
    def test_stars_dikenali_dan_premium_sama_dengan_stars(self):
        self.assertIn("stars", TU.KIND_RESMI)
        self.assertEqual(TU.normalisasi_kind("stars"), "stars")
        self.assertEqual(TU.normalisasi_kind("premium"), "stars")

    def test_stars_belum_dipasang_ikut_topik_beli(self):
        self.assertIsNone(TU._target_row("stars"))
        self.db.add(NotificationTarget(kind="beli", chat_id="-100111", thread_id="5", title="Beli"))
        self.db.commit()
        row = TU._target_row("stars")
        self.assertEqual((row.kind, row.chat_id, row.thread_id), ("beli", "-100111", "5"))

    def test_topik_stars_dipakai_bila_sudah_dipasang(self):
        self.db.add(NotificationTarget(kind="beli", chat_id="-100111", thread_id="5", title="Beli"))
        self.db.add(NotificationTarget(kind="stars", chat_id="-100111", thread_id="9", title="Stars"))
        self.db.commit()
        self.assertEqual(TU.admin_notification_targets("stars"), [("-100111", 9)])
        self.assertEqual(TU.admin_notification_targets("beli"), [("-100111", 5)])

    def test_jenis_lain_tidak_jatuh_ke_topik_beli(self):
        self.db.add(NotificationTarget(kind="beli", chat_id="-100111", thread_id="5", title="Beli"))
        self.db.commit()
        self.assertIsNone(TU._target_row("jual"))


class TestTestimoniChannel(_Db):
    def test_format_stars_menyensor_user_dan_tanpa_tujuan(self):
        text = T.format_stars_testimony("budi_santoso", USER, "stars", "1.000 Stars", 278_500, "GOPAY_QRIS", "TokoKoinID_Bot")
        self.assertIn("Telegram Stars Berhasil Dikirim", text)
        self.assertIn("1.000 Stars", text)
        self.assertIn("Rp 278.500", text)
        self.assertIn("Qris", text)
        self.assertNotIn("budi_santoso", text)
        self.assertIn("@TokoKoinID_Bot", text)

    def test_format_premium(self):
        text = T.format_stars_testimony(None, USER, "premium", "3 Bulan", 228_000, "BOT_BALANCE")
        self.assertIn("Telegram Premium Berhasil Dikirim", text)
        self.assertIn("Saldo Bot", text)

    async def test_post_ke_channel_testimoni(self):
        order = stars_crud.create_balance_order(self.db, USER, "stars", 100, "teman_ku")
        bot = SimpleNamespace(send_message=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(username="TokoKoinID_Bot")))
        with patch.dict(os.environ, {"TESTIMONY_CHANNEL": "@TokoKoinID"}):
            self.assertTrue(await T.post_stars_testimony(bot, order, self.db))
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], "@TokoKoinID")
        self.assertIn("100 Stars", kwargs["text"])
        self.assertNotIn("teman_ku", kwargs["text"])

    async def test_gagal_kirim_tidak_melempar_error(self):
        order = stars_crud.create_balance_order(self.db, USER, "stars", 100, "teman_ku")
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("channel salah")),
                              get_me=AsyncMock(return_value=SimpleNamespace(username="TokoKoinID_Bot")))
        self.assertFalse(await T.post_stars_testimony(bot, order, self.db))

    async def test_admin_sudah_dikirim_memposting_testimoni_tolak_tidak(self):
        from bot.handlers import stars as H
        for action, expect_post in (("ok", True), ("no", False)):
            order = stars_crud.create_balance_order(self.db, USER, "premium", 3, "teman_ku")
            query = SimpleNamespace(
                data=f"admin_stp_{action}_{order.order_id}", from_user=SimpleNamespace(id=1),
                answer=AsyncMock(), edit_message_text=AsyncMock(),
                message=SimpleNamespace(text_html="kartu", reply_text=AsyncMock()))
            up = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1))
            ctx = SimpleNamespace(bot=AsyncMock(), user_data={})
            post = AsyncMock(return_value=True)
            with patch("bot.handlers.admin.is_admin", return_value=True), \
                 patch("bot.utils.telegram_utils.safe_send_message", new=AsyncMock()), \
                 patch.object(H, "notify_admins", new=AsyncMock()), \
                 patch("services.testimony_service.post_stars_testimony", new=post):
                await H.admin_stars_callback(up, ctx)
            self.assertEqual(post.await_count, 1 if expect_post else 0, action)


if __name__ == "__main__":
    unittest.main()
