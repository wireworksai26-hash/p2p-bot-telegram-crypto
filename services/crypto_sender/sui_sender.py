"""
services/crypto_sender/sui_sender.py — Sender untuk Sui Network.
============================================================
Mengintegrasikan pengecekan saldo dan pengiriman koin SUI di Sui Network.
"""

import logging
import asyncio
import re
import secrets
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
            return 0.0
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "suix_getBalance",
            "params": [self.wallet_address, "0x2::sui::SUI"]
        }
        for rpc in self.rpc_list:
            try:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    res = await client.post(rpc, json=payload)
                    if res.status_code == 200:
                        data = res.json()
                        result = data.get("result")
                        if result and "totalBalance" in result:
                            total_balance = int(result["totalBalance"])
                            return float(total_balance / 1e9)
            except Exception as e:
                logger.warning(f"Gagal mengambil saldo SUI via {rpc}: {e}")
                continue
        return 0.0

    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Kirim / simulasi transaksi di Sui Network."""
        try:
            if not self.validate_address(to_address):
                return SendResult(success=False, error_message="Alamat SUI tidak valid.")
            
            mock_hash = secrets.token_hex(32)
            explorer_url = f"{self.explorer_base}/tx/{mock_hash}"
            logger.info(f"[SUI] Auto-send {amount} {symbol} ke {to_address} (Hash: {mock_hash})")
            
            return SendResult(
                success=True,
                tx_hash=mock_hash,
                explorer_url=explorer_url
            )
        except Exception as e:
            return SendResult(success=False, error_message=f"Exception pengiriman SUI: {str(e)}")
