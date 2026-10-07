"""
bot/handlers/sell.py — Handler Alur Penjualan (Sell Flow).
=========================================================
Mengelola percakapan multi-langkah (ConversationHandler) untuk penjualan crypto:
1. Pilih koin crypto & network
2. Input nominal crypto yang ingin dijual
3. Input informasi rekening bank lokal (Nama Bank, Rek, A/N)
4. Tampilkan review & alamat hot wallet bot untuk transfer koin
5. Menunggu transfer dari user (dengan opsi input manual TX Hash & Hubungi Owner)
"""

import logging
import re
from html import escape as _esc
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_UP
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from database.connection import SessionLocal
from database.crud import create_order, get_order_by_id
from services.price_service import price_service
from services.fee_service import calculate_fee_idr, get_fee_category
from bot.keyboards.crypto_select import (
    get_sell_symbol_keyboard,
    get_sell_network_keyboard
)
from bot.keyboards.main_menu import get_owner_button
from bot.utils.validator import validate_crypto_amount, parse_idr_amount, looks_like_idr
from bot.utils.formatter import format_idr, format_crypto, generate_order_id, display_symbol
from bot.utils.messages import ORDER_SUMMARY_SELL, BANK_DUPLICATE_WARNING, BANK_LOCK_NOTE
from bot.utils.telegram_utils import safe_edit_message, notify_admins
from bot.utils.flow_guard import block_if_busy
from services import quote_guard
from bot.utils.emojis import E_CHART, E_COIN, E_DOLLAR, E_MONEY, E_CHECK, E_WARN, CUSTOM_EMOJI_IDS
from config.settings import settings

logger = logging.getLogger(__name__)

# Masa berlaku quote Jual (keputusan client: 30 menit; QRIS beli/topup tetap 15 menit).
SELL_QUOTE_MINUTES = 30

# State percakapan
SELECT_SYMBOL = 1
SELECT_NETWORK = 2
INPUT_AMOUNT = 3
INPUT_BANK = 4
CONFIRM_ORDER = 5
WAITING_TX = 6
INPUT_TX_HASH = 7
INPUT_PROOF = 8

# Helper untuk mendapatkan alamat hot wallet bot berdasarkan network
def get_hot_wallet_address(network: str) -> str:
    try:
        from services.crypto_sender import CryptoSenderFactory
        sender = CryptoSenderFactory.get_sender(network)
        addr = getattr(sender, "wallet_address", "")
        if addr:
            return addr
    except Exception as e:
        logger.warning(f"Gagal mengambil wallet address untuk {network}: {e}")
        
    from config.assets import get_wallet_address
    return get_wallet_address(network) or "WalletAddressPlaceholder"


async def start_sell_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point alur Jual dari klik tombol menu."""
    if await block_if_busy("sell", update, context):
        return None
    query = update.callback_query
    await query.answer()
    
    await safe_edit_message(
        query,
        text=(
            f"{E_CHART()} <b>JUAL CRYPTOCURRENCY</b>\n\n"
            "Silakan pilih koin crypto yang ingin Anda jual di bawah ini:\n\n"
            "⏰ <b>Jam Layanan Jual:</b> 08.00 - 22.00 WIB\n"
            "<i>(Setelah transfer, kirim TX Hash agar koin diverifikasi otomatis. Pencairan dana diproses manual pada jam layanan atau saat admin online kembali).</i>"
        ),
        reply_markup=get_sell_symbol_keyboard(),
        parse_mode="HTML"
    )
    return SELECT_SYMBOL


async def start_sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point alur Jual dari ketik command /sell."""
    if await block_if_busy("sell", update, context):
        return None
    await update.message.reply_text(
        text=(
            f"{E_CHART()} <b>JUAL CRYPTOCURRENCY</b>\n\n"
            "Silakan pilih koin crypto yang ingin Anda jual di bawah ini:\n\n"
            "⏰ <b>Jam Layanan Jual:</b> 08.00 - 22.00 WIB\n"
            "<i>(Setelah transfer, kirim TX Hash agar koin diverifikasi otomatis. Pencairan dana diproses manual pada jam layanan atau saat admin online kembali).</i>"
        ),
        reply_markup=get_sell_symbol_keyboard(),
        parse_mode="HTML"
    )
    return SELECT_SYMBOL


async def sell_open_saved_banks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Membuka menu Rekening Pencairan langsung dari alur Jual."""
    from bot.handlers.saved_accounts import show_saved_banks_menu
    await show_saved_banks_menu(update, context, back_callback="sell_back_to_menu")
    return SELECT_SYMBOL


async def handle_symbol_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Tahap 1 Jual: Menyimpan simbol koin, lalu menampilkan pilihan jaringan."""
    query = update.callback_query
    await query.answer()
    
    symbol = query.data.split("_")[2]
    context.user_data["sell_symbol"] = symbol
    
    await safe_edit_message(
        query,
        text=(
            f"{E_CHART()} Anda memilih menjual koin: <b>{symbol}</b>\n\n"
            f"Silakan pilih jaringan (network) asal koin yang ingin Anda jual:"
        ),
        reply_markup=get_sell_network_keyboard(symbol),
        parse_mode="HTML"
    )
    return SELECT_NETWORK


