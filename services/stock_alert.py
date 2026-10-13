"""
services/stock_alert.py — Peringatan stok koin menipis ke admin
================================================================
Dicek tiap 5 menit (setelah sinkron saldo on-chain). Batas dihitung dari NILAI USD stok yang
tersedia (saldo wallet dikurangi stok yang sedang dikunci untuk payout), sehingga sama artinya
untuk semua koin:

- 🔴 KRITIS  : nilai < STOCK_ALERT_CRITICAL_USD (bawaan $3) — order baru kemungkinan gagal.
- 🟠 MENIPIS : nilai < STOCK_ALERT_LOW_USD (bawaan $15).

Anti-spam: alert dikirim saat sebuah koin pertama kali menipis atau makin parah, lalu pengingat tiap
STOCK_ALERT_REMIND_HOURS (bawaan 6 jam) selama belum diisi. Saat stok kembali aman, admin dikabari.
Dulu fiturnya mati secara bawaan dan hanya mengirim angka mentah tiap 6 jam.
"""

import logging
import os
import time
from html import escape
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

LOW_USD = float(os.getenv("STOCK_ALERT_LOW_USD", "15"))
CRITICAL_USD = float(os.getenv("STOCK_ALERT_CRITICAL_USD", "3"))
REMIND_SECONDS = float(os.getenv("STOCK_ALERT_REMIND_HOURS", "6")) * 3600
ENABLED = os.getenv("STOCK_ALERT_ENABLED", "true").lower() in ("true", "1", "yes")

# Dipakai hanya bila harga koin tidak tersedia (jumlah koin, bukan USD).
FALLBACK_UNIT_LOW = {
    "USDT": 15.0, "USDC": 15.0, "USDG": 15.0, "ETH": 0.005, "BNB": 0.02, "SOL": 0.1, "AVAX": 0.5,
    "TRX": 50.0, "MATIC": 20.0, "POL": 20.0, "TON": 5.0, "SUI": 5.0, "APT": 5.0, "HYPE": 0.5,
    "ARB": 20.0, "KAIA": 50.0, "BERA": 5.0,
}

LEVEL_RANK = {"ok": 0, "low": 1, "critical": 2}
_state: dict = {}   # (network, symbol) -> {"level": str, "alerted_at": float}


def classify(available: float, usd_value: Optional[float], symbol: str) -> str:
    if usd_value is not None:
        if usd_value < CRITICAL_USD:
            return "critical"
        return "low" if usd_value < LOW_USD else "ok"
    limit = FALLBACK_UNIT_LOW.get(symbol.upper())
    if limit is None:
        return "ok"
    if available <= limit * CRITICAL_USD / LOW_USD:
        return "critical"
    return "low" if available < limit else "ok"


def _row_text(row: dict) -> str:
    from bot.handlers.stocks import format_stock_qty, format_usd_compact
    from bot.utils.formatter import display_symbol
    sym = display_symbol(row["symbol"])
    value = f" ≈ <b>{format_usd_compact(row['usd'])}</b>" if row["usd"] is not None else ""
    line = f"• <b>{escape(sym)}</b> ({escape(row['network'])}): {format_stock_qty(row['available'])} {escape(sym)}{value}"
    if row.get("address"):
        line += f"\n   Isi ke: <code>{escape(row['address'])}</code>"
    return line


def build_alert(rows: list, reminder: bool) -> str:
    critical = [r for r in rows if r["level"] == "critical"]
    low = [r for r in rows if r["level"] == "low"]
    title = "⏰ <b>PENGINGAT: STOK MASIH MENIPIS</b>" if reminder else "📦 <b>PERINGATAN STOK MENIPIS</b>"
    parts = [title, ""]
    if critical:
        parts.append(f"🔴 <b>KRITIS / HAMPIR HABIS</b> (di bawah ${CRITICAL_USD:g}) — segera isi ulang:")
        parts += [_row_text(r) for r in critical]
        parts.append("")
    if low:
        parts.append(f"🟠 <b>MENIPIS</b> (di bawah ${LOW_USD:g}):")
        parts += [_row_text(r) for r in low]
        parts.append("")
    parts.append(
        "Order Beli / Convert untuk koin di atas bisa gagal dan masuk proses manual bila stok tidak cukup.\n"
        "Isi ulang dari wallet owner (terdaftar di <code>OWNER_WALLET_ADDRESSES</code>), lalu tekan "
        "<b>🔄 Sync On-Chain</b> agar stok langsung terbaca.")
    return "\n".join(parts)


