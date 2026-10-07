"""
bot/handlers/withdraw.py — Penarikan Saldo IDR ke Rekening / E-Wallet
======================================================================
Tombol Withdraw di menu Saldo & Profil terbuka saat saldo >= WITHDRAW_MIN_IDR
(reward referral, topup, atau pemasukan saldo lain). Alur:
  wd_start -> pilih rekening -> pilih nominal -> konfirmasi -> saldo dipotong +
  request dibuat -> admin transfer manual lalu Approve (PAID) / Tolak (refund).
"""

import logging
from html import escape as _esc

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import settings
from database.connection import SessionLocal
from database.crud import (
    WITHDRAW_MIN_IDR,
    get_user_balance,
    get_user_saved_banks,
    get_saved_bank_by_id,
    create_withdraw_request,
    get_withdraw_request,
    settle_withdraw_request,
)
from bot.utils.formatter import format_idr
from bot.utils.telegram_utils import safe_send_message
from bot.utils.validator import validate_amount_idr

logger = logging.getLogger(__name__)

_FLAGS = ("awaiting_withdraw_amount", "wd_bank_id", "wd_amount")


def _clear_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in _FLAGS:
        context.user_data.pop(key, None)


def withdraw_button_for(balance: float) -> InlineKeyboardButton:
    """Tombol Withdraw untuk menu saldo: aktif bila saldo cukup, terkunci bila belum."""
    if int(balance) >= WITHDRAW_MIN_IDR:
        return InlineKeyboardButton("💸 Withdraw Saldo", callback_data="wd_start")
    return InlineKeyboardButton("🔒 Withdraw Saldo", callback_data="wd_locked")


async def withdraw_locked_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer(
        f"🔒 Withdraw terbuka saat saldo minimal {format_idr(WITHDRAW_MIN_IDR)} "
        "(dari reward referral, topup, atau bonus lain).",
        show_alert=True,
    )


async def withdraw_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Langkah 1: pilih rekening tujuan."""
    query = update.callback_query
    user = update.effective_user
    _clear_flow(context)

    db = SessionLocal()
    try:
        balance = int(get_user_balance(db, user.id))
        banks = get_user_saved_banks(db, user.id)
    finally:
        db.close()

    if balance < WITHDRAW_MIN_IDR:
        await withdraw_locked_handler(update, context)
        return

    if not banks:
        await query.answer()
        await query.edit_message_text(
            "🏦 <b>Belum ada rekening pencairan.</b>\n\n"
            "Tambahkan rekening bank / e-wallet dulu agar saldo bisa ditarik.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✍️ Tambah Rekening", callback_data="act_add_saved_bank")],
                [InlineKeyboardButton("🔙 Kembali", callback_data="menu_balance")],
            ]),
            parse_mode="HTML",
        )
        return

    keyboard = []
    for b in banks:
        icon = "📱" if b.account_type == "EWALLET" else "🏦"
        label = f"{icon} {b.bank_name} - {b.account_number}"
        keyboard.append([InlineKeyboardButton(label[:60], callback_data=f"wd_bank_{b.id}")])
    keyboard.append([InlineKeyboardButton("🔙 Batal", callback_data="menu_balance")])

    await query.answer()
    await query.edit_message_text(
        f"💸 <b>WITHDRAW SALDO</b>\n\n"
        f"💳 Saldo tersedia: <b>{format_idr(balance)}</b>\n"
        f"📉 Minimal penarikan: <b>{format_idr(WITHDRAW_MIN_IDR)}</b>\n\n"
        "Pilih rekening / e-wallet tujuan:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def withdraw_bank_selected_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Langkah 2: pilih nominal."""
    query = update.callback_query
    user = update.effective_user
    bank_id = int(query.data.replace("wd_bank_", ""))

    db = SessionLocal()
    try:
        bank = get_saved_bank_by_id(db, bank_id, user.id)
        balance = int(get_user_balance(db, user.id))
    finally:
        db.close()

    if not bank:
        await query.answer("⚠️ Rekening tidak ditemukan.", show_alert=True)
        return
    if balance < WITHDRAW_MIN_IDR:
        await withdraw_locked_handler(update, context)
        return

    _clear_flow(context)
    context.user_data["wd_bank_id"] = bank_id
    await query.answer()
    await query.edit_message_text(
        f"💸 <b>Nominal Withdraw</b>\n\n"
        f"Tujuan: <b>{_esc(bank.bank_name)}</b> <code>{_esc(bank.account_number)}</code>\n"
        f"a.n <i>{_esc(bank.account_name)}</i>\n"
        f"💳 Saldo tersedia: <b>{format_idr(balance)}</b>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Tarik Semua ({format_idr(balance)})", callback_data="wd_all")],
            [InlineKeyboardButton("✏️ Nominal Lain", callback_data="wd_custom")],
            [InlineKeyboardButton("🔙 Ganti Rekening", callback_data="wd_start")],
        ]),
        parse_mode="HTML",
    )


