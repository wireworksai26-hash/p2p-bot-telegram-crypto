"""Uji update fee client (update Price List & Fee):
- Tier persen di atas 1.030k / 1.035k (3% / 2,5% untuk Altcoin & Convert, 2,3% / 2% untuk USD)
  hingga maksimal Rp 5.000.000.
- Transaksi di atas Rp 5.000.000 ditolak (ValueError diarahkan chat admin).
- Penghapusan flat surcharge Rp 500 untuk JUAL altcoin (sudah tidak ada).
- Surcharge gas Rp 2.500 (beli/convert target) + minimum Rp 7.500 untuk 4 pasangan:
  ETH-ETH, TRX-TRON, USDT-ETH, USDC-ETH.
- Minimum convert sekarang Rp 5.000 (sama dengan altcoin & USD).
"""
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

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

from services.fee_service import (
    GAS_SURCHARGE_IDR,
    GAS_SURCHARGE_PAIRS,
    MAX_TRANSACTION_IDR,
    calculate_fee_idr,
    gas_surcharge_note,
    get_fee_category,
    is_gas_pair,
)
from database.connection import Base, engine, SessionLocal
from database.models import Order, PriceConfig
from database import crud


class TestTierPersen(unittest.TestCase):
    """Fee persen di atas tier fixed — batas sambung, pembulatan ke bawah, max 5M."""

    def test_altcoin_persen(self):
        self.assertEqual(calculate_fee_idr(1_030_001, "ALTCOIN"), 30_900)   # 3%
        self.assertEqual(calculate_fee_idr(3_100_000, "ALTCOIN"), 93_000)   # 3%
        self.assertEqual(calculate_fee_idr(3_100_001, "ALTCOIN"), 77_500)   # 2,5%
        self.assertEqual(calculate_fee_idr(5_000_000, "ALTCOIN"), 125_000)  # 2,5%

    def test_usd_persen(self):
        self.assertEqual(calculate_fee_idr(1_035_001, "USD"), 23_805)   # 2,3%
        self.assertEqual(calculate_fee_idr(3_800_000, "USD"), 87_400)   # 2,3%
        self.assertEqual(calculate_fee_idr(3_800_001, "USD"), 76_000)   # 2%
        self.assertEqual(calculate_fee_idr(5_000_000, "USD"), 100_000)  # 2%

    def test_convert_persen(self):
        self.assertEqual(calculate_fee_idr(1_030_001, "CONVERT"), 30_900)   # 3%
        self.assertEqual(calculate_fee_idr(3_100_000, "CONVERT"), 93_000)   # 3%
        self.assertEqual(calculate_fee_idr(3_100_001, "CONVERT"), 77_500)   # 2,5%
        self.assertEqual(calculate_fee_idr(5_000_000, "CONVERT"), 125_000)  # 2,5%

    def test_maksimal_5_juta(self):
        # Transaksi di atas Rp 5.000.000 ditolak dan diarahkan chat admin
        with self.assertRaises(ValueError) as ctx:
            calculate_fee_idr(5_000_001, "ALTCOIN")
        self.assertIn("5.000.000", str(ctx.exception))
        self.assertIn("admin", str(ctx.exception).lower())

        with self.assertRaises(ValueError):
            calculate_fee_idr(10_000_000, "USD")


class TestNoSellAltcoinSurcharge(unittest.TestCase):
    """Surcharge flat Rp 500 untuk JUAL altcoin telah dihapus."""

    def test_jual_altcoin_tanpa_tambahan_500(self):
        # Tier 55.001 - 105.000 fee adalah 5.000 (tidak ada +500)
        fee = calculate_fee_idr(105_000, "ALTCOIN", "SOL", "SOLANA", is_outgoing=False)
        self.assertEqual(fee, 5_000)
        self.assertEqual(105_000 - fee, 100_000)

        # 105.001 masuk tier 105.001 - 110.000 fee 5.500
        self.assertEqual(
            calculate_fee_idr(105_001, "ALTCOIN", "SOL", "SOLANA", is_outgoing=False), 5_500)

        # Nominal 50.000 (tier 47.001-55k fee 4.500)
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "ETH", "BASE", is_outgoing=False), 4_500)
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "ETH", "BASE", is_outgoing=True), 4_500)


