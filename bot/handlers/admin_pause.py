"""
bot/handlers/admin_pause.py — Panel & perintah admin: pause dan jadwal maintenance / update sistem
==================================================================================================
Panel   : Dashboard Admin ➔ ⏸ Pause & Jadwal Maintenance
Perintah: /pause                          -> panel
          /pause 22:00 60m Update sistem  -> pratinjau jadwal (dikonfirmasi dulu sebelum diumumkan)
          /pause 12/10 22:00 2j Migrasi   -> dengan tanggal
          /pause now [durasi] [catatan]   -> pause sekarang tanpa pengumuman ke user
          /pause off                      -> lanjutkan sekarang
          /pause batal                    -> batalkan jadwal yang belum mulai
Efek pause: transaksi BARU dijeda (bot/utils/maintenance_guard.py); order berjalan tetap diproses.
"""

import html
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from services import system_pause as sp

logger = logging.getLogger(__name__)

AWAITING_FLAG = "admin_awaiting_pause_schedule"   # prefiks admin_awaiting_ dibersihkan _clear_reward_flags
PENDING_KEY = "pause_pending_schedule"

SCHEDULE_HELP = (
    "🗓 <b>JADWALKAN MAINTENANCE</b>\n\n"
    "Ketik jadwal dalam WIB dengan format:\n"
    "<code>HH:MM [durasi] [catatan]</code>\n\n"
    "Contoh:\n"
    "• <code>22:00 60m Update sistem</code>\n"
    "• <code>23:30 2j Migrasi server</code>\n"
    "• <code>12/10 01:00 90m Maintenance database</code>\n\n"
    "Tanpa tanggal = hari ini (atau besok bila jamnya sudah lewat). Tanpa durasi = pause sampai "
    "Anda menekan ▶️ Lanjutkan. Anda akan melihat pratinjau sebelum pengumuman dikirim.\n"
    "Ketik /cancel untuk batal."
)


def _back_row():
    return [InlineKeyboardButton("🔄 Refresh", callback_data="admin_panel_pause"),
            InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")]


def build_pause_view(now: datetime = None) -> tuple:
    """(teks, keyboard) panel pause."""
    now = now or datetime.utcnow()
    info = sp.pending_info()
    lines = ["⏸ <b>PAUSE &amp; JADWAL MAINTENANCE</b>\n"]
    if info is None:
        lines.append("🟢 <b>Normal</b> — semua transaksi berjalan.")
        keyboard = [
            [InlineKeyboardButton("⏸ Pause Sekarang (sampai dilanjutkan)", callback_data="admin_panel_pause_now_0")],
            [InlineKeyboardButton("⏸ Pause ½ jam", callback_data="admin_panel_pause_now_30"),
             InlineKeyboardButton("⏸ Pause 1 jam", callback_data="admin_panel_pause_now_60")],
            [InlineKeyboardButton("🗓 Jadwalkan Maintenance + Pengingat", callback_data="admin_panel_pause_sched")],
        ]
    elif info.start_at <= now:
        lines.append(f"⏸ <b>PAUSE AKTIF</b> sejak {sp.fmt_wib(info.start_at)}")
        lines.append(f"Selesai: <b>{sp.fmt_wib(info.end_at)}</b> (dibuka otomatis)" if info.end_at
                     else "Selesai: sampai Anda menekan <b>▶️ Lanjutkan</b>")
        lines.append("Kabar ke user saat selesai: " + ("ya" if info.announce else "tidak (pause mendadak)"))
        keyboard = [[InlineKeyboardButton("▶️ Lanjutkan Sekarang", callback_data="admin_panel_pause_resume")]]
    else:
        lines.append(f"🗓 <b>TERJADWAL</b>: {sp.window_text(info)}")
        lines.append("Pengumuman, pengingat 60 &amp; 10 menit, pause otomatis saat mulai, dan kabar "
                     "aktif kembali dikirim otomatis.")
        keyboard = [[InlineKeyboardButton("❌ Batalkan Jadwal", callback_data="admin_panel_pause_cancel")]]
    if info is not None and info.note:
        lines.append(f"Catatan: <i>{html.escape(info.note)}</i>")
    lines.append(
        "\nSaat pause, user <b>tidak bisa memulai</b> Beli, Jual, Convert, Topup, dan Withdraw. Order yang "
        "sedang berjalan (deposit, kirim TX hash, payout) tetap diproses. Admin tidak terkena pause."
    )
    keyboard.append(_back_row())
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def _show(query, text: str, markup) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


def _preview(start_at, end_at, note: str) -> tuple:
    """(teks, keyboard) pratinjau jadwal sebelum pengumuman dikirim."""
    from database.connection import SessionLocal
    from database.crud import get_segment_count
    info = sp.PauseInfo(id=0, start_at=start_at, end_at=end_at, note=note, announce=True, status="scheduled")
    db = SessionLocal()
    try:
        total = get_segment_count(db, "all")
    finally:
        db.close()
    finish = ("dibuka otomatis + kabar ke user" if end_at
              else "tekan ▶️ Lanjutkan di panel (lalu kabar ke user)")
    text = (
        "🗓 <b>PRATINJAU JADWAL MAINTENANCE</b>\n\n"
        f"Waktu: <b>{sp.window_text(info)}</b>\n"
        "Pengingat: 60 &amp; 10 menit sebelum mulai (yang masih sempat)\n"
        f"Saat mulai: transaksi baru dijeda otomatis\nSaat selesai: {finish}\n\n"
        f"Pengumuman di bawah akan dikirim <b>sekarang</b> ke <b>{total} user</b>:\n"
        "──────────\n"
        f"{sp.announcement_text(info)}\n"
        "──────────"
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Jadwalkan & Umumkan", callback_data="admin_panel_pause_ok"),
        InlineKeyboardButton("❌ Batal", callback_data="admin_panel_pause"),
    ]])
    return text, markup


