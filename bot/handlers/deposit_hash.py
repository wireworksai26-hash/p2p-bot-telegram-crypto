"""
bot/handlers/deposit_hash.py — Penerimaan TX hash deposit Jual/Convert.
=======================================================================
Nominal koin tidak lagi berkode unik, jadi satu-satunya bukti kepemilikan deposit adalah
TX hash yang dikirim user. `submit_deposit_hash` memverifikasi hash itu on-chain
(wallet tujuan, nominal, jendela waktu, belum dipakai order lain) lalu menyerahkannya ke
DepositDetector. Terverifikasi -> admin dikabari untuk transfer Rupiah (Jual) atau koin
tujuan langsung dikirim (Convert).

Juga menyediakan /txhash: user yang keluar dari percakapan jual/convert tetap bisa
mengirim hash untuk order yang masih menunggu deposit.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape as _esc

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

from config.settings import settings
from database.connection import SessionLocal
from database.crud import get_order_by_id
from database.models import AuditLog, Order
from bot.keyboards.main_menu import get_owner_button
from bot.utils.emojis import CUSTOM_EMOJI_IDS
from bot.utils.formatter import format_crypto
from services import tx_verifier
from services.deposit_amount import format_deposit_amount

logger = logging.getLogger(__name__)

WAIT_HASH = 1
_BACK_ICON = CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850")


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=_BACK_ICON)],
        [get_owner_button()],
    ])


def _is_pending_reason(reason: str) -> bool:
    """Hash belum bisa dinilai (tx belum confirmed / RPC bermasalah) -> coba lagi nanti, bukan ditolak."""
    return (not reason or reason.startswith("Menunggu konfirmasi")
            or "belum dapat diverifikasi" in reason)


async def submit_deposit_hash(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              order_id: str, raw_hash: str) -> str:
    """Terima TX hash user untuk order sell/swap. Return 'retry' (minta hash lagi) atau 'done'."""
    from services.detector import deposit_detector
    message = getattr(update, "message", None) or update.effective_message
    db = SessionLocal()
    try:
        order = get_order_by_id(db, order_id) if order_id else None
        if (not order or order.telegram_id != update.effective_user.id
                or order.order_type not in ("sell", "swap")):
            await message.reply_text("❌ Order tidak ditemukan.", reply_markup=_menu_keyboard())
            return "done"
        if order.status != "WAITING_CRYPTO_DEPOSIT" and not deposit_detector.is_recoverable_expired(order):
            await message.reply_text(
                f"ℹ️ Order <code>{_esc(order.order_id)}</code> sudah tidak menunggu deposit "
                f"(status: <b>{_esc(str(order.status))}</b>).",
                parse_mode="HTML", reply_markup=_menu_keyboard())
            return "done"

        try:
            tx_hash = tx_verifier.normalize_tx_hash(order.network, raw_hash)
        except (ValueError, TypeError):
            await message.reply_text(
                "❌ <b>Format TX Hash Salah!</b>\n\n"
                f"Hash tidak valid untuk jaringan <b>{_esc(order.network)}</b>. "
                "Salin ulang TX Hash (atau link explorer) lalu kirim lagi:",
                parse_mode="HTML")
            return "retry"

        if deposit_detector._is_hash_used(db, tx_hash, exclude_order=order.order_id):
            await message.reply_text(
                "❌ <b>TX Hash ini sudah dipakai order lain.</b>\n\n"
                "Satu transaksi hanya bisa dipakai untuk satu order. "
                "Kirim hash transaksi yang benar, atau hubungi admin.",
                parse_mode="HTML")
            return "retry"

        hasil = await tx_verifier.verify_deposit(
            network=order.network,
            symbol=order.crypto_symbol,
            tx_hash=tx_hash,
            expected_wallet=order.deposit_wallet,
            expected_amount=float(order.crypto_amount),
            not_before=order.created_at,
            not_after=deposit_detector.deposit_deadline(order),
        )
        lolos = bool((hasil or {}).get("verified"))
        alasan = (hasil or {}).get("reason") or ""

        if not lolos and not _is_pending_reason(alasan):
            await message.reply_text(
                f"❌ <b>Deposit Belum Bisa Diverifikasi</b>\n\n"
                f"Order ID: <code>{_esc(order.order_id)}</code>\n"
                f"TX Hash: <code>{_esc(tx_hash)}</code>\n\n"
                f"Alasan: <b>{_esc(alasan)}</b>\n\n"
                f"Pastikan transaksi mengirim <b>tepat "
                f"{format_deposit_amount(order.crypto_amount, order.crypto_symbol)} {_esc(order.crypto_symbol)}</b> "
                f"({_esc(order.network)}) ke <code>{_esc(order.deposit_wallet or '-')}</code>, "
                f"lalu kirim TX Hash yang benar.",
                parse_mode="HTML")
            return "retry"

        order.deposit_tx_hash = tx_hash
        db.commit()
        await message.reply_text(
            f"✅ <b>TX Hash Diterima!</b>\n\n"
            f"Order ID: <code>{_esc(order.order_id)}</code>\n"
            f"TX Hash: <code>{_esc(tx_hash)}</code>\n\n"
            + ("🔍 Memeriksa transaksi di blockchain..." if lolos else
               "⏳ Transaksi belum terkonfirmasi penuh. Bot terus memeriksa otomatis, "
               "kamu akan dapat kabar begitu terverifikasi."),
            parse_mode="HTML", reply_markup=_menu_keyboard())

        if lolos:
            await deposit_detector._process_order(db, order, context.application)
            db.refresh(order)
            if order.status in ("WAITING_CRYPTO_DEPOSIT", "expired"):
                escalated = db.query(AuditLog.id).filter(
                    AuditLog.order_id == order.order_id,
                    AuditLog.action == "DEPOSIT_HASH_NEEDS_REVIEW",
                    AuditLog.details.like(f"Hash {tx_hash}:%"),
                ).first()
                if escalated:
                    await message.reply_text(
                        "🕵️ <b>Deposit sedang dicek admin</b>\n\n"
                        "Transaksimu terdeteksi di blockchain, tetapi perlu dicocokkan manual oleh admin "
                        "sebelum diproses. Kamu akan menerima notifikasi setelah selesai. 🙏",
                        parse_mode="HTML")
                else:
                    await message.reply_text(
                        "⏳ TX Hash tersimpan. Bot masih memeriksa transaksimu; "
                        "kamu akan menerima notifikasi begitu terverifikasi.")
                    asyncio.create_task(deposit_detector.verifikasi_cepat(order.order_id, context.application))
        else:
            asyncio.create_task(deposit_detector.verifikasi_cepat(order.order_id, context.application))
        return "done"
    except Exception as exc:
        db.rollback()
        logger.error("Gagal memproses TX hash order %s: %s", order_id, exc, exc_info=True)
        await message.reply_text("⚠️ Terjadi kesalahan internal saat memproses TX Hash. Coba lagi sesaat lagi.")
        return "done"
    finally:
        db.close()


# ---------------- /txhash: kirim hash untuk order yang masih menunggu deposit ----------------
def _open_orders(db, telegram_id: int):
    window_start = datetime.utcnow() - timedelta(minutes=settings.SELL_DEPOSIT_WINDOW_MINUTES)
    return (
        db.query(Order)
        .filter(
            Order.telegram_id == telegram_id,
            Order.order_type.in_(["sell", "swap"]),
            (Order.status == "WAITING_CRYPTO_DEPOSIT")
            | ((Order.status == "expired") & (Order.created_at >= window_start)),
        )
        .order_by(Order.id.desc())
        .limit(5)
        .all()
    )


def _describe(order) -> str:
    kind = "Jual" if order.order_type == "sell" else "Convert"
    return (f"{kind} {format_deposit_amount(order.crypto_amount, order.crypto_symbol)} "
            f"{order.crypto_symbol} ({order.network})")


async def _prompt_for(update: Update, context: ContextTypes.DEFAULT_TYPE, order) -> int:
    context.user_data["txhash_order_id"] = order.order_id
    text = (
        "✍️ <b>KIRIM TX HASH</b>\n\n"
        f"Order: <code>{_esc(order.order_id)}</code>\n"
        f"{_esc(_describe(order))}\n\n"
        "Kirim <b>TX Hash / link explorer</b> dari transfer koinmu ke chat ini. "
        "Bot akan memeriksanya di blockchain."
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="txhash_cancel", icon_custom_emoji_id=_BACK_ICON)]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    return WAIT_HASH


async def txhash_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db = SessionLocal()
    try:
        orders = _open_orders(db, update.effective_user.id)
        if not orders:
            await update.effective_message.reply_text(
                "ℹ️ Tidak ada order Jual/Convert yang sedang menunggu deposit.",
                reply_markup=_menu_keyboard())
            return ConversationHandler.END
        if len(orders) == 1:
            return await _prompt_for(update, context, orders[0])
        rows = [[InlineKeyboardButton(f"{o.order_id} — {_describe(o)}"[:60], callback_data=f"txhash_pick_{o.order_id}")]
                for o in orders]
        await update.effective_message.reply_text(
            "Pilih order yang hash-nya ingin kamu kirim:", reply_markup=InlineKeyboardMarkup(rows))
        return WAIT_HASH
    finally:
        db.close()


async def txhash_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    order_id = query.data.replace("txhash_pick_", "")
    db = SessionLocal()
    try:
        order = get_order_by_id(db, order_id)
        if not order or order.telegram_id != update.effective_user.id:
            await query.edit_message_text("❌ Order tidak ditemukan.")
            return ConversationHandler.END
        return await _prompt_for(update, context, order)
    finally:
        db.close()


async def txhash_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order_id = context.user_data.get("txhash_order_id")
    if not order_id:
        await update.message.reply_text("Pilih order dulu dengan perintah /txhash.")
        return ConversationHandler.END
    result = await submit_deposit_hash(update, context, order_id, update.message.text)
    if result == "retry":
        return WAIT_HASH
    context.user_data.pop("txhash_order_id", None)
    return ConversationHandler.END


async def txhash_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("txhash_order_id", None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Dibatalkan. Kirim /txhash kapan saja untuk mengirim hash.")
    else:
        await update.message.reply_text("Dibatalkan. Kirim /txhash kapan saja untuk mengirim hash.")
    return ConversationHandler.END


txhash_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("txhash", txhash_command)],
    states={
        WAIT_HASH: [
            CallbackQueryHandler(txhash_pick, pattern="^txhash_pick_"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, txhash_receive),
            CallbackQueryHandler(txhash_cancel, pattern="^txhash_cancel$"),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", txhash_cancel),
        CallbackQueryHandler(txhash_cancel, pattern="^(txhash_cancel|menu_back)$"),
    ],
    allow_reentry=True,
)
