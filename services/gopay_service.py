"""
services/gopay_service.py — GoPay / Gopiz API Gateway Integration Client
========================================================================
Berkomunikasi dengan API Gateway Mandiri GoPay (ahmadzakiyox/gopay-api-gateaway)
untuk request QRIS Dinamis dan verifikasi pembayaran mutasi secara otomatis.
"""

import httpx
import logging
from typing import Optional, Dict, Any
from config.settings import settings

logger = logging.getLogger(__name__)


class GopayGatewayService:
    def __init__(self):
        self.base_url = (settings.GOPAY_GATEWAY_URL or "http://127.0.0.1:3005").rstrip("/")
        self.api_key = settings.GOPAY_API_KEY

    def _headers(self) -> dict:
        # Key di header, bukan URL: query string tercatat di log/proxy.
        return {"X-Api-Key": self.api_key}

    async def check_payment(self, amount: int, trx_id: str, start_time=None) -> Optional[Dict[str, Any]]:
        """
        Mengecek mutasi pembayaran masuk untuk nominal dan trx_id spesifik.

        start_time (datetime UTC naive/aware): hanya transaksi SESUDAH waktu ini
        yang boleh cocok. Tanpa ini gateway mencari 24 jam ke belakang sehingga
        pembayaran lama bernominal sama bisa "melunasi" order baru.

        Returns:
            dict: {
                "paid": bool,
                "transaction": dict
            }
        """
        try:
            url = f"{self.base_url}/check-payment"
            params = {
                "amount": amount,
                "trx_id": trx_id,
            }
            if start_time is not None:
                params["startTime"] = _iso_utc(start_time)
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.get(url, params=params, headers=self._headers())
                if res.status_code == 200:
                    json_res = res.json()
                    if json_res.get("success") is False:
                        return {"paid": False, "transaction": None, "available": False}
                    return {
                        "paid": bool(json_res.get("paid")),
                        "transaction": json_res.get("transaction"),
                        "available": True,
                    }
                logger.warning("GopayGatewayService check_payment HTTP %s (%s)", res.status_code, trx_id)
                return {"paid": False, "transaction": None, "available": False}
        except Exception as e:
            logger.warning(f"GopayGatewayService check_payment error ({trx_id}): {e}")
            return {"paid": False, "transaction": None, "available": False}

    async def get_recent_transactions(
        self,
        start_time: int = None,
        end_time: int = None,
        page_size: int = 100,
    ) -> list:
        """
        Mengambil riwayat mutasi transaksi dari gateway GoPay
        (endpoint GET /transactions).

        Args:
            start_time (int, optional): Timestamp unix detik awal (default 3 hari lalu).
            end_time (int, optional): Timestamp unix detik akhir (default sekarang).
            page_size (int, optional): Jumlah transaksi maksimal (default 100).

        Returns:
            list: Daftar transaksi (dict) atau [] jika gagal/tidak ada.
        """
        try:
            params = {"pageSize": page_size}
            if start_time is not None:
                params["startTime"] = start_time
            if end_time is not None:
                params["endTime"] = end_time

            url = f"{self.base_url}/transactions"
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.get(url, params=params, headers=self._headers())
                if res.status_code != 200:
                    logger.warning(f"GET /transactions HTTP {res.status_code}")
                    return []

                data = res.json()

            # Parsing defensif: response bisa berupa list langsung
            # atau dict {success, data: [...]}
            if isinstance(data, list):
                return data

            inner = data.get("data")
            if isinstance(inner, list):
                return inner
            if isinstance(inner, dict):
                for key in ("transactions", "items", "list", "mutasi"):
                    val = inner.get(key)
                    if isinstance(val, list):
                        return val
            return []
        except Exception as e:
            logger.warning(f"GopayGatewayService get_recent_transactions error: {e}")
            return []


    async def confirm_payment(self, db, *, amount: int, ref_id: str, kind: str, created_at) -> Optional[bool]:
        """Satu pintu verifikasi QRIS: cek gateway (sejak order dibuat) + klaim tx di DB.

        True hanya bila ada pembayaran setelah `created_at` yang BELUM dipakai
        order/topup lain. Dipakai tombol "Saya Sudah Transfer", bukti foto,
        poller topup, dan final-check sebelum expire. False berarti gateway berhasil
        memeriksa dan pembayaran belum cocok; None berarti gateway tidak dapat
        memastikan hasilnya, jadi pemanggil tidak boleh meng-expire order.
        """
        from database.crud import claim_qris_payment

        res = await self.check_payment(int(amount), ref_id, start_time=payment_window_start(created_at))
        if not res or res.get("available", True) is False:
            return None
        if not res.get("paid"):
            return False
        tx = res.get("transaction") or {}
        tx_id = tx.get("transaction_id") or tx.get("id") or tx.get("order_id")
        if not tx_id:
            logger.warning("Pembayaran %s terdeteksi tanpa transaction_id — tidak bisa diklaim, abaikan", ref_id)
            return None
        if not claim_qris_payment(db, str(tx_id), ref_id, kind, int(amount)):
            logger.warning("Transaksi %s sudah dipakai order/topup lain — tolak untuk %s", tx_id, ref_id)
            return False
        return True


# Toleransi jam server vs GoPay untuk transaksi yang terjadi sesaat setelah order dibuat.
PAYMENT_CLOCK_SKEW_SECONDS = 60


def payment_window_start(created_at):
    """Awal jendela pembayaran yang sah untuk order/topup (UTC naive)."""
    from datetime import timedelta
    if created_at is None:
        return None
    return _to_utc_naive(created_at) - timedelta(seconds=PAYMENT_CLOCK_SKEW_SECONDS)


def _to_utc_naive(dt):
    from datetime import timezone
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso_utc(dt) -> str:
    """ISO-8601 UTC dengan 'Z' — dibaca gateway via `new Date(...)`."""
    return _to_utc_naive(dt).isoformat(timespec="seconds") + "Z"


gopay_service = GopayGatewayService()
