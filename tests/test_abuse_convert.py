"""Uji abuse/crossvalidation alur CONVERT — hasil audit keamanan 26 Sep 2026.

Skenario penyerangan yang diuji:
1. Parser nominal menerima literal float aneh ("1e3", "nan", "inf") dan salah
   menafsirkan "1.000" (desimal koin vs ribuan Rupiah).
2. Quote dibekukan saat input; konfirmasi tidak pernah menyegarkan harga
   (stale rate lock — user menahan layar konfirmasi sampai harga menguntungkan).
3. Invariant server-side src==tgt (koin/jaringan sama persis) tidak divalidasi
   di confirm_swap_order — bergantung UI saja (callback bisa diforging/di-forward).
4. Alamat target = hot wallet sendiri tidak ditolak (self-dealing / dana nyangkut).

Konvensi: XFAIL = bukti RED dari bug yang belum diperbaiki.
"""
import os
import sys
import unittest
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
from database.models import Order, User
from bot.handlers.swap import (
    CONFIRM_SWAP,
    confirm_swap_order,
    input_target_addr,
    parse_convert_amount,
)

HARGA_SRC = 16000.0
KURS_USDT = 16000.0


class TestParseConvertAmount(unittest.TestCase):
    """Parser input convert — permukaan abuse paling lebar di alur convert."""

    def test_usd_dan_idr_dan_crypto_normal(self):
        self.assertEqual(parse_convert_amount("$10", HARGA_SRC, KURS_USDT, "USDT"),
                         (10.0, 160000, "USD"))
        self.assertEqual(parse_convert_amount("50k", HARGA_SRC, KURS_USDT, "BNB"),
                         (50000 / HARGA_SRC, 50000, "IDR"))
        self.assertEqual(parse_convert_amount("50.000", HARGA_SRC, KURS_USDT, "BNB"),
                         (50000 / HARGA_SRC, 50000, "IDR"))
        self.assertEqual(parse_convert_amount("Rp 50000", HARGA_SRC, KURS_USDT, "BNB"),
                         (50000 / HARGA_SRC, 50000, "IDR"))
        self.assertEqual(parse_convert_amount("0.5", HARGA_SRC, KURS_USDT, "BNB"),
                         (0.5, 8000, "CRYPTO"))

    def test_nan_dan_inf_wajib_raise(self):
        for raw in ("nan", "inf", "infinity", "-inf"):
            with self.subTest(raw=raw):
                with self.assertRaises((ValueError, OverflowError)):
                    parse_convert_amount(raw, HARGA_SRC, KURS_USDT, "USDT")

    @unittest.expectedFailure
    def test_notasi_ilmiah_ditolak(self):
        """"1e3" bukan format nominal yang boleh diterima (ambigu: 1000 koin atau Rp 1.000?)."""
        with self.assertRaises(ValueError):
            parse_convert_amount("1e3", HARGA_SRC, KURS_USDT, "BNB")

    @unittest.expectedFailure
    def test_titik_desimal_koin_usd_tidak_jadi_rupiah(self):
        """User USDT mengetik "1.000" (artinya 1 koin) — saat ini dibaca Rp 1.000 (IDR)."""
        _, _, mode = parse_convert_amount("1.000", HARGA_SRC, KURS_USDT, "USDT")
        self.assertEqual(
            mode, "CRYPTO",
            "'1.000' pada koin USD harus dianggap jumlah koin (CRYPTO), bukan Rp 1.000",
        )


