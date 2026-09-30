"""Uji abuse input & state machine — hasil audit keamanan 26 Sep 2026.

Skenario penyerangan yang diuji:
1. Validator nominal: digit Unicode/non-ASCII, format aneh, batas panjang.
2. validate_crypto_amount tanpa batas atas → angka raksasa jadi inf (diterima!).
3. update_order_status menerima transisi status apa pun (completed→cancelled).
4. is_banned tidak pernah dicek di jalur trading (hanya broadcast/users panel).
5. Data bank free-text tanpa batas panjang (kolom DB 250).

Konvensi: XFAIL = bukti RED dari bug yang belum diperbaiki.
"""
import os
import sys
import unittest
from datetime import datetime
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
from database import crud
from bot.utils.validator import validate_amount_idr, validate_crypto_amount
from bot.handlers.sell import handle_bank_input, start_sell_callback


class TestValidateAmountIdr(unittest.TestCase):
    def test_format_valid(self):
        for raw, expected in (
            ("50000", 50000),
            ("50.000", 50000),
            ("50,000", 50000),
            ("Rp 50.000", 50000),
            ("rp. 25000", 25000),
            ("50k", 50000),
            ("10rb", 10000),
            ("1.000.000", 1000000),
        ):
            with self.subTest(raw=raw):
                ok, val = validate_amount_idr(raw)
                self.assertTrue(ok, raw)
                self.assertEqual(val, expected)

    def test_ditolak(self):
        for raw in ("4000", "11000000", "abc", "", "0x" + "1" * 40,
                    "T" + "1" * 33, "+5000", "5.000.0", "1..000",
                    "9" * 31, "-5000", "5e3"):
            with self.subTest(raw=raw):
                ok, _ = validate_amount_idr(raw)
                self.assertFalse(ok, f"{raw!r} harus ditolak")

    def test_digit_arab_ditolak(self):
        """Digit non-ASCII (\u0665\u0660\u0660\u0660 = 5000) lolos regex \\d Unicode."""
        ok, val = validate_amount_idr("\u0665\u0660\u0660\u0660")
        self.assertFalse(ok, f"digit non-ASCII harus ditolak (terbaca {val})")

    def test_digit_fullwidth_ditolak(self):
        """Digit fullwidth (５０００) juga lolos via int() Python."""
        ok, val = validate_amount_idr("\uff15\uff10\uff10\uff10")
        self.assertFalse(ok, f"digit fullwidth harus ditolak (terbaca {val})")


class TestValidateCryptoAmount(unittest.TestCase):
    def test_format_valid(self):
        ok, val = validate_crypto_amount("1,5")
        self.assertTrue(ok)
        self.assertAlmostEqual(val, 1.5)

    def test_ditolak(self):
        for raw in ("0", "-1", "1.2.3", "nan", "inf", "1e3", ""):
            with self.subTest(raw=raw):
                ok, _ = validate_crypto_amount(raw)
                self.assertFalse(ok, f"{raw!r} harus ditolak")

    def test_angka_raksasa_jadi_inf_ditolak(self):
        """'9'*400 lolos regex desimal lalu float() → inf dan DITERIMA sebagai
        jumlah crypto (harga jual dihitung dari inf)."""
        ok, val = validate_crypto_amount("9" * 400)
        self.assertFalse(ok, f"angka tak berbatas harus ditolak (terbaca {val})")


class TestUpdateOrderStatusTransisi(unittest.TestCase):
    """update_order_status tidak punya tabel transisi — any → any."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(Order(
            order_id="ORD-STATE-1", telegram_id=1, order_type="sell",
            crypto_symbol="USDT", network="BSC", crypto_amount=10,
            price_per_unit=16000, nominal_idr=160000, fee_idr=6000,
            total_idr=154000, status="completed",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    @unittest.expectedFailure
    def test_completed_tidak_bisa_dibatalkan(self):
        hasil = crud.update_order_status(self.db, "ORD-STATE-1", new_status="cancelled")
        self.assertIsNone(
            hasil,
            "transisi completed → cancelled harus ditolak oleh state machine",
        )


class TestBannedUserTrading(unittest.IsolatedAsyncioTestCase):
    """is_banned di-set admin tapi tidak dicek di entry trading."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    @unittest.expectedFailure
    async def test_user_banned_tidak_bisa_mulai_jual(self):
        self.db.add(User(telegram_id=777, username="banned", full_name="Banned", is_banned=True))
        self.db.commit()
        query = AsyncMock()
        query.from_user = SimpleNamespace(id=777, first_name="Banned")
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(user_data={})
        with patch("bot.handlers.sell.safe_edit_message", new=AsyncMock()) as sem:
            await start_sell_callback(update, context)
        teks = sem.call_args.kwargs.get("text", "") if sem.call_args else ""
        self.assertIn(
            "blokir", teks.lower(),
            "user banned harus ditolak di entry trading (is_banned tidak pernah dicek)",
        )


class TestBankInfoFreeText(unittest.IsolatedAsyncioTestCase):
    """Data bank free-text: tanpa charset & tanpa batas panjang (kolom DB 250)."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def test_data_bank_tidak_melebihi_kolom_db(self):
        message = AsyncMock()
        message.text = "Bank X, " + "9" * 300 + ", Pemilik"
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=555, first_name="T"))
        context = SimpleNamespace(user_data={
            "sell_crypto_amount": 10.0,
            "sell_symbol": "USDT",
            "sell_network": "BSC",
            "sell_price_per_unit": 16000,
            "sell_fee_idr": 6000,
            "sell_net_idr": 154000,
        })
        await handle_bank_input(update, context)
        acc = context.user_data.get("sell_bank_acc", "")
        self.assertLessEqual(
            len(acc), 250,
            "nomor rekening melebihi panjang kolom DB (String(250)) — Postgres akan error saat insert",
        )


if __name__ == "__main__":
    unittest.main()
