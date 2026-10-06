"""Stock display: distinguish confirmed zero, failed reads and stale balances."""

import asyncio
import logging
from datetime import datetime
from html import escape

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from config.assets import STOCK_ASSETS, MANUAL_PAYOUT_NETWORKS
from database.connection import SessionLocal
from database.crud import get_all_wallet_balances, wallet_balance_is_fresh
from services.price_service import price_service
from bot.keyboards.main_menu import get_owner_button
from bot.utils.formatter import display_symbol, format_datetime
from bot.utils.emojis import get_coin_emoji, get_network_emoji

logger = logging.getLogger(__name__)
COIN_ORDER = [
    "USDT", "USDC", "ETH", "SOL", "BNB", "TRX", "TON", "SUI", "APT",
    "MATIC", "POL", "ARB", "AVAX", "KAIA", "BERA", "HYPE", "USDG",
]
COIN_FULL_NAMES = {
    "USDT": "Tether (USDT)", "USDC": "USD Coin (USDC)", "ETH": "Ethereum (ETH)",
    "SOL": "Solana (SOL)", "BNB": "BNB Chain (BNB)", "TRX": "TRON (TRX)",
    "TON": "Gram (GRAM)", "SUI": "Sui (SUI)", "APT": "Aptos (APT)",
    "MATIC": "Polygon (MATIC / POL)", "POL": "Polygon (POL)",
    "ARB": "Arbitrum (ARB)", "AVAX": "Avalanche (AVAX)", "KAIA": "Kaia (KAIA)",
    "BERA": "Berachain (BERA)", "HYPE": "Hyperliquid (HYPE)", "USDG": "Global Dollar (USDG)",
}
NETWORK_LABELS = {
    "BSC": "BNB Smart Chain (BEP20)", "BASE": "Base Mainnet", "ARB": "Arbitrum One",
    "POLYGON": "Polygon (POL)", "ETH": "Ethereum (ERC20)", "SOLANA": "Solana Mainnet",
    "TRON": "TRON (TRC20)", "TON": "TON Network", "OPTIMISM": "Optimism (OP)",
    "ROBINHOOD": "Robinhood", "SUI": "Sui Mainnet", "APTOS": "Aptos Mainnet",
    "AVAX": "Avalanche C-Chain", "KAIA": "Kaia Network", "BERA": "Berachain",
    "HYPEREVM": "HyperEVM", "MORPH": "Morph Network",
}


def format_crypto_qty(balance: float, symbol: str) -> str:
    from bot.utils.formatter import display_symbol

    ticker = display_symbol(symbol)
    if balance == 0:
        return f"0 {ticker}"
    precision = 6 if symbol in ("USDT", "USDC") else 9
    quantity = f"{balance:,.{precision}f}".rstrip("0").rstrip(".")
    if balance > 0 and quantity == "0":
        quantity = f"{balance:.4g}"
    return f"{quantity} {ticker}"


async def fetch_usd_prices(symbols: list[str]) -> dict[str, float]:
    async def fetch(symbol):
        if symbol in ("USDT", "USDC"):
            return symbol, 1.0
        try:
            info = await price_service.get_price(symbol)
            rate = float((info or {}).get("usdt_idr_rate", 0))
            market = float((info or {}).get("market_price_idr", 0))
            if rate > 0 and market > 0:
                return symbol, market / rate
        except Exception:
            pass
        return symbol, 0.0
    async def fetch_idr_rate():
        try:
            info = await price_service.get_price("USDT")
            return "_IDR", float((info or {}).get("market_price_idr", 0))
        except Exception:
            return "_IDR", 0.0
    return dict(await asyncio.gather(fetch_idr_rate(), *(fetch(symbol) for symbol in symbols)))


