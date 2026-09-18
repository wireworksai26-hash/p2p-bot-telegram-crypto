"""
services/crypto_sender/aptos_sender.py — Sender untuk Aptos Network.
================================================================
Mengintegrasikan pengecekan saldo dan pengiriman koin APT di Aptos Network.
"""

import logging
import re
import httpx
from config.settings import settings
from services.crypto_sender import BaseCryptoSender, SendResult

logger = logging.getLogger(__name__)

class AptosSender(BaseCryptoSender):
    def __init__(self):
        self.network = "APTOS"
        raw_rpcs = [settings.APTOS_RPC, "https://fullnode.mainnet.aptoslabs.com/v1"]
        self.rpc_list = list(dict.fromkeys(r.rstrip("/") for r in raw_rpcs if r and r.strip()))
        self.wallet_address = settings.APTOS_WALLET_ADDRESS
        self.explorer_base = "https://explorer.aptoslabs.com"

    def validate_address(self, address: str) -> bool:
        """Validasi format alamat Aptos."""
        return bool(address and re.fullmatch(r"0x[0-9a-fA-F]{1,64}", address))

    async def get_balance(self, symbol: str = "") -> float:
        """Ambil saldo APT, termasuk saldo yang sudah dimigrasi ke Fungible Asset."""
        if not self.wallet_address:
            raise RuntimeError("APTOS_WALLET_ADDRESS belum dikonfigurasi.")
        if symbol and symbol.upper() != "APT":
            raise ValueError(f"Token '{symbol}' tidak didukung pada Aptos.")

        payload = {
            "function": "0x1::coin::balance",
            "type_arguments": ["0x1::aptos_coin::AptosCoin"],
            "arguments": [self.wallet_address],
        }
        last_error = None
        for rpc in self.rpc_list:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    res = await client.post(f"{rpc}/view", json=payload)
                    res.raise_for_status()
                    data = res.json()
                    if not isinstance(data, list) or not data:
                        raise RuntimeError(f"respons view tidak valid: {data}")
                    return int(data[0]) / 1e8
            except Exception as exc:
                last_error = exc
                logger.warning("Gagal mengambil saldo Aptos via %s: %s", rpc, exc)
        raise RuntimeError(f"Semua endpoint Aptos gagal membaca saldo APT: {last_error}")

    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Tolak auto-payout sampai signing dan broadcast Aptos tersedia."""
        if not self.validate_address(to_address):
            return SendResult(success=False, error_message="Alamat Aptos tidak valid.")
        return SendResult(
            success=False,
            error_message=(
                "MANUAL_REVIEW: Auto-payout Aptos belum tersedia; "
                "kirim APT secara manual dan catat TX hash asli."
            ),
        )
