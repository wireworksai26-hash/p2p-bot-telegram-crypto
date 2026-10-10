"""Pause transaksi & jadwal maintenance / update sistem + pengingat otomatis ke user."""
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from telegram import Chat, Message, Update, User as TgUser  # noqa: E402
from telegram.ext import ApplicationHandlerStop, ConversationHandler  # noqa: E402

import database.models  # noqa: F401,E402
from config.settings import settings  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import MaintenanceWindow, TopupOrder  # noqa: E402
from services import system_pause as sp  # noqa: E402

ADMIN, USER = 1, 5
# 10 Okt 2026 10:00 WIB
NOW = datetime(2026, 10, 10, 3, 0)


class _DB(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.pin = patch.object(settings, "ADMIN_CHAT_IDS", [ADMIN])
        self.pin.start()

    def tearDown(self):
        self.pin.stop()
        Base.metadata.drop_all(bind=engine)

    def _row(self):
        db = SessionLocal()
        try:
            return db.query(MaintenanceWindow).order_by(MaintenanceWindow.id.desc()).first()
        finally:
            db.close()


class ParseJadwal(unittest.TestCase):
    def test_jam_durasi_catatan_dalam_wib(self):
        start, end, note = sp.parse_schedule("22:00 60m Update sistem", now=NOW)
        self.assertEqual(start, datetime(2026, 10, 10, 15, 0))   # 22:00 WIB
        self.assertEqual(end - start, timedelta(minutes=60))
        self.assertEqual(note, "Update sistem")

    def test_jam_lewat_berarti_besok_dan_durasi_jam(self):
        start, end, _ = sp.parse_schedule("08:00 2j", now=NOW)
        self.assertEqual(start, datetime(2026, 10, 11, 1, 0))
        self.assertEqual(end - start, timedelta(hours=2))

    def test_dengan_tanggal_tanpa_durasi(self):
        start, end, note = sp.parse_schedule("12/10 01:00 Migrasi server", now=NOW)
        self.assertEqual(start, datetime(2026, 10, 11, 18, 0))
        self.assertIsNone(end)
        self.assertEqual(note, "Migrasi server")

    def test_format_salah_dan_tanggal_lewat_ditolak(self):
        for text in ("besok malam", "25:00 60m", "01/10 22:00 60m", ""):
            with self.subTest(text=text), self.assertRaises(sp.PauseError):
                sp.parse_schedule(text, now=NOW)


class StatusPause(_DB):
    def test_pause_hanya_berlaku_di_dalam_jendela_waktu(self):
        start = NOW + timedelta(hours=2)
        sp.schedule(start, start + timedelta(hours=1), "Update", ADMIN, now=NOW)
        self.assertIsNone(sp.active_pause(now=NOW))
        self.assertIsNotNone(sp.active_pause(now=start + timedelta(minutes=5)))
        self.assertIsNone(sp.active_pause(now=start + timedelta(hours=1, seconds=1)))

    def test_hanya_satu_jadwal_aktif(self):
        sp.schedule(NOW + timedelta(hours=2), None, "", ADMIN, now=NOW)
        with self.assertRaises(sp.PauseError):
            sp.schedule(NOW + timedelta(hours=5), None, "", ADMIN, now=NOW)

    def test_lanjutkan_dan_batalkan(self):
        sp.pause_now(None, "darurat", ADMIN, now=NOW)
        self.assertIsNotNone(sp.active_pause(now=NOW + timedelta(hours=3)))
        self.assertIsNone(sp.cancel_scheduled(ADMIN, now=NOW), "pause yang berjalan bukan jadwal")
        self.assertIsNotNone(sp.resume_now(ADMIN, now=NOW + timedelta(minutes=1)))
        self.assertIsNone(sp.active_pause(now=NOW + timedelta(minutes=2)))


class JadwalOtomatis(_DB):
    async def _tick(self, now):
        with patch("services.system_pause.start_broadcast") as broadcast, \
             patch("bot.utils.telegram_utils.notify_admins", new=AsyncMock()) as notify:
            await sp.tick(object(), now=now)
        return broadcast, notify

    async def test_pengumuman_pengingat_mulai_dan_selesai(self):
        start = NOW + timedelta(hours=2)
        sp.schedule(start, start + timedelta(hours=1), "Update sistem", ADMIN, now=NOW)

        broadcast, _ = await self._tick(NOW)
        self.assertEqual(broadcast.call_count, 1)
        self.assertIn("Jadwal Maintenance", broadcast.call_args.args[1])
        broadcast, _ = await self._tick(NOW + timedelta(seconds=30))
        broadcast.assert_not_called()   # tidak dobel

        broadcast, _ = await self._tick(start - timedelta(minutes=55))
        self.assertIn("55 menit lagi", broadcast.call_args.args[1])
        broadcast, _ = await self._tick(start - timedelta(minutes=8))
        self.assertIn("8 menit lagi", broadcast.call_args.args[1])
        broadcast, _ = await self._tick(start - timedelta(minutes=5))
        broadcast.assert_not_called()

        broadcast, notify = await self._tick(start)
        self.assertEqual(self._row().status, "active")
        self.assertIn("Maintenance dimulai", notify.await_args.args[1])

        broadcast, notify = await self._tick(start + timedelta(hours=1))
        self.assertEqual(self._row().status, "done")
        self.assertIn("aktif kembali", broadcast.call_args.args[1])

    async def test_bot_hidup_mepet_jadwal_tidak_mengirim_pengingat_60_menit(self):
        start = NOW + timedelta(hours=2)
        sp.schedule(start, None, "", ADMIN, now=NOW)
        await self._tick(NOW)
        broadcast, _ = await self._tick(start - timedelta(minutes=5))
        self.assertEqual(broadcast.call_count, 1)
        self.assertIn("5 menit lagi", broadcast.call_args.args[1])
        broadcast, _ = await self._tick(start - timedelta(minutes=4))
        broadcast.assert_not_called()

    async def test_jadwal_dekat_tidak_mengulang_pengingat_yang_sudah_lewat(self):
        sp.schedule(NOW + timedelta(minutes=30), None, "", ADMIN, now=NOW)
        broadcast, _ = await self._tick(NOW)
        self.assertEqual(broadcast.call_count, 1, "hanya pengumuman; pengingat 60 menit sudah tercakup")

    async def test_pause_mendadak_tidak_dibroadcast(self):
        sp.pause_now(10, "", ADMIN, now=NOW)
        broadcast, notify = await self._tick(NOW + timedelta(minutes=11))
        broadcast.assert_not_called()
        self.assertEqual(self._row().status, "done")
        notify.assert_awaited()


class GerbangTransaksiBaru(_DB):
    def _update(self, data=None, text=None, user_id=USER):
        query = SimpleNamespace(data=data, answer=AsyncMock()) if data else None
        message = SimpleNamespace(text=text) if text else None
        return SimpleNamespace(callback_query=query, message=message,
                               effective_user=SimpleNamespace(id=user_id),
                               effective_chat=SimpleNamespace(id=user_id, type="private"))

    async def _gate(self, update):
        from bot.utils.maintenance_guard import _pause_gate
        context = SimpleNamespace(bot=AsyncMock())
        await _pause_gate(update, context)
        return context

    async def test_tombol_dan_perintah_transaksi_baru_dijeda(self):
        sp.pause_now(60, "Update sistem", ADMIN)
        for update in (self._update(data="menu_buy"), self._update(data="confirm_swap_order"),
                       self._update(data="topup_nom_50000"), self._update(text="/beli@TokoBot")):
            with self.subTest(update=update), self.assertRaises(ApplicationHandlerStop):
                context = SimpleNamespace(bot=AsyncMock())
                try:
                    from bot.utils.maintenance_guard import _pause_gate
                    await _pause_gate(update, context)
                finally:
                    sent = context.bot.send_message.await_args.kwargs["text"]
                    self.assertIn("maintenance", sent)
                    self.assertIn("Update sistem", sent)

    async def test_menu_lain_admin_dan_tanpa_pause_tetap_jalan(self):
        await self._gate(self._update(data="menu_buy"))            # tidak pause
        sp.pause_now(None, "", ADMIN)
        await self._gate(self._update(data="menu_history"))        # bukan transaksi baru
        await self._gate(self._update(data="txhash_pick_ORD-1"))   # order berjalan
        await self._gate(self._update(data="menu_buy", user_id=ADMIN))

    async def test_maintenance_gate_memanggil_pause_untuk_update_asli(self):
        from bot.utils.maintenance_guard import maintenance_gate
        sp.pause_now(None, "", ADMIN)
        msg = Message(message_id=1, date=datetime.utcnow(), chat=Chat(id=USER, type="private"),
                      from_user=TgUser(id=USER, first_name="U", is_bot=False), text="/jual")
        context = SimpleNamespace(bot=AsyncMock(), user_data={})
        with self.assertRaises(ApplicationHandlerStop):
            await maintenance_gate(Update(update_id=1, message=msg), context)

    async def test_topup_nominal_ketik_sendiri_tidak_membuat_invoice(self):
        from bot.handlers.balance import generate_and_send_qris
        sp.pause_now(None, "", ADMIN)
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=USER), callback_query=None, message=message)
        result = await generate_and_send_qris(update, SimpleNamespace(user_data={}, bot=AsyncMock()), 50000)
        self.assertEqual(result, ConversationHandler.END)
        self.assertIn("maintenance", message.reply_text.await_args.args[0])
        db = SessionLocal()
        try:
            self.assertEqual(db.query(TopupOrder).count(), 0)
        finally:
            db.close()


