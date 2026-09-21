"""
tests/test_sell_deposit_windows.py
==================================
Regresi perbaikan verifikasi deposit order jual:
1. Jendela verifikasi memakai SELL_DEPOSIT_WINDOW_MINUTES (default 24 jam), bukan 15 menit.
2. Order sell "expired" tetap dapat diverifikasi bila depositnya sah (dengan audit log).
3. Verifikasi EVM punya fallback explorer (Etherscan V2) saat semua RPC publik gagal.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
from database.models import Order
from services import tx_verifier
from services.detector import DepositDetector

WALLET = "0x" + "1" * 40


class TestSellDepositWindow(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.detector = DepositDetector()
        self._seq = 0

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _order(self, status="WAITING_CRYPTO_DEPOSIT", age_minutes=0, order_type="sell"):
        self._seq += 1
        created = datetime.utcnow() - timedelta(minutes=age_minutes)
        order = Order(
            order_id=f"SELL-TEST-{self._seq}",
            telegram_id=555,
            order_type=order_type,
            crypto_symbol="USDT",
            network="BSC",
            crypto_amount=Decimal("1.2"),
            price_per_unit=15000,
            nominal_idr=18000,
            fee_idr=3000,
            total_idr=18000,
            buyer_wallet="0x" + "2" * 40,
            deposit_wallet=WALLET,
            status=status,
            created_at=created,
            expired_at=created + timedelta(minutes=15),
        )
        self.db.add(order)
        self.db.commit()
        self.db.refresh(order)
        return order

    def test_deposit_deadline_follows_24h_window(self):
        order = self._order(age_minutes=20)
        deadline = self.detector.deposit_deadline(order)
        self.assertGreater(deadline, datetime.utcnow() + timedelta(hours=20))

    def test_expired_sell_and_swap_orders_are_recoverable(self):
        sell = self._order(status="expired", age_minutes=20)
        swap = self._order(status="expired", age_minutes=20, order_type="swap")
        self.assertTrue(self.detector.is_recoverable_expired(sell))
        self.assertTrue(self.detector.is_recoverable_expired(swap))

    def test_late_deposit_confirms_expired_swap_order(self):
        order = self._order(status="expired", age_minutes=20, order_type="swap")
        order.target_crypto_symbol = "USDC"
        order.target_network = "POLYGON"
        order.target_crypto_amount = Decimal("1.0")
        self.db.commit()
        verified = {
            "verified": True,
            "amount": Decimal("1.2"),
            "timestamp": int(tx_verifier._timestamp(order.created_at)) + 20 * 60,
            "tx_hash": "0x" + "c" * 64,
        }
        with patch("services.detector.notify_admins", new=AsyncMock()), \
             patch("services.detector.safe_send_message", new=AsyncMock()), \
             patch.object(self.detector, "_execute_payout", new=AsyncMock()):
            asyncio.run(self.detector._confirm_order(
                self.db, order, verified["tx_hash"], verified, None))
        self.db.refresh(order)
        self.assertEqual(order.status, "CRYPTO_CONFIRMED")

    def test_late_deposit_confirms_expired_sell_order(self):
        order = self._order(status="expired", age_minutes=20)
        verified = {
            "verified": True,
            "amount": Decimal("1.2"),
            "timestamp": int(tx_verifier._timestamp(order.created_at)) + 20 * 60,
            "tx_hash": "0x" + "a" * 64,
        }
        with patch("services.detector.notify_admins", new=AsyncMock()), \
             patch("services.detector.safe_send_message", new=AsyncMock()):
            asyncio.run(self.detector._confirm_order(
                self.db, order, verified["tx_hash"], verified, None))
        self.db.refresh(order)
        self.assertEqual(order.status, "CRYPTO_CONFIRMED")

    def test_deposit_beyond_window_is_not_confirmed(self):
        order = self._order(status="expired", age_minutes=1500)  # > 24 jam
        verified = {
            "verified": True,
            "amount": Decimal("1.2"),
            "timestamp": int(tx_verifier._timestamp(order.created_at)) + 1500 * 60,
            "tx_hash": "0x" + "b" * 64,
        }
        with patch("services.detector.notify_admins", new=AsyncMock()), \
             patch("services.detector.safe_send_message", new=AsyncMock()):
            asyncio.run(self.detector._confirm_order(
                self.db, order, verified["tx_hash"], verified, None))
        self.db.refresh(order)
        self.assertEqual(order.status, "expired")


class TestExplorerFallback(unittest.IsolatedAsyncioTestCase):
    async def test_evm_verify_falls_back_to_explorer_when_rpc_fails(self):
        from services.crypto_sender import CryptoSenderFactory

        sender = CryptoSenderFactory.get_sender("BSC")
        sender.rpc_list = ["https://stub.invalid"]  # satu percobaan saja, tanpa rotasi
        sender.w3 = SimpleNamespace(eth=SimpleNamespace(
            chain_id=56,
            get_transaction_receipt=Mock(side_effect=RuntimeError("RPC quota/archieve")),
        ))

        raw_amount = 1_200_000_000_000_000_000  # 1.2 USDT (18 desimal)
        receipt = {
            "status": "0x1",
            "blockNumber": "0x10",
            "logs": [{
                "address": sender.config["tokens"]["USDT"],
                "topics": [
                    "0x" + tx_verifier.TRANSFER_TOPIC,
                    "0x" + "0" * 24 + "3" * 40,
                    "0x" + "0" * 24 + WALLET[2:],
                ],
                "data": hex(raw_amount),
            }],
        }

        async def fake_json(method, url, **kwargs):
            action = (kwargs.get("params") or {}).get("action")
            if action == "eth_getTransactionReceipt":
                return {"status": "1", "result": receipt}
            if action == "eth_blockNumber":
                return {"status": "1", "result": "0x20"}
            if action == "eth_getBlockByNumber":
                return {"status": "1", "result": {"timestamp": "0x64"}}
            return {"status": "0", "result": "aksi tidak dikenal"}

        with patch.object(tx_verifier.settings, "ETHERSCAN_API_KEY", "TESTKEY"), \
             patch("services.tx_verifier._json", new=fake_json):
            result = await tx_verifier._verify_evm("BSC", "USDT", "0x" + "c" * 64, WALLET)

        self.assertTrue(result.get("verified"), result)
        self.assertAlmostEqual(float(result["amount"]), 1.2)

    async def test_explorer_fallback_skipped_without_key(self):
        from services.crypto_sender import CryptoSenderFactory

        sender = CryptoSenderFactory.get_sender("BSC")
        sender.rpc_list = ["https://stub.invalid"]
        sender.w3 = SimpleNamespace(eth=SimpleNamespace(
            chain_id=56,
            get_transaction_receipt=Mock(side_effect=RuntimeError("RPC quota")),
        ))
        with patch.object(tx_verifier.settings, "ETHERSCAN_API_KEY", ""):
            result = await tx_verifier._verify_evm("BSC", "USDT", "0x" + "d" * 64, WALLET)
        self.assertFalse(result.get("verified"))
        self.assertIn("RPC EVM gagal", result.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
