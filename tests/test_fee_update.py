"""Uji update fee client 1 Okt 2026:
- Tier persen di atas 1.010k/1.015k (3% / 2,5% / 2% / 1,5%) — tanpa max-cap.
- Tambahan flat Rp 500 untuk JUAL altcoin nominal < Rp 1.010.000.
- Surcharge gas Rp 2.500 (beli/convert) + minimum Rp 7.500 untuk 4 pasangan:
  ETH-ETH, TRX-TRON, USDT-ETH, USDC-ETH.
- Contoh client: jual 105k altcoin -> fee 6.000 -> net 99.000.
- Kode unik QRIS 001-400 + default spread 0.5%.
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
    calculate_fee_idr,
    gas_surcharge_note,
    get_fee_category,
    is_gas_pair,
)
from database.connection import Base, engine, SessionLocal
from database.models import Order, PriceConfig
from database import crud


class TestTierPersen(unittest.TestCase):
    """Fee persen di atas tier fixed — batas sambung, pembulatan ke bawah."""

    def test_altcoin_persen(self):
        self.assertEqual(calculate_fee_idr(1_010_001, "ALTCOIN"), 30_300)   # 3%
        self.assertEqual(calculate_fee_idr(2_000_000, "ALTCOIN"), 60_000)   # 3%
        self.assertEqual(calculate_fee_idr(2_000_001, "ALTCOIN"), 50_000)   # 2,5%
        self.assertEqual(calculate_fee_idr(3_500_000, "ALTCOIN"), 87_500)
        self.assertEqual(calculate_fee_idr(3_500_001, "ALTCOIN"), 70_000)   # 2%
        self.assertEqual(calculate_fee_idr(8_500_000, "ALTCOIN"), 170_000)
        self.assertEqual(calculate_fee_idr(8_500_001, "ALTCOIN"), 127_500)  # 1,5% floor

    def test_usd_persen(self):
        self.assertEqual(calculate_fee_idr(1_015_001, "USD"), 20_300)   # 2%
        self.assertEqual(calculate_fee_idr(3_600_000, "USD"), 72_000)
        self.assertEqual(calculate_fee_idr(3_600_001, "USD"), 54_000)   # 1,5%

    def test_convert_persen(self):
        self.assertEqual(calculate_fee_idr(1_010_001, "CONVERT"), 30_300)
        self.assertEqual(calculate_fee_idr(8_500_001, "CONVERT"), 127_500)

    def test_tanpa_batas_atas(self):
        # Nominal sangat besar tetap terlayani (tanpa raise "tanya admin").
        self.assertEqual(calculate_fee_idr(50_000_000, "ALTCOIN"), 750_000)  # 1,5%


class TestSellAltcoinSurcharge(unittest.TestCase):
    """Tambahan flat Rp 500 khusus JUAL altcoin, nominal < Rp 1.010.000."""

    def test_contoh_client_105k(self):
        fee = calculate_fee_idr(105_000, "ALTCOIN", "SOL", "SOLANA", is_outgoing=False)
        self.assertEqual(fee, 6_000)  # 5.500 tier + 500
        self.assertEqual(105_000 - fee, 99_000)

    def test_jual_altcoin_kena_500(self):
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "ETH", "BASE", is_outgoing=False), 5_500)
        self.assertEqual(calculate_fee_idr(1_009_999, "ALTCOIN", "ETH", "BASE", is_outgoing=False), 19_500)

    def test_batas_1010k_tidak_kena(self):
        self.assertEqual(calculate_fee_idr(1_010_000, "ALTCOIN", "ETH", "BASE", is_outgoing=False), 19_000)
        self.assertEqual(calculate_fee_idr(1_010_001, "ALTCOIN", "ETH", "BASE", is_outgoing=False), 30_300)

    def test_bukan_jual_altcoin_tidak_kena(self):
        # Beli altcoin
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "ETH", "BASE", is_outgoing=True), 5_000)
        # Jual USD (USDT) — tidak kena 500
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDT", "BSC", is_outgoing=False), 4_000)
        # Convert — tidak kena 500
        self.assertEqual(calculate_fee_idr(50_000, "CONVERT", "ETH", "BASE", is_outgoing=True), 5_500)


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
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "ETH", "ETH"), 7_500)        # 5000 + 2500
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "TRX", "TRON"), 7_500)
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDT", "ETH"), 6_500)          # 4000 + 2500
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDC", "ETH"), 6_500)
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDT", "TRON"), 4_000)  # bukan pasangan gas
        self.assertEqual(calculate_fee_idr(50_000, "CONVERT", "ETH", "ETH"), 8_000)       # 5500 + 2500

    def test_jual_tidak_kena_surcharge_gas(self):
        # Jual ETH di jaringan ETH: kategori ALTCOIN + 500 jual, TANPA surcharge gas.
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "ETH", "ETH", is_outgoing=False), 5_500)

    def test_pasangan_lain_tidak_kena(self):
        self.assertEqual(calculate_fee_idr(50_000, "ALTCOIN", "ETH", "ARB"), 5_000)
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDT", "BSC"), 4_000)
        self.assertEqual(calculate_fee_idr(50_000, "USD", "USDC", "BASE"), 4_000)

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
            calculate_fee_idr(5_999, "CONVERT", "ETH", "BASE")

    def test_note_hanya_untuk_pasangan_gas(self):
        self.assertIn("2.500", gas_surcharge_note("TRX", "TRON"))
        self.assertEqual(gas_surcharge_note("USDT", "TRON"), "")
        self.assertEqual(gas_surcharge_note("USDT", "BSC"), "")
        self.assertEqual(gas_surcharge_note("SOL", "SOLANA"), "")


class TestHandlerKategoriDanSpread(unittest.TestCase):
    """Kategori USD lebih murah + default spread 0.5%."""

    def test_kategori(self):
        self.assertEqual(get_fee_category("USDT"), "USD")
        self.assertEqual(get_fee_category("USDC"), "USD")
        self.assertEqual(get_fee_category("USDG"), "USD")
        self.assertEqual(get_fee_category("ETH"), "ALTCOIN")
        # USDT jauh lebih murah dari altcoin di nominal sama
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

    def test_selalu_dalam_rentang_1_400(self):
        # Isi pool penuh 1..400 → fallback pun harus tetap di rentang 1..400.
        for c in range(1, 401):
            self.db.add(self._order(f"ORD-{c:03d}", c))
        self.db.commit()
        kode = crud.generate_unique_payment_code(self.db)
        self.assertGreaterEqual(kode, 1)
        self.assertLessEqual(kode, 400)

    def test_kode_pending_tidak_dipakai_ulang(self):
        for c in range(1, 400):
            self.db.add(self._order(f"ORD-{c:03d}", c))
        self.db.commit()
        self.assertEqual(crud.generate_unique_payment_code(self.db), 400)


if __name__ == "__main__":
    unittest.main()
