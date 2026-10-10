"""Gerbang pemeliharaan — dijalankan sebelum semua handler (group -2).

1. Mode darurat database: bila Postgres tidak terjangkau saat boot, bot jatuh ke SQLite
   sementara agar tetap hidup. Order, topup, dan saldo yang dibuat di SQLite itu akan hilang
   saat restart berikutnya, jadi semua interaksi user (selain admin) dijawab dengan info
   pemeliharaan dan tidak diteruskan ke handler transaksi.
2. Pause maintenance / update sistem (dijadwalkan admin): hanya transaksi BARU yang dijeda;
   menu lain dan order yang sedang berjalan tetap dilayani.
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


# Pause terjadwal/manual (services/system_pause.py): hanya pemicu transaksi BARU yang dijeda.
# Tombol konfirmasi ikut dijeda agar user yang sudah setengah jalan tidak membuat order saat pause.
PAUSE_BLOCKED_CALLBACKS = frozenset({
    "menu_buy", "menu_sell", "start_swap", "menu_swap", "start_topup_qris", "wd_start",
    "buy_confirm", "sell_confirm", "confirm_swap_order", "wd_confirm", "wd_all", "wd_custom",
})
PAUSE_BLOCKED_PREFIXES = ("topup_nom_", "wd_bank_")
PAUSE_BLOCKED_COMMANDS = frozenset({"buy", "beli", "sell", "jual", "swap", "convert", "topup"})


def _is_new_transaction_trigger(update: Update) -> bool:
    query = update.callback_query
    if query is not None:
        data = query.data or ""
        return data in PAUSE_BLOCKED_CALLBACKS or data.startswith(PAUSE_BLOCKED_PREFIXES)
    message = update.message
    text = (message.text or "").strip() if message else ""
    if not text.startswith("/"):
        return False
    command = text.split()[0][1:].split("@")[0].lower()
    return command in PAUSE_BLOCKED_COMMANDS


async def _pause_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or user.id in settings.ADMIN_CHAT_IDS or not _is_new_transaction_trigger(update):
        return
    from services.system_pause import active_pause, block_text
    info = active_pause()
    if info is None:
        return
    try:
        if update.callback_query:
            await update.callback_query.answer("🛠 Bot sedang maintenance — transaksi baru dijeda sementara.",
                                               show_alert=True)
        if update.effective_chat and update.effective_chat.type == "private":
            await context.bot.send_message(chat_id=update.effective_chat.id, text=block_text(info),
                                           parse_mode="HTML")
    except Exception as exc:
        logger.warning("Gagal kirim info pause ke %s: %s", user.id, exc)
    raise ApplicationHandlerStop


async def maintenance_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not isinstance(update, Update):
        return
    if not connection.DB_DEGRADED:
        await _pause_gate(update, context)
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
