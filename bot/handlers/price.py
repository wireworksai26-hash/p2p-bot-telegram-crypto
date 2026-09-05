"""
bot/handlers/price.py — Handler Cek Harga Crypto Terkini.
=========================================================
Menampilkan daftar harga beli dan jual dalam IDR untuk semua koin pendukung.
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

async def show_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Menampilkan harga terkini untuk seluruh aset crypto.
    Dipanggil saat user menekan tombol '💵 Cek Harga' di menu utama.
    """
    query = update.callback_query
    
    # Beritahu user sedang memproses
    await query.message.reply_chat_action(action="typing")
    
    db = SessionLocal()
    try:
        # Fetch harga dari Binance (menggunakan cache/API)
        prices_data = await price_service.get_all_prices(db)
        
        if not prices_data:
            await query.message.reply_text(
                text="⚠️ Gagal mengambil data harga saat ini. Silakan coba sesaat lagi.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Kembali ke Menu", callback_data="menu_back")
                ]])
            )
            return

        # Pemetaan network agar user tahu
        from config.assets import STOCK_ASSETS
        net_mapping = {symbol: network for symbol, network in STOCK_ASSETS}
        
        # Build text tabel harga
        text_lines = [
            "📊 <b>DAFTAR HARGA CRYPTO HARI INI</b>\n",
            "<i>Berikut adalah harga beli (Rupiah ke Crypto) & jual (Crypto ke Rupiah) terupdate:</i>\n",
        ]
        
        # Urutkan list koin agar rapi
        for symbol, data in prices_data.items():
            network = net_mapping.get(symbol, "EVM")
            net_emoji = NETWORK_EMOJIS.get(network.upper(), "🪙")
            
            buy_price = format_idr(data["buy_price_idr"])
            sell_price = format_idr(data["sell_price_idr"])
            
            text_lines.append(
                f"{net_emoji} <b>{symbol} ({network})</b>\n"
                f"   🛒 Beli: <code>{buy_price}</code>\n"
                f"   💵 Jual: <code>{sell_price}</code>\n"
            )
            
        text_lines.append(f"⏱️ <i>Update: {format_datetime(datetime.now(timezone.utc))}</i>")
        text_lines.append("⚠️ <i>Harga di atas sudah termasuk markup/markdown spread bot.</i>")
        
        message_text = "\n".join(text_lines)
        
        keyboard = [
            [InlineKeyboardButton("📋 Price List Fee Lengkap", callback_data="price_fee_list")],
            [InlineKeyboardButton("🔙 Kembali ke Menu", callback_data="menu_back")],
            [get_owner_button()]
        ]
        
        await query.edit_message_text(
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error di show_prices: {e}", exc_info=True)
        await query.message.reply_text(
            text="⚠️ Terjadi kesalahan internal saat mengambil harga.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Kembali ke Menu", callback_data="menu_back")
            ]])
        )
    finally:
        db.close()


async def show_fee_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Menampilkan tabel Price List Fee Transaksi resmi untuk Altcoin dan USD.
    """
    query = update.callback_query
    if query:
        await query.answer()

    fee_text = (
        "📋 <b>PRICE LIST FEE TRANSAKSI RESMI</b>\n\n"
        "🪙 <b>Minimum Pembelian / Penjualan: Rp 5.000</b>\n\n"
        "🟢 <b>LIST FEE KHUSUS USD (USDT / USDC)</b>\n"
        "<i>(USDT lebih murah & stabil)</i>\n"
        "➡️ Jual/Beli 5k - 35k = Fee Rp 3.000\n"
        "➡️ Jual/Beli 36k - 55k = Fee Rp 3.500\n"
        "➡️ Jual/Beli 56k - 70k = Fee Rp 4.000\n"
        "➡️ Jual/Beli 71k - 110k = Fee Rp 4.500\n"
        "➡️ Jual/Beli 111k - 170k = Fee Rp 5.000\n"
        "➡️ Jual/Beli 171k - 200k = Fee Rp 5.500\n"
        "➡️ Jual/Beli 201k - 250k = Fee Rp 6.500\n"
        "➡️ Jual/Beli 251k - 330k = Fee Rp 7.000\n"
        "➡️ Jual/Beli 331k - 400k = Fee Rp 7.500\n"
        "➡️ Jual/Beli 401k - 450k = Fee Rp 8.000\n"
        "➡️ Jual/Beli 451k - 550k = Fee Rp 8.500\n"
        "➡️ Jual/Beli 551k - 800k = Fee Rp 9.000\n"
        "➡️ Jual/Beli 801k - 900k = Fee Rp 11.000\n"
        "➡️ Jual/Beli 901k - 950k = Fee Rp 13.000\n"
        "➡️ Jual/Beli 951k - 1015k = Fee Rp 14.500\n\n"
        "🟣 <b>PRICE LIST KHUSUS FEE ALTCOIN</b>\n"
        "<i>(ETH, SOL, SUI, TRX, BNB, MATIC, ARB, AVAX, dll.)</i>\n"
        "➡️ Jual/Beli 5k - 10k = Fee Rp 3.000\n"
        "➡️ Jual/Beli 11k - 15k = Fee Rp 3.500\n"
        "➡️ Jual/Beli 16k - 48k = Fee Rp 4.000\n"
        "➡️ Jual/Beli 49k - 93k = Fee Rp 5.000\n"
        "➡️ Jual/Beli 94k - 105k = Fee Rp 5.500\n"
        "➡️ Jual/Beli 106k - 110k = Fee Rp 6.000\n"
        "➡️ Jual/Beli 111k - 119k = Fee Rp 6.500\n"
        "➡️ Jual/Beli 120k - 150k = Fee Rp 7.000\n"
        "➡️ Jual/Beli 161k - 185k = Fee Rp 7.500\n"
        "➡️ Jual/Beli 186k - 220k = Fee Rp 8.000\n"
        "➡️ Jual/Beli 221k - 300k = Fee Rp 8.500\n"
        "➡️ Jual/Beli 301k - 330k = Fee Rp 9.000\n"
        "➡️ Jual/Beli 331k - 380k = Fee Rp 9.500\n"
        "➡️ Jual/Beli 381k - 420k = Fee Rp 10.000\n"
        "➡️ Jual/Beli 421k - 460k = Fee Rp 10.500\n"
        "➡️ Jual/Beli 461k - 500k = Fee Rp 11.000\n"
        "➡️ Jual/Beli 501k - 600k = Fee Rp 11.500\n\n"
        "🛍 <b>Pembelian atau Penjualan Nominal di atas list yang tertera tanya admin dahulu</b> 🛍\n\n"
        "📣 <i>Adanya fee transaksi yang berbeda-beda dikarenakan volatilitas harga coin crypto yang sangat berfluktuasi (naik-turunnya nilai) dan spread usdt (selisih harga) yang berubah-ubah guna menghindari kerugian stok coin pihak admin.</i>"
    )

    keyboard = [
        [InlineKeyboardButton("💵 Cek Kurs Koin Realtime", callback_data="menu_price")],
        [InlineKeyboardButton("🔙 Kembali ke Menu", callback_data="menu_back")],
        [get_owner_button()]
    ]

    if query:
        await query.edit_message_text(
            text=fee_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            text=fee_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
