"""
bot/handlers/price.py — Handler Cek Harga & Price List Resmi Crypto.
=====================================================================
Menampilkan Price List perhitungan resmi (FEE ALTCOIN & USD) saat user
menekan tombol "💵 Cek Harga", serta opsi melihat kurs pasar koin realtime.
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

# Emoji network helper
NETWORK_EMOJIS = {
    "BSC": "🟢", "ETH": "🔷", "SOLANA": "🟣", "AVAX": "🔴",
    "TRON": "❤️", "POLYGON": "🟪", "GRAVITY": "🌌",
    "BASE": "🔵", "ARB": "💎"
}

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
    Menampilkan Price List & Skema Fee Resmi saat user menekan '💵 Cek Harga'.
    """
    query = update.callback_query
    if query:
        await query.answer()

    keyboard = [
        [InlineKeyboardButton("📈 Cek Kurs Pasar Koin (Live)", callback_data="show_live_market")],
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


async def show_live_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Menampilkan harga pasar realtime dari Binance/Exchange jika user ingin memantau kurs live.
    """
    query = update.callback_query
    if query:
        await query.answer()
        await query.message.reply_chat_action(action="typing")

    db = SessionLocal()
    try:
        prices_data = await price_service.get_all_prices(db)
        if not prices_data:
            await query.message.reply_text(
                text="⚠️ Gagal mengambil data harga saat ini. Silakan coba sesaat lagi.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Kembali ke Price List", callback_data="menu_price")
                ]])
            )
            return

        from config.assets import STOCK_ASSETS
        net_mapping = {symbol: network for symbol, network in STOCK_ASSETS}

        text_lines = [
            "📈 <b>KURS PASAR CRYPTO REALTIME (BINANCE)</b>\n",
            "<i>Berikut adalah referensi kurs pasar per koin:</i>\n",
        ]

        for symbol, data in prices_data.items():
            network = net_mapping.get(symbol, "EVM")
            net_emoji = NETWORK_EMOJIS.get(network.upper(), "🪙")
            rate_price = format_idr(data["buy_price_idr"])

            text_lines.append(
                f"{net_emoji} <b>{symbol} ({network})</b>: <code>{rate_price}</code>"
            )

        text_lines.append(f"\n⏱️ <i>Update: {format_datetime(datetime.now(timezone.utc))}</i>")
        text_lines.append("📌 <i>Perhitungan transaksi menggunakan Price List Fee resmi.</i>")

        message_text = "\n".join(text_lines)

        keyboard = [
            [InlineKeyboardButton("📋 Kembali ke Price List Fee", callback_data="menu_price")],
            [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")],
            [get_owner_button()]
        ]

        await query.edit_message_text(
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error di show_live_market: {e}", exc_info=True)
        await query.message.reply_text(
            text="⚠️ Terjadi kesalahan internal saat mengambil kurs pasar.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Kembali ke Price List", callback_data="menu_price")
            ]])
        )
    finally:
        db.close()


# Alias for backward compatibility
show_fee_list = show_prices
