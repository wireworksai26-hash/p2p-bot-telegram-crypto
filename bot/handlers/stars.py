"""
bot/handlers/stars.py — Beli Telegram Stars & Telegram Premium.

Alur user: menu -> pilih produk -> pilih paket -> isi username tujuan (akun sendiri atau teman, tanpa
anti-fraud sesuai permintaan klien) -> pilih pembayaran (Saldo Bot / QRIS) -> konfirmasi.
Setelah dibayar, admin menerima kartu pesanan, membeli di Fragment, mengirim ke username tujuan, lalu
menekan "Sudah Dikirim" (user diberi kabar) atau "Tolak & Refund" (dana kembali ke Saldo Bot).
"""

import logging
from html import escape as _esc

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters,
)

from config.settings import settings
from database.connection import SessionLocal
from database import stars_crud
from database.crud import get_user_balance
from bot.keyboards.main_menu import get_owner_button
from bot.utils.emojis import (
    CUSTOM_EMOJI_IDS, E_STAR, E_CROWN, E_CHECK, E_CROSS, E_WARN, E_CARD, E_TAG, E_BOX, E_DOLLAR,
    E_CALENDAR, E_HISTORY, E_CHART, E_VERIFIED,
)
from bot.utils.flow_guard import block_if_busy
from bot.utils.formatter import format_idr, format_datetime
from bot.utils.telegram_utils import notify_admins, refresh_message_status, relabel_recheck_button, wib_clock
from services.stars_catalog import (
    PRODUCTS, package_label, package_price, parse_username, pricelist_text,
)

logger = logging.getLogger(__name__)

ST_MENU, ST_PACKAGE, ST_USERNAME, ST_PAYMENT, ST_CONFIRM = range(5)

_STATUS_TEXT = {
    "pending": "⏳ Menunggu pembayaran",
    "paid": "🛠 Dibayar, diproses admin",
    "completed": "✅ Selesai dikirim",
    "expired": "⌛ Kedaluwarsa",
    "cancelled": "🚫 Dibatalkan",
    "rejected": "❌ Ditolak, dana dikembalikan ke Saldo Bot",
}
_PAY_LABEL = {"BOT_BALANCE": "Saldo Bot", "GOPAY_QRIS": "QRIS"}


def _btn(text: str, data: str, icon: str = "") -> InlineKeyboardButton:
    """Tombol dengan ikon emoji premium (custom emoji) di depan teks; tanpa ikon bila kuncinya tidak ada."""
    emoji_id = CUSTOM_EMOJI_IDS.get(icon) if icon else None
    if emoji_id:
        return InlineKeyboardButton(text, callback_data=data, icon_custom_emoji_id=emoji_id)
    return InlineKeyboardButton(text, callback_data=data)


def _icon(product: str) -> str:
    """Emoji premium produk: Stars = bintang, Premium = mahkota."""
    return E_STAR() if product == "stars" else E_CROWN()


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("Beli Telegram Stars", "stp_prod_stars", "STAR")],
        [_btn("Beli Telegram Premium", "stp_prod_premium", "CROWN")],
        [_btn("Pricelist", "stp_pricelist", "CHART"),
         _btn("Pesanan Saya", "stp_mine", "HISTORY")],
        [_btn("Menu Utama", "menu_back", "BACK")],
    ])


def _menu_text() -> str:
    return (
        f"{E_STAR()} <b>TELEGRAM STARS &amp; PREMIUM</b>\n\n"
        "Beli Stars atau Premium dan kirim ke akun Telegram mana pun: akun sendiri maupun akun teman.\n"
        "Pembayaran lewat <b>Saldo Bot</b> atau <b>QRIS</b>. Admin mengirim setelah pembayaran diterima."
    )


async def _send(update: Update, text: str, markup=None) -> None:
    """Edit pesan bila dari tombol, atau kirim pesan baru bila dari perintah/ketikan."""
    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)


async def start_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await block_if_busy("stars", update, context):
        return None
    if update.callback_query:
        await update.callback_query.answer()
    for key in ("stp_product", "stp_package", "stp_recipient", "stp_method"):
        context.user_data.pop(key, None)
    await _send(update, _menu_text(), _menu_keyboard())
    return ST_MENU


