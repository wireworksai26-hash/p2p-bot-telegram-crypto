"""
bot/handlers/balance.py — Balance Management & QRIS Topup Handlers
====================================================================
Menangani fitur:
1. Cek Saldo & Profil User (💰 Cek Saldo / 🤖 Profil)
2. Topup Saldo via QRIS Dinamis (GoPay / Gopiz API Gateway)
3. Cek Status Pembayaran Manual & Pembatalan Invoice Topup
"""

import logging
import os
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from database.connection import SessionLocal
from database.crud import (
    get_user_balance,
    credit_user_balance,
    create_topup_order,
    get_topup_order_by_id,
    update_topup_status,
    generate_unique_payment_code,
    claim_topup_success,
)
from services.gopay_service import gopay_service
from bot.keyboards.main_menu import get_owner_button
from bot.utils.formatter import format_idr
from bot.utils.validator import validate_amount_idr
from config.assets import QRIS_STATIC_IMAGE

logger = logging.getLogger(__name__)

# State percakapan topup
SELECT_TOPUP_NOMINAL = 1
WAITING_CUSTOM_NOMINAL = 2


async def show_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan tampilan Cek Saldo & Profil User."""
    user = update.effective_user
    db = SessionLocal()
    try:
        balance = get_user_balance(db, user.id)
    finally:
        db.close()

    text = (
        f"💰 <b>CEK SALDO & PROFIL USER</b>\n\n"
        f"👤 <b>Nama</b>    : {user.full_name or 'N/A'}\n"
        f"🏷️ <b>Username</b>: @{user.username or 'N/A'}\n"
        f"🆔 <b>ID User</b> : <code>{user.id}</code>\n"
        f"💳 <b>Saldo IDR</b>: <b>{format_idr(int(balance))}</b>\n\n"
        f"💡 <i>Saldo IDR dapat digunakan untuk membeli koin crypto secara instan (1-Tap) tanpa perlu transfer bank!</i>"
    )

    keyboard = [
        [InlineKeyboardButton("➕ Topup Saldo (QRIS)", callback_data="start_topup_qris")],
        [InlineKeyboardButton("🔙 Kembali ke Menu", callback_data="menu_back")],
        [get_owner_button()]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def start_topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menampilkan pilihan nominal preset topup saldo."""
    query = update.callback_query
    await query.answer()

    text = (
        "💰 <b>TOPUP SALDO BOT (QRIS)</b>\n\n"
        "Silakan pilih nominal deposit saldo di bawah ini:\n"
        "<i>Semua pembayaran QRIS via GoPay, OVO, Dana, ShopeePay, BCA, Mandiri, dll.</i>"
    )

    keyboard = [
        [
            InlineKeyboardButton("Rp 5.000", callback_data="topup_nom_5000"),
            InlineKeyboardButton("Rp 10.000", callback_data="topup_nom_10000"),
        ],
        [
            InlineKeyboardButton("Rp 25.000", callback_data="topup_nom_25000"),
            InlineKeyboardButton("Rp 50.000", callback_data="topup_nom_50000"),
        ],
        [
            InlineKeyboardButton("Rp 100.000", callback_data="topup_nom_100000"),
            InlineKeyboardButton("✏️ Custom Nominal", callback_data="topup_nom_custom"),
        ],
        [InlineKeyboardButton("🔙 Batal", callback_data="cancel_topup")],
        [get_owner_button()]
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return SELECT_TOPUP_NOMINAL


async def handle_preset_nominal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Memproses nominal preset yang dipilih user."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "topup_nom_custom":
        await query.edit_message_text(
            "✏️ <b>Ketik Nominal Topup Custom:</b>\n\n"
            "Ketik angka nominal Rupiah yang ingin Anda deposit (minimal Rp 5.000, contoh: <code>15000</code>):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Batal", callback_data="cancel_topup")]])
        )
        return WAITING_CUSTOM_NOMINAL

    nominal = int(data.replace("topup_nom_", ""))
    return await generate_and_send_qris(update, context, nominal)


