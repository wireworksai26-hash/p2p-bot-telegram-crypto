"""B6/B7 — hash deposit: beda format tetap terdeteksi; hash tempelan penyerang tidak memblokir korban.

B6: order A menyimpan '0xAB..', order B menyimpan 'ab..' atau URL explorer dari hash
    yang sama -> dulu dianggap berbeda, admin bisa membayar Rupiah dua kali.
B7: penyerang menempel hash deposit korban ke order miliknya lalu membiarkannya
    expired -> dulu hash dianggap "sudah dipakai" dan deposit korban ditolak.
"""
import os
import unittest
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

import database.models  # noqa: F401,E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import DepositClaim, Order  # noqa: E402
from services.detector import DepositDetector  # noqa: E402

H = "ab" * 32


def _order(order_id, status, deposit_hash, telegram_id=1):
    return Order(
        order_id=order_id, telegram_id=telegram_id, order_type="sell", crypto_symbol="USDT",
        network="BSC", crypto_amount=Decimal("10.0037"), price_per_unit=1, nominal_idr=1,
        fee_idr=0, total_idr=1, buyer_wallet="BCA | 1 | X", deposit_wallet="0x" + "1" * 40,
        deposit_tx_hash=deposit_hash, status=status, created_at=datetime.utcnow())


class HashReuse(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def used(self, h, exclude="ORD-NEW"):
        return DepositDetector._is_hash_used(self.db, h, exclude_order=exclude)

    def test_b6_beda_huruf_dan_prefix(self):
        self.db.add(_order("ORD-A", "completed", "0x" + H.upper()))
        self.db.commit()
        for variant in (H, "0x" + H, H.upper(), "0X" + H):
            self.assertTrue(self.used(variant), variant)

    def test_b6_url_explorer(self):
        self.db.add(_order("ORD-A", "CRYPTO_CONFIRMED", "0x" + H))
        self.db.commit()
        self.assertTrue(self.used(f"https://bscscan.com/tx/0x{H.upper()}"))

    def test_b6_deposit_claim_beda_format(self):
        self.db.add(DepositClaim(network="BSC", tx_hash="0x" + H, order_id="ORD-A"))
        self.db.commit()
        self.assertTrue(self.used(H.upper()))

    def test_b7_hash_di_order_expired_penyerang_tidak_memblokir(self):
        self.db.add(_order("ORD-ATK", "expired", "0x" + H, telegram_id=666))
        self.db.add(_order("ORD-ATK2", "cancelled", "0x" + H, telegram_id=666))
        self.db.commit()
        self.assertFalse(self.used("0x" + H, exclude="ORD-VICTIM"),
                         "hash yang belum pernah diterima order mana pun tidak boleh dianggap terpakai")

    def test_b7_kontrol_hash_order_terkonfirmasi_tetap_terpakai(self):
        for st in ("CRYPTO_CONFIRMED", "PAYOUT_QUEUED", "completed", "COMPLETED", "manual_review"):
            self.db.query(Order).delete()
            self.db.add(_order("ORD-A", st, "0x" + H))
            self.db.commit()
            self.assertTrue(self.used("0x" + H), st)

    def test_hash_kosong_dianggap_terpakai(self):
        self.assertTrue(self.used(""))


if __name__ == "__main__":
    unittest.main()