def _store_pending(context, start_at, end_at, note: str) -> None:
    context.user_data[PENDING_KEY] = {
        "start": start_at.isoformat(), "end": end_at.isoformat() if end_at else None, "note": note}


async def _confirm_schedule(query, context, admin_id: int) -> None:
    pending = context.user_data.pop(PENDING_KEY, None)
    if not pending:
        await query.answer("Pratinjau sudah kedaluwarsa. Jadwalkan ulang.", show_alert=True)
        await _show(query, *build_pause_view())
        return
    start_at = datetime.fromisoformat(pending["start"])
    end_at = datetime.fromisoformat(pending["end"]) if pending["end"] else None
    try:
        info = sp.schedule(start_at, end_at, pending["note"], admin_id, announce=True)
    except sp.PauseError as exc:
        await query.answer(str(exc), show_alert=True)
        await _show(query, *build_pause_view())
        return
    await sp.tick(context.bot)   # kirim pengumuman sekarang (job berkala juga akan mengirim bila terlewat)
    from bot.utils.telegram_utils import notify_admins
    await notify_admins(context.bot, f"🗓 <b>Maintenance dijadwalkan</b> oleh admin <code>{admin_id}</code>: "
                                     f"{sp.window_text(info)}.", kind="ops")
    await query.answer("Jadwal tersimpan. Pengumuman sedang dikirim ke user.")
    await _show(query, *build_pause_view())


async def _pause_now(bot, admin_id: int, minutes, note: str = "") -> str:
    """Jalankan pause mendadak; return pesan hasil untuk admin."""
    try:
        info = sp.pause_now(minutes, note, admin_id)
    except sp.PauseError as exc:
        return f"⛔ {exc}"
    from bot.utils.telegram_utils import notify_admins
    until = f" sampai {sp.fmt_wib(info.end_at)}" if info.end_at else " sampai ada yang menekan ▶️ Lanjutkan"
    await notify_admins(bot, f"⏸ <b>Pause diaktifkan</b> oleh admin <code>{admin_id}</code> — transaksi baru "
                             f"dijeda{until}. Tidak ada pengumuman ke user.", kind="ops")
    return f"⏸ Pause aktif{until}."


async def _resume(bot, admin_id: int) -> str:
    info = sp.resume_now(admin_id)
    if info is None:
        return "ℹ️ Tidak ada pause yang sedang berjalan."
    await sp.announce_resumed(bot, info, by_admin=True)
    return "▶️ Transaksi dibuka kembali." + (" Kabar ke user sedang dikirim." if info.announce else "")


async def _cancel(bot, admin_id: int) -> str:
    info = sp.cancel_scheduled(admin_id)
    if info is None:
        return "ℹ️ Tidak ada jadwal yang menunggu."
    if info.announce:
        sp.start_broadcast(bot, sp.cancelled_text(info), "Kabar jadwal maintenance dibatalkan")
    from bot.utils.telegram_utils import notify_admins
    await notify_admins(bot, f"❌ <b>Jadwal maintenance dibatalkan</b> oleh admin <code>{admin_id}</code>.",
                        kind="ops")
    return "❌ Jadwal dibatalkan." + (" Kabar ke user sedang dikirim." if info.announce else "")


