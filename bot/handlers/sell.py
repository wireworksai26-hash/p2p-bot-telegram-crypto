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
from database.crud import create_order, get_order_by_id
from services.price_service import price_service
from services.fee_service import calculate_fee_idr, get_fee_category
from bot.keyboards.crypto_select import (
    get_sell_symbol_keyboard,
    get_sell_network_keyboard
)
from bot.keyboards.main_menu import get_owner_button
from bot.utils.validator import validate_crypto_amount
from bot.utils.formatter import format_idr, format_crypto, generate_order_id
from bot.utils.messages import ORDER_SUMMARY_SELL
from bot.utils.telegram_utils import safe_edit_message, notify_admins
from config.settings import settings

logger = logging.getLogger(__name__)

# State percakapan
SELECT_SYMBOL = 1
SELECT_NETWORK = 2
INPUT_AMOUNT = 3
INPUT_BANK = 4
CONFIRM_ORDER = 5
WAITING_TX = 6
INPUT_TX_HASH = 7

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
        
    net = network.upper()
    if net in ["BSC", "ETH", "AVAX", "POLYGON", "BASE", "ARB", "GRAVITY", "OPTIMISM", "ROBINHOOD", "KAIA", "BERA", "HYPEREVM"]:
        return settings.EVM_WALLET_ADDRESS or "0x0000000000000000000000000000000000000000"
    elif net == "SOLANA":
        return settings.SOL_WALLET_ADDRESS or "SolanaWalletAddressPlaceholder"
    elif net == "TRON":
        return settings.TRX_WALLET_ADDRESS or "TronWalletAddressPlaceholder"
    elif net == "TON":
        return settings.TON_WALLET_ADDRESS or "TonWalletAddressPlaceholder"
    elif net == "SUI":
        return settings.SUI_WALLET_ADDRESS or "SuiWalletAddressPlaceholder"
    return settings.EVM_WALLET_ADDRESS or "WalletAddressPlaceholder"


async def start_sell_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point alur Jual dari klik tombol menu."""
    query = update.callback_query
    await query.answer()
    
    await safe_edit_message(
        query,
        text=(
            "📈 <b>JUAL CRYPTOCURRENCY</b>\n\n"
            "Silakan pilih koin crypto yang ingin Anda jual di bawah ini:"
        ),
        reply_markup=get_sell_symbol_keyboard(),
        parse_mode="HTML"
    )
    return SELECT_SYMBOL


async def start_sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point alur Jual dari ketik command /sell."""
    await update.message.reply_text(
        text=(
            "📈 <b>JUAL CRYPTOCURRENCY</b>\n\n"
            "Silakan pilih koin crypto yang ingin Anda jual di bawah ini:"
        ),
        reply_markup=get_sell_symbol_keyboard(),
        parse_mode="HTML"
    )
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
            f"📈 Anda memilih menjual koin: <b>{symbol}</b>\n\n"
            f"Silakan pilih jaringan (network) asal koin yang ingin Anda jual:"
        ),
        reply_markup=get_sell_network_keyboard(symbol),
        parse_mode="HTML"
    )
    return SELECT_NETWORK


