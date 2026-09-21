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
from bot.utils.formatter import format_datetime
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
    "HYPEREVM": "HyperEVM",
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
    return dict(await asyncio.gather(*(fetch(symbol) for symbol in symbols)))


def build_stock_pages(balances, prices: dict, now=None) -> list[str]:
    """Build bounded HTML pages; keep each coin and its networks together."""
    now = now or datetime.utcnow()
    grouped = {}
    for wallet in balances:
        grouped.setdefault(wallet.symbol.upper(), []).append(wallet)
    symbols = [sym for sym in COIN_ORDER if sym in grouped]
    symbols += sorted(set(grouped) - set(symbols))
    header = "📦 <b>STOK CRYPTO</b>\n<i>Saldo hot wallet; diperbarui setiap 5 menit.</i>\n\n"
    fresh_times = [w.last_success_at for w in balances if wallet_balance_is_fresh(w, now)]
    if fresh_times:
        header += f"<i>Saldo terverifikasi tertua: {format_datetime(min(fresh_times))}.</i>\n\n"
    footer = "\n<i>Data lama/gagal dibaca tidak dihitung sebagai stok terverifikasi.</i>"
    blocks = []
    for symbol in symbols:
        wallets = grouped[symbol]
        lines = [f"{get_coin_emoji(symbol)} <b>{escape(COIN_FULL_NAMES.get(symbol, symbol))}</b>"]
        total, fresh_count = 0.0, 0
        for wallet in wallets:
            network = wallet.network.upper()
            label = escape(NETWORK_LABELS.get(network, network))
            prefix = f"  {get_network_emoji(network)} {label}: "
            balance = float(wallet.balance or 0)
            fresh = wallet_balance_is_fresh(wallet, now)
            if fresh:
                total += balance
                fresh_count += 1
                line = prefix + f"<code>{format_crypto_qty(balance, symbol)}</code>"
                price = prices.get(symbol, 0)
                if balance == 0:
                    line += " <i>(Kosong)</i>"
                elif price > 0:
                    line += f" (~${balance * price:,.2f})"
                if network in MANUAL_PAYOUT_NETWORKS:
                    line += " <i>· pengiriman admin</i>"
            elif wallet.last_success_at:
                reason = "sinkronisasi gagal" if wallet.sync_status == "ERROR" else "belum diperbarui"
                line = prefix + f"<code>{format_crypto_qty(balance, symbol)}</code> ⚠️\n"
                line += f"    <i>Data lama: {format_datetime(wallet.last_success_at)}; {reason}.</i>"
            else:
                status = "Gagal membaca saldo" if wallet.sync_status == "ERROR" else "Menunggu sinkronisasi"
                line = prefix + f"<i>{status}</i> ⚠️"
            lines.append(line)
        if len(wallets) > 1:
            if fresh_count:
                lines.append(f"  <b>Total terverifikasi:</b> <code>{format_crypto_qty(total, symbol)}</code>")
            else:
                lines.append("  <i>Total belum dapat dikonfirmasi.</i>")
        blocks.append("\n".join(lines) + "\n\n")
    if not blocks:
        blocks = ["⏳ Data stok belum tersedia.\n"]
    pages, current = [], header
    for block in blocks:
        # Conservative serialized HTML limit also stays below Telegram's parsed limit.
        if len(current) + len(block) + len(footer) > 3800 and current != header:
            pages.append(current + footer)
            current = header
        current += block
    pages.append(current + footer)
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
            text=pages[page] + f"\nHalaman {page + 1}/{len(pages)}",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML",
        )
    except Exception:
        logger.exception("Gagal menampilkan stok wallet.")
        await query.message.reply_text("⚠️ Gagal mengambil data stok. Silakan coba kembali atau hubungi admin.")
