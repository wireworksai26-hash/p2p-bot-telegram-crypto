"""
bot/handlers/price.py — Handler Cek Harga Crypto Hari Ini & Price List Fee.
==========================================================================
Menampilkan daftar harga crypto terupdate sesuai format resmi permintaan client:
- Pasangan koin terkurasi (USDT TON, ETH, BNB, SOL, AVAX, TRX, MATIC, G, BASE, ARB)
- Harga Beli & Jual terupdate realtime
- Opsi melihat Price List Fee transaksi resmi
"""

import logging
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database.connection import SessionLocal
from services.price_service import price_service
from bot.keyboards.main_menu import get_owner_button
from bot.utils.formatter import format_idr, format_datetime

logger = logging.getLogger(__name__)

# Daftar 10 koin terkurasi yang wajib ditampilkan saat Cek Harga
CURATED_PRICE_ASSETS = [
    ("USDT", "TON", "🪙", "USDT (TON)"),
    ("ETH", "ETH", "🔷", "ETH (ETH)"),
    ("BNB", "BSC", "🟢", "BNB (BSC)"),
    ("SOL", "SOLANA", "🟣", "SOL (SOLANA)"),
    ("AVAX", "AVAX", "🔴", "AVAX (AVAX)"),
    ("TRX", "TRON", "❤️", "TRX (TRON)"),
    ("MATIC", "POLYGON", "🟪", "MATIC (POLYGON)"),
    ("G", "GRAVITY", "🌌", "G (GRAVITY)"),
    ("BASE", "EVM", "🪙", "BASE (EVM)"),
    ("ARB", "ARB", "💎", "ARB (ARB)"),
]

OFFICIAL_PRICE_LIST_TEXT = (
    "📊 <b>PRICE LIST & CARA PERHITUNGAN TRANSAKSI</b>\n\n"
    "<b>Price list khusus FEE ALTCOIN (USDT BEDA dan lebih murah)</b>\n"
    "🪙 <b>Minimum pembelian 5000</b> 🪙\n\n"
    "➡️ Jual/Beli 5k-10k  = fee 3k IDR\n"
    "➡️ Jual/Beli 11-15k  = fee 3.5k IDR\n"
    "➡️ Jual/Beli 16k-48k  = fee 4k IDR\n"
    "➡️ Jual/Beli 49k-93k = fee 5k IDR\n"
    "➡️ Jual/Beli 94k-105k = fee 5.5k IDR\n"
    "➡️ Jual/Beli 106k-110k = fee 6k IDR\n"
    "➡️ Jual/Beli 111k-119k = fee 6.5k IDR\n"
    "➡️ Jual/Beli 120k-150k = fee 7k IDR\n"
    "➡️ Jual/Beli 161k-185k = fee 7.5k IDR\n"
    "➡️ Jual/Beli 186k-220k = fee 8k IDR\n"
    "➡️ Jual/Beli 221k-300k = fee 8.5k IDR\n"
    "➡️ Jual/Beli 301k-330k = fee 9k IDR\n"
    "➡️ Jual/Beli 331k-380k = fee 9.5k IDR\n"
    "➡️ Jual/Beli 381k-420k = fee 10k IDR\n"
    "➡️ Jual/Beli 421k-460k = fee 10.5k IDR\n"
    "➡️ Jual/Beli 461k-500k = fee 11k IDR\n"
    "➡️ Jual/Beli 501k-600k = fee 11.5k IDR\n\n"
    "🪙 <b>List fee Khusus USD</b>\n"
    "🪙 <b>Minimum pembelian 5k</b> 🪙\n\n"
    "➡️ Jual/Beli 5k-35k  = fee 3k IDR\n"
    "➡️ Jual/Beli 36k-55k  = fee 3.5k IDR\n"
    "➡️ Jual/Beli 56k-70k  = fee 4k IDR\n"
    "➡️ Jual/Beli 71k-110k  = fee 4.5k IDR\n"
    "➡️ Jual/Beli 111k-170k  = fee 5k IDR\n"
    "➡️ Jual/Beli 171k-200k  = fee 5.5k IDR\n"
    "➡️ Jual/Beli 201k-250k = fee 6,5k IDR\n"
    "➡️ Jual/Beli 251k-330k = fee 7k IDR\n"
    "➡️ Jual/Beli 331k-400k = fee 7,5k IDR\n"
    "➡️ Jual/Beli 401k-450k = fee 8k IDR\n"
    "➡️ Jual/Beli 451k-550k = fee 8.5k IDR\n"
    "➡️ Jual/Beli 551-800k = fee 9k IDR\n"
    "➡️ Jual/Beli 801k-900k = fee 11k IDR\n"
    "➡️ Jual/Beli 901k-950k = fee 13k IDR\n"
    "➡️ Jual/Beli 951k-1015k = fee 14.5k IDR\n\n"
    "🛍 <b>Pembelian atau Penjualan Nominal di atas list yang tertera tanya admin dahulu</b> 🛍\n\n"
    "📣 <i>Adanya fee transaksi yang berbeda-beda dikarenakan volatilitas harga coin crypto yang sangat berfluktuasi (naik-turunnya nilai) dan sphread usdt (selisih harga) yang berubah ubah guna menghindari kerugian stok coin pihak admin.</i> 📣"
)


