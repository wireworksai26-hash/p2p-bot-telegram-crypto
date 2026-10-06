"""
services/fee_service.py — Triple-Tier Fee Engine (USD, Altcoin, & Convert)
==========================================================================
Menghitung biaya transaksi IDR sesuai aturan tier resmi dari client
(update 1 Okt 2026):

1. USD Fee Tier (USDT/USDC/USDG): Min Rp 5.000.
2. Altcoin Fee Tier: Min Rp 5.000.
3. Convert Fee Tier: Min Rp 6.000.
4. Di atas tier fixed, fee memakai persen (tanpa batas atas):
   ALTCOIN/CONVERT: 3% (1.010.001-2jt), 2,5% (2jt-3,5jt), 2% (3,5jt-8,5jt), 1,5% (>8,5jt)
   USD: 2% (1.015.001-3,6jt), 1,5% (>3,6jt)
   Fee persen dibulatkan ke bawah (int()).
5. Jual altcoin (bukan USD) kena tambahan flat Rp 500 untuk nominal < Rp 1.010.000.
6. Pasangan gas mahal (ETH-ETH, TRX-TRON, USDT-ETH, USDC-ETH):
   - surcharge kirim Rp 2.500 (hanya saat bot mengirim koin: Beli/Convert target),
   - minimum transaksi Rp 7.500 untuk semua jenis transaksi.

Lompatan/penurunan fee di batas tier adalah keputusan client (antisipasi
fluktuasi harga coin) — jangan "diperhalus" tanpa persetujuan ulang.
"""

import logging
import math

logger = logging.getLogger(__name__)

USD_FEE_TIERS = [
    (5000, 34000, 3000),
    (34001, 41000, 3500),
    (41001, 67000, 4000),
    (67001, 100000, 4500),
    (100001, 140000, 5000),
    (140001, 180000, 5500),
    (180001, 200000, 6000),
    (200001, 245000, 6500),
    (245001, 330000, 7000),
    (330001, 400000, 7500),
    (400001, 420000, 8000),
    (420001, 550000, 8500),
    (550001, 680000, 9000),
    (680001, 875000, 11000),
    (875001, 950000, 13000),
    (950001, 1015000, 14500),
]

ALTCOIN_FEE_TIERS = [
    (5000, 10000, 3000),
    (10001, 15000, 3500),
    (15001, 44000, 4000),
    (44001, 49000, 4400),
    (49001, 93000, 5000),
    (93001, 105000, 5500),
    (105001, 110000, 6000),
    (110001, 119000, 6500),
    (119001, 150000, 7000),
    (150001, 185000, 7500),
    (185001, 220000, 8000),
    (220001, 300000, 8500),
    (300001, 330000, 9000),
    (330001, 380000, 9500),
    (380001, 420000, 10000),
    (420001, 460000, 10500),
    (460001, 500000, 11000),
    (500001, 600000, 11500),
    (600001, 690000, 12000),
    (690001, 770000, 12500),
    (770001, 840000, 13500),
    (840001, 890000, 14000),
    (890001, 940000, 17000),
    (940001, 1010000, 19000),
]

CONVERT_FEE_TIERS = [
    (6000, 10000, 3500),
    (10001, 19000, 4000),
    (19001, 47000, 4500),
    (47001, 98000, 5500),
    (98001, 109000, 6000),
    (109001, 119000, 6500),
    (119001, 135000, 7000),
    (135001, 165000, 7500),
    (165001, 198000, 8000),
    (198001, 260000, 8500),
    (260001, 350000, 9000),
    (350001, 390000, 9500),
    (390001, 425000, 10500),
    (425001, 475000, 11000),
    (475001, 600000, 11500),
    (600001, 680000, 12000),
    (680001, 760000, 12500),
    (760001, 830000, 13500),
    (830001, 880000, 14000),
    (880001, 940000, 16000),
    (940001, 1010000, 18000),
]

# Tier persen di atas tier fixed (batas sambung: fixed terakhir + 1).
USD_PERCENT_TIERS = [
    (1_015_001, 3_600_000, 2.0),
    (3_600_001, None, 1.5),
]
ALTCOIN_PERCENT_TIERS = [
    (1_010_001, 2_000_000, 3.0),
    (2_000_001, 3_500_000, 2.5),
    (3_500_001, 8_500_000, 2.0),
    (8_500_001, None, 1.5),
]
CONVERT_PERCENT_TIERS = ALTCOIN_PERCENT_TIERS

# Surcharge gas untuk pasangan coin/jaringan dengan biaya kirim mahal.
GAS_SURCHARGE_IDR = 2500
GAS_SURCHARGE_PAIRS = {
    ("ETH", "ETH"),
    ("TRX", "TRON"),
    ("USDT", "ETH"),
    ("USDC", "ETH"),
}
GAS_PAIR_MIN_IDR = 7500

# Tambahan flat untuk JUAL altcoin (bukan USD), khusus nominal < Rp 1.010.000.
SELL_ALTCOIN_SURCHARGE_IDR = 500
SELL_ALTCOIN_SURCHARGE_BELOW = 1_010_000


