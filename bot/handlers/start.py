"""
bot/handlers/start.py — Handler /start & Navigasi Menu Utama.
==============================================================
Menangani command /start untuk menyapa pengguna dan mendaftarkannya ke DB.
Juga berfungsi sebagai router untuk callback query tombol menu navigasi dasar.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes

from database.connection import SessionLocal
from database.models import User
from database.crud import create_user, get_user_count, get_user, get_completed_order_count
from bot.keyboards.main_menu import get_main_menu_keyboard
from bot.keyboards.crypto_select import get_owner_button
from bot.utils.messages import WELCOME_MESSAGE, SNK_TEXT
from bot.utils.emojis import CUSTOM_EMOJI_IDS

logger = logging.getLogger(__name__)

def get_wib_datetime_info() -> tuple[str, str]:
    from datetime import datetime, timezone, timedelta
    wib = timezone(timedelta(hours=7))
    now = datetime.now(wib)
    
    hour = now.hour
    if 4 <= hour < 11:
        greeting = "Pagi"
    elif 11 <= hour < 15:
        greeting = "Siang"
    elif 15 <= hour < 18:
        greeting = "Sore"
    else:
        greeting = "Malam"
        
    days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    months = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    
    day_str = days[now.weekday()]
    month_str = months[now.month - 1]
    time_str = f"{day_str}, {now.day} {month_str} {now.year} pukul {now.strftime('%H.%M.%S')} WIB"
    
    return greeting, time_str


def build_welcome_message(user, db_user, total_users: int, total_success: int) -> str:
    import html
    from bot.utils.emojis import E_WAVE, E_CALENDAR, E_CHART, E_MONEY, E_CART, E_USER, E_CHECK
    from bot.utils.formatter import format_idr

    greeting, time_str = get_wib_datetime_info()
    user_name = html.escape(user.first_name or "Kak")
    user_bal = int(getattr(db_user, "balance_idr", 0) or 0) if db_user else 0
    user_orders = int(getattr(db_user, "total_orders", 0) or 0) if db_user else 0

    return (
        f"{E_WAVE()} <b>Selamat {greeting}, {user_name}!</b>\n"
        f"{E_CALENDAR()} <i>{time_str}</i>\n\n"
        f"Selamat Datang di <b>HSN STORE BOT</b> — P2P Crypto Trading Automation.\n\n"
        f"{E_CHART()} <b>STATISTIK AKUN</b>\n"
        f"├── {E_MONEY()} <b>Saldo Aktif</b>   : <b>{format_idr(user_bal)}</b>\n"
        f"└── {E_CART()} <b>Total Order</b>   : <b>{user_orders} Transaksi</b>\n\n"
        f"{E_CHART()} <b>STATISTIK BOT</b>\n"
        f"├── {E_USER()} <b>Total Pengguna</b> : <b>{total_users:,} Member</b>\n"
        f"└── {E_CHECK()} <b>Total Transaksi</b>: <b>{total_success:,}x Berhasil</b>\n\n"
        f"Silakan gunakan menu di bawah untuk memulai transaksi:"
    )


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Kirim atau edit pesan kembali ke menu utama dengan tampilan 3D Dashboard.
    """
    user = update.effective_user
    query = update.callback_query
    
    db = SessionLocal()
    try:
        db_user = get_user(db, user.id)
        if not db_user:
            db_user = create_user(
                db=db,
                telegram_id=user.id,
                username=user.username,
                full_name=user.full_name
            )
        total_users = get_user_count(db)
        total_success = get_completed_order_count(db)
    finally:
        db.close()
        
    welcome_text = build_welcome_message(
        user=user,
        db_user=db_user,
        total_users=total_users,
        total_success=total_success
    )
    
    if query:
        try:
            await query.edit_message_text(
                text=welcome_text,
                reply_markup=get_main_menu_keyboard(),
                parse_mode="HTML"
            )
        except Exception:
            try:
                await query.message.reply_text(
                    text=welcome_text,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode="HTML"
                )
            except Exception:
                pass
    else:
        await update.message.reply_text(
            text=welcome_text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode="HTML"
        )


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler untuk command /start.
    Mendaftarkan user ke database jika baru, kemudian mengirim welcome message.
    """
    try:
        user = update.effective_user
        
        # Daftarkan user terlebih dahulu
        db = SessionLocal()
        try:
            create_user(
                db=db,
                telegram_id=user.id,
                username=user.username,
                full_name=user.full_name
            )
        finally:
            db.close()
            
        await send_main_menu(update, context)
        
    except Exception as e:
        logger.error(f"Error di start_handler: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Terjadi kesalahan saat memproses data Anda. Silakan coba lagi.")


async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catch-all CallbackQueryHandler untuk menangani tombol menu statis.
    Mengarahkan menu_* callback ke fungsinya masing-masing.
    """
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass
    
    data = query.data if query else None
    logger.info(f"Callback query received: {data}")
    
    if data == "menu_snk":
        # Tampilkan Syarat & Ketentuan
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        keyboard = [
            [InlineKeyboardButton("Kembali ke Menu", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
        await query.edit_message_text(
            text=SNK_TEXT,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        
    elif data in ["menu_back", "buy_cancel", "sell_cancel", "calc_cancel"]:
        await send_main_menu(update, context)
        
    elif data in ["menu_balance", "show_balance"]:
        from bot.handlers.balance import show_balance_menu
        await show_balance_menu(update, context)
        
    elif data.startswith("check_topup_"):
        from bot.handlers.balance import check_topup_payment_manual
        await check_topup_payment_manual(update, context)

    elif data.startswith("cancel_topup_"):
        from bot.handlers.balance import cancel_topup_manual
        await cancel_topup_manual(update, context)

    elif data.startswith("check_buy_payment_"):
        from bot.handlers.buy import check_buy_payment
        await check_buy_payment(update, context)

    elif data.startswith("admin_approve_buy_"):
        from bot.handlers.admin import admin_approve_buy_callback
        await admin_approve_buy_callback(update, context)

    elif data.startswith("admin_reject_buy_"):
        from bot.handlers.admin import admin_reject_buy_callback
        await admin_reject_buy_callback(update, context)

    elif data.startswith("admin_approve_topup_"):
        from bot.handlers.admin import admin_approve_topup_callback
        await admin_approve_topup_callback(update, context)

    elif data.startswith("admin_reject_topup_"):
        from bot.handlers.admin import admin_reject_topup_callback
        await admin_reject_topup_callback(update, context)

    elif data.startswith("admin_confirm_sell_"):
        from bot.handlers.admin import admin_confirm_sell_callback
        await admin_confirm_sell_callback(update, context)

    elif data.startswith("admin_approve_swap_"):
        from bot.handlers.admin import admin_approve_swap_callback
        await admin_approve_swap_callback(update, context)

    elif data.startswith("admin_reject_swap_"):
        from bot.handlers.admin import admin_reject_swap_callback
        await admin_reject_swap_callback(update, context)

    elif data == "menu_price":
        from bot.handlers.price import show_prices
        await show_prices(update, context)

    elif data == "price_fee_list":
        from bot.handlers.price import show_fee_list
        await show_fee_list(update, context)

    elif data == "show_live_market":
        from bot.handlers.price import show_live_market
        await show_live_market(update, context)

        
    elif data == "menu_stocks":
        from bot.handlers.stocks import show_stocks
        await show_stocks(update, context)
        
    elif data == "menu_history":
        from bot.handlers.history import show_history
        await show_history(update, context)
        
    else:
        logger.warning(f"Unhandled callback query: {data}")
