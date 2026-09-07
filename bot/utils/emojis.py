"""
bot/utils/emojis.py — Helper & Konfigurasi Telegram Custom Emoji (3D Animated Premium).
========================================================================================
Mendukung format Telegram Bot API Custom Emoji:
<tg-emoji emoji-id="1234567890">👋</tg-emoji>

Mendukung:
1. ID bawaan (Built-in Animated Forum & Premium Packs Telegram)
2. Custom ID dinamis dari /syncpack atau /setemoji yang disimpan otomatis ke data/custom_emojis.json
3. Fallback graceful ke Unicode Emoji jika ID tidak tersedia
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
CUSTOM_EMOJIS_FILE = os.path.join(DATA_DIR, "custom_emojis.json")

# ============================================================================
# Pemetaan Default ID Custom Emoji Telegram (Animated 3D Built-in Packs)
# ============================================================================
DEFAULT_EMOJI_IDS = {
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
    "ROCKET":     "5203966320692969547",  # 🚀 (Emoji666D — animated rocket)
    "DIAMOND":    "5309958691854754293",  # 💎 (Forum — animated diamond)
    "SPARKLES":   "5472164874886846699",  # ✨ (RestrictedEmoji — animated sparkles)
    "STAR":       "5235579393115438657",  # ⭐️ (Forum — animated star)
    "PARTY":      "5310228579009699834",  # 🎉 (Forum — animated party)

    # --- Crypto Coins (3D Animated Custom Emojis) ---
    "COIN_USDT":  "5978561005351865286",  # 🟢 3D Green Sphere
    "COIN_USDC":  "5341683880103527632",  # 🔵 3D Blue Sphere
    "COIN_ETH":   "5309958691854754293",  # 🔷 3D Diamond / Ethereum
    "COIN_SOL":   "5341580002024503875",  # 🟣 3D Purple Sphere
    "COIN_TRX":   "5967750804596067258",  # ❤️ 3D Red Heart
    "COIN_BNB":   "5976277053413003198",  # 🟡 3D Gold Sphere
    "COIN_SUI":   "5979034860503698472",  # 💧 3D Aqua Crystal
    "COIN_TON":   "5462902520215002477",  # 💎 3D Ton Diamond
    "COIN_POL":   "5204196780048137722",  # 🟪 3D Purple Cube
    "COIN_MATIC": "5204196780048137722",  # 🟪 3D Purple Cube
    "COIN_ARB":   "5309958691854754293",  # 💎 3D Crystal
    "COIN_AVAX":  "5204321862380698787",  # 🔴 3D Red Sphere
    "COIN_KAIA":  "5474417568053745249",  # 🌱 3D Sprout
    "COIN_BERA":  "5379815450160943570",  # 🐻 3D Animated Bear
    "COIN_APT":   "5312016608254762256",  # ⚡ 3D Lightning
    "COIN_HYPE":  "5203966320692969547",  # 🚀 3D Rocket
    "COIN_G":     "5979034860503698472",  # 🌌 3D Crystal
    "COIN_BASE":  "5341683880103527632",  # 🔵 3D Blue Sphere

    # --- Network Identifiers (3D Animated) ---
    "NET_BSC":        "5976277053413003198",
    "NET_POLYGON":    "5204196780048137722",
    "NET_ARB":        "5309958691854754293",
    "NET_TON":        "5462902520215002477",
    "NET_SOLANA":     "5341580002024503875",
    "NET_ETH":        "5309958691854754293",
    "NET_BASE":       "5341683880103527632",
    "NET_OPTIMISM":   "5204321862380698787",
    "NET_ROBINHOOD":  "5978561005351865286",
    "NET_TRON":       "5967750804596067258",
    "NET_SUI":        "5979034860503698472",
    "NET_AVAX":       "5204321862380698787",
    "NET_KAIA":       "5474417568053745249",
    "NET_BERA":       "5379815450160943570",
    "NET_APTOS":      "5312016608254762256",
    "NET_HYPEREVM":   "5203966320692969547",
}

DEFAULT_EMOJI_ALTS = {
    "WAVE": "👍",
    "CALENDAR": "📆",
    "BOT": "🤖",
    "USER": "👑",
    "CROWN": "👑",
    "VERIFIED": "✅",
    "CHART": "📈",
    "CHART_UP": "📈",
    "MONEY_BAG": "💰",
    "DOLLAR": "💸",
    "CARD": "💼",
    "COIN": "🪙",
    "CART": "🛒",
    "BOX": "🛍",
    "SWAP": "💱",
    "CHECK": "✅",
    "CROSS": "🚫",
    "WARNING": "⚠️",
    "PHONE": "📱",
    "CHAT": "💬",
    "HISTORY": "📝",
    "FIRE": "🔥",
    "ROCKET": "🚀",
    "DIAMOND": "💎",
    "SPARKLES": "✨",
    "STAR": "⭐️",
    "PARTY": "🎉",

    # Coins
    "COIN_USDT": "🟢",
    "COIN_USDC": "🔵",
    "COIN_ETH":  "🔷",
    "COIN_SOL":  "🟣",
    "COIN_TRX":  "❤️",
    "COIN_BNB":  "🟡",
    "COIN_SUI":  "💧",
    "COIN_TON":  "💎",
    "COIN_POL":  "🟪",
    "COIN_MATIC": "🟪",
    "COIN_ARB":  "💎",
    "COIN_AVAX": "🔴",
    "COIN_KAIA": "🌱",
    "COIN_BERA": "🐻",
    "COIN_APT":  "⚡",
    "COIN_HYPE": "🚀",
    "COIN_G":    "🌌",
    "COIN_BASE": "🔵",

    # Networks
    "NET_BSC":        "🟡",
    "NET_POLYGON":    "🟪",
    "NET_ARB":        "💎",
    "NET_TON":        "💎",
    "NET_SOLANA":     "🟣",
    "NET_ETH":        "🔷",
    "NET_BASE":       "🔵",
    "NET_OPTIMISM":   "🔴",
    "NET_ROBINHOOD":  "🟢",
    "NET_TRON":       "❤️",
    "NET_SUI":        "💧",
    "NET_AVAX":       "🔴",
    "NET_KAIA":       "🌱",
    "NET_BERA":       "🐻",
    "NET_APTOS":      "⚡",
    "NET_HYPEREVM":   "🚀",
}

# In-memory working copies
CUSTOM_EMOJI_IDS = dict(DEFAULT_EMOJI_IDS)
CUSTOM_EMOJI_ALTS = dict(DEFAULT_EMOJI_ALTS)


def load_custom_emojis() -> None:
    """Memuat custom emoji kustom yang tersimpan dari data/custom_emojis.json."""
    global CUSTOM_EMOJI_IDS, CUSTOM_EMOJI_ALTS
    try:
        if os.path.exists(CUSTOM_EMOJIS_FILE):
            with open(CUSTOM_EMOJIS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    ids = data.get("ids", {})
                    alts = data.get("alts", {})
                    CUSTOM_EMOJI_IDS.update(ids)
                    CUSTOM_EMOJI_ALTS.update(alts)
                    logger.info("Berhasil memuat %d custom emoji kustom.", len(ids))
    except Exception as exc:
        logger.warning("Gagal memuat custom_emojis.json: %s", exc)


def save_custom_emojis() -> bool:
    """Menyimpan custom emoji aktif ke file data/custom_emojis.json."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CUSTOM_EMOJIS_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "ids": CUSTOM_EMOJI_IDS,
                "alts": CUSTOM_EMOJI_ALTS
            }, f, indent=2, ensure_ascii=False)
        return True
    except Exception as exc:
        logger.error("Gagal menyimpan custom_emojis.json: %s", exc)
        return False