# Pajak QRIS GoPay/GoBiz 0,3% untuk pembayaran QRIS nominal > Rp 500.000.
QRIS_MDR_PCT = 0.3
QRIS_MDR_THRESHOLD = 500_000


def calculate_qris_mdr(nominal_idr: int) -> int:
    """Pajak QRIS 0,3% (pembulatan ke atas) — 0 bila nominal <= Rp 500.000."""
    if nominal_idr <= QRIS_MDR_THRESHOLD:
        return 0
    return math.ceil(nominal_idr * QRIS_MDR_PCT / 100)


def qris_mdr_note(nominal_idr: int) -> str:
    """Keterangan pajak QRIS untuk pesan; string kosong bila tidak kena."""
    mdr = calculate_qris_mdr(nominal_idr)
    if not mdr:
        return ""
    rupiah = f"{mdr:,}".replace(",", ".")
    return f"\n🧾 <i>Pajak QRIS 0,3% (nominal di atas Rp 500.000): +Rp {rupiah}.</i>"


def is_gas_pair(symbol: str, network: str) -> bool:
    """True bila (coin, jaringan) termasuk pasangan gas mahal."""
    return ((symbol or "").upper(), (network or "").upper()) in GAS_SURCHARGE_PAIRS


def gas_surcharge_note(symbol: str, network: str) -> str:
    """Keterangan surcharge gas untuk pesan; string kosong bila tidak kena."""
    if not is_gas_pair(symbol, network):
        return ""
    surcharge = f"{GAS_SURCHARGE_IDR:,}".replace(",", ".")
    return f"\n⛽ <i>Fee sudah termasuk tambahan Rp {surcharge} untuk biaya gas pengiriman.</i>"


def _percent_fee(nominal_idr: int, tiers: list) -> int:
    """Fee persen (dibulatkan ke bawah) dari tier (min, max|None, pct)."""
    for min_val, max_val, pct in tiers:
        if nominal_idr >= min_val and (max_val is None or nominal_idr <= max_val):
            return int(nominal_idr * pct / 100)
    raise ValueError(f"Nominal Rp {nominal_idr:,} tidak masuk tier fee mana pun.")


def _fixed_fee(nominal_idr: int, tiers: list) -> int | None:
    """Fee fixed dari tier (min, max, fee); None bila di atas tier terakhir."""
    for min_val, max_val, fee in tiers:
        if min_val <= nominal_idr <= max_val:
            return fee
    return None


def calculate_fee_idr(
    nominal_idr: int,
    category: str = "ALTCOIN",
    symbol: str = None,
    network: str = None,
    is_outgoing: bool = True
) -> int:
    """
    Menghitung fee IDR berdasarkan nominal dan kategori transaksi.

    Args:
        nominal_idr (int): Nominal transaksi dalam Rupiah.
        category (str): Kategori fee: 'USD', 'ALTCOIN', atau 'CONVERT'.
        symbol (str, optional): Simbol koin (e.g. 'ETH', 'TRX', 'USDT').
        network (str, optional): Jaringan blockchain (e.g. 'ETH', 'TRON').
        is_outgoing (bool, optional): True jika bot mengirim koin ke buyer (Beli / Convert target).
                                      False jika buyer mengirim koin ke bot (Jual).

    Returns:
        int: Fee dalam Rupiah.
    """
    category_upper = category.upper()
    gas_pair = is_gas_pair(symbol, network)

    if gas_pair:
        min_nominal, min_note = GAS_PAIR_MIN_IDR, " (pasangan gas ETH/TRON)"
    elif category_upper == "CONVERT":
        min_nominal, min_note = 6000, ""
    else:
        min_nominal, min_note = 5000, ""

    if nominal_idr < min_nominal:
        raise ValueError(
            f"Minimum transaksi {category_upper} adalah Rp {min_nominal:,}{min_note}"
        )

    if category_upper == "USD":
        fixed, percent = USD_FEE_TIERS, USD_PERCENT_TIERS
    elif category_upper == "CONVERT":
        fixed, percent = CONVERT_FEE_TIERS, CONVERT_PERCENT_TIERS
    else:  # Default: ALTCOIN
        fixed, percent = ALTCOIN_FEE_TIERS, ALTCOIN_PERCENT_TIERS

    base_fee = _fixed_fee(nominal_idr, fixed)
    if base_fee is None:
        base_fee = _percent_fee(nominal_idr, percent)

    # Surcharge gas: hanya saat bot mengirim koin keluar (Beli / Convert target).
    if gas_pair and is_outgoing:
        base_fee += GAS_SURCHARGE_IDR

    # Tambahan flat jual altcoin (bukan USD), nominal di bawah batas tier persen.
    if (not is_outgoing) and category_upper == "ALTCOIN" and nominal_idr < SELL_ALTCOIN_SURCHARGE_BELOW:
        base_fee += SELL_ALTCOIN_SURCHARGE_IDR

    return base_fee


def get_fee_category(symbol: str) -> str:
    """
    Menentukan kategori fee berdasarkan simbol koin.
    USDT, USDC, dan USDG -> 'USD' (list lebih murah), selainnya -> 'ALTCOIN'.
    """
    sym_upper = symbol.upper()
    if sym_upper in ["USDT", "USDC", "USDG"]:
        return "USD"
    return "ALTCOIN"
