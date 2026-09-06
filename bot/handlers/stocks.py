"""
bot/handlers/stocks.py — Handler Cek Stok Aset Crypto (Hot Wallet Balances).
==========================================================================
Menampilkan saldo cryptocurrency yang tersedia di hot wallet bot untuk dibeli user
dengan format sub-bab rapi per koin, nominal koin, dan estimasi USD (tanpa IDR).
"""

import logging
import asyncio
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database.connection import SessionLocal
from database.crud import get_all_wallet_balances
from services.price_service import price_service
from bot.keyboards.main_menu import get_owner_button
from bot.utils.formatter import format_datetime

logger = logging.getLogger(__name__)

# Urutan prioritas tampilan koin
COIN_ORDER = [
    "USDT", "USDC", "ETH", "SOL", "BNB",
    "TRX", "TON", "SUI", "APT", "MATIC",
    "ARB", "AVAX", "KAIA", "BERA", "HYPE", "G"
]

COIN_FULL_NAMES = {
    "USDT": "Tether (USDT)",
    "USDC": "USD Coin (USDC)",
    "ETH": "Ethereum (ETH)",
    "SOL": "Solana (SOL)",
    "BNB": "BNB Chain (BNB)",
    "TRX": "TRON (TRX)",
    "TON": "Toncoin (TON)",
    "SUI": "Sui (SUI)",
    "APT": "Aptos (APT)",
    "MATIC": "Polygon (MATIC / POL)",
    "ARB": "Arbitrum (ARB)",
    "AVAX": "Avalanche (AVAX)",
    "KAIA": "Kaia Network (KAIA)",
    "BERA": "Berachain (BERA)",
    "HYPE": "Hyperliquid (HYPE)",
    "G": "Gravity (G)",
}

COIN_HEADER_EMOJIS = {
    "USDT": "💵",
    "USDC": "🪙",
    "ETH": "🔷",
    "SOL": "🟣",
    "BNB": "🟡",
    "TRX": "❤️",
    "TON": "💎",
    "SUI": "💧",
    "APT": "⚡",
    "MATIC": "🟪",
    "ARB": "🔷",
    "AVAX": "🔺",
    "KAIA": "🌱",
    "BERA": "🐻",
    "HYPE": "🚀",
    "G": "🌌",
}

NETWORK_LABELS = {
    "BSC": "BNB Smart Chain (BEP20)",
    "BASE": "Base Mainnet",
    "ARB": "Arbitrum One",
    "POLYGON": "Polygon (POL)",
    "ETH": "Ethereum (ERC20)",
    "SOLANA": "Solana Mainnet",
    "TRON": "TRON (TRC20)",
    "TON": "TON Network",
    "OPTIMISM": "Optimism (OP)",
    "ROBINHOOD": "Robinhood",
    "SUI": "Sui Mainnet",
    "APTOS": "Aptos Mainnet",
    "AVAX": "Avalanche C-Chain",
    "KAIA": "Kaia Network",
    "BERA": "Berachain",
    "HYPEREVM": "HyperEVM",
    "GRAVITY": "Gravity Alpha",
}

NETWORK_EMOJIS = {
    "BSC": "🟢",
    "BASE": "🔵",
    "ARB": "💎",
    "POLYGON": "🟪",
    "ETH": "🔷",
    "SOLANA": "🟣",
    "TRON": "❤️",
    "TON": "💎",
    "OPTIMISM": "🔴",
    "ROBINHOOD": "🏹",
    "SUI": "💧",
    "APTOS": "⚡",
    "AVAX": "🔺",
    "KAIA": "🌱",
    "BERA": "🐻",
    "HYPEREVM": "🚀",
    "GRAVITY": "🌌",
}


def format_crypto_qty(bal: float, symbol: str) -> str:
    """Format kuantitas koin crypto agar rapi."""
    if bal == 0:
        return f"0 {symbol}"
    if symbol in ("USDT", "USDC"):
        return f"{bal:.2f} {symbol}"
    elif bal < 0.0001:
        return f"{bal:.6f} {symbol}"
    elif bal < 1.0:
        return f"{bal:.4f} {symbol}"
    else:
        return f"{bal:,.4f}".rstrip("0").rstrip(".") + f" {symbol}"


