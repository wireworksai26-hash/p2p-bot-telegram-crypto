"""Gerbang global user banned — dijalankan sebelum semua handler (group -1).

`/ban` hanya mengisi `users.is_banned`; tanpa gerbang ini user banned tetap
bisa Beli, Jual, Convert, Topup, dan memakai Saldo Bot.
"""
import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config.settings import settings
from database.connection import SessionLocal
from database.models import User

logger = logging.getLogger(__name__)

BANNED_TEXT = (
    "⛔ <b>Akun Anda diblokir.</b>\n\n"
    "Anda tidak dapat menggunakan layanan bot ini. "
    "Hubungi owner bila menurut Anda ini sebuah kesalahan."
)


def is_user_banned(telegram_id: int) -> bool:
    db = SessionLocal()
    try:
        row = db.query(User.is_banned).filter(User.telegram_id == telegram_id).first()
        return bool(row and row[0])
    finally:
        db.close()


async def ban_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not isinstance(update, Update):
        return
    user = update.effective_user
    if not user or user.id in settings.ADMIN_CHAT_IDS:
        return
    try:
        banned = is_user_banned(user.id)
    except Exception as exc:
        # Fail-closed hanya untuk jalur transaksi akan menghentikan seluruh bot saat
        # DB sesaat bermasalah; handler di belakangnya juga butuh DB dan akan gagal sendiri.
        logger.warning("Cek ban user %s gagal: %s", user.id, exc)
        return
    if not banned:
        return

    from bot.keyboards.main_menu import get_owner_button
    markup = InlineKeyboardMarkup([[get_owner_button()]])
    try:
        if update.callback_query:
            await update.callback_query.answer("Akun Anda diblokir.", show_alert=True)
        if update.effective_chat and update.effective_chat.type == "private":
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=BANNED_TEXT,
                parse_mode="HTML", reply_markup=markup)
    except Exception as exc:
        logger.warning("Gagal kirim notifikasi ban ke %s: %s", user.id, exc)
    if context.user_data is not None:
        context.user_data.clear()
    raise ApplicationHandlerStop