class TestGasPair(unittest.TestCase):
    """Surcharge gas 2.500 (beli/convert) + minimum 7.500 untuk 4 pasangan."""

    def test_daftar_pasangan(self):
        self.assertEqual(GAS_SURCHARGE_IDR, 2500)
        self.assertEqual(GAS_SURCHARGE_PAIRS, {
            ("ETH", "ETH"), ("TRX", "TRON"), ("USDT", "ETH"),
            ("USDC", "ETH"),
        })
        self.assertFalse(is_gas_pair("USDT", "TRON"))
        self.assertFalse(is_gas_pair("USDT", "BSC"))

    def test_surcharge_beli_convert(self):
        # 50.000 ALTCOIN (47.001-55k) fee dasar 4.500 + gas 2.500 = 7.000
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "ETH", "ETH"), 7_000)
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "TRX", "TRON"), 7_000)

        # 50.000 USD (34.001-50k) fee dasar 3.500 + gas 2.500 = 6.000
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDT", "ETH"), 6_000)
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDC", "ETH"), 6_000)
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDT", "TRON"), 3_500)  # bukan pasangan gas

        # 50.000 CONVERT (47.001-55k) fee dasar 4.500 + gas 2.500 = 7.000
        self.assertEqual(calculate_fee_idr(50_000, "CONVERT", "ETH", "ETH"), 7_000)

    def test_jual_tidak_kena_surcharge_gas(self):
        # Jual ETH di jaringan ETH: TANPA surcharge gas kirim (is_outgoing=False)
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "ETH", "ETH", is_outgoing=False), 4_500)

    def test_pasangan_lain_tidak_kena(self):
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "ETH", "ARB"), 4_500)
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDT", "BSC"), 3_500)
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDC", "BASE"), 3_500)

    def test_minimum_7500_pasangan_gas(self):
        for kategori, sym, net in (
            ("ALTCOIN", "ETH", "ETH"), ("ALTCOIN", "TRX", "TRON"),
            ("USD", "USDT", "ETH"), ("USD", "USDC", "ETH"),
            ("CONVERT", "ETH", "ETH"),
        ):
            with self.subTest(sym=sym, net=net):
                with self.assertRaises(ValueError):
                    calculate_fee_idr(7_499, kategori, sym, net)
                calculate_fee_idr(7_500, kategori, sym, net)  # tidak raise

    def test_minimum_biasa_tetap(self):
        with self.assertRaises(ValueError):
            calculate_fee_idr(4_999, "ALTCOIN", "SOL", "SOLANA")
        with self.assertRaises(ValueError):
            calculate_fee_idr(4_999, "USD", "USDT", "BSC")
        with self.assertRaises(ValueError):
            calculate_fee_idr(4_999, "CONVERT", "ETH", "BASE")
        # 5000 kini valid untuk convert juga
        calculate_fee_idr(5_000, "CONVERT", "ETH", "BASE")

    def test_note_hanya_untuk_pasangan_gas(self):
        self.assertIn("2.500", gas_surcharge_note("TRX", "TRON"))
        self.assertEqual(gas_surcharge_note("USDT", "TRON"), "")
        self.assertEqual(gas_surcharge_note("USDT", "BSC"), "")
        self.assertEqual(gas_surcharge_note("SOL", "SOLANA"), "")


class TestHandlerKategoriDanSpread(unittest.TestCase):
    """Kategori USD lebih murah + default spread 0.0%."""

    def test_kategori(self):
        self.assertEqual(get_fee_category("USDT"), "USD")
        self.assertEqual(get_fee_category("USDC"), "USD")
        self.assertEqual(get_fee_category("USDG"), "USD")
        self.assertEqual(get_fee_category("ETH"), "ALTCOIN")
        # USDT lebih murah dari altcoin di nominal sama
        self.assertLess(calculate_fee_idr(100_000, "USD"), calculate_fee_idr(100_000, "ALTCOIN"))

    def test_default_spread_0(self):
        from config.settings import settings
        self.assertEqual(settings.DEFAULT_SPREAD_PCT, 0.0)
        default = PriceConfig.__table__.c.spread_pct.default
        self.assertEqual(float(default.arg), 0.0)


class TestKodeUnikQris(unittest.TestCase):
    """Kode unik QRIS 001-400 (dari 1-200)."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _order(self, oid, code):
        return Order(
            order_id=oid, telegram_id=1, order_type="buy", crypto_symbol="USDT",
            network="BSC", crypto_amount=Decimal("1"), price_per_unit=16000,
            nominal_idr=50000, fee_idr=3000, total_idr=50000 + code,
            unique_code=code, status="pending",
        )

    def test_default_max_400(self):
        import inspect
        sig = inspect.signature(crud.generate_unique_payment_code)
        self.assertEqual(sig.parameters["max_code"].default, 400)

    def test_pool_penuh_ditolak_bukan_kode_kembar(self):
        # Pool 1..400 penuh → None (K1): kode kembar = nominal kembar = pembayaran salah klaim.
        for c in range(1, 401):
            self.db.add(self._order(f"ORD-{c:03d}", c))
        self.db.commit()
        self.assertIsNone(crud.generate_unique_payment_code(self.db))

    def test_kode_pending_tidak_dipakai_ulang(self):
        for c in range(1, 400):
            self.db.add(self._order(f"ORD-{c:03d}", c))
        self.db.commit()
        self.assertEqual(crud.generate_unique_payment_code(self.db), 400)


if __name__ == "__main__":
    unittest.main()
