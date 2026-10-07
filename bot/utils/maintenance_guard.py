"""Mode darurat database — dijalankan sebelum semua handler (group -2).

Bila Postgres tidak terjangkau saat boot, bot jatuh ke SQLite sementara agar tetap
hidup. Order, topup, dan saldo yang dibuat di SQLite itu akan hilang saat restart
berikutnya, jadi selama mode darurat semua interaksi user (selain admin) dijawab
dengan info pemeliharaan dan tidak diteruskan ke handler transaksi.
"""
import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config.settings import settings
from database import connection

logger = logging.getLogger(__name__)

MAINTENANCE_TEXT = (
    "🛠 <b>Bot sedang dalam pemeliharaan singkat.</b>\n\n"
    "Transaksi untuk sementara tidak dapat diproses. Silakan coba lagi beberapa menit lagi. "
    "Pesanan dan saldo Anda yang sudah ada tetap aman."
)


async def maintenance_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not connection.DB_DEGRADED or not isinstance(update, Update):
        return
    user = update.effective_user
    if not user or user.id in settings.ADMIN_CHAT_IDS:
        return
    try:
        if update.callback_query:
            await update.callback_query.answer("Bot sedang pemeliharaan.", show_alert=True)
        if update.effective_chat and update.effective_chat.type == "private":
            await context.bot.send_message(chat_id=update.effective_chat.id,
                                           text=MAINTENANCE_TEXT, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Gagal kirim info pemeliharaan ke %s: %s", user.id, exc)
    if context.user_data is not None:
        context.user_data.clear()
    raise ApplicationHandlerStop