async def show_pricelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await _send(update, pricelist_text(), InlineKeyboardMarkup([
        [_btn("Beli Stars", "stp_prod_stars", "STAR"),
         _btn("Beli Premium", "stp_prod_premium", "CROWN")],
        [_btn("Kembali", "stp_menu", "BACK")],
    ]))
    return ST_MENU


async def show_my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    db = SessionLocal()
    try:
        rows = stars_crud.get_user_orders(db, update.effective_user.id, limit=5)
    finally:
        db.close()
    if not rows:
        text = f"{E_HISTORY()} <b>PESANAN SAYA</b>\n\nBelum ada pesanan Stars atau Premium."
    else:
        lines = [f"{E_HISTORY()} <b>PESANAN SAYA</b> <i>(5 terakhir)</i>"]
        for o in rows:
            lines.append(
                f"\n{E_TAG()} <code>{_esc(o.order_id)}</code>\n"
                f"{package_label(o.product.lower(), o.package)} → @{_esc(o.recipient_username)} · {format_idr(int(o.price_idr))}\n"
                f"{_STATUS_TEXT.get(o.status, o.status)} · {format_datetime(o.created_at)}")
        text = "\n".join(lines)
    await _send(update, text, InlineKeyboardMarkup([[_btn("Kembali", "stp_menu", "BACK")]]))
    return ST_MENU


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start_stars(update, context)


