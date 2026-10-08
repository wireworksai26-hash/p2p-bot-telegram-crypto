"""
bot/utils/animated.py — Emoji animasi (3D) otomatis untuk panel admin
=====================================================================
Dua mekanisme, keduanya hanya menyentuh sisi admin:

1. AnimatedButton: tombol inline yang emoji di depan labelnya otomatis dijadikan
   icon_custom_emoji_id (icon animasi di tombol). Dipakai sebagai pengganti
   InlineKeyboardButton di handler admin, tanpa mengubah tiap pemanggilan.
2. install_animated_bot: pesan HTML yang dikirim ke chat admin (ADMIN_CHAT_IDS)
   otomatis memakai <tg-emoji> animasi. User biasa tidak terpengaruh.

Aman bila gagal: bila Telegram menolak pesan beranimasi, pesan dikirim ulang apa adanya.
"""

import logging
import re

from telegram import InlineKeyboardButton
from telegram.error import BadRequest
from telegram.ext import ExtBot

from bot.utils.animated_map import ANIMATED_EMOJI

logger = logging.getLogger(__name__)

# Batas aman jumlah emoji animasi per pesan (Telegram membatasi jumlah entity per pesan).
MAX_ANIMATED_PER_MESSAGE = 40

_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿⌀-⏿]️?")
_TAG_RE = re.compile(r"(<[^>]*>)")
_NO_EMOJI_TAGS = ("code", "pre", "tg-emoji")


def _norm(char: str) -> str:
    return char.replace("️", "")


def animated_id(char: str):
    return ANIMATED_EMOJI.get(_norm(char))


def split_leading_emoji(label: str):
    """('🎁 Kirim Reward') -> (id_animasi, 'Kirim Reward'); (None, label) bila tidak bisa."""
    if not label:
        return None, label
    m = _EMOJI_RE.match(label)
    if not m:
        return None, label
    emoji_id = animated_id(m.group(0))
    rest = label[m.end():].strip()
    if not emoji_id or not rest:
        return None, label
    return emoji_id, rest


def AnimatedButton(text, *args, **kwargs):
    """Sama seperti InlineKeyboardButton, tetapi emoji di depan label jadi icon animasi."""
    if isinstance(text, str) and kwargs.get("icon_custom_emoji_id") is None:
        emoji_id, rest = split_leading_emoji(text)
        if emoji_id:
            text = rest
            kwargs["icon_custom_emoji_id"] = emoji_id
    return InlineKeyboardButton(text, *args, **kwargs)


def animate_html(text, limit: int = MAX_ANIMATED_PER_MESSAGE):
    """Ganti emoji Unicode di teks HTML dengan <tg-emoji> animasi (di luar tag & blok kode)."""
    if not isinstance(text, str) or not text:
        return text
    out, blocked, count = [], 0, 0
    for part in _TAG_RE.split(text):
        if part.startswith("<") and part.endswith(">"):
            name = part.strip("<>/ ").split(" ", 1)[0].lower()
            if name in _NO_EMOJI_TAGS:
                blocked += -1 if part.startswith("</") else 1
                blocked = max(blocked, 0)
            out.append(part)
            continue
        if blocked or count >= limit:
            out.append(part)
            continue

        def _sub(m):
            nonlocal count
            emoji_id = animated_id(m.group(0))
            if not emoji_id or count >= limit:
                return m.group(0)
            count += 1
            return f'<tg-emoji emoji-id="{emoji_id}">{m.group(0)}</tg-emoji>'

        out.append(_EMOJI_RE.sub(_sub, part))
    return "".join(out)


def _is_html(kwargs) -> bool:
    return str(kwargs.get("parse_mode") or "").upper() == "HTML" and not kwargs.get("entities") \
        and not kwargs.get("caption_entities")


def _admin_ids() -> set:
    from config.settings import settings
    return {int(x) for x in settings.ADMIN_CHAT_IDS}


def _is_admin_chat(chat_id) -> bool:
    try:
        return int(chat_id) in _admin_ids()
    except (TypeError, ValueError):
        return False


async def _try_animated(call, kwargs, fields):
    """Panggil API dengan teks beranimasi; bila ditolak Telegram, ulangi dengan teks asli."""
    animated = dict(kwargs)
    changed = False
    for field in fields:
        value = animated.get(field)
        new = animate_html(value) if isinstance(value, str) else value
        if new != value:
            animated[field] = new
            changed = True
    if not changed:
        return await call(**kwargs)
    try:
        return await call(**animated)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            raise
        logger.warning("Pesan beranimasi ditolak (%s); dikirim ulang tanpa animasi.", exc)
        return await call(**kwargs)


class AnimatedExtBot(ExtBot):
    """ExtBot yang menganimasikan emoji pada pesan ke chat admin."""

    __slots__ = ()

    async def send_message(self, chat_id, text, *args, **kwargs):
        if args or not _is_html(kwargs) or not _is_admin_chat(chat_id):
            return await super().send_message(chat_id, text, *args, **kwargs)
        return await _try_animated(
            lambda **kw: super(AnimatedExtBot, self).send_message(**kw),
            {"chat_id": chat_id, "text": text, **kwargs}, ("text",))

    async def edit_message_text(self, text, chat_id=None, *args, **kwargs):
        if args or not _is_html(kwargs) or not _is_admin_chat(chat_id):
            return await super().edit_message_text(text, chat_id, *args, **kwargs)
        return await _try_animated(
            lambda **kw: super(AnimatedExtBot, self).edit_message_text(**kw),
            {"text": text, "chat_id": chat_id, **kwargs}, ("text",))

    async def send_photo(self, chat_id, photo, *args, **kwargs):
        if args or not _is_html(kwargs) or not _is_admin_chat(chat_id):
            return await super().send_photo(chat_id, photo, *args, **kwargs)
        return await _try_animated(
            lambda **kw: super(AnimatedExtBot, self).send_photo(**kw),
            {"chat_id": chat_id, "photo": photo, **kwargs}, ("caption",))

    async def send_document(self, chat_id, document, *args, **kwargs):
        if args or not _is_html(kwargs) or not _is_admin_chat(chat_id):
            return await super().send_document(chat_id, document, *args, **kwargs)
        return await _try_animated(
            lambda **kw: super(AnimatedExtBot, self).send_document(**kw),
            {"chat_id": chat_id, "document": document, **kwargs}, ("caption",))


def install_animated_bot(application) -> bool:
    """Ganti kelas bot Application menjadi AnimatedExtBot (tata letak objek identik)."""
    try:
        bot = application.bot
        if isinstance(bot, ExtBot) and not isinstance(bot, AnimatedExtBot):
            object.__setattr__(bot, "__class__", AnimatedExtBot)
        return isinstance(application.bot, AnimatedExtBot)
    except Exception as exc:
        logger.warning("Emoji animasi admin tidak aktif: %s", exc)
        return False
