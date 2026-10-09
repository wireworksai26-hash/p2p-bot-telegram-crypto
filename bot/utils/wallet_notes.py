"""
bot/utils/wallet_notes.py — Catatan peringatan soal alamat exchange (Beli / Jual / Convert).
==========================================================================================
Exchange / dompet kustodian (mis. Cwallet) memakai alamat bersama dan memotong biaya penarikan:
jumlah yang sampai bisa kurang dari yang diminta, dan koin yang dikirim ke alamat deposit exchange
bisa tersangkut. Ikon jaringan BSC dipakai sebagai penanda contoh; tanpa ID emoji kustom, otomatis
jatuh ke emoji biasa.
"""
from bot.utils.emojis import tg_emoji


def _bsc() -> str:
    return tg_emoji("NET_BSC", "🟡")


def exchange_send_note() -> str:
    """Untuk layar setoran Jual / Convert: user mengirim koin KE bot."""
    return (
        f"🚫 <b>Jangan kirim dari akun exchange</b> seperti {_bsc()} Cwallet atau sejenisnya: "
        f"jumlah yang sampai bisa kurang dan pengirimnya sulit dilacak. Kirim dari wallet pribadi."
    )


def exchange_receive_note() -> str:
    """Untuk layar input alamat penerima (Beli / tujuan Convert): bot mengirim koin KE user."""
    return (
        f"🚫 <i>Jangan isi alamat deposit exchange seperti {_bsc()} Cwallet atau sejenisnya; "
        f"koin bisa tersangkut. Pakai alamat wallet pribadi.</i>"
    )