def set_custom_emoji(key: str, emoji_id: str, alt_char: str = "") -> bool:
    """Mengubah atau mendaftarkan satu custom emoji spesifik."""
    key = key.upper().strip()
    CUSTOM_EMOJI_IDS[key] = str(emoji_id).strip()
    if alt_char:
        CUSTOM_EMOJI_ALTS[key] = str(alt_char).strip()
    return save_custom_emojis()


def reset_custom_emojis() -> bool:
    """Mereset semua custom emoji kembali ke default."""
    global CUSTOM_EMOJI_IDS, CUSTOM_EMOJI_ALTS
    CUSTOM_EMOJI_IDS = dict(DEFAULT_EMOJI_IDS)
    CUSTOM_EMOJI_ALTS = dict(DEFAULT_EMOJI_ALTS)
    if os.path.exists(CUSTOM_EMOJIS_FILE):
        try:
            os.remove(CUSTOM_EMOJIS_FILE)
        except Exception:
            pass
    return True


# Peta pencocokan otomatis emoji karakter bawaan -> Key sistem
CHAR_TO_KEY_MAP = {
    "👋": "WAVE",
    "👍": "WAVE",
    "🤖": "BOT",
    "👑": "CROWN",
    "👤": "USER",
    "📈": "CHART",
    "📊": "CHART",
    "💰": "MONEY_BAG",
    "💸": "DOLLAR",
    "💵": "DOLLAR",
    "💼": "CARD",
    "💳": "CARD",
    "🪙": "COIN",
    "🛒": "CART",
    "🛍": "BOX",
    "📦": "BOX",
    "💱": "SWAP",
    "🔄": "SWAP",
    "✅": "CHECK",
    "🛡": "VERIFIED",
    "🛡️": "VERIFIED",
    "🚫": "CROSS",
    "❌": "CROSS",
    "⚠️": "WARNING",
    "❗️": "WARNING",
    "📱": "PHONE",
    "☎️": "PHONE",
    "💬": "CHAT",
    "🗣": "CHAT",
    "📝": "HISTORY",
    "📜": "HISTORY",
    "🔥": "FIRE",
    "⚡️": "ROCKET",
    "⚡": "ROCKET",
    "🚀": "ROCKET",
    "💎": "DIAMOND",
    "✨": "SPARKLES",
    "⭐️": "STAR",
    "⭐": "STAR",
    "🎉": "PARTY",
    "📆": "CALENDAR",
    "🗓": "CALENDAR",
    "🗓️": "CALENDAR",
}


