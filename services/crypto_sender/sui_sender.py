"""
services/crypto_sender/sui_sender.py — Sender untuk Sui Network.
============================================================
Mengintegrasikan pengecekan saldo dan pengiriman koin SUI di Sui Network.
"""

import logging
import re
import httpx
from config.settings import settings
from services.crypto_sender import BaseCryptoSender, SendResult

logger = logging.getLogger(__name__)

class SuiSender(BaseCryptoSender):
    def __init__(self):
        self.network = "SUI"
        raw_rpcs = [
            settings.SUI_RPC,
            "https://sui-rpc.publicnode.com",
            "https://mainnet.sui.rpcpool.com",
            "https://sui-mainnet-endpoint.blockvision.org",
        ]
        self.rpc_list = [r.strip() for r in raw_rpcs if r and r.strip()]
        self.wallet_address = settings.SUI_WALLET_ADDRESS
        self.explorer_base = "https://suiscan.xyz"

    def validate_address(self, address: str) -> bool:
        """Validasi format alamat Sui (0x + 64 hex chars)."""
        return bool(address and re.fullmatch(r"0x[0-9a-fA-F]{64}", address))

    async def get_balance(self, symbol: str = "") -> float:
        """Ambil saldo SUI dari node Sui JSON-RPC."""
        if not self.wallet_address:
            raise RuntimeError("SUI_WALLET_ADDRESS belum dikonfigurasi.")
        if symbol and symbol.upper() != "SUI":
            raise ValueError(f"Token '{symbol}' tidak didukung pada Sui.")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "suix_getBalance",
            "params": [self.wallet_address, "0x2::sui::SUI"]
        }
        last_error = None
        for rpc in self.rpc_list:
            try:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    res = await client.post(rpc, json=payload)
                    res.raise_for_status()
                    data = res.json()
                    if data.get("error"):
                        raise RuntimeError(str(data["error"]))
                    return int(data["result"]["totalBalance"]) / 1e9
            except Exception as e:
                last_error = e
                logger.warning(f"Gagal mengambil saldo SUI via {rpc}: {e}")
                continue
        raise RuntimeError(f"Semua RPC Sui gagal membaca saldo SUI: {last_error}")

    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Tolak auto-payout sampai signing dan broadcast Sui tersedia."""
        if not self.validate_address(to_address):
            return SendResult(success=False, error_message="Alamat SUI tidak valid.")
        return SendResult(
            success=False,
            error_message=(
                "MANUAL_REVIEW: Auto-payout Sui belum tersedia; "
                "kirim SUI secara manual dan catat digest transaksi asli."
            ),
        )
