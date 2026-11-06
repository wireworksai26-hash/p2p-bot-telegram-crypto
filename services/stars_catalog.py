"""Katalog Telegram Stars & Telegram Premium: harga resmi klien, validasi username, teks pricelist.

Harga dalam Rupiah (k = ribuan, mis. 16.5k = 16.500; 1361k = 1.361.000). Pembeli boleh mengisi username
akun mana pun (milik sendiri atau teman), sesuai permintaan klien tidak ada pengecekan anti-fraud.
"""
import re
from html import escape as _esc

from bot.utils.emojis import E_STAR, E_CROWN
from bot.utils.formatter import format_idr

# jumlah Stars -> harga (IDR)
STARS_PACKAGES = {
    50: 16_500,
    75: 24_000,
    100: 31_000,
    150: 45_000,
    250: 74_000,
    350: 103_000,
    500: 143_000,
    750: 215_000,
    1000: 278_500,
    1500: 414_000,
    2500: 690_000,
    5000: 1_361_000,
}

# lama langganan (bulan) -> harga (IDR)
PREMIUM_PACKAGES = {
    3: 228_000,
    6: 305_400,
    12: 539_500,
}

PRODUCTS = {
    "stars": {"label": "Telegram Stars", "icon": "⭐", "packages": STARS_PACKAGES},
    "premium": {"label": "Telegram Premium", "icon": "👑", "packages": PREMIUM_PACKAGES},
}

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def package_label(product: str, key: int) -> str:
    """'1.000 Stars' atau '3 Bulan'."""
    if product == "stars":
        return f"{int(key):,}".replace(",", ".") + " Stars"
    return f"{int(key)} Bulan"


def package_price(product: str, key) -> int:
    """Harga paket, atau 0 bila produk/paket tidak ada."""
    try:
        return int(PRODUCTS[product]["packages"].get(int(key), 0))
    except (KeyError, TypeError, ValueError):
        return 0


def parse_username(text: str):
    """Username Telegram dari '@nama', 'nama', atau tautan t.me/nama; None bila bentuknya tidak valid."""
    value = (text or "").strip()
    value = re.sub(r"^(https?://)?(www\.)?t\.me/", "", value, flags=re.I)
    value = value.lstrip("@").strip().rstrip("/")
    return value if _USERNAME_RE.match(value) else None


def pricelist_text() -> str:
    """Teks pricelist Stars & Premium."""
    stars = "\n".join(
        f"🔴 {package_label('stars', k)} = {format_idr(p)}" for k, p in STARS_PACKAGES.items())
    premium = "\n".join(
        f"🔴 {package_label('premium', k)} = {format_idr(p)}" for k, p in PREMIUM_PACKAGES.items())
    return (
        f"{E_STAR()} <b>PRICELIST TELEGRAM STARS</b>\n" + _esc_block(stars) + "\n\n"
        f"{E_CROWN()} <b>PRICELIST TELEGRAM PREMIUM</b>\n" + _esc_block(premium) + "\n\n"
        "<i>Bisa dikirim ke akun sendiri maupun akun teman: cukup isi username tujuan saat pesan.</i>"
    )


def _esc_block(text: str) -> str:
    return _esc(text)