class TestConfirmSwapHardening(unittest.IsolatedAsyncioTestCase):
    """confirm_swap_order: invariant & harga harus divalidasi ulang server-side."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _user_data(self, src_sym="USDT", src_net="BSC", tgt_sym="USDT", tgt_net="POLYGON"):
        return {
            "swap_src_symbol": src_sym,
            "swap_src_network": src_net,
            "swap_src_amount": 10.0,
            "swap_tgt_symbol": tgt_sym,
            "swap_tgt_network": tgt_net,
            "swap_tgt_amount": 9.9,
            "swap_nominal_idr": 160000,
            "swap_fee_idr": 6000,
            "swap_target_addr": "0x" + "3" * 40,
            "swap_seller_deposit_wallet": "0x" + "1" * 40,
        }

    async def _konfirmasi(self, user_data):
        query = AsyncMock()
        query.from_user = SimpleNamespace(id=777, username="u", full_name="U")
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(user_data=user_data, bot=AsyncMock())
        with patch("services.wallet_sync.sync_wallet_balances", new=AsyncMock()), \
             patch("database.crud.get_available_inventory", return_value=Decimal("999")), \
             patch("bot.handlers.swap.SessionLocal", side_effect=lambda: SessionLocal()):
            await confirm_swap_order(update, context)
        return query

    async def test_stok_kurang_ditolak_dan_tanpa_order(self):
        query = AsyncMock()
        query.from_user = SimpleNamespace(id=777, username="u", full_name="U")
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(user_data=self._user_data(), bot=AsyncMock())
        with patch("services.wallet_sync.sync_wallet_balances", new=AsyncMock()), \
             patch("database.crud.get_available_inventory", return_value=Decimal("0.0001")), \
             patch("bot.handlers.swap.SessionLocal", side_effect=lambda: SessionLocal()):
            await confirm_swap_order(update, context)
        teks = query.edit_message_text.call_args.args[0] if query.edit_message_text.call_args.args else ""
        self.assertIn("Stok tujuan tidak cukup", teks)
        self.assertEqual(self.db.query(Order).count(), 0)

    async def test_src_tgt_sama_persis_ditolak(self):
        """Callback bisa datang dari pesan lama/forward: server wajib menolak
        src == tgt pada koin DAN jaringan yang sama, bukan hanya UI menyembunyikannya."""
        await self._konfirmasi(self._user_data(tgt_sym="USDT", tgt_net="BSC"))
        self.assertEqual(
            self.db.query(Order).count(), 0,
            "convert src == tgt (USDT BSC -> USDT BSC) harus ditolak server-side",
        )

    async def test_konfirmasi_wajib_segarkan_harga(self):
        """Quote dibekukan saat input nominal (bisa ditahan berjam-jam karena tidak
        ada timeout percakapan). Konfirmasi harus memvalidasi ulang kesegaran harga."""
        import time
        user_data = self._user_data()
        user_data.update(swap_quoted_at=time.time() - 3600, swap_src_price=16000, swap_tgt_price=16000)
        with patch("services.price_service.PriceService.get_price", new=AsyncMock(return_value={
            "symbol": "USDT", "market_price_idr": 17000, "buy_price_idr": 17000, "sell_price_idr": 17000,
            "source": "MOCK", "price_updated_at": 0, "spread_pct": 0,
        })) as get_price:
            await self._konfirmasi(user_data)
        self.assertGreaterEqual(
            get_price.await_count, 1,
            "confirm_swap_order harus memeriksa harga terkini, bukan memakai quote beku user_data",
        )
        # Harga bergerak 6% > 0,5% → order tidak dibuat.
        self.assertEqual(self.db.query(Order).count(), 0)

    async def test_quote_basi_tapi_harga_stabil_tetap_jalan(self):
        import time
        user_data = self._user_data()
        user_data.update(swap_quoted_at=time.time() - 3600, swap_src_price=16000, swap_tgt_price=16000)
        with patch("services.price_service.PriceService.get_price", new=AsyncMock(return_value={
            "symbol": "USDT", "market_price_idr": 16010, "source": "MOCK", "price_updated_at": 0,
        })):
            await self._konfirmasi(user_data)
        self.assertEqual(self.db.query(Order).count(), 1)


class TestTargetAddressSelfDealing(unittest.IsolatedAsyncioTestCase):
    """Alamat tujuan convert = hot wallet sendiri harus ditolak."""

    class _FakeSender:
        def __init__(self, wallet):
            self.wallet_address = wallet

        def validate_address(self, address):
            return True

    async def test_target_alamat_hot_wallet_ditolak(self):
        hot = "0x" + "1" * 40
        message = AsyncMock()
        message.text = hot
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=777))
        context = SimpleNamespace(user_data={
            "swap_src_symbol": "USDT",
            "swap_src_network": "BSC",
            "swap_src_amount": 10.0,
            "swap_tgt_symbol": "USDC",
            "swap_tgt_network": "BSC",
            "swap_tgt_amount": 9.9,
            "swap_nominal_idr": 160000,
            "swap_fee_idr": 6000,
        })
        with patch("bot.handlers.swap.CryptoSenderFactory") as factory:
            factory.get_sender.side_effect = lambda net: self._FakeSender(hot)
            state = await input_target_addr(update, context)
        self.assertNotEqual(
            state, CONFIRM_SWAP,
            "alamat tujuan = hot wallet bot sendiri harus ditolak sebelum konfirmasi",
        )


if __name__ == "__main__":
    unittest.main()
