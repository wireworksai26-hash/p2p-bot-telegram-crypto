"""
bot/handlers/swap.py — Handler Tukar Antar Jaringan / OTC Convert (Bot 3 Feature)
===================================================================================
Alur Transaksi Convert/Swap (FULL OTOMATIS — tanpa verifikasi admin):
1. Buyer memilih koin/jaringan asal (misal SOL Solana).
2. Buyer memilih koin/jaringan tujuan (misal ETH Base).
3. Buyer memasukkan nominal koin asal dan alamat wallet tujuan (ETH Base buyer).
4. Bot membuat Quote (Expiry 30 Menit) dengan Fee Convert Tier (Min Rp 6.000, Max Rp 600.000).
5. Bot memberikan alamat deposit hot wallet seller (Solana seller).
6. Buyer menukar koin & memasukkan TX Hash deposit (atau auto-scan riwayat wallet).
7. Bot memverifikasi deposit ON-CHAIN secara otomatis (services.tx_verifier)
   -> Status `CRYPTO_CONFIRMED` & notif user.
8. Bot mengeksekusi payout otomatis koin tujuan ke wallet buyer
   -> Status `COMPLETED` + link Explorer + notif user.
9. Admin hanya menerima notifikasi informatif (tanpa tombol approve/reject).
"""

import asyncio
import logging
import re
from html import escape as _esc
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from config.settings import settings
from config.assets import MANUAL_PAYOUT_NETWORKS
from database.connection import SessionLocal
from database.models import User, Order, AuditLog
from services.price_service import price_service
from services.fee_service import calculate_fee_idr, eth_surcharge_note
from services.crypto_sender import CryptoSenderFactory
from services import tx_verifier
from services.detector import deposit_detector
from bot.keyboards.main_menu import get_owner_button
from bot.utils.formatter import format_crypto
from bot.utils.telegram_utils import notify_admins
from bot.utils.emojis import (
    E_SWAP,
    E_CHECK,
    E_CROSS,
    E_WARN,
    E_COIN,
    E_DOLLAR,
    E_MONEY,
    CUSTOM_EMOJI_IDS,
    get_coin_emoji_id,
    get_network_emoji_id,
    coin_button_text,
    network_button_text,
)


logger = logging.getLogger(__name__)

# State Conversation
SELECT_SRC_SYMBOL, SELECT_SRC_NET, SELECT_TGT_SYMBOL, SELECT_TGT_NET, INPUT_AMOUNT, INPUT_TARGET_ADDR, CONFIRM_SWAP, WAITING_DEPOSIT_HASH = range(8)

SUPPORTED_ASSETS = ["USDT", "USDC", "ETH", "SOL", "BNB", "TRX", "SUI", "TON", "MATIC", "ARB", "AVAX", "KAIA", "BERA", "APT", "HYPE", "USDG"]

NETWORKS_BY_SYMBOL = {
    "USDT": ["BSC", "POLYGON", "ARB", "TON", "SOLANA", "ETH"],
    "USDC": ["BASE", "ETH", "BSC", "ARB", "SOLANA", "POLYGON"],
    "ETH": ["BASE", "ARB", "OPTIMISM", "ROBINHOOD", "ETH"],
    "SOL": ["SOLANA"],
    "BNB": ["BSC"],
    "TRX": ["TRON"],
    "SUI": ["SUI"],
    "TON": ["TON"],
    "MATIC": ["POLYGON"],
    "ARB": ["ARB"],
    "AVAX": ["AVAX"],
    "KAIA": ["KAIA"],
    "BERA": ["BERA"],
    "APT": ["APTOS"],
    "HYPE": ["HYPEREVM"],
    "USDG": ["ROBINHOOD"],
}


