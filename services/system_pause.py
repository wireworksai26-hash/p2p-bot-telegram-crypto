"""
services/system_pause.py — Pause global & jadwal maintenance / update sistem
=============================================================================
Admin bisa:
- Pause sekarang (tanpa pengumuman ke user), opsional dengan batas waktu.
- Menjadwalkan maintenance: bot mengumumkan ke semua user, mengirim pengingat 60 & 10 menit
  sebelum mulai, pause otomatis saat mulai, lalu membuka kembali + mengabari user saat selesai.

Selama pause hanya transaksi BARU (Beli/Jual/Convert/Topup/Withdraw) yang dijeda
(bot/utils/maintenance_guard.py); deposit, payout, dan TX hash order yang berjalan tetap
diproses supaya dana user tidak tertahan. Gagal baca DB = dianggap tidak pause (sama seperti
chain_maintenance), karena pause hanya pelindung tambahan.
"""

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Optional

logger = logging.getLogger(__name__)

WIB = timedelta(hours=7)
PENDING = ("scheduled", "active")
REMINDER_MINUTES = (10, 60)          # urut dari terkecil: lihat _due_reminder
MAX_DURATION_MINUTES = 3 * 24 * 60
_BULAN = ("", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")
_tasks = set()


class PauseError(ValueError):
    """Input jadwal/pause tidak valid; pesannya aman ditampilkan ke admin."""


@dataclass(frozen=True)
class PauseInfo:
    id: int
    start_at: datetime
    end_at: Optional[datetime]
    note: str
    announce: bool
    status: str


def _snapshot(row) -> PauseInfo:
    return PauseInfo(id=row.id, start_at=row.start_at, end_at=row.end_at, note=row.note or "",
                     announce=bool(row.announce), status=row.status)


def _now() -> datetime:
    return datetime.utcnow()


# ---------------- Format waktu ----------------
def fmt_wib(dt: datetime, with_date: bool = True) -> str:
    local = dt + WIB
    clock = f"{local:%H:%M} WIB"
    return f"{local.day} {_BULAN[local.month]} {clock}" if with_date else clock


def window_text(info: PauseInfo) -> str:
    """'12 Okt 22:00 WIB s/d 23:00 WIB' (tanggal akhir ditulis bila beda hari)."""
    start = fmt_wib(info.start_at)
    if info.end_at is None:
        return f"mulai {start} sampai ada kabar dari admin"
    same_day = (info.start_at + WIB).date() == (info.end_at + WIB).date()
    return f"{start} s/d {fmt_wib(info.end_at, with_date=not same_day)}"


def _note_line(info: PauseInfo) -> str:
    return f"📝 {escape(info.note)}\n" if info.note else ""


# ---------------- Status ----------------
def _pending_row(db):
    from database.models import MaintenanceWindow
    return (db.query(MaintenanceWindow).filter(MaintenanceWindow.status.in_(PENDING))
            .order_by(MaintenanceWindow.start_at).first())


def _in_effect(row, now: datetime) -> bool:
    return (row.status in PENDING and row.start_at <= now
            and (row.end_at is None or now < row.end_at))


def pending_info() -> Optional[PauseInfo]:
    """Jadwal atau pause yang belum selesai (untuk panel admin)."""
    from database.connection import SessionLocal
    db = SessionLocal()
    try:
        row = _pending_row(db)
        return _snapshot(row) if row else None
    finally:
        db.close()


def active_pause(now: Optional[datetime] = None) -> Optional[PauseInfo]:
    """Pause yang sedang berlaku saat ini, atau None. Gagal baca DB = tidak pause."""
    try:
        from database.connection import SessionLocal
        db = SessionLocal()
        try:
            row = _pending_row(db)
            return _snapshot(row) if row and _in_effect(row, now or _now()) else None
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Gagal membaca status pause (dianggap normal): %s", exc)
        return None


# ---------------- Aksi admin ----------------
def schedule(start_at: datetime, end_at: Optional[datetime], note: str, admin_id: Optional[int],
             announce: bool = True, now: Optional[datetime] = None) -> PauseInfo:
    """Buat jadwal/pause baru. Hanya satu yang boleh menunggu/berjalan pada satu waktu."""
    from database.connection import SessionLocal
    from database.models import MaintenanceWindow
    now = now or _now()
    if start_at < now - timedelta(minutes=1):
        raise PauseError("Waktu mulai sudah lewat.")
    if end_at is not None and end_at <= start_at:
        raise PauseError("Waktu selesai harus setelah waktu mulai.")
    db = SessionLocal()
    try:
        if _pending_row(db):
            raise PauseError("Sudah ada jadwal/pause yang belum selesai. Batalkan atau lanjutkan dulu.")
        row = MaintenanceWindow(
            start_at=start_at, end_at=end_at, note=(note or "").strip()[:300] or None,
            announce=announce, status="active" if start_at <= now else "scheduled", created_by=admin_id)
        if announce:
            # Pengingat yang jaraknya sudah lebih dekat dari waktu pengumuman tidak perlu dikirim lagi.
            left = start_at - now
            if left <= timedelta(minutes=60):
                row.reminded_60_at = now
            if left <= timedelta(minutes=10):
                row.reminded_10_at = now
        db.add(row)
        db.commit()
        db.refresh(row)
        return _snapshot(row)
    finally:
        db.close()


def pause_now(minutes: Optional[int], note: str, admin_id: Optional[int],
              now: Optional[datetime] = None) -> PauseInfo:
    """Pause mendadak tanpa broadcast; minutes=None berarti sampai admin menekan Lanjutkan."""
    now = now or _now()
    end_at = now + timedelta(minutes=minutes) if minutes else None
    return schedule(now, end_at, note, admin_id, announce=False, now=now)


def resume_now(admin_id: Optional[int], now: Optional[datetime] = None) -> Optional[PauseInfo]:
    """Akhiri pause yang sedang berlaku. None bila tidak ada pause yang berjalan."""
    from database.connection import SessionLocal
    now = now or _now()
    db = SessionLocal()
    try:
        row = _pending_row(db)
        if not row or not (row.status == "active" or row.start_at <= now):
            return None
        row.status, row.ended_at, row.resumed_notified_at = "done", now, now
        db.commit()
        logger.info("Pause #%s diakhiri admin %s", row.id, admin_id)
        return _snapshot(row)
    finally:
        db.close()


def cancel_scheduled(admin_id: Optional[int], now: Optional[datetime] = None) -> Optional[PauseInfo]:
    """Batalkan jadwal yang belum mulai. None bila tidak ada jadwal yang menunggu."""
    from database.connection import SessionLocal
    now = now or _now()
    db = SessionLocal()
    try:
        row = _pending_row(db)
        if not row or row.status != "scheduled" or row.start_at <= now:
            return None
        row.status, row.ended_at = "cancelled", now
        db.commit()
        logger.info("Jadwal maintenance #%s dibatalkan admin %s", row.id, admin_id)
        return _snapshot(row)
    finally:
        db.close()


# ---------------- Parsing input admin ----------------
_DATE = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$")
_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_DURATION = re.compile(r"^(\d+)(m|mnt|menit|j|jam)?$", re.IGNORECASE)


def parse_duration(token: str) -> Optional[int]:
    """'90' / '90m' / '2j' / '2jam' -> menit. None bila bukan durasi."""
    match = _DURATION.match(token or "")
    if not match:
        return None
    minutes = int(match.group(1)) * (60 if (match.group(2) or "").lower() in ("j", "jam") else 1)
    if not 1 <= minutes <= MAX_DURATION_MINUTES:
        raise PauseError("Durasi harus 1 menit sampai 3 hari.")
    return minutes


def parse_schedule(text: str, now: Optional[datetime] = None) -> tuple:
    """'[DD/MM[/YYYY]] HH:MM [durasi] [catatan]' (WIB) -> (start_utc, end_utc|None, catatan).

    Tanpa tanggal: hari ini, atau besok bila jamnya sudah lewat.
    """
    now = now or _now()
    tokens = (text or "").split()
    local_now = now + WIB
    day = month = year = None
    if tokens and _DATE.match(tokens[0]):
        d, m, y = _DATE.match(tokens.pop(0)).groups()
        day, month = int(d), int(m)
        year = (int(y) + 2000 if len(y) == 2 else int(y)) if y else local_now.year
    if not tokens or not _TIME.match(tokens[0]):
        raise PauseError("Format: <code>HH:MM [durasi] [catatan]</code>, mis. <code>22:00 60m Update sistem</code>.")
    hour, minute = (int(x) for x in _TIME.match(tokens.pop(0)).groups())
    if hour > 23 or minute > 59:
        raise PauseError("Jam tidak valid.")
    minutes = parse_duration(tokens[0]) if tokens else None
    if minutes is not None:
        tokens.pop(0)
    note = " ".join(tokens)
    try:
        if day is None:
            local_start = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if local_start <= local_now:
                local_start += timedelta(days=1)
        else:
            local_start = datetime(year, month, day, hour, minute)
    except ValueError:
        raise PauseError("Tanggal tidak valid.")
    start_at = local_start - WIB
    if start_at <= now:
        raise PauseError("Waktu mulai sudah lewat.")
    end_at = start_at + timedelta(minutes=minutes) if minutes else None
    return start_at, end_at, note


# ---------------- Teks untuk user ----------------
def block_text(info: PauseInfo) -> str:
    """Balasan saat user mencoba memulai transaksi baru selama pause."""
    until = f" sampai sekitar <b>{fmt_wib(info.end_at)}</b>" if info.end_at else ""
    return (
        "🛠 <b>Bot sedang maintenance / update sistem</b>\n\n"
        f"Transaksi baru (Beli, Jual, Convert, Topup, Withdraw) dijeda sementara{until}.\n"
        f"{_note_line(info)}\n"
        "Order yang sedang berjalan tetap diproses seperti biasa: deposit dan pengiriman koin tidak "
        "terganggu. Silakan coba lagi setelah maintenance selesai. 🙏"
    )


def announcement_text(info: PauseInfo) -> str:
    return (
        "📢 <b>Jadwal Maintenance / Update Sistem</b>\n\n"
        f"🗓 {window_text(info)}\n"
        f"{_note_line(info)}\n"
        "Selama maintenance, transaksi baru (Beli, Jual, Convert, Topup, Withdraw) dijeda sementara. "
        "Order yang sudah berjalan tetap diproses.\n\n"
        "Selesaikan transaksimu sebelum waktu mulai ya. Kami kabari lagi saat bot aktif kembali. 🙏"
    )


def reminder_text(info: PauseInfo, minutes_left: int) -> str:
    return (
        f"⏰ <b>Pengingat: maintenance mulai {minutes_left} menit lagi</b>\n\n"
        f"🗓 {window_text(info)}\n"
        f"{_note_line(info)}\n"
        "Transaksi baru akan dijeda saat maintenance dimulai. Segera selesaikan transaksi yang sedang "
        "kamu buat. Order yang sudah berjalan tetap diproses."
    )


def resumed_text(info: PauseInfo) -> str:
    return (
        "✅ <b>Maintenance selesai — bot aktif kembali</b>\n\n"
        "Semua transaksi (Beli, Jual, Convert, Topup, Withdraw) sudah bisa digunakan lagi. "
        "Terima kasih sudah menunggu! 🙏"
    )


def cancelled_text(info: PauseInfo) -> str:
    return (
        "ℹ️ <b>Jadwal maintenance dibatalkan</b>\n\n"
        f"Maintenance yang dijadwalkan {window_text(info)} <b>tidak jadi</b> dilaksanakan. "
        "Semua transaksi tetap berjalan normal."
    )


# ---------------- Broadcast & job berkala ----------------
async def broadcast(bot, text: str, label: str) -> tuple:
    """Kirim ke semua user non-banned (pelan, aman dari flood limit). Return (terkirim, total)."""
    from database.connection import SessionLocal
    from database.crud import get_users_by_segment
    from bot.handlers.admin import BROADCAST_PACE_EVERY, BROADCAST_PACE_SECONDS, _send_broadcast_to_user
    from bot.utils.telegram_utils import notify_admins, resolve_bot
    bot = resolve_bot(bot)   # job berkala mengirim Application, handler mengirim Bot
    db = SessionLocal()
    try:
        ids = [u.telegram_id for u in get_users_by_segment(db, "all")]
    finally:
        db.close()
    sent = 0
    for index, telegram_id in enumerate(ids, start=1):
        if await _send_broadcast_to_user(bot, telegram_id, text):
            sent += 1
        if index % BROADCAST_PACE_EVERY == 0:
            await asyncio.sleep(BROADCAST_PACE_SECONDS)
    logger.info("Broadcast %s: %d/%d user", label, sent, len(ids))
    try:
        await notify_admins(bot, f"📣 <b>{escape(label)}</b> terkirim ke {sent}/{len(ids)} user.", kind="ops")
    except Exception as exc:
        logger.warning("Gagal lapor hasil broadcast %s: %s", label, exc)
    return sent, len(ids)


def start_broadcast(bot, text: str, label: str) -> None:
    """Broadcast di latar belakang (job berkala tidak boleh tertahan menunggu ribuan pesan)."""
    task = asyncio.create_task(broadcast(bot, text, label))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _due_reminder(row, now: datetime) -> Optional[int]:
    """Pengingat yang jatuh tempo (menit), ditandai terkirim beserta pengingat yang lebih jauh.

    Dicek dari yang terkecil: bila bot mati lalu hidup 5 menit sebelum mulai, yang dikirim
    pengingat 10 menit, bukan "60 menit lagi".
    """
    left = row.start_at - now
    for minutes in REMINDER_MINUTES:
        attr = f"reminded_{minutes}_at"
        if getattr(row, attr) is None and timedelta(0) < left <= timedelta(minutes=minutes):
            for bigger in REMINDER_MINUTES:
                if bigger >= minutes and getattr(row, f"reminded_{bigger}_at") is None:
                    setattr(row, f"reminded_{bigger}_at", now)
            return max(1, math.ceil(left.total_seconds() / 60))
    return None


async def announce_resumed(bot, info: PauseInfo, by_admin: bool) -> None:
    from bot.utils.telegram_utils import notify_admins
    who = "oleh admin" if by_admin else "otomatis sesuai jadwal"
    await notify_admins(bot, f"▶️ <b>Transaksi dibuka kembali</b> ({who}).", kind="ops")
    if info.announce:
        start_broadcast(bot, resumed_text(info), "Kabar bot aktif kembali")


async def tick(bot, now: Optional[datetime] = None) -> None:
    """Dipanggil scheduler tiap 30 detik: pengumuman, pengingat, mulai & selesai otomatis."""
    from database.connection import SessionLocal
    from bot.utils.telegram_utils import notify_admins
    now = now or _now()
    db = SessionLocal()
    try:
        row = _pending_row(db)
        if not row:
            return
        if row.status == "scheduled" and row.announce:
            # Tandai dulu baru kirim: crash di tengah broadcast tidak membuat pesan terkirim dua kali.
            if row.announced_at is None:
                row.announced_at = now
                db.commit()
                start_broadcast(bot, announcement_text(_snapshot(row)), "Pengumuman jadwal maintenance")
            minutes_left = _due_reminder(row, now)
            if minutes_left:
                db.commit()
                start_broadcast(bot, reminder_text(_snapshot(row), minutes_left),
                                f"Pengingat maintenance {minutes_left} menit")
        if row.status == "scheduled" and row.start_at <= now:
            row.status = "active"
            db.commit()
            info = _snapshot(row)
            until = f" sampai {fmt_wib(info.end_at)}" if info.end_at else " sampai admin menekan ▶️ Lanjutkan"
            await notify_admins(bot, f"⏸ <b>Maintenance dimulai</b> — transaksi baru dijeda{until}.", kind="ops")
        if row.status == "active" and row.end_at is not None and now >= row.end_at:
            row.status, row.ended_at = "done", now
            row.resumed_notified_at = now
            db.commit()
            await announce_resumed(bot, _snapshot(row), by_admin=False)
    finally:
        db.close()