class PanelAdmin(_DB):
    def _query(self, data):
        return SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())

    async def test_jadwalkan_lewat_panel_pratinjau_lalu_umumkan(self):
        from bot.handlers import admin_pause as ap
        context = SimpleNamespace(user_data={}, bot=AsyncMock())
        await ap.handle_pause_callback(self._query("admin_panel_pause_sched"), "admin_panel_pause_sched",
                                       ADMIN, context)
        self.assertTrue(context.user_data.get(ap.AWAITING_FLAG))

        message = SimpleNamespace(text="23:00 45m Update sistem", reply_text=AsyncMock())
        handled = await ap.handle_schedule_text(SimpleNamespace(message=message), context)
        self.assertTrue(handled)
        preview = message.reply_text.await_args.args[0]
        self.assertIn("PRATINJAU", preview)
        self.assertIn("Update sistem", preview)
        self.assertIsNone(self._row(), "belum dijadwalkan sebelum dikonfirmasi")

        with patch("services.system_pause.start_broadcast") as broadcast, \
             patch("bot.utils.telegram_utils.notify_admins", new=AsyncMock()):
            await ap.handle_pause_callback(self._query("admin_panel_pause_ok"), "admin_panel_pause_ok",
                                           ADMIN, context)
        self.assertEqual(self._row().status, "scheduled")
        self.assertIn("Jadwal Maintenance", broadcast.call_args.args[1])

    async def test_perintah_pause_now_dan_off(self):
        from bot.handlers import admin_pause as ap
        reply = AsyncMock()
        update = SimpleNamespace(message=SimpleNamespace(reply_text=reply), effective_user=SimpleNamespace(id=ADMIN))
        with patch("bot.utils.telegram_utils.notify_admins", new=AsyncMock()):
            await ap.pause_command_handler(update, SimpleNamespace(args=["now", "45m", "Deploy"], bot=AsyncMock()))
            info = sp.active_pause()
            self.assertEqual(info.note, "Deploy")
            self.assertAlmostEqual((info.end_at - info.start_at).total_seconds(), 45 * 60, delta=5)
            await ap.pause_command_handler(update, SimpleNamespace(args=["off"], bot=AsyncMock()))
        self.assertIsNone(sp.active_pause())
        self.assertIn("dibuka kembali", reply.await_args.args[0])

    async def test_batalkan_jadwal_yang_sudah_diumumkan_mengabari_user(self):
        from bot.handlers import admin_pause as ap
        sp.schedule(datetime.utcnow() + timedelta(hours=3), None, "", ADMIN)
        with patch("services.system_pause.start_broadcast") as broadcast, \
             patch("bot.utils.telegram_utils.notify_admins", new=AsyncMock()):
            await ap.handle_pause_callback(self._query("admin_panel_pause_cancelok"),
                                           "admin_panel_pause_cancelok", ADMIN,
                                           SimpleNamespace(user_data={}, bot=AsyncMock()))
        self.assertEqual(self._row().status, "cancelled")
        self.assertIn("dibatalkan", broadcast.call_args.args[1])

    def test_tombol_panel_ada_di_dashboard(self):
        from bot.handlers.admin import get_admin_dashboard_keyboard
        callbacks = [b.callback_data for row in get_admin_dashboard_keyboard().inline_keyboard for b in row]
        self.assertIn("admin_panel_pause", callbacks)


if __name__ == "__main__":
    unittest.main()
