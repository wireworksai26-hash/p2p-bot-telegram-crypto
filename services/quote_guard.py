"""services/quote_guard.py — Validasi ulang harga quote saat konfirmasi.

Quote dibekukan saat user mengetik nominal, sedangkan percakapan tidak punya
timeout. Dengan spread 0%, user bisa menahan quote lalu konfirmasi setelah harga
bergerak menguntungkannya (Saldo Bot / Convert = payout instan).

Aturan: quote yang dikonfirmasi <= QUOTE_FRESH_SECONDS dipakai apa adanya.
Lebih lama dari itu, harga pasar diambil ulang; jika bergeser > QUOTE_MAX_DRIFT
konfirmasi ditolak dan user diminta mengulang dengan harga baru.
"""

import logging
import time
from decimal import Decimal

logger = logging.getLogger(__name__)

QUOTE_FRESH_SECONDS = 60
QUOTE_MAX_DRIFT = Decimal("0.005")  # 0,5%

# Masa berlaku order Jual/Convert (Beli/QRIS: settings.ORDER_EXPIRE_MINUTES, juga 10 menit).
# Koin yang masuk SETELAH batas ini + toleransi blok tidak dibayar dengan harga terkunci.
QUOTE_MINUTES = 10
LATE_DEPOSIT_GRACE_SECONDS = 120
# Estimasi maksimal admin mentransfer Rupiah setelah koin Jual terverifikasi.
SELL_PAYOUT_ETA_MINUTES = 20

QUOTE_MOVED_TEXT = (
    "⏱️ <b>Harga pasar sudah berubah</b>\n\n"
    "Harga koin bergerak lebih dari 0,5% sejak simulasi dibuat. "
    "Supaya adil untuk kedua pihak, silakan ulangi transaksi untuk mendapatkan harga terbaru."
)


def stamp() -> float:
    return time.time()


def is_fresh(quoted_at) -> bool:
    """Tanpa stempel (percakapan lama / tes lama) dianggap segar — perilaku sebelumnya."""
    if not quoted_at:
        return True
    return (time.time() - float(quoted_at)) <= QUOTE_FRESH_SECONDS


def _drifted(quoted, current) -> bool:
    quoted, current = Decimal(str(quoted or 0)), Decimal(str(current or 0))
    if quoted <= 0 or current <= 0:
        return True
    return abs(current - quoted) / quoted > QUOTE_MAX_DRIFT


async def prices_still_valid(quoted_at, quotes: dict) -> bool:
    """quotes = {symbol: harga_idr_saat_quote}. True bila quote masih boleh dipakai."""
    if is_fresh(quoted_at):
        return True
    from services.price_service import price_service
    for symbol, quoted in quotes.items():
        try:
            info = await price_service.get_price(symbol)
        except Exception as exc:
            logger.warning("Re-quote %s gagal: %s", symbol, exc)
            return False
        current = (info or {}).get("market_price_idr") or (info or {}).get("buy_price_idr")
        if _drifted(quoted, current):
            logger.info("Quote %s basi: %s -> %s", symbol, quoted, current)
            return False
    return True
