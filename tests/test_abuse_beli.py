"""Uji abuse/crossvalidation alur BELI + QRIS — hasil audit keamanan 26 Sep 2026.

Skenario penyerangan yang diuji:
1. Cross-attribution: transaksi bank di-match hanya dari nominal + waktu (tidak
   ada ikatan ke order) — satu pembayaran bisa menyasar order lain senilai sama.
2. Daur ulang kode unik (1..150) setelah order expired, padahal transaksi
   pembayarannya masih di jendela lookback 24 jam.
3. Toleransi 5 menit ke belakang → transaksi sebelum order dibuat dianggap sah.
4. Atomic claim order (paid/payout_processing) harus kebal double-click.
5. Guard fee >= nominal, kepemilikan order pada tombol "Saya Sudah Transfer".

Konvensi: kasus yang SUDAH aman = test hijau (lock-in). Kasus yang masih bocor =
assert ekspektasi AMAN + @unittest.expectedFailure (XFAIL) supaya suite tetap
hijau; saat bug diperbaiki test menjadi XPASS dan marker dihapus.
"""
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
    "EVM_PRIVATE_KEY": "",
})

from database.connection import Base, engine, SessionLocal
from database.models import Order, TopupOrder, User
from database import crud
from main import _match_transaction, _cleanup_matched_tx_ids
from bot.handlers.buy import (
    check_buy_payment,
    handle_amount_input,
    INPUT_AMOUNT,
)

WAKTU_ORDER = datetime(2026, 9, 26, 10, 0, 0)


def _order(order_id="ORD-ABUSE-1", status="pending", total=50077, code=77, telegram_id=999):
    return Order(
        order_id=order_id,
        telegram_id=telegram_id,
        order_type="buy",
        crypto_symbol="USDT",
        network="BSC",
        crypto_amount=Decimal("1"),
        price_per_unit=16000,
        nominal_idr=50000,
        fee_idr=3000,
        total_idr=total,
        unique_code=code,
        buyer_wallet="0x" + "3" * 40,
        payment_method="GOPAY_QRIS",
        status=status,
    )


class TestMatchTransactionCrossAttribution(unittest.TestCase):
    """Matcher pembayaran hanya nominal+waktu: sejauh mana ia bisa salah atribusi?"""

    def test_nominal_beda_tidak_mencocok(self):
        txn = {"transaction_id": "TX-1", "amount": 50076,
               "transaction_time": "2026-09-26T10:00:30Z"}
        self.assertFalse(_match_transaction(txn, 50077, WAKTU_ORDER, set()))

    def test_tx_id_sudah_dipakai_tidak_mencocok_lagi(self):
        txn = {"transaction_id": "TX-1", "amount": 50077,
               "transaction_time": "2026-09-26T10:00:30Z"}
        self.assertFalse(_match_transaction(txn, 50077, WAKTU_ORDER, {"TX-1"}))

    def test_transaksi_jauh_sebelum_order_ditolak(self):
        txn = {"transaction_id": "TX-1", "amount": 50077,
               "transaction_time": "2026-09-26T09:54:00Z"}  # 6 menit sebelum order
        self.assertFalse(_match_transaction(txn, 50077, WAKTU_ORDER, set()))

    def test_transaksi_sesudah_order_mencocok(self):
        txn = {"transaction_id": "TX-1", "amount": 50077,
               "transaction_time": "2026-09-26T10:00:30Z"}
        self.assertTrue(_match_transaction(txn, 50077, WAKTU_ORDER, set()))

    def test_transaksi_sebelum_order_tidak_boleh_mencocok(self):
        """Crossvalidation ketat: tx 3 menit SEBELUM order dibuat tidak boleh dianggap bayar.

        Toleransi 5 menit ke belakang membuka jendela replay: transaksi lama
        (masih dalam lookback 24 jam) bisa menutup order baru senilai sama.
        """
        txn = {"transaction_id": "TX-LAMA", "amount": 50077,
               "transaction_time": "2026-09-26T09:57:00Z"}
        self.assertFalse(
            _match_transaction(txn, 50077, WAKTU_ORDER, set()),
            "transaksi 3 menit sebelum order tidak boleh dianggap pembayaran order ini",
        )

    def test_cleanup_map_tx_tidak_hapus_yang_baru(self):
        now = datetime.utcnow().timestamp()
        data = {"TX-BARU": now, "TX-TUA": now - 90000}
        _cleanup_matched_tx_ids(data, max_age_seconds=86400)
        self.assertIn("TX-BARU", data)
        self.assertNotIn("TX-TUA", data)