async def handle_custom_nominal_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Memvalidasi dan memproses nominal custom yang diketik user."""
    text_input = (update.message.text or "").strip()
    
    # 1. Cek jika user keliru menginput alamat wallet
    if text_input.lower().startswith("0x") or (len(text_input) >= 32 and not text_input.isdigit()):
        await update.message.reply_text(
            "⚠️ <b>Input Terdeteksi Sebagai Alamat Wallet!</b>\n\n"
            "Pada langkah ini, silakan masukkan <b>Nominal Rupiah (IDR)</b> yang ingin di-topup (contoh: <code>50000</code> atau <code>50k</code>):",
            parse_mode="HTML"
        )
        return WAITING_CUSTOM_NOMINAL

    # 2. Validasi nominal IDR
    is_valid, nominal = validate_amount_idr(text_input)
    if not is_valid:
        if nominal > 0 and nominal < 5000:
            await update.message.reply_text("❌ Minimal topup adalah <b>Rp 5.000</b>. Silakan ketik nominal yang lebih besar:", parse_mode="HTML")
        elif nominal > 10_000_000:
            await update.message.reply_text("❌ Maksimal topup adalah <b>Rp 10.000.000</b> per transaksi (limit QRIS BI). Silakan ketik nominal lain:", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Format nominal tidak valid. Ketik angka nominal (contoh: <code>25000</code> atau <code>25.000</code> atau <code>25k</code>):", parse_mode="HTML")
        return WAITING_CUSTOM_NOMINAL

    return await generate_and_send_qris(update, context, nominal)


async def generate_and_send_qris(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int) -> int:
    """Menyajikan invoice topup QRIS statis (pembayaran manual) kepada user."""
    user = update.effective_user
    status_msg = None
    if update.callback_query:
        status_msg = await update.callback_query.edit_message_text("⏳ <i>Menyiapkan invoice pembayaran...</i>", parse_mode="HTML")
    else:
        status_msg = await update.message.reply_text("⏳ <i>Menyiapkan invoice pembayaran...</i>", parse_mode="HTML")

    topup_id = f"TOPUP-{int(datetime.utcnow().timestamp())}"
    expires_at = datetime.utcnow() + timedelta(minutes=30)

    db = SessionLocal()
    try:
        unique_code = generate_unique_payment_code(db)
        final_amount = amount + unique_code
        topup_order = create_topup_order(
            db=db,
            topup_id=topup_id,
            telegram_id=user.id,
            amount_idr=final_amount,
            expires_at=expires_at
        )
        topup_order.unique_code = unique_code
        db.commit()
    finally:
        db.close()

    context.user_data["active_topup_id"] = topup_id

    caption_text = (
        f"💰 <b>INVOICE TOPUP SALDO BOT (QRIS)</b>\n\n"
        f"🎫 <b>ID Topup</b>: <code>{topup_id}</code>\n"
        f"💵 <b>Nominal IDR</b>: <code>{final_amount}</code> ({format_idr(final_amount)})\n"
        f"⏰ <b>Batas Waktu</b>: 30 Menit\n\n"
        f"📌 <b>Cara Bayar:</b>\n"
        f"1. Scan QRIS di atas dengan GoPay, OVO, Dana, ShopeePay, atau Mobile Banking.\n"
        f"2. Ketik/input nominal <b>{format_idr(final_amount)}</b> secara manual.\n"
        f"3. Setelah transfer, <b>kirim foto bukti transfer</b> ke chat ini.\n"
        f"4. Saldo IDR bot Anda otomatis bertambah setelah pembayaran terverifikasi.\n\n"
        f"ℹ️ <i><b>Catatan Nominal:</b> Transfer <b>PAS SESUAI NOMINAL PRESISI</b> dan kode unik. Jika nominal berbeda, pembayaran tidak terverifikasi otomatis dan perlu bantuan admin.</i>"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Saya Sudah Transfer", callback_data=f"check_topup_{topup_id}")],
        [InlineKeyboardButton("❌ Batalkan Topup", callback_data=f"cancel_topup_{topup_id}")],
        [get_owner_button()]
    ]


    # Delete status message
    try:
        if update.callback_query:
            await update.callback_query.message.delete()
        elif status_msg:
            await status_msg.delete()
    except Exception:
        pass

    from services.qris_generator import get_qris_image_stream
    qris_stream = get_qris_image_stream(final_amount)
    sent = False
    if qris_stream:
        try:
            await context.bot.send_photo(
                chat_id=user.id,
                photo=qris_stream,
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            sent = True
        except Exception as pe:
            logger.warning(f"Gagal upload QRIS photo topup: {pe}")
    
    if not sent:
        await context.bot.send_message(
            chat_id=user.id,
            text=caption_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return ConversationHandler.END


async def check_topup_payment_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mengecek status pembayaran topup secara manual saat user mengklik tombol."""
    query = update.callback_query
    await query.answer()

    topup_id = query.data.replace("check_topup_", "")
    db = SessionLocal()
    try:
        topup = get_topup_order_by_id(db, topup_id)
        if not topup:
            await query.answer("❌ Data topup tidak ditemukan.", show_alert=True)
            return

        if topup.status == "SUCCESS":
            await query.answer("✅ Topup ini sudah lunas & saldo telah masuk!", show_alert=True)
            return
        elif topup.status in ["CANCELLED", "EXPIRED"]:
            await query.answer(f"⚠️ Topup ini sudah {topup.status.lower()}.", show_alert=True)
            return

        # Check with Gopay Gateway
        try:
            pay_res = await gopay_service.check_payment(topup.amount_idr, topup.topup_id)
        except Exception as gw_err:
            logger.warning(f"GoPay Gateway error for {topup_id}: {gw_err}")
            pay_res = {"paid": False, "transaction": None}

        if pay_res and pay_res.get("paid"):
            if not claim_topup_success(db, topup.topup_id):
                await query.answer("ℹ️ Topup ini sudah diproses sistem.", show_alert=True)
                return
            try:
                await query.answer("✅ Pembayaran diterima! Menambahkan saldo...", show_alert=False)
            except Exception:
                pass
            new_bal = credit_user_balance(db, topup.telegram_id, topup.amount_idr)

            success_text = (
                f"✅ <b>PEMBAYARAN QRIS TERVERIFIKASI!</b>\n\n"
                f"🎉 Topup saldo sebesar <b>{format_idr(topup.amount_idr)}</b> telah berhasil masuk!\n"
                f"💳 <b>Total Saldo Bot Anda Saat Ini</b>: <b>{format_idr(int(new_bal))}</b>\n\n"
                f"<i>Terima kasih! Anda dapat langsung menggunakan saldo ini untuk membeli crypto secara instan.</i>"
            )
            keyboard = [[InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")]]
            await query.message.reply_text(success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            # Pembayaran belum terdeteksi — beri instruksi kirim bukti foto
            not_found_text = (
                f"⏳ <b>Pembayaran belum terdeteksi otomatis</b>\n\n"
                f"🎫 ID Topup: <code>{topup_id}</code>\n"
                f"💵 Nominal: <b>{format_idr(topup.amount_idr)}</b>\n\n"
                f"Jika Anda sudah transfer, silakan <b>kirim foto bukti transfer</b> langsung ke chat ini.\n"
                f"Admin akan memverifikasi dan menambahkan saldo secara manual.\n\n"
                f"<i>⚠️ Pastikan nominal transfer sesuai dengan yang tertera pada invoice.</i>"
            )
            keyboard = [
                [InlineKeyboardButton("🔄 Cek Ulang", callback_data=f"check_topup_{topup_id}")],
                [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")],
            ]
            await query.message.reply_text(
                not_found_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
    finally:
        db.close()


async def handle_topup_transfer_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verifikasi foto bukti transfer untuk topup QRIS pending milik user."""
    from database.crud import get_pending_topup_orders
    from config.settings import settings
    from bot.utils.formatter import format_idr
    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        pending = [t for t in get_pending_topup_orders(db) if t.telegram_id == user_id]
        if not pending:
            return
        topup = pending[0]

        photo_file_id = None
        try:
            photo = update.message.photo[-1]
            photo_file_id = photo.file_id
            file = await photo.get_file()
            os.makedirs("proofs", exist_ok=True)
            await file.download_to_drive(f"proofs/{topup.topup_id}.jpg")
        except Exception as exc:
            logger.warning("Gagal simpan bukti topup %s: %s", topup.topup_id, exc)

        # 1. Forward foto bukti topup ke Admin
        admin_caption = (
            f"📸 <b>BUKTI TRANSFER DITERIMA (TOPUP IDR)</b>\n\n"
            f"ID Topup: <code>{topup.topup_id}</code>\n"
            f"User: {update.effective_user.name} (ID: <code>{user_id}</code>)\n"
            f"Total Nominal: <b>{format_idr(topup.amount_idr)}</b>\n\n"
            f"Tekan tombol <b>Approve</b> di bawah jika pembayaran valid untuk menambah saldo user secara otomatis."
        )
        admin_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve Topup Saldo", callback_data=f"admin_approve_topup_{topup.topup_id}"),
                InlineKeyboardButton("❌ Tolak", callback_data=f"admin_reject_topup_{topup.topup_id}")
            ]
        ])
        if photo_file_id:
            for admin_id in settings.ADMIN_CHAT_IDS:
                try:
                    await context.bot.send_photo(
                        chat_id=admin_id,
                        photo=photo_file_id,
                        caption=admin_caption,
                        parse_mode="HTML",
                        reply_markup=admin_keyboard
                    )
                except Exception as admin_err:
                    logger.warning(f"Gagal kirim bukti topup ke admin {admin_id}: {admin_err}")

        # 2. Pesan ke User bahwa bukti diterima
        await update.message.reply_text(
            "⏳ <b>Bukti transfer top-up telah diterima & sedang diverifikasi.</b>\n"
            "Saldo IDR Anda akan otomatis bertambah setelah verifikasi selesai.",
            parse_mode="HTML"
        )

        # 3. Cek otomatis via API GoPay
        pay_res = await gopay_service.check_payment(topup.amount_idr, topup.topup_id)
        if pay_res.get("paid"):
            if not claim_topup_success(db, topup.topup_id):
                return
            new_bal = credit_user_balance(db, topup.telegram_id, topup.amount_idr)
            menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")]])
            await update.message.reply_text(
                f"✅ <b>PEMBAYARAN QRIS TERVERIFIKASI (OTOMATIS)!</b>\n\n"
                f"🎉 Topup saldo sebesar <b>{format_idr(topup.amount_idr)}</b> telah berhasil!\n"
                f"💳 <b>Total Saldo Bot Anda Saat Ini</b>: <b>{format_idr(int(new_bal))}</b>\n\n"
                f"<i>Anda dapat langsung menggunakan saldo ini untuk membeli koin crypto secara instan.</i>",
                reply_markup=menu_keyboard,
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "⚠️ <b>Pembayaran belum terdeteksi sesuai nominal tagihan.</b>\n\n"
                "Jika nominal atau kode unik berbeda, silakan hubungi admin untuk "
                "verifikasi manual.",
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"Error handle_topup_transfer_proof user {user_id}: {e}", exc_info=True)
    finally:
        db.close()



async def cancel_topup_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membatalkan invoice topup."""
    query = update.callback_query
    await query.answer()

    topup_id = query.data.replace("cancel_topup_", "")
    db = SessionLocal()
    try:
        update_topup_status(db, topup_id, "CANCELLED")
    finally:
        db.close()

    await query.edit_message_caption(
        caption=f"❌ <b>Invoice Topup {topup_id} telah dibatalkan.</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")]]),
        parse_mode="HTML"
    )


async def cancel_topup_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Membatalkan alur percakapan topup."""
    query = update.callback_query
    if query:
        await query.answer()
        await show_balance_menu(update, context)
    else:
        await update.message.reply_text("❌ Alur topup dibatalkan.")
    return ConversationHandler.END


topup_conversation_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_topup_callback, pattern="^start_topup_qris$")
    ],
    states={
        SELECT_TOPUP_NOMINAL: [
            CallbackQueryHandler(handle_preset_nominal, pattern="^topup_nom_")
        ],
        WAITING_CUSTOM_NOMINAL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_nominal_input)
        ]
    },
    fallbacks=[
        CallbackQueryHandler(cancel_topup_flow, pattern="^cancel_topup$"),
        CommandHandler("cancel", cancel_topup_flow)
    ]
)
