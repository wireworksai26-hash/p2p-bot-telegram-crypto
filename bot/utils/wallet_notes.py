"""
bot/utils/wallet_notes.py — Catatan peringatan soal alamat exchange (Beli / Jual / Convert).
==========================================================================================
Exchange / dompet kustodian (mis. Cwallet) memakai alamat bersama dan memotong biaya penarikan:
jumlah yang sampai bisa kurang dari yang diminta, dan koin yang dikirim ke alamat deposit exchange
bisa tersangkut. Ikon jaringan BSC dipakai sebagai penanda contoh; tanpa ID emoji kustom, otomatis
jatuh ke emoji biasa.
"""
from bot.utils.emojis import CUSTOM_EMOJI_IDS, tg_emoji

# Logo exchange di catatan "jangan kirim dari exchange". Tiap logo tampil otomatis begitu ID emoji
# kustomnya terdaftar di CUSTOM_EMOJI_IDS dengan kunci di bawah; tanpa ID, hanya nama yang tampil.
EXCHANGE_LOGOS = (
    ("EXCH_GATE", "Gate.io"),
    ("EXCH_BYBIT", "Bybit"),
    ("EXCH_BITGET", "Bitget"),
    ("EXCH_CWALLET", "Cwallet"),
)


def _exchange_names() -> str:
    parts = []
    for key, name in EXCHANGE_LOGOS:
        logo = tg_emoji(key, "🏦") if CUSTOM_EMOJI_IDS.get(key, "").strip() else ""
        parts.append(f"{logo} {name}".strip())
    return ", ".join(parts)


def exchange_send_note() -> str:
    """Untuk layar setoran Jual / Convert: user mengirim koin KE bot."""
    return (
        f"🚫 <b>Jangan kirim dari akun Exchange</b> seperti {_exchange_names()} dan sejenisnya. "
        f"Jumlah yang sampai bisa kurang dan dikhawatirkan tidak terdeteksi oleh bot. "
        f"Kirim dari <b>Alamat Wallet Web3 Pribadi</b>."
    )

