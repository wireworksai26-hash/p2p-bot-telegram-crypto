"""
bot/utils/telegram_utils.py — Helper umum interaksi Telegram.
====================================================================
Menangani edit pesan inline yang aman: jika pesan gagal di-edit
(misal "message is not modified" / pesan sudah terlalu lama),
fallback otomatis ke reply_text agar tidak memicu error global bot.
"""

import html as _html
import logging
import re
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

logger = logging.getLogger(__name__)


def resolve_bot(sender):
    """Bot API object dari sender (Bot/ExtBot, Application, atau CallbackContext).

    Jangan `getattr(sender, "bot", sender)`: pada Bot/ExtBot, `.bot` adalah
    telegram.User milik bot (hasil get_me), bukan objek Bot — send_photo/
    send_message(chat_id=...) di User langsung TypeError.
    """
    from telegram import Bot
    if isinstance(sender, Bot):
        return sender
    inner = getattr(sender, "bot", None)
    return inner if inner is not None else sender


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


STATUS_DIVIDER = "━━━━━━━━━━━━━━━━━━━━"
_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096


def wib_clock() -> str:
    """Jam sekarang WIB (HH:MM:SS) untuk penanda "terakhir dicek"."""
    return (datetime.utcnow() + timedelta(hours=7)).strftime("%H:%M:%S")


def relabel_recheck_button(markup, callback_prefix: str, label: str = "Cek Ulang"):
    """Salin keyboard dengan tombol berawalan callback_prefix diberi label baru (isi lain tak berubah)."""
    if markup is None:
        return None
    from bot.utils.emojis import CUSTOM_EMOJI_IDS

    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            if button.callback_data and str(button.callback_data).startswith(callback_prefix):
                new_row.append(InlineKeyboardButton(
                    label, callback_data=button.callback_data,
                    icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("SWAP", "5310107765874632305")))
            else:
                new_row.append(button)
        rows.append(new_row)
    return InlineKeyboardMarkup(rows)


async def refresh_message_status(query, status_html: str, reply_markup=None) -> bool:
    """Perbarui blok status di pesan yang SAMA (teks biasa atau caption foto) — tidak membuat pesan baru.

    Dipakai tombol "Cek Ulang": tiap klik menimpa blok status di bawah pesan (dengan jam cek terbaru),
    bukan menambah pesan duplikat yang terlihat seperti spam. Return True bila pesan sudah menampilkan
    status itu; False bila tidak bisa diedit (mis. melewati batas panjang) sehingga pemanggil memakai
    pop-up (query.answer) sebagai gantinya.
    """
    message = getattr(query, "message", None)
    if message is None:
        return False
    is_caption = getattr(message, "caption", None) is not None and not getattr(message, "text", None)
    base = (getattr(message, "caption_html", None) if is_caption else getattr(message, "text_html", None)) or ""
    if STATUS_DIVIDER in base:
        base = base.split(STATUS_DIVIDER)[0]
    base = base.rstrip()
    new_text = f"{base}\n\n{STATUS_DIVIDER}\n{status_html}"
    limit = _CAPTION_LIMIT if is_caption else _TEXT_LIMIT
    if len(re.sub(r"<[^>]+>", "", _html.unescape(new_text))) > limit:
        return False
    markup = reply_markup if reply_markup is not None else getattr(message, "reply_markup", None)
    try:
        if is_caption:
            await query.edit_message_caption(caption=new_text, parse_mode="HTML", reply_markup=markup)
        else:
            await query.edit_message_text(text=new_text, parse_mode="HTML", reply_markup=markup)
        return True
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return True  # isi sama persis (klik di detik yang sama): status sudah tampil
        logger.warning("Gagal memperbarui status pesan: %s", exc)
        return False
    except Exception as exc:
        logger.warning("Gagal memperbarui status pesan: %s", exc)
        return False