async def fetch_usd_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch harga USD realtime untuk setiap koin via price_service."""
    prices = {"USDT": 1.0, "USDC": 1.0}

    async def get_sym_price(sym: str):
        if sym in ("USDT", "USDC"):
            return sym, 1.0
        try:
            p_info = await price_service.get_price(sym)
            if p_info:
                market_idr = float(p_info.get("market_price_idr", 0.0))
                usdt_rate = float(p_info.get("usdt_idr_rate", 16000.0))
                if usdt_rate > 0 and market_idr > 0:
                    return sym, market_idr / usdt_rate
        except Exception:
            pass
        return sym, 0.0

    tasks = [get_sym_price(s) for s in symbols if s not in ("USDT", "USDC")]
    if tasks:
        results = await asyncio.gather(*tasks)
        for sym, p in results:
            prices[sym] = p
    return prices


async def show_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Menampilkan stok (saldo wallet) untuk semua aset crypto.
    Dikelompokkan rapi per sub-bab koin dengan jumlah koin & estimasi USD.
    """
    query = update.callback_query
    await query.message.reply_chat_action(action="typing")

    db = SessionLocal()
    try:
        balances = get_all_wallet_balances(db)
        if not balances:
            try:
                from main import _job_sync_wallet_balances
                await _job_sync_wallet_balances()
                db.close()
                db = SessionLocal()
                balances = get_all_wallet_balances(db)
            except Exception as sync_err:
                logger.warning(f"On-demand stock sync error: {sync_err}")

        text_lines = [
            "📦 <b>STOK CRYPTO TERSEDIA</b>\n",
            "<i>Saldo koin di hot wallet kami yang siap dikirim secara instan:</i>\n",
        ]

        if not balances:
            text_lines.append("⚠️ <i>Belum ada data stok tercatat. Saldo sedang disinkronisasikan...</i>")
        else:
            # Grouping balances by symbol
            grouped: dict[str, list] = {}
            for wallet in balances:
                sym = wallet.symbol.upper()
                grouped.setdefault(sym, []).append(wallet)

            usd_prices = await fetch_usd_prices(list(grouped.keys()))

            # Urutan koin
            all_symbols = [s for s in COIN_ORDER if s in grouped] + [s for s in grouped if s not in COIN_ORDER]

            for sym in all_symbols:
                wallets = grouped[sym]
                header_emoji = COIN_HEADER_EMOJIS.get(sym, "🪙")
                full_name = COIN_FULL_NAMES.get(sym, sym)
                usd_price = usd_prices.get(sym, 0.0)

                total_bal = sum(float(w.balance or 0.0) for w in wallets)
                total_usd = total_bal * usd_price

                text_lines.append(f"{header_emoji} <b>{full_name}</b>")
                for w in wallets:
                    net_upper = w.network.upper()
                    net_emoji = NETWORK_EMOJIS.get(net_upper, "•")
                    net_label = NETWORK_LABELS.get(net_upper, w.network)
                    bal_val = float(w.balance or 0.0)
                    bal_usd = bal_val * usd_price

                    if bal_val > 0:
                        bal_str = format_crypto_qty(bal_val, sym)
                        if usd_price > 0:
                            text_lines.append(f"  {net_emoji} {net_label}: <code>{bal_str}</code> (~${bal_usd:,.2f})")
                        else:
                            text_lines.append(f"  {net_emoji} {net_label}: <code>{bal_str}</code> ✅")
                    else:
                        text_lines.append(f"  {net_emoji} {net_label}: <code>0 {sym}</code> <i>(Kosong)</i>")

                # Subtotal koin jika memiliki lebih dari 1 jaringan
                if len(wallets) > 1:
                    total_bal_str = format_crypto_qty(total_bal, sym)
                    if usd_price > 0:
                        text_lines.append("  ────────────────────")
                        text_lines.append(f"  💰 <b>Total {sym}:</b> <code>{total_bal_str}</code> <b>(~${total_usd:,.2f})</b>\n")
                    else:
                        text_lines.append("  ────────────────────")
                        text_lines.append(f"  💰 <b>Total {sym}:</b> <code>{total_bal_str}</code>\n")
                else:
                    text_lines.append("")

        text_lines.append(f"⏱️ <i>Update: {format_datetime(datetime.now(timezone.utc))}</i>")
        text_lines.append("💡 <i>Seluruh stok koin aktif 24/7 dan disinkronisasi dengan blockchain secara realtime.</i>")

        message_text = "\n".join(text_lines)

        keyboard = [
            [InlineKeyboardButton("🔙 Kembali ke Menu", callback_data="menu_back")],
            [get_owner_button()]
        ]

        await query.edit_message_text(
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Error di show_stocks: {e}", exc_info=True)
        await query.message.reply_text(
            text="⚠️ Gagal mengambil data stok saldo wallet. Silakan hubungi admin.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Kembali ke Menu", callback_data="menu_back")
            ]])
        )
    finally:
        db.close()