def sync_from_stickers(stickers: list) -> dict:
    """
    Menyinkronkan list sticker dari Telegram StickerSet (Custom Emoji Pack)
    ke konfigurasi emoji bot secara otomatis.
    """
    synced = {}
    for st in stickers:
        custom_id = getattr(st, "custom_emoji_id", None) or getattr(st, "file_unique_id", None)
        alt_emoji = getattr(st, "emoji", "") or "✨"
        
        # Cek apakah custom_id valid (string angka panjang)
        if not custom_id or not str(custom_id).isdigit():
            continue
            
        custom_id = str(custom_id)
        # Cari key yang cocok berdasarkan karakter emoji fallback
        matched_keys = [k for char, k in CHAR_TO_KEY_MAP.items() if char in alt_emoji or alt_emoji in char]
        
        if matched_keys:
            for key in matched_keys:
                CUSTOM_EMOJI_IDS[key] = custom_id
                CUSTOM_EMOJI_ALTS[key] = alt_emoji
                synced[key] = (custom_id, alt_emoji)
        else:
            # Jika tidak ada di map standar, simpan dengan nama key representatif
            key_name = f"CUSTOM_{len(synced)+1}"
            CUSTOM_EMOJI_IDS[key_name] = custom_id
            CUSTOM_EMOJI_ALTS[key_name] = alt_emoji
            synced[key_name] = (custom_id, alt_emoji)

    if synced:
        save_custom_emojis()
    return synced


# Muat data tersimpan saat module diimpor
load_custom_emojis()


def tg_emoji(key: str, fallback: str = "") -> str:
    """
    Menghasilkan tag <tg-emoji emoji-id="...">alt</tg-emoji> jika custom_emoji_id terdaftar,
    atau fallback unicode emoji jika belum ada ID.
    """
    emoji_id = CUSTOM_EMOJI_IDS.get(key, "").strip()
    alt_char = CUSTOM_EMOJI_ALTS.get(key, "").strip() or fallback or "✨"
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{alt_char}</tg-emoji>'
    return fallback or alt_char