async def handle_pause_callback(query, data: str, admin_id: int, context) -> None:
    """Routing tombol panel admin_panel_pause*."""
    if data == "admin_panel_pause":
        context.user_data.pop(PENDING_KEY, None)
        await _show(query, *build_pause_view())
    elif data.startswith("admin_panel_pause_now_"):
        suffix = data.rsplit("_", 1)[1]
        minutes = int(suffix) if suffix.isdigit() else 0
        await query.answer(await _pause_now(context.bot, admin_id, minutes or None), show_alert=True)
        await _show(query, *build_pause_view())
    elif data == "admin_panel_pause_resume":
        await query.answer(await _resume(context.bot, admin_id), show_alert=True)
        await _show(query, *build_pause_view())
    elif data == "admin_panel_pause_cancel":
        info = sp.pending_info()
        if info is None or info.status != "scheduled":
            await _show(query, *build_pause_view())
            return
        await _show(query, (
            "❌ <b>BATALKAN JADWAL MAINTENANCE?</b>\n\n"
            f"Jadwal: <b>{sp.window_text(info)}</b>\n"
            + ("User sudah diberi pengumuman, jadi kabar pembatalan akan dikirim ke semua user." if info.announce
               else "")),
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Ya, Batalkan", callback_data="admin_panel_pause_cancelok"),
                InlineKeyboardButton("🔙 Kembali", callback_data="admin_panel_pause"),
            ]]))
    elif data == "admin_panel_pause_cancelok":
        await query.answer(await _cancel(context.bot, admin_id), show_alert=True)
        await _show(query, *build_pause_view())
    elif data == "admin_panel_pause_sched":
        from bot.handlers.admin import _arm_reward_wizard
        _arm_reward_wizard(context, AWAITING_FLAG)
        await _show(query, SCHEDULE_HELP, InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Kembali", callback_data="admin_panel_pause")]]))
    elif data == "admin_panel_pause_ok":
        await _confirm_schedule(query, context, admin_id)
    else:
        await query.answer("Tombol tidak dikenal.", show_alert=True)


async def _reply_preview(message, context, text: str) -> bool:
    """Parse jadwal dan balas pratinjau. False bila format salah (pesan error sudah dikirim)."""
    try:
        start_at, end_at, note = sp.parse_schedule(text)
    except sp.PauseError as exc:
        await message.reply_text(f"⛔ {exc}\n\nContoh: <code>22:00 60m Update sistem</code>", parse_mode="HTML")
        return False
    _store_pending(context, start_at, end_at, note)
    preview, markup = _preview(start_at, end_at, note)
    await message.reply_text(preview, parse_mode="HTML", reply_markup=markup)
    return True


async def handle_schedule_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Input teks jadwal setelah admin menekan 🗓 Jadwalkan. True bila pesan ini ditangani."""
    if not context.user_data.get(AWAITING_FLAG):
        return False
    if await _reply_preview(update.message, context, update.message.text.strip()):
        context.user_data.pop(AWAITING_FLAG, None)
    return True


async def pause_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pause [now|off|batal|jadwal]: lihat docstring modul."""
    from bot.handlers.admin import is_admin
    if not update.message or not update.effective_user or not is_admin(update.effective_user.id):
        return
    admin_id = update.effective_user.id
    args = context.args or []
    action = args[0].lower() if args else ""
    if not action:
        text, markup = build_pause_view()
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
        return
    if action in ("now", "sekarang"):
        rest = args[1:]
        try:
            minutes = sp.parse_duration(rest[0]) if rest else None
        except sp.PauseError as exc:
            await update.message.reply_text(f"⛔ {exc}")
            return
        note = " ".join(rest[1:] if minutes is not None else rest)
        await update.message.reply_text(await _pause_now(context.bot, admin_id, minutes, note))
        return
    if action in ("off", "lanjut", "resume"):
        await update.message.reply_text(await _resume(context.bot, admin_id))
        return
    if action in ("batal", "cancel"):
        await update.message.reply_text(await _cancel(context.bot, admin_id))
        return
    await _reply_preview(update.message, context, " ".join(args))
