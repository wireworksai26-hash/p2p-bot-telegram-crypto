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
        text="Hubungi Owner (Chat Admin)",
        url=owner_url,
        icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CHAT", "5417915203100613993")
    )


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Mendapatkan keyboard untuk menu utama bot.
    Layout tombol:
      [ 🛒 Beli Crypto ]   [ 📈 Jual Crypto ]
      [ 💰 Saldo & Profil ] [ 🔄 Convert Crypto ]
      [ 💵 Cek Harga  ]   [ 📦 Cek Stok    ]
      [ 📜 Riwayat Transaksi ] [ ⚠️ Syarat & Ketentuan ]
      [ 💬 Hubungi Owner (Chat Admin)      ]
    """
    keyboard = [
        [
            InlineKeyboardButton("Beli Crypto", callback_data="menu_buy", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CART", "5431492767249342908")),
            InlineKeyboardButton("Jual Crypto", callback_data="menu_sell", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CHART", "5350305691942788490")),
        ],
        [
            InlineKeyboardButton("Cek Saldo & Profil", callback_data="menu_balance", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("MONEY_BAG", "5350452584119279096")),
            InlineKeyboardButton("Convert Crypto", callback_data="start_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("SWAP", "5310107765874632305")),
        ],
        [
            InlineKeyboardButton("Cek Harga", callback_data="menu_price", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("DOLLAR", "5309929258443874898")),
            InlineKeyboardButton("Cek Stok", callback_data="menu_stocks", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BOX", "5350699789551935589")),
        ],
        [
            InlineKeyboardButton("Riwayat Transaksi", callback_data="menu_history", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("HISTORY", "5373251851074415873")),
            InlineKeyboardButton("Syarat & Ketentuan", callback_data="menu_snk", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("WARNING", "5447644880824181073")),
        ],
        [
            get_owner_button()
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

