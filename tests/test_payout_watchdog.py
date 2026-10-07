"""Uji watchdog payout + persist hash saat broadcast-belum-receipt + explorer HYPE.

Regresi dari laporan client 30 Sep 2026 (USDT TON & ETH OPTIMISM dilaporkan
"gagal" padahal on-chain terkirim):
1. buy finalize WAJIB menyimpan payout_tx_hash walau receipt timeout
   ("Transaksi sudah dibroadcast, menunggu receipt").
2. Watchdog merekonsiliasi order ber-hash -> COMPLETED saat receipt muncul,
   alert admin sekali untuk revert / pending >6 jam.
3. explorer HYPEREVM = hyperscan.com (hyperevm.cloud mati).
4. ton_sender._await_delivery tetap memverifikasi walau saldo awal jetton
   gagal dibaca (rate limit tonapi).
"""
import os
import sys
import unittest
from decimal import Decimal
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

from database.connection import Base, engine, SessionLocal
from database.models import Order, User
from services import payout_watchdog
from services.payout_watchdog import _explorer_url, reconcile_broadcasted_payouts
from bot.handlers.buy import finalize_gopay_buy_payment

HASH_EVM = "0x" + "ab" * 32


def _order(order_id="ORD-WD-1", status="manual_review", payout_tx_hash=HASH_EVM,
           telegram_id=999, updated_at=None, order_type="buy", network="BSC",
           crypto_symbol="USDT"):
    return Order(
        order_id=order_id,
        telegram_id=telegram_id,
        order_type=order_type,
        crypto_symbol=crypto_symbol,
        network=network,
        crypto_amount=Decimal("10"),
        price_per_unit=16000,
        nominal_idr=160000,
        fee_idr=6000,
        total_idr=160000,
        buyer_wallet="0x" + "3" * 40,
        payment_method="GOPAY_QRIS",
        status=status,
        payout_tx_hash=payout_tx_hash,
        updated_at=updated_at,
        created_at=updated_at,
    )