async def choose_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    product = query.data.removeprefix("stp_prod_")
    if product not in PRODUCTS:
        return ST_MENU
    context.user_data["stp_product"] = product
    info = PRODUCTS[product]
    rows, row = [], []
    for key, price in info["packages"].items():
        row.append(InlineKeyboardButton(f"{package_label(product, key)} · {format_idr(price)}",
                                        callback_data=f"stp_pkg_{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn("Kembali", "stp_menu", "BACK")])
    await _send(update, f"{_icon(product)} <b>{info['label'].upper()}</b>\n\nPilih paket:", InlineKeyboardMarkup(rows))
    return ST_PACKAGE


async def choose_package(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    product = context.user_data.get("stp_product")
    try:
        key = int(query.data.removeprefix("stp_pkg_"))
    except ValueError:
        key = 0
    if not product or package_price(product, key) <= 0:
        await query.answer("Sesi habis, mulai ulang dari menu.", show_alert=True)
        return await start_stars(update, context)
    await query.answer()
    context.user_data["stp_package"] = key
    keyboard = []
    own = (update.effective_user.username or "").strip()
    if own:
        keyboard.append([_btn(f"Akun saya (@{own})", "stp_self", "USER")])
    keyboard.append([_btn("Ganti paket", f"stp_prod_{product}", "BACK")])
    await _send(
        update,
        f"{_icon(product)} <b>{package_label(product, key)}</b> · {format_idr(package_price(product, key))}\n\n"
        "Kirim <b>username Telegram tujuan</b> (akun sendiri atau akun teman), contoh: <code>@namauser</code>.\n"
        "<i>Pastikan username benar, pesanan yang sudah dikirim tidak bisa ditarik kembali.</i>",
        InlineKeyboardMarkup(keyboard))
    return ST_USERNAME


async def _go_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, recipient: str) -> int:
    product, key = context.user_data["stp_product"], context.user_data["stp_package"]
    context.user_data["stp_recipient"] = recipient
    price = package_price(product, key)
    db = SessionLocal()
    try:
        balance = int(get_user_balance(db, update.effective_user.id))
        open_order = stars_crud.get_open_qris_order(db, update.effective_user.id)
    finally:
        db.close()
    if open_order:
        await _send(update, (
            f"{E_WARN()} Kamu masih punya pesanan QRIS yang belum dibayar "
            f"(<code>{_esc(open_order.order_id)}</code>). Selesaikan atau batalkan dulu sebelum membuat pesanan baru."),
            InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back")]]))
        return ConversationHandler.END
    keyboard = []
    if balance >= price:
        keyboard.append([InlineKeyboardButton(f"Saldo Bot ({format_idr(balance)}) — Instan", callback_data="stp_pay_BOT_BALANCE")])
    keyboard.append([InlineKeyboardButton("QRIS (semua e-wallet & bank)", callback_data="stp_pay_GOPAY_QRIS")])
    keyboard.append([_btn("Ganti paket", f"stp_prod_{product}", "BACK"),
                     _btn("Batal", "stp_abort", "CROSS")])
    await _send(update, (
        f"{E_CARD()} <b>PILIH PEMBAYARAN</b>\n\n"
        f"Produk: <b>{package_label(product, key)}</b> ({PRODUCTS[product]['label']})\n"
        f"Tujuan: <b>@{_esc(recipient)}</b>\n"
        f"Harga: <b>{format_idr(price)}</b>"), InlineKeyboardMarkup(keyboard))
    return ST_PAYMENT


async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data.get("stp_package"):
        return await start_stars(update, context)
    recipient = parse_username(update.message.text)
    if not recipient:
        await update.message.reply_text(
            f"{E_CROSS()} <b>Username tidak valid.</b>\n\nUsername Telegram 5–32 karakter (huruf, angka, garis bawah), "
            "diawali huruf. Contoh: <code>@namauser</code>. Coba kirim lagi:", parse_mode="HTML")
        return ST_USERNAME
    return await _go_payment(update, context, recipient)


async def use_own_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    own = parse_username(update.effective_user.username or "")
    if not own or not context.user_data.get("stp_package"):
        await query.answer("Akunmu belum punya username. Ketik username tujuan.", show_alert=True)
        return ST_USERNAME
    await query.answer()
    return await _go_payment(update, context, own)


async def choose_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    method = query.data.removeprefix("stp_pay_")
    ud = context.user_data
    if method not in _PAY_LABEL or not ud.get("stp_recipient"):
        await query.answer("Sesi habis, mulai ulang dari menu.", show_alert=True)
        return await start_stars(update, context)
    await query.answer()
    ud["stp_method"] = method
    product, key = ud["stp_product"], ud["stp_package"]
    price = package_price(product, key)
    extra = ("\n<i>Kode unik akan ditambahkan ke total bayar untuk verifikasi otomatis.</i>"
             if method == "GOPAY_QRIS" else "")
    await _send(update, (
        f"{E_HISTORY()} <b>KONFIRMASI PESANAN</b>\n\n"
        f"Produk: <b>{package_label(product, key)}</b> ({PRODUCTS[product]['label']})\n"
        f"Tujuan: <b>@{_esc(ud['stp_recipient'])}</b>\n"
        f"Harga: <b>{format_idr(price)}</b>\n"
        f"Pembayaran: <b>{_PAY_LABEL[method]}</b>{extra}\n\n"
        "Pastikan username sudah benar sebelum membayar."),
        InlineKeyboardMarkup([
            [_btn("Konfirmasi & Bayar", "stp_confirm", "CHECK"),
             _btn("Batal", "stp_abort", "CROSS")]]))
    return ST_CONFIRM


def _qris_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Saya Sudah Transfer", callback_data=f"stp_check_{order_id}")],
        [_btn("Batalkan Order", f"stp_cancel_{order_id}", "CROSS")],
        [InlineKeyboardButton("Menu Utama", callback_data="menu_back")],
        [get_owner_button()],
    ])


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    ud = context.user_data
    product, key, recipient, method = ud.get("stp_product"), ud.get("stp_package"), ud.get("stp_recipient"), ud.get("stp_method")
    if not (product and key and recipient and method):
        await query.answer("Sesi habis, mulai ulang dari menu.", show_alert=True)
        return await start_stars(update, context)
    await query.answer()
    user_id = update.effective_user.id
    menu_back = InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back")]])

    db = SessionLocal()
    try:
        if method == "BOT_BALANCE":
            order = stars_crud.create_balance_order(db, user_id, product, key, recipient)
            if order is None:
                await query.edit_message_text(
                    f"{E_CROSS()} <b>Saldo Bot tidak cukup.</b>\n\nTopup saldo dulu atau pilih pembayaran QRIS.",
                    reply_markup=menu_back, parse_mode="HTML")
                return ConversationHandler.END
            await query.edit_message_text(
                f"{E_CHECK()} <b>PEMBAYARAN DITERIMA</b>\n\n"
                f"{E_TAG()} <code>{_esc(order.order_id)}</code>\n"
                f"{package_label(product, key)} → <b>@{_esc(recipient)}</b>\n"
                f"Dibayar: <b>{format_idr(int(order.total_idr))}</b> (Saldo Bot)\n\n"
                "Admin akan mengirim pesananmu dan memberi kabar di sini.",
                reply_markup=menu_back, parse_mode="HTML")
            await announce_paid(context.bot, order, db)
            return ConversationHandler.END

        if stars_crud.get_open_qris_order(db, user_id):
            await query.edit_message_text(
                f"{E_WARN()} Kamu masih punya pesanan QRIS yang belum dibayar. Selesaikan atau batalkan dulu.",
                reply_markup=menu_back, parse_mode="HTML")
            return ConversationHandler.END
        order = stars_crud.create_qris_order(db, user_id, product, key, recipient, settings.ORDER_EXPIRE_MINUTES)
        if order is None:
            await query.edit_message_text(
                "⏳ <b>Antrean pembayaran QRIS sedang penuh.</b>\n\nCoba lagi beberapa menit lagi. Pembayaran belum dilakukan.",
                reply_markup=menu_back, parse_mode="HTML")
            return ConversationHandler.END
        total = int(order.total_idr)
        caption = (
            f"{E_CARD()} <b>BAYAR VIA QRIS DINAMIS</b>\n\n"
            f"{E_TAG()} <b>ID Order</b>: <code>{_esc(order.order_id)}</code>\n"
            f"{E_BOX()} <b>Pesanan</b>: {package_label(product, key)} → @{_esc(recipient)}\n"
            f"{E_DOLLAR()} <b>Total Bayar</b>: <b>{format_idr(total)}</b>\n"
            f"{E_CALENDAR()} <b>Batas Waktu</b>: {settings.ORDER_EXPIRE_MINUTES} Menit\n\n"
            "<b>Cara Bayar:</b>\n"
            "1. Scan QRIS di atas dengan e-wallet atau mobile banking.\n"
            f"2. Nominal <b>{format_idr(total)}</b> akan muncul otomatis.\n"
            "<i>Kode unik di belakang nominal dipakai untuk verifikasi pembayaran otomatis.</i>\n"
            "3. Selesaikan pembayaran, pesanan diteruskan ke admin setelah terdeteksi.\n\n"
            f"{E_WARN()} <i>Bayar tepat {format_idr(total)} agar terdeteksi otomatis.</i>")
        markup = _qris_keyboard(order.order_id)
        from services.qris_generator import get_qris_image_stream
        stream = get_qris_image_stream(total)
        sent = False
        if stream:
            try:
                await context.bot.send_photo(chat_id=user_id, photo=stream, caption=caption,
                                             parse_mode="HTML", reply_markup=markup)
                sent = True
            except Exception as exc:
                logger.warning("Gagal kirim QRIS Stars/Premium: %s", exc)
        if not sent:
            await context.bot.send_message(chat_id=user_id, text=caption, parse_mode="HTML", reply_markup=markup)
        try:
            await query.message.delete()
        except Exception:
            pass
        return ConversationHandler.END
    except Exception:
        logger.exception("Gagal membuat pesanan Stars/Premium")
        await query.message.reply_text("⚠️ Terjadi kesalahan saat membuat pesanan. Coba lagi sesaat lagi.")
        return ConversationHandler.END
    finally:
        db.close()


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        from bot.handlers.start import send_main_menu
        await send_main_menu(update, context)
    else:
        await update.message.reply_text("❌ Pesanan dibatalkan.")
    return ConversationHandler.END


async def start_from_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/start di tengah alur: keluar dari alur lalu tampilkan sambutan seperti biasa."""
    from bot.handlers.start import start_handler
    await start_handler(update, context)
    return ConversationHandler.END


# ───────────────────── pembayaran QRIS (di luar percakapan) ─────────────────────

async def _post_testimony(bot, order) -> None:
    """Testimoni Stars/Premium ke channel setelah pesanan selesai. Tidak pernah melempar (pesanan sudah selesai)."""
    try:
        from services.testimony_service import post_stars_testimony
        db = SessionLocal()
        try:
            await post_stars_testimony(bot, order, db)
        finally:
            db.close()
    except Exception:
        logger.exception("Gagal memposting testimoni Stars/Premium %s", getattr(order, "order_id", "?"))


def _admin_card(order, user_html: str):
    from bot.utils.admin_alert import idr_copy
    from services.stars_fulfillment import autofulfill_enabled
    late = (f"\n{E_WARN()} <b>DIBAYAR SETELAH KEDALUWARSA</b>: cek mutasi dulu sebelum mengirim."
            if order.late_payment else "")
    if order.status == "review":
        head = "🚨 <b>PESANAN STARS / PREMIUM — BUTUH DICEK</b>"
        late += (f"\n🚨 Otomatis terhenti: {_esc(order.fulfill_error or '-')}. Ref: <code>{_esc(order.fulfill_ref or '-')}</code>. "
                 "Cek wallet Fragment/tonviewer dan status Stars di akun tujuan SEBELUM kirim ulang atau refund.")
    else:
        head = f"{E_STAR()} <b>PESANAN STARS / PREMIUM — SIAP DIKIRIM</b>"
        if autofulfill_enabled() and order.status == "paid" and not order.fulfill_attempts:
            late += "\n🤖 Mode otomatis aktif: sistem sedang memprosesnya. Tunggu hasilnya sebelum bertindak manual."
        elif order.fulfill_error:
            late += f"\nℹ️ Otomatis tidak dipakai: {_esc(order.fulfill_error)}"
    text = (
        f"{head}\n\n"
        f"{E_TAG()} ID: <code>{_esc(order.order_id)}</code>\n"
        f"User: {user_html}\n"
        f"Produk: <b>{package_label(order.product.lower(), order.package)}</b> ({PRODUCTS[order.product.lower()]['label']})\n"
        f"Kirim ke: <code>@{_esc(order.recipient_username)}</code>\n"
        f"Dibayar: {idr_copy(order.total_idr)} ({_PAY_LABEL.get(order.payment_method, order.payment_method)}){late}\n\n"
        "Beli di Fragment dan kirim ke username di atas, lalu tekan <b>Sudah Dikirim</b>. "
        "Tolak akan mengembalikan dana ke Saldo Bot user."
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✋ Saya Tangani (sebelum kirim)", callback_data=f"admin_stp_take_{order.order_id}")],
        [_btn("Sudah Dikirim", f"admin_stp_ok_{order.order_id}", "CHECK"),
         _btn("Tolak & Refund", f"admin_stp_no_{order.order_id}", "CROSS")],
    ])
    return text, markup


async def announce_paid(bot, order, db) -> None:
    """Pesanan baru dibayar: kabari admin (topik beli + DM) dengan kartu bertombol. Tidak pernah melempar."""
    try:
        from bot.utils.admin_alert import user_label
        text, markup = _admin_card(order, user_label(order.telegram_id, db))
        await notify_admins(bot, text, reply_markup=markup, kind="stars", butuh_tindakan=True)
    except Exception:
        logger.exception("Gagal mengabari admin untuk pesanan %s", getattr(order, "order_id", "?"))
    # Pemenuhan otomatis berjalan terpisah dan tidak boleh menahan/menggagalkan alur pembayaran.
    try:
        from services.stars_fulfillment import autofulfill_enabled, try_auto_fulfill
        if autofulfill_enabled():
            import asyncio
            asyncio.create_task(try_auto_fulfill(bot, order.order_id))
    except Exception:
        logger.exception("Gagal memulai pemenuhan otomatis %s", getattr(order, "order_id", "?"))


async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tombol 'Saya Sudah Transfer' pada QRIS Stars/Premium."""
    from services.gopay_service import gopay_service

    query = update.callback_query
    order_id = query.data.removeprefix("stp_check_")
    db = SessionLocal()
    try:
        order = stars_crud.get_order(db, order_id)
        if not order or order.telegram_id != update.effective_user.id:
            await query.answer("❌ Pesanan tidak ditemukan.", show_alert=True)
            return
        if order.status not in ("pending", "expired"):
            await query.answer(f"ℹ️ {_STATUS_TEXT.get(order.status, order.status)}", show_alert=True)
            return
        paid = await gopay_service.confirm_payment(
            db, amount=int(order.total_idr), ref_id=order.order_id, kind="stars", created_at=order.created_at)
        if paid:
            claimed = stars_crud.mark_paid(db, order.order_id)
            await query.answer("✅ Pembayaran diterima! Pesanan diteruskan ke admin.", show_alert=False)
            if claimed:
                await _after_qris_paid(context.bot, claimed, db)
            return
        stamp = wib_clock()
        status = (f"⏳ <b>Status:</b> pembayaran belum terdeteksi · dicek {stamp} WIB\n"
                  "<i>Mutasi QRIS butuh 30-60 detik. Tekan Cek Ulang lagi.</i>")
        updated = await refresh_message_status(
            query, status, relabel_recheck_button(query.message.reply_markup, "stp_check_"))
        await query.answer("⏳ Belum terdeteksi" if updated else
                           f"⏳ Pembayaran belum terdeteksi (dicek {stamp} WIB). Coba lagi sebentar lagi.",
                           show_alert=not updated)
    except Exception:
        logger.exception("Error stp check_payment %s", order_id)
        try:
            await query.answer("❌ Terjadi kesalahan saat memeriksa pembayaran. Coba lagi.", show_alert=True)
        except Exception:
            pass
    finally:
        db.close()


async def _after_qris_paid(bot, order, db) -> None:
    """Pembayaran QRIS baru terdeteksi: kabari user dan admin (dipanggil tepat sekali oleh pemenang klaim)."""
    try:
        from bot.utils.telegram_utils import safe_send_message
        await safe_send_message(
            bot, order.telegram_id,
            f"{E_CHECK()} <b>PEMBAYARAN DITERIMA</b>\n\n"
            f"{E_TAG()} <code>{_esc(order.order_id)}</code>\n"
            f"{package_label(order.product.lower(), order.package)} → <b>@{_esc(order.recipient_username)}</b>\n\n"
            "Admin akan mengirim pesananmu dan memberi kabar di sini.")
    except Exception:
        logger.exception("Gagal mengabari user pesanan %s", order.order_id)
    await announce_paid(bot, order, db)


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tombol 'Batalkan Order': cek pembayaran terakhir dulu, baru batalkan."""
    from services.gopay_service import gopay_service

    query = update.callback_query
    order_id = query.data.removeprefix("stp_cancel_")
    db = SessionLocal()
    try:
        order = stars_crud.get_order(db, order_id)
        if not order or order.telegram_id != update.effective_user.id:
            await query.answer("❌ Pesanan tidak ditemukan.", show_alert=True)
            return
        if order.status != "pending":
            await query.answer(f"ℹ️ {_STATUS_TEXT.get(order.status, order.status)}", show_alert=True)
            return
        paid = await gopay_service.confirm_payment(
            db, amount=int(order.total_idr), ref_id=order.order_id, kind="stars", created_at=order.created_at)
        if paid is None:
            await query.answer("⚠️ Pembayaran belum bisa dipastikan. Coba lagi sebentar lagi agar uangmu tidak nyangkut.",
                               show_alert=True)
            return
        if paid:
            claimed = stars_crud.mark_paid(db, order.order_id)
            await query.answer("✅ Ternyata pembayaranmu sudah masuk, pesanan diteruskan ke admin.", show_alert=True)
            if claimed:
                await _after_qris_paid(context.bot, claimed, db)
            return
        if stars_crud.cancel_pending(db, order.order_id, update.effective_user.id):
            await query.answer("Pesanan dibatalkan.")
            text = f"{E_CROSS()} <b>Pesanan <code>{_esc(order.order_id)}</code> dibatalkan.</b>\n\nJangan membayar QRIS ini."
            try:
                if query.message.photo:
                    await query.edit_message_caption(caption=text, parse_mode="HTML")
                else:
                    await query.edit_message_text(text, parse_mode="HTML")
            except Exception:
                pass
        else:
            await query.answer("ℹ️ Pesanan sudah berubah status.", show_alert=True)
    except Exception:
        logger.exception("Error stp cancel_order %s", order_id)
        try:
            await query.answer("❌ Terjadi kesalahan. Coba lagi.", show_alert=True)
        except Exception:
            pass
    finally:
        db.close()


# ───────────────────────────── sisi admin ─────────────────────────────

async def admin_stars_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """admin_stp_{open|take|ok|no}_{order_id}."""
    from bot.handlers.admin import is_admin
    from bot.utils.admin_alert import user_label
    from bot.utils.telegram_utils import safe_send_message
    from database.crud import claim_admin_task

    query = update.callback_query
    admin_id = query.from_user.id
    if not is_admin(admin_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return
    action, _, order_id = query.data.removeprefix("admin_stp_").partition("_")

    db = SessionLocal()
    try:
        order = stars_crud.get_order(db, order_id)
        if order is None:
            await query.answer("Pesanan tidak ditemukan.", show_alert=True)
            return
        if action == "open":
            if order.status not in ("paid", "review"):
                await query.answer(f"{order_id} sudah diproses ({order.status}).", show_alert=True)
                return
            text, markup = _admin_card(order, user_label(order.telegram_id, db))
            await query.answer()
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
            return

        if order.status == "processing":
            await query.answer("🤖 Pesanan ini sedang diproses otomatis. Tunggu hasilnya (maks beberapa menit).", show_alert=True)
            return

        mine, owner = claim_admin_task(db, f"stars:{order_id}", admin_id)
        if not mine:
            await query.answer(f"⛔ {order_id} sedang ditangani admin lain (ID {owner}).", show_alert=True)
            return
        if action == "take":
            await query.answer(f"✋ {order_id} kamu yang tangani. Kirim lalu tekan Sudah Dikirim.", show_alert=True)
            await notify_admins(context.bot, f"⏳ {order_id} sedang ditangani admin <code>{admin_id}</code>. Jangan diproses lagi.",
                                kind="stars")
            return

        approve = action == "ok"
        settled = stars_crud.complete(db, order_id, admin_id) if approve else stars_crud.reject_and_refund(db, order_id, admin_id)
        if settled is None:
            await query.answer("ℹ️ Pesanan ini sudah diproses.", show_alert=True)
            return
        label = package_label(settled.product.lower(), settled.package)
        if approve:
            user_msg = (f"{E_VERIFIED()} <b>PESANAN SELESAI</b>\n\n{E_TAG()} <code>{_esc(settled.order_id)}</code>\n"
                        f"<b>{label}</b> sudah dikirim ke <b>@{_esc(settled.recipient_username)}</b>.\nTerima kasih! 🙏")
            suffix = "✅ <b>SUDAH DIKIRIM</b>"
        else:
            user_msg = (f"{E_CROSS()} <b>PESANAN DITOLAK</b>\n\n{E_TAG()} <code>{_esc(settled.order_id)}</code>\n"
                        f"Dana <b>{format_idr(int(settled.total_idr))}</b> dikembalikan ke Saldo Bot kamu. "
                        "Hubungi admin bila ada pertanyaan.")
            suffix = "❌ <b>DITOLAK & DANA DI-REFUND</b>"
        await safe_send_message(context.bot, settled.telegram_id, user_msg)
        if approve:
            await _post_testimony(context.bot, settled)
        await query.answer("Selesai.")
        try:
            await query.edit_message_text(f"{query.message.text_html}\n\n{suffix}", parse_mode="HTML")
        except Exception:
            pass
    except Exception:
        logger.exception("Error admin_stars_callback %s", order_id)
        try:
            await query.answer("❌ Gagal memproses pesanan.", show_alert=True)
        except Exception:
            pass
    finally:
        db.close()


def build_admin_stars_view(db):
    """Antrean pesanan Stars/Premium yang sudah dibayar dan menunggu dikirim."""
    from bot.handlers.admin import _age_text
    from bot.utils.admin_alert import user_label

    count, total = stars_crud.paid_summary(db)
    rows = stars_crud.get_paid_orders(db, limit=8)
    lines = [f"{E_STAR()} <b>STARS &amp; PREMIUM</b>", f"Menunggu dikirim: <b>{count}</b> pesanan · <b>{format_idr(total)}</b>"]
    keyboard = []
    if not rows:
        lines.append(f"\n{E_CHECK()} Tidak ada pesanan yang menunggu.")
    for o in rows:
        lines.append(
            f"\n{E_TAG()} <code>{_esc(o.order_id)}</code> · <b>{package_label(o.product.lower(), o.package)}</b> → "
            f"@{_esc(o.recipient_username)} · {format_idr(int(o.total_idr))} · {_age_text(o.paid_at)}"
            f"{' ⚠️ telat' if o.late_payment else ''}{' 🚨 CEK' if o.status == 'review' else ''}\n{user_label(o.telegram_id, db)}")
        keyboard.append([InlineKeyboardButton(f"📄 Buka {o.order_id[-8:]} · {package_label(o.product.lower(), o.package)}",
                                              callback_data=f"admin_stp_open_{o.order_id}")])
    if count > len(rows):
        lines.append(f"\n… dan {count - len(rows)} lainnya. Selesaikan yang di atas dulu, lalu refresh.")
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="admin_panel_stars"),
                     _btn("Dashboard Utama", "admin_panel_main", "BACK")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def stars_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/starscheck (admin): kesiapan pemenuhan otomatis Stars/Premium. Tidak membeli apa pun, tidak mencetak rahasia."""
    from bot.handlers.admin import is_admin
    from services import stars_fulfillment as F

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Anda tidak memiliki akses.")
        return
    lines = ["🔎 <b>KESIAPAN STARS/PREMIUM OTOMATIS</b>\n"]
    lines.append(f"Mode otomatis: <b>{'AKTIF' if F.autofulfill_enabled() else 'NONAKTIF (manual)'}</b> "
                 "<i>(STARS_AUTOFULFILL)</i>")
    gaps = F.missing_config()
    lines.append("Pengaturan: " + ("✅ lengkap" if not gaps else "❌ belum ada: <code>" + ", ".join(gaps) + "</code>"))
    lines.append(f"Batas per pesanan: {F._idr(F.settings.STARS_MAX_ORDER_IDR)} · harian: {F._idr(F.settings.STARS_DAILY_LIMIT_IDR)} "
                 f"· margin minimum: {F.settings.STARS_MIN_MARGIN_PCT:g}%")
    db = SessionLocal()
    try:
        spent = stars_crud.auto_spent_today_idr(db)
        count, total = stars_crud.paid_summary(db)
    finally:
        db.close()
    lines.append(f"Terpakai otomatis hari ini: {F._idr(spent)} · antrean menunggu: {count} pesanan ({F._idr(total)})")

    if not F.settings.STARS_TON_PRIVATE_KEY or not F.settings.STARS_TON_WALLET_ADDRESS:
        lines.append("Wallet: ⚪ belum diisi")
    else:
        try:
            wallet = F.build_wallet()
            version = wallet.verify_key_matches_address()
            balance = await wallet.get_balance("TON")
            lines.append(f"Wallet: ✅ <code>{_esc(wallet.wallet_address)}</code> ({version}) · saldo {balance:.4f} TON")
        except Exception as exc:
            lines.append(f"Wallet: ❌ {_esc(str(exc)[:160])}")
    if not (F.settings.FRAGMENT_HASH and F.settings.FRAGMENT_COOKIE):
        lines.append("Sesi Fragment: ⚪ belum diisi")
    else:
        try:
            name = await F.build_client().check_session(F.settings.OWNER_USERNAME or "telegram")
            lines.append(f"Sesi Fragment: ✅ aktif (uji cari penerima: {_esc(name)})")
        except Exception as exc:
            lines.append(f"Sesi Fragment: ❌ {_esc(str(exc)[:160])}")
    price = await F.ton_price_idr()
    lines.append(f"Harga TON: {F._idr(price)}" if price else "Harga TON: ❌ tidak tersedia")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


stars_conversation_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_stars, pattern="^menu_stars$"),
        CommandHandler(["stars", "premium"], start_stars),
    ],
    states={
        ST_MENU: [
            CallbackQueryHandler(choose_product, pattern="^stp_prod_"),
            CallbackQueryHandler(show_pricelist, pattern="^stp_pricelist$"),
            CallbackQueryHandler(show_my_orders, pattern="^stp_mine$"),
            CallbackQueryHandler(back_to_menu, pattern="^stp_menu$"),
        ],
        ST_PACKAGE: [
            CallbackQueryHandler(choose_package, pattern="^stp_pkg_"),
            CallbackQueryHandler(back_to_menu, pattern="^stp_menu$"),
        ],
        ST_USERNAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username),
            CallbackQueryHandler(use_own_username, pattern="^stp_self$"),
            CallbackQueryHandler(choose_product, pattern="^stp_prod_"),
        ],
        ST_PAYMENT: [
            CallbackQueryHandler(choose_payment, pattern="^stp_pay_"),
            CallbackQueryHandler(choose_product, pattern="^stp_prod_"),
        ],
        ST_CONFIRM: [
            CallbackQueryHandler(confirm_order, pattern="^stp_confirm$"),
        ],
    },
    fallbacks=[
        CallbackQueryHandler(cancel_flow, pattern="^stp_abort$"),
        CallbackQueryHandler(cancel_flow, pattern="^menu_back$"),
        CommandHandler("cancel", cancel_flow),
        CommandHandler("start", start_from_command),
    ],
    allow_reentry=True,
)