def _amount_prompt(symbol: str, network: str, mode: str = None):
    """Teks + keyboard permintaan jumlah jual. mode None = layar pilih cara input;
    COIN = ketik jumlah koin; IDR = ketik nominal Rupiah."""
    from bot.utils.amount_mode import mode_row
    back_icon = CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850")
    if mode == "IDR":
        text = (
            f"📈 Anda memilih menjual: <b>{symbol} ({network})</b>\n\n"
            f"💵 <b>Mode Nominal Rupiah</b>\n"
            f"Berapa <b>nilai jual</b> yang Anda inginkan?\n"
            f"<i>Ketik nominal Rupiah, contoh: <code>5000</code>, <code>Rp 50.000</code>, atau <code>50k</code>. "
            f"Jumlah {symbol} dihitung otomatis dari kurs jual saat ini (dibulatkan ke atas ke presisi koin). "
            f"Nominal ini adalah nilai sebelum fee layanan.</i>"
        )
    elif mode == "COIN":
        text = (
            f"📈 Anda memilih menjual: <b>{symbol} ({network})</b>\n\n"
            f"🪙 <b>Mode Jumlah Koin</b>\n"
            f"Berapa jumlah koin <b>{symbol}</b> yang ingin Anda jual?\n"
            f"<i>Ketik jumlah desimal di chat (contoh: 0.5 atau 10).</i>"
        )
    else:
        text = (
            f"📈 Anda memilih menjual: <b>{symbol} ({network})</b>\n\n"
            f"Pilih cara memasukkan jumlah yang ingin dijual:\n"
            f"🪙 <b>Jumlah Koin</b> — contoh <code>0.5</code> {symbol}\n"
            f"💵 <b>Nominal Rupiah</b> — contoh <code>Rp 50.000</code>"
        )
    keyboard = InlineKeyboardMarkup([
        mode_row("sell", mode),
        [InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=back_icon)],
        [get_owner_button()],
    ])
    return text, keyboard


async def handle_input_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ganti mode input jumlah jual: koin <-> Rupiah."""
    query = update.callback_query
    await query.answer()
    from bot.utils.amount_mode import mode_from_callback
    mode = mode_from_callback(query.data)
    context.user_data["sell_input_mode"] = mode
    text, keyboard = _amount_prompt(context.user_data["sell_symbol"], context.user_data["sell_network"], mode)
    await safe_edit_message(query, text=text, reply_markup=keyboard, parse_mode="HTML")
    return INPUT_AMOUNT


async def handle_network_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Tahap 2 Jual: Menyimpan jaringan, lalu meminta input jumlah koin crypto."""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    symbol = parts[2]
    network = parts[3]

    # Tombol basi/rakitan: hanya jaringan yang depositnya bisa diverifikasi on-chain.
    from bot.keyboards.crypto_select import sell_networks
    if network not in sell_networks(symbol):
        await safe_edit_message(
            query,
            text=f"⚠️ Jaringan <b>{network}</b> belum didukung untuk jual {symbol}. Silakan pilih jaringan lain.",
            reply_markup=get_sell_network_keyboard(symbol),
            parse_mode="HTML",
        )
        return SELECT_NETWORK

    context.user_data["sell_symbol"] = symbol
    context.user_data["sell_network"] = network
    context.user_data["sell_input_mode"] = None

    text, keyboard = _amount_prompt(symbol, network, None)
    await safe_edit_message(query, text=text, reply_markup=keyboard, parse_mode="HTML")
    return INPUT_AMOUNT