def _markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Sync On-Chain", callback_data="admin_panel_sync_wallets"),
        InlineKeyboardButton("💼 Lihat Stok", callback_data="admin_panel_wallets"),
    ]])


def _current_rows(db, prices: dict, now_dt) -> list:
    from config.assets import MANUAL_PAYOUT_NETWORKS
    from database.crud import get_all_wallet_balances, wallet_balance_is_fresh
    rows = []
    for wallet in get_all_wallet_balances(db):
        network, symbol = (wallet.network or "").upper(), (wallet.symbol or "").upper()
        # Data basi / gagal dibaca tidak dinilai: lebih baik diam daripada alarm palsu.
        if network in MANUAL_PAYOUT_NETWORKS or not wallet_balance_is_fresh(wallet, now_dt):
            continue
        available = max(0.0, float(wallet.balance or 0) - float(wallet.reserved_balance or 0))
        price = float(prices.get(symbol) or 0)
        usd = available * price if price > 0 else None
        rows.append({"network": network, "symbol": symbol, "available": available, "usd": usd,
                     "address": wallet.address, "level": classify(available, usd, symbol)})
    return rows


async def check_stock_levels(bot, prices: Optional[dict] = None, now: Optional[float] = None,
                             now_dt=None) -> dict:
    """Bandingkan stok sekarang dengan alert sebelumnya, kirim alert/pengingat/kabar pulih bila perlu."""
    from datetime import datetime
    from database.connection import SessionLocal
    from bot.utils.telegram_utils import notify_admins
    now = time.time() if now is None else now
    now_dt = now_dt or datetime.utcnow()

    db = SessionLocal()
    try:
        if prices is None:
            from database.crud import get_all_wallet_balances
            from bot.handlers.stocks import fetch_usd_prices
            symbols = sorted({(w.symbol or "").upper() for w in get_all_wallet_balances(db)})
            prices = await fetch_usd_prices(symbols)
        rows = _current_rows(db, prices, now_dt)
    finally:
        db.close()

    flagged = [r for r in rows if r["level"] != "ok"]
    worsened = [r for r in flagged
                if LEVEL_RANK[r["level"]] > LEVEL_RANK[_state.get((r["network"], r["symbol"]), {}).get("level", "ok")]]
    reminder_due = any(now - _state.get((r["network"], r["symbol"]), {}).get("alerted_at", now) >= REMIND_SECONDS
                       for r in flagged)
    recovered = [r for r in rows if r["level"] == "ok" and (r["network"], r["symbol"]) in _state]

    sent = {"alert": False, "recovered": []}
    if flagged and (worsened or reminder_due):
        flagged.sort(key=lambda r: (-LEVEL_RANK[r["level"]], r["usd"] if r["usd"] is not None else 0))
        await notify_admins(bot, build_alert(flagged, reminder=not worsened), reply_markup=_markup(),
                            kind="alarm", butuh_tindakan=True)
        for r in flagged:
            _state[(r["network"], r["symbol"])] = {"level": r["level"], "alerted_at": now}
        sent["alert"] = True
    else:
        for r in flagged:  # tingkat membaik (kritis → menipis) tanpa alert baru
            key = (r["network"], r["symbol"])
            if key in _state:
                _state[key]["level"] = r["level"]

    if recovered:
        from bot.handlers.stocks import format_usd_compact
        from bot.utils.formatter import display_symbol
        lines = [f"• {escape(display_symbol(r['symbol']))} ({escape(r['network'])})"
                 + (f" ≈ {format_usd_compact(r['usd'])}" if r["usd"] is not None else "") for r in recovered]
        await notify_admins(bot, "✅ <b>Stok sudah aman kembali</b>\n\n" + "\n".join(lines), kind="alarm")
        for r in recovered:
            _state.pop((r["network"], r["symbol"]), None)
        sent["recovered"] = [(r["network"], r["symbol"]) for r in recovered]
    return sent