async def start_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Langkah 1: Mulai alur Swap / Convert — Pilih Koin Asal."""
    query = update.callback_query
    if query:
        await query.answer()

    keyboard = []
    row = []
    for i, symbol in enumerate(SUPPORTED_ASSETS, 1):
        row.append(
            InlineKeyboardButton(
                text=coin_button_text(symbol),
                callback_data=f"swap_src_sym_{symbol}",
                icon_custom_emoji_id=get_coin_emoji_id(symbol)
            )
        )
        if i % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("Batal", callback_data="cancel_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))])

    text = f"{E_SWAP()} <b>[TUKAR ANTAR JARINGAN / OTC CONVERT]</b>\n\nSilakan pilih <b>Koin Asal</b> yang ingin kamu kirim:"
    
    if query:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        
    return SELECT_SRC_SYMBOL


async def select_src_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    symbol = query.data.replace("swap_src_sym_", "")
    context.user_data["swap_src_symbol"] = symbol

    networks = NETWORKS_BY_SYMBOL.get(symbol, ["BSC"])
    keyboard = [
        [
            InlineKeyboardButton(
                text=network_button_text(net),
                callback_data=f"swap_src_net_{net}",
                icon_custom_emoji_id=get_network_emoji_id(net)
            )
        ]
        for net in networks
    ]
    keyboard.append([InlineKeyboardButton("Batal", callback_data="cancel_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))])

    await query.edit_message_text(
        f"Koin Asal: <b>{symbol}</b>\n\nSilakan pilih <b>Jaringan Koin Asal</b>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_SRC_NET


async def select_src_net(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    net = query.data.replace("swap_src_net_", "")
    context.user_data["swap_src_network"] = net

    keyboard = []
    row = []
    src_symbol = context.user_data.get("swap_src_symbol")
    for i, symbol in enumerate(SUPPORTED_ASSETS, 1):
        if symbol == src_symbol:
            # Bridge: koin sama boleh dipilih bila punya lebih dari satu jaringan
            if len(NETWORKS_BY_SYMBOL.get(symbol, [])) < 2:
                continue
            label = f"{coin_button_text(symbol)} (Bridge)"
        else:
            label = coin_button_text(symbol)
        row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"swap_tgt_sym_{symbol}",
                icon_custom_emoji_id=get_coin_emoji_id(symbol)
            )
        )
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("Batal", callback_data="cancel_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))])

    await query.edit_message_text(
        f"Koin Asal: <b>{context.user_data['swap_src_symbol']} ({net})</b>\n\nSilakan pilih <b>Koin Tujuan (Target)</b>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_TGT_SYMBOL


async def select_tgt_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    symbol = query.data.replace("swap_tgt_sym_", "")
    context.user_data["swap_tgt_symbol"] = symbol

    networks = NETWORKS_BY_SYMBOL.get(symbol, ["BSC"])
    # Bridge: jaringan asal tidak boleh jadi tujuan saat koin sama
    if symbol == context.user_data.get("swap_src_symbol") and len(networks) > 1:
        networks = [n for n in networks if n != context.user_data.get("swap_src_network")]
    keyboard = [
        [
            InlineKeyboardButton(
                text=network_button_text(net),
                callback_data=f"swap_tgt_net_{net}",
                icon_custom_emoji_id=get_network_emoji_id(net)
            )
        ]
        for net in networks
    ]
    keyboard.append([InlineKeyboardButton("Batal", callback_data="cancel_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))])

    await query.edit_message_text(
        f"Koin Asal: <b>{context.user_data['swap_src_symbol']} ({context.user_data['swap_src_network']})</b>\n"
        f"Koin Tujuan: <b>{symbol}</b>\n\nSilakan pilih <b>Jaringan Koin Tujuan</b>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_TGT_NET


def parse_convert_amount(raw_text: str, src_idr_price: float, usdt_idr_rate: float, src_sym: str = "") -> tuple[float, int, str]:
    """
    Parse input nominal convert dari user:
    - USD ($10, 10$, 10 usd, 10 usdt, usd 25)
    - IDR (50000, 50.000, Rp 50.000, 50k, 50rb, 1.5jt)
    - Crypto Amount (0.05, 1.5, 4.2, 10)
    """
    text = raw_text.strip()
    src_sym_upper = src_sym.upper() if src_sym else ""

    # 1. USD pattern: $10, 10$, 10 usd, 10 usdt, usd 10, $ 10.5
    usd_pattern = r'^(?:\$\s*([0-9]+(?:[\.,][0-9]+)?)|([0-9]+(?:[\.,][0-9]+)?)\s*(?:\$|usd|usdt)|(?:usd|usdt)\s*([0-9]+(?:[\.,][0-9]+)?))$'
    m_usd = re.match(usd_pattern, text, re.IGNORECASE)
    if m_usd:
        num_str = (m_usd.group(1) or m_usd.group(2) or m_usd.group(3)).replace(',', '.')
        usd_val = float(num_str)
        if usd_val <= 0:
            raise ValueError("Nominal USD harus lebih besar dari 0")
        nominal_idr = int(usd_val * usdt_idr_rate)
        if src_sym_upper in ["USDT", "USDC"]:
            src_amount = usd_val
        else:
            src_amount = nominal_idr / src_idr_price if src_idr_price > 0 else 0
        return src_amount, nominal_idr, "USD"

    # 2. IDR suffixes: 50k, 50rb, 1jt, 1m, 1.5jt
    clean = re.sub(r'^(?:rp|idr)\.?\s*', '', text, flags=re.IGNORECASE).strip()
    m_suffix = re.match(r'^([0-9]+(?:[\.,][0-9]+)?)\s*(k|rb|ribu|jt|juta|m)$', clean, re.IGNORECASE)
    if m_suffix:
        num = float(m_suffix.group(1).replace(',', '.'))
        suf = m_suffix.group(2).lower()
        multiplier = 1000 if suf in ['k', 'rb', 'ribu'] else 1000000
        nominal_idr = int(num * multiplier)
        if nominal_idr <= 0:
            raise ValueError("Nominal Rupiah harus lebih besar dari 0")
        src_amount = nominal_idr / src_idr_price if src_idr_price > 0 else 0
        return src_amount, nominal_idr, "IDR"

    # 3. IDR thousand dot notation: 50.000, 1.000.000 (starts with 1-9)
    if re.match(r'^[1-9]\d{0,2}(?:\.\d{3})+$', clean):
        nominal_idr = int(clean.replace('.', ''))
        if nominal_idr <= 0:
            raise ValueError("Nominal Rupiah harus lebih besar dari 0")
        src_amount = nominal_idr / src_idr_price if src_idr_price > 0 else 0
        return src_amount, nominal_idr, "IDR"

    # 4. Explicit Rp / IDR prefix: Rp 50000, IDR 10000
    if re.match(r'^(?:rp|idr)\.?\s*[0-9]+', text, re.IGNORECASE):
        num_str = re.sub(r'[^0-9]', '', text)
        if num_str:
            nominal_idr = int(num_str)
            if nominal_idr <= 0:
                raise ValueError("Nominal Rupiah harus lebih besar dari 0")
            src_amount = nominal_idr / src_idr_price if src_idr_price > 0 else 0
            return src_amount, nominal_idr, "IDR"

    # 5. General number: crypto amount or raw IDR (if >= 1000)
    num_str = text.replace(',', '.')
    val = float(num_str)
    if val <= 0:
        raise ValueError("Nominal harus lebih besar dari 0")
    if val >= 1000 and src_sym_upper not in ["USDT", "USDC"]:
        nominal_idr = int(val)
        src_amount = nominal_idr / src_idr_price if src_idr_price > 0 else 0
        return src_amount, nominal_idr, "IDR"
    else:
        src_amount = val
        if src_sym_upper in ["USDT", "USDC"]:
            nominal_idr = int(src_amount * usdt_idr_rate)
        else:
            nominal_idr = int(src_amount * src_idr_price)
        return src_amount, nominal_idr, "CRYPTO"


async def select_tgt_net(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    net = query.data.replace("swap_tgt_net_", "")
    if net in MANUAL_PAYOUT_NETWORKS:
        await query.edit_message_text("Pengiriman otomatis jaringan tujuan ini belum tersedia. Hubungi admin; jangan menyetor crypto dahulu.")
        return ConversationHandler.END
    context.user_data["swap_tgt_network"] = net

    src_sym = context.user_data["swap_src_symbol"]
    src_net = context.user_data["swap_src_network"]
    tgt_sym = context.user_data["swap_tgt_symbol"]
    tgt_net = net

    rate_info = ""
    try:
        src_price_info = await price_service.get_price(src_sym)
        tgt_price_info = await price_service.get_price(tgt_sym)
        if src_price_info and tgt_price_info:
            src_mkt = src_price_info.get("market_price_idr", 0)
            tgt_mkt = tgt_price_info.get("market_price_idr", 1)
            usdt_rate = src_price_info.get("usdt_idr_rate", 16000)

            src_usd = 1.0 if src_sym.upper() in ["USDT", "USDC"] else (src_mkt / usdt_rate if usdt_rate else 0)
            tgt_usd = 1.0 if tgt_sym.upper() in ["USDT", "USDC"] else (tgt_mkt / usdt_rate if usdt_rate else 0)

            if src_sym.upper() in ["USDT", "USDC"]:
                rate_info = f"• <b>Kurs {src_sym}:</b> <code>$1.00 (Rp {int(src_mkt):,})</code>\n"
            else:
                rate_info = f"• <b>Kurs {src_sym}:</b> <code>${src_usd:,.4f} (Rp {int(src_mkt):,})</code>\n"

            if tgt_sym.upper() in ["USDT", "USDC"]:
                rate_info += f"• <b>Kurs {tgt_sym}:</b> <code>$1.00 (Rp {int(tgt_mkt):,})</code>\n"
            else:
                rate_info += f"• <b>Kurs {tgt_sym}:</b> <code>${tgt_usd:,.4f} (Rp {int(tgt_mkt):,})</code>\n"

            if tgt_mkt > 0:
                direct_ratio = src_mkt / tgt_mkt
                rate_info += f"• <b>Estimasi Rate:</b> <code>1 {src_sym} ≈ {direct_ratio:.6f} {tgt_sym}</code>\n"
    except Exception as exc:
        logger.warning(f"Error format rate info di swap: {exc}")

    keyboard = [
        [InlineKeyboardButton("Batal Transaksi", callback_data="cancel_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
        [get_owner_button()]
    ]

    await query.edit_message_text(
        f"🔄 <b>Konfigurasi Convert:</b>\n"
        f"• <b>Dari:</b> <code>{src_sym} ({src_net})</code>\n"
        f"• <b>Ke:</b> <code>{tgt_sym} ({tgt_net})</code>\n"
        f"{rate_info}\n"
        f"💡 <b>Pilihan Format Input:</b>\n"
        f"1️⃣ <b>Jumlah Koin / Altcoin:</b>\n"
        f"   Ketik jumlah koin asal yang ingin ditukar (misal: <code>0.5</code>, <code>2.5</code>, <code>10</code>)\n"
        f"2️⃣ <b>Nominal Rupiah (IDR):</b>\n"
        f"   Ketik nominal rupiah yang ingin dikonversi (misal: <code>50000</code>, <code>100.000</code>, <code>50k</code>)\n"
        f"3️⃣ <b>Nominal USD ($ / USDT):</b>\n"
        f"   Ketik nominal dalam dollar (misal: <code>$10</code>, <code>25 USD</code>, <code>10.5$</code>)\n\n"
        f"Silakan ketikkan nominal yang ingin Anda convert ke chat:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return INPUT_AMOUNT


async def input_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_input = update.message.text.strip()
    src_sym = context.user_data["swap_src_symbol"]
    src_net = context.user_data["swap_src_network"]
    tgt_sym = context.user_data["swap_tgt_symbol"]
    tgt_net = context.user_data["swap_tgt_network"]

    # Ambil harga koin asal & koin tujuan secara realtime
    src_price_info = await price_service.get_price(src_sym)
    tgt_price_info = await price_service.get_price(tgt_sym)

    if not src_price_info or not tgt_price_info:
        await update.message.reply_text("❌ Gagal mengambil harga realtime. Silakan coba beberapa saat lagi.")
        return ConversationHandler.END

    src_market_price = src_price_info.get("market_price_idr") or src_price_info.get("sell_price_idr", 0)
    tgt_market_price = tgt_price_info.get("market_price_idr") or tgt_price_info.get("buy_price_idr", 0)
    usdt_idr_rate = src_price_info.get("usdt_idr_rate", 16000.0)

    try:
        src_amount, nominal_idr, mode = parse_convert_amount(text_input, src_market_price, usdt_idr_rate, src_sym)
    except (ValueError, Exception):
        keyboard = [
            [InlineKeyboardButton("Batal Transaksi", callback_data="cancel_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
        await update.message.reply_text(
            "❌ <b>Format input tidak valid!</b>\n\n"
            "Silakan masukkan salah satu format berikut:\n"
            "• <b>Jumlah Koin:</b> contoh <code>0.5</code> atau <code>10</code>\n"
            "• <b>Nominal Rupiah:</b> contoh <code>50000</code>, <code>100.000</code>, atau <code>50k</code>\n"
            "• <b>Nominal USD:</b> contoh <code>$10</code> atau <code>25 USD</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return INPUT_AMOUNT

    # Hitung Fee Convert Tier (Min Rp 6.000, Max Rp 1.010.000)
    try:
        fee_idr = calculate_fee_idr(
            nominal_idr,
            category="CONVERT",
            symbol=tgt_sym,
            network=tgt_net,
            is_outgoing=True
        )
    except ValueError as fee_err:
        keyboard = [
            [InlineKeyboardButton("Batal Transaksi", callback_data="cancel_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
        await update.message.reply_text(
            f"⚠️ {str(fee_err)}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return INPUT_AMOUNT

    # Perhitungan koin tujuan yang diterima (berdasarkan market rate murni)
    net_idr_for_target = nominal_idr - fee_idr
    tgt_amount = net_idr_for_target / tgt_market_price if tgt_market_price > 0 else 0

    # Store exactly the amount shown in deposit instructions. Never request a
    # rounded-down display amount while expecting a higher hidden amount.
    src_amount = float(Decimal(str(src_amount)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))
    if src_amount <= 0 or tgt_amount <= 0:
        await update.message.reply_text("Nominal terlalu kecil untuk convert.")
        return INPUT_AMOUNT
    context.user_data["swap_src_amount"] = src_amount
    context.user_data["swap_nominal_idr"] = nominal_idr
    context.user_data["swap_fee_idr"] = fee_idr
    context.user_data["swap_tgt_amount"] = tgt_amount

    keyboard = [
        [InlineKeyboardButton("Batal Transaksi", callback_data="cancel_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
        [get_owner_button()]
    ]

    await update.message.reply_text(
        f"📝 <b>[INPUT WALLET TUJUAN]</b>\n\n"
        f"Kamu akan menukar:\n"
        f"<b>{src_amount:.6f} {src_sym} ({src_net})</b> (~Rp {nominal_idr:,})\n"
        f"Fee Convert: <b>Rp {fee_idr:,}</b>{eth_surcharge_note(tgt_net)}\n"
        f"Estimasi yang diterima: <b>{tgt_amount:.6f} {tgt_sym} ({tgt_net})</b>\n\n"
        f"Silakan masukkan <b>Alamat Wallet {tgt_sym} ({tgt_net})</b> tujuan milikmu:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return INPUT_TARGET_ADDR


async def input_target_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_addr = update.message.text.strip()
    tgt_net = context.user_data["swap_tgt_network"]

    # Validasi alamat wallet tujuan
    sender = CryptoSenderFactory.get_sender(tgt_net)
    if not sender.validate_address(target_addr):
        keyboard = [
            [InlineKeyboardButton("Batal Transaksi", callback_data="cancel_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
        await update.message.reply_text(
            f"❌ Format alamat wallet {tgt_net} tidak valid! Silakan periksa kembali dan masukkan ulang:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return INPUT_TARGET_ADDR

    context.user_data["swap_target_addr"] = target_addr

    src_sym = context.user_data["swap_src_symbol"]
    src_net = context.user_data["swap_src_network"]
    src_amount = context.user_data["swap_src_amount"]
    tgt_sym = context.user_data["swap_tgt_symbol"]
    tgt_amount = context.user_data["swap_tgt_amount"]
    nominal_idr = context.user_data["swap_nominal_idr"]
    fee_idr = context.user_data["swap_fee_idr"]

    # Ambil deposit wallet seller
    src_sender = CryptoSenderFactory.get_sender(src_net)
    seller_deposit_wallet = getattr(src_sender, "wallet_address", settings.EVM_WALLET_ADDRESS)

    context.user_data["swap_seller_deposit_wallet"] = seller_deposit_wallet

    keyboard = [
        [InlineKeyboardButton("Ya, Konfirmasi & Dapatkan Alamat Setor", callback_data="confirm_swap_order", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CHECK", "5237699328843200968"))],
        [InlineKeyboardButton("Batal", callback_data="cancel_swap", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]
    ]

    await update.message.reply_text(
        f"📑 <b>[RINGKASAN QUOTE CONVERT]</b>\n"
        f"<i>Masa berlaku quote: 30 Menit</i>\n\n"
        f"<b>Kirim:</b> {src_amount:.6f} {src_sym} ({src_net})\n"
        f"<b>Nilai IDR:</b> Rp {nominal_idr:,}\n"
        f"<b>Fee Convert:</b> Rp {fee_idr:,}{eth_surcharge_note(tgt_net)}\n"
        f"<b>Terima:</b> ~{tgt_amount:.6f} {tgt_sym} ({tgt_net})\n"
        f"<b>Wallet Tujuan:</b> <code>{target_addr}</code>\n\n"
        f"Apakah data di atas sudah sesuai?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRM_SWAP


async def confirm_swap_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    telegram_id = query.from_user.id
    order_id = f"SWAP-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{telegram_id % 1000:03d}"

    src_sym = context.user_data["swap_src_symbol"]
    src_net = context.user_data["swap_src_network"]
    src_amount = context.user_data["swap_src_amount"]
    tgt_sym = context.user_data["swap_tgt_symbol"]
    tgt_net = context.user_data["swap_tgt_network"]
    tgt_amount = context.user_data["swap_tgt_amount"]
    nominal_idr = context.user_data["swap_nominal_idr"]
    fee_idr = context.user_data["swap_fee_idr"]
    target_addr = context.user_data["swap_target_addr"]
    seller_deposit_wallet = context.user_data["swap_seller_deposit_wallet"]
    if tgt_net in MANUAL_PAYOUT_NETWORKS or not seller_deposit_wallet:
        await query.edit_message_text("Jaringan/wallet belum siap untuk auto-convert. Jangan menyetor crypto; hubungi admin.")
        return ConversationHandler.END
    from services.wallet_sync import sync_wallet_balances
    from database.crud import get_available_inventory
    stock_symbol = "MATIC" if tgt_net == "POLYGON" and tgt_sym == "POL" else tgt_sym
    await sync_wallet_balances([(stock_symbol, tgt_net)])
    with SessionLocal() as stock_db:
        available = get_available_inventory(stock_db, tgt_net, stock_symbol)
    if available is None or available < Decimal(str(tgt_amount)):
        await query.edit_message_text("Stok tujuan tidak cukup atau belum terverifikasi. Order belum dibuat; jangan menyetor crypto.")
        return ConversationHandler.END

    quoted_at = datetime.utcnow()
    expires_at = quoted_at + timedelta(minutes=30)

    db = SessionLocal()
    try:
        # User record
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            user = User(
                telegram_id=telegram_id,
                username=query.from_user.username,
                full_name=query.from_user.full_name
            )
            db.add(user)
            db.commit()

        # Create Order
        order = Order(
            order_id=order_id,
            telegram_id=telegram_id,
            order_type="swap",
            crypto_symbol=src_sym,
            network=src_net,
            crypto_amount=src_amount,
            target_crypto_symbol=tgt_sym,
            target_network=tgt_net,
            target_crypto_amount=tgt_amount,
            price_per_unit=0,
            nominal_idr=nominal_idr,
            fee_idr=fee_idr,
            total_idr=nominal_idr,
            fee_category="CONVERT",
            buyer_wallet=target_addr,
            deposit_wallet=seller_deposit_wallet,
            status="WAITING_CRYPTO_DEPOSIT",
            quoted_at=quoted_at,
            quote_expires_at=expires_at
        )
        db.add(order)
        
        audit = AuditLog(
            telegram_id=telegram_id,
            action="CREATE_SWAP_ORDER",
            order_id=order_id,
            from_status="DRAFT",
            to_status="WAITING_CRYPTO_DEPOSIT",
            details=f"Convert {src_amount} {src_sym} ({src_net}) -> {tgt_amount} {tgt_sym} ({tgt_net})"
        )
        db.add(audit)
        db.commit()

    except Exception as e:
        db.rollback()
        logger.error(f"Error creating swap order: {e}")
        await query.edit_message_text("❌ Gagal membuat order swap. Silakan coba lagi.")
        db.close()
        return ConversationHandler.END
    finally:
        db.close()

    context.user_data["active_swap_order_id"] = order_id

    keyboard = [
        [InlineKeyboardButton("⛓ Salin Alamat Setoran", copy_text=CopyTextButton(text=seller_deposit_wallet))],
        [InlineKeyboardButton("Masukkan TX Hash / Bukti Setor", callback_data=f"input_swap_tx_{order_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("HISTORY", "5373251851074415873"))],
        [InlineKeyboardButton("Batal Order", callback_data=f"cancel_swap_order_{order_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]
    ]

    await query.edit_message_text(
        f"✅ <b>ORDER CONVERT BERHASIL DIBUAT!</b>\n"
        f"<b>ID Order:</b> <code>{order_id}</code>\n"
        f"<b>Batas Waktu Quote:</b> 30 Menit\n\n"
        f"📌 <b>INSTRUKSI SETORAN DANA:</b>\n"
        f"Silakan kirim tepat <code>{src_amount:.6f}</code> <b>{src_sym} ({src_net})</b> ke alamat wallet seller berikut:\n\n"
        f"<code>{seller_deposit_wallet}</code>\n\n"
        f"Setelah mengirim, tekan tombol di bawah ini untuk memasukkan TX Hash atau foto bukti pengirimanmu:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_DEPOSIT_HASH


async def prompt_input_tx_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = query.data.replace("input_swap_tx_", "")
    context.user_data["active_swap_order_id"] = order_id

    keyboard = [
        [InlineKeyboardButton("Batal Order", callback_data=f"cancel_swap_order_{order_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
        [get_owner_button()]
    ]

    await query.edit_message_text(
        f"🔗 <b>INPUT TX HASH / BUKTI SETOR CONVERT</b>\n\n"
        f"ID Order: <code>{order_id}</code>\n\n"
        f"Silakan kirimkan <b>TX Hash (Hash Transaksi)</b> atau <b>Foto Screenshot Bukti Pengiriman</b> ke chat bot ini:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_DEPOSIT_HASH


async def input_deposit_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = context.user_data.get("active_swap_order_id")
    if not order_id and update.callback_query:
        order_id = update.callback_query.data.replace("input_swap_tx_", "")

    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.order_id == order_id).first() if order_id else None
        if not order or order.telegram_id != update.effective_user.id or order.status != "WAITING_CRYPTO_DEPOSIT":
            await update.message.reply_text("❌ Order tidak ditemukan atau sudah kadaluarsa.")
            return ConversationHandler.END

        deposit_proof = ""
        photo_file_id = None

        if update.message and update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            deposit_proof = f"PHOTO:{photo_file_id}"
        elif update.message and update.message.text:
            try:
                deposit_proof = tx_verifier.normalize_tx_hash(order.network, update.message.text)
            except (ValueError, TypeError):
                await update.message.reply_text("⚠️ Hash/link explorer tidak valid untuk jaringan ini.")
                return WAITING_DEPOSIT_HASH
        else:
            await update.message.reply_text("⚠️ Silakan kirimkan teks TX Hash atau unggah foto screenshot bukti transfer.")
            return WAITING_DEPOSIT_HASH

        # Simpan bukti. Status tetap WAITING_CRYPTO_DEPOSIT hingga
        # deposit terverifikasi on-chain (bypass verifikasi admin).
        if photo_file_id:
            order.deposit_proof_file_id = photo_file_id
        else:
            order.deposit_tx_hash = deposit_proof
        db.commit()

        # --- Verifikasi on-chain langsung (respons instan) ---
        verified_result = None
        if not deposit_proof.startswith("PHOTO:"):
            verified_result = await tx_verifier.verify_deposit(
                network=order.network,
                symbol=order.crypto_symbol,
                tx_hash=deposit_proof,
                expected_wallet=order.deposit_wallet,
                expected_amount=float(order.crypto_amount),
                not_before=order.created_at,
                not_after=order.quote_expires_at or order.expired_at,
            )

        menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]])
        if verified_result and verified_result.get("verified"):
            # Konfirmasi + eksekusi payout otomatis (kirim koin tujuan)
            # Pesan status verifikasi dan hasil convert dikirimkan oleh deposit_detector._confirm_order
            await deposit_detector._confirm_order(
                db, order, deposit_proof, verified_result, context.application
            )
        else:
            alasan = (verified_result or {}).get("reason") or ""
            tertunda = (
                deposit_proof.startswith("PHOTO:")
                or not alasan
                or alasan.startswith("Menunggu konfirmasi")
                or "belum dapat diverifikasi" in alasan
            )
            if not tertunda:
                await update.message.reply_text(
                    f"❌ <b>Deposit Belum Bisa Diverifikasi</b>\n\n"
                    f"ID Order: <code>{order.order_id}</code>\n"
                    f"TX Hash: <code>{deposit_proof}</code>\n\n"
                    f"Alasan: <b>{alasan}</b>\n\n"
                    f"Silakan periksa kembali transaksimu (nominal tepat "
                    f"<code>{float(order.crypto_amount):g} {order.crypto_symbol}</code>, alamat tujuan "
                    f"<code>{order.deposit_wallet}</code>, jaringan {order.network}) lalu kirim bukti "
                    f"yang benar, atau tekan Batal Order.",
                    parse_mode="HTML",
                    reply_markup=menu_keyboard
                )
                return ConversationHandler.END
            # Belum terlihat di chain -> verifikasi cepat berulang sampai ~1 menit
            await _notify_admin_deposit_pending(order, deposit_proof, photo_file_id, context)
            await update.message.reply_text(
                f"📥 <b>Bukti Setoran Diterima!</b>\n\n"
                f"ID Order: <code>{order.order_id}</code>\n"
                f"TX Hash: <code>{deposit_proof}</code>\n\n"
                f"🔍 Deposit sedang <b>diverifikasi otomatis</b> di blockchain "
                f"(<b>biasanya di bawah 1 menit</b>). Kamu akan otomatis dapat kabar berikutnya: "
                f"TERVERIFIKASI lalu koin tujuan <b>{order.target_crypto_symbol} ({order.target_network})</b> "
                f"langsung dikirim ke walletmu, <b>tanpa konfirmasi admin</b>.",
                parse_mode="HTML",
                reply_markup=menu_keyboard
            )
            asyncio.create_task(deposit_detector.verifikasi_cepat(order.order_id, context.application))
    except Exception as e:
        db.rollback()
        logger.error(f"Error handling deposit proof for swap: {e}", exc_info=True)
        await update.message.reply_text("❌ Terjadi kesalahan saat menyimpan bukti transfer.")
    finally:
        db.close()

    return ConversationHandler.END


async def _notify_admin_deposit_pending(order, deposit_proof, photo_file_id, context):
    """Notifikasi informatif ke admin dilengkapi tombol Approve jika verifikasi instan dibutuhkan."""
    try:
        admin_msg = (
            f"📥 <b>[DEPOSIT SETORAN SWAP DITERIMA]</b>\n\n"
            f"ID Order: <code>{order.order_id}</code>\n"
            f"User: <code>{order.telegram_id}</code>\n"
            f"Deposit Asal: <b>{order.crypto_amount} {order.crypto_symbol}</b> ({order.network})\n"
            f"Koin Tujuan: <b>{order.target_crypto_amount} {order.target_crypto_symbol}</b> ({order.target_network})\n"
            f"Wallet Tujuan: <code>{order.buyer_wallet}</code>\n"
            f"Bukti/TX Hash: <code>{_esc(str(deposit_proof or '-'))}</code>\n\n"
            f"ℹ️ <i>Belum terverifikasi. Tombol admin hanya memeriksa ulang blockchain, bukan menyetujui foto sebagai deposit.</i>"
        )
        admin_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Cek Ulang Deposit", callback_data=f"admin_approve_swap_{order.order_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CHECK", "5237699328843200968")),
                InlineKeyboardButton("Tolak", callback_data=f"admin_reject_swap_{order.order_id}", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("CROSS", "5462882007451185227"))
            ]
        ])
        if photo_file_id:
            try:
                await context.bot.send_photo(
                    chat_id=settings.ADMIN_GROUP_ID,
                    photo=photo_file_id,
                    caption=admin_msg,
                    parse_mode="HTML",
                    reply_markup=admin_keyboard
                )
            except Exception:
                from bot.utils.telegram_utils import kirim_ke_topik
                await kirim_ke_topik(context.bot, kind="convert", photo=photo_file_id,
                                     text=admin_msg, reply_markup=admin_keyboard)
                for admin_id in settings.ADMIN_CHAT_IDS:
                    try:
                        await context.bot.send_photo(
                            chat_id=admin_id,
                            photo=photo_file_id,
                            caption=admin_msg,
                            parse_mode="HTML",
                            reply_markup=admin_keyboard
                        )
                    except Exception:
                        pass
        else:
            await notify_admins(context.bot, admin_msg, reply_markup=admin_keyboard, kind="convert")
    except Exception as admin_err:
        logger.error(f"Error sending admin notification for swap proof: {admin_err}")


async def cancel_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass
        from bot.handlers.start import send_main_menu
        await send_main_menu(update, context)
    else:
        await update.message.reply_text(
            text="❌ Sesi convert dibatalkan.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
            ]])
        )
    return ConversationHandler.END

swap_conv_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_swap, pattern="^start_swap$"),
        CommandHandler("swap", start_swap),
        CommandHandler("convert", start_swap),
    ],
    states={
        SELECT_SRC_SYMBOL: [
            CallbackQueryHandler(select_src_symbol, pattern="^swap_src_sym_"),
            CallbackQueryHandler(cancel_swap, pattern="^cancel_swap$"),
            CallbackQueryHandler(cancel_swap, pattern="^menu_back$"),
        ],
        SELECT_SRC_NET: [
            CallbackQueryHandler(select_src_net, pattern="^swap_src_net_"),
            CallbackQueryHandler(cancel_swap, pattern="^cancel_swap$"),
            CallbackQueryHandler(cancel_swap, pattern="^menu_back$"),
        ],
        SELECT_TGT_SYMBOL: [
            CallbackQueryHandler(select_tgt_symbol, pattern="^swap_tgt_sym_"),
            CallbackQueryHandler(cancel_swap, pattern="^cancel_swap$"),
            CallbackQueryHandler(cancel_swap, pattern="^menu_back$"),
        ],
        SELECT_TGT_NET: [
            CallbackQueryHandler(select_tgt_net, pattern="^swap_tgt_net_"),
            CallbackQueryHandler(cancel_swap, pattern="^cancel_swap$"),
            CallbackQueryHandler(cancel_swap, pattern="^menu_back$"),
        ],
        INPUT_AMOUNT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, input_amount),
            CallbackQueryHandler(cancel_swap, pattern="^cancel_swap$"),
            CallbackQueryHandler(cancel_swap, pattern="^menu_back$"),
        ],
        INPUT_TARGET_ADDR: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, input_target_addr),
            CallbackQueryHandler(cancel_swap, pattern="^cancel_swap$"),
            CallbackQueryHandler(cancel_swap, pattern="^menu_back$"),
        ],
        CONFIRM_SWAP: [
            CallbackQueryHandler(confirm_swap_order, pattern="^confirm_swap_order$"),
            CallbackQueryHandler(cancel_swap, pattern="^cancel_swap$"),
            CallbackQueryHandler(cancel_swap, pattern="^menu_back$"),
        ],
        WAITING_DEPOSIT_HASH: [
            CallbackQueryHandler(prompt_input_tx_hash, pattern="^input_swap_tx_"),
            MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, input_deposit_hash),
            CallbackQueryHandler(cancel_swap, pattern="^cancel_swap$"),
            CallbackQueryHandler(cancel_swap, pattern="^cancel_swap_order_"),
            CallbackQueryHandler(cancel_swap, pattern="^menu_back$"),
        ],
    },
    fallbacks=[
        CallbackQueryHandler(cancel_swap, pattern="^cancel_swap$"),
        CallbackQueryHandler(cancel_swap, pattern="^cancel_swap_order_"),
        CallbackQueryHandler(cancel_swap, pattern="^menu_back$"),
        CallbackQueryHandler(cancel_swap, pattern="^(menu_buy|menu_sell|menu_balance|menu_price|menu_stocks|menu_history|menu_snk)$"),
        CommandHandler("cancel", cancel_swap),
        CommandHandler("start", cancel_swap),
    ],
    allow_reentry=True
)