CHAIN_SHORT_LABELS = {
    "BSC": "BEP20", "ETH": "ERC20", "POLYGON": "Poly", "ARB": "Arb", "BASE": "Base",
    "SOLANA": "Solana", "TRON": "TRC20", "TON": "TON", "OPTIMISM": "OP",
    "ROBINHOOD": "Robinhood", "SUI": "Sui", "APTOS": "Aptos", "AVAX": "Avax",
    "KAIA": "Kaia", "BERA": "Bera", "HYPEREVM": "HyperEVM", "MORPH": "Morph",
}
# Kartu stok: (judul, simbol). Simbol di luar daftar masuk "Token Lainnya".
STOCK_CARDS = [
    ("USDT + CHAIN", "USDT"),
    ("USDC + CHAIN", "USDC"),
    ("ETH + CHAIN", "ETH"),
]
OTHER_CARD_TITLE = "Token Lainnya"
STOCK_FOOTER = "\n<i>⚠️ Data lama / gagal dibaca tidak dihitung sebagai stok terverifikasi.</i>"


def format_stock_qty(balance: float) -> str:
    """Jumlah ringkas: 13.28 · 0.0105 · 1500 · 0 (tanpa nol di belakang).

    Tanpa pemisah ribuan: "1,500" mudah terbaca 1,5 oleh user Indonesia.
    """
    if balance <= 0:
        return "0"
    if balance >= 1:
        return f"{balance:.2f}".rstrip("0").rstrip(".")
    if balance < 0.0001:
        return f"{balance:.8f}".rstrip("0").rstrip(".")
    return f"{balance:.4g}"


def format_rp_compact(value: float) -> str:
    """Rupiah ringkas: Rp467 · Rp269rb · Rp1,2jt · Rp1,8M (miliar)."""
    value = round(max(float(value), 0.0))
    if value < 1000:
        return f"Rp{value}"
    if round(value / 1000) < 1000:
        return f"Rp{round(value / 1000)}rb"
    for divisor, unit in ((1_000_000, "jt"), (1_000_000_000, "M")):
        scaled = round(value / divisor, 1)
        if scaled < 1000 or unit == "M":
            text = f"{scaled:.1f}".replace(".", ",").removesuffix(",0")
            return f"Rp{text}{unit}"


def _stock_row(label: str, emoji: str, wallet, prices: dict, now) -> tuple[float, str]:
    """Satu baris kartu: (nilai urut, html). Kolom rapi dalam <code>."""
    symbol, network = wallet.symbol.upper(), wallet.network.upper()
    balance = float(wallet.balance or 0)
    fresh = wallet_balance_is_fresh(wallet, now)
    if fresh:
        usd = balance * prices.get(symbol, 0)
        idr_rate = prices.get("_IDR", 0)
        rupiah = format_rp_compact(usd * idr_rate) if idr_rate > 0 and (usd > 0 or balance == 0) else ""
        cells = f"{label:<9}{format_stock_qty(balance):>9}  {rupiah}".rstrip()
        return usd, f"{emoji} <code>{escape(cells)}</code>"
    if network in MANUAL_PAYOUT_NETWORKS:
        # Jaringan kirim manual (admin): tidak ada saldo otomatis, jangan tampilkan error.
        cells = f"{label:<9}{'Manual':>9}"
        return -1.0, f"{emoji} <code>{escape(cells)}</code>"
    qty = format_stock_qty(balance) if wallet.last_success_at else "-"
    cells = f"{label:<9}{qty:>9}"
    return -1.0, f"{emoji} <code>{escape(cells)}</code> ⚠️"


def _stock_card(title_html: str, rows: list[tuple[float, str]]) -> str:
    rows = sorted(rows, key=lambda row: row[0], reverse=True)
    return "<blockquote expandable>" + "\n".join([title_html] + [html for _, html in rows]) + "</blockquote>\n"


