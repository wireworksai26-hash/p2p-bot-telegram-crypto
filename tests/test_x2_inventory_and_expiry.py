"""Inventory & expiry (review Core WR-02/WR-03, Buy WR-02).

- expire_stale_orders menimpa status dari objek ORM basi: order yang baru dibayar di
  antara SELECT dan COMMIT ikut menjadi 'expired' (saldo/QRIS sudah masuk, order hilang).
- release_order_inventory baca-ubah-tulis: sesi dengan baris wallet basi menghapus
  reservasi order lain; setelah payout sukses stok (balance) tetap utuh sampai sync
  berikutnya -> bot menjual koin yang sebenarnya sudah keluar (oversell).
- payout gagal sebelum broadcast (tanpa hash) tidak melepas reservasi -> stok
  tampak berkurang selamanya.
"""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from sqlalchemy import event  # noqa: E402

import database.models  # noqa: F401,E402
from database import crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import InventoryReservation, Order, User, WalletBalance  # noqa: E402

WALLET = "0x" + "c" * 40


def _order(order_id, status="pending", method="GOPAY_QRIS", age_min=30, paid=False):
    t = datetime.utcnow() - timedelta(minutes=age_min)
    return Order(order_id=order_id, telegram_id=1, order_type="buy", crypto_symbol="USDT", network="BSC",
                 crypto_amount=Decimal("10"), price_per_unit=17915, nominal_idr=179150, fee_idr=3000,
                 total_idr=182150, buyer_wallet=WALLET, payment_method=method, status=status,
                 paid_at=datetime.utcnow() if paid else None, created_at=t)


class Base_(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add(User(telegram_id=1))
        db.add(WalletBalance(network="BSC", symbol="USDT", balance=Decimal("1000"),
                             reserved_balance=Decimal("0"), address="0x1", sync_status="OK",
                             last_success_at=datetime.utcnow()))
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)

    def wallet(self):
        db = SessionLocal()
        try:
            w = db.query(WalletBalance).one()
            return Decimal(str(w.balance)), Decimal(str(w.reserved_balance))
        finally:
            db.close()


class ExpiryRace(Base_):
    def test_order_yang_dibayar_saat_proses_expire_tidak_ikut_expired(self):
        db = SessionLocal()
        db.add(_order("ORD-RACE"))
        db.commit()
        fired = {"done": False}

        def pay_in_between(conn, cursor, statement, parameters, context, executemany):
            if not fired["done"] and statement.lstrip().upper().startswith("SELECT") and "FROM orders" in statement:
                fired["done"] = True
                other = conn.connection.cursor()  # kursor terpisah: jangan rusak hasil SELECT
                other.execute("UPDATE orders SET status='paid', paid_at=CURRENT_TIMESTAMP WHERE order_id='ORD-RACE'")
                other.close()

        event.listen(engine, "after_cursor_execute", pay_in_between)
        try:
            count = crud.expire_stale_orders(db, minutes=15)
        finally:
            event.remove(engine, "after_cursor_execute", pay_in_between)
            db.close()
        check = SessionLocal()
        try:
            self.assertEqual(check.query(Order).filter(Order.order_id == "ORD-RACE").one().status, "paid")
        finally:
            check.close()
        self.assertEqual(count, 0)

    def test_kontrol_order_lama_belum_dibayar_tetap_expired(self):
        db = SessionLocal()
        db.add(_order("ORD-OLD"))
        db.add(_order("ORD-BAL", method="BOT_BALANCE", paid=True))
        db.commit()
        self.assertEqual(crud.expire_stale_orders(db, minutes=15), 1)
        statuses = {o.order_id: o.status for o in db.query(Order).all()}
        db.close()
        self.assertEqual(statuses, {"ORD-OLD": "expired", "ORD-BAL": "pending"})


