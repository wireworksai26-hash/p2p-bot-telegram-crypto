"""Kode unik koin untuk deposit Jual/Convert ke hot wallet bersama.

Deposit dicocokkan lewat nominal. Tanpa kode unik, transfer yang bukan milik
order mana pun (isi ulang stok oleh owner, deposit telat dari order yang sudah
expired) bisa diklaim order lain bernominal sama. Nominal deposit kini:

    dasar (dibulatkan ke bawah ke presisi tampilan) + kode 1..99 satuan terkecil

dengan syarat dua digit terakhir bukan 00 (isi ulang bernominal "bulat" tidak
pernah cocok) dan tidak sama dengan order lain yang depositnya masih bisa masuk.
"""
import logging
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from config.settings import settings
from database.models import Order

logger = logging.getLogger(__name__)

STABLE_LIKE = {"USDT", "USDC", "USDG", "TRX", "TON"}
HIGH_VALUE = {"ETH", "BNB"}
MAX_CODE = 99


def deposit_precision(symbol: str) -> int:
    """Jumlah desimal nominal deposit (≤ desimal on-chain semua jaringan yang didukung).

    Kode unik maksimal 99 satuan: USDT/TRX/TON 0,0099; ETH/BNB 0,00000099; lainnya 0,000099.
    """
    sym = (symbol or "").upper()
    if sym in STABLE_LIKE:
        return 4
    if sym in HIGH_VALUE:
        return 8
    return 6


def quantum(symbol: str) -> Decimal:
    return Decimal(1).scaleb(-deposit_precision(symbol))


def base_amount(symbol: str, requested) -> Decimal:
    """Nominal dasar yang dijual/di-convert: dibulatkan ke bawah, menyisakan 2 digit untuk kode."""
    return Decimal(str(requested)).quantize(Decimal(1).scaleb(2 - deposit_precision(symbol)), rounding=ROUND_DOWN)


def format_deposit_amount(amount, symbol: str) -> str:
    """Nominal deposit persis seperti yang harus dikirim (tanpa pembulatan tampilan)."""
    return f"{Decimal(str(amount)).quantize(quantum(symbol)):f}"


def deposit_code_of(amount, symbol: str) -> int:
    """Kode unik yang melekat pada nominal deposit (2 digit terakhir)."""
    q = quantum(symbol)
    units = int(Decimal(str(amount)).quantize(q) / q)
    return units % 100


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
    """Nominal deposit unik untuk order baru, atau None bila semua kode terpakai."""
    q = quantum(symbol)
    base = base_amount(symbol, requested)
    if base <= 0:
        return None
    taken = {
        Decimal(str(o.crypto_amount)).quantize(Decimal("0.00000001"))
        for o in active_deposit_orders(db, network, symbol, wallet)
        if o.crypto_amount is not None
    }
    candidates = [base + q * code for code in range(1, MAX_CODE + 1)
                  if (base + q * code).quantize(Decimal("0.00000001")) not in taken]
    if not candidates:
        logger.warning("Kode unik deposit %s/%s habis untuk nominal %s", symbol, network, base)
        return None
    return secrets.choice(candidates)
