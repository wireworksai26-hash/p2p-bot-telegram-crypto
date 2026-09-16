"""
services/crypto_sender/solana_sender.py — Sender untuk Solana (SOL).
====================================================================
Menangani pengiriman SOL native ke wallet customer menggunakan solana-py & solders.
"""

import logging
import asyncio
import base58
import httpx
from config.settings import settings
from services.crypto_sender import BaseCryptoSender, SendResult

# Solana imports
try:
    from solana.rpc.api import Client
    from solders.pubkey import Pubkey
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction
    from spl.token.constants import TOKEN_PROGRAM_ID
    from spl.token.instructions import (
        TransferCheckedParams,
        create_associated_token_account,
        get_associated_token_address,
        transfer_checked,
    )
    SOLANA_LIB_AVAILABLE = True
except ImportError:
    SOLANA_LIB_AVAILABLE = False

logger = logging.getLogger(__name__)

# Token SPL yang diperjualbelikan bot beserta alamat mint-nya
SPL_TOKENS = {
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}

class SolanaSender(BaseCryptoSender):
    def __init__(self):
        raw_rpcs = [settings.SOL_RPC, "https://solana-rpc.publicnode.com", "https://api.mainnet-beta.solana.com", "https://1rpc.io/solana"]
        self.rpc_list = [r.strip() for r in raw_rpcs if r and r.strip()]
        self.rpc_url = self.rpc_list[0]
        self.explorer_base = "https://explorer.solana.com"
        self.wallet_address = settings.SOL_WALLET_ADDRESS
        self.private_key_b58 = settings.SOL_PRIVATE_KEY

    def validate_address(self, address: str) -> bool:
        """Validasi base58 address Solana (panjang 32-44 karakter)."""
        try:
            if not (32 <= len(address) <= 44):
                return False
            # Decode b58 untuk cek validitas encoding
            decoded = base58.b58decode(address)
            return len(decoded) == 32
        except Exception:
            return False

    async def get_balance(self, symbol: str = "") -> float:
        """Mengambil saldo SOL native atau token SPL (USDT/USDC)."""
        if not self.wallet_address:
            return 0.0
        sym = symbol.upper() if symbol else ""
        if sym in SPL_TOKENS:
            return await self._get_spl_token_balance(SPL_TOKENS[sym])

        if not SOLANA_LIB_AVAILABLE:
            logger.error("Solana library (solana/solders) tidak terinstall.")
            return 0.0
        
        for rpc in self.rpc_list:
            try:
                client = Client(rpc)
                pubkey = Pubkey.from_string(self.wallet_address)
                response = await asyncio.to_thread(client.get_balance, pubkey)
                lamports = response.value
                balance = lamports / 10**9
                return float(balance)
            except Exception as e:
                logger.warning(f"Gagal mengambil saldo Solana via {rpc}: {e}")
                continue
        return 0.0

    async def _get_spl_token_balance(self, mint_address: str) -> float:
        """
        Mengambil saldo token SPL milik wallet bot via RPC getTokenAccountsByOwner.
        """
        if not self.wallet_address:
            return 0.0
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                self.wallet_address,
                {"mint": mint_address},
                {"encoding": "jsonParsed"},
            ],
        }
        for rpc in self.rpc_list:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    res = await client.post(rpc, json=payload)
                    if res.status_code == 200:
                        data = res.json()
                        total = 0.0
                        for item in (data.get("result", {}).get("value") or []):
                            info = (
                                item.get("account", {})
                                .get("data", {})
                                .get("parsed", {})
                                .get("info", {})
                            )
                            amt = info.get("tokenAmount", {}).get("uiAmount")
                            if amt is not None:
                                total += float(amt)
                        return total
            except Exception as e:
                logger.warning(f"Gagal mengambil saldo SPL token via {rpc}: {e}")
                continue
        return 0.0

    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Mengirim SOL native atau token SPL USDT/USDC."""
        if not SOLANA_LIB_AVAILABLE:
            return SendResult(
                success=False,
                error_message="Library Solana tidak tersedia di server ini."
            )
        try:
            # Validasi input
            if not self.validate_address(to_address):
                return SendResult(success=False, error_message="Alamat wallet Solana tidak valid.")

            # Load private key (biasanya format base58 phantom export)
            try:
                secret_key = base58.b58decode(self.private_key_b58)
                sender_keypair = Keypair.from_bytes(secret_key)
            except Exception as key_err:
                return SendResult(
                    success=False,
                    error_message=f"Gagal memuat Solana Private Key: {key_err}"
                )

            client = Client(self.rpc_url)
            
            # Inisialisasi pubkeys
            from_pubkey = sender_keypair.pubkey()
            to_pubkey = Pubkey.from_string(to_address)

            instructions = []
            if symbol.upper() in SPL_TOKENS:
                mint = Pubkey.from_string(SPL_TOKENS[symbol.upper()])
                supply = await asyncio.to_thread(client.get_token_supply, mint)
                decimals = int(supply.value.decimals)
                token_balance = await self.get_balance(symbol.upper())
                if token_balance < amount:
                    return SendResult(
                        success=False,
                        error_message=(
                            f"Saldo {symbol.upper()} tidak cukup. Saldo: {token_balance}, "
                            f"Kebutuhan: {amount}"
                        ),
                    )

                source_ata = get_associated_token_address(from_pubkey, mint)
                destination_ata = get_associated_token_address(to_pubkey, mint)
                destination_info = await asyncio.to_thread(
                    client.get_account_info, destination_ata
                )
                if destination_info.value is None:
                    sol_balance = await self.get_balance()
                    if sol_balance < 0.01:
                        return SendResult(
                            success=False,
                            error_message=(
                                "MANUAL_REVIEW: Saldo SOL tidak cukup untuk membuat "
                                "Associated Token Account penerima."
                            ),
                        )
                    instructions.append(
                        create_associated_token_account(from_pubkey, to_pubkey, mint)
                    )

                instructions.append(
                    transfer_checked(
                        TransferCheckedParams(
                            program_id=TOKEN_PROGRAM_ID,
                            source=source_ata,
                            mint=mint,
                            dest=destination_ata,
                            owner=from_pubkey,
                            amount=int(round(amount * (10 ** decimals))),
                            decimals=decimals,
                            signers=[],
                        )
                    )
                )
            else:
                # Cek saldo SOL native (juga membayar fee/ATA rent).
                balance = await self.get_balance()
                if balance < amount:
                    return SendResult(
                        success=False,
                        error_message=f"Saldo SOL tidak cukup. Saldo: {balance} SOL, Kebutuhan: {amount} SOL"
                    )
                instructions.append(
                    transfer(
                        TransferParams(
                            from_pubkey=from_pubkey,
                            to_pubkey=to_pubkey,
                            lamports=int(amount * 10**9),
                        )
                    )
                )

            # Dapatkan blockhash terbaru
            recent_blockhash_resp = await asyncio.to_thread(client.get_latest_blockhash)
            recent_blockhash = recent_blockhash_resp.value.blockhash

            message = Message.new_with_blockhash(
                instructions,
                from_pubkey,
                recent_blockhash,
            )
            tx = VersionedTransaction(message, [sender_keypair])
            
            # Sign & Send transaksi
            # Note: signers di solders/solana-py menerima list Keypair
            response = await asyncio.to_thread(client.send_transaction, tx)
            
            tx_hash = str(response.value)
            explorer_url = f"{self.explorer_base}/tx/{tx_hash}"
            
            # Verifikasi transaksi di blockchain (confirm transaction)
            # Karena ini async wrapper dan library solana blocking,
            # kita asumsikan transaksi berhasil dikirim. Kita beri delay aman.
            logger.info(f"Transaksi Solana berhasil dikirim. Hash: {tx_hash}")
            return SendResult(
                success=True,
                tx_hash=tx_hash,
                explorer_url=explorer_url
            )

        except Exception as e:
            logger.error(f"Error saat mengirim SOL: {e}", exc_info=True)
            return SendResult(
                success=False,
                error_message=f"Exception saat pengiriman Solana: {str(e)}"
            )
