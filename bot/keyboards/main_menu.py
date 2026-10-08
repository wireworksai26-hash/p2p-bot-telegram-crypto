"""
bot/keyboards/main_menu.py — Keyboard Menu Utama P2P Crypto Bot.
================================================================
Menyediakan inline keyboard untuk menu utama dan tombol reusable "Hubungi Owner".
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config.settings import settings
from bot.utils.emojis import CUSTOM_EMOJI_IDS


def get_owner_button() -> InlineKeyboardButton:
    """
    Mengembalikan tombol inline 'Hubungi Owner' yang reusable.
    Dapat ditempelkan di bagian bawah keyboard transaksi mana saja.
    """
    owner_url = f"https://t.me/{settings.OWNER_USERNAME}"
    return InlineKeyboardButton(
        text="💬 Hubungi Owner (Chat Admin)",
        url=owner_url
    )


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Mendapatkan keyboard untuk menu utama bot.
    Layout tombol:
      [ 🛒 Beli Crypto ]   [ 💸 Jual Crypto ]
      [ 💰 Cek Saldo & Profil ] [ 🔄 Convert Crypto ]
      [ 💵 Cek Harga  ]   [ 📦 Cek Stok    ]
      [ 📝 Riwayat Transaksi ] [ 👥 Program Referral ]
      [ 📖 Panduan Transaksi ] [ ⚠️ Syarat & Ketentuan ]
      [ 💬 Hubungi Owner (Chat Admin)      ]
    """
    keyboard = [
        [
            InlineKeyboardButton("🛒 Beli Crypto", callback_data="menu_buy"),
            InlineKeyboardButton("💸 Jual Crypto", callback_data="menu_sell"),
        ],
        [
            InlineKeyboardButton("💰 Cek Saldo & Profil", callback_data="menu_balance"),
            InlineKeyboardButton("🔄 Convert Crypto", callback_data="start_swap"),
        ],
        [
            InlineKeyboardButton("💵 Cek Harga", callback_data="menu_price"),
            InlineKeyboardButton("📦 Cek Stok", callback_data="menu_stocks"),
        ],
        [
            InlineKeyboardButton("📝 Riwayat Transaksi", callback_data="menu_history"),
            InlineKeyboardButton("👥 Program Referral", callback_data="menu_referral"),
        ],
        [
            InlineKeyboardButton("📖 Panduan Transaksi", callback_data="menu_guide"),
            InlineKeyboardButton("⚠️ Syarat & Ketentuan", callback_data="menu_snk"),
        ],
        [
            get_owner_button()
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

