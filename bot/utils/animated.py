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

import inspect
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


# ---------------------------------------------------------------------------
# Fallback otomatis: emoji animasi hanya boleh dipakai bot yang usernamenya dibeli di Fragment
# atau yang pemiliknya akun Telegram Premium. Bila Telegram menolak (mis. setelah ganti token
# ke bot yang pemiliknya bukan Premium), pesan dikirim ulang dengan emoji Unicode biasa,
# bukan gagal total. State disimpan di modul karena objek Bot PTB tidak boleh diberi atribut baru.
# ---------------------------------------------------------------------------
_STATE = {"rejects": 0, "accepts": 0, "warned": False}
_TG_EMOJI_RE = re.compile(r"<tg-emoji\b[^>]*>(.*?)</tg-emoji>", re.S)
_METHODS = ("send_message", "edit_message_text", "send_photo", "send_document",
            "edit_message_caption", "edit_message_reply_markup")
_SIGS = {name: inspect.signature(getattr(ExtBot, name)) for name in _METHODS}


def _icon_chars() -> dict:
    """id emoji animasi -> karakter Unicode (untuk label tombol saat icon dilepas)."""
    chars = {v: k for k, v in ANIMATED_EMOJI.items()}
    try:
        from bot.utils import emojis as E
        for key, cid in E.DEFAULT_EMOJI_IDS.items():
            alt = E.DEFAULT_EMOJI_ALTS.get(key)
            if cid and alt:
                chars.setdefault(str(cid), alt)
    except Exception:
        pass
    return chars


def _has_custom_emoji(params: dict) -> bool:
    for field in ("text", "caption"):
        value = params.get(field)
        if isinstance(value, str) and "<tg-emoji" in value:
            return True
    for field in ("entities", "caption_entities"):
        if any(getattr(e, "type", None) == "custom_emoji" for e in (params.get(field) or ())):
            return True
    markup = params.get("reply_markup")
    rows = getattr(markup, "inline_keyboard", None) or ()
    return any(getattr(b, "icon_custom_emoji_id", None) for row in rows for b in row)


def _strip_custom_emoji(params: dict, bot) -> dict:
    """Salinan params tanpa emoji animasi: tag <tg-emoji> jadi karakternya, icon tombol jadi awalan label."""
    out = dict(params)
    for field in ("text", "caption"):
        if isinstance(out.get(field), str):
            out[field] = _TG_EMOJI_RE.sub(r"\1", out[field])
    for field in ("entities", "caption_entities"):
        if out.get(field):
            out[field] = [e for e in out[field] if getattr(e, "type", None) != "custom_emoji"] or None
    markup = out.get("reply_markup")
    if getattr(markup, "inline_keyboard", None):
        chars = _icon_chars()
        data = markup.to_dict()
        for row in data["inline_keyboard"]:
            for btn in row:
                icon = btn.pop("icon_custom_emoji_id", None)
                if icon:
                    btn["text"] = f"{chars.get(str(icon), '')} {btn['text']}".strip()
        out["reply_markup"] = type(markup).de_json(data, bot)
    return out


def _is_retryable(exc) -> bool:
    return isinstance(exc, BadRequest) and "not modified" not in str(exc).lower()


class AnimatedExtBot(ExtBot):
    """
    ExtBot dengan dua tugas:
    1. Pesan HTML ke chat admin dianimasikan otomatis (emoji Unicode -> <tg-emoji>).
    2. Semua pesan yang membawa emoji animasi dikirim ulang tanpa emoji animasi bila
       Telegram menolaknya (bot tanpa hak emoji animasi).
    """

    __slots__ = ()

    async def _animated_call(self, name, args, kwargs):
        parent = getattr(ExtBot, name)
        params = dict(_SIGS[name].bind(self, *args, **kwargs).arguments)
        params.pop("self", None)

        if name in ("send_message", "edit_message_text", "send_photo", "send_document") \
                and _is_html(params) and _is_admin_chat(params.get("chat_id")):
            for field in ("text", "caption"):
                if isinstance(params.get(field), str):
                    params[field] = animate_html(params[field])

        if not _has_custom_emoji(params):
            return await parent(self, **params)

        # Setelah beberapa penolakan berturut-turut tanpa satu pun sukses, lepas emoji lebih dulu
        # (hemat satu request gagal per pesan).
        if _STATE["rejects"] >= 2 and _STATE["accepts"] == 0:
            return await parent(self, **_strip_custom_emoji(params, self))

        # Upload (mis. foto QRIS dari BytesIO) sudah terbaca habis pada percobaan pertama; ingat posisinya
        # agar kirim ulang tidak mengirim file kosong.
        streams = [(v, v.tell()) for v in params.values() if hasattr(v, "seek") and hasattr(v, "tell")]
        try:
            result = await parent(self, **params)
            _STATE["accepts"] += 1
            return result
        except BadRequest as exc:
            if not _is_retryable(exc):
                raise
            for stream, position in streams:
                stream.seek(position)
            plain = _strip_custom_emoji(params, self)
            result = await parent(self, **plain)  # bila ini juga gagal, error aslinya yang naik
            _STATE["rejects"] += 1
            if not _STATE["warned"]:
                _STATE["warned"] = True
                logger.warning(
                    "Telegram menolak emoji animasi (%s); pesan dikirim ulang dengan emoji biasa. "
                    "Emoji animasi hanya bisa dipakai bot milik akun Telegram Premium atau bot dengan "
                    "username dari Fragment.", exc)
            return result


def _make_method(name):
    async def method(self, *args, **kwargs):
        return await self._animated_call(name, args, kwargs)
    method.__name__ = name
    method.__qualname__ = f"AnimatedExtBot.{name}"
    return method


for _name in _METHODS:
    setattr(AnimatedExtBot, _name, _make_method(_name))


def install_animated_bot(application) -> bool:
    """Ganti kelas bot Application menjadi AnimatedExtBot (tata letak objek identik)."""
    try:
        bot = application.bot
        if isinstance(bot, ExtBot) and not isinstance(bot, AnimatedExtBot):
            object.__setattr__(bot, "__class__", AnimatedExtBot)
        active = isinstance(application.bot, AnimatedExtBot)
        if active:
            logger.info("Emoji animasi admin aktif (pesan ke ADMIN_CHAT_IDS).")
        return active
    except Exception as exc:
        logger.warning("Emoji animasi admin tidak aktif: %s", exc)
        return False