async def safe_send_message(sender, chat_id: int, text: str, parse_mode="HTML",
                            reply_markup=None, message_thread_id=None) -> bool:
    """
    Kirim pesan Telegram dengan aman dan fallback otomatis jika parse HTML gagal.
    message_thread_id dipertahankan saat retry agar pesan tidak jatuh ke topik General.
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

        thread = int(message_thread_id) if message_thread_id else None
        try:
            await bot_obj.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                message_thread_id=thread
            )
            return True
        except Exception as send_err:
            # Kirim ulang tanpa parse_mode HANYA bila Telegram menolak format (BadRequest). Timeout/jaringan
            # bisa berarti pesan sudah terkirim; kirim ulang akan menggandakan notifikasi uang ke user.
            if parse_mode and isinstance(send_err, BadRequest):
                logger.warning("Retry kirim pesan ke %s tanpa parse_mode karena: %s", chat_id, send_err)
                await bot_obj.send_message(
                    chat_id=int(chat_id),
                    text=text,
                    parse_mode=None,
                    reply_markup=reply_markup,
                    message_thread_id=thread
                )
                return True
            raise send_err
    except Exception as exc:
        logger.warning("Gagal kirim pesan ke %s: %s", chat_id, exc)
        return False


KIND_ALIAS = {"buy": "beli", "sell": "jual", "swap": "convert", "convert": "convert",
              "ops": "ops", "error": "error", "alarm": "alarm", "topup": "topup"}
KIND_RESMI = ("beli", "jual", "convert", "error", "alarm", "topup", "ops")


def normalisasi_kind(kind):
    kind = (kind or "ops").lower()
    kind = KIND_ALIAS.get(kind, kind)
    return kind if kind in KIND_RESMI else "ops"


def _target_row(kind):
    """Baris NotificationTarget untuk jenis; None bila belum dipasang."""
    try:
        from database.connection import SessionLocal
        from database.models import NotificationTarget
        db = SessionLocal()
        try:
            return db.query(NotificationTarget).filter(
                NotificationTarget.kind == normalisasi_kind(kind)).first()
        finally:
            db.close()
    except Exception:
        return None


def admin_notification_targets(kind=None):
    """[(chat_id, thread_id), ...] tujuan notifikasi; fallback DM admin bila belum dipasang."""
    from config.settings import settings
    row = _target_row(kind)
    if row:
        return [(str(row.chat_id), int(row.thread_id) if row.thread_id else None)]
    return [(str(a), None) for a in dict.fromkeys(settings.ADMIN_CHAT_IDS)]


async def kirim_ke_topik(sender, kind=None, order_type=None, text=None, photo=None,
                         parse_mode="HTML", reply_markup=None) -> bool:
    """Kirim pesan/foto ke topik fitur yang sudah dipasang; no-op bila belum dipasang."""
    kind = normalisasi_kind(kind or order_type)
    row = _target_row(kind)
    if not row:
        return False
    bot = resolve_bot(sender)
    try:
        if photo:
            await bot.send_photo(chat_id=str(row.chat_id), photo=photo, caption=text,
                                 parse_mode=parse_mode, reply_markup=reply_markup,
                                 message_thread_id=int(row.thread_id) if row.thread_id else None)
        else:
            await bot.send_message(chat_id=str(row.chat_id), text=text, parse_mode=parse_mode,
                                   reply_markup=reply_markup,
                                   message_thread_id=int(row.thread_id) if row.thread_id else None)
        return True
    except Exception as exc:
        logger.warning("Kirim ke topik %s gagal: %s", kind, exc)
        return False


async def notify_admins(sender, text: str, parse_mode="HTML", reply_markup=None,
                        order_type=None, kind=None, butuh_tindakan=False) -> None:
    """Kirim notifikasi ke topik/grup sesuai jenis fitur.

    Pesan yang butuh tindakan admin (ber-tombol/flag butuh_tindakan) juga disalin
    ke DM masing-masing admin supaya bisa konfirmasi langsung dari HP.
    Jika pengiriman ke topik gagal, retry dengan thread yang sama lalu eskalasi
    ke DM admin; tidak pernah diam-diam jatuh ke topik General.
    """
    from config.settings import settings
    kind = normalisasi_kind(kind or order_type)
    row = _target_row(kind)
    tujuan = admin_notification_targets(kind)
    bot = resolve_bot(sender)
    gagal_topik = []
    for chat_id, thread_id in tujuan:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode,
                                   reply_markup=reply_markup,
                                   message_thread_id=thread_id or None)
        except Exception as exc:
            logger.warning("Notifikasi %s ke %s (thread %s) gagal: %s",
                           kind, chat_id, thread_id or "-", exc)
            ok = await safe_send_message(sender, chat_id, text, parse_mode=parse_mode,
                                         reply_markup=reply_markup,
                                         message_thread_id=thread_id or None)
            if not ok and thread_id:
                gagal_topik.append(thread_id)
    if row is not None and (butuh_tindakan or reply_markup is not None):
        for admin_id in dict.fromkeys(settings.ADMIN_CHAT_IDS):
            await safe_send_message(sender, admin_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    if gagal_topik:
        alasan = ", ".join(f"thread {t}" for t in gagal_topik)
        for admin_id in dict.fromkeys(settings.ADMIN_CHAT_IDS):
            await safe_send_message(
                sender, admin_id,
                f"\u26a0\ufe0f Notifikasi <b>{kind}</b> gagal masuk topik ({alasan}); "
                f"pesan dikirim ke sini. Jalankan ulang /settarget {kind} di topik tujuan "
                f"bila topik sudah berubah.\n\n{text}",
                parse_mode=parse_mode, reply_markup=reply_markup)
