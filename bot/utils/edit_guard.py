"""Abaikan perintah slash yang DIEDIT — dijalankan sebelum semua handler (group -3).

CommandHandler bawaan python-telegram-bot juga menerima `edited_message`. Pada update itu
`update.message` bernilai None, sehingga handler yang memakai `update.message.reply_text`
error ("'NoneType' object has no attribute 'reply_text'"). Lebih berbahaya lagi, perintah
admin yang diedit (mis. /credit, /broadcast) bisa terjalankan ULANG. Perintah baru harus
dikirim sebagai pesan baru; edit pada teks biasa (bukan perintah) tidak disentuh.
"""
import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

logger = logging.getLogger(__name__)


async def edited_command_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not isinstance(update, Update):
        return
    edited = update.edited_message
    if edited is None or not (edited.text or edited.caption or "").lstrip().startswith("/"):
        return
    logger.info("Perintah hasil edit diabaikan (user %s): %s",
                update.effective_user.id if update.effective_user else "?",
                (edited.text or edited.caption or "")[:40])
    raise ApplicationHandlerStop