async def show_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Menampilkan Daftar Harga Crypto Hari Ini terupdate (Beli & Jual).
    Format 100% presisi sesuai permintaan client.
    """
    query = update.callback_query
    if query:
        await query.answer()
        await query.message.reply_chat_action(action="typing")
    elif update.message:
        await update.message.reply_chat_action(action="typing")

    db = SessionLocal()
    try:
        text_lines = [
            "📊 <b>DAFTAR HARGA CRYPTO HARI INI</b>\n",
            "<i>Berikut adalah harga beli (Rupiah ke Crypto) & jual (Crypto ke Rupiah) terupdate:</i>\n",
        ]

        for symbol, network, emoji, display_label in CURATED_PRICE_ASSETS:
            price_data = await price_service.get_price(symbol, db)
            if price_data:
                buy_price = format_idr(price_data["buy_price_idr"])
                sell_price = format_idr(price_data["sell_price_idr"])
            else:
                buy_price = "-"
                sell_price = "-"

            text_lines.append(
                f"{emoji} <b>{display_label}</b>\n"
                f"🛒 Beli: <code>{buy_price}</code>\n"
                f"💵 Jual: <code>{sell_price}</code>\n"
            )

        update_time_str = format_datetime(datetime.now(timezone.utc))
        text_lines.append(f"⏱️ Update: {update_time_str}")
        text_lines.append("⚠️ Harga di atas sudah termasuk markup/markdown spread bot.")

        message_text = "\n".join(text_lines)

        keyboard = [
            [InlineKeyboardButton("📋 Price List Fee Lengkap", callback_data="price_fee_list")],
            [InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data="menu_back")],
            [get_owner_button()]
        ]

        if query:
            await query.edit_message_text(
                text=message_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                text=message_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error di show_prices: {e}", exc_info=True)
        fallback_msg = "⚠️ Terjadi kesalahan saat mengambil harga crypto saat ini. Silakan coba sesaat lagi."
        keyboard = [
            [InlineKeyboardButton("🔙 Kembali ke Menu", callback_data="menu_back")],
            [get_owner_button()]
        ]
        if query:
            await query.message.reply_text(text=fallback_msg, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(text=fallback_msg, reply_markup=InlineKeyboardMarkup(keyboard))
    finally:
        db.close()


async def show_fee_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Menampilkan tabel Price List Fee Transaksi resmi untuk Altcoin dan USD.
    """
    query = update.callback_query
    if query:
        await query.answer()

    keyboard = [
        [InlineKeyboardButton("💵 Cek Harga Crypto Terupdate", callback_data="menu_price")],
        [InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data="menu_back")],
        [get_owner_button()]
    ]

    if query:
        await query.edit_message_text(
            text=OFFICIAL_PRICE_LIST_TEXT,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            text=OFFICIAL_PRICE_LIST_TEXT,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


# Alias untuk live market
show_live_market = show_prices