class Inventory(Base_):
    def _reserve(self, oid, amount="100"):
        db = SessionLocal()
        try:
            self.assertTrue(crud.reserve_order_inventory(db, oid, "BSC", "USDT", Decimal(amount)))
        finally:
            db.close()

    def test_payout_sukses_mengurangi_stok_langsung(self):
        self._reserve("ORD-1")
        db = SessionLocal()
        try:
            self.assertTrue(crud.release_order_inventory(db, "ORD-1", consumed=True))
            available = crud.get_available_inventory(db, "BSC", "USDT")
        finally:
            db.close()
        self.assertEqual(available, Decimal("900"), "100 USDT sudah keluar dari wallet")
        self.assertEqual(self.wallet(), (Decimal("900"), Decimal("0")))

    def test_batal_tanpa_kirim_koin_tidak_mengurangi_saldo(self):
        self._reserve("ORD-1")
        db = SessionLocal()
        try:
            self.assertTrue(crud.release_order_inventory(db, "ORD-1"))
        finally:
            db.close()
        self.assertEqual(self.wallet(), (Decimal("1000"), Decimal("0")))

    def test_release_dua_kali_tidak_mengurangi_dua_kali(self):
        self._reserve("ORD-1")
        db = SessionLocal()
        try:
            self.assertTrue(crud.release_order_inventory(db, "ORD-1", consumed=True))
            self.assertFalse(crud.release_order_inventory(db, "ORD-1", consumed=True))
        finally:
            db.close()
        self.assertEqual(self.wallet(), (Decimal("900"), Decimal("0")))

    def test_sesi_basi_tidak_menghapus_reservasi_order_lain(self):
        self._reserve("ORD-1")
        stale = SessionLocal()
        stale.query(WalletBalance).one()  # sesi memegang baris wallet (reserved=100)
        self._reserve("ORD-2", "50")      # order lain mereservasi -> 150
        try:
            crud.release_order_inventory(stale, "ORD-1")
        finally:
            stale.close()
        self.assertEqual(self.wallet()[1], Decimal("50"))


class FailedPayoutReleases(Base_):
    async def _finalize(self, result):
        from bot.handlers import buy
        db = SessionLocal()
        db.add(_order("ORD-PAID", status="paid", age_min=1, paid=True))
        db.commit()
        order = db.query(Order).filter(Order.order_id == "ORD-PAID").one()
        with patch("services.payout_service.send_order_payout", new=AsyncMock(return_value=result)), \
             patch.object(buy, "notify_admins", new=AsyncMock()), \
             patch.object(buy, "safe_send_message", new=AsyncMock()):
            await buy.finalize_gopay_buy_payment(db, order, bot=AsyncMock(), allow_recovery=True)
        db.close()

    def _reservation_status(self):
        db = SessionLocal()
        try:
            return db.query(InventoryReservation).one().status
        finally:
            db.close()


class FailedPayoutReleasesAsync(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base_.setUp(self)

    def tearDown(self):
        Base_.tearDown(self)

    _finalize = FailedPayoutReleases._finalize
    _reservation_status = FailedPayoutReleases._reservation_status
    wallet = Base_.wallet

    async def test_gagal_sebelum_broadcast_melepas_reservasi(self):
        await self._finalize({"success": False, "tx_hash": "", "explorer_url": "", "error_message": "RPC down"})
        self.assertEqual(self._reservation_status(), "RELEASED")
        self.assertEqual(self.wallet(), (Decimal("1000"), Decimal("0")))

    async def test_gagal_setelah_broadcast_menahan_reservasi(self):
        await self._finalize({"success": False, "tx_hash": "0x" + "a" * 64, "explorer_url": "",
                              "error_message": "receipt belum ada"})
        self.assertEqual(self._reservation_status(), "RESERVED")

    async def test_sukses_mengonsumsi_stok(self):
        await self._finalize({"success": True, "tx_hash": "0x" + "b" * 64, "explorer_url": "", "error_message": ""})
        self.assertEqual(self._reservation_status(), "RELEASED")
        self.assertEqual(self.wallet(), (Decimal("990"), Decimal("0")))


if __name__ == "__main__":
    unittest.main()
