"""
bot/utils/command_menu.py — Menu tombol ☰ (daftar perintah saat mengetik "/") khusus admin.
==========================================================================================
Dipasang per chat lewat BotCommandScopeChat. Dipakai oleh:
- startup (main.set_bot_commands) untuk semua admin + grup admin,
- /admin (otomatis, sekali per proses per chat) sehingga admin tidak perlu menunggu restart,
- /refreshmenu (paksa pasang ulang + laporan hasil per chat).
"""
import asyncio
import logging

from telegram import BotCommand, BotCommandScopeChat

from config.settings import settings

logger = logging.getLogger(__name__)

# Urutan = urutan tampil. Nama: a-z0-9_ maks 32; deskripsi 3-256 karakter.
ADMIN_COMMAND_MENU = [
    ("admin", "Dashboard admin (semua menu)"),
    ("panduan", "Panduan lengkap fitur admin"),
    ("orders", "Antrean order"),
    ("sellorders", "Dashboard order Jual crypto"),
    ("confirm", "Konfirmasi order selesai: /confirm ORDER_ID"),
    ("verifysell", "Cek ulang deposit Jual: /verifysell ORDER_ID"),
    ("stats", "Statistik & volume transaksi"),
    ("report", "Laporan mingguan"),
    ("refreshwallet", "Sinkron saldo wallet stok"),
    ("setspread", "Atur spread harga: /setspread KOIN PERSEN"),
    ("credit", "Kirim saldo bot ke user"),
    ("topupbot", "Top up kas bot"),
    ("topupqris", "Top up kas bot via QRIS"),
    ("campaign", "Campaign & giveaway"),
    ("setreferral", "Atur program referral"),
    ("broadcast", "Siaran pesan ke semua user"),
    ("ban", "Blokir user: /ban USER_ID"),
    ("unban", "Buka blokir user: /unban USER_ID"),
    ("testtesti", "Tes koneksi channel testimoni"),
    ("posttesti", "Posting testimoni order: /posttesti ORDER_ID"),
    ("postlasttesti", "Posting testimoni order selesai terakhir"),
    ("targets", "Daftar tujuan notifikasi admin"),
    ("checkapi", "Cek status API & RPC"),
    ("chatid", "Lihat chat ID ini"),
    ("txhash", "Kirim TX Hash deposit Jual/Convert"),
    ("refreshmenu", "Pasang ulang menu ☰ admin"),
    ("start", "Menu utama bot"),
    ("cancel", "Membatalkan proses berjalan"),
]

# Menu ☰ untuk SEMUA user (scope default). Tanpa daftar ini tombol ☰ tiga garis tidak muncul
# di chat dengan bot (mis. setelah token diganti ke bot baru).
USER_COMMAND_MENU = [
    ("start", "Menu utama"),
    ("beli", "Beli crypto"),
    ("jual", "Jual crypto"),
    ("convert", "Convert / Swap crypto"),
    ("cancel", "Membatalkan transaksi"),
]

# Chat yang sudah dipasang pada proses ini (agar /admin tidak memanggil Telegram tiap kali).
_applied_chats: set = set()
_default_menu_ok = False


async def ensure_default_menu(bot) -> bool:
    """
    Pastikan menu ☰ default terpasang di bot ini. Dicek ke Telegram sekali per proses; bila kosong
    (bot baru / startup gagal memasang) langsung dipasang. Tidak pernah melempar.
    """
    global _default_menu_ok
    if _default_menu_ok:
        return True
    try:
        wanted = [BotCommand(cmd, desc) for cmd, desc in USER_COMMAND_MENU]

        async def _apply():
            current = await bot.get_my_commands()
            if [(c.command, c.description) for c in current] != [(c.command, c.description) for c in wanted]:
                await bot.set_my_commands(wanted)
                logger.info("Menu ☰ default dipasang otomatis (%d perintah).", len(wanted))

        await asyncio.wait_for(_apply(), timeout=5)  # jangan menahan /start bila API lambat
        _default_menu_ok = True
    except Exception as exc:
        logger.warning("Menu ☰ default gagal dipasang: %s", exc)
    return _default_menu_ok


def admin_menu_chat_ids() -> list:
    """Chat yang mendapat menu admin: tiap admin + grup admin (bila diatur), tanpa duplikat."""
    ids = list(settings.ADMIN_CHAT_IDS)
    if settings.ADMIN_GROUP_ID:
        ids.append(settings.ADMIN_GROUP_ID)
    return list(dict.fromkeys(ids))


async def apply_admin_command_menu(bot, chat_id: int) -> int:
    """Pasang menu admin untuk satu chat. Return jumlah perintah yang terbaca balik dari Telegram
    (atau jumlah yang dikirim bila pembacaan balik tidak tersedia). Melempar error bila gagal."""
    scope = BotCommandScopeChat(chat_id)
    commands = [BotCommand(cmd, desc) for cmd, desc in ADMIN_COMMAND_MENU]
    await bot.set_my_commands(commands, scope=scope)
    _applied_chats.add(chat_id)
    try:
        stored = await bot.get_my_commands(scope=scope)
        return len(stored)
    except Exception:
        return len(commands)


async def ensure_admin_menu(bot, chat_id: int) -> None:
    """Pasang menu admin sekali per chat per proses (dipanggil dari /admin). Tidak pernah melempar."""
    if chat_id in _applied_chats:
        return
    try:
        await apply_admin_command_menu(bot, chat_id)
        logger.info("Menu admin terpasang otomatis untuk chat %s", chat_id)
    except Exception as exc:
        logger.warning("Menu admin gagal dipasang untuk chat %s: %s", chat_id, exc)
