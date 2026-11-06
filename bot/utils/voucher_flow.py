"""Alur voucher yang dipakai bersama oleh Beli, Jual, dan Convert.

Tombol "Punya Voucher?" muncul di layar ringkasan (sebelum konfirmasi / sebelum QRIS terbit). User mengetik
kode, fee berkurang sesuai potongan (tidak melebihi fee), ringkasan dihitung ulang, lalu user menekan konfirmasi.
Tiap alur memberi dua fungsi:
  reprice(ud, discount) -> pesan_error | None : hitung ulang harga dengan potongan (0 = lepas voucher)
  render(update, context) -> state             : tampilkan ulang ringkasan dan kembali ke state konfirmasi
"""
import logging
from datetime import datetime
from html import escape as _esc

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.utils.formatter import format_idr
from database.connection import SessionLocal
from services import voucher_service as VS

logger = logging.getLogger(__name__)

PROMPT_TEXT = (
    "🎟 <b>PUNYA VOUCHER?</b>\n\n"
    "Masukkan kode voucher untuk mendapat <b>potongan fee transaksi</b>.\n"
    "Ketik kodenya di bawah ini (tanpa spasi), contoh: <code>tokokoin1111</code>\n\n"
    "<i>Potongan langsung mengurangi fee dan terlihat di ringkasan sebelum kamu membayar.</i>"
)


def _keys(prefix: str):
    return f"{prefix}_voucher_code", f"{prefix}_voucher_discount", f"{prefix}_voucher_applied"


def clear_voucher(ud: dict, prefix: str) -> None:
    for key in _keys(prefix):
        ud.pop(key, None)


def voucher_line(ud: dict, prefix: str) -> str:
    """Baris ringkasan voucher (kosong bila tidak ada)."""
    code_key, _, applied_key = _keys(prefix)
    if not ud.get(code_key):
        return ""
    return f"\n🎟 <b>Voucher {_esc(str(ud[code_key]).upper())}:</b> fee -{format_idr(int(ud.get(applied_key) or 0))}"


def voucher_button_row(ud: dict, prefix: str) -> list:
    """Satu baris tombol untuk keyboard ringkasan: pakai atau lepas voucher."""
    if ud.get(_keys(prefix)[0]):
        return [InlineKeyboardButton("❌ Lepas Voucher", callback_data=f"{prefix}_voucher_off")]
    return [InlineKeyboardButton("🎟 Punya Voucher?", callback_data=f"{prefix}_voucher")]


def build_handlers(*, prefix: str, flow: str, state_voucher: int, reprice, render, cancel_callback: str):
    """Pembuat handler voucher untuk satu alur. Mengembalikan dict: prompt, text_input, back, remove."""
    code_key, discount_key, applied_key = _keys(prefix)

    async def prompt(update, context):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            PROMPT_TEXT, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Kembali ke Ringkasan", callback_data=f"{prefix}_voucher_back")],
                [InlineKeyboardButton("Batal", callback_data=cancel_callback)],
            ]))
        return state_voucher

    async def text_input(update, context):
        ud = context.user_data
        db = SessionLocal()
        try:
            voucher, error = VS.check_voucher(db, update.message.text, update.effective_user.id, flow)
        finally:
            db.close()
        if voucher is None:
            await update.message.reply_text(
                f"❌ {_esc(error)}\n\nKetik kode voucher lain, atau tekan <b>Kembali ke Ringkasan</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Kembali ke Ringkasan", callback_data=f"{prefix}_voucher_back")]]))
            return state_voucher
        ud[code_key], ud[discount_key] = voucher.code, int(voucher.discount_idr)
        problem = reprice(ud, int(voucher.discount_idr))
        if problem:
            clear_voucher(ud, prefix)
            reprice(ud, 0)
            await update.message.reply_text(f"❌ {problem}", parse_mode="HTML")
            return await render(update, context)
        applied = int(ud.get(applied_key) or 0)
        await update.message.reply_text(
            f"✅ <b>Voucher {_esc(voucher.code.upper())} dipakai!</b>\n"
            f"Fee transaksi dipotong <b>{format_idr(applied)}</b>.", parse_mode="HTML")
        return await render(update, context)

    async def back(update, context):
        await update.callback_query.answer()
        return await render(update, context)

    async def remove(update, context):
        await update.callback_query.answer("Voucher dilepas.")
        clear_voucher(context.user_data, prefix)
        reprice(context.user_data, 0)
        return await render(update, context)

    return {"prompt": prompt, "text_input": text_input, "back": back, "remove": remove}


async def guard_at_confirm(update, context, *, prefix: str, flow: str, reprice, render) -> bool:
    """Dipanggil tepat sebelum order dibuat. True bila aman lanjut; False bila voucher tak berlaku lagi
    (dilepas, harga dihitung ulang, ringkasan ditampilkan ulang; user menekan konfirmasi sekali lagi)."""
    ud = context.user_data
    code = ud.get(f"{prefix}_voucher_code")
    if not code:
        return True
    db = SessionLocal()
    try:
        voucher, error = VS.check_voucher(db, code, update.effective_user.id, flow)
    finally:
        db.close()
    if voucher is not None:
        return True
    clear_voucher(ud, prefix)
    reprice(ud, 0)
    message = update.callback_query.message
    await render(update, context)
    await message.reply_text(
        f"⚠️ Voucher dilepas: {_esc(error)}\nHarga sudah dihitung ulang tanpa voucher. Cek ringkasan lalu konfirmasi lagi.",
        parse_mode="HTML")
    return False


