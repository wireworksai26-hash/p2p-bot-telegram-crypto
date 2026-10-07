"""B8 — tombol bayar admin tidak boleh membayar dua kali.

- "Ya, Kirim Saldo Sekarang!" ditekan dua kali (atau salinan tombol lama/admin lain)
  dulu mengkredit dua kali; callback juga bisa dirakit dengan nominal apa pun.
- /bulkcredit dengan ID ganda mengkredit user yang sama berkali-kali.
- Undian acak ditekan dua kali membagikan hadiah dua kali.
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from tests._settings_guard import e2e_setup, e2e_teardown  # noqa: E402
from tests import test_e2e_bot_flows as _e2e  # noqa: E402
from database import crud  # noqa: E402
from database.connection import SessionLocal  # noqa: E402
from database.models import Campaign, CampaignActionLock  # noqa: E402


class AdminDoublePay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await e2e_setup(self)

    async def asyncTearDown(self):
        await e2e_teardown(self)

    _user = _e2e.BotFlowE2E._user
    _dispatch = _e2e.BotFlowE2E._dispatch
    say = _e2e.BotFlowE2E.say
    tap = _e2e.BotFlowE2E.tap
    _seed_admin_world = _e2e.BotFlowE2E._seed_admin_world
    _balance = _e2e.BotFlowE2E._balance
    ADMIN_UID, GIFT_A, GIFT_B = _e2e.BotFlowE2E.ADMIN_UID, _e2e.BotFlowE2E.GIFT_A, _e2e.BotFlowE2E.GIFT_B

    def _issue_token(self, action, payload, message_id=1):
        db = SessionLocal()
        try:
            return crud.issue_admin_action_token(
                db, self.ADMIN_UID, action, payload,
                chat_id=self.ADMIN_UID, message_id=message_id,
            )
        finally:
            db.close()

    def _confirm_button(self):
        return next(b["callback_data"] for b in self.last_buttons[self.ADMIN_UID]
                    if b.get("callback_data", "").startswith("admin_send_bal_confirm_"))

    async def _to_confirm_screen(self, amount=50000):
        await self.tap(self.ADMIN_UID, f"admin_send_bal_user_{self.GIFT_A}", from_screen=False)
        await self.tap(self.ADMIN_UID, f"admin_send_bal_amt_{amount}", from_screen=False)
        return self._confirm_button()

    async def test_kirim_saldo_tekan_dua_kali_hanya_sekali(self):
        self._seed_admin_world(treasury=0)
        data = await self._to_confirm_screen()
        await self.tap(self.ADMIN_UID, data, from_screen=False)
        await self.tap(self.ADMIN_UID, data, from_screen=False)
        self.assertEqual(self._balance(self.GIFT_A), 50000)

    async def test_kontrol_kirim_saldo_lewat_layar_konfirmasi(self):
        self._seed_admin_world(treasury=0)
        data = await self._to_confirm_screen()
        shown = await self.tap(self.ADMIN_UID, data, from_screen=False)
        self.assertIn("Nominal Terkirim", shown)
        self.assertEqual(self._balance(self.GIFT_A), 50000)

    async def test_callback_rakitan_tanpa_layar_konfirmasi_ditolak(self):
        self._seed_admin_world(treasury=0)
        await self.tap(self.ADMIN_UID, f"admin_send_bal_confirm_{self.GIFT_A}_9000000", from_screen=False)
        self.assertEqual(self._balance(self.GIFT_A), 0)

    def test_token_terikat_admin_payload_dan_pesan_lalu_hanya_bisa_diklaim_sekali(self):
        db = SessionLocal()
        try:
            token = crud.issue_admin_action_token(
                db, self.ADMIN_UID, "send_balance", f"{self.GIFT_A}:50000",
                chat_id=self.ADMIN_UID, message_id=1,
            )
            self.assertIsNone(crud.claim_admin_action_token(
                db, token, self.ADMIN_UID + 1, "send_balance", chat_id=self.ADMIN_UID, message_id=1,
            ))
            self.assertIsNone(crud.claim_admin_action_token(
                db, token, self.ADMIN_UID, "send_balance", chat_id=self.ADMIN_UID, message_id=2,
            ))
            self.assertEqual(crud.claim_admin_action_token(
                db, token, self.ADMIN_UID, "send_balance",
                payload=f"{self.GIFT_A}:50000", chat_id=self.ADMIN_UID, message_id=1,
            ), f"{self.GIFT_A}:50000")
            db.commit()
            self.assertIsNone(crud.claim_admin_action_token(
                db, token, self.ADMIN_UID, "send_balance", chat_id=self.ADMIN_UID, message_id=1,
            ))
        finally:
            db.close()

    async def test_bulkcredit_id_ganda_dikredit_sekali(self):
        self._seed_admin_world(treasury=0)
        with patch("bot.handlers.admin.is_admin", side_effect=lambda uid: uid == self.ADMIN_UID):
            await self.say(self.ADMIN_UID, f"/bulkcredit 10000 {self.GIFT_A} {self.GIFT_A} {self.GIFT_B}")
        self.assertEqual(self._balance(self.GIFT_A), 10000)
        self.assertEqual(self._balance(self.GIFT_B), 10000)

    async def test_undian_tekan_dua_kali_hanya_sekali(self):
        self._seed_admin_world(treasury=0)
        with patch("services.campaign_service.crud.get_random_winners",
                   side_effect=lambda *a, **k: [{"telegram_id": self.GIFT_A, "username": "budi",
                                                    "full_name": "Budi"}]):
            token = self._issue_token("random_draw", "ALL|1|25000")
            data = f"admin_draw_exec_ALL_1_25000_{token}"
            await self.tap(self.ADMIN_UID, data, from_screen=False)
            db = SessionLocal()
            try:
                db.query(Campaign).filter(Campaign.template_type == "tpl_flash_random").update(
                    {Campaign.created_at: datetime.utcnow() - timedelta(minutes=3)},
                    synchronize_session=False,
                )
                db.query(CampaignActionLock).filter_by(action="random_draw").update(
                    {CampaignActionLock.locked_until: datetime.utcnow() - timedelta(seconds=1)},
                    synchronize_session=False,
                )
                db.commit()
            finally:
                db.close()
            # Token yang sama tetap tidak dapat dipakai lagi setelah cooldown berlalu.
            await self.tap(self.ADMIN_UID, data, from_screen=False)
        db = SessionLocal()
        try:
            runs = db.query(Campaign).filter(Campaign.template_type == "tpl_flash_random").count()
        finally:
            db.close()
        self.assertEqual(runs, 1)
        self.assertEqual(self._balance(self.GIFT_A), 25000)

    async def test_undian_aksi_lain_diblokir_lock_global(self):
        self._seed_admin_world(treasury=0)
        token_a = self._issue_token("random_draw", "ALL|1|25000", message_id=1)
        token_b = self._issue_token("random_draw", "ALL|1|25000", message_id=1)
        data_a = f"admin_draw_exec_ALL_1_25000_{token_a}"
        data_b = f"admin_draw_exec_ALL_1_25000_{token_b}"
        with patch("services.campaign_service.crud.get_random_winners",
                   side_effect=lambda *a, **k: [{"telegram_id": self.GIFT_A, "username": "budi",
                                                    "full_name": "Budi"}]):
            await self.tap(self.ADMIN_UID, data_a, from_screen=False)
            db = SessionLocal()
            try:
                db.query(Campaign).filter(Campaign.template_type == "tpl_flash_random").update(
                    {Campaign.created_at: datetime.utcnow() - timedelta(minutes=3)},
                    synchronize_session=False,
                )
                db.commit()
            finally:
                db.close()
            await self.tap(self.ADMIN_UID, data_b, from_screen=False)
        self.assertEqual(self._balance(self.GIFT_A), 25000)

    async def test_topup_kas_preset_tap_ganda_hanya_satu_kali(self):
        self._seed_admin_world(treasury=0)
        await self.tap(self.ADMIN_UID, "admin_panel_treasury", from_screen=False)
        data = next(
            b["callback_data"] for b in self.last_buttons[self.ADMIN_UID]
            if b.get("callback_data", "").startswith("admin_treasury_topup_100000_")
        )
        await self.tap(self.ADMIN_UID, data, from_screen=False)
        await self.tap(self.ADMIN_UID, data, from_screen=False)
        db = SessionLocal()
        try:
            self.assertEqual(crud.get_bot_treasury_balance(db), 100000)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
