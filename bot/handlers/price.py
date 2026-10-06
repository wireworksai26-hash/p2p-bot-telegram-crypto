"""
bot/handlers/price.py — Handler Cek Harga Crypto Hari Ini & Price List Fee.
==========================================================================
Menampilkan daftar harga crypto terupdate sesuai format resmi permintaan client:
- Pasangan koin terkurasi (USDT TON, ETH, BNB, SOL, AVAX, TRX, MATIC, G, BASE, ARB)
- Harga Beli & Jual terupdate realtime
- Opsi melihat Price List Fee transaksi resmi
"""

import logging
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database.connection import SessionLocal
from services.price_service import price_service
from services.fee_service import (
    ALTCOIN_FEE_TIERS,
    ALTCOIN_PERCENT_TIERS,
    CONVERT_FEE_TIERS,
    CONVERT_PERCENT_TIERS,
    USD_FEE_TIERS,
    USD_PERCENT_TIERS,
)
from bot.keyboards.main_menu import get_owner_button
from bot.utils.formatter import format_idr, format_datetime
from bot.utils.emojis import (
    E_CHART,
    E_COIN,
    E_DOLLAR,
    E_CART,
    E_SWAP,
    E_BOX,
    E_WARN,
    E_CALENDAR,
    E_SPARKLES,
    CUSTOM_EMOJI_IDS,
    get_coin_emoji,
)

logger = logging.getLogger(__name__)

# Seluruh simbol yang diperjualbelikan (menu Beli/Jual) ditampilkan saat Cek Harga.
CURATED_PRICE_ASSETS = [
    ("USDT", "BSC", "💵", "USDT (BSC)"),
    ("USDC", "BSC", "💲", "USDC (BSC)"),
    ("ETH", "ETH", "🔷", "ETH (ETH)"),
    ("SOL", "SOLANA", "🟣", "SOL (SOLANA)"),
    ("TRX", "TRON", "🔺", "TRX (TRON)"),
    ("BNB", "BSC", "🟡", "BNB (BSC)"),
    ("SUI", "SUI", "💧", "SUI (SUI)"),
    ("TON", "TON", "💎", "TON (TON)"),
    ("POL", "POLYGON", "🟪", "POL (POLYGON)"),
    ("ARB", "ARB", "🔷", "ARB (ARB)"),
    ("AVAX", "AVAX", "🔴", "AVAX (AVAX)"),
    ("KAIA", "KAIA", "🌱", "KAIA (KAIA)"),
    ("BERA", "BERA", "🐻", "BERA (BERA)"),
    ("APT", "APTOS", "⚫", "APT (APTOS)"),
    ("HYPE", "HYPEREVM", "🟢", "HYPE (HYPEREVM)"),
    ("USDG", "ROBINHOOD", "🏹", "USDG (ROBINHOOD)"),
]


def _fmt_k(v: int) -> str:
    """Nominal selalu dalam K: 5000 -> 5k ; 10001 -> 10k ; 1010000 -> 1.010k."""
    return f"{v // 1000:,}".replace(",", ".") + "k"


def _fmt_fee(f: int) -> str:
    """3000 -> 3k ; 3500 -> 3,5k."""
    if f % 1000 == 0:
        return f"{f // 1000}k"
    return f"{f / 1000:g}k".replace(".", ",")


def _fmt_pct(p: float) -> str:
    """2.5 -> 2,5%."""
    return f"{p:g}%".replace(".", ",")


def _fee_card(title: str, subtitle: str, fixed_tiers, percent_tiers, footer: str = "") -> str:
    """Kartu fee: <blockquote expandable> berisi judul + baris monospace rapi."""
    rows = [(f"{_fmt_k(lo)} - {_fmt_k(hi)}", _fmt_fee(fee)) for lo, hi, fee in fixed_tiers]
    for lo, hi, pct in percent_tiers:
        rng = f"> {_fmt_k(lo - 1)}" if hi is None else f"{_fmt_k(lo - 1)} - {_fmt_k(hi)}"
        rows.append((rng, _fmt_pct(pct)))
    width = max(len(r) for r, _ in rows)
    fee_width = max(len(f) for _, f in rows)
    lines = [title, subtitle]
    lines += [f"<code>{rng:<{width}}  {fee:>{fee_width}}</code>" for rng, fee in rows]
    if footer:
        lines.append(footer)
    return "<blockquote expandable>" + chr(10).join(lines) + "</blockquote>"


PRICE_LIST_NOTE = (
    "<blockquote expandable><b>📝 Note :</b>\n"
    "• ⛽ Pasangan gas (ETH-ETH, TRX-TRON, USDT-ETH, USDC-ETH) +Rp 2.500 untuk Gas Fee Kirim &amp; "
    "min transaksi Rp 7.500 untuk List Coin tersebut, Selain itu minimal transaksi Rp. 5.000\n\n"
    "• 🧾 Pajak QRIS 0,3% utk bayar via QRIS nominal di atas Rp 500.000 (masuk total bayar).\n\n"
    "• ➕ Adanya kode Unik untuk biaya transaksi Qris otomatis\n\n"
    "• 💱 Harga realtime pasar murni mengikuti bursa global tanpa spread tersembunyi.\n\n"
    "• 📣 Adanya fee transaksi yang berbeda-beda dikarenakan volatilitas harga coin crypto yang "
    "sangat berfluktuasi (naik-turunnya nilai) setiap detik dan Spread USD (selisih harga) yang "
    "berubah ubah guna menghindari kerugian stok coin pihak admin.</blockquote>"
)


