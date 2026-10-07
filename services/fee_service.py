"""
services/fee_service.py — Triple-Tier Fee Engine (USD, Altcoin, & Convert)
==========================================================================
Menghitung biaya transaksi IDR sesuai aturan tier resmi dari client:

1. USD Fee Tier (USDT/USDC/USDG): Min Rp 5.000, Max Rp 5.000.000.
2. Altcoin Fee Tier: Min Rp 5.000, Max Rp 5.000.000.
3. Convert Fee Tier: Min Rp 5.000, Max Rp 5.000.000.
4. Di atas tier fixed, fee memakai persen (maksimal Rp 5.000.000):
   ALTCOIN/CONVERT: 3% (1.030.001 - 3.100.000), 2,5% (3.100.001 - 5.000.000)
   USD: 2.3% (1.035.001 - 3.800.000), 2% (3.800.001 - 5.000.000)
   Fee persen dibulatkan ke bawah (int()).
5. Batas transaksi maksimal adalah Rp 5.000.000 (di atas itu chat admin).
6. Pasangan gas mahal (ETH-ETH, TRX-TRON, USDT-ETH, USDC-ETH):
   - surcharge kirim Rp 2.500 (hanya saat bot mengirim koin: Beli/Convert target),
   - minimum transaksi Rp 7.500 untuk semua jenis transaksi.

Lompatan/penurunan fee di batas tier adalah keputusan client (antisipasi
fluktuasi harga coin) — jangan "diperhalus" tanpa persetujuan ulang.
"""

import logging
import math

logger = logging.getLogger(__name__)

# Batas maksimal transaksi di bot (di atas itu chat admin)
MAX_TRANSACTION_IDR = 5_000_000

USD_FEE_TIERS = [
    (5000, 34000, 3000),
    (34001, 50000, 3500),
    (50001, 76000, 4000),
    (76001, 100000, 4500),
    (100001, 150000, 5000),
    (150001, 180000, 5500),
    (180001, 200000, 6000),
    (200001, 245000, 6500),
    (245001, 310000, 7000),
    (310001, 360000, 7500),
    (360001, 405000, 8000),
    (405001, 480000, 8500),
    (480001, 580000, 9000),
    (580001, 680000, 9500),
    (680001, 735000, 11000),
    (735001, 875000, 12000),
    (875001, 950000, 14000),
    (950001, 1010000, 15500),
    (1010001, 1035000, 18000),
]

ALTCOIN_FEE_TIERS = [
    (5000, 12000, 3000),
    (12001, 18000, 3500),
    (18001, 47000, 4000),
    (47001, 55000, 4500),
    (55001, 105000, 5000),
    (105001, 110000, 5500),
    (110001, 119000, 6000),
    (119001, 150000, 7000),
    (150001, 220000, 7500),
    (220001, 300000, 8500),
    (300001, 330000, 9000),
    (330001, 380000, 9500),
    (380001, 460000, 10500),
    (460001, 500000, 11000),
    (500001, 600000, 11500),
    (600001, 700000, 12500),
    (700001, 770000, 13500),
    (770001, 840000, 15500),
    (840001, 890000, 16000),
    (890001, 940000, 18000),
    (940001, 1010000, 20500),
    (1010001, 1030000, 23000),
]

CONVERT_FEE_TIERS = [
    (5000, 10000, 3000),
    (10001, 18000, 3500),
    (18001, 47000, 4000),
    (47001, 55000, 4500),
    (55001, 105000, 5000),
    (105001, 110000, 5500),
    (110001, 119000, 6500),
    (119001, 135000, 7000),
    (135001, 165000, 7500),
    (165001, 198000, 8000),
    (198001, 280000, 8500),
    (280001, 300000, 9000),
    (300001, 350000, 9500),
    (350001, 425000, 11000),
    (425001, 600000, 12500),
    (600001, 710000, 14500),
    (710001, 810000, 17500),
    (810001, 920000, 19500),
    (920001, 1010000, 22000),
    (1010001, 1030000, 25000),
]

