"""
bot/handlers/order_report.py — 🆘 Laporkan Kendala Order
=========================================================
User memilih salah satu order terakhirnya; bot membuat template pesan (Order ID, jenis transaksi,
pembayaran, waktu, TX hash) yang bisa disalin atau langsung dikirim ke owner/admin dengan satu
ketukan, supaya user tidak perlu menjelaskan dari nol.

Callback: report_issue            -> pilih order
          report_order_<ORDER_ID> -> template pesan untuk order itu (hanya milik user sendiri)
"""

import html
import logging
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config.settings import settings
from database.connection import SessionLocal
from database.crud import deposit_review_order_ids, get_orders_by_user
from database.models import Order
from bot.utils.formatter import format_crypto, format_datetime, format_idr

logger = logging.getLogger(__name__)

_TYPE_LABEL = {"buy": "Beli", "sell": "Jual", "swap": "Convert", "convert": "Convert"}
_PAYMENT_LABEL = {"GOPAY_QRIS": "QRIS (GoPay)", "BOT_BALANCE": "Saldo Bot", "balance": "Saldo Bot",
                  "saldo": "Saldo Bot", "qris": "QRIS (GoPay)", "gopay": "QRIS (GoPay)"}
_STATUS_LABEL = {"pending": "⏳ Pending", "paid": "💳 Paid", "completed": "✅ Selesai", "expired": "❌ Expired",
                 "failed": "🚨 Gagal", "manual_review": "⚙️ Dicek Admin", "cancelled": "🚫 Dibatalkan"}
_PICK_LIMIT = 8


def _type_label(order) -> str:
    return _TYPE_LABEL.get((order.order_type or "buy").lower(), (order.order_type or "-").capitalize())


def _status_label(order, in_review: bool = False) -> str:
    if in_review:
        # Koin user sudah masuk dan menunggu keputusan admin: jangan tampil "Expired".
        return "🕵️ Deposit dicek admin"
    return _STATUS_LABEL.get((order.status or "").lower(), (order.status or "-").upper())


def _payment_label(order) -> str:
    raw = order.payment_method or ""
    if raw:
        return _PAYMENT_LABEL.get(raw, _PAYMENT_LABEL.get(raw.lower(), raw))
    return "Kirim koin ke wallet bot" if (order.order_type or "").lower() == "sell" else "-"


def _asset_label(order) -> str:
    amount = format_crypto(float(order.crypto_amount or 0), order.crypto_symbol)
    if (order.order_type or "").lower() in ("swap", "convert") and order.target_crypto_symbol:
        target = format_crypto(float(order.target_crypto_amount or 0), order.target_crypto_symbol)
        return f"{amount} ({order.network}) -> {target} ({order.target_network})"
    return f"{amount} ({order.network})"


def build_report_template(order, in_review: bool = False) -> str:
    """Template pesan polos (tanpa HTML) untuk diteruskan user ke admin."""
    tx_hash = (order.payout_tx_hash or order.tx_hash or order.deposit_tx_hash or "").strip() or "-"
    lines = [
        "Halo Admin, saya ingin melaporkan kendala pada order saya:",
        "",
        f"Order ID: {order.order_id}",
        f"Jenis Transaksi: {_type_label(order)}",
        f"Aset: {_asset_label(order)}",
        f"Nominal: {format_idr(order.total_idr)}",
        f"Pembayaran: {_payment_label(order)}",
        f"Status: {_status_label(order, in_review)}",
        f"Waktu Order: {format_datetime(order.created_at)}",
    ]
    if order.completed_at:
        lines.append(f"Waktu Selesai: {format_datetime(order.completed_at)}")
    lines += [f"TX Hash: {tx_hash}", "", "Kendala: (tulis kendala Anda di sini)"]
    return "\n".join(lines)


def _owner_url(template: str) -> str:
    owner = (settings.OWNER_USERNAME or "").lstrip("@")
    return f"https://t.me/{owner}?text={quote(template)}"


def _back_row():
    return [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")]


async def report_issue_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Langkah 1: pilih order yang bermasalah (order terakhir milik user)."""
    query = update.callback_query
    await query.answer()
    db = SessionLocal()
    try:
        orders = get_orders_by_user(db, telegram_id=update.effective_user.id, limit=_PICK_LIMIT)
        review_ids = deposit_review_order_ids(db, [o.order_id for o in orders])
        rows = [
            [InlineKeyboardButton(
                f"{_type_label(o)} · {o.order_id} · {_status_label(o, o.order_id in review_ids)}"[:60],
                callback_data=f"report_order_{o.order_id}")]
            for o in orders
        ]
    finally:
        db.close()

    if not rows:
        text = "🆘 <b>LAPORKAN KENDALA ORDER</b>\n\nAnda belum memiliki order. Jika butuh bantuan, hubungi owner."
    else:
        text = ("🆘 <b>LAPORKAN KENDALA ORDER</b>\n\n"
                "Pilih order yang bermasalah. Bot akan menyiapkan pesan lengkap (Order ID, jenis transaksi, "
                "pembayaran, waktu, TX hash) yang bisa langsung Anda kirim ke admin.")
    rows.append([InlineKeyboardButton("🔙 Kembali", callback_data="menu_history")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")


async def report_order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Langkah 2: template pesan untuk order terpilih. Hanya order milik user yang meminta."""
    query = update.callback_query
    order_id = query.data[len("report_order_"):]
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.order_id == order_id).first()
        if not order or order.telegram_id != update.effective_user.id:
            await query.answer("Order tidak ditemukan.", show_alert=True)
            return
        template = build_report_template(order, bool(deposit_review_order_ids(db, [order.order_id])))
    finally:
        db.close()

    await query.answer()
    text = (
        "🆘 <b>LAPORAN KENDALA ORDER</b>\n\n"
        "Salin pesan di bawah (ketuk untuk menyalin) atau tekan <b>Kirim ke Admin</b>, lalu tambahkan "
        "kendala Anda di baris terakhir.\n\n"
        f"<pre>{html.escape(template)}</pre>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Kirim ke Admin", url=_owner_url(template))],
        [InlineKeyboardButton("🔙 Pilih Order Lain", callback_data="report_issue")],
        _back_row(),
    ])
    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="HTML")
