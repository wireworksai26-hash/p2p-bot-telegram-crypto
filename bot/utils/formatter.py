"""
bot/utils/formatter.py — Helper formatting untuk P2P Crypto Bot.
===================================================================
Berisi fungsi-fungsi format angka Rupiah, format jumlah cryptocurrency,
format tanggal/waktu ke timezone WIB, dan pembuatan ID Order unik.
"""

import secrets
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


def mask_public_name(name) -> str:
    """Sensor nama untuk tampilan publik (leaderboard): 2 karakter awal + *** + 2 karakter akhir.

    @Oxhusnun -> Ox***un, @Nandaderak -> Na***ak. Karakter '@' dibuang total supaya nama tidak
    jadi mention yang bisa diklik (cegah scraping dan DM penipuan). Nama pendek disensor lebih
    ketat agar tidak terbaca utuh: 3-4 karakter -> a***d, 1-2 karakter -> a***, kosong -> User***.
    """
    cleaned = "".join(ch for ch in str(name or "") if ch.isprintable() and ch != "@").strip()
    cleaned = " ".join(cleaned.split())
    n = len(cleaned)
    if n == 0:
        return "User***"
    if n >= 5:
        return f"{cleaned[:2]}***{cleaned[-2:]}"
    if n >= 3:
        return f"{cleaned[0]}***{cleaned[-1]}"
    return f"{cleaned[0]}***"


# Token yang sudah rebrand resmi: tampilkan ticker pasar, simbol internal lama tetap dipakai
# untuk harga, saldo, dan record order.
DISPLAY_SYMBOLS = {"TON": "GRAM"}


def display_symbol(symbol: str) -> str:
    """Ticker yang ditampilkan ke user (mis. TON -> GRAM setelah rebrand 15 Juni 2026)."""
    sym = (symbol or "").upper()
    return DISPLAY_SYMBOLS.get(sym, sym)


def format_crypto_copy(amount, symbol: str, exact: bool = False) -> str:
    """
    Jumlah koin untuk pesan HTML dengan ANGKA-nya saja di dalam <code> (ketuk = tersalin),
    mis. '<code>5.0000</code> USDT'. exact=True memakai presisi deposit persis seperti
    tersimpan di order (untuk admin yang menyalin angka ke wallet/explorer).
    """
    try:
        if exact:
            from services.deposit_amount import format_deposit_amount
            return f"<code>{format_deposit_amount(amount, symbol)}</code> {display_symbol(symbol)}"
        number, _, label = format_crypto(amount, symbol).partition(" ")
        return f"<code>{number}</code> {label}".strip()
    except Exception:
        return f"<code>{amount}</code> {symbol}"


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
        if sym in ["USDT", "USDG", "TON"]:
            precision = 4
        elif sym in ["SOL", "AVAX", "POLYGON", "MATIC"]:
            precision = 6
        else: # ETH, BNB, BASE, ARB (BNB 8: nominal deposit berkode unik 8 desimal)
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
    Format: ORD-YYYYMMDD-XXXXXXXX (8 karakter acak kriptografis, 36^8 kombinasi/hari).
    Contoh: ORD-20260526-A3B9K2QZ
    """
    date_str = datetime.now().strftime("%Y%m%d")
    alphabet = string.ascii_uppercase + string.digits
    random_str = ''.join(secrets.choice(alphabet) for _ in range(8))
    return f"ORD-{date_str}-{random_str}"
