"""Regresi feedback verifikasi deposit (kasus order SWAP-20260923073608-165 'stuck').

Bug: penolakan deposit (mis. transfer ke diri sendiri / nominal kurang) tidak pernah
sampai ke user, dan pesan nominal tidak menyebut angkanya.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
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
})

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Order
from services import tx_verifier
from services.detector import DepositDetector

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
TestingSession = sessionmaker(bind=engine)

HASH = "0x" + "a" * 64
WALLET = "0x" + "1" * 40


class TestPesanNominal(unittest.TestCase):
    def test_nominal_kurang_menyebut_angka_diterima_dan_dibutuhkan(self):
        async def fake_evm(net, symbol, tx_hash, wallet):
            return {"verified": True, "amount": Decimal("0.48"),
                    "timestamp": 1790000000, "tx_hash": tx_hash, "reason": "OK"}

        with patch.object(tx_verifier, "_verify_evm", new=AsyncMock(side_effect=fake_evm)):
            res = asyncio.run(tx_verifier.verify_deposit(
                "BSC", "USDT", HASH, WALLET, 0.5))

        self.assertFalse(res["verified"])
        self.assertIn("0.48", res["reason"])
        self.assertIn("dibutuhkan 0.5", res["reason"])

    def test_nominal_sesuai_lolos(self):
        async def fake_evm(net, symbol, tx_hash, wallet):
            return {"verified": True, "amount": Decimal("0.5"),
                    "timestamp": 1790000000, "tx_hash": tx_hash, "reason": "OK"}

        with patch.object(tx_verifier, "_verify_evm", new=AsyncMock(side_effect=fake_evm)):
            res = asyncio.run(tx_verifier.verify_deposit(
                "BSC", "USDT", HASH, WALLET, 0.5))

        self.assertTrue(res["verified"])


class TestVerifikasiCepat(unittest.TestCase):
    def test_berhenti_setelah_status_berubah(self):
        db = TestingSession()
        order = Order(
            order_id="UJI-VERIF-1", telegram_id=555, order_type="swap",
            crypto_symbol="USDT", network="BSC", crypto_amount=Decimal("0.5"),
            price_per_unit=15000, nominal_idr=7500, fee_idr=3500, total_idr=7500,
            buyer_wallet="0x" + "2" * 40, deposit_wallet=WALLET,
            status="WAITING_CRYPTO_DEPOSIT", created_at=datetime.utcnow(),
        )
        db.add(order)
        db.commit()
        db.close()

        dipanggil = []

        async def fake_process(session, order_obj, bot_app):
            dipanggil.append(order_obj.order_id)
            order_obj.status = "CRYPTO_CONFIRMED"
            session.commit()

        detector = DepositDetector()
        with patch("services.detector.SessionLocal", TestingSession), \
                patch.object(detector, "_process_order", new=AsyncMock(side_effect=fake_process)):
            asyncio.run(detector.verifikasi_cepat("UJI-VERIF-1", attempts=5, interval=0))

        self.assertEqual(dipanggil, ["UJI-VERIF-1"])

        db = TestingSession()
        db.query(Order).filter(Order.order_id == "UJI-VERIF-1").delete()
        db.commit()
        db.close()


if __name__ == "__main__":
    unittest.main()
