"""
bot/handlers/buy.py — Handler Alur Pembelian (Buy Flow).
======================================================
Mengelola percakapan multi-langkah (ConversationHandler) untuk pembelian crypto:
1. Pilih koin crypto & network
2. Input nominal Rupiah (min Rp 10.000)
3. Input alamat wallet penerima koin (sesuai network)
4. Pilih metode pembayaran Tripay (QRIS / VA)
5. Konfirmasi order & generate invoice Tripay
"""

import asyncio
import logging
import os
from weakref import WeakValueDictionary
from datetime import datetime, timedelta
from decimal import Decimal
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from database.connection import SessionLocal
from database.models import Order
from database.crud import (
    create_order,
    get_user_balance,
    deduct_user_balance,
    get_order_by_id,
    get_pending_gopay_order_for_user,
    update_order_status,
    generate_unique_payment_code,
    claim_order_paid,
    claim_order_payout_processing,
    claim_stale_payout_processing,
    get_available_inventory,
    reserve_order_inventory,
    release_order_inventory,
)
from services.price_service import price_service
from services.fee_service import calculate_fee_idr, get_fee_category
from services.gopay_service import gopay_service
from bot.keyboards.crypto_select import (
    get_buy_symbol_keyboard,
    get_buy_network_keyboard,
)
from bot.keyboards.main_menu import get_owner_button
from bot.utils.validator import validate_amount_idr, validate_wallet_address
from bot.utils.formatter import format_idr, format_crypto, generate_order_id
from bot.utils.messages import ORDER_SUMMARY_BUY
from bot.utils.telegram_utils import safe_edit_message, safe_send_message, notify_admins
from config.assets import QRIS_STATIC_IMAGE
from config.settings import settings

logger = logging.getLogger(__name__)
_finalize_locks = WeakValueDictionary()

# State percakapan
SELECT_SYMBOL = 1
SELECT_NETWORK = 2
INPUT_AMOUNT = 3
INPUT_WALLET = 4
SELECT_PAYMENT = 5
CONFIRM_ORDER = 6

# Label tampilan metode pembayaran
PAYMENT_METHOD_LABELS = {
    "BOT_BALANCE": "Saldo Bot (Instan)",
    "GOPAY_QRIS": "QRIS GoPay (All E-Wallet & Bank)",
}

async def start_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point alur Beli dari klik tombol menu utama.
    """
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        text=(
            "🛒 <b>BELI CRYPTOCURRENCY</b>\n\n"
            "Silakan pilih aset koin crypto yang ingin Anda beli di bawah ini:"
        ),
        reply_markup=get_buy_symbol_keyboard(),
        parse_mode="HTML"
    )
    return SELECT_SYMBOL


async def start_buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point alur Beli dari ketik command /buy.
    """
    await update.message.reply_text(
        text=(
            "🛒 <b>BELI CRYPTOCURRENCY</b>\n\n"
            "Silakan pilih aset koin crypto yang ingin Anda beli di bawah ini:"
        ),
        reply_markup=get_buy_symbol_keyboard(),
        parse_mode="HTML"
    )
    return SELECT_SYMBOL