def get_official_price_list_text() -> str:
    """Teks Price List Fee — selalu sinkron dengan services/fee_service.py."""
    return "\n".join([
        f"{E_CHART()} <b>PRICE LIST &amp; CARA PERHITUNGAN TRANSAKSI</b>",
        "",
        _fee_card(
            f"{E_COIN()} <b>Fee ALTCOIN</b> <i>(USDT beda &amp; lebih murah)</i>",
            "<i>Min. Jual/Beli 5k · fee dalam IDR</i>",
            ALTCOIN_FEE_TIERS, ALTCOIN_PERCENT_TIERS,
            "📌 <i>Khusus JUAL altcoin: +Rp 500 utk nominal di bawah Rp 1.010k (jual USD tidak kena).</i>",
        ),
        _fee_card(
            f"{E_DOLLAR()} <b>Fee Khusus USD</b>",
            "<i>Min. Jual/Beli 5k · fee dalam IDR</i>",
            USD_FEE_TIERS, USD_PERCENT_TIERS,
        ),
        _fee_card(
            f"{E_SWAP()} <b>Fee Convert</b>",
            "<i>Min. Convert 6k · fee dalam IDR</i>",
            CONVERT_FEE_TIERS, CONVERT_PERCENT_TIERS,
        ),
        PRICE_LIST_NOTE,
    ])


async def show_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Menampilkan Daftar Harga Crypto Hari Ini terupdate (Beli & Jual).
    Format 100% presisi sesuai permintaan client.
    """
    query = update.callback_query
    if query:
        await query.answer()
        await query.message.reply_chat_action(action="typing")
    elif update.message:
        await update.message.reply_chat_action(action="typing")

    db = SessionLocal()
    try:
        text_lines = [
            f"{E_CHART()} <b>DAFTAR HARGA CRYPTO HARI INI</b>\n",
            "<i>Berikut adalah harga crypto realtime terupdate (Beli & Jual sama sesuai market):</i>\n",
        ]

        for symbol, network, emoji, display_label in CURATED_PRICE_ASSETS:
            price_data = await price_service.get_price(symbol, db)
            if price_data:
                price_idr = price_data.get("market_price_idr", 0)
                price_str = format_idr(price_idr)
                usdt_rate = price_data.get("usdt_idr_rate") or price_data.get("market_price_idr", 0)
                if symbol.upper() in ["USDT", "USDC"]:
                    usd_str = "$1.00"
                else:
                    price_usd = price_idr / usdt_rate if usdt_rate > 0 else 0
                    usd_str = f"${price_usd:,.2f}" if price_usd >= 1 else f"${price_usd:,.4f}"
            else:
                price_str = "-"
                usd_str = "-"

            text_lines.append(
                f"{get_coin_emoji(symbol)} <b>{display_label}</b>\n"
                f"{E_CART()} Beli / {E_DOLLAR()} Jual: <code>{price_str}</code> (~{usd_str})\n"
            )

        update_time_str = format_datetime(datetime.now(timezone.utc))
        text_lines.append(f"{E_CALENDAR()} Update: {update_time_str}")
        text_lines.append(f"{E_SPARKLES()} <i>Harga realtime pasar murni. Transaksi dikenakan fee fixed resmi sesuai tier.</i>")

        message_text = "\n".join(text_lines)

        keyboard = [
            [InlineKeyboardButton("Price List Fee Lengkap", callback_data="price_fee_list", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("HISTORY", "5373251851074415873"))],
            [InlineKeyboardButton("Kembali ke Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]

        if query:
            await query.edit_message_text(
                text=message_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                text=message_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error di show_prices: {e}", exc_info=True)
        fallback_msg = "⚠️ Terjadi kesalahan saat mengambil harga crypto saat ini. Silakan coba sesaat lagi."
        keyboard = [
            [InlineKeyboardButton("Kembali ke Menu", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
        if query:
            await query.message.reply_text(text=fallback_msg, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(text=fallback_msg, reply_markup=InlineKeyboardMarkup(keyboard))
    finally:
        db.close()


async def show_fee_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Menampilkan tabel Price List Fee Transaksi resmi untuk Altcoin dan USD.
    """
    query = update.callback_query
    if query:
        await query.answer()

    keyboard = [
        [InlineKeyboardButton("Cek Harga Crypto Terupdate", callback_data="menu_price", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("DOLLAR", "5309929258443874898"))],
        [InlineKeyboardButton("Kembali ke Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
        [get_owner_button()]
    ]

    if query:
        await query.edit_message_text(
            text=get_official_price_list_text(),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            text=get_official_price_list_text(),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


# Alias untuk live market
show_live_market = show_prices