class TestWatchdogRekonsiliasi(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        payout_watchdog._alerted.clear()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        payout_watchdog._alerted.clear()

    def _reload(self, order_id="ORD-WD-1"):
        self.db.expire_all()
        return self.db.query(Order).filter(Order.order_id == order_id).first()

    async def test_receipt_sukses_direkonsiliasi_completed(self):
        self.db.add(_order())
        self.db.commit()
        with patch.object(payout_watchdog, "_receipt_state",
                          new=AsyncMock(return_value="success")), \
             patch.object(payout_watchdog, "notify_admins", new=AsyncMock()) as na, \
             patch.object(payout_watchdog, "safe_send_message", new=AsyncMock()) as sm:
            jumlah = await reconcile_broadcasted_payouts(bot=AsyncMock())
        self.assertEqual(jumlah, 1)
        order = self._reload()
        self.assertEqual(order.status, "completed")
        self.assertIsNotNone(order.completed_at)
        sm.assert_awaited_once()
        na.assert_awaited_once()

    async def test_revert_alert_admin_sekali_status_tetap(self):
        self.db.add(_order())
        self.db.commit()
        with patch.object(payout_watchdog, "_receipt_state",
                          new=AsyncMock(return_value="reverted")), \
             patch.object(payout_watchdog, "notify_admins", new=AsyncMock()) as na:
            await reconcile_broadcasted_payouts(bot=AsyncMock())
            await reconcile_broadcasted_payouts(bot=AsyncMock())
        self.assertEqual(self._reload().status, "manual_review")
        self.assertEqual(na.await_count, 1, "alert revert tidak boleh spam")

    async def test_pending_lewat_6jam_alert_sekali(self):
        tua = datetime.utcnow() - timedelta(hours=7)
        self.db.add(_order(updated_at=tua))
        self.db.commit()
        with patch.object(payout_watchdog, "_receipt_state",
                          new=AsyncMock(return_value="pending")), \
             patch.object(payout_watchdog, "notify_admins", new=AsyncMock()) as na:
            await reconcile_broadcasted_payouts(bot=AsyncMock())
            await reconcile_broadcasted_payouts(bot=AsyncMock())
        self.assertEqual(self._reload().status, "manual_review")
        self.assertEqual(na.await_count, 1)
        self.assertTrue(na.call_args.kwargs.get("butuh_tindakan"))

    async def test_tanpa_hash_tidak_disentuh(self):
        self.db.add(_order(payout_tx_hash=None))
        self.db.commit()
        with patch.object(payout_watchdog, "_receipt_state",
                          new=AsyncMock(side_effect=AssertionError("tidak boleh cek"))):
            jumlah = await reconcile_broadcasted_payouts(bot=AsyncMock())
        self.assertEqual(jumlah, 0)
        self.assertEqual(self._reload().status, "manual_review")


class TestFinalizeSimpanHash(unittest.IsolatedAsyncioTestCase):
    """finalize buy harus menyimpan hash saat 'broadcast sudah dicoba'."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=999, username="u", full_name="U"))
        self.db.add(_order(order_id="ORD-FIN-1", status="paid", payout_tx_hash=None))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def test_hash_disimpan_saat_broadcast_belum_receipt(self):
        order = self.db.query(Order).filter(Order.order_id == "ORD-FIN-1").first()
        result = {
            "success": False,
            "tx_hash": HASH_EVM,
            "explorer_url": f"https://bscscan.com/tx/{HASH_EVM}",
            "error_message": "Transaksi sudah dibroadcast, menunggu receipt. Jangan kirim ulang.",
        }
        with patch("bot.handlers.buy.reserve_order_inventory", return_value=True), \
             patch("services.payout_service.send_order_payout", new=AsyncMock(return_value=result)), \
             patch("bot.handlers.buy.safe_send_message", new=AsyncMock()), \
             patch("bot.handlers.buy.notify_admins", new=AsyncMock()) as na:
            await finalize_gopay_buy_payment(self.db, order, bot=AsyncMock())

        self.db.expire_all()
        order = self.db.query(Order).filter(Order.order_id == "ORD-FIN-1").first()
        self.assertEqual(order.status, "manual_review")
        self.assertEqual(order.payout_tx_hash, HASH_EVM,
                         "hash broadcast harus disimpan agar watchdog bisa menyelesaikan")
        teks_admin = na.call_args.args[1] if na.call_args and na.call_args.args else ""
        self.assertIn(HASH_EVM, teks_admin)
        self.assertIn("bscscan.com", teks_admin)


class TestExplorerHyperevm(unittest.TestCase):
    def test_explorer_hyperscan_bukan_domain_mati(self):
        from services.crypto_sender.evm_sender import EVMSender
        self.assertEqual(EVMSender.EVM_CHAINS["HYPEREVM"]["explorer"], "https://hyperscan.com")

    def test_explorer_url_helper(self):
        self.assertEqual(_explorer_url("HYPEREVM", "0xdead"), "https://hyperscan.com/tx/0xdead")
        self.assertEqual(_explorer_url("BSC", "0xabc"), "https://bscscan.com/tx/0xabc")
        self.assertEqual(_explorer_url("TON", "msg:" + "a" * 64),
                         "https://tonviewer.com/transaction/" + "a" * 64)
        self.assertEqual(_explorer_url("", "0xabc"), "")


class TestTonAwaitDeliveryJettonRetry(unittest.IsolatedAsyncioTestCase):
    """Saldo awal gagal dibaca tidak boleh membuat verifikasi di-skip."""

    async def test_saldo_awal_gagal_lalu_terbaca(self):
        from services.crypto_sender.ton_sender import TonSender

        fake = TonSender.__new__(TonSender)
        fake.explorer_base = "https://tonviewer.com"
        panggilan = {"n": 0}

        async def _jb(recipient):
            panggilan["n"] += 1
            if panggilan["n"] < 3:
                raise RuntimeError("rate limit")
            return 2.0 if panggilan["n"] == 3 else 2.6

        fake._jetton_balance_of = _jb
        fake._recent_out_tx_hash = AsyncMock(return_value=("a" * 64, False))

        with patch("services.crypto_sender.ton_sender.asyncio.sleep", new=AsyncMock()):
            hasil = await TonSender._await_delivery(
                fake, "0:" + "1" * 64, 0.5, "USDT", 0, "msg:" + "b" * 64, "jettonwallet",
            )
        self.assertTrue(hasil.success)
        self.assertEqual(hasil.tx_hash, "a" * 64)
        self.assertGreaterEqual(panggilan["n"], 4)


if __name__ == "__main__":
    unittest.main()
