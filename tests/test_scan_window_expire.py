# Regresi: jendela scan getLogs dibatasi + order macet tidak dipindai/di-expire.
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
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
    "EVM_PRIVATE_KEY": "0x" + "1" * 64,
    "ETHERSCAN_API_KEY": "TESTKEY",
})

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database.models import Base, Order
from database.crud import expire_stale_orders
from services.detector import DepositDetector
from services import tx_verifier

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
TestingSession = sessionmaker(bind=engine)


def make_order(db, order_id, created_at, status="WAITING_CRYPTO_DEPOSIT"):
    db.add(Order(
        order_id=order_id, telegram_id=555, order_type="swap",
        crypto_symbol="USDT", network="BSC", crypto_amount=Decimal("0.5"),
        price_per_unit=15000, nominal_idr=7500, fee_idr=3500, total_idr=7500,
        buyer_wallet="0x" + "2" * 40, deposit_wallet="0x" + "1" * 40,
        status=status, created_at=created_at))
    db.commit()


class FakeWeb3:
    def __init__(self):
        self.queries = []
        self.eth = SimpleNamespace(chain_id=56, block_number=1000, get_logs=self._get)

    def _get(self, query):
        self.queries.append(query)
        return []

    def to_checksum_address(self, address):
        return address

    def to_hex(self, value):
        return value


class TestJendelaScan(unittest.TestCase):
    def test_getlogs_jendela_kecil(self):
        w3 = FakeWeb3()
        sender = SimpleNamespace(
            rpc_list=["https://rpc.invalid"],
            config={"chain_id": 56, "native_symbol": "BNB",
                    "tokens": {"USDT": "0x55d398326f99059ff775485246999027b3197955"}})

        async def run():
            async for _ in tx_verifier._scan_hashes(
                    "BSC", "USDT", "0x" + "a" * 40, 20, datetime.utcnow()):
                pass

        with patch.object(tx_verifier.CryptoSenderFactory, "get_sender", \
                          return_value=sender), \
                patch.object(tx_verifier, "_scan_web3", return_value=w3):
            asyncio.run(run())

        self.assertTrue(w3.queries)
        for q in w3.queries:
            self.assertGreaterEqual(q["fromBlock"], 950)


class TestOrderMacet(unittest.TestCase):
    def setUp(self):
        db = TestingSession()
        db.query(Order).delete()
        db.commit()
        db.close()

    def test_order_tua_tidak_dipindai(self):
        db = TestingSession()
        make_order(db, "MACET-1", datetime.utcnow() - timedelta(hours=50))
        make_order(db, "BARU-1", datetime.utcnow())
        db.close()

        diproses = []

        async def fake_process(session, order_obj, bot_app):
            diproses.append(order_obj.order_id)

        detector = DepositDetector()
        with patch("services.detector.SessionLocal", TestingSession), \
                patch.object(detector, "_process_order", \
                             new=AsyncMock(side_effect=fake_process)):
            asyncio.run(detector.scan_incoming_deposits(None))

        self.assertIn("BARU-1", diproses)
        self.assertNotIn("MACET-1", diproses)

    def test_order_macet_diekspire(self):
        db = TestingSession()
        make_order(db, "MACET-2", datetime.utcnow() - timedelta(hours=50))
        make_order(db, "BARU-2", datetime.utcnow())
        db.close()

        db = TestingSession()
        expire_stale_orders(db, minutes=30)
        status_map = {o.order_id: o.status for o in db.query(Order).all()}
        db.close()
        self.assertEqual(status_map.get("MACET-2"), "expired")
        self.assertEqual(status_map.get("BARU-2"), "WAITING_CRYPTO_DEPOSIT")


if __name__ == "__main__":
    unittest.main()
