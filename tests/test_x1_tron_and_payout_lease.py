"""WR-10 / WR-11 (review Sell-Swap).

WR-11: alamat TRON hanya dicek panjang & base58 (tanpa checksum) -> salah ketik satu
huruf lolos dan koin terkirim ke alamat yang tidak ada (tidak bisa dipulihkan).
WR-10: watchdog PAYOUT_QUEUED 120 dtk lebih pendek dari tunggu solid TRON 150 dtk ->
order dipindah ke manual_review saat payout masih berjalan; admin bisa kirim ulang
(payout ganda) dan coroutine lama menimpa status dari objek basi.
"""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import base58  # noqa: E402
from bot.utils.validator import validate_wallet_address  # noqa: E402
from services import wallet_detector  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order  # noqa: E402
from services.detector import DepositDetector, PAYOUT_INFLIGHT_SECONDS  # noqa: E402

VALID = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # kontrak USDT TRC-20 (checksum valid)


def _mutate(addr):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    i = 10
    repl = next(c for c in alphabet if c != addr[i])
    return addr[:i] + repl + addr[i + 1:]


class TronChecksum(unittest.TestCase):
    def test_alamat_valid_diterima(self):
        self.assertTrue(validate_wallet_address(VALID, "TRON"))
        self.assertTrue(wallet_detector.validate_wallet_address(VALID, "TRON"))

    def test_salah_ketik_ditolak(self):
        bad = _mutate(VALID)
        self.assertEqual(len(bad), 34)
        base58.b58decode(bad)  # masih base58 valid: inilah yang dulu lolos
        self.assertFalse(validate_wallet_address(bad, "TRON"))
        self.assertFalse(wallet_detector.validate_wallet_address(bad, "TRON"))

    def test_karakter_non_base58_ditolak(self):
        for ch in "0OIl":
            bad = VALID[:5] + ch + VALID[6:]
            self.assertFalse(wallet_detector.validate_wallet_address(bad, "TRON"), ch)

    def test_prefix_salah_ditolak(self):
        raw = b"\x42" + b"\x01" * 20
        addr = base58.b58encode_check(raw).decode()
        self.assertFalse(validate_wallet_address(addr, "TRON"))


class PayoutLease(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def _order(self, age_seconds):
        db = SessionLocal()
        db.add(Order(order_id="ORD-PQ", telegram_id=1, order_type="swap", crypto_symbol="USDT",
                     network="BSC", crypto_amount=Decimal("10.0001"), target_crypto_symbol="TRX",
                     target_network="TRON", target_crypto_amount=Decimal("50"), price_per_unit=0,
                     nominal_idr=1, fee_idr=0, total_idr=1, buyer_wallet=VALID,
                     deposit_wallet="0x" + "1" * 40, status="PAYOUT_QUEUED",
                     created_at=datetime.utcnow(),
                     updated_at=datetime.utcnow() - timedelta(seconds=age_seconds)))
        db.commit()
        db.close()

    async def _tick(self):
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.order_id == "ORD-PQ").one()
            await DepositDetector()._process_order(db, order, bot_app=None)
            return db.query(Order).filter(Order.order_id == "ORD-PQ").one().status
        finally:
            db.close()

    def test_lease_lebih_panjang_dari_tunggu_tron(self):
        self.assertGreater(PAYOUT_INFLIGHT_SECONDS, 150 * 2)

    async def test_payout_masih_berjalan_tidak_dipindah_ke_review(self):
        self._order(age_seconds=200)  # > 120 dtk lama, < tunggu TRON + retry
        self.assertEqual(await self._tick(), "PAYOUT_QUEUED")

    async def test_payout_macet_lama_baru_ke_review(self):
        self._order(age_seconds=PAYOUT_INFLIGHT_SECONDS + 60)
        self.assertEqual(await self._tick(), "manual_review")


if __name__ == "__main__":
    unittest.main()