async def handle_network_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Tahap 2 Jual: Menyimpan jaringan, lalu meminta input jumlah koin crypto."""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split("_")
    symbol = parts[2]
    network = parts[3]
    
    context.user_data["sell_symbol"] = symbol
    context.user_data["sell_network"] = network
    
    keyboard = [
        [InlineKeyboardButton("🔙 Batal", callback_data="sell_cancel")],
        [get_owner_button()]
    ]
    
    await safe_edit_message(
        query,
        text=(
            f"📈 Anda memilih menjual: <b>{symbol} ({network})</b>\n\n"
            f"Berapa jumlah koin <b>{symbol}</b> yang ingin Anda jual?\n"
            f"<i>Ketik jumlah desimal di chat (contoh: 0.5 atau 10).</i>"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return INPUT_AMOUNT


async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Memproses nominal crypto, mengecek batas minimum order, lalu meminta info bank."""
    text_input = update.message.text
    
    is_valid, crypto_amount = validate_crypto_amount(text_input)
    if not is_valid:
        keyboard = [
            [InlineKeyboardButton("🔙 Batal", callback_data="sell_cancel")],
            [get_owner_button()]
        ]
        await update.message.reply_text(
            text=(
                "❌ <b>Jumlah Tidak Valid!</b>\n\n"
                "Format angka salah. Harap kirimkan angka desimal positif (contoh: <code>1.5</code> atau <code>50</code>):"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return INPUT_AMOUNT

    symbol = context.user_data["sell_symbol"]
    network = context.user_data["sell_network"]
    
    db = SessionLocal()
    try:
        # Fetch harga jual terkini (0% spread)
        price_data = await price_service.get_price(symbol, db)
        if not price_data:
            raise ValueError("Gagal mengambil harga jual")
            
        sell_price_idr = price_data["sell_price_idr"]
        
        # Hitung kotor nominal IDR
        gross_nominal_idr = int(crypto_amount * sell_price_idr)
        
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
        
        # Cek minimal transaksi Rp 5.000 bersih
        if net_nominal_idr < 5000:
            keyboard = [
                [InlineKeyboardButton("🔙 Batal", callback_data="sell_cancel")],
                [get_owner_button()]
            ]
            await update.message.reply_text(
                text=(
                    f"⚠️ <b>Nominal Terlalu Kecil!</b>\n\n"
                    f"Hasil penjualan bersih Anda adalah {format_idr(net_nominal_idr)}.\n"
                    f"Minimal penjualan bersih yang kami proses adalah <b>Rp 5.000</b>.\n"
                    f"Silakan masukkan jumlah koin yang lebih besar:"
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            db.close()
            return INPUT_AMOUNT

        # Simpan rincian perhitungan ke context
        context.user_data["sell_crypto_amount"] = crypto_amount
        context.user_data["sell_price_per_unit"] = sell_price_idr
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
        [InlineKeyboardButton("🔙 Batal", callback_data="sell_cancel")],
        [get_owner_button()]
    ]

    await update.message.reply_text(
        text=(
            f"🪙 <b>Simulasi Perhitungan Penjualan:</b>\n"
            f"• Aset Dijual: <code>{format_crypto(crypto_amount, symbol)} ({network})</code>\n"
            f"• Kurs Jual: <code>{format_idr(sell_price_idr)}</code>\n"
            f"• Nominal Kotor: <code>{format_idr(gross_nominal_idr)}</code>\n"
            f"• Fee Layanan: <code>{format_idr(fee_idr)}</code>\n"
            f"• <b>Nominal Bersih Anda Terima:</b> <b>{format_idr(net_nominal_idr)}</b>\n\n"
            f"Silakan ketik detail <b>Rekening Bank / E-Wallet Penerima</b> Anda.\n"
            f"<i>Format bebas, disarankan: Nama Bank, No Rekening, Atas Nama.</i>\n"
            f"<i>(Contoh: BCA, 882049281, Budi Santoso)</i>"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return INPUT_BANK


async def handle_bank_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menyimpan detail rekening bank dan menyajikan summary order."""
    bank_info = update.message.text.strip()
    
    # Validasi input sederhana (pastikan tidak kosong dan punya pemisah koma / spasi)
    if len(bank_info) < 8:
        keyboard = [
            [InlineKeyboardButton("🔙 Batal", callback_data="sell_cancel")],
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

    # Coba mem-parse bank info dengan koma
    parts = [p.strip() for p in bank_info.split(",") if p.strip()]
    if len(parts) >= 3:
        context.user_data["sell_bank_name"] = parts[0]
        context.user_data["sell_bank_acc"] = parts[1]
        context.user_data["sell_bank_holder"] = " ".join(parts[2:])
    else:
        # Jika format tidak pakai koma, simpan sebagai string utuh
        context.user_data["sell_bank_name"] = "Bank Lokal"
        context.user_data["sell_bank_acc"] = bank_info
        context.user_data["sell_bank_holder"] = update.effective_user.first_name

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
        bank_name=context.user_data["sell_bank_name"],
        bank_acc=context.user_data["sell_bank_acc"],
        bank_holder=context.user_data["sell_bank_holder"]
    )
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Konfirmasi Jual", callback_data="sell_confirm"),
            InlineKeyboardButton("❌ Batal", callback_data="sell_cancel")
        ],
        [get_owner_button()]
    ]
    
    await update.message.reply_text(
        text=summary,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return CONFIRM_ORDER


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
    
    hot_wallet = get_hot_wallet_address(network)
    
    db = SessionLocal()
    try:
        # Simpan order ke DB dengan status WAITING_CRYPTO_DEPOSIT
        # (deposit crypto akan diverifikasi otomatis oleh DepositDetector)
        order_data = {
            "order_id": order_id,
            "telegram_id": user_id,
            "order_type": "sell",
            "crypto_symbol": symbol,
            "network": network,
            "crypto_amount": Decimal(str(crypto_amount)),
            "price_per_unit": int(price_per_unit),
            "nominal_idr": int(nominal_idr),
            "fee_idr": int(fee_idr),
            "total_idr": int(net_idr), # Bersih diterima user
            "buyer_wallet": f"{context.user_data['sell_bank_name']} | {context.user_data['sell_bank_acc']} | {context.user_data['sell_bank_holder']}", # Kita simpan info bank disini
            "deposit_wallet": hot_wallet,
            "status": "WAITING_CRYPTO_DEPOSIT",
            "expired_at": datetime.utcnow() + timedelta(minutes=15) # Sell order expire dalam 15 menit
        }
        create_order(db, order_data)
        
        waiting_text = (
            f"📥 <b>ORDER PENJUALAN DIBUAT</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"Harap kirimkan tepat <b>{format_crypto(crypto_amount, symbol)}</b> ke alamat Hot Wallet kami di bawah ini:\n\n"
            f"Network: <b>{network}</b>\n"
            f"Alamat Hot Wallet:\n<code>{hot_wallet}</code>\n\n"
            f"⏳ *Batas Waktu:* 15 Menit\n\n"
            f"Sistem akan memantau transfer masuk secara otomatis. Jika Anda telah mengirim, Anda juga dapat mengirimkan "
            f"<b>TX Hash / Transaction ID</b> transaksi tersebut agar proses verifikasi lebih cepat."
        )
        
        keyboard = [
            [InlineKeyboardButton("✍️ Masukkan TX Hash Manual", callback_data="sell_input_tx")],
            [InlineKeyboardButton("❌ Batal Jual", callback_data="sell_cancel")],
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
        
        # Beritahu admin
        admin_alert = (
            f"🔔 <b>ORDER BARU DIBUAT (SELL)</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"User: {update.effective_user.name} (ID: {user_id})\n"
            f"Crypto Dijual: {format_crypto(crypto_amount, symbol)} ({network})\n"
            f"Rupiah Bersih Harus Dikirim: <b>{format_idr(net_idr)}</b>\n"
            f"Tujuan Rekening:\n"
            f"• {context.user_data['sell_bank_name']} - {context.user_data['sell_bank_acc']} a/n {context.user_data['sell_bank_holder']}"
        )
        await notify_admins(context.bot, admin_alert)
                
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
        [InlineKeyboardButton("🔙 Batal", callback_data="sell_cancel")],
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
    """Memproses input TX Hash dari user, mengupdate status order, dan meneruskan ke admin."""
    if not update.message or not update.message.text:
        await update.message.reply_text(
            "⚠️ Kirimkan <b>TX Hash</b> dalam bentuk teks. Contoh: <code>0xabc...def</code>",
            parse_mode="HTML"
        )
        return INPUT_TX_HASH

    tx_hash = update.message.text.strip()
    order_id = context.user_data["sell_order_id"]
    symbol = context.user_data["sell_symbol"]
    network = context.user_data["sell_network"]
    crypto_amount = context.user_data["sell_crypto_amount"]
    net_idr = context.user_data["sell_net_idr"]
    bank_info = f"{context.user_data['sell_bank_name']} | {context.user_data['sell_bank_acc']} | {context.user_data['sell_bank_holder']}"
    
    # Validasi format TX Hash sederhana (biasanya hex, minimal 10 karakter)
    if len(tx_hash) < 10:
        keyboard = [
            [InlineKeyboardButton("🔙 Batal", callback_data="sell_cancel")],
            [get_owner_button()]
        ]
        await update.message.reply_text(
            text=(
                "❌ <b>Format TX Hash Salah!</b>\n\n"
                "Karakter TX Hash terlalu pendek atau mengandung karakter ilegal.\n"
                "Silakan ketikkan ulang TX Hash yang valid:"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return INPUT_TX_HASH

    db = SessionLocal()
    try:
        # Simpan TX hash, biarkan DepositDetector memverifikasi on-chain
        # lalu mengirim notifikasi admin untuk transfer Rupiah.
        order = get_order_by_id(db, order_id)
        if order:
            order.deposit_tx_hash = tx_hash
            db.commit()

        response_user = (
            f"✅ <b>TX Hash Diterima!</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"TX Hash: <code>{tx_hash}</code>\n\n"
            f"🔍 Deposit sedang <b>diverifikasi otomatis</b> di blockchain. "
            f"Setelah terverifikasi, admin akan segera mentransfer Rupiah "
            f"ke rekening Anda dan kamu akan menerima notifikasi. 🙏"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")],
            [get_owner_button()]
        ]
        
        await update.message.reply_text(
            text=response_user,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error memproses input TX Hash jual: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Terjadi kesalahan internal saat menyimpan TX Hash.")
    finally:
        db.close()
        
    return ConversationHandler.END


async def cancel_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Membatalkan alur jual dan kembali ke menu utama."""
    query = update.callback_query
    if query:
        try:
            await query.answer("❌ Penjualan dibatalkan.", show_alert=True)
        except Exception:
            pass
            
        order_id = context.user_data.get("sell_order_id")
        if order_id:
            db = SessionLocal()
            try:
                from database.crud import update_order_status
                update_order_status(db, order_id, new_status="cancelled", failure_reason="Dibatalkan oleh pengguna")
            except Exception as e:
                logger.warning(f"Gagal membatalkan order sell {order_id}: {e}")
            finally:
                db.close()
                
        from bot.handlers.start import send_main_menu
        await send_main_menu(update, context)
    else:
        await update.message.reply_text(
            text="❌ Sesi penjualan dibatalkan.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")
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
            CallbackQueryHandler(cancel_sell, pattern="^menu_back$")
        ],
        SELECT_NETWORK: [
            CallbackQueryHandler(handle_network_selection, pattern="^sell_net_[A-Z0-9]+_[A-Z0-9]+$"),
            CallbackQueryHandler(start_sell_callback, pattern="^sell_back_symbols$"),
            CallbackQueryHandler(cancel_sell, pattern="^menu_back$")
        ],
        INPUT_AMOUNT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$")
        ],
        INPUT_BANK: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bank_input),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$")
        ],
        CONFIRM_ORDER: [
            CallbackQueryHandler(handle_order_confirmation, pattern="^sell_confirm$"),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$")
        ],
        WAITING_TX: [
            CallbackQueryHandler(prompt_tx_hash, pattern="^sell_input_tx$"),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$")
        ],
        INPUT_TX_HASH: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tx_hash_input),
            CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$")
        ]
    },
    fallbacks=[
        CallbackQueryHandler(cancel_sell, pattern="^sell_cancel$"),
        CommandHandler("cancel", cancel_sell)
    ]
)