# Tier persen di atas tier fixed (batas sambung: fixed terakhir + 1). Maksimal Rp 5.000.000.
USD_PERCENT_TIERS = [
    (1_035_001, 3_800_000, 2.3),
    (3_800_001, 5_000_000, 2.0),
]
ALTCOIN_PERCENT_TIERS = [
    (1_030_001, 3_100_000, 3.0),
    (3_100_001, 5_000_000, 2.5),
]
CONVERT_PERCENT_TIERS = [
    (1_030_001, 3_100_000, 3.0),
    (3_100_001, 5_000_000, 2.5),
]

# Surcharge gas untuk pasangan coin/jaringan dengan biaya kirim mahal.
GAS_SURCHARGE_IDR = 2500
GAS_SURCHARGE_PAIRS = {
    ("ETH", "ETH"),
    ("TRX", "TRON"),
    ("USDT", "ETH"),
    ("USDC", "ETH"),
}
GAS_PAIR_MIN_IDR = 7500


# Pajak QRIS GoPay/GoBiz 0,3% untuk pembayaran QRIS nominal > Rp 500.000.
QRIS_MDR_PCT = 0.3
QRIS_MDR_THRESHOLD = 500_000


def calculate_qris_mdr(nominal_idr: int) -> int:
    """Pajak QRIS 0,3% (pembulatan ke atas) — 0 bila nominal <= Rp 500.000."""
    if nominal_idr <= QRIS_MDR_THRESHOLD:
        return 0
    return math.ceil(nominal_idr * QRIS_MDR_PCT / 100)


QRIS_MAX_TOTAL_IDR = 10_000_000  # batas transaksi QRIS (BI)
QRIS_MAX_UNIQUE_CODE = 400


def qris_max_nominal() -> int:
    """Nominal terbesar yang total tagihannya (nominal + pajak QRIS + kode unik) masih
    di bawah batas QRIS. Di atas itu QRIS dinamis tidak bisa dibuat."""
    lo, hi = 0, QRIS_MAX_TOTAL_IDR
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if mid + calculate_qris_mdr(mid) + QRIS_MAX_UNIQUE_CODE <= QRIS_MAX_TOTAL_IDR:
            lo = mid
        else:
            hi = mid - 1
    return lo


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
    else:
        min_nominal, min_note = 5000, ""

    if nominal_idr < min_nominal:
        raise ValueError(
            f"Minimum transaksi {category_upper} adalah Rp {min_nominal:,}{min_note}"
        )

    if nominal_idr > MAX_TRANSACTION_IDR:
        raise ValueError(
            "Batas transaksi maksimal adalah Rp 5.000.000. "
            "Untuk transaksi di atas Rp 5.000.000, silakan hubungi admin."
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

    return base_fee


def gross_for_net_idr(
    net_idr: int,
    category: str = "ALTCOIN",
    symbol: str = None,
    network: str = None,
    is_outgoing: bool = False,
    min_gross: int = 0,
) -> int:
    """
    Nominal kotor terkecil (>= min_gross) yang setelah dipotong fee tetap menyisakan
    minimal `net_idr`. Dipakai Jual mode Rupiah: user ketik 50.000 = yang masuk rekening
    50.000, fee ditambahkan di atasnya (bukan dipotong dari 50.000).

    Fee berupa tangga (bisa naik/turun di batas tier), jadi dicari iteratif dari bawah:
    selisih kekurangan ditambahkan ke gross sampai net tercapai.
    Raise ValueError (dari calculate_fee_idr) bila gross di bawah minimum / di atas batas.
    """
    gross = max(int(net_idr) + 3000, int(min_gross))  # 3000 = fee fixed terkecil
    for _ in range(200):
        fee = calculate_fee_idr(gross, category=category, symbol=symbol, network=network,
                                is_outgoing=is_outgoing)
        shortfall = int(net_idr) - (gross - fee)
        if shortfall <= 0:
            return gross
        gross += shortfall
    raise ValueError("Nominal tidak dapat dihitung. Silakan coba nominal lain.")


def get_fee_category(symbol: str) -> str:
    """
    Menentukan kategori fee berdasarkan simbol koin.
    USDT, USDC, dan USDG -> 'USD' (list lebih murah), selainnya -> 'ALTCOIN'.
    """
    sym_upper = symbol.upper()
    if sym_upper in ["USDT", "USDC", "USDG"]:
        return "USD"
    return "ALTCOIN"
