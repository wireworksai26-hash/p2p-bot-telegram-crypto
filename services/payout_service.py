"""
services/payout_service.py — Service Eksekusi Pengiriman Crypto Otomatis.
==========================================================================
Fungsi reusable untuk mengirim crypto ke wallet user dengan retry.

Dipakai oleh:
  - Webhook Tripay (pembayaran Beli via Tripay QRIS/VA).
  - Polling GoPay QRIS (pembayaran Beli via GoPay QRIS).
  - DepositDetector (auto-payout Convert/swap setelah deposit terverifikasi).
"""

import asyncio
import logging

from services.crypto_sender import CryptoSenderFactory

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAYS = [5, 15, 30]  # seconds between retry attempts


async def send_crypto_with_retry(
    sender,
    to_address: str,
    amount: float,
    symbol: str,
) -> dict:
    """
    Attempt to send crypto with retries.

    Returns:
        dict with keys 'success', 'tx_hash', 'error_message', 'explorer_url'.
    """
    last_error = ""
    for attempt in range(MAX_RETRIES):
        try:
            result = await sender.send(
                to_address=to_address,
                amount=amount,
                symbol=symbol,
            )

            if result.success:
                logger.info(
                    "Crypto sent successfully — tx: %s", result.tx_hash,
                )
                return {
                    "success": True,
                    "tx_hash": result.tx_hash,
                    "explorer_url": result.explorer_url,
                    "error_message": "",
                }

            last_error = result.error_message or "Send failed"

            if result.error_message.startswith("MANUAL_REVIEW:"):
                return {
                    "success": False,
                    "tx_hash": result.tx_hash or "",
                    "explorer_url": result.explorer_url or "",
                    "error_message": result.error_message.removeprefix("MANUAL_REVIEW:").strip(),
                }

            logger.warning(
                "Send attempt %d/%d failed: %s",
                attempt + 1, MAX_RETRIES, result.error_message,
            )

        except Exception as exc:
            last_error = f"{exc.__class__.__name__}: {str(exc)}"
            logger.error(
                "Exception on send attempt %d/%d: %s",
                attempt + 1, MAX_RETRIES, exc,
                exc_info=True,
            )

        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(RETRY_DELAYS[attempt])

    return {
        "success": False,
        "tx_hash": "",
        "explorer_url": "",
        "error_message": last_error or "Max retries exceeded",
    }


async def send_order_payout(order) -> dict:
    """
    Kirim crypto sesuai jenis Order:
      - Buy / Sell-tujuan / normal: kirim order.crypto_symbol sejumlah
        order.crypto_amount ke order.buyer_wallet.
      - Swap/convert: kirim order.target_crypto_symbol (order.target_network)
        sejumlah order.target_crypto_amount ke order.buyer_wallet.

    Returns:
        dict hasil dari send_crypto_with_retry.
    """
    if order.order_type == "swap":
        sender = CryptoSenderFactory.get_sender(order.target_network)
        return await send_crypto_with_retry(
            sender=sender,
            to_address=order.buyer_wallet,
            amount=float(order.target_crypto_amount),
            symbol=order.target_crypto_symbol,
        )

    sender = CryptoSenderFactory.get_sender(order.network)
    return await send_crypto_with_retry(
        sender=sender,
        to_address=order.buyer_wallet,
        amount=float(order.crypto_amount),
        symbol=order.crypto_symbol,
    )
