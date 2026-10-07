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
from html import escape as _esc
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
    create_bot_balance_order,
    get_user_balance,
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
from services.price_service import price_service, quote_source_text
from services.fee_service import calculate_fee_idr, get_fee_category, gas_surcharge_note, calculate_qris_mdr, qris_mdr_note
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
from bot.utils.flow_guard import block_if_busy
from bot.utils.emojis import (
    E_CARD,
    E_DOLLAR,
    E_MONEY,
    E_CHECK,
    E_CART,
    E_SPARKLES,
    CUSTOM_EMOJI_IDS,
)
from config.assets import QRIS_STATIC_IMAGE, MANUAL_PAYOUT_NETWORKS
from config.settings import settings
from bot.utils.messages import WALLET_DUPLICATE_WARNING, WALLET_LOCK_NOTE
from services import quote_guard

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
    if await block_if_busy("buy", update, context):
        return None
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        text=(
            f"{E_CART()} <b>BELI CRYPTOCURRENCY</b>\n\n"
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
    if await block_if_busy("buy", update, context):
        return None
    await update.message.reply_text(
        text=(
            f"{E_CART()} <b>BELI CRYPTOCURRENCY</b>\n\n"
            "Silakan pilih aset koin crypto yang ingin Anda beli di bawah ini:"
        ),
        reply_markup=get_buy_symbol_keyboard(),
        parse_mode="HTML"
    )
    return SELECT_SYMBOL


async def buy_open_saved_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Membuka menu Alamat Wallet langsung dari alur Beli."""
    from bot.handlers.saved_accounts import show_saved_wallets_menu
    await show_saved_wallets_menu(update, context, back_callback="buy_back_to_menu")
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
            f"{E_CART()} Anda memilih koin: <b>{symbol}</b>\n\n"
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
    
    if network.upper() in MANUAL_PAYOUT_NETWORKS:
        keyboard = [
            [InlineKeyboardButton("Kembali (Pilih Koin)", callback_data="buy_back_symbols", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
        await query.edit_message_text(
            text=(
                f"ℹ️ <b>Pengiriman Otomatis Belum Tersedia</b>\n\n"
                f"Pengiriman koin otomatis untuk jaringan <b>{network}</b> saat ini belum tersedia.\n"
                f"Silakan hubungi admin untuk transaksi manual; <b>jangan melakukan pembayaran dahulu</b>."
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return SELECT_NETWORK

    keyboard = [
        [InlineKeyboardButton("Batal", callback_data="buy_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
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
        [InlineKeyboardButton("Batal", callback_data="buy_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
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
            raise ValueError(f"Harga {symbol} belum tersedia, coba lagi sebentar")
            
        # Hitung fee dinamis (termasuk tambahan fee gas Rp 2.000 untuk ETH/TRX jika berlaku)
        fee_category = get_fee_category(symbol)
        base_fee_idr = calculate_fee_idr(nominal_idr, category=fee_category, symbol=symbol, network=network)

        # Phase 7: Cek diskon referral aktif milik user
        user_id = update.effective_user.id
        from database.crud import get_referral_discount_info
        disc_info = get_referral_discount_info(db, user_id)
        discount_applied = False
        discount_pct = 0.0
        discount_amount = 0
        discount_note = ""

        fee_idr = base_fee_idr
        if disc_info.get("active") and base_fee_idr > 0:
            discount_pct = float(disc_info["discount_pct"])
            discount_amount = int(base_fee_idr * discount_pct / 100)
            fee_idr = max(0, base_fee_idr - discount_amount)
            discount_applied = True
            discount_note = (
                f"\n🎁 <b>Diskon Referral:</b> -{format_idr(discount_amount)} "
                f"({discount_pct:.0f}%, sisa {disc_info['remaining']}x transaksi)"
            )

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
            return INPUT_AMOUNT

        buy_price_idr = price_data["buy_price_idr"]
        
        # Hitung jumlah crypto yang didapatkan ((Nominal - Fee) / Kurs Beli)
        received_idr = nominal_idr - fee_idr
        crypto_amount = received_idr / buy_price_idr
        
        # Simpan rincian perhitungan ke context
        context.user_data["buy_nominal_idr"] = nominal_idr
        context.user_data["buy_base_fee_idr"] = base_fee_idr
        context.user_data["buy_fee_idr"] = fee_idr
        context.user_data["buy_discount_applied"] = discount_applied
        context.user_data["buy_discount_pct"] = discount_pct
        context.user_data["buy_discount_amount"] = discount_amount
        context.user_data["buy_received_idr"] = received_idr
        context.user_data["buy_total_idr"] = nominal_idr
        context.user_data["buy_price_per_unit"] = buy_price_idr
        context.user_data["buy_quoted_at"] = quote_guard.stamp()
        context.user_data["buy_crypto_amount"] = crypto_amount

        available_inventory = get_available_inventory(db, network, symbol)
        if available_inventory is not None and available_inventory < Decimal(str(crypto_amount)):
            available_text = format_crypto(float(available_inventory), symbol)
            await update.message.reply_text(
                text=(
                    f"⚠️ <b>Stok {symbol} ({network}) Tidak Mencukupi!</b>\n\n"
                    f"Jumlah yang ingin Anda beli: <code>{format_crypto(crypto_amount, symbol)}</code>\n"
                    f"Stok tersedia saat ini: <code>{available_text}</code>\n\n"
                    "Silakan masukkan nominal Rupiah yang lebih kecil, atau hubungi admin untuk transaksi manual."
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return INPUT_AMOUNT

        quote_info = ""
        try:
            quote_info = f"\nℹ️ <i>{quote_source_text(price_data)}</i>"
        except Exception:
            pass

        # Ambil daftar alamat wallet tersimpan milik user untuk network ini
        user_id = update.effective_user.id
        from database.crud import get_user_saved_wallets
        saved_wallets = get_user_saved_wallets(db, user_id, network=network)

        saved_buttons = []
        for sw in saved_wallets:
            short_addr = f"{sw.wallet_address[:6]}...{sw.wallet_address[-4:]}" if len(sw.wallet_address) > 12 else sw.wallet_address
            lbl = f"👛 Gunakan: {short_addr}"
            if sw.label:
                lbl = f"👛 Gunakan: {sw.label} ({short_addr})"
            saved_buttons.append([InlineKeyboardButton(lbl, callback_data=f"buy_saved_wallet_{sw.id}")])

        input_wallet_keyboard = saved_buttons + keyboard

        fee_display = f"-{format_idr(fee_idr)}"
        if discount_applied:
            fee_display = f"<s>{format_idr(base_fee_idr)}</s> <b>{format_idr(fee_idr)}</b>"

        await update.message.reply_text(
            text=(
                f"🪙 <b>Simulasi Perhitungan Pembelian:</b>\n"
                f"• Aset: <code>{format_crypto(crypto_amount, symbol)}</code>\n"
                f"• Kurs Beli: <code>{format_idr(buy_price_idr)}</code>\n"
                f"• Nominal Bayar: <code>{format_idr(nominal_idr)}</code>\n"
                f"• Fee Layanan (dipotong): {fee_display}"
                f"{discount_note}"
                f"{quote_info}"
                f"{gas_surcharge_note(symbol, network)}"
                f"{qris_mdr_note(nominal_idr)}\n\n"
                f"• Nilai Koin Diterima: <b>{format_idr(received_idr)}</b>\n\n"
                f"Silakan ketik <b>Alamat Wallet {symbol} ({network})</b> Anda penerima koin:\n"
                f"<i>⚠️ Pastikan Anda mengirimkan alamat wallet yang benar di network {network}!</i>"
            ),
            reply_markup=InlineKeyboardMarkup(input_wallet_keyboard),
            parse_mode="HTML"
        )
        return INPUT_WALLET

    except ValueError as val_err:
        await update.message.reply_text(f"⚠️ {str(val_err)}")
        return INPUT_AMOUNT
    except Exception as e:
        logger.error(f"Gagal memproses nominal untuk {symbol}: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Terjadi kesalahan saat mengambil rate harga. Silakan coba sesaat lagi.")
        return ConversationHandler.END
    finally:
        db.close()


async def _proceed_to_payment_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, wallet_address: str) -> int:
    """Helper untuk memproses wallet terpilih dan beralih ke pemilihan metode pembayaran."""
    network = context.user_data["buy_network"]
    symbol = context.user_data["buy_symbol"]
    total_idr = context.user_data.get("buy_total_idr", 0)
    user_id = update.effective_user.id

    context.user_data["buy_wallet"] = wallet_address

    db = SessionLocal()
    try:
        user_balance = get_user_balance(db, user_id)
        available_inventory = get_available_inventory(db, network, symbol)
        if available_inventory is None:
            from services.wallet_sync import sync_wallet_balances
            stock_sym = "MATIC" if network.upper() == "POLYGON" and symbol.upper() == "POL" else symbol
            await sync_wallet_balances([(stock_sym, network)])
            available_inventory = get_available_inventory(db, network, symbol)

        if available_inventory is None or available_inventory < Decimal(str(context.user_data["buy_crypto_amount"])):
            available_text = (
                format_crypto(float(available_inventory), symbol)
                if available_inventory is not None
                else "belum tersedia"
            )
            msg_text = (
                f"⚠️ <b>Stok {symbol} ({network}) belum mencukupi.</b>\n\n"
                f"Stok tersedia: <code>{available_text}</code>\n"
                "Silakan hubungi admin untuk proses manual."
            )
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
            ]])
            if update.callback_query:
                await update.callback_query.edit_message_text(msg_text, reply_markup=markup, parse_mode="HTML")
            else:
                await update.message.reply_text(msg_text, reply_markup=markup, parse_mode="HTML")
            return ConversationHandler.END
    finally:
        db.close()

    keyboard = []
    if user_balance >= total_idr:
        keyboard.append([
            InlineKeyboardButton(f"Saldo Bot ({format_idr(int(user_balance))}) — Instan", callback_data="paymethod_BOT_BALANCE", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("MONEY_BAG", "5350452584119279096"))
        ])

    keyboard.extend([
        [InlineKeyboardButton("QRIS GoPay (All E-Wallet & Bank)", callback_data="paymethod_GOPAY_QRIS", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("PHONE", "5409357944619802453"))],
        [InlineKeyboardButton("Batal", callback_data="buy_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
        [get_owner_button()]
    ])

    text_msg = (
        "💳 <b>PILIH METODE PEMBAYARAN</b>\n\n"
        f"Alamat Wallet: <code>{wallet_address}</code>\n"
        f"Total Pembayaran: <b>{format_idr(total_idr)}</b>\n"
        f"Saldo IDR Anda: <b>{format_idr(int(user_balance))}</b>"
        f"{qris_mdr_note(total_idr)}\n\n"
        "Silakan pilih metode pembayaran di bawah ini:"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text=text_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text=text_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    return SELECT_PAYMENT


async def handle_wallet_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Memvalidasi wallet address yang diketik manual, lalu beralih ke pemilihan metode pembayaran.
    Alamat valid otomatis disimpan agar dapat digunakan kembali (1-Tap).
    """
    wallet_address = update.message.text.strip()
    network = context.user_data["buy_network"]
    symbol = context.user_data["buy_symbol"]
    
    # Validasi alamat wallet per network
    if not validate_wallet_address(wallet_address, network):
        keyboard = [
            [InlineKeyboardButton("Batal", callback_data="buy_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
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

    # Anti-fraud: satu alamat hanya boleh milik satu user (Chat ID)
    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        from database.crud import is_wallet_address_taken_by_other
        taken = is_wallet_address_taken_by_other(db, wallet_address, user_id)
    finally:
        db.close()
    if taken:
        await update.message.reply_text(
            text=WALLET_DUPLICATE_WARNING,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Batal", callback_data="buy_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
                [get_owner_button()],
            ]),
            parse_mode="HTML",
        )
        return INPUT_WALLET

    # Auto-save alamat wallet valid ke data tersimpan user
    db = SessionLocal()
    try:
        from database.crud import save_user_wallet
        save_user_wallet(db, user_id, wallet_address, network=network)
    except Exception as exc:
        logger.debug(f"Auto save wallet error: {exc}")
    finally:
        db.close()

    return await _proceed_to_payment_selection(update, context, wallet_address)


async def handle_saved_wallet_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Menggunakan alamat wallet tersimpan yang dipilih via tombol inline.
    """
    query = update.callback_query
    await query.answer()

    wallet_id = int(query.data.replace("buy_saved_wallet_", ""))
    user_id = update.effective_user.id

    db = SessionLocal()
    try:
        from database.crud import get_saved_wallet_by_id
        sw = get_saved_wallet_by_id(db, wallet_id, user_id)
        if not sw:
            await query.answer("Wallet tidak ditemukan atau sudah dihapus.", show_alert=True)
            return INPUT_WALLET
        wallet_address = sw.wallet_address
    finally:
        db.close()

    network = context.user_data["buy_network"]
    if not validate_wallet_address(wallet_address, network):
        await query.answer(f"Alamat tidak cocok dengan format network {network}!", show_alert=True)
        return INPUT_WALLET

    # Anti-fraud: alamat yang sudah terkunci ke user lain (transaksi sukses) tidak boleh
    # lolos lewat tombol 1-tap — alamat bisa saja disimpan sebelum pemiliknya bertransaksi.
    if _wallet_locked_by_other(user_id, wallet_address):
        await query.answer("Alamat ini sudah dipakai user lain.", show_alert=True)
        await query.message.reply_text(
            WALLET_DUPLICATE_WARNING,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Batal", callback_data="buy_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
                [get_owner_button()],
            ]),
            parse_mode="HTML",
        )
        return INPUT_WALLET

    return await _proceed_to_payment_selection(update, context, wallet_address)


def _wallet_locked_by_other(user_id: int, wallet_address: str) -> bool:
    """True bila alamat sudah terkunci ke user lain (lihat is_wallet_address_taken_by_other)."""
    from database.crud import is_wallet_address_taken_by_other
    db = SessionLocal()
    try:
        return is_wallet_address_taken_by_other(db, wallet_address, user_id)
    finally:
        db.close()



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

    # Tambahkan baris informasi diskon referral jika berlaku
    if context.user_data.get("buy_discount_applied"):
        disc_amt = context.user_data.get("buy_discount_amount", 0)
        disc_pct = context.user_data.get("buy_discount_pct", 10.0)
        summary_text += f"\n🎁 <b>Diskon Referral:</b> -{format_idr(disc_amt)} ({disc_pct:.0f}%)"
    
    # Tambahkan baris informasi metode pembayaran
    method_label = PAYMENT_METHOD_LABELS.get(method_code, method_code)
    summary_text += f"\n💳 <b>Metode Pembayaran:</b> {method_label}"
    summary_text += gas_surcharge_note(symbol, network)
    mdr_idr = calculate_qris_mdr(nominal_idr) if method_code == "GOPAY_QRIS" else 0
    context.user_data["buy_mdr_idr"] = mdr_idr
    if method_code == "GOPAY_QRIS":
        summary_text += qris_mdr_note(nominal_idr)
        summary_text += (
            "\nℹ️ <i>Kode unik akan ditambahkan ke total bayar "
            "untuk verifikasi otomatis.</i>"
        )
    summary_text += "\n\n" + WALLET_LOCK_NOTE

    keyboard = [
        [
            InlineKeyboardButton("Konfirmasi & Bayar", callback_data="buy_confirm", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CHECK", "5237699328843200968")),
            InlineKeyboardButton("Batal", callback_data="buy_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
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

    # --- 00. Alamat bisa saja terkunci ke user lain sejak dipilih (transaksi orang lain baru sukses) ---
    if _wallet_locked_by_other(user_id, context.user_data.get("buy_wallet", "")):
        await query.edit_message_text(
            text=WALLET_DUPLICATE_WARNING,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]]),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # --- 0. Quote basi? (harga dibekukan saat input nominal, percakapan tanpa timeout) ---
    if not await quote_guard.prices_still_valid(
        context.user_data.get("buy_quoted_at"),
        {context.user_data["buy_symbol"]: context.user_data["buy_price_per_unit"]},
    ):
        await query.edit_message_text(
            text=quote_guard.QUOTE_MOVED_TEXT,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]]),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    # --- 1. Rate Limiting Check (Max 5 orders aktif per 10 menit) ---
    db = SessionLocal()
    try:
        ten_minutes_ago = datetime.utcnow() - timedelta(minutes=10)
        recent_orders_count = (
            db.query(Order)
            .filter(
                Order.telegram_id == user_id,
                Order.created_at >= ten_minutes_ago,
                # [FIX MEDIUM-3] Hitung hanya order yang aktif, bukan yang dibatalkan/expired
                Order.status.notin_(["cancelled", "expired", "rejected"]),
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
                    InlineKeyboardButton("Kembali ke Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
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
        discount_applied = context.user_data.get("buy_discount_applied", False)
        discount_pct = context.user_data.get("buy_discount_pct")
        discount_amount = context.user_data.get("buy_discount_amount", 0)
        
        if network.upper() in MANUAL_PAYOUT_NETWORKS:
            await query.edit_message_text(
                text=(
                    f"ℹ️ <b>Pengiriman Otomatis Belum Tersedia</b>\n\n"
                    f"Jaringan <b>{network}</b> belum mendukung pengiriman otomatis. "
                    "Silakan hubungi admin untuk transaksi manual."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Kembali ke Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
                ]]),
                parse_mode="HTML"
            )
            return ConversationHandler.END

        available_inventory = get_available_inventory(db, network, symbol)
        if available_inventory is None:
            from services.wallet_sync import sync_wallet_balances
            stock_sym = "MATIC" if network.upper() == "POLYGON" and symbol.upper() == "POL" else symbol
            await sync_wallet_balances([(stock_sym, network)])
            available_inventory = get_available_inventory(db, network, symbol)

        if available_inventory is None or available_inventory < Decimal(str(crypto_amount)):
            available_text = (
                format_crypto(float(available_inventory), symbol)
                if available_inventory is not None
                else "belum tersedia"
            )
            await query.edit_message_text(
                text=(
                    f"⚠️ <b>Stok {symbol} ({network}) tidak mencukupi.</b>\n\n"
                    f"Stok tersedia saat ini: <code>{available_text}</code>\n"
                    "Mohon maaf, ketersediaan stok telah berubah atau belum mencukupi. Silakan hubungi admin untuk transaksi manual. Pembayaran belum dilakukan."
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Kembali ke Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
                ]]),
                parse_mode="HTML"
            )
            return ConversationHandler.END

        # Helper: konsumsi diskon referral jika diterapkan
        def _consume_discount_if_needed():
            if discount_applied:
                try:
                    from database.crud import consume_referral_discount, get_referral_discount_info
                    from services.referral_discount_service import notify_discount_used
                    consume_referral_discount(db, user_id)
                    info_after = get_referral_discount_info(db, user_id)
                    asyncio.create_task(
                        notify_discount_used(
                            context.bot,
                            user_id,
                            info_after.get("remaining", 0),
                            discount_amount,
                        )
                    )
                except Exception as dexc:
                    logger.warning(f"Gagal consume/notify discount untuk {user_id}: {dexc}")

        # --- 2. Handle Payment Method ---
        if method_code == "BOT_BALANCE":
            # [FIX KRITIS-1] Atomic: Buat order DULU, baru potong saldo.
            # Jika potongan gagal, order dihapus — tidak ada state inconsistency.
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
                "quoted_at": datetime.utcnow(),
                "quote_expires_at": datetime.utcnow() + timedelta(minutes=settings.ORDER_EXPIRE_MINUTES),
                "referral_discount_applied": discount_applied,
                "referral_discount_pct": discount_pct,
                "discount_amount_idr": discount_amount,
            }
            order = create_bot_balance_order(db, order_data)
            if not order:
                await query.edit_message_text(
                    text="❌ <b>Saldo Bot Tidak Mencukupi!</b>\n\nSilakan topup saldo bot Anda terlebih dahulu.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]]),
                    parse_mode="HTML"
                )
                return ConversationHandler.END

            # Konsumsi kuota diskon jika ada
            _consume_discount_if_needed()

            # Kirim notifikasi sukses ke user
            received_idr = context.user_data["buy_received_idr"]
            success_msg = (
                f"🎉 <b>PEMBELIAN BERHASIL (SALDO BOT)!</b>\n\n"
                f"📝 <b>ID Order:</b> <code>{order_id}</code>\n"
                f"🪙 <b>Aset:</b> {format_crypto(crypto_amount, symbol)} ({network})\n"
                f"💵 <b>Nominal Bayar:</b> {format_idr(total_idr)} (Saldo Bot)\n"
                f"🔌 <b>Fee Layanan (dipotong):</b> -{format_idr(fee_idr)}"
                f"{gas_surcharge_note(symbol, network)}\n"
                f"💰 <b>Nilai Koin Diterima:</b> {format_idr(received_idr)}\n"
                f"📍 <b>Wallet Tujuan:</b> <code>{buyer_wallet}</code>\n\n"
                f"✅ Pembayaran menggunakan Saldo Bot lunas! Koin crypto sedang diproses untuk dikirimkan ke wallet Anda."
            )
            # Jadwalkan payout SEBELUM edit pesan: saldo sudah terpotong, dan bila
            # edit Telegram gagal (timeout/"not modified") payout tidak boleh ikut hilang.
            asyncio.create_task(_run_finalize_background(order.order_id, context.bot))
            try:
                await query.edit_message_text(
                    text=success_msg,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]]),
                    parse_mode="HTML"
                )
            except Exception as exc:
                logger.warning(f"Gagal tampilkan sukses saldo {order.order_id}: {exc}")
            return ConversationHandler.END

        # --- 2b. GoPay QRIS Payment (QRIS Statis, pembayaran manual) ---
        if method_code == "GOPAY_QRIS":
            mdr_idr = int(context.user_data.get("buy_mdr_idr") or 0)
            from services.fee_service import QRIS_MAX_TOTAL_IDR, QRIS_MAX_UNIQUE_CODE
            if int(total_idr) + mdr_idr + QRIS_MAX_UNIQUE_CODE > QRIS_MAX_TOTAL_IDR:
                await query.edit_message_text(
                    text=(
                        "❌ <b>Nominal terlalu besar untuk QRIS.</b>\n\n"
                        "Total bayar (termasuk pajak QRIS & kode unik) tidak boleh melewati "
                        "batas QRIS Rp 10.000.000. Kurangi nominal, bagi menjadi beberapa "
                        "transaksi, atau gunakan Saldo Bot. Pembayaran belum dilakukan."
                    ),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]]),
                    parse_mode="HTML",
                )
                return ConversationHandler.END
            unique_code = generate_unique_payment_code(db, base_amount=int(total_idr) + mdr_idr)
            if unique_code is None:
                await query.edit_message_text(
                    text=(
                        "⏳ <b>Antrean pembayaran QRIS sedang penuh.</b>\n\n"
                        "Silakan coba lagi beberapa menit lagi atau gunakan nominal lain. "
                        "Pembayaran belum dilakukan."
                    ),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]]),
                    parse_mode="HTML",
                )
                return ConversationHandler.END
            final_total_idr = int(total_idr) + mdr_idr + unique_code

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
                "mdr_idr": mdr_idr,
                "unique_code": unique_code,
                "total_idr": final_total_idr,
                "buyer_wallet": buyer_wallet,
                "payment_method": "GOPAY_QRIS",
                "status": "pending",
                "expired_at": datetime.utcnow() + timedelta(minutes=settings.ORDER_EXPIRE_MINUTES),
                "referral_discount_applied": discount_applied,
                "referral_discount_pct": discount_pct,
                "discount_amount_idr": discount_amount,
            }
            create_order(db, order_data)

            # Konsumsi kuota diskon jika ada
            _consume_discount_if_needed()

            # Kirim QRIS dinamis + instruksi pembayaran otomatis
            received_idr = context.user_data["buy_received_idr"]
            mdr_line = f"\n🧾 <b>Pajak QRIS 0,3%</b>: +{format_idr(mdr_idr)}" if mdr_idr else ""
            caption = (
                f"{E_CARD()} <b>BAYAR VIA QRIS DINAMIS</b>\n\n"
                f"🎫 <b>ID Order</b>: <code>{order_id}</code>\n"
                f"{E_DOLLAR()} <b>Total Bayar</b>: <b>{format_idr(final_total_idr)}</b>\n"
                f"🔌 <b>Fee Layanan (dipotong)</b>: -{format_idr(fee_idr)}"
                f"{gas_surcharge_note(symbol, network)}"
                f"{mdr_line}\n"
                f"{E_MONEY()} <b>Nilai Koin Diterima</b>: <b>{format_idr(received_idr)}</b>\n"
                f"⏰ <b>Batas Waktu</b>: {settings.ORDER_EXPIRE_MINUTES} Menit\n\n"
                f"📌 <b>Cara Bayar:</b>\n"
                f"1. Scan QRIS di atas dengan <b>GoPay, OVO, DANA, ShopeePay, BCA, atau Mobile Banking</b>.\n"
                f"2. Nominal <b>{format_idr(final_total_idr)}</b> akan muncul otomatis (QRIS Dinamis).\n"
                f"3. Selesaikan pembayaran di aplikasi e-wallet / bank Anda.\n"
                f"4. Koin crypto akan <b>otomatis terkirim</b> ke wallet Anda seketika setelah pembayaran terdeteksi!\n\n"
                f"ℹ️ <i><b>Catatan:</b> Pastikan nominal pembayaran sesuai presisi ({format_idr(final_total_idr)}) agar proses verifikasi & pengiriman koin berjalan otomatis tanpa delay.</i>"
            )
            keyboard = [
                [InlineKeyboardButton("Saya Sudah Transfer", callback_data=f"check_buy_payment_{order_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CHECK", "5237699328843200968"))],
                [InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
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
                f"User: {_esc(update.effective_user.name)} (ID: {user_id})\n"
                f"Koin: {format_crypto(crypto_amount, symbol)} ({network})\n"
                f"Total Pembayaran: <b>{format_idr(final_total_idr)}</b> (Kode Unik: {unique_code}"
                f"{f', Pajak QRIS: {format_idr(mdr_idr)}' if mdr_idr else ''})\n"
                f"Metode: GOPAY_QRIS\n"
                f"Wallet: <code>{buyer_wallet}</code>"
            )
            await notify_admins(context.bot, admin_alert, kind="beli")

            return ConversationHandler.END


        # Metode pembayaran tidak dikenali (harusnya tidak terjadi)
        await safe_edit_message(
            query,
            text=(
                "❌ <b>Metode pembayaran tidak dikenali!</b>\n\n"
                "Silakan mulai ulang alur pembelian."
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
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
                InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
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
    allow_expired_payment=False,
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

        if order.status == "pending" or (
            order.status == "expired"
            and allow_expired_payment
            and order.payment_method == "GOPAY_QRIS"
        ):
            if not claim_order_paid(
                db, order.order_id,
                allow_expired_qris=(order.status == "expired" and allow_expired_payment),
            ):
                return
            db.refresh(order)

        if order.status == "paid":
            if not claim_order_payout_processing(db, order.order_id):
                return
            db.refresh(order)
        elif order.status == "payout_processing":
            if not allow_recovery or not claim_stale_payout_processing(db, order.order_id):
                return
            # Proses sebelumnya mati DI TENGAH pengiriman (restart/deploy/OOM): hash
            # baru disimpan setelah sender selesai, jadi koin mungkin SUDAH terkirim.
            # Kirim ulang otomatis = risiko payout dobel → serahkan ke admin.
            await _escalate_interrupted_payout(db, order, bot or bot_app)
            return
        elif order.status in ("manual_review", "expired"):
            if not allow_admin:
                return
            if not claim_order_payout_processing(db, order.order_id, (order.status,)):
                return
            db.refresh(order)
        else:
            return

        # 1. Pesan Progres: Pembayaran Diterima & Proses Pengiriman Koin
        if order.order_type != "buy":
            reserved = True
        else:
            order_amount = Decimal(str(order.crypto_amount))
            reserved = reserve_order_inventory(
                db, order.order_id, order.network, order.crypto_symbol, order_amount,
            )
            if not reserved:
                # Pesanan sudah dibayar: segarkan stok basi sekali sebelum menolak dan minta admin kirim manual.
                from services.wallet_sync import sync_wallet_balances
                stock_sym = "MATIC" if order.network.upper() == "POLYGON" and order.crypto_symbol.upper() == "POL" else order.crypto_symbol
                await sync_wallet_balances([(stock_sym, order.network)])
                reserved = reserve_order_inventory(
                    db, order.order_id, order.network, order.crypto_symbol, order_amount,
                )

        if not reserved:
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
            release_order_inventory(db, order.order_id, consumed=True)
            user_msg = (
                f"✅ <b>Crypto Terkirim!</b>\n\n"
                f"<b>Order:</b> <code>{order.order_id}</code>\n"
                f"🪙 <b>Jumlah:</b> <code>{format_crypto(float(order.crypto_amount), order.crypto_symbol)} ({order.network})</code>\n"
                f"🏦 <b>Ke:</b> <code>{order.buyer_wallet}</code>\n"
                f"🔗 <b>TX:</b> <code>{result['tx_hash']}</code>\n"
            )
            if result.get("explorer_url"):
                user_msg += f"\n🌐 <a href=\"{result['explorer_url']}\">Lihat di Explorer</a>"
            user_msg += (
                "\n\nTerimakasih sudah bertransaksi di sini, Lancar selalu 🙏🙏\n"
                "Testimoni : t.me/TokoKoinID\n"
                "Channel : t.me/ROBHSN_STORE_SELLER"
            )
            menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]])
            await safe_send_message(bot or bot_app, order.telegram_id, user_msg, reply_markup=menu_keyboard)

            # Post testimony ke channel (Phase 8)
            try:
                from services.testimony_service import post_transaction_testimony
                asyncio.create_task(post_transaction_testimony(bot or bot_app, order, db=db))
            except Exception as texc:
                logger.warning(f"Gagal trigger testimony buy order {order.order_id}: {texc}")
        else:
            # Hash tetap disimpan walau receipt belum terkonfirmasi — supaya
            # watchdog (services/payout_watchdog.py) bisa menyelesaikan order
            # begitu receipt muncul, bukan nyangkut manual_review tanpa jejak.
            tx_hash_gagal = (result.get("tx_hash") or "").strip()
            extra = {"payout_tx_hash": tx_hash_gagal, "tx_hash": tx_hash_gagal} if tx_hash_gagal else {}
            update_order_status(
                db,
                order.order_id,
                new_status="manual_review",
                failure_reason=result["error_message"],
                **extra,
            )
            if not tx_hash_gagal:
                # Tidak ada broadcast: koin masih di wallet, jangan tahan stok selamanya.
                release_order_inventory(db, order.order_id)
            jejak = f"\nTX (broadcast): <code>{tx_hash_gagal}</code>" if tx_hash_gagal else ""
            if result.get("explorer_url"):
                jejak += f"\n🌐 <a href=\"{result['explorer_url']}\">Lihat di Explorer</a>"
            pm = (getattr(order, "payment_method", "") or "").lower()
            if pm in ("balance", "saldo"):
                pay_label = "Saldo Bot"
            elif pm in ("qris", "gopay"):
                pay_label = "GoPay QRIS"
            elif pm in ("bank", "manual_transfer", "bank_transfer"):
                pay_label = "Transfer Bank"
            else:
                pay_label = (order.payment_method or "Manual").upper()

            admin_msg = (
                f"🚨 <b>MANUAL REVIEW REQUIRED ({pay_label})</b>\n\n"
                f"Order: <code>{order.order_id}</code>\n"
                f"User: {order.telegram_id}\n"
                f"Crypto: {order.crypto_amount} {order.crypto_symbol} ({order.network})\n"
                f"Wallet: <code>{order.buyer_wallet}</code>\n"
                f"Error: {result['error_message']}{jejak}\n\n"
                f"Pembayaran sudah diterima tapi pengiriman crypto gagal. Kirim manual."
            )
            await notify_admins(bot or bot_app, admin_msg, kind="error", butuh_tindakan=True)

            user_msg = (
                f"⏳ <b>Pembayaran Diterima</b>\n\n"
                f"Order: <code>{order.order_id}</code>\n"
                f"Pembayaranmu sudah kami terima. Pengiriman crypto sedang diproses oleh admin.\n"
                f"Kami akan mengirim notifikasi setelah selesai. 🙏"
            )
            await safe_send_message(bot or bot_app, order.telegram_id, user_msg)


async def _escalate_interrupted_payout(db, order, bot) -> None:
    """Payout terputus tanpa hash: tandai manual_review & minta admin cek on-chain dulu."""
    update_order_status(
        db,
        order.order_id,
        new_status="manual_review",
        failure_reason="Payout terputus (bot restart) — cek on-chain sebelum kirim ulang",
    )
    admin_msg = (
        f"🚨 <b>PAYOUT TERPUTUS — CEK DULU SEBELUM KIRIM ULANG</b>\n\n"
        f"Order: <code>{order.order_id}</code>\n"
        f"User: {order.telegram_id}\n"
        f"Crypto: {order.crypto_amount} {order.crypto_symbol} ({order.network})\n"
        f"Wallet: <code>{_esc(order.buyer_wallet or '')}</code>\n\n"
        f"Bot berhenti saat sedang mengirim koin, jadi transaksi <b>mungkin sudah terkirim</b>.\n"
        f"Cek riwayat masuk wallet tujuan di explorer. Jika belum ada, tekan "
        f"<b>Approve &amp; Kirim Crypto</b>; jika sudah ada, selesaikan manual."
    )
    admin_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve & Kirim Crypto", callback_data=f"admin_approve_buy_{order.order_id}"),
    ]])
    await notify_admins(bot, admin_msg, reply_markup=admin_keyboard, kind="error", butuh_tindakan=True)


async def _run_finalize_background(
    order_id: str,
    bot=None,
    *,
    allow_admin=False,
    allow_recovery=False,
    allow_expired_payment=False,
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
                allow_expired_payment=allow_expired_payment,
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
    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        order = get_order_by_id(db, order_id)
        if not order:
            await query.answer("❌ Order tidak ditemukan.", show_alert=True)
            return

        # [SECURITY] Validasi kepemilikan order — tolak jika bukan pemiliknya
        if order.telegram_id != user_id:
            logger.warning(
                "User %s mencoba akses order %s milik user %s — ditolak.",
                user_id, order_id, order.telegram_id,
            )
            await query.answer("❌ Akses ditolak.", show_alert=True)
            return

        if order.status != "pending":
            await query.answer(
                f"ℹ️ Order sudah berstatus {order.status.upper()}.", show_alert=True
            )
            return

        if await gopay_service.confirm_payment(
            db, amount=int(order.total_idr), ref_id=order.order_id,
            kind="buy", created_at=order.created_at,
        ):
            try:
                await query.answer("✅ Pembayaran diterima! Memproses pengiriman koin...", show_alert=False)
            except Exception as ans_err:
                logger.debug("query.answer error: %s", ans_err)

            asyncio.create_task(_run_finalize_background(
                order.order_id, context.bot, allow_expired_payment=True,
            ))
            return

        # Jika belum terdeteksi otomatis (misal delay sync mutasi GoPay)
        not_detected_text = (
            f"⏳ <b>Pembayaran sedang disinkronisasi...</b>\n\n"
            f"ID Order: <code>{order.order_id}</code>\n"
            f"Total Nominal: <b>{format_idr(order.total_idr)}</b>\n\n"
            f"Mutasi QRIS GoPay biasanya membutuhkan waktu 30-60 detik untuk sinkron.\n\n"
            f"👉 Silakan klik tombol <b>🔄 Cek Ulang</b> dalam beberapa saat, atau langsung <b>kirim screenshot/foto bukti transfer</b> ke chat ini untuk diproses manual oleh Admin."
        )
        keyboard = [
            [InlineKeyboardButton("Cek Ulang", callback_data=f"check_buy_payment_{order.order_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("SWAP", "5310107765874632305"))],
            [get_owner_button()]
        ]
        await query.message.reply_text(
            not_detected_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error check_buy_payment {order_id}: {e}", exc_info=True)
        try:
            await query.answer("❌ Terjadi kesalahan saat memeriksa pembayaran. Coba lagi.", show_alert=True)
        except Exception:
            pass
    finally:
        db.close()


async def handle_transfer_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Menerima foto bukti transfer dari user untuk order Beli GoPay QRIS.
    Simpan foto, kirim notifikasi & tombol approve ke Admin, serta lakukan cek otomatis di background.
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
            f"User: {_esc(update.effective_user.name)} (ID: <code>{user_id}</code>)\n"
            f"Total Nominal: <b>{format_idr(order.total_idr)}</b>\n"
            f"Koin: {format_crypto(float(order.crypto_amount), order.crypto_symbol)} ({order.network})\n"
            f"Wallet Target: <code>{order.buyer_wallet}</code>\n\n"
            f"Tekan tombol <b>Approve</b> di bawah jika pembayaran valid untuk memicu pengiriman crypto otomatis."
        )
        admin_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve & Kirim Crypto", callback_data=f"admin_approve_buy_{order.order_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CHECK", "5237699328843200968")),
                InlineKeyboardButton("Tolak", callback_data=f"admin_reject_buy_{order.order_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CROSS", "5462882007451185227"))
            ]
        ])
        if photo_file_id:
            from bot.utils.telegram_utils import kirim_ke_topik
            await kirim_ke_topik(context.bot, kind="beli", photo=photo_file_id,
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
                    logger.warning(f"Gagal kirim bukti ke admin {admin_id}: {admin_err}")

        # 2. Pesan penenang ke User bahwa bukti telah diterima dan sedang diproses
        await safe_send_message(
            context.bot, user_id,
            "⏳ <b>Bukti transfer telah diterima!</b>\n\n"
            "Admin telah menerima bukti pembayaran Anda dan sedang memverifikasinya. "
            "Koin crypto akan otomatis dikirimkan ke alamat wallet Anda begitu disetujui."
        )

        # 3. Cek otomatis via API GoPay di background (jika mutasi sudah muncul, langsung eksekusi)
        try:
            if await gopay_service.confirm_payment(
                db, amount=int(order.total_idr), ref_id=order.order_id,
                kind="buy", created_at=order.created_at,
            ):
                await finalize_gopay_buy_payment(db, order, bot=context.bot)
        except Exception as check_err:
            logger.warning("Auto-check during transfer proof failed: %s", check_err)

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
            CallbackQueryHandler(buy_open_saved_wallets, pattern="^buy_saved_wallets$"),
            CallbackQueryHandler(start_buy_callback, pattern="^buy_back_to_menu$"),
            CallbackQueryHandler(cancel_buy, pattern="^menu_back$"),
            CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$"),
        ],
        SELECT_NETWORK: [
            CallbackQueryHandler(handle_network_selection, pattern="^buy_net_[A-Z0-9]+_[A-Z0-9]+$"),
            CallbackQueryHandler(start_buy_callback, pattern="^buy_back_symbols$"),
            CallbackQueryHandler(cancel_buy, pattern="^menu_back$"),
            CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$"),
        ],
        INPUT_AMOUNT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input),
            CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$"),
            CallbackQueryHandler(cancel_buy, pattern="^menu_back$"),
        ],
        INPUT_WALLET: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wallet_input),
            CallbackQueryHandler(handle_saved_wallet_selection, pattern="^buy_saved_wallet_[0-9]+$"),
            CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$"),
            CallbackQueryHandler(cancel_buy, pattern="^menu_back$"),
        ],
        SELECT_PAYMENT: [
            CallbackQueryHandler(handle_payment_selection, pattern="^paymethod_[A-Z0-9_]+$"),
            CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$"),
            CallbackQueryHandler(cancel_buy, pattern="^menu_back$"),
        ],
        CONFIRM_ORDER: [
            CallbackQueryHandler(handle_order_confirmation, pattern="^buy_confirm$"),
            CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$"),
            CallbackQueryHandler(cancel_buy, pattern="^menu_back$"),
        ]
    },
    fallbacks=[
        CallbackQueryHandler(cancel_buy, pattern="^buy_cancel$"),
        CallbackQueryHandler(cancel_buy, pattern="^menu_back$"),
        CommandHandler("cancel", cancel_buy),
        CommandHandler("start", cancel_buy),
    ],
    allow_reentry=True
)

