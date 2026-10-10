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
import secrets
from datetime import datetime, timedelta
from html import escape as _esc

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
from database.models import TopupOrder
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
from services.fee_service import calculate_qris_mdr
from bot.keyboards.main_menu import get_owner_button
from bot.handlers.withdraw import withdraw_button_for
from bot.utils.formatter import format_idr
from bot.utils.flow_guard import block_if_busy
from bot.utils.validator import validate_amount_idr
from config.assets import QRIS_STATIC_IMAGE
from config.settings import settings
from bot.utils.emojis import (
    E_MONEY,
    E_USER,
    E_CARD,
    E_DOLLAR,
    E_CHECK,
    E_SPARKLES,
    E_WARN,
    E_TAG,
    E_ID,
    E_BACK,
    E_PLUS,
    CUSTOM_EMOJI_IDS,
)

logger = logging.getLogger(__name__)

# State percakapan topup
SELECT_TOPUP_NOMINAL = 1
WAITING_CUSTOM_NOMINAL = 2


async def show_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan tampilan Cek Saldo & Profil User."""
    user = update.effective_user
    for key in ("awaiting_withdraw_amount", "wd_bank_id", "wd_amount"):
        context.user_data.pop(key, None)
    db = SessionLocal()
    try:
        balance = get_user_balance(db, user.id)
    finally:
        db.close()

    text = (
        f"{E_MONEY()} <b>CEK SALDO &amp; PROFIL USER</b>\n\n"
        f"{E_USER()} <b>Nama</b>    : {_esc(user.full_name or 'N/A')}\n"
        f"{E_TAG()} <b>Username</b>: @{user.username or 'N/A'}\n"
        f"{E_ID()} <b>ID User</b> : <code>{user.id}</code>\n"
        f"{E_CARD()} <b>Saldo IDR</b>: <b>{format_idr(int(balance))}</b>\n"
        f"💸 <i>Withdraw: min 10k, maks 100k/hari, gratis biaya (promo). "
        f"Syarat: sudah menyelesaikan 1 transaksi beli/jual/convert.</i>\n\n"
        f"{E_SPARKLES()} <i>Saldo IDR dapat digunakan untuk membeli koin crypto secara instan (1-Tap) tanpa perlu transfer bank!</i>"
    )

    keyboard = [
        [InlineKeyboardButton("Topup Saldo (QRIS)", callback_data="start_topup_qris", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("PLUS", "5204256218100547827"))],
        [withdraw_button_for(balance)],
        [
            InlineKeyboardButton("👛 Alamat Wallet", callback_data="menu_saved_wallets"),
            InlineKeyboardButton("🏦 Rekening Pencairan", callback_data="menu_saved_banks"),
        ],
        [InlineKeyboardButton("Kembali ke Menu", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
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
    if await block_if_busy("topup", update, context):
        return None
    query = update.callback_query  # None saat dipanggil via /topup
    if query:
        await query.answer()

    text = (
        f"{E_MONEY()} <b>TOPUP SALDO BOT (QRIS)</b>\n\n"
        "Silakan pilih nominal deposit saldo di bawah ini:\n"
        "<i>Semua pembayaran QRIS via GoPay, OVO, Dana, ShopeePay, BCA, Mandiri, dll.</i>"
    )

    keyboard = [
        [
            InlineKeyboardButton("Rp 5.000", callback_data="topup_nom_5000", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("MONEY_BAG", "5350452584119279096")),
            InlineKeyboardButton("Rp 10.000", callback_data="topup_nom_10000", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("MONEY_BAG", "5350452584119279096")),
        ],
        [
            InlineKeyboardButton("Rp 25.000", callback_data="topup_nom_25000", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("MONEY_BAG", "5350452584119279096")),
            InlineKeyboardButton("Rp 50.000", callback_data="topup_nom_50000", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("MONEY_BAG", "5350452584119279096")),
        ],
        [
            InlineKeyboardButton("Rp 100.000", callback_data="topup_nom_100000", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("MONEY_BAG", "5350452584119279096")),
            InlineKeyboardButton("Custom Nominal", callback_data="topup_nom_custom", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("HISTORY", "5373251851074415873")),
        ],
        [InlineKeyboardButton("Batal", callback_data="cancel_topup", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
        [get_owner_button()]
    ]

    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Batal", callback_data="cancel_topup", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]])
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
    # Nominal ketik sendiri berupa pesan teks, jadi tidak tertangkap gerbang pause (yang memeriksa
    # tombol/perintah); cek di sini agar invoice baru tidak dibuat selama maintenance.
    if user.id not in settings.ADMIN_CHAT_IDS:
        from services.system_pause import active_pause, block_text
        info = active_pause()
        if info is not None:
            target = update.callback_query.message if update.callback_query else update.message
            await target.reply_text(block_text(info), parse_mode="HTML")
            return ConversationHandler.END
    status_msg = None
    if update.callback_query:
        status_msg = await update.callback_query.edit_message_text("⏳ <i>Menyiapkan invoice pembayaran...</i>", parse_mode="HTML")
    else:
        status_msg = await update.message.reply_text("⏳ <i>Menyiapkan invoice pembayaran...</i>", parse_mode="HTML")

    now = datetime.utcnow()
    topup_id = f"TOPUP-{int(now.timestamp())}-{secrets.token_hex(3).upper()}"
    expires_at = now + timedelta(minutes=settings.ORDER_EXPIRE_MINUTES)

    db = SessionLocal()
    try:
        # Satu topup PENDING per user: invoice beruntun menghabiskan kode unik
        # sehingga nominal tagihan bisa kembar dengan tagihan user lain.
        active = db.query(TopupOrder).filter(
            TopupOrder.telegram_id == user.id,
            TopupOrder.status == "PENDING",
            (TopupOrder.expires_at.is_(None)) | (TopupOrder.expires_at > now),
        ).first()
        if active:
            await status_msg.edit_text(
                f"⚠️ Anda masih punya invoice topup aktif <code>{_esc(active.topup_id)}</code> "
                f"sebesar <b>{format_idr(active.amount_idr)}</b>.\n\n"
                "Selesaikan pembayarannya atau batalkan dulu sebelum membuat topup baru.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Saya Sudah Transfer", callback_data=f"check_topup_{active.topup_id}")],
                    [InlineKeyboardButton("Batalkan Topup", callback_data=f"cancel_topup_{active.topup_id}")],
                ]),
            )
            return ConversationHandler.END

        from services.fee_service import qris_max_nominal
        if amount > qris_max_nominal():
            await status_msg.edit_text(
                f"❌ Nominal topup QRIS maksimal <b>{format_idr(qris_max_nominal())}</b> "
                "(total + pajak QRIS + kode unik tidak boleh melewati batas QRIS Rp 10.000.000). "
                "Untuk nominal lebih besar, bagi menjadi beberapa topup.",
                parse_mode="HTML",
            )
            return ConversationHandler.END
        mdr_idr = calculate_qris_mdr(amount)
        unique_code = generate_unique_payment_code(db, base_amount=amount + mdr_idr)
        if unique_code is None:
            await status_msg.edit_text(
                "⏳ Antrean pembayaran QRIS sedang penuh. Silakan coba lagi beberapa menit lagi "
                "atau gunakan nominal lain.",
                parse_mode="HTML",
            )
            return ConversationHandler.END
        final_amount = amount + mdr_idr + unique_code
        topup_order = create_topup_order(
            db=db,
            topup_id=topup_id,
            telegram_id=user.id,
            amount_idr=final_amount,
            expires_at=expires_at
        )
        topup_order.unique_code = unique_code
        topup_order.mdr_idr = mdr_idr
        db.commit()
    finally:
        db.close()

    context.user_data["active_topup_id"] = topup_id

    mdr_line = f"\n🧾 <b>Pajak QRIS 0,3%</b>: +{format_idr(mdr_idr)}" if mdr_idr else ""
    caption_text = (
        f"{E_MONEY()} <b>INVOICE TOPUP SALDO BOT (QRIS)</b>\n\n"
        f"🎫 <b>ID Topup</b>: <code>{topup_id}</code>\n"
        f"{E_DOLLAR()} <b>Total Bayar</b>: <b>{format_idr(final_amount)}</b>"
        f"{mdr_line}\n"
        f"⏰ <b>Batas Waktu</b>: {settings.ORDER_EXPIRE_MINUTES} Menit\n\n"
        f"📌 <b>Cara Bayar:</b>\n"
        f"1. Scan QRIS di atas dengan <b>GoPay, OVO, DANA, ShopeePay, BCA, atau Mobile Banking</b>.\n"
        f"2. Nominal <b>{format_idr(final_amount)}</b> akan muncul otomatis (QRIS Dinamis).\n"
        f"3. Selesaikan pembayaran di aplikasi e-wallet / bank Anda.\n"
        f"4. Saldo akun bot Anda akan <b>otomatis bertambah</b> seketika setelah pembayaran terdeteksi!\n\n"
        f"ℹ️ <i><b>Catatan:</b> Pastikan nominal pembayaran sesuai presisi ({format_idr(final_amount)}) agar saldo masuk otomatis tanpa delay.</i>"
    )

    keyboard = [
        [InlineKeyboardButton("Saya Sudah Transfer", callback_data=f"check_topup_{topup_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CHECK", "5237699328843200968"))],
        [InlineKeyboardButton("Batalkan Topup", callback_data=f"cancel_topup_{topup_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
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
    query = update.callback_query  # dijawab di tiap jalur di bawah (toast / pop-up), bukan di awal

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
            paid = await gopay_service.confirm_payment(
                db, amount=topup.amount_idr, ref_id=topup.topup_id,
                kind="topup", created_at=topup.created_at,
            )
        except Exception as gw_err:
            logger.warning(f"GoPay Gateway error for {topup_id}: {gw_err}")
            paid = False

        if paid:
            from database.crud import claim_and_credit_topup
            settled = claim_and_credit_topup(db, topup.topup_id)
            if settled is None:
                await query.answer("ℹ️ Topup ini sudah diproses sistem.", show_alert=True)
                return
            try:
                await query.answer("✅ Pembayaran diterima! Menambahkan saldo...", show_alert=False)
            except Exception:
                pass
            # Pajak QRIS tidak masuk saldo (merchant yang menanggung ke GoPay).
            topup_mdr = int(topup.mdr_idr or 0)
            net_amt = topup.amount_idr - topup_mdr

            if str(topup.topup_id).startswith("TREASURY-") or str(topup.topup_id).startswith("TOPUP-TREASURY-"):
                from bot.utils.emojis import tg_emoji
                new_treasury_bal = settled[2]
                success_text = (
                    f"{tg_emoji('BANK', '🏦')} ✅ <b>PEMBAYARAN QRIS KAS BOT TERVERIFIKASI!</b>\n\n"
                    f"🎉 Top up kas bot sebesar <b>{format_idr(net_amt)}</b> telah berhasil masuk!\n"
                    f"💰 <b>Total Saldo Kas Bot Sekarang:</b> <b>{format_idr(new_treasury_bal)}</b>\n\n"
                    f"<i>Saldo siap digunakan untuk alokasi campaign, giveaway, dan reward loyalitas.</i>"
                )
                keyboard = [
                    [InlineKeyboardButton("🎁 Buka Wizard Campaign", callback_data="admin_panel_campaign")],
                    [InlineKeyboardButton("🏦 Dompet & Kas Bot", callback_data="camp_treasury_view")],
                    [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
                ]
                await query.message.reply_text(success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
            else:
                new_bal = settled[2]
                mdr_credit_line = f"\n🧾 Pajak QRIS 0,3%: -{format_idr(topup_mdr)}" if topup_mdr else ""

                success_text = (
                    f"✅ <b>PEMBAYARAN QRIS TERVERIFIKASI!</b>\n\n"
                    f"🎉 Topup saldo sebesar <b>{format_idr(net_amt)}</b> telah berhasil masuk!"
                    f"{mdr_credit_line}\n"
                    f"💳 <b>Total Saldo Bot Anda Saat Ini</b>: <b>{format_idr(int(new_bal))}</b>\n\n"
                    f"<i>Terima kasih! Anda dapat langsung menggunakan saldo ini untuk membeli crypto secara instan.</i>"
                )
                keyboard = [[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]]
                await query.message.reply_text(success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            # Belum terdeteksi (delay sync 10-30 dtk): perbarui status di pesan tagihan yang SAMA,
            # bukan kirim pesan baru tiap klik (terlihat seperti spam).
            from bot.utils.telegram_utils import refresh_message_status, relabel_recheck_button, wib_clock
            stamp = wib_clock()
            status = (
                f"⏳ <b>Status:</b> pembayaran belum terdeteksi · dicek {stamp} WIB\n"
                f"<i>Topup berjalan otomatis; mutasi QRIS butuh 10-30 detik. Tekan Cek Ulang lagi, "
                f"atau kirim foto bukti transfer ke chat ini bila nominalnya berbeda.</i>"
            )
            updated = await refresh_message_status(
                query, status, relabel_recheck_button(query.message.reply_markup, "check_topup_"))
            if updated:
                await query.answer("⏳ Belum terdeteksi")
            else:
                await query.answer(
                    f"⏳ Pembayaran belum terdeteksi (dicek {stamp} WIB). Mutasi QRIS butuh 10-30 detik, "
                    f"coba lagi sebentar lagi.", show_alert=True)
    except Exception as exc:
        logger.error("Error check_topup_payment_manual %s: %s", topup_id, exc, exc_info=True)
        try:
            await query.answer("❌ Terjadi kesalahan saat memeriksa pembayaran. Coba lagi.", show_alert=True)
        except Exception:
            pass
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
            await file.download_to_drive(f"proofs/topup_{topup.topup_id}.jpg")
        except Exception as exc:
            pass

        # 1. Forward foto bukti topup ke Admin
        from bot.utils.admin_alert import user_label
        admin_caption = (
            f"📸 <b>BUKTI TRANSFER TOPUP SALDO BOT DARI USER</b>\n\n"
            f"ID Topup: <code>{topup.topup_id}</code>\n"
            f"User: {user_label(user_id, user=update.effective_user)}\n"
            f"Total Nominal: <b>{format_idr(topup.amount_idr)}</b>\n\n"
            f"Cek mutasi GoPay: dana sudah masuk? Bila ya, tekan <b>Approve</b> untuk menambah saldo "
            f"user. Bila tidak ada, tekan <b>Tolak</b>."
        )
        admin_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve Topup Saldo", callback_data=f"admin_approve_topup_{topup.topup_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CHECK", "5237699328843200968")),
                InlineKeyboardButton("Tolak", callback_data=f"admin_reject_topup_{topup.topup_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CROSS", "5462882007451185227"))
            ]
        ])
        if photo_file_id:
            from bot.utils.telegram_utils import kirim_ke_topik
            await kirim_ke_topik(context.bot, kind="topup", photo=photo_file_id,
                                 text=admin_caption)
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
            "⏳ <b>Bukti transfer telah diterima!</b>\n\n"
            "Admin telah menerima bukti transfer Anda dan sedang memverifikasinya. "
            "Saldo IDR akan otomatis bertambah ke akun Anda.",
            parse_mode="HTML"
        )

        # 3. Cek otomatis via API GoPay di background
        try:
            if await gopay_service.confirm_payment(
                db, amount=topup.amount_idr, ref_id=topup.topup_id,
                kind="topup", created_at=topup.created_at,
            ):
                from database.crud import claim_and_credit_topup
                # Net (tanpa pajak QRIS) & TREASURY ke kas bot — sama dengan jalur otomatis.
                settled = claim_and_credit_topup(db, topup.topup_id)
                if settled is not None:
                    is_treasury, net_amt, new_bal = settled
                    label = "Kas Bot" if is_treasury else "Saldo Bot Anda"
                    menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]])
                    await update.message.reply_text(
                        f"✅ <b>PEMBAYARAN QRIS TERVERIFIKASI (OTOMATIS)!</b>\n\n"
                        f"🎉 Topup sebesar <b>{format_idr(net_amt)}</b> telah berhasil!\n"
                        f"💳 <b>Total {label} Saat Ini</b>: <b>{format_idr(int(new_bal))}</b>\n\n"
                        f"<i>Anda dapat langsung menggunakan saldo ini untuk membeli koin crypto secara instan.</i>",
                        reply_markup=menu_keyboard,
                        parse_mode="HTML"
                    )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error handle_topup_transfer_proof user {user_id}: {e}", exc_info=True)
    finally:
        db.close()


async def cancel_topup_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Membatalkan invoice topup."""
    query = update.callback_query

    topup_id = query.data.replace("cancel_topup_", "")
    db = SessionLocal()
    try:
        topup = get_topup_order_by_id(db, topup_id)
        if not topup:
            await query.answer("❌ Data topup tidak ditemukan.", show_alert=True)
            return
        from bot.handlers.admin import is_admin
        is_adm = is_admin(query.from_user.id)
        is_treasury = topup_id.startswith("TREASURY-") or topup_id.startswith("TOPUP-TREASURY-")
        if topup.telegram_id != query.from_user.id and not (is_adm and is_treasury):
            await query.answer("❌ Topup ini bukan milik Anda.", show_alert=True)
            return
        if (topup.status or "").upper() != "PENDING":
            await query.answer(f"⚠️ Topup sudah {topup.status.lower()} dan tidak bisa dibatalkan.", show_alert=True)
            return
        update_topup_status(db, topup_id, "CANCELLED")
    finally:
        db.close()

    await query.answer()
    back_button = (
        InlineKeyboardButton("🏦 Kembali ke Kas Bot", callback_data="camp_treasury_view")
        if is_treasury
        else InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
    )
    cancel_text = f"❌ <b>Invoice Topup {topup_id} telah dibatalkan.</b>"
    markup = InlineKeyboardMarkup([[back_button]])
    try:
        await query.edit_message_caption(
            caption=cancel_text,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception:
        try:
            await query.edit_message_text(
                text=cancel_text,
                reply_markup=markup,
                parse_mode="HTML"
            )
        except Exception:
            await query.message.reply_text(
                text=cancel_text,
                reply_markup=markup,
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
        CallbackQueryHandler(start_topup_callback, pattern="^start_topup_qris$"),
        CommandHandler("topup", start_topup_callback),
    ],
    states={
        SELECT_TOPUP_NOMINAL: [
            CallbackQueryHandler(handle_preset_nominal, pattern="^topup_nom_"),
            CallbackQueryHandler(cancel_topup_flow, pattern="^cancel_topup$"),
            CallbackQueryHandler(cancel_topup_flow, pattern="^menu_back$"),
        ],
        WAITING_CUSTOM_NOMINAL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_nominal_input),
            CallbackQueryHandler(cancel_topup_flow, pattern="^cancel_topup$"),
            CallbackQueryHandler(cancel_topup_flow, pattern="^menu_back$"),
        ]
    },
    fallbacks=[
        CallbackQueryHandler(cancel_topup_flow, pattern="^cancel_topup$"),
        CallbackQueryHandler(cancel_topup_flow, pattern="^menu_back$"),
        CommandHandler("cancel", cancel_topup_flow),
        CommandHandler("start", cancel_topup_flow),
    ],
    allow_reentry=True
)

