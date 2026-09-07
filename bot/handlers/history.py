"""
bot/handlers/history.py — Handler Cek Riwayat Transaksi.
======================================================
Menampilkan riwayat 10 transaksi terakhir pengguna (Buy maupun Sell).
"""

import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database.connection import SessionLocal
from database.crud import get_orders_by_user
from bot.keyboards.main_menu import get_owner_button
from bot.utils.formatter import format_idr, format_crypto, format_datetime
from bot.utils.emojis import (
    E_HISTORY,
    E_CART,
    E_DOLLAR,
    E_COIN,
    E_CARD,
    E_CHECK,
    E_CROSS,
    E_WARN,
    E_CALENDAR,
)

logger = logging.getLogger(__name__)

# Mapping status ke emoji untuk tampilan visual
STATUS_EMOJIS = {
    "pending": "⏳ Pending",
    "paid": "💳 Paid",
    "completed": "✅ Success",
    "expired": "❌ Expired",
    "failed": "🚨 Failed",
    "manual_review": "⚙️ Manual Review",
}

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Mengambil data riwayat order milik user dari database dan menyajikannya.
    Dipanggil saat user memilih '📜 Riwayat' dari menu utama.
    """
    query = update.callback_query
    user_id = update.effective_user.id
    
    # Beritahu user sedang memproses
    await query.message.reply_chat_action(action="typing")
    
    db = SessionLocal()
    try:
        # Ambil 10 order terakhir milik user
        orders = get_orders_by_user(db, telegram_id=user_id, limit=10)
        
        text_lines = [
            f"{E_HISTORY()} <b>RIWAYAT TRANSAKSI ANDA</b>\n",
            "<i>Menampilkan maksimal 10 transaksi terakhir:</i>\n",
        ]
        
        if not orders:
            text_lines.append("<i>Anda belum pernah melakukan transaksi di bot ini.</i>")
        else:
            for idx, order in enumerate(orders, 1):
                order_type_str = f"{E_CART()} BELI" if order.order_type == "buy" else f"{E_DOLLAR()} JUAL"
                status_str = STATUS_EMOJIS.get(order.status.lower(), order.status.upper())
                
                # Format crypto amount
                crypto_str = format_crypto(float(order.crypto_amount), order.crypto_symbol)
                
                # Tanggal order dibuat
                date_str = format_datetime(order.created_at)
                
                text_lines.append(
                    f"{idx}. <b>{order_type_str} | {order.order_id}</b>\n"
                    f"   {E_COIN()} Aset: <code>{crypto_str} ({order.network})</code>\n"
                    f"   {E_CARD()} Total: <code>{format_idr(order.total_idr)}</code>\n"
                    f"   🚦 Status: <b>{status_str}</b>\n"
                    f"   {E_CALENDAR()} Waktu: <i>{date_str}</i>\n"
                )
                
        message_text = "\n".join(text_lines)
        
        keyboard = [
            [InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data="menu_back")],
            [get_owner_button()]
        ]
        
        await query.edit_message_text(
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error di show_history: {e}", exc_info=True)
        await query.message.reply_text(
            text="⚠️ Gagal memuat riwayat transaksi Anda. Silakan coba sesaat lagi.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data="menu_back")
            ]])
        )
    finally:
        db.close()
