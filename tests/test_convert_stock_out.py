"""Convert: koin A user sudah masuk, tapi stok koin B habis saat akan dikirim.

Dulu pesan ke user hanya "Convert memerlukan bantuan admin. Silakan hubungi admin" dan alert admin
umum ("jangan kirim ulang jika broadcast belum pasti"), padahal belum ada koin yang terkirim.
Convert juga langsung menyerah tanpa sinkron ulang stok (alur Beli sudah sinkron ulang sekali).
"""
import os
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order, User, WalletBalance  # noqa: E402
from services.detector import deposit_detector  # noqa: E402

USER = 97101


def _swap():
    return Order(order_id="SWAP-SO-1", telegram_id=USER, order_type="swap", crypto_symbol="USDC",
                 network="BASE", crypto_amount=Decimal("5"), price_per_unit=16000, nominal_idr=80000,
                 fee_idr=0, total_idr=80000, target_crypto_symbol="BNB", target_network="BSC",
                 target_crypto_amount=Decimal("0.0123"), buyer_wallet="0x" + "c" * 40,
                 deposit_wallet="0x" + "1" * 40, status="CRYPTO_CONFIRMED", created_at=datetime.utcnow())


class ConvertStokHabis(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, username="husnun", full_name="Mas Husnun"))
        self.db.add(WalletBalance(network="BSC", symbol="BNB", balance=Decimal("0.001"), reserved_balance=0,
                                  address="0x" + "1" * 40, sync_status="OK", last_success_at=datetime.utcnow()))
        self.db.add(_swap())
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def _payout(self, sync_effect=None, payout_result=None):
        order = self.db.query(Order).filter_by(order_id="SWAP-SO-1").one()
        payout = AsyncMock(return_value=payout_result or {"success": True, "tx_hash": "0x" + "ee" * 32})
        with patch("services.wallet_sync.sync_wallet_balances", new=AsyncMock(side_effect=sync_effect)) as sync, \
             patch("services.payout_service.send_order_payout", new=payout), \
             patch("services.detector.notify_admins", new=AsyncMock()) as notify, \
             patch("services.detector.safe_send_message", new=AsyncMock()) as to_user, \
             patch("services.testimony_service.schedule_transaction_testimony"):
            await deposit_detector._execute_payout(self.db, order, bot_app=object())
        self.db.expire_all()
        return self.db.query(Order).filter_by(order_id="SWAP-SO-1").one(), sync, payout, notify, to_user

    async def test_stok_habis_admin_dan_user_dapat_pesan_jelas(self):
        order, sync, payout, notify, to_user = await self._payout()
        sync.assert_awaited_once()                       # cek ulang stok sebelum menyerah
        payout.assert_not_awaited()
        self.assertEqual(order.status, "manual_review")

        alert = notify.await_args.args[1]
        for needle in ("STOK KOIN TUJUAN HABIS", "Belum ada koin yang terkirim", "@husnun",
                       "CONVERT USDC (BASE) → BNB (BSC)", "Stok tersedia saat ini", "0.001"):
            self.assertIn(needle, alert)
        callbacks = [b.callback_data for row in notify.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(callbacks, ["admin_manual_sent_SWAP-SO-1"])

        user_msg = to_user.await_args.args[2]
        for needle in ("aman", "Stok BNB (BSC) sedang habis", "Tidak perlu mengirim ulang", "USDC"):
            self.assertIn(needle, user_msg)
        self.assertNotIn("hubungi admin", user_msg)

    async def test_restock_saat_cek_ulang_langsung_terkirim_otomatis(self):
        def restock(*_args, **_kwargs):
            db = SessionLocal()
            try:
                wallet = db.query(WalletBalance).filter_by(network="BSC", symbol="BNB").one()
                wallet.balance = Decimal("1")
                wallet.last_success_at = datetime.utcnow()
                db.commit()
            finally:
                db.close()

        order, sync, payout, notify, to_user = await self._payout(sync_effect=restock)
        payout.assert_awaited_once()
        self.assertEqual(order.status, "COMPLETED")
        self.assertIn("CONVERT SELESAI", notify.await_args.args[1])

    async def test_gagal_karena_sebab_lain_tetap_alert_umum_tapi_user_tenang(self):
        wallet = self.db.query(WalletBalance).filter_by(network="BSC", symbol="BNB").one()
        wallet.balance = Decimal("1")
        self.db.commit()
        order, sync, payout, notify, to_user = await self._payout(
            payout_result={"success": False, "tx_hash": "", "error_message": "RPC timeout"})
        sync.assert_not_awaited()
        self.assertEqual(order.status, "manual_review")
        alert = notify.await_args.args[1]
        self.assertIn("AUTO-PAYOUT GAGAL (CONVERT)", alert)
        self.assertNotIn("STOK KOIN TUJUAN HABIS", alert)
        user_msg = to_user.await_args.args[2]
        self.assertIn("aman", user_msg)
        self.assertIn("Tidak perlu mengirim ulang", user_msg)


if __name__ == "__main__":
    unittest.main()