async def withdraw_all_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = SessionLocal()
    try:
        balance = int(get_user_balance(db, update.effective_user.id))
    finally:
        db.close()
    await _show_confirm(update, context, balance)


async def withdraw_custom_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not context.user_data.get("wd_bank_id"):
        await query.answer("⚠️ Sesi habis, mulai ulang dari menu saldo.", show_alert=True)
        return
    context.user_data["awaiting_withdraw_amount"] = True
    await query.answer()
    await query.edit_message_text(
        f"✏️ <b>Ketik nominal withdraw</b> (minimal {format_idr(WITHDRAW_MIN_IDR)}).\n"
        "Contoh: <code>25000</code> atau <code>25k</code>\n\n"
        "<i>Ketik /cancel untuk membatalkan.</i>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Batal", callback_data="menu_balance")]]),
        parse_mode="HTML",
    )


async def handle_withdraw_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Input teks nominal. True bila pesan ditangani."""
    if not context.user_data.get("awaiting_withdraw_amount"):
        return False
    raw = (update.message.text or "").strip()
    if raw.lower() in ("/cancel", "batal"):
        _clear_flow(context)
        await update.message.reply_text("❌ Withdraw dibatalkan.")
        return True

    ok, amount = validate_amount_idr(raw, min_amount=WITHDRAW_MIN_IDR, max_amount=100_000_000)
    if not ok:
        await update.message.reply_text(
            f"❌ Nominal tidak valid. Minimal {format_idr(WITHDRAW_MIN_IDR)}, contoh: <code>25000</code> atau <code>25k</code>.",
            parse_mode="HTML",
        )
        return True

    db = SessionLocal()
    try:
        balance = int(get_user_balance(db, update.effective_user.id))
    finally:
        db.close()
    if amount > balance:
        await update.message.reply_text(
            f"❌ Saldo tidak cukup. Saldo Anda <b>{format_idr(balance)}</b>. Ketik nominal lain:",
            parse_mode="HTML",
        )
        return True

    context.user_data.pop("awaiting_withdraw_amount", None)
    await _show_confirm(update, context, amount)
    return True


async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int) -> None:
    """Langkah 3: konfirmasi."""
    user = update.effective_user
    bank_id = context.user_data.get("wd_bank_id")
    db = SessionLocal()
    try:
        bank = get_saved_bank_by_id(db, bank_id, user.id) if bank_id else None
    finally:
        db.close()

    query = update.callback_query
    if not bank or amount < WITHDRAW_MIN_IDR:
        msg = "⚠️ Sesi habis atau nominal tidak valid. Mulai ulang dari menu saldo."
        if query:
            await query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return

    context.user_data["wd_amount"] = amount
    text = (
        "💸 <b>KONFIRMASI WITHDRAW</b>\n\n"
        f"Nominal: <b>{format_idr(amount)}</b>\n"
        f"Tujuan: <b>{_esc(bank.bank_name)}</b> <code>{_esc(bank.account_number)}</code>\n"
        f"a.n <i>{_esc(bank.account_name)}</i>\n\n"
        "Saldo langsung dipotong dan dikembalikan bila admin menolak. Pastikan data rekening benar."
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya, Ajukan Withdraw", callback_data="wd_confirm")],
        [InlineKeyboardButton("🔙 Batal", callback_data="menu_balance")],
    ])
    if query:
        await query.answer()
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def withdraw_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Langkah 4: potong saldo, buat request, kabari admin."""
    query = update.callback_query
    user = update.effective_user
    bank_id = context.user_data.get("wd_bank_id")
    amount = context.user_data.get("wd_amount")
    if not bank_id or not amount:
        await query.answer("⚠️ Sesi habis, mulai ulang dari menu saldo.", show_alert=True)
        return
    _clear_flow(context)

    db = SessionLocal()
    try:
        bank = get_saved_bank_by_id(db, bank_id, user.id)
        req = create_withdraw_request(db, user.id, bank, amount) if bank else None
        if req is None:
            await query.answer("❌ Saldo tidak cukup atau rekening tidak ditemukan.", show_alert=True)
            return
        req_id, req_amount = req.id, int(req.amount_idr)
        bank_name, acc_no, acc_name = req.bank_name, req.account_number, req.account_name
        new_balance = int(get_user_balance(db, user.id))
    finally:
        db.close()

    await query.answer("✅ Permintaan withdraw dikirim.")
    await query.edit_message_text(
        f"✅ <b>WITHDRAW DIAJUKAN</b>\n\n"
        f"🎫 ID: <code>WD-{req_id}</code>\n"
        f"Nominal: <b>{format_idr(req_amount)}</b>\n"
        f"Tujuan: <b>{_esc(bank_name)}</b> <code>{_esc(acc_no)}</code> a.n <i>{_esc(acc_name)}</i>\n"
        f"💳 Sisa saldo: <b>{format_idr(new_balance)}</b>\n\n"
        "Admin akan memproses transfer dan memberi kabar di sini.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back")]]),
        parse_mode="HTML",
    )

    admin_text = (
        "💸 <b>PERMINTAAN WITHDRAW BARU</b>\n\n"
        f"🎫 ID: <code>WD-{req_id}</code>\n"
        f"User: {_esc(user.full_name or '-')} (<code>{user.id}</code>)\n"
        f"Nominal: <b>{format_idr(req_amount)}</b>\n"
        f"Tujuan: <b>{_esc(bank_name)}</b>\n"
        f"No: <code>{_esc(acc_no)}</code>\n"
        f"a.n: <b>{_esc(acc_name)}</b>\n\n"
        "Transfer manual dulu, lalu tekan <b>Sudah Ditransfer</b>. Tolak akan mengembalikan saldo user."
    )
    admin_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sudah Ditransfer", callback_data=f"admin_wd_ok_{req_id}"),
        InlineKeyboardButton("❌ Tolak & Refund", callback_data=f"admin_wd_no_{req_id}"),
    ]])
    for admin_id in settings.ADMIN_CHAT_IDS:
        await safe_send_message(context.bot, admin_id, admin_text, reply_markup=admin_markup)


