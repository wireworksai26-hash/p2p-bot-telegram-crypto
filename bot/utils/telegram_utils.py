"""
bot/utils/telegram_utils.py — Helper umum interaksi Telegram.
====================================================================
Menangani edit pesan inline yang aman: jika pesan gagal di-edit
(misal "message is not modified" / pesan sudah terlalu lama),
fallback otomatis ke reply_text agar tidak memicu error global bot.
"""

import logging

logger = logging.getLogger(__name__)


async def safe_edit_message(query, text: str, reply_markup=None, parse_mode="HTML", **kwargs):
    """
    Edit pesan callback_query dengan fallback ke reply_text.

    Args:
        query: CallbackQuery dari python-telegram-bot.
        text (str): Isi pesan baru.
        reply_markup: InlineKeyboardMarkup opsional.
        parse_mode (str): Mode parsing (default 'HTML').
    """
    if query is None:
        return

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            **kwargs
        )
    except Exception as edit_err:
        # Fallback: kirim pesan baru sebagai reply
        try:
            await query.message.reply_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                **kwargs
            )
        except Exception as reply_err:
            logger.warning(
                "Gagal edit & reply pesan (query=%s): edit_err=%s reply_err=%s",
                getattr(query, "id", "?"), edit_err, reply_err,
            )


async def safe_send_message(sender, chat_id: int, text: str, parse_mode="HTML", reply_markup=None) -> bool:
    """
    Kirim pesan Telegram dengan aman dan fallback otomatis jika parse HTML gagal.
    Returns True jika berhasil, False jika gagal.
    """
    try:
        from services.bot_runtime import bot_app
        bot_obj = None
        
        # 1. Direct send_message callable on sender (e.g. context.bot, Bot, ExtBot)
        if sender and hasattr(sender, "send_message") and callable(getattr(sender, "send_message")):
            bot_obj = sender
        # 2. Context or Application wrapper (e.g. context.bot, app.bot)
        elif sender and hasattr(sender, "bot") and hasattr(sender.bot, "send_message") and callable(getattr(sender.bot, "send_message")):
            bot_obj = sender.bot
        
        # 3. Fallback to global bot_app
        if not bot_obj and bot_app:
            if hasattr(bot_app, "bot") and bot_app.bot:
                bot_obj = bot_app.bot
            elif hasattr(bot_app, "send_message") and callable(getattr(bot_app, "send_message")):
                bot_obj = bot_app

        if not bot_obj:
            logger.warning("Gagal kirim pesan ke %s: bot_obj tidak ditemukan", chat_id)
            return False

        try:
            await bot_obj.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            return True
        except Exception as send_err:
            if parse_mode:
                logger.warning("Retry kirim pesan ke %s tanpa parse_mode karena: %s", chat_id, send_err)
                await bot_obj.send_message(
                    chat_id=int(chat_id),
                    text=text,
                    parse_mode=None,
                    reply_markup=reply_markup
                )
                return True
            raise send_err
    except Exception as exc:
        logger.warning("Gagal kirim pesan ke %s: %s", chat_id, exc)
        return False


async def notify_admins(sender, text: str, parse_mode="HTML", reply_markup=None) -> None:
    """Kirim pesan ke semua admin (ADMIN_CHAT_IDS) dengan aman."""
    from config.settings import settings
    for admin_id in settings.ADMIN_CHAT_IDS:
        await safe_send_message(sender, admin_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