async def handle_symbol_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Tahap 1 Beli: Menyimpan simbol koin yang dipilih, lalu menampilkan pilihan jaringan.
    """
    query = update.callback_query
    await query.answer()
    
    # Callback format: buy_sym_{SYMBOL} (e.g. buy_sym_USDT)
    symbol = query.data.split("_")[2]
    context.user_data["buy_symbol"] = symbol
    
    await query.edit_message_text(
        text=(
            f"🛒 Anda memilih koin: <b>{symbol}</b>\n\n"
            f"Silakan pilih jaringan (network) yang ingin Anda gunakan:"
        ),
        reply_markup=get_buy_network_keyboard(symbol),
        parse_mode="HTML"
    )
    return SELECT_NETWORK


async def handle_network_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Tahap 2 Beli: Menyimpan jaringan yang dipilih, lalu meminta input nominal Rupiah.
    """
    query = update.callback_query
    await query.answer()
    
    # Callback format: buy_net_{SYMBOL}_{NETWORK} (e.g. buy_net_USDT_BSC)
    parts = query.data.split("_")
    symbol = parts[2]
    network = parts[3]
    
    context.user_data["buy_symbol"] = symbol
    context.user_data["buy_network"] = network
    
    keyboard = [
        [InlineKeyboardButton("🔙 Batal", callback_data="buy_cancel")],
        [get_owner_button()]
    ]
    
    await query.edit_message_text(
        text=(
            f"🛒 Anda memilih: <b>{symbol} ({network})</b>\n\n"
            f"Berapa nominal Rupiah (IDR) koin yang ingin Anda beli?\n"
            f"<i>Ketik nominal langsung di chat (contoh: 50000 atau Rp 50.000).</i>\n\n"
            f"⚠️ Batas minimal pembelian adalah <b>Rp 5.000</b>."
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return INPUT_AMOUNT


async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Memproses nominal rupiah, menghitung rate & fee, lalu meminta wallet address.
    """
    text_input = (update.message.text or "").strip()
    
    keyboard = [
        [InlineKeyboardButton("🔙 Batal", callback_data="buy_cancel")],
        [get_owner_button()]
    ]

    # 1. Cek jika user keliru menginput alamat wallet
    if text_input.lower().startswith("0x") or (len(text_input) >= 32 and not text_input.isdigit()):
        await update.message.reply_text(
            text=(
                "⚠️ <b>Input Terdeteksi Sebagai Alamat Wallet!</b>\n\n"
                "Pada langkah ini, silakan masukkan <b>Nominal Rupiah (IDR)</b> yang ingin Anda beli, bukan alamat wallet.\n"
                "Alamat wallet Anda akan diminta pada langkah selanjutnya.\n\n"
                "Silakan ketik nominal Rupiah (contoh: <code>50000</code> atau <code>Rp 50.000</code>):"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return INPUT_AMOUNT

    # 2. Validasi nominal IDR
    is_valid, nominal_idr = validate_amount_idr(text_input)
    if not is_valid:
        if nominal_idr > 0 and nominal_idr < 5000:
            err_msg = "Nominal kurang dari batas minimal <b>Rp 5.000</b>."
        elif nominal_idr > 10_000_000:
            err_msg = "Nominal melebihi batas maksimal <b>Rp 10.000.000</b> (limit transaksi QRIS BI)."
        else:
            err_msg = "Format input salah atau mengandung karakter yang tidak valid."

        await update.message.reply_text(
            text=(
                f"❌ <b>Nominal Tidak Valid!</b>\n\n"
                f"{err_msg}\n"
                "Silakan ketik ulang nominal Rupiah (contoh: <code>50000</code> atau <code>50k</code>):"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return INPUT_AMOUNT

    symbol = context.user_data["buy_symbol"]
    network = context.user_data["buy_network"]
    
    db = SessionLocal()
    try:
        # Fetch harga buy terkini dari price service
        price_data = await price_service.get_price(symbol, db)
        if not price_data:
            raise ValueError(f"Harga {symbol} tidak ditemukan")
            
        # Hitung fee dinamis (termasuk tambahan fee gas Rp 2.000 untuk ETH/TRX jika berlaku)
        fee_category = get_fee_category(symbol)
        fee_idr = calculate_fee_idr(nominal_idr, category=fee_category, symbol=symbol, network=network)

        # Revisi skema fee: nominal pembelian DIKURANGI fee (bukan ditambah ke total bayar).
        # Contoh: beli Rp 10.000 -> fee Rp 3.000 -> nilai koin diterima Rp 7.000.
        if fee_idr >= nominal_idr:
            await update.message.reply_text(
                text=(
                    "❌ <b>Nominal Terlalu Kecil!</b>\n\n"
                    f"Setelah potongan fee <b>{format_idr(fee_idr)}</b>, tidak ada nilai koin tersisa.\n"
                    f"Minimal nominal yang bisa diproses: <b>{format_idr(fee_idr + 1)}</b>.\n"
                    "Silakan ketik ulang nominal yang lebih besar:"
                ),
                parse_mode="HTML"
            )
            db.close()
            return INPUT_AMOUNT

    except ValueError as val_err:
        await update.message.reply_text(f"⚠️ {str(val_err)}")
        db.close()
        return INPUT_AMOUNT
    except Exception as e:
        logger.error(f"Gagal memproses nominal untuk {symbol}: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Terjadi kesalahan saat mengambil rate harga. Silakan coba sesaat lagi.")
        db.close()
        return ConversationHandler.END
    finally:
        db.close()

    buy_price_idr = price_data["buy_price_idr"]
    
    # Hitung jumlah crypto yang didapatkan ((Nominal - Fee) / Kurs Beli)
    received_idr = nominal_idr - fee_idr
    crypto_amount = received_idr / buy_price_idr
    
    # Simpan rincian perhitungan ke context
    context.user_data["buy_nominal_idr"] = nominal_idr
    context.user_data["buy_fee_idr"] = fee_idr
    context.user_data["buy_received_idr"] = received_idr
    context.user_data["buy_total_idr"] = nominal_idr
    context.user_data["buy_price_per_unit"] = buy_price_idr
    context.user_data["buy_crypto_amount"] = crypto_amount
    
    keyboard = [
        [InlineKeyboardButton("🔙 Batal", callback_data="buy_cancel")],
        [get_owner_button()]
    ]

    await update.message.reply_text(
        text=(
            f"🪙 <b>Simulasi Perhitungan Pembelian:</b>\n"
            f"• Aset: <code>{format_crypto(crypto_amount, symbol)}</code>\n"
            f"• Kurs Beli: <code>{format_idr(buy_price_idr)}</code>\n"
            f"• Nominal Bayar: <code>{format_idr(nominal_idr)}</code>\n"
            f"• Fee Layanan (dipotong): <code>-{format_idr(fee_idr)}</code>\n"
            f"• Nilai Koin Diterima: <b>{format_idr(received_idr)}</b>\n\n"
            f"Silakan ketik <b>Alamat Wallet {symbol} ({network})</b> Anda penerima koin:\n"
            f"<i>⚠️ Pastikan Anda mengirimkan alamat wallet yang benar di network {network}!</i>"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return INPUT_WALLET


async def handle_wallet_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Memvalidasi wallet address, lalu meminta memilih metode pembayaran.
    """
    wallet_address = update.message.text.strip()
    network = context.user_data["buy_network"]
    symbol = context.user_data["buy_symbol"]
    
    # Validasi alamat wallet per network
    if not validate_wallet_address(wallet_address, network):
        keyboard = [
            [InlineKeyboardButton("🔙 Batal", callback_data="buy_cancel")],
            [get_owner_button()]
        ]
        await update.message.reply_text(
            text=(
                f"❌ <b>Alamat Wallet Tidak Valid!</b>\n\n"
                f"Alamat yang Anda kirim tidak cocok dengan format network <b>{network}</b>.\n"
                f"Silakan kirimkan alamat wallet {symbol} ({network}) yang valid:"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return INPUT_WALLET

    # Simpan wallet ke context
    context.user_data["buy_wallet"] = wallet_address
    
    total_idr = context.user_data.get("buy_total_idr", 0)
    user_id = update.effective_user.id
    
    db = SessionLocal()
    try:
        user_balance = get_user_balance(db, user_id)
        available_inventory = get_available_inventory(db, network, symbol)
        if available_inventory is None or available_inventory < Decimal(str(context.user_data["buy_crypto_amount"])):
            available_text = (
                format_crypto(float(available_inventory), symbol)
                if available_inventory is not None
                else "belum tersedia"
            )
            await update.message.reply_text(
                f"⚠️ <b>Stok {symbol} ({network}) belum mencukupi.</b>\n\n"
                f"Stok tersedia: <code>{available_text}</code>\n"
                "Silakan hubungi admin untuk proses manual.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")
                ]]),
                parse_mode="HTML",
            )
            return ConversationHandler.END
    finally:
        db.close()

    keyboard = []
    if user_balance >= total_idr:
        keyboard.append([
            InlineKeyboardButton(f"⚡ 💰 Saldo Bot ({format_idr(int(user_balance))}) — Instan", callback_data="paymethod_BOT_BALANCE")
        ])

    keyboard.extend([
        [InlineKeyboardButton("📱 QRIS GoPay (All E-Wallet & Bank)", callback_data="paymethod_GOPAY_QRIS")],
        [InlineKeyboardButton("🔙 Batal", callback_data="buy_cancel")],
        [get_owner_button()]
    ])
    
    await update.message.reply_text(
        text=(
            "💳 <b>PILIH METODE PEMBAYARAN</b>\n\n"
            f"Total Pembayaran: <b>{format_idr(total_idr)}</b>\n"
            f"Saldo IDR Anda: <b>{format_idr(int(user_balance))}</b>\n\n"
            "Silakan pilih metode pembayaran di bawah ini:"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return SELECT_PAYMENT


async def handle_payment_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Menyimpan pilihan payment method, lalu menyajikan konfirmasi final.
    """
    query = update.callback_query
    await query.answer()
    
    # format callback: paymethod_{CODE} (e.g. paymethod_GOPAY_QRIS)
    method_code = query.data.split("paymethod_", 1)[1] if "paymethod_" in query.data else query.data.split("_")[1]
    context.user_data["buy_pay_method"] = method_code
    
    # Ambil detail transaksi untuk summary
    order_id = generate_order_id()
    context.user_data["buy_order_id"] = order_id
    
    crypto_amount = context.user_data["buy_crypto_amount"]
    symbol = context.user_data["buy_symbol"]
    network = context.user_data["buy_network"]
    price_per_unit = context.user_data["buy_price_per_unit"]
    nominal_idr = context.user_data["buy_nominal_idr"]
    fee_idr = context.user_data["buy_fee_idr"]
    received_idr = context.user_data["buy_received_idr"]
    total_idr = context.user_data["buy_total_idr"]
    buyer_wallet = context.user_data["buy_wallet"]
    
    summary_text = ORDER_SUMMARY_BUY.format(
        order_id=order_id,
        crypto_amount_str=format_crypto(crypto_amount, symbol),
        network=network,
        price_per_unit_str=format_idr(price_per_unit),
        nominal_idr_str=format_idr(nominal_idr),
        fee_idr_str=format_idr(fee_idr),
        received_idr_str=format_idr(received_idr),
        buyer_wallet=buyer_wallet
    )
    
    # Tambahkan baris informasi metode pembayaran
    method_label = PAYMENT_METHOD_LABELS.get(method_code, method_code)
    summary_text += f"\n💳 <b>Metode Pembayaran:</b> {method_label}"
    if method_code == "GOPAY_QRIS":
        summary_text += (
            "\nℹ️ <i>Kode unik (1-999) akan ditambahkan ke total bayar "
            "untuk verifikasi otomatis.</i>"
        )
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Konfirmasi & Bayar", callback_data="buy_confirm"),
            InlineKeyboardButton("❌ Batal", callback_data="buy_cancel")
        ],
        [get_owner_button()]
    ]
    
    await query.edit_message_text(
        text=summary_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return CONFIRM_ORDER


async def handle_order_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Memverifikasi rate limit, melakukan request invoice ke Tripay, menyimpan ke DB, dan mengirim link bayar.
    """
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    # --- 1. Rate Limiting Check (Max 5 orders per 10 minutes) ---
    db = SessionLocal()
    try:
        ten_minutes_ago = datetime.utcnow() - timedelta(minutes=10)
        recent_orders_count = (
            db.query(Order)
            .filter(
                Order.telegram_id == user_id,
                Order.created_at >= ten_minutes_ago
            )
            .count()
        )
        
        if recent_orders_count >= 5:
            await query.edit_message_text(
                text=(
                    "⚠️ <b>Batas Limit Transaksi Tercapai!</b>\n\n"
                    "Anda telah membuat terlalu banyak pesanan dalam 10 menit terakhir.\n"
                    "Silakan tunggu beberapa saat atau hubungi owner untuk bantuan."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data="menu_back")
                ]]),
                parse_mode="HTML"
            )
            return ConversationHandler.END

        # Ambil data order dari context
        order_id = context.user_data["buy_order_id"]
        symbol = context.user_data["buy_symbol"]
        network = context.user_data["buy_network"]
        crypto_amount = context.user_data["buy_crypto_amount"]
        price_per_unit = context.user_data["buy_price_per_unit"]
        nominal_idr = context.user_data["buy_nominal_idr"]
        fee_idr = context.user_data["buy_fee_idr"]
        total_idr = context.user_data["buy_total_idr"]
        buyer_wallet = context.user_data["buy_wallet"]
        method_code = context.user_data["buy_pay_method"]
        
        # --- 2. Handle Payment Method ---
        if method_code == "BOT_BALANCE":
            # Potong saldo IDR user di DB
            deduct_success = deduct_user_balance(db, user_id, float(total_idr))
            if not deduct_success:
                await query.edit_message_text(
                    text="❌ <b>Saldo Bot Tidak Mencukupi!</b>\n\nSilakan topup saldo bot Anda terlebih dahulu.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")]]),
                    parse_mode="HTML"
                )
                return ConversationHandler.END

            # Buat order langsung sukses bayar IDR
            order_data = {
                "order_id": order_id,
                "telegram_id": user_id,
                "order_type": "buy",
                "crypto_symbol": symbol,
                "network": network,
                "crypto_amount": Decimal(str(crypto_amount)),
                "price_per_unit": int(price_per_unit),
                "nominal_idr": int(nominal_idr),
                "fee_idr": int(fee_idr),
                "total_idr": int(total_idr),
                "buyer_wallet": buyer_wallet,
                "payment_method": "BOT_BALANCE",
                "status": "pending",
                "paid_at": datetime.utcnow(),
                "quoted_at": datetime.utcnow(),
                "quote_expires_at": datetime.utcnow() + timedelta(minutes=30)
            }

            order = create_order(db, order_data)

            # Kirim notifikasi sukses ke user
            received_idr = context.user_data["buy_received_idr"]
            success_msg = (
                f"🎉 <b>PEMBELIAN BERHASIL (SALDO BOT)!</b>\n\n"
                f"📝 <b>ID Order:</b> <code>{order_id}</code>\n"
                f"🪙 <b>Aset:</b> {format_crypto(crypto_amount, symbol)} ({network})\n"
                f"💵 <b>Nominal Bayar:</b> {format_idr(total_idr)} (Saldo Bot)\n"
                f"🔌 <b>Fee Layanan (dipotong):</b> -{format_idr(fee_idr)}\n"
                f"💰 <b>Nilai Koin Diterima:</b> {format_idr(received_idr)}\n"
                f"📍 <b>Wallet Tujuan:</b> <code>{buyer_wallet}</code>\n\n"
                f"✅ Pembayaran menggunakan Saldo Bot lunas! Koin crypto sedang diproses untuk dikirimkan ke wallet Anda."
            )
            await query.edit_message_text(
                text=success_msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")]]),
                parse_mode="HTML"
            )
            asyncio.create_task(_run_finalize_background(order.order_id, context.bot))
            return ConversationHandler.END

        # --- 2b. GoPay QRIS Payment (QRIS Statis, pembayaran manual) ---
        if method_code == "GOPAY_QRIS":
            unique_code = generate_unique_payment_code(db)
            final_total_idr = int(total_idr) + unique_code

            order_data = {
                "order_id": order_id,
                "telegram_id": user_id,
                "order_type": "buy",
                "crypto_symbol": symbol,
                "network": network,
                "crypto_amount": Decimal(str(crypto_amount)),
                "price_per_unit": int(price_per_unit),
                "nominal_idr": int(nominal_idr),
                "fee_idr": int(fee_idr),
                "unique_code": unique_code,
                "total_idr": final_total_idr,
                "buyer_wallet": buyer_wallet,
                "payment_method": "GOPAY_QRIS",
                "status": "pending",
                "expired_at": datetime.utcnow() + timedelta(minutes=30)
            }
            create_order(db, order_data)

            # Kirim QRIS statis + instruksi pembayaran manual dengan kode unik & disclaimer
            received_idr = context.user_data["buy_received_idr"]
            caption = (
                "💳 <b>BAYAR VIA QRIS</b>\n\n"
                f"🎫 <b>ID Order</b>: <code>{order_id}</code>\n"
                f"💵 <b>Total Bayar</b>: <code>{final_total_idr}</code> ({format_idr(final_total_idr)})\n"
                f"🔌 <b>Fee Layanan (dipotong)</b>: -{format_idr(fee_idr)}\n"
                f"💰 <b>Nilai Koin Diterima</b>: {format_idr(received_idr)}\n"
                f"⏰ <b>Batas Waktu</b>: 30 Menit\n\n"
                f"📌 <b>Cara Bayar:</b>\n"
                f"1. Scan QRIS di atas dengan GoPay, OVO, Dana, ShopeePay, atau Mobile Banking.\n"
                f"2. Ketik/input nominal <b>{format_idr(final_total_idr)}</b> secara manual.\n"
                f"3. Setelah transfer, <b>kirim foto bukti transfer</b> ke chat ini.\n"
                f"4. Koin otomatis dikirim ke wallet Anda setelah transfer terverifikasi.\n\n"
                f"ℹ️ <i><b>Catatan Nominal:</b> Transfer <b>PAS SESUAI NOMINAL PRESISI</b> dan kode unik. Jika nominal berbeda, pembayaran tidak terverifikasi otomatis dan perlu bantuan admin.</i>"
            )
            keyboard = [
                [InlineKeyboardButton("✅ Saya Sudah Transfer", callback_data=f"check_buy_payment_{order_id}")],
                [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")],
                [get_owner_button()]
            ]
            from services.qris_generator import get_qris_image_stream
            qris_stream = get_qris_image_stream(final_total_idr)
            sent = False
            if qris_stream:
                try:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=qris_stream,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    sent = True
                except Exception as pe:
                    logger.warning(f"Gagal upload QRIS photo: {pe}")
            
            if not sent:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            # Notify admins
            admin_alert = (
                f"🔔 <b>ORDER BARU DIBUAT (BUY - GoPay QRIS)</b>\n\n"
                f"Order ID: <code>{order_id}</code>\n"
                f"User: {update.effective_user.name} (ID: {user_id})\n"
                f"Koin: {format_crypto(crypto_amount, symbol)} ({network})\n"
                f"Total Pembayaran: <b>{format_idr(final_total_idr)}</b> (Kode Unik: {unique_code})\n"
                f"Metode: GOPAY_QRIS\n"
                f"Wallet: <code>{buyer_wallet}</code>"
            )
            await notify_admins(context.bot, admin_alert)

            return ConversationHandler.END


        # Metode pembayaran tidak dikenali (harusnya tidak terjadi)
        await safe_edit_message(
            query,
            text=(
                "❌ <b>Metode pembayaran tidak dikenali!</b>\n\n"
                "Silakan mulai ulang alur pembelian."
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")
            ]])
        )
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error saat konfirmasi order buy: {e}", exc_info=True)
        await query.message.reply_text("⚠️ Terjadi kesalahan internal saat memproses pesanan.")
    finally:
        db.close()
        
    return ConversationHandler.END


