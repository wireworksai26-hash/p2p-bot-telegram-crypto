"""Nominal koin deposit Jual/Convert ke hot wallet bersama.

Nominal deposit = persis jumlah yang dipilih user (dibulatkan ke bawah ke presisi
tampilan), TANPA kode unik. Kode unik hanya ada di tagihan QRIS (Rupiah), bukan di koin.

Karena tanpa kode unik deposit tidak bisa dicocokkan lewat nominal, kepemilikan deposit
dibuktikan lewat TX hash yang WAJIB dikirim user (lihat bot/handlers/deposit_hash.py)
dan diverifikasi on-chain (wallet, nominal, waktu, belum dipakai order lain).
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from config.settings import settings
from database.models import Order

logger = logging.getLogger(__name__)

STABLE_LIKE = {"USDT", "USDC", "USDG", "TRX", "TON"}
HIGH_VALUE = {"ETH", "BNB"}


def deposit_precision(symbol: str) -> int:
    """Jumlah desimal nominal deposit (≤ desimal on-chain semua jaringan yang didukung)."""
    sym = (symbol or "").upper()
    if sym in STABLE_LIKE:
        return 4
    if sym in HIGH_VALUE:
        return 8
    return 6


def quantum(symbol: str) -> Decimal:
    return Decimal(1).scaleb(-deposit_precision(symbol))


def base_amount(symbol: str, requested) -> Decimal:
    """Nominal yang dijual/di-convert: dibulatkan ke bawah ke presisi deposit."""
    return Decimal(str(requested)).quantize(quantum(symbol), rounding=ROUND_DOWN)


def format_deposit_amount(amount, symbol: str) -> str:
    """Nominal deposit persis seperti yang harus dikirim (tanpa pembulatan tampilan)."""
    return f"{Decimal(str(amount)).quantize(quantum(symbol)):f}"


def deposit_code_of(amount, symbol: str) -> int:
    """Kode unik koin sudah dihapus; selalu 0 (dipertahankan agar pemanggil lama tetap jalan)."""
    return 0


def active_deposit_orders(db, network: str, symbol: str, wallet: str, exclude_order: str = None):
    """Order yang depositnya masih bisa masuk ke wallet ini (menunggu, atau expired dalam jendela)."""
    window_start = datetime.utcnow() - timedelta(minutes=settings.SELL_DEPOSIT_WINDOW_MINUTES)
    query = db.query(Order).filter(
        Order.network == network,
        Order.crypto_symbol == symbol,
        Order.deposit_wallet == wallet,
        (Order.status == "WAITING_CRYPTO_DEPOSIT")
        | ((Order.status == "expired")
           & Order.order_type.in_(["sell", "swap"])
           & (Order.created_at >= window_start)),
    )
    if exclude_order:
        query = query.filter(Order.order_id != exclude_order)
    return query.all()


def assign_deposit_amount(db, network: str, symbol: str, wallet: str, requested) -> Optional[Decimal]:
    """Nominal deposit order baru = nominal dasar persis (tanpa kode unik); None bila terlalu kecil."""
    base = base_amount(symbol, requested)
    if base <= 0:
        return None
    return base
