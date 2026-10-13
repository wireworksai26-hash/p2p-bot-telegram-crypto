"""
bot/handlers/admin_emoji_check.py — /cekemoji: apakah Telegram mengizinkan bot ini memakai emoji premium
=========================================================================================================
Sejak Bot API 9.4, bot hanya boleh memakai emoji premium (custom emoji) di pesannya bila PEMILIK bot
(akun yang membuat bot di @BotFather) berlangganan Telegram Premium. Bila tidak, AnimatedExtBot
diam-diam mengirim ulang pesan dengan emoji biasa, sehingga admin hanya melihat emoji "tidak premium".
Perintah ini mengirim pesan uji langsung ke Telegram (tanpa fallback) dan menampilkan jawabannya.
"""

from html import escape

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ExtBot

from bot.utils import animated
from bot.utils.emojis import CUSTOM_EMOJI_IDS

TEST_EMOJI_ID = CUSTOM_EMOJI_IDS.get("CHECK") or "5237699328843200968"

FIX_STEPS = (
    "Sejak Bot API 9.4, bot hanya boleh memakai emoji premium bila <b>pemilik bot</b> (akun yang "
    "membuat bot di @BotFather) berlangganan <b>Telegram Premium</b>.\n\n"
    "<b>Solusi (pilih salah satu):</b>\n"
    "1. Aktifkan Telegram Premium di akun pemilik bot, atau\n"
    "2. Pindahkan kepemilikan bot ke akun yang sudah Premium: @BotFather → /mybots → pilih bot → "
    "Bot Settings → Transfer Ownership.\n\n"
    "Setelah itu jalankan /cekemoji lagi. Emoji premium aktif kembali paling lambat 10 menit tanpa restart."
)


async def emoji_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from bot.handlers.admin import is_admin
    if not update.message or not update.effective_user or not is_admin(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    try:
        # ExtBot.send_message asli, melewati fallback AnimatedExtBot agar penolakan Telegram terlihat.
        await ExtBot.send_message(
            context.bot, chat_id=chat_id, parse_mode="HTML",
            text=f'<tg-emoji emoji-id="{TEST_EMOJI_ID}">✅</tg-emoji> Pesan uji emoji premium')
    except BadRequest as exc:
        animated.record_custom_emoji_result(False)
        await update.message.reply_text(
            f"❌ <b>Telegram MENOLAK emoji premium dari bot ini.</b>\n"
            f"Jawaban Telegram: <code>{escape(str(exc))}</code>\n\n{FIX_STEPS}",
            parse_mode="HTML")
        return
    animated.record_custom_emoji_result(True)
    status = animated.custom_emoji_status()
    await update.message.reply_text(
        "✅ <b>Telegram menerima emoji premium dari bot ini.</b>\n\n"
        "Cek pesan uji di atas: bila centangnya tampil sebagai emoji premium (berwarna/animasi), emoji "
        "premium di bot, dashboard, dan broadcast sudah aktif. Bila tampil sebagai ✅ biasa, Telegram "
        f"menurunkannya diam-diam.\n\n{FIX_STEPS}\n\n"
        f"<i>Sejak bot terakhir start: {status['accepts']} pesan beremoji premium diterima, "
        f"{status['rejects']} ditolak.</i>",
        parse_mode="HTML")