def redeem_for_order(user_id: int, ud: dict, prefix: str, order_id: str) -> None:
    """Catat pemakaian voucher untuk order yang baru dibuat (bila ada potongan yang berlaku)."""
    code_key, _, applied_key = _keys(prefix)
    applied = int(ud.get(applied_key) or 0)
    if not ud.get(code_key) or applied <= 0:
        return
    db = SessionLocal()
    try:
        VS.redeem(db, ud[code_key], user_id, order_id, applied)
    finally:
        db.close()


# ───────────── perintah admin ─────────────

VOUCHER_HELP = (
    "🎟 <b>KELOLA VOUCHER</b>\n\n"
    "<code>/voucher</code> daftar voucher & jumlah pemakaian\n"
    "<code>/voucher add KODE POTONGAN [maks_per_user] [maks_total] [berlaku]</code>\n"
    "  contoh: <code>/voucher add promo500 500 1 100 7d</code>\n"
    "  (maks 0 = tanpa batas; bawaan 1x per user, total tanpa batas)\n"
    "  berlaku: <code>12h</code> jam, <code>7d</code> hari, <code>2w</code> minggu sejak sekarang, atau tanggal "
    "<code>2026-12-31</code> (sampai akhir hari WIB); <code>0</code> = tanpa batas waktu\n"
    "  Untuk mencegah penyalahgunaan, sebaiknya selalu isi batas waktu dan batas total.\n"
    "<code>/voucher off KODE</code> nonaktifkan · <code>/voucher on KODE</code> aktifkan kembali\n\n"
    "<i>Potongan mengurangi fee transaksi Beli, Jual, dan Convert, dan tidak pernah melebihi fee itu.</i>"
)


def _voucher_list_text(db) -> str:
    rows = VS.list_vouchers(db)
    if not rows:
        return "Belum ada voucher.\n\n" + VOUCHER_HELP
    lines = ["🎟 <b>DAFTAR VOUCHER</b>\n"]
    for v, used in rows:
        per_user = "tanpa batas" if not v.max_per_user else f"{v.max_per_user}x/user"
        total = "tanpa batas" if not v.max_total else f"maks {v.max_total}"
        expired = bool(v.valid_until and v.valid_until < datetime.utcnow())
        status = "⌛" if expired else ("🟢" if v.active else "⚪")
        lines.append(
            f"{status} <code>{_esc(v.code)}</code> · fee -{format_idr(v.discount_idr)} · "
            f"{per_user} · total {total} · terpakai <b>{used}</b> · {VS.format_validity(v.valid_until)}"
            f"{' (KEDALUWARSA)' if expired else ''}")
    return "\n".join(lines) + "\n\n<i>Ketik /voucher untuk bantuan perintah.</i>"


async def voucher_command(update, context) -> None:
    """/voucher (admin): daftar, tambah/ubah, aktif/nonaktif."""
    from bot.handlers.admin import is_admin

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Anda tidak memiliki akses.")
        return
    args = list(context.args or [])
    db = SessionLocal()
    try:
        if not args:
            await update.message.reply_text(_voucher_list_text(db), parse_mode="HTML")
            return
        action = args[0].lower()
        if action in ("on", "off") and len(args) == 2:
            ok = VS.set_active(db, args[1], action == "on")
            await update.message.reply_text(
                f"✅ Voucher <code>{_esc(VS.normalize_code(args[1]))}</code> {'diaktifkan' if action == 'on' else 'dinonaktifkan'}."
                if ok else "❌ Kode voucher tidak ditemukan.", parse_mode="HTML")
            return
        if action == "add" and 3 <= len(args) <= 6:
            code = VS.normalize_code(args[1])
            try:
                discount = int(args[2].replace(".", ""))
                per_user = int(args[3]) if len(args) > 3 else 1
                total = int(args[4]) if len(args) > 4 else 0
            except ValueError:
                await update.message.reply_text("❌ Potongan dan batas harus berupa angka.\n\n" + VOUCHER_HELP, parse_mode="HTML")
                return
            try:
                valid_until = VS.parse_validity(args[5]) if len(args) > 5 else None
            except ValueError as exc:
                await update.message.reply_text(f"❌ Masa berlaku tidak valid ({exc}).\n\n" + VOUCHER_HELP, parse_mode="HTML")
                return
            if not code.isalnum() or not 3 <= len(code) <= 40 or discount <= 0 or discount > 100_000 or per_user < 0 or total < 0:
                await update.message.reply_text(
                    "❌ Kode 3-40 huruf/angka tanpa spasi, potongan Rp 1 - 100.000, batas tidak negatif.\n\n" + VOUCHER_HELP,
                    parse_mode="HTML")
                return
            v = VS.upsert_voucher(db, code, discount, per_user, total, valid_until=valid_until)
            await update.message.reply_text(
                f"✅ Voucher <code>{_esc(v.code)}</code> tersimpan: fee -{format_idr(v.discount_idr)}, "
                f"{'tanpa batas' if not v.max_per_user else str(v.max_per_user) + 'x/user'}, "
                f"total {'tanpa batas' if not v.max_total else 'maks ' + str(v.max_total)}, "
                f"{VS.format_validity(v.valid_until)}."
                + ("" if v.valid_until and v.max_total else
                   "\n⚠️ <i>Tanpa batas waktu/total voucher ini rawan disalahgunakan; sebaiknya isi keduanya.</i>"),
                parse_mode="HTML")
            return
        await update.message.reply_text(VOUCHER_HELP, parse_mode="HTML")
    finally:
        db.close()