class TestKodeUnikDaurUlang(unittest.TestCase):
    """Kode unik 1..200: apa yang terjadi setelah order expired?"""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_kode_pending_tidak_dipakai_ulang(self):
        self.db.add(_order(order_id="ORD-PENDING", status="pending", code=1))
        self.db.commit()
        kode = crud.generate_unique_payment_code(self.db, min_code=1, max_code=1)
        self.assertNotEqual(kode, 1, "kode order pending tidak boleh dipakai order baru")

    def test_kode_daur_ulang_tidak_bisa_dilunasi_pembayaran_lama(self):
        """Kode order expired boleh didaur ulang, TAPI pembayaran yang sudah
        melunasi order lama tidak bisa melunasi order baru bernominal sama:
        transaksi diklaim permanen di DB (qris_payment_claims)."""
        self.assertTrue(crud.claim_qris_payment(self.db, "TX-LAMA", "ORD-LAMA", "buy", 50077))
        self.assertFalse(crud.claim_qris_payment(self.db, "TX-LAMA", "ORD-BARU", "buy", 50077))
        # Re-check order yang sama tetap idempoten.
        self.assertTrue(crud.claim_qris_payment(self.db, "TX-LAMA", "ORD-LAMA", "buy", 50077))


class TestAtomicClaims(unittest.TestCase):
    """Claim atomik order harus idempoten dan kebal double-click."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_claim_paid_hanya_sekali(self):
        self.db.add(_order(status="pending"))
        self.db.commit()
        self.assertTrue(crud.claim_order_paid(self.db, "ORD-ABUSE-1"))
        self.assertFalse(crud.claim_order_paid(self.db, "ORD-ABUSE-1"))

    def test_claim_payout_tolak_jika_hash_sudah_ada(self):
        o = _order(status="paid")
        o.payout_tx_hash = "0x" + "f" * 64
        self.db.add(o)
        self.db.commit()
        self.assertFalse(crud.claim_order_payout_processing(self.db, "ORD-ABUSE-1"))

    def test_claim_payout_hanya_sekali(self):
        self.db.add(_order(status="paid"))
        self.db.commit()
        self.assertTrue(crud.claim_order_payout_processing(self.db, "ORD-ABUSE-1"))
        self.assertFalse(crud.claim_order_payout_processing(self.db, "ORD-ABUSE-1"))

    def test_claim_stale_hanya_untuk_yang_lama(self):
        o = _order(status="payout_processing")
        self.db.add(o)
        self.db.commit()
        self.assertFalse(crud.claim_stale_payout_processing(self.db, "ORD-ABUSE-1", stale_seconds=120))
        o.updated_at = datetime.utcnow() - timedelta(minutes=10)
        self.db.commit()
        self.assertTrue(crud.claim_stale_payout_processing(self.db, "ORD-ABUSE-1", stale_seconds=120))


class TestBuyHandlerAbuse(unittest.IsolatedAsyncioTestCase):
    """Simulasi serangan via handler beli (fee guard + tombol cek pembayaran)."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def test_fee_melebihi_nominal_ditolak(self):
        message = AsyncMock()
        message.text = "5000"
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=123))
        context = SimpleNamespace(user_data={"buy_symbol": "USDT", "buy_network": "BSC"})
        with patch("services.price_service.PriceService.get_price", new=AsyncMock(return_value={
            "symbol": "USDT", "buy_price_idr": 16000, "sell_price_idr": 15800,
            "source": "MOCK", "price_updated_at": int(datetime.utcnow().timestamp()),
        })), patch("bot.handlers.buy.calculate_fee_idr", return_value=99999), \
             patch("bot.handlers.buy.SessionLocal", side_effect=lambda: SessionLocal()):
            state = await handle_amount_input(update, context)
        self.assertEqual(state, INPUT_AMOUNT)
        text = message.reply_text.call_args.kwargs.get("text", "")
        self.assertIn("Nominal Terlalu Kecil", text)

    async def test_tombol_cek_terhadap_order_user_lain_ditolak(self):
        self.db.add(User(telegram_id=999, username="korban", full_name="Korban"))
        self.db.add(_order(status="pending"))
        self.db.commit()

        query = AsyncMock()
        query.data = "check_buy_payment_ORD-ABUSE-1"
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=111))
        gopay = AsyncMock()
        with patch("bot.handlers.buy.SessionLocal", side_effect=lambda: SessionLocal()), \
             patch("bot.handlers.buy.gopay_service", gopay):
            await check_buy_payment(update, SimpleNamespace(bot=AsyncMock()))

        gopay.check_payment.assert_not_awaited()
        balasan = [str(c.args[0]) if c.args else "" for c in query.answer.call_args_list]
        self.assertTrue(any("Akses ditolak" in b for b in balasan),
                        f"order milik user lain harus ditolak, balasan: {balasan}")


if __name__ == "__main__":
    unittest.main()