async def cancel_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Membatalkan alur beli dan kembali ke menu utama.
    """
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass
        # Panggil helper send_main_menu secara langsung
        from bot.handlers.start import send_main_menu
        await send_main_menu(update, context)
    else:
        # Jika via teks command
        await update.message.reply_text(
            text="❌ Sesi pembelian dibatalkan.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")
            ]])
        )
    return ConversationHandler.END


async def finalize_gopay_buy_payment(
    db,
    order,
    bot=None,
    *,
    allow_admin=False,
    allow_recovery=False,
) -> None:
    """
    Finalisasi order Buy (GoPay QRIS / Saldo Bot):
      - Claim atomic pending -> paid (hanya pemenang race yang lanjut payout).
      - Resume order 'paid'/'manual_review' yang payout-nya belum pernah sukses
        (tanpa payout_tx_hash) — recovery crash, admin approve, dll.
      - Auto-send crypto ke wallet buyer (pakai payout_service).
        Sukses -> COMPLETED + notif user (TX hash + explorer).
        Gagal -> MANUAL_REVIEW + notif admin & user.
    """
    from services.bot_runtime import bot_app
    from services.payout_service import send_order_payout

    lock = _finalize_locks.setdefault(order.order_id, asyncio.Lock())
    async with lock:
        # Always refresh inside the lock: callers may hold a stale ORM object.
        db.refresh(order)
        if order.payout_tx_hash or order.status == "completed":
            return

        if order.status == "pending":
            if not claim_order_paid(db, order.order_id):
                return
            db.refresh(order)

        if order.status == "paid":
            if not claim_order_payout_processing(db, order.order_id):
                return
            db.refresh(order)
        elif order.status == "payout_processing":
            if not allow_recovery or not claim_stale_payout_processing(db, order.order_id):
                return
            db.refresh(order)
        elif order.status in ("manual_review", "expired"):
            if not allow_admin:
                return
            if not claim_order_payout_processing(db, order.order_id, (order.status,)):
                return
            db.refresh(order)
        else:
            return

        # 1. Pesan Progres: Pembayaran Diterima & Proses Pengiriman Koin
        if order.order_type == "buy" and not reserve_order_inventory(
            db,
            order.order_id,
            order.network,
            order.crypto_symbol,
            Decimal(str(order.crypto_amount)),
        ):
            result = {
                "success": False,
                "tx_hash": "",
                "explorer_url": "",
                "error_message": (
                    f"Stok {order.crypto_symbol} ({order.network}) tidak mencukupi. "
                    "Silakan proses manual melalui admin."
                ),
            }
        else:
            result = None

        progress_msg = (
            f"✅ <b>Pembayaran Diterima!</b>\n\n"
            f"🔄 <b>Mengirim {format_crypto(float(order.crypto_amount), order.crypto_symbol)} ({order.network}) ke wallet Anda...</b>"
        )
        if result is None:
            await safe_send_message(bot or bot_app, order.telegram_id, progress_msg)
            result = await send_order_payout(order)

        if result["success"]:
            update_order_status(
                db,
                order.order_id,
                new_status="completed",
                tx_hash=result["tx_hash"],
                payout_tx_hash=result["tx_hash"],
                completed_at=datetime.utcnow(),
            )
            release_order_inventory(db, order.order_id)
            user_msg = (
                f"✅ <b>Crypto Terkirim!</b>\n\n"
                f"<b>Order:</b> <code>{order.order_id}</code>\n"
                f"🪙 <b>Jumlah:</b> <code>{format_crypto(float(order.crypto_amount), order.crypto_symbol)} ({order.network})</code>\n"
                f"🏦 <b>Ke:</b> <code>{order.buyer_wallet}</code>\n"
                f"🔗 <b>TX:</b> <code>{result['tx_hash']}</code>\n"
            )
            if result.get("explorer_url"):
                user_msg += f"\n🌐 <a href=\"{result['explorer_url']}\">Lihat di Explorer</a>"
            user_msg += "\n\nTerima kasih! 🙏"
            menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")]])
            await safe_send_message(bot or bot_app, order.telegram_id, user_msg, reply_markup=menu_keyboard)
        else:
            update_order_status(
                db,
                order.order_id,
                new_status="manual_review",
                failure_reason=result["error_message"],
            )
            admin_msg = (
                f"🚨 <b>MANUAL REVIEW REQUIRED (GoPay QRIS)</b>\n\n"
                f"Order: <code>{order.order_id}</code>\n"
                f"User: {order.telegram_id}\n"
                f"Crypto: {order.crypto_amount} {order.crypto_symbol} ({order.network})\n"
                f"Wallet: <code>{order.buyer_wallet}</code>\n"
                f"Error: {result['error_message']}\n\n"
                f"Pembayaran sudah diterima tapi pengiriman crypto gagal. Kirim manual."
            )
            await notify_admins(bot or bot_app, admin_msg)

            user_msg = (
                f"⏳ <b>Pembayaran Diterima</b>\n\n"
                f"Order: <code>{order.order_id}</code>\n"
                f"Pembayaranmu sudah kami terima. Pengiriman crypto sedang diproses oleh admin.\n"
                f"Kami akan mengirim notifikasi setelah selesai. 🙏"
            )
            await safe_send_message(bot or bot_app, order.telegram_id, user_msg)


async def _run_finalize_background(
    order_id: str,
    bot=None,
    *,
    allow_admin=False,
    allow_recovery=False,
) -> None:
    """
    Jalankan finalize payout di background task dengan session DB sendiri.
    Handler callback tidak boleh menunggu payout (retry bisa 50+ detik).
    Job polling 20s tetap jadi backstop: order 'pending'/'paid' akan diproses lagi.
    """
    db = SessionLocal()
    try:
        order = get_order_by_id(db, order_id)
        if order:
            await finalize_gopay_buy_payment(
                db,
                order,
                bot=bot,
                allow_admin=allow_admin,
                allow_recovery=allow_recovery,
            )
    except Exception as exc:
        logger.error("Background finalize order %s gagal: %s", order_id, exc, exc_info=True)
    finally:
        db.close()


async def check_buy_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler tombol '✅ Saya Sudah Transfer' untuk order Beli GoPay QRIS.
    Verifikasi via riwayat transaksi Gopiz (nominal match). Jika terdeteksi
    -> finalisasi otomatis (paid + send crypto).
    """
    query = update.callback_query
    await query.answer()

    order_id = query.data.replace("check_buy_payment_", "")
    db = SessionLocal()
    try:
        order = get_order_by_id(db, order_id)
        if not order:
            await query.answer("❌ Order tidak ditemukan.", show_alert=True)
            return

        if order.status != "pending":
            await query.answer(
                f"ℹ️ Order sudah berstatus {order.status.upper()}.", show_alert=True
            )
            return

        pay_res = await gopay_service.check_payment(int(order.total_idr), order.order_id)
        if not pay_res.get("paid"):
            await query.answer(
                "⏳ Pembayaran belum terdeteksi sesuai nominal tagihan. "
                "Jika transfer berbeda, hubungi admin untuk verifikasi manual.",
                show_alert=True,
            )
            return

        try:
            await query.answer("✅ Pembayaran diterima! Memproses pengiriman koin...", show_alert=False)
        except Exception as ans_err:
            logger.debug("query.answer error: %s", ans_err)

        asyncio.create_task(_run_finalize_background(order.order_id, context.bot))
    except Exception as e:
        logger.error(f"Error check_buy_payment {order_id}: {e}", exc_info=True)
        try:
            await query.answer("❌ Terjadi kesalahan. Coba lagi.", show_alert=True)
        except Exception:
            pass
    finally:
        db.close()