def build_stock_pages(balances, prices: dict, now=None) -> list[str]:
    """Kartu ringkas (blockquote expandable) per grup; dipecah bila melebihi batas pesan.

    `prices`: harga USD per simbol, plus kunci opsional "_IDR" (kurs USD->IDR)
    untuk kolom nilai Rupiah.
    """
    now = now or datetime.utcnow()
    grouped = {}
    for wallet in balances:
        grouped.setdefault(wallet.symbol.upper(), []).append(wallet)
    header = "📦 <b>Stock Tersedia</b>\n"
    fresh_times = [w.last_success_at for w in balances if wallet_balance_is_fresh(w, now)]
    if fresh_times:
        header += f"<i>Saldo terverifikasi tertua: {format_datetime(min(fresh_times))}</i>\n"
    header += "\n"

    blocks = []
    card_symbols = {symbol for _, symbol in STOCK_CARDS}
    for title, symbol in STOCK_CARDS:
        wallets = grouped.get(symbol)
        if not wallets:
            continue
        rows = [
            _stock_row(CHAIN_SHORT_LABELS.get(w.network.upper(), w.network.title()),
                       get_network_emoji(w.network.upper()), w, prices, now)
            for w in wallets
        ]
        blocks.append(_stock_card(f"{get_coin_emoji(symbol)} <b>{title}</b>", rows))

    other_symbols = [sym for sym in COIN_ORDER if sym in grouped and sym not in card_symbols]
    other_symbols += sorted(set(grouped) - set(other_symbols) - card_symbols)
    rows = []
    for symbol in other_symbols:
        for w in grouped[symbol]:
            rows.append(_stock_row(display_symbol(symbol), get_coin_emoji(symbol), w, prices, now))
    if rows:
        blocks.append(_stock_card(f"<b>{OTHER_CARD_TITLE}</b>", rows))
    if not blocks:
        blocks = ["⏳ Data stok belum tersedia.\n"]

    pages, current = [], header
    for block in blocks:
        # Conservative serialized HTML limit also stays below Telegram's parsed limit.
        if len(current) + len(block) + len(STOCK_FOOTER) > 3800 and current != header:
            pages.append(current + STOCK_FOOTER)
            current = header
        current += block
    pages.append(current + STOCK_FOOTER)
    return pages


async def show_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        db = SessionLocal()
        try:
            balances = get_all_wallet_balances(db)
            expected = {(net, sym) for sym, net in STOCK_ASSETS}
            recorded = {(w.network, w.symbol) for w in balances}
            needs_sync = bool(expected - recorded) or any(w.sync_status == "UNKNOWN" for w in balances)
        finally:
            db.close()
        if needs_sync:
            from services.wallet_sync import sync_wallet_balances
            await sync_wallet_balances()
            db = SessionLocal()
            try:
                balances = get_all_wallet_balances(db)
            finally:
                db.close()
        prices = await fetch_usd_prices(sorted({w.symbol for w in balances}))
        pages = build_stock_pages(balances, prices)
        suffix = query.data.removeprefix("menu_stocks_page_")
        page = int(suffix) if suffix.isdigit() else 0
        page = min(max(page, 0), len(pages) - 1)
        keyboard = []
        if len(pages) > 1:
            navigation = []
            if page > 0:
                navigation.append(InlineKeyboardButton("← Sebelumnya", callback_data=f"menu_stocks_page_{page - 1}"))
            if page < len(pages) - 1:
                navigation.append(InlineKeyboardButton("Berikutnya →", callback_data=f"menu_stocks_page_{page + 1}"))
            keyboard.append(navigation)
        keyboard += [[InlineKeyboardButton("Kembali ke Menu", callback_data="menu_back")], [get_owner_button()]]
        await query.edit_message_text(
            text=pages[page] + (f"\nHalaman {page + 1}/{len(pages)}" if len(pages) > 1 else ""),
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML",
        )
    except Exception:
        logger.exception("Gagal menampilkan stok wallet.")
        await query.message.reply_text("⚠️ Gagal mengambil data stok. Silakan coba kembali atau hubungi admin.")