# Shortcut siap pakai untuk tampilan pesan
E_WAVE = lambda: tg_emoji("WAVE", "👍")
E_CALENDAR = lambda: tg_emoji("CALENDAR", "📆")
E_BOT = lambda: tg_emoji("BOT", "🤖")
E_USER = lambda: tg_emoji("USER", "👑")
E_CROWN = lambda: tg_emoji("CROWN", "👑")
E_VERIFIED = lambda: tg_emoji("VERIFIED", "✅")
E_CHART = lambda: tg_emoji("CHART", "📈")
E_CHART_UP = lambda: tg_emoji("CHART_UP", "📈")
E_MONEY = lambda: tg_emoji("MONEY_BAG", "💰")
E_DOLLAR = lambda: tg_emoji("DOLLAR", "💸")
E_CARD = lambda: tg_emoji("CARD", "💼")
E_COIN = lambda: tg_emoji("COIN", "🪙")
E_CART = lambda: tg_emoji("CART", "🛒")
E_BOX = lambda: tg_emoji("BOX", "🛍")
E_SWAP = lambda: tg_emoji("SWAP", "💱")
E_CHECK = lambda: tg_emoji("CHECK", "✅")
E_CROSS = lambda: tg_emoji("CROSS", "🚫")
E_WARN = lambda: tg_emoji("WARNING", "⚠️")
E_PHONE = lambda: tg_emoji("PHONE", "📱")
E_CHAT = lambda: tg_emoji("CHAT", "💬")
E_HISTORY = lambda: tg_emoji("HISTORY", "📝")
E_ROCKET = lambda: tg_emoji("ROCKET", "🚀")
E_DIAMOND = lambda: tg_emoji("DIAMOND", "💎")
E_SPARKLES = lambda: tg_emoji("SPARKLES", "✨")
E_FIRE = lambda: tg_emoji("FIRE", "🔥")
E_STAR = lambda: tg_emoji("STAR", "⭐️")
E_PARTY = lambda: tg_emoji("PARTY", "🎉")

# Shortcut per koin crypto
E_COIN_USDT = lambda: tg_emoji("COIN_USDT", "🟢")
E_COIN_USDC = lambda: tg_emoji("COIN_USDC", "🔵")
E_COIN_ETH  = lambda: tg_emoji("COIN_ETH", "🔷")
E_COIN_SOL  = lambda: tg_emoji("COIN_SOL", "🟣")
E_COIN_TRX  = lambda: tg_emoji("COIN_TRX", "❤️")
E_COIN_BNB  = lambda: tg_emoji("COIN_BNB", "🟡")
E_COIN_SUI  = lambda: tg_emoji("COIN_SUI", "💧")
E_COIN_TON  = lambda: tg_emoji("COIN_TON", "💎")
E_COIN_POL  = lambda: tg_emoji("COIN_POL", "🟪")
E_COIN_ARB  = lambda: tg_emoji("COIN_ARB", "💎")
E_COIN_AVAX = lambda: tg_emoji("COIN_AVAX", "🔴")
E_COIN_KAIA = lambda: tg_emoji("COIN_KAIA", "🌱")
E_COIN_BERA = lambda: tg_emoji("COIN_BERA", "🐻")
E_COIN_APT  = lambda: tg_emoji("COIN_APT", "⚡")
E_COIN_HYPE = lambda: tg_emoji("COIN_HYPE", "🚀")


def get_coin_emoji(symbol: str) -> str:
    """Mengembalikan tag custom emoji untuk koin crypto tertentu."""
    sym = symbol.upper().strip()
    key = f"COIN_{sym}"
    if key in CUSTOM_EMOJI_IDS:
        return tg_emoji(key, DEFAULT_EMOJI_ALTS.get(key, "🪙"))
    return tg_emoji("COIN", "🪙")


def get_coin_emoji_id(symbol: str) -> str | None:
    """Mengembalikan custom_emoji_id string untuk tombol Telegram."""
    sym = symbol.upper().strip()
    key = f"COIN_{sym}"
    return CUSTOM_EMOJI_IDS.get(key)


def get_network_emoji_id(network: str) -> str | None:
    """Mengembalikan custom_emoji_id string untuk tombol network Telegram."""
    net = network.upper().strip()
    key = f"NET_{net}"
    return CUSTOM_EMOJI_IDS.get(key)

