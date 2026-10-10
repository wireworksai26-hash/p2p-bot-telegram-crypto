"""bot/utils/manual_payout.py — Tombol "Selesaikan Pengiriman Manual" untuk payout crypto yang gagal (SS, TX hash, atau tanpa bukti).

Dipakai saat auto-payout gagal (mis. RPC error): admin mengirim crypto sendiri dari wallet,
lalu mengirim screenshot transfer ke bot. Bot meneruskannya ke user dan menyelesaikan order.
"""
from telegram import InlineKeyboardButton

CALLBACK_PREFIX = "admin_manual_sent_"

# Order jual tidak termasuk: payout-nya Rupiah dan sudah punya alur bukti sendiri.
ALLOWED_ORDER_TYPES = ("buy", "swap")
# manual_review: payout gagal/terputus. payout_broadcasted: hash ada tapi receipt belum/ gagal terkonfirmasi.
ALLOWED_STATUSES = ("manual_review", "payout_broadcasted")


def manual_payout_button(order_id: str) -> InlineKeyboardButton:
    return InlineKeyboardButton("✅ Selesaikan Pengiriman Manual", callback_data=f"{CALLBACK_PREFIX}{order_id}")


def manual_payout_block_reason(order) -> str:
    """Alasan order TIDAK boleh diselesaikan lewat bukti transfer manual ('' = boleh)."""
    if not order:
        return "❌ Order tidak ditemukan."
    if order.order_type not in ALLOWED_ORDER_TYPES:
        return "❌ Fitur ini hanya untuk order beli/convert."
    status = str(order.status or "").lower()
    if status == "completed":
        return "ℹ️ Order ini sudah COMPLETED."
    if status not in ALLOWED_STATUSES:
        return f"ℹ️ Order berstatus {str(order.status).upper()} — hanya order MANUAL_REVIEW yang bisa diselesaikan manual."
    return ""
