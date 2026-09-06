"""
bot/handlers/calculator.py — Handler Kalkulator Fee Transaksi.
==============================================================
Menghitung simulasi biaya transaksi (fee) berdasarkan nominal Rupiah (IDR).
Menggunakan ConversationHandler untuk menerima input teks dari user.
"""

import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from services.fee_service import calculate_fee_idr
from bot.keyboards.main_menu import get_owner_button
from bot.utils.validator import validate_amount_idr
from bot.utils.formatter import format_idr
from bot.utils.emojis import (
    E_CHART,
    E_MONEY,
    E_DOLLAR,
    E_COIN,
    E_SWAP,
    E_WARN,
    E_CROSS,
)

logger = logging.getLogger(__name__)

# State untuk ConversationHandler
WAITING_NOMINAL = 1


async def start_calculator_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point kalkulator melalui klik tombol inline menu utama (menu_calc).
    """
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🔙 Batal & Kembali", callback_data="calc_cancel")],
        [get_owner_button()]
    ]
    
    await query.edit_message_text(
        text=(
            f"{E_CHART()} <b>KALKULATOR SIMULASI FEE</b>\n\n"
            "Silakan masukkan nominal Rupiah (IDR) yang ingin Anda simulasikan.\n"
            "<i>Ketik nominal langsung di chat (contoh: 50000 atau Rp 50.000).</i>\n\n"
            f"{E_WARN()} Minimal nominal simulasi adalah <b>Rp 5.000</b>."
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return WAITING_NOMINAL


async def start_calculator_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point kalkulator melalui command /calculator di chat.
    """
    keyboard = [
        [InlineKeyboardButton("🔙 Batal & Kembali", callback_data="calc_cancel")],
        [get_owner_button()]
    ]
    
    await update.message.reply_text(
        text=(
            f"{E_CHART()} <b>KALKULATOR SIMULASI FEE</b>\n\n"
            "Silakan masukkan nominal Rupiah (IDR) yang ingin Anda simulasikan.\n"
            "<i>Ketik nominal langsung di chat (contoh: 50000 atau Rp 50.000).</i>\n\n"
            f"{E_WARN()} Minimal nominal simulasi adalah <b>Rp 5.000</b>."
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return WAITING_NOMINAL


async def process_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Memproses input teks nominal dari user, menghitung fee, dan menampilkan rincian.
    """
    text_input = update.message.text
    
    # Validasi nominal IDR
    is_valid, nominal_idr = validate_amount_idr(text_input)
    
    if not is_valid:
        keyboard = [
            [InlineKeyboardButton("🔙 Batal & Kembali", callback_data="calc_cancel")],
            [get_owner_button()]
        ]
        await update.message.reply_text(
            text=(
                f"{E_CROSS()} <b>Nominal Tidak Valid!</b>\n\n"
                "Format input salah atau nominal kurang dari batas minimal Rp 5.000.\n"
                "Silakan masukkan nominal kembali (contoh: <code>50000</code>):"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return WAITING_NOMINAL

    try:
        fee_usd = calculate_fee_idr(nominal_idr, "USD") if nominal_idr <= 1015000 else "N/A"
    except Exception:
        fee_usd = "N/A"
        
    try:
        fee_alt = calculate_fee_idr(nominal_idr, "ALTCOIN") if nominal_idr <= 1010000 else "N/A"
    except Exception:
        fee_alt = "N/A"

    try:
        fee_conv = calculate_fee_idr(nominal_idr, "CONVERT") if nominal_idr <= 1010000 else "N/A"
    except Exception:
        fee_conv = "N/A"

    # Build response breakdown
    def fmt_fee(v):
        return "N/A (di atas batas)" if v == "N/A" else format_idr(v)

    breakdown_text = (
        f"{E_CHART()} <b>RINCIAN SIMULASI BIAYA (FEE)</b>\n\n"
        f"{E_MONEY()} <b>Nominal Aset:</b> {format_idr(nominal_idr)}\n"
        f"⚙️ <b>Skema Layanan:</b> Tiered Fee\n"
        f"────────────────────\n"
        f"{E_DOLLAR()} <b>USD Tier (USDT/USDC):</b> {fmt_fee(fee_usd)}\n"
        f"{E_COIN()} <b>Altcoin Tier:</b> {fmt_fee(fee_alt)}\n"
        f"{E_SWAP()} <b>Convert Tier:</b> {fmt_fee(fee_conv)}\n"
        f"────────────────────\n"
        f"<i>Catatan: Nominal transaksi di atas batas list resmi silakan hubungi admin.</i>"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 Hitung Nominal Lain", callback_data="calc_again")],
        [InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data="menu_back")],
        [get_owner_button()]
    ]

    await update.message.reply_text(
        text=breakdown_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return ConversationHandler.END


async def cancel_calculator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Membatalkan sesi kalkulator dan kembali ke menu utama.
    """
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass
        from bot.handlers.start import send_main_menu
        await send_main_menu(update, context)
    else:
        await update.message.reply_text(
            text="❌ Sesi kalkulator dibatalkan.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")
            ]])
        )
    return ConversationHandler.END


# Definisikan ConversationHandler
calculator_conversation_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_calculator_callback, pattern="^menu_calc$"),
        CommandHandler("calculator", start_calculator_command),
        CallbackQueryHandler(start_calculator_callback, pattern="^calc_again$")
    ],
    states={
        WAITING_NOMINAL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, process_nominal),
            CallbackQueryHandler(cancel_calculator, pattern="^calc_cancel$"),
        ]
    },
    fallbacks=[
        CallbackQueryHandler(cancel_calculator, pattern="^calc_cancel$"),
        CommandHandler("cancel", cancel_calculator) # Support fallback manual command /cancel
    ]
)
