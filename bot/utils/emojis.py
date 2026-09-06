"""
bot/utils/emojis.py — Helper & Konfigurasi Telegram Custom Emoji (3D Animated Premium).
========================================================================================
Mendukung format Telegram Bot API Custom Emoji:
<tg-emoji emoji-id="1234567890">👋</tg-emoji>

Menggunakan ID dari Telegram's built-in animated emoji packs (Forum Topic Icons,
RestrictedEmoji, NewsEmoji, HandEmoji, dll.) yang bersifat universal dan ANIMATED.

Jika custom_emoji_id kosong, fungsi tg_emoji() otomatis melakukan fallback ke Unicode Emoji.
"""

# ============================================================================
# Pemetaan ID Custom Emoji Telegram — Animated 3D dari built-in packs Telegram
# Semua ID di bawah sudah diverifikasi ANIMATED via getForumTopicIconStickers
# dan getCustomEmojiStickers Bot API.
# ============================================================================
CUSTOM_EMOJI_IDS = {
    # --- Greeting & Status ---
    "WAVE":       "5368324170671202286",  # 👍 (HandEmoji — animated wave/thumbs)
    "CALENDAR":   "5433614043006903194",  # 📆 (Forum — animated calendar)
    "BOT":        "5309832892262654231",  # 🤖 (Forum — animated robot)
    "USER":       "5357107601584693888",  # 👑 (Forum — animated crown as user badge)
    "CROWN":      "5357107601584693888",  # 👑 (Forum — animated crown)
    "VERIFIED":   "5237699328843200968",  # ✅ (Forum — animated checkmark shield)

    # --- Chart & Analytics ---
    "CHART":      "5350305691942788490",  # 📈 (Forum — animated chart up)
    "CHART_UP":   "5350305691942788490",  # 📈 (Forum — animated chart up)

    # --- Money & Finance ---
    "MONEY_BAG":  "5350452584119279096",  # 💰 (Forum — animated money bag)
    "DOLLAR":     "5309929258443874898",  # 💸 (Forum — animated flying money)
    "CARD":       "5348227245599105972",  # 💼 (Forum — animated briefcase/wallet)
    "COIN":       "5377690785674175481",  # 🪙 (Forum — animated coin)

    # --- Shopping & Orders ---
    "CART":       "5431492767249342908",  # 🛒 (Forum — animated shopping cart)
    "BOX":        "5350699789551935589",  # 🛍 (Forum — animated shopping bag)
    "SWAP":       "5310107765874632305",  # 💱 (Forum — animated currency exchange)

    # --- Status Indicators ---
    "CHECK":      "5237699328843200968",  # ✅ (Forum — animated green check)
    "CROSS":      "5462882007451185227",  # 🚫 (GameEmoji — animated cross/ban)
    "WARNING":    "5447644880824181073",  # ⚠️ (NewsEmoji — animated warning)

    # --- Communication ---
    "PHONE":      "5409357944619802453",  # 📱 (Forum — animated phone)
    "CHAT":       "5417915203100613993",  # 💬 (Forum — animated chat bubble)
    "HISTORY":    "5373251851074415873",  # 📝 (Forum — animated notepad/history)

    # --- Effects & Flair ---
    "FIRE":       "5312241539987020022",  # 🔥 (Forum — animated fire)
    "ROCKET":     "5312016608254762256",  # ⚡️ (Forum — animated lightning/rocket)
    "DIAMOND":    "5309958691854754293",  # 💎 (Forum — animated diamond)
    "SPARKLES":   "5472164874886846699",  # ✨ (RestrictedEmoji — animated sparkles)
    "STAR":       "5235579393115438657",  # ⭐️ (Forum — animated star)
    "PARTY":      "5310228579009699834",  # 🎉 (Forum — animated party)
}


def tg_emoji(key: str, fallback: str) -> str:
    """
    Menghasilkan tag <tg-emoji emoji-id="...">fallback</tg-emoji> jika custom_emoji_id terdaftar,
    atau fallback unicode emoji jika belum ada ID.
    """
    emoji_id = CUSTOM_EMOJI_IDS.get(key, "").strip()
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback


# Shortcut siap pakai untuk tampilan pesan
E_WAVE = lambda: tg_emoji("WAVE", "👋")
E_CALENDAR = lambda: tg_emoji("CALENDAR", "🗓️")
E_BOT = lambda: tg_emoji("BOT", "🤖")
E_USER = lambda: tg_emoji("USER", "👤")
E_CROWN = lambda: tg_emoji("CROWN", "👑")
E_VERIFIED = lambda: tg_emoji("VERIFIED", "🛡️")
E_CHART = lambda: tg_emoji("CHART", "📊")
E_CHART_UP = lambda: tg_emoji("CHART_UP", "📈")
E_MONEY = lambda: tg_emoji("MONEY_BAG", "💰")
E_DOLLAR = lambda: tg_emoji("DOLLAR", "💵")
E_CARD = lambda: tg_emoji("CARD", "💳")
E_COIN = lambda: tg_emoji("COIN", "🪙")
E_CART = lambda: tg_emoji("CART", "🛒")
E_BOX = lambda: tg_emoji("BOX", "📦")
E_SWAP = lambda: tg_emoji("SWAP", "🔄")
E_CHECK = lambda: tg_emoji("CHECK", "✅")
E_CROSS = lambda: tg_emoji("CROSS", "❌")
E_WARN = lambda: tg_emoji("WARNING", "⚠️")
E_PHONE = lambda: tg_emoji("PHONE", "☎️")
E_CHAT = lambda: tg_emoji("CHAT", "💬")
E_HISTORY = lambda: tg_emoji("HISTORY", "📜")
E_ROCKET = lambda: tg_emoji("ROCKET", "🚀")
E_DIAMOND = lambda: tg_emoji("DIAMOND", "💎")
E_SPARKLES = lambda: tg_emoji("SPARKLES", "✨")
E_FIRE = lambda: tg_emoji("FIRE", "🔥")
E_STAR = lambda: tg_emoji("STAR", "⭐️")
E_PARTY = lambda: tg_emoji("PARTY", "🎉")
