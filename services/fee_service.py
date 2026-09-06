"""
services/fee_service.py — Triple-Tier Fee Engine (USD, Altcoin, & Convert)
==========================================================================
Menghitung biaya transaksi IDR fixed sesuai dengan aturan tier resmi dari client:
1. USD Fee Tier (USDT & USDC): Min Rp 5.000, Max Rp 1.015.000
2. Altcoin Fee Tier (ETH, SOL, SUI, TRX, BNB, MATIC, ARB, AVAX, KAIA, BERA, APT, TON, HYPE): Min Rp 5.000, Max Rp 1.010.000
3. Convert Fee Tier (Tukar koin antar jaringan): Min Rp 6.000, Max Rp 1.010.000
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


def _tier_fee(nominal_idr: int, tiers: list, category: str, min_nominal: int, max_nominal: int) -> int:
    """Cari fee fixed dari list tier (min, max, fee). Raise jika di luar rentang."""
    if nominal_idr < min_nominal:
        raise ValueError(f"Minimum transaksi {category} adalah Rp {min_nominal:,}")
    for min_val, max_val, fee in tiers:
        if min_val <= nominal_idr <= max_val:
            return fee
    raise ValueError(
        f"Pembelian atau Penjualan Nominal {category} di atas list (Rp {max_nominal:,}) tanya admin dahulu."
    )


def calculate_fee_idr(
    nominal_idr: int,
    category: str = "ALTCOIN",
    symbol: str = None,
    network: str = None,
    is_outgoing: bool = True
) -> int:
    """
    Menghitung fee fixed IDR berdasarkan nominal dan kategori transaksi.
    
    Args:
        nominal_idr (int): Nominal transaksi dalam Rupiah.
        category (str): Kategori fee: 'USD', 'ALTCOIN', atau 'CONVERT'.
        symbol (str, optional): Simbol koin (e.g. 'ETH', 'TRX', 'USDT').
        network (str, optional): Jaringan blockchain (e.g. 'ETH', 'TRON').
        is_outgoing (bool, optional): True jika bot mengirim koin ke buyer (Beli / Convert Target).
                                      False jika buyer mengirim koin ke bot (Jual / Convert Source).

    Returns:
        int: Fee fixed dalam Rupiah.
    """
    category_upper = category.upper()

    if category_upper == "USD":
        if nominal_idr > 1_015_000:
            raise ValueError("Pembelian atau Penjualan Nominal USD di atas Rp 1.015.000 silakan tanya admin dahulu.")
        base_fee = _tier_fee(nominal_idr, USD_FEE_TIERS, "USD (USDT/USDC)", 5000, 1015000)
    elif category_upper == "CONVERT":
        if nominal_idr > 1_010_000:
            raise ValueError("Transaksi Convert di atas Rp 1.010.000 silakan tanya admin dahulu.")
        base_fee = _tier_fee(nominal_idr, CONVERT_FEE_TIERS, "Convert", 6000, 1010000)
    else:  # Default: ALTCOIN
        if nominal_idr > 1_010_000:
            raise ValueError("Pembelian atau Penjualan Nominal Altcoin di atas Rp 1.010.000 silakan tanya admin dahulu.")
        base_fee = _tier_fee(nominal_idr, ALTCOIN_FEE_TIERS, "Altcoin", 5000, 1010000)

    return base_fee


def get_fee_category(symbol: str) -> str:
    """
    Menentukan kategori fee berdasarkan simbol koin.
    USDT dan USDC -> 'USD', selainnya -> 'ALTCOIN'.
    """
    sym_upper = symbol.upper()
    if sym_upper in ["USDT", "USDC"]:
        return "USD"
    return "ALTCOIN"