async def handle_transfer_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Menerima foto bukti transfer dari user untuk order Beli GoPay QRIS.
    Simpan foto, kirim notifikasi & tombol approve ke Admin, serta lakukan cek otomatis.
    """
    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        order = get_pending_gopay_order_for_user(db, user_id)
        if not order:
            return

        photo_file_id = None
        # Arsip bukti transfer ke folder proofs/ (untuk audit admin)
        try:
            photo = update.message.photo[-1]
            photo_file_id = photo.file_id
            file = await photo.get_file()
            os.makedirs("proofs", exist_ok=True)
            await file.download_to_drive(f"proofs/{order.order_id}.jpg")
        except Exception as exc:
            logger.warning("Gagal simpan bukti transfer %s: %s", order.order_id, exc)

        # 1. Forward foto bukti ke seluruh Admin dengan tombol Approve & Reject
        admin_caption = (
            f"📸 <b>BUKTI TRANSFER DITERIMA (BUY)</b>\n\n"
            f"ID Order: <code>{order.order_id}</code>\n"
            f"User: {update.effective_user.name} (ID: <code>{user_id}</code>)\n"
            f"Total Nominal: <b>{format_idr(order.total_idr)}</b>\n"
            f"Koin: {format_crypto(float(order.crypto_amount), order.crypto_symbol)} ({order.network})\n"
            f"Wallet Target: <code>{order.buyer_wallet}</code>\n\n"
            f"Tekan tombol <b>Approve</b> di bawah jika pembayaran valid untuk memicu pengiriman crypto otomatis."
        )
        admin_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve & Kirim Crypto", callback_data=f"admin_approve_buy_{order.order_id}"),
                InlineKeyboardButton("❌ Tolak", callback_data=f"admin_reject_buy_{order.order_id}")
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
                    logger.warning(f"Gagal kirim bukti ke admin {admin_id}: {admin_err}")

        # 2. Pesan penenang ke User bahwa bukti telah diterima
        await safe_send_message(
            context.bot, user_id,
            "⏳ <b>Bukti transfer telah diterima & sedang diverifikasi.</b>\n"
            "Transaksi Anda sedang diperiksa oleh sistem/admin. Koin akan otomatis dikirim setelah verifikasi selesai."
        )

        # 3. Cek otomatis via API GoPay
        pay_res = await gopay_service.check_payment(int(order.total_idr), order.order_id)
        if pay_res.get("paid"):
            await finalize_gopay_buy_payment(db, order, bot=context.bot)
        else:
            await safe_send_message(
                context.bot,
                user_id,
                "⚠️ <b>Pembayaran belum terdeteksi sesuai nominal tagihan.</b>\n\n"
                "Jika Anda sudah transfer dengan nominal/kode unik berbeda, "
                "silakan hubungi admin untuk verifikasi manual.",
            )

    except Exception as e:
        logger.error(f"Error handle_transfer_proof user {user_id}: {e}", exc_info=True)
    finally:
        db.close()



# Definisikan ConversationHandler untuk Buy
buy_conversation_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_buy_callback, pattern="^menu_buy$"),
        CommandHandler("buy", start_buy_command)
    ],
    states={
        SELECT_SYMBOL: [
            CallbackQueryHandler(handle_symbol_selection, pattern="^buy_sym_[A-Z0-9]+$"),
            CallbackQueryHandler(cancel_buy, pattern="^menu_back$")
        ],
        SELECT_NETWORK: [
            CallbackQueryHandler(handle_network_selection, pattern="^buy_net_[A-Z0-9]+_[A-Z0-9]+$"),
            CallbackQueryHandler(start_buy_callback, pattern="^buy_back_symbols$"),
            CallbackQueryHandler(cancel_buy, pattern="^menu_back$")
        ],
        INPUT_AMOUNT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input),
            CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$")
        ],
        INPUT_WALLET: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wallet_input),
            CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$")
        ],
        SELECT_PAYMENT: [
            CallbackQueryHandler(handle_payment_selection, pattern="^paymethod_[A-Z0-9_]+$"),
            CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$")
        ],
        CONFIRM_ORDER: [
            CallbackQueryHandler(handle_order_confirmation, pattern="^buy_confirm$"),
            CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$")
        ]
    },
    fallbacks=[
        CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$"),
        CommandHandler("cancel", cancel_buy)
    ]
)
