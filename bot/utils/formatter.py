"""
bot/utils/formatter.py — Helper formatting untuk P2P Crypto Bot.
===================================================================
Berisi fungsi-fungsi format angka Rupiah, format jumlah cryptocurrency,
format tanggal/waktu ke timezone WIB, dan pembuatan ID Order unik.
"""

import random
import string
from datetime import datetime, timezone, timedelta

def format_idr(amount: int) -> str:
    """
    Format integer ke format mata uang Rupiah.
    Contoh: 500000 -> "Rp 500.000"
    """
    try:
        if amount is None:
            return "Rp 0"
        return f"Rp {int(amount):,}".replace(",", ".")
    except Exception:
        return f"Rp {amount}"


# Token yang sudah rebrand resmi: tampilkan ticker pasar, simbol internal lama tetap dipakai
# untuk harga, saldo, dan record order.
DISPLAY_SYMBOLS = {"TON": "GRAM"}


def display_symbol(symbol: str) -> str:
    """Ticker yang ditampilkan ke user (mis. TON -> GRAM setelah rebrand 15 Juni 2026)."""
    sym = (symbol or "").upper()
    return DISPLAY_SYMBOLS.get(sym, sym)


def format_crypto(amount: float, symbol: str) -> str:
    """
    Format jumlah cryptocurrency dengan presisi yang sesuai.
    Contoh: 30.47 -> "30.4700 USDT"
    """
    try:
        if amount is None:
            return f"0.0000 {display_symbol(symbol)}"
        
        # Atur presisi berdasarkan jenis koin
        sym = symbol.upper()
        if sym in ["USDT", "G", "TON"]:
            precision = 4
        elif sym in ["BNB", "SOL", "AVAX", "POLYGON", "MATIC"]:
            precision = 6
        else: # ETH, BASE, ARB
            precision = 8
            
        formatted_amount = f"{amount:.{precision}f}"
        return f"{formatted_amount} {display_symbol(sym)}"
    except Exception:
        return f"{amount} {symbol}"


def format_datetime(dt: datetime) -> str:
    """
    Format UTC datetime ke format lokal WIB (Waktu Indonesia Barat) UTC+7.
    Contoh: 2026-05-26 05:00:00 -> "26 Mei 2026, 12:00 WIB"
    """
    if dt is None:
        return "-"
        
    try:
        # Konversi ke WIB (UTC+7) jika datetime naive atau UTC
        if dt.tzinfo is None or dt.tzinfo == timezone.utc:
            dt_wib = dt.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=7)))
        else:
            dt_wib = dt.astimezone(timezone(timedelta(hours=7)))

        # Pemetaan nama bulan bahasa Indonesia
        months = {
            1: "Januari", 2: "Februari", 3: "Maret", 4: "April",
            5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus",
            9: "September", 10: "Oktober", 11: "November", 12: "Desember"
        }
        
        day = dt_wib.day
        month = months[dt_wib.month]
        year = dt_wib.year
        time_str = dt_wib.strftime("%H:%M")
        
        return f"{day} {month} {year}, {time_str} WIB"
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def generate_order_id() -> str:
    """
    Generate ID order acak yang unik.
    Format: ORD-YYYYMMDD-XYZ (3 karakter alfanumerik acak di belakang).
    Contoh: ORD-20260526-A3B
    """
    date_str = datetime.now().strftime("%Y%m%d")
    random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=3))
    return f"ORD-{date_str}-{random_str}"
