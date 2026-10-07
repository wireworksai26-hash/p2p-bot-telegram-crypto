"""
bot/utils/amount_mode.py — Tombol pilihan cara input jumlah (Beli, Jual, Convert).
================================================================================
Dua tombol terpisah di satu baris: "🪙 Jumlah Koin" dan "💵 Nominal Rupiah". Mode aktif
diberi tanda ✅. callback_data: `{prefix}_mode_coin` / `{prefix}_mode_idr`.
"""
from telegram import InlineKeyboardButton

COIN = "COIN"
IDR = "IDR"


def mode_row(prefix: str, current: str = None) -> list:
    """Satu baris keyboard berisi tombol mode Koin dan mode Rupiah."""
    return [
        InlineKeyboardButton(("✅ " if current == COIN else "") + "🪙 Jumlah Koin", callback_data=f"{prefix}_mode_coin"),
        InlineKeyboardButton(("✅ " if current == IDR else "") + "💵 Nominal Rupiah", callback_data=f"{prefix}_mode_idr"),
    ]


def mode_from_callback(data: str) -> str:
    return IDR if (data or "").endswith("_idr") else COIN
