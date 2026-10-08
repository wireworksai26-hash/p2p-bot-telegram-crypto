"""
services/report_service.py — Rekapitulasi Laporan Transaksi Mingguan & Export Spreadsheet (Phase 8)
===================================================================================================
Menyediakan agregasi data transaksi (7 hari / mingguan), pembuatan file CSV siap pakai untuk
Google Sheets & Microsoft Excel, ringkasan eksekutif via bot Telegram, serta webhook sync.
Khusus dapat diakses oleh Admin.
"""

import io
import csv
import os
import html
import logging
import httpx
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Any
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database.models import Order, User
from bot.utils.formatter import format_idr, format_crypto
from services.testimony_service import get_explorer_url_for_tx

logger = logging.getLogger(__name__)


def _format_wib(utc_dt: Optional[datetime]) -> str:
    """
    UTC (naive, seperti disimpan di DB) -> teks WIB, mis. '2026-10-08 14:30:05 WIB'.
    Akhiran ' WIB' sengaja ada: tanpa itu Excel/Sheets menganggapnya tanggal-waktu dan
    menampilkan '#####' bila kolom sempit. Sebagai teks, nilai selalu terbaca penuh.
    """
    if not utc_dt:
        return "-"
    return (utc_dt + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S") + " WIB"


def get_weekly_transactions_data(db: Session, days: int = 7) -> list[dict[str, Any]]:
    """
    Mengambil seluruh data transaksi selama N hari terakhir (default: 7 hari).
    Mencakup detail user, tipe transaksi, nominal, tujuan (wallet / rekening), dan tx hash.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    rows = (
        db.query(
            Order,
            User.username,
            User.full_name,
        )
        .outerjoin(User, User.telegram_id == Order.telegram_id)
        .filter(Order.created_at >= cutoff)
        .order_by(Order.created_at.desc())
        .all()
    )

    transactions = []
    for order, uname, fname in rows:
        created_str = _format_wib(order.created_at or datetime.utcnow())
        # Waktu transaksi berhasil diselesaikan; order lama bisa belum punya completed_at.
        is_done = str(order.status or "").lower() == "completed"
        completed_str = _format_wib(order.completed_at or (order.updated_at if is_done else None))

        ot = (order.order_type or "buy").upper()
        if ot in ("BUY", "BELI"):
            type_label = "Beli"
            destination = order.buyer_wallet or "-"
        elif ot in ("SELL", "JUAL"):
            type_label = "Jual"
            destination = order.buyer_wallet or "-"
        elif ot in ("SWAP", "CONVERT"):
            type_label = "Swap"
            t_net = order.target_network or ""
            t_sym = order.target_crypto_symbol or ""
            destination = f"{order.buyer_wallet or '-'} ({t_sym} {t_net})"
        else:
            type_label = ot.capitalize()
            destination = order.buyer_wallet or "-"

        status_str = (order.status or "UNKNOWN").upper()
        nominal_idr = int(order.total_idr or order.nominal_idr or 0)
        fee_idr = int(order.fee_idr or 0)
        crypto_amt = float(order.crypto_amount or 0.0)
        symbol = order.crypto_symbol or "-"
        network = order.network or "-"
        tx_hash = (order.payout_tx_hash or order.tx_hash or order.deposit_tx_hash or "").strip()
        explorer_url = get_explorer_url_for_tx(network or order.target_network, tx_hash) if tx_hash else ""

        transactions.append({
            "order_id": order.order_id,
            "created_at": created_str,
            "completed_at": completed_str,
            "order_type": type_label,
            "status": status_str,
            "telegram_id": order.telegram_id,
            "username": f"@{uname}" if uname else "-",
            "full_name": fname or "-",
            "nominal_idr": nominal_idr,
            "crypto_amount": crypto_amt,
            "crypto_symbol": symbol,
            "network": network,
            "destination": destination,
            "payment_method": order.payment_method or "-",
            "fee_idr": fee_idr,
            "tx_hash": tx_hash or "-",
            "explorer_url": explorer_url or "-",
        })

    return transactions


def calculate_weekly_summary(transactions: list[dict[str, Any]], days: int = 7) -> dict[str, Any]:
    """Menghitung ringkasan statistik agregat dari daftar transaksi."""
    total_orders = len(transactions)
    completed_orders = [t for t in transactions if t["status"] == "COMPLETED"]
    completed_count = len(completed_orders)
    pending_count = sum(1 for t in transactions if t["status"] in ("PENDING", "WAITING_CONFIRMATION", "WAITING_PAYMENT"))
    failed_count = sum(1 for t in transactions if t["status"] in ("FAILED", "CANCELLED", "EXPIRED", "REVERTED"))

    vol_beli = sum(t["nominal_idr"] for t in completed_orders if t["order_type"] == "Beli")
    vol_jual = sum(t["nominal_idr"] for t in completed_orders if t["order_type"] == "Jual")
    vol_swap = sum(t["nominal_idr"] for t in completed_orders if t["order_type"] == "Swap")
    total_turnover = vol_beli + vol_jual + vol_swap
    total_fees = sum(t["fee_idr"] for t in completed_orders)

    unique_users = len({t["telegram_id"] for t in transactions if t["telegram_id"]})

    now_wib = datetime.utcnow() + timedelta(hours=7)
    start_wib = now_wib - timedelta(days=days)

    return {
        "days": days,
        "period_start": start_wib.strftime("%d %b %Y"),
        "period_end": now_wib.strftime("%d %b %Y"),
        "total_orders": total_orders,
        "completed_count": completed_count,
        "pending_count": pending_count,
        "failed_count": failed_count,
        "vol_beli": vol_beli,
        "vol_jual": vol_jual,
        "vol_swap": vol_swap,
        "total_turnover": total_turnover,
        "total_fees": total_fees,
        "unique_users": unique_users,
    }


_FORMULA_PREFIXES = ("=", "+", "-", "@", chr(9), chr(13))


def _csv_text(value) -> str:
    """Teks dari user dinetralkan agar tidak dieksekusi sebagai formula Excel/Sheets."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def generate_weekly_report_csv_buffer(transactions: list[dict[str, Any]]) -> io.BytesIO:
    """
    Menghasilkan file CSV (BytesIO) berformat rapi dengan encoding UTF-8 BOM
    agar langsung terbaca dengan sempurna di Google Sheets & Excel.
    """
    output = io.StringIO()
    writer = csv.writer(output, delimiter=",", quoting=csv.QUOTE_MINIMAL)

    # Header kolom
    headers = [
        "No",
        "ID Order",
        "Waktu Order Dibuat (WIB)",
        "Waktu Transaksi Selesai (WIB)",
        "Jenis Transaksi",
        "Status",
        "Username Telegram",
        "Telegram ID",
        "Nama Pengguna",
        "Nominal IDR",
        "Jumlah Koin",
        "Simbol Koin",
        "Jaringan (Network)",
        "Tujuan (Wallet / Rekening)",
        "Metode Pembayaran",
        "Fee Layanan (IDR)",
        "TX Hash Blockchain",
        "Link Explorer",
    ]
    writer.writerow(headers)

    for idx, t in enumerate(transactions, start=1):
        writer.writerow([
            idx,
            _csv_text(t["order_id"]),
            t["created_at"],
            t.get("completed_at", "-"),
            t["order_type"],
            t["status"],
            _csv_text(t["username"]),
            str(t["telegram_id"]),
            _csv_text(t["full_name"]),
            t["nominal_idr"],
            f"{t['crypto_amount']:.8f}".rstrip("0").rstrip("."),
            _csv_text(t["crypto_symbol"]),
            _csv_text(t["network"]),
            _csv_text(t["destination"]),
            _csv_text(t["payment_method"]),
            t["fee_idr"],
            _csv_text(t["tx_hash"]),
            _csv_text(t["explorer_url"]),
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    buf = io.BytesIO(csv_bytes)
    buf.seek(0)
    return buf


def format_weekly_report_telegram_message(summary: dict[str, Any]) -> str:
    """Menyusun pesan teks ringkasan laporan mingguan untuk dikirim ke Admin."""
    days = summary["days"]
    p_start = summary["period_start"]
    p_end = summary["period_end"]

    msg = (
        f"📊 <b>REKAP LAPORAN TRANSAKSI ({days} HARI TERAKHIR)</b>\n"
        f"📅 <i>Periode: {p_start} — {p_end} (WIB)</i>\n"
        "-------------------------------------\n\n"
        "📈 <b>Ringkasan Aktivitas:</b>\n"
        f"• Total Pesanan: <b>{summary['total_orders']} order</b>\n"
        f"  ├── ✅ Selesai (Completed): <b>{summary['completed_count']}</b>\n"
        f"  ├── ⏳ Menunggu / Proses: <b>{summary['pending_count']}</b>\n"
        f"  └── ❌ Batal / Gagal: <b>{summary['failed_count']}</b>\n\n"
        "💰 <b>Volume & Perputaran Dana (Selesai):</b>\n"
        f"• Volume Beli (Buy)  : <b>{format_idr(summary['vol_beli'])}</b>\n"
        f"• Volume Jual (Sell) : <b>{format_idr(summary['vol_jual'])}</b>\n"
        f"• Volume Swap        : <b>{format_idr(summary['vol_swap'])}</b>\n"
        f"• <b>Total Omzet</b>     : <b>{format_idr(summary['total_turnover'])}</b>\n"
        f"• Estimasi Fee/Margin: <b>{format_idr(summary['total_fees'])}</b>\n\n"
        f"👥 Pengguna Aktif: <b>{summary['unique_users']} orang</b>\n\n"
        "📥 <i>File laporan lengkap format <b>.CSV</b> terlampir di bawah. "
        "File ini dapat langsung Anda import ke <b>Google Sheets</b> atau buka di <b>Microsoft Excel</b>.</i>"
    )
    return msg


async def sync_to_google_sheet_webhook(
    transactions: list[dict[str, Any]],
    webhook_url: Optional[str] = None,
) -> bool:
    """
    Kirim data transaksi ke Google Sheets Apps Script Webhook (jika dikonfigurasi).
    URL dapat disetel melalui env GOOGLE_SHEET_WEBHOOK_URL.
    """
    url = webhook_url or os.getenv("GOOGLE_SHEET_WEBHOOK_URL")
    if not url:
        return False

    try:
        payload = {
            "source": "HSN_CRYPTO_BOT",
            "timestamp": datetime.utcnow().isoformat(),
            "transaction_count": len(transactions),
            "transactions": transactions[:200],  # batch up to 200 rows
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Sync laporan mingguan ke Google Sheets webhook berhasil.")
                return True
            else:
                logger.warning(f"Google Sheets webhook merespons status {resp.status_code}: {resp.text}")
                return False
    except Exception as e:
        logger.warning(f"Gagal sync ke Google Sheets webhook: {e}")
        return False
