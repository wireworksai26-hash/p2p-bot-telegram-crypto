"""
bot/utils/emojis.py — Helper & Konfigurasi Telegram Custom Emoji (3D Animated Premium).
========================================================================================
Mendukung format Telegram Bot API Custom Emoji:
<tg-emoji emoji-id="1234567890">👋</tg-emoji>

Jika custom_emoji_id kosong, fungsi tg_emoji() otomatis melakukan fallback ke Unicode Emoji.
Admin dapat menyetel ID custom emoji di dictionary CUSTOM_EMOJI_IDS di bawah ini.
"""

# Pemetaan ID Custom Emoji Telegram (Dapat diisi custom_emoji_id dari stiker/emoji pack Telegram Premium)
CUSTOM_EMOJI_IDS = {
    "WAVE": "",          # 👋
    "CALENDAR": "",      # 🗓️
    "BOT": "",           # 🤖
    "USER": "",          # 👤
    "CROWN": "",         # 👑
    "VERIFIED": "",      # 🛡️
    "CHART": "",         # 📊
    "CHART_UP": "",      # 📈
    "MONEY_BAG": "",     # 💰
    "DOLLAR": "",        # 💵
    "CARD": "",          # 💳
    "COIN": "",          # 🪙
    "CART": "",          # 🛒
    "BOX": "",           # 📦
    "SWAP": "",          # 🔄
    "CHECK": "",         # ✅
    "CROSS": "",         # ❌
    "WARNING": "",       # ⚠️
    "PHONE": "",         # ☎️
    "CHAT": "",          # 💬
    "HISTORY": "",       # 📜
    "FIRE": "",          # 🔥
    "ROCKET": "",        # 🚀
    "DIAMOND": "",       # 💎
    "SPARKLES": "",      # ✨
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