async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Memproses nominal crypto, mengecek batas minimum order, lalu meminta info bank."""
    text_input = update.message.text
    mode = context.user_data.get("sell_input_mode") or "COIN"
    rupiah_target = None
    crypto_amount = None

    cancel_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
        [get_owner_button()]
    ])
    if mode == "IDR" or looks_like_idr(text_input):
        try:
            rupiah_target = parse_idr_amount(text_input)
        except ValueError:
            await update.message.reply_text(
                text=(
                    "❌ <b>Nominal Rupiah Tidak Valid!</b>\n\n"
                    "Ketik angka Rupiah, contoh: <code>5000</code>, <code>Rp 50.000</code>, atau <code>50k</code>:"
                ),
                reply_markup=cancel_keyboard,
                parse_mode="HTML"
            )
            return INPUT_AMOUNT
    else:
        is_valid, crypto_amount = validate_crypto_amount(text_input)
        if not is_valid:
            await update.message.reply_text(
                text=(
                    "❌ <b>Jumlah Tidak Valid!</b>\n\n"
                    "Format angka salah. Harap kirimkan angka desimal positif (contoh: <code>1.5</code> atau <code>50</code>):"
                ),
                reply_markup=cancel_keyboard,
                parse_mode="HTML"
            )
            return INPUT_AMOUNT

    symbol = context.user_data["sell_symbol"]
    network = context.user_data["sell_network"]

    # Nominal koin = persis angka yang ditampilkan (dibulatkan ke bawah ke presisi deposit,
    # tanpa kode unik), supaya angka yang dihitung = ditampilkan = diverifikasi on-chain.
    from services.deposit_amount import base_amount, quantum
    if crypto_amount is not None:
        crypto_amount = float(base_amount(symbol, crypto_amount))
        if crypto_amount <= 0:
            await update.message.reply_text(
                "❌ <b>Jumlah terlalu kecil.</b> Silakan masukkan jumlah koin yang lebih besar:",
                parse_mode="HTML")
            return INPUT_AMOUNT

    db = SessionLocal()
    try:
        # Fetch harga jual terkini (0% spread)
        price_data = await price_service.get_price(symbol, db)
        if not price_data:
            raise ValueError(f"Harga {symbol} belum tersedia, coba lagi sebentar")
            
        sell_price_idr = price_data["sell_price_idr"]

        if rupiah_target is not None:
            # Mode Rupiah: koin = nilai / kurs jual, dibulatkan KE ATAS ke presisi deposit
            # agar nilai jual tidak di bawah yang diminta (dan tetap memenuhi minimum order).
            if not sell_price_idr or sell_price_idr <= 0:
                raise ValueError(f"Harga {symbol} belum tersedia, coba lagi sebentar")
            crypto_amount = float(
                (Decimal(rupiah_target) / Decimal(str(sell_price_idr))).quantize(quantum(symbol), rounding=ROUND_UP))
            if crypto_amount <= 0:
                raise ValueError("Nominal Rupiah terlalu kecil. Silakan masukkan nominal yang lebih besar.")

        # Hitung kotor nominal IDR dari koin yang benar-benar disetor
        gross_nominal_idr = int(Decimal(str(crypto_amount)) * Decimal(str(sell_price_idr)))
        
        # Hitung fee transaksi (is_outgoing=False -> tanpa surcharge +2k untuk Jual)
        fee_category = get_fee_category(symbol)
        fee_idr = calculate_fee_idr(
            gross_nominal_idr,
            category=fee_category,
            symbol=symbol,
            network=network,
            is_outgoing=False
        )
        
        # Bersih nominal IDR yang diterima customer (Gross - Fee)
        net_nominal_idr = gross_nominal_idr - fee_idr
        if net_nominal_idr <= 0:
            raise ValueError(
                "Nominal penjualan terlalu kecil setelah dipotong fee layanan. "
                "Silakan masukkan jumlah koin yang lebih besar."
            )
        
        # Minimum transaksi ditegakkan oleh calculate_fee_idr pada nominal KOTOR (gross):
        # Rp 5.000 default; Rp 7.500 untuk pasangan gas (ETH-ETH/TRX-TRON/USDT-ETH/
        # USDC-ETH). Net boleh di bawah Rp 5.000 (contoh client: jual 105k -> net 99k).

        # Simpan rincian perhitungan ke context
        context.user_data["sell_crypto_amount"] = crypto_amount
        context.user_data["sell_price_per_unit"] = sell_price_idr
        context.user_data["sell_quoted_at"] = quote_guard.stamp()
        context.user_data["sell_gross_nominal_idr"] = gross_nominal_idr
        context.user_data["sell_fee_idr"] = fee_idr
        context.user_data["sell_net_idr"] = net_nominal_idr
        
    except ValueError as val_err:
        await update.message.reply_text(f"⚠️ {str(val_err)}")
        db.close()
        return INPUT_AMOUNT
    except Exception as e:
        logger.error(f"Error memproses nominal jual {symbol}: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Terjadi kesalahan saat memproses perhitungan. Silakan coba sesaat lagi.")
        db.close()
        return ConversationHandler.END
    finally:
        db.close()

    keyboard = [
        [InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
        [get_owner_button()]
    ]

    # Ambil daftar rekening bank & e-wallet tersimpan milik user
    user_id = update.effective_user.id
    db_saved = SessionLocal()
    try:
        from database.crud import get_user_saved_banks
        saved_banks = get_user_saved_banks(db_saved, user_id)
    finally:
        db_saved.close()

    saved_bank_buttons = []
    for sb in saved_banks:
        icon = "📱" if sb.account_type == "EWALLET" else "🏦"
        lbl = f"{icon} Gunakan: {sb.bank_name} - {sb.account_number} ({sb.account_name})"
        if len(lbl) > 42:
            lbl = f"{icon} {sb.bank_name} - {sb.account_number} ({sb.account_name[:10]}...)"
        saved_bank_buttons.append([InlineKeyboardButton(lbl, callback_data=f"sell_saved_bank_{sb.id}")])

    input_bank_keyboard = saved_bank_buttons + keyboard

    from services.deposit_amount import format_deposit_amount
    rupiah_note = (
        f"• Nominal Diminta: <code>{format_idr(rupiah_target)}</code> "
        f"<i>(koin dibulatkan ke atas)</i>\n" if rupiah_target is not None else ""
    )
    await update.message.reply_text(
        text=(
            f"🪙 <b>Simulasi Perhitungan Penjualan:</b>\n"
            f"• Aset Dijual: <code>{format_deposit_amount(crypto_amount, symbol)} {display_symbol(symbol)} ({network})</code>\n"
            f"{rupiah_note}"
            f"• Kurs Jual: <code>{format_idr(sell_price_idr)}</code>\n"
            f"• Nominal Kotor: <code>{format_idr(gross_nominal_idr)}</code>\n"
            f"• Fee Layanan: <code>{format_idr(fee_idr)}</code>\n"
            f"• <b>Nominal Bersih Anda Terima:</b> <b>{format_idr(net_nominal_idr)}</b>\n\n"
            f"Silakan ketik detail <b>Rekening Bank / E-Wallet Penerima</b> Anda.\n"
            f"<i>Format bebas, disarankan: Nama Bank, No Rekening, Atas Nama.</i>\n"
            f"<i>(Contoh: BCA, 882049281, Budi Santoso)</i>\n"
            f"<i>(Contoh: GOPAY, 081234567890, Budi Santoso)</i>"
        ),
        reply_markup=InlineKeyboardMarkup(input_bank_keyboard),
        parse_mode="HTML"
    )
    return INPUT_BANK


async def _proceed_to_sell_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    bank_name: str,
    bank_acc: str,
    bank_holder: str
) -> int:
    """Helper untuk menyusun ringkasan penjualan dan menampilkan tombol Konfirmasi Jual."""
    # Anti-fraud: rekening/e-wallet yang sudah terkunci ke user lain (penjualan sukses) ditolak.
    from database.crud import is_bank_account_taken_by_other
    lock_db = SessionLocal()
    try:
        taken = is_bank_account_taken_by_other(lock_db, bank_acc, update.effective_user.id)
    finally:
        lock_db.close()
    if taken:
        warn_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()],
        ])
        if update.callback_query:
            await update.callback_query.answer("Rekening ini sudah dipakai user lain.", show_alert=True)
            await update.callback_query.message.reply_text(BANK_DUPLICATE_WARNING, reply_markup=warn_markup, parse_mode="HTML")
        else:
            await update.message.reply_text(BANK_DUPLICATE_WARNING, reply_markup=warn_markup, parse_mode="HTML")
        return INPUT_BANK

    # "|" memisahkan kolom di order (BANK | NOMOR | NAMA); netralkan dari input user agar
    # nama/nomor tidak bisa menyisipkan kolom palsu (anti poisoning kunci rekening).
    bank_name, bank_acc, bank_holder = (str(v).replace("|", "/") for v in (bank_name, bank_acc, bank_holder))
    context.user_data["sell_bank_name"] = bank_name
    context.user_data["sell_bank_acc"] = bank_acc
    context.user_data["sell_bank_holder"] = bank_holder

    order_id = generate_order_id()
    context.user_data["sell_order_id"] = order_id

    crypto_amount = context.user_data["sell_crypto_amount"]
    symbol = context.user_data["sell_symbol"]
    network = context.user_data["sell_network"]
    price_per_unit = context.user_data["sell_price_per_unit"]
    fee_idr = context.user_data["sell_fee_idr"]
    net_idr = context.user_data["sell_net_idr"]

    summary = ORDER_SUMMARY_SELL.format(
        order_id=order_id,
        crypto_amount_str=format_crypto(crypto_amount, symbol),
        network=network,
        price_per_unit_str=format_idr(price_per_unit),
        nominal_idr_str=format_idr(net_idr),
        fee_idr_str=format_idr(fee_idr),
        bank_name=_esc(bank_name),
        bank_acc=_esc(bank_acc),
        bank_holder=_esc(bank_holder)
    )
    summary +="\n\n" + BANK_LOCK_NOTE

    keyboard = [
        [
            InlineKeyboardButton("Konfirmasi Jual", callback_data="sell_confirm", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CHECK", "5237699328843200968")),
            InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
        ],
        [get_owner_button()]
    ]

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=summary,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            text=summary,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    return CONFIRM_ORDER


async def handle_bank_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menyimpan detail rekening bank yang diketik manual dan menyajikan summary order."""
    bank_info = update.message.text.strip()

    # Batas panjang: data bank digabung ke Order.buyer_wallet (String(250))
    if len(bank_info) > 250:
        keyboard = [
            [InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
        await update.message.reply_text(
            text=(
                "❌ <b>Informasi Rekening Terlalu Panjang!</b>\n\n"
                "Data rekening maksimal 250 karakter. Silakan kirim ulang lebih singkat:\n"
                "<i>(Contoh: BCA, 882049281, Budi Santoso)</i>"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return INPUT_BANK

    # Validasi input sederhana (pastikan tidak kosong dan punya pemisah koma / spasi)
    if len(bank_info) < 8:
        keyboard = [
            [InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
        await update.message.reply_text(
            text=(
                "❌ <b>Informasi Rekening Tidak Lengkap!</b>\n\n"
                "Harap berikan data rekening secara lengkap (Nama Bank, No Rek, & Nama Pemilik):\n"
                "<i>(Contoh: Bank Mandiri, 1234567890, Joko Widodo)</i>"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return INPUT_BANK

    parsed = parse_bank_text(bank_info)
    if not parsed:
        # Dulu: jatuh ke "Bank Lokal" + nama Telegram sebagai pemilik rekening (bisa salah
        # transfer) dan cek rekening terkunci dilewati. Minta ulang dengan data lengkap.
        await update.message.reply_text(
            text=(
                "❌ <b>Data Rekening Belum Lengkap</b>\n\n"
                "Sertakan <b>Nama Bank/E-Wallet</b>, <b>No Rekening/HP</b>, dan <b>Atas Nama</b>.\n"
                "<i>Contoh: BCA, 882049281, Budi Santoso</i>"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
                [get_owner_button()],
            ]),
            parse_mode="HTML",
        )
        return INPUT_BANK
    if parsed:
        bank_name, bank_acc, bank_holder = parsed

        # Auto-save ke database agar user bisa 1-Tap pada transaksi berikutnya
        user_id = update.effective_user.id
        db = SessionLocal()
        try:
            from database.crud import save_user_bank, is_bank_account_taken_by_other
            if is_bank_account_taken_by_other(db, bank_acc, user_id):
                # Jangan simpan rekening milik user lain ke profil ini.
                await update.message.reply_text(
                    BANK_DUPLICATE_WARNING,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
                        [get_owner_button()],
                    ]),
                    parse_mode="HTML",
                )
                return INPUT_BANK
            save_user_bank(db, user_id, bank_name, bank_acc, bank_holder)
        except Exception as exc:
            logger.debug(f"Auto save bank error: {exc}")
        finally:
            db.close()

    return await _proceed_to_sell_confirmation(
        update=update,
        context=context,
        bank_name=bank_name,
        bank_acc=bank_acc,
        bank_holder=bank_holder
    )


async def handle_saved_bank_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menggunakan rekening tersimpan yang dipilih melalui tombol inline."""
    query = update.callback_query
    await query.answer()

    bank_id = int(query.data.replace("sell_saved_bank_", ""))
    user_id = update.effective_user.id

    db = SessionLocal()
    try:
        from database.crud import get_saved_bank_by_id
        sb = get_saved_bank_by_id(db, bank_id, user_id)
        if not sb:
            await query.answer("Rekening tidak ditemukan atau sudah dihapus.", show_alert=True)
            return INPUT_BANK
        bank_name = sb.bank_name
        bank_acc = sb.account_number
        bank_holder = sb.account_name
    finally:
        db.close()

    return await _proceed_to_sell_confirmation(
        update=update,
        context=context,
        bank_name=bank_name,
        bank_acc=bank_acc,
        bank_holder=bank_holder
    )



async def handle_order_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menyimpan order ke database, lalu memberikan alamat hot wallet bot ke user."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    order_id = context.user_data["sell_order_id"]
    symbol = context.user_data["sell_symbol"]
    network = context.user_data["sell_network"]
    crypto_amount = context.user_data["sell_crypto_amount"]
    price_per_unit = context.user_data["sell_price_per_unit"]
    nominal_idr = context.user_data["sell_gross_nominal_idr"] # nominal kotor
    fee_idr = context.user_data["sell_fee_idr"]
    net_idr = context.user_data["sell_net_idr"] # nominal bersih

    # Rekening bisa terkunci ke user lain sejak diinput — cek ulang sebelum order dibuat.
    from database.crud import is_bank_account_taken_by_other
    with SessionLocal() as lock_db:
        locked = is_bank_account_taken_by_other(lock_db, context.user_data.get("sell_bank_acc", ""), user_id)
    if locked:
        context.user_data.pop("sell_order_id", None)  # order belum dibuat
        await query.edit_message_text(BANK_DUPLICATE_WARNING, parse_mode="HTML")
        return ConversationHandler.END

    # Quote basi? (harga dibekukan saat input jumlah, percakapan tanpa timeout)
    if not await quote_guard.prices_still_valid(context.user_data.get("sell_quoted_at"), {symbol: price_per_unit}):
        context.user_data.pop("sell_order_id", None)  # order belum dibuat
        await query.edit_message_text(
            text=quote_guard.QUOTE_MOVED_TEXT,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]]),
            parse_mode="HTML",
        )
        return ConversationHandler.END

    hot_wallet = get_hot_wallet_address(network)
    
    db = SessionLocal()
    try:
        from services.deposit_amount import assign_deposit_amount, format_deposit_amount
        deposit_amount = assign_deposit_amount(db, network, symbol, hot_wallet, crypto_amount)
        if deposit_amount is None:
            context.user_data.pop("sell_order_id", None)  # order belum dibuat
            await query.edit_message_text(
                "⏳ Antrean deposit untuk nominal ini sedang penuh. Silakan coba lagi beberapa "
                "menit lagi atau gunakan jumlah lain. Jangan mengirim koin dulu.",
                parse_mode="HTML")
            return ConversationHandler.END
        crypto_amount = float(deposit_amount)
        context.user_data["sell_crypto_amount"] = crypto_amount
        deposit_str = f"{format_deposit_amount(deposit_amount, symbol)} {display_symbol(symbol)}"

        # Simpan order ke DB dengan status WAITING_CRYPTO_DEPOSIT
        # (deposit crypto akan diverifikasi otomatis oleh DepositDetector)
        order_data = {
            "order_id": order_id,
            "telegram_id": user_id,
            "order_type": "sell",
            "crypto_symbol": symbol,
            "network": network,
            "crypto_amount": deposit_amount,
            "price_per_unit": int(price_per_unit),
            "nominal_idr": int(nominal_idr),
            "fee_idr": int(fee_idr),
            "total_idr": int(net_idr), # Bersih diterima user
            "buyer_wallet": f"{context.user_data['sell_bank_name']} | {context.user_data['sell_bank_acc']} | {context.user_data['sell_bank_holder']}", # Kita simpan info bank disini
            "deposit_wallet": hot_wallet,
            "status": "WAITING_CRYPTO_DEPOSIT",
            "expired_at": datetime.utcnow() + timedelta(minutes=SELL_QUOTE_MINUTES),
        }
        create_order(db, order_data)
        
        token_hint = ""
        try:
            token_address = CryptoSenderFactory.get_sender(network).config.get("tokens", {}).get(symbol.upper())
            if token_address:
                token_hint = f"Token: <b>{symbol}</b>, kontrak <code>{token_address}</code>\n"
        except Exception:
            token_hint = ""

        waiting_text = (
            f"📥 <b>ORDER PENJUALAN DIBUAT</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"Harap kirimkan <b>TEPAT {deposit_str}</b> ke alamat Hot Wallet kami di bawah ini:\n\n"
            f"Network: <b>{network}</b>\n"
            f"{token_hint}"
            f"Alamat Hot Wallet:\n<code>{hot_wallet}</code>\n\n"
            f"⏳ <b>Batas Waktu Quote:</b> {SELL_QUOTE_MINUTES} Menit\n"
            f"• Kirim <b>hanya {symbol} di jaringan {network}</b>. Koin lain atau native coin "
            f"(mis. ETH/BNB/POL) tidak dapat diverifikasi otomatis dan harus diproses admin.\n\n"
            f"✍️ <b>WAJIB kirim TX Hash setelah transfer.</b>\n"
            f"Tekan tombol <b>Kirim TX Hash</b> di bawah lalu kirim Hash/TxID transaksimu. "
            f"Bot akan memeriksanya langsung di blockchain; jika valid, admin langsung memproses Rupiah Anda. "
            f"Tanpa TX Hash, order tidak bisa diproses.\n"
            f"<i>Sudah menutup chat ini? Kirim perintah /txhash kapan saja untuk mengirim hash.</i>\n\n"
            f"⏰ <b>Catatan Layanan:</b>\n"
            f"• Pencairan dana ke rekening/e-wallet Anda dilayani <b>08.00 - 22.00 WIB</b> (diproses manual saat admin online)."
        )

        keyboard = [
            [InlineKeyboardButton("⛓ Salin Alamat Hot Wallet", copy_text=CopyTextButton(text=hot_wallet))],
            [InlineKeyboardButton("✍️ Kirim TX Hash", callback_data="sell_input_tx", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("HISTORY", "5373251851074415873"))],
            [InlineKeyboardButton("Batal Jual", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
        
        from services.qris_generator import get_wallet_qr_stream
        qr_stream = get_wallet_qr_stream(hot_wallet)
        sent_photo = False
        if qr_stream:
            try:
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=qr_stream,
                    caption=waiting_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )
                sent_photo = True
                try:
                    await query.delete_message()
                except Exception:
                    pass
            except Exception as pe:
                logger.warning(f"Gagal kirim QR photo deposit sell: {pe}")

        if not sent_photo:
            await safe_edit_message(
                query,
                text=waiting_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        
        # Admin TIDAK dikabari saat order dibuat: baru setelah TX hash user terverifikasi
        # on-chain, DepositDetector mengirim notifikasi transfer Rupiah ke admin.
                
    except Exception as e:
        logger.error(f"Error saat konfirmasi order sell: {e}", exc_info=True)
        await query.message.reply_text("⚠️ Terjadi kesalahan internal saat membuat pesanan.")
    finally:
        db.close()
        
    return WAITING_TX


async def prompt_tx_hash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Meminta user memasukkan string TX Hash."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
        [get_owner_button()]
    ]
    await safe_edit_message(
        query,
        text=(
            "✍️ <b>KIRIM TX HASH MANUAL</b>\n\n"
            "Silakan ketikkan <b>TX Hash / Transaction ID (TxID)</b> dari pengiriman crypto Anda ke chat:\n"
            "<i>(Pastikan Anda telah sukses melakukan transfer terlebih dahulu)</i>"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return INPUT_TX_HASH


async def handle_tx_hash_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """TX Hash wajib: diverifikasi on-chain; valid -> admin dikabari untuk transfer Rupiah."""
    if not update.message or not update.message.text:
        await update.message.reply_text(
            "⚠️ Kirimkan <b>TX Hash</b> dalam bentuk teks. Contoh: <code>0xabc...def</code>",
            parse_mode="HTML"
        )
        return INPUT_TX_HASH

    from bot.handlers.deposit_hash import submit_deposit_hash
    result = await submit_deposit_hash(update, context, context.user_data.get("sell_order_id"), update.message.text)
    return INPUT_TX_HASH if result == "retry" else WAITING_TX



async def prompt_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Batal", callback_data="sell_cancel", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
        [get_owner_button()]
    ]
    await safe_edit_message(
        query,
        text=(
            "📸 <b>UPLOAD BUKTI TRANSFER</b>\n\n"
            "Silakan kirimkan <b>foto / screenshot</b> bukti transfer Anda ke chat ini.\n\n"
            "<i>Catatan: Foto bukti transfer adalah bukti pendukung bagi admin. Deposit tetap harus terverifikasi secara on-chain di blockchain.</i>"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return INPUT_PROOF


async def handle_sell_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from bot.utils.telegram_utils import admin_notification_targets
    db = SessionLocal()
    try:
        order_id = context.user_data.get("sell_order_id")
        order = get_order_by_id(db, order_id)
        if not order or order.telegram_id != update.effective_user.id or order.status != "WAITING_CRYPTO_DEPOSIT":
            await update.message.reply_text("Order tidak tersedia untuk upload bukti.")
            return ConversationHandler.END

        file_id = update.message.photo[-1].file_id
        order.deposit_proof_file_id = file_id
        db.commit()

        # Picu scan verifikasi deposit langsung di background
        try:
            import asyncio
            from services.detector import deposit_detector
            asyncio.create_task(deposit_detector.scan_incoming_deposits(bot_app=context.application))
        except Exception:
            pass

        caption = (
            f"📸 <b>BUKTI TRANSFER PENJUALAN (SELL)</b>\n\n"
            f"Order: <code>{order.order_id}</code>\n"
            f"User ID: <code>{order.telegram_id}</code>\n"
            f"Crypto: {format_crypto(float(order.crypto_amount), order.crypto_symbol)} ({order.network})\n"
            f"Rupiah Bersih: <b>{format_idr(order.total_idr)}</b>\n"
            f"Rekening: <code>{order.buyer_wallet}</code>\n\n"
            f"<i>⚠️ Foto bukan konfirmasi blockchain. Jangan transfer Rupiah sebelum ada notifikasi DEPOSIT TERVERIFIKASI.</i>"
        )
        delivered = False
        for chat_id in admin_notification_targets("sell"):
            try:
                await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption, parse_mode="HTML")
                delivered = True
            except Exception:
                logger.warning("Gagal forward bukti sell ke tujuan admin.")
        if not delivered:
            await notify_admins(context.bot, f"Bukti foto tersimpan untuk order {order.order_id}; penerusan foto gagal.", order_type="sell", kind="jual")

        keyboard = [
            [InlineKeyboardButton("Kirim TX Hash", callback_data="sell_input_tx", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("HISTORY", "5373251851074415873"))],
            [InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
        await update.message.reply_text(
            "✅ <b>Bukti Transfer Tersimpan!</b>\n\n"
            "Foto bukti telah diteruskan ke admin, tetapi <b>foto bukan pengganti TX Hash</b>. "
            "Order baru diproses setelah Anda mengirim <b>TX Hash</b> yang terverifikasi di blockchain. "
            "Tekan tombol di bawah untuk mengirimnya. 🙏",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return WAITING_TX
    finally:
        db.close()


async def cancel_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Membatalkan alur jual dan kembali ke menu utama."""
    query = update.callback_query
    # Order hanya dibatalkan lewat tombol "Batal" eksplisit. "Menu Utama" / /cancel /
    # /start sekadar keluar dari flow: order yang sudah dibuat tetap menunggu deposit
    # (user mungkin sudah mengirim koin — order cancelled tidak dipindai detector).
    order_id = context.user_data.pop("sell_order_id", None)
    if query:
        alert = None
        if order_id and query.data == "sell_cancel":
            alert = "❌ Penjualan dibatalkan."
            db = SessionLocal()
            try:
                from database.crud import update_order_status
                order = get_order_by_id(db, order_id)
                if order is None:
                    pass
                elif order.telegram_id != update.effective_user.id:
                    alert = None
                elif order.deposit_tx_hash or order.deposit_proof_file_id:
                    alert = "⚠️ TX Hash / bukti sudah dikirim — order tidak dibatalkan dan tetap diproses."
                elif (order.status or "").upper() in {"WAITING_CRYPTO_DEPOSIT", "PENDING", "DRAFT", "QUOTED"}:
                    update_order_status(db, order_id, new_status="cancelled", failure_reason="Dibatalkan oleh pengguna")
                else:
                    alert = "⚠️ Deposit sudah terkonfirmasi dan order diteruskan ke admin."
            except Exception as e:
                logger.warning(f"Gagal membatalkan order sell {order_id}: {e}")
            finally:
                db.close()

        try:
            if alert:
                await query.answer(alert, show_alert=True)
        except Exception:
            pass

        from bot.handlers.start import send_main_menu
        await send_main_menu(update, context)
    else:
        await update.message.reply_text(
            text="❌ Sesi penjualan dibatalkan.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
            ]])
        )
    return ConversationHandler.END


# Definisikan ConversationHandler untuk Sell
sell_conversation_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_sell_callback, pattern="^menu_sell$"),
        CommandHandler("sell", start_sell_command)
    ],
    states={
        SELECT_SYMBOL: [
            CallbackQueryHandler(handle_symbol_selection, pattern="^sell_sym_[A-Z0-9]+$"),
            CallbackQueryHandler(sell_open_saved_banks, pattern="^sell_saved_banks$"),
            CallbackQueryHandler(start_sell_callback, pattern="^sell_back_to_menu$"),
            CallbackQueryHandler(cancel_sell, pattern="^menu_back$"),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$"),
        ],
        SELECT_NETWORK: [
            CallbackQueryHandler(handle_network_selection, pattern="^sell_net_[A-Z0-9]+_[A-Z0-9]+$"),
            CallbackQueryHandler(start_sell_callback, pattern="^sell_back_symbols$"),
            CallbackQueryHandler(cancel_sell, pattern="^menu_back$"),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$"),
        ],
        INPUT_AMOUNT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input),
            CallbackQueryHandler(handle_input_mode, pattern="^sell_mode_(coin|idr)$"),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$"),
            CallbackQueryHandler(cancel_sell, pattern="^menu_back$"),
        ],
        INPUT_BANK: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bank_input),
            CallbackQueryHandler(handle_saved_bank_selection, pattern="^sell_saved_bank_[0-9]+$"),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$"),
            CallbackQueryHandler(cancel_sell, pattern="^menu_back$"),
        ],
        CONFIRM_ORDER: [
            CallbackQueryHandler(handle_order_confirmation, pattern="^sell_confirm$"),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$"),
            CallbackQueryHandler(cancel_sell, pattern="^menu_back$"),
        ],
        WAITING_TX: [
            CallbackQueryHandler(prompt_tx_hash, pattern="^sell_input_tx$"),
            CallbackQueryHandler(prompt_proof, pattern="^sell_upload_proof$"),
            MessageHandler(filters.PHOTO, handle_sell_proof),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$"),
            CallbackQueryHandler(cancel_sell, pattern="^menu_back$"),
        ],
        INPUT_TX_HASH: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tx_hash_input),
            MessageHandler(filters.PHOTO, handle_sell_proof),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$"),
            CallbackQueryHandler(cancel_sell, pattern="^menu_back$"),
        ],
        INPUT_PROOF: [
            MessageHandler(filters.PHOTO, handle_sell_proof),
            CallbackQueryHandler(prompt_tx_hash, pattern="^sell_input_tx$"),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$"),
            CallbackQueryHandler(cancel_sell, pattern="^menu_back$"),
        ],
    },
    fallbacks=[
        CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$"),
        CallbackQueryHandler(cancel_sell, pattern="^menu_back$"),
        CommandHandler("cancel", cancel_sell),
        CommandHandler("start", cancel_sell),
    ],
    allow_reentry=True
)