async def admin_withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: admin_wd_ok_{id} (sudah transfer) / admin_wd_no_{id} (tolak + refund)."""
    from bot.handlers.admin import is_admin

    query = update.callback_query
    admin_id = query.from_user.id
    if not is_admin(admin_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    approve = query.data.startswith("admin_wd_ok_")
    req_id = int(query.data.rsplit("_", 1)[1])

    db = SessionLocal()
    try:
        if not settle_withdraw_request(db, req_id, admin_id, approve):
            await query.answer("ℹ️ Withdraw ini sudah diproses.", show_alert=True)
            return
        req = get_withdraw_request(db, req_id)
        user_id, amount = req.telegram_id, int(req.amount_idr)
        bank_name, acc_no = req.bank_name, req.account_number
        balance = int(get_user_balance(db, user_id))
    except Exception:
        logger.error("Error admin_withdraw_callback #%s", req_id, exc_info=True)
        await query.answer("❌ Gagal memproses withdraw.", show_alert=True)
        return
    finally:
        db.close()

    if approve:
        user_msg = (
            f"✅ <b>WITHDRAW BERHASIL</b>\n\n"
            f"🎫 <code>WD-{req_id}</code>\n"
            f"<b>{format_idr(amount)}</b> sudah ditransfer ke {_esc(bank_name)} <code>{_esc(acc_no)}</code>."
        )
        suffix = "✅ <b>SUDAH DITRANSFER</b>"
    else:
        user_msg = (
            f"❌ <b>WITHDRAW DITOLAK</b>\n\n"
            f"🎫 <code>WD-{req_id}</code>\n"
            f"Saldo <b>{format_idr(amount)}</b> dikembalikan. Saldo Anda sekarang <b>{format_idr(balance)}</b>."
        )
        suffix = "❌ <b>DITOLAK & SALDO DI-REFUND</b>"

    await safe_send_message(context.bot, user_id, user_msg)
    await query.answer("Selesai.")
    try:
        await query.edit_message_text(
            f"{query.message.text_html}\n\n{suffix}", parse_mode="HTML"
        )
    except Exception:
        pass