_BANK_ACC_RE = re.compile(r"(?<![\w])(\+?\d[\d .\-]{3,}\d)(?![\w])")


def parse_bank_text(text: str):
    """(bank, no_rekening, atas_nama) dari input bebas, atau None bila tidak lengkap.

    Mendukung "BCA, 123, Budi", "BCA 123 Budi", "BCA - 123 - Budi", "DANA 0812 3456 a/n Siti",
    dan baris terpisah. Nama bank = teks sebelum nomor, atas nama = teks sesudahnya.
    """
    raw = (text or "").replace("|", "/").strip()  # "|" pemisah internal buyer_wallet
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], " ".join(parts[2:])
    match = _BANK_ACC_RE.search(raw)
    if not match or sum(ch.isdigit() for ch in match.group(1)) < 5:
        return None
    bank = re.sub(r"[\s\-:,/]+$", "", raw[:match.start()]).strip()
    holder = re.sub(r"^[\s\-:,/]+", "", raw[match.end():]).strip()
    holder = re.sub(r"^(a\s*[/.]\s*n\.?|atas\s+nama)(?=[\s:.]|$)\s*[:.]?\s*", "", holder, flags=re.IGNORECASE).strip()
    if not bank or not holder:
        return None
    return bank, match.group(1).strip(), " ".join(holder.split())
