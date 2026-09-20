"""
services/crypto_sender/solana_sender.py — Sender untuk Solana (SOL).
====================================================================
Menangani pengiriman SOL native ke wallet customer menggunakan solana-py & solders.
"""

import logging
import asyncio
import base64
from decimal import Decimal
import base58
import httpx
from config.settings import settings
from services.crypto_sender import BaseCryptoSender, SendResult

# Solana imports
try:
    from solders.hash import Hash
    from solders.pubkey import Pubkey
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction
    from spl.token.constants import TOKEN_PROGRAM_ID
    from spl.token.instructions import (
        create_associated_token_account,
        get_associated_token_address,
        transfer_checked,
    )
    try:
        from spl.token.models import TransferCheckedParams
    except ImportError:
        from spl.token.instructions import TransferCheckedParams
    SOLANA_LIB_AVAILABLE = True
except ImportError:
    SOLANA_LIB_AVAILABLE = False

logger = logging.getLogger(__name__)

# Token SPL yang diperjualbelikan bot beserta alamat mint-nya
SPL_TOKENS = {
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}

# Harus 44 karakter penuh; nilai terpotong menolak semua payout mainnet.
SOLANA_MAINNET_GENESIS_HASH = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"


class SolanaSender(BaseCryptoSender):
    def __init__(self):
        raw_rpcs = [
            settings.SOL_RPC,
            "https://solana-rpc.publicnode.com",
            "https://api.mainnet-beta.solana.com",
        ]
        self.rpc_list = list(dict.fromkeys(r.strip() for r in raw_rpcs if r and r.strip()))
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
            raise RuntimeError("SOL_WALLET_ADDRESS belum dikonfigurasi.")
        sym = symbol.upper() if symbol else ""
        if sym in SPL_TOKENS:
            return await self._get_spl_token_balance(SPL_TOKENS[sym])
        if sym not in ("", "SOL"):
            raise ValueError(f"Token '{sym}' tidak didukung pada Solana.")

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [self.wallet_address, {"commitment": "confirmed"}],
        }
        last_error = None
        for rpc in self.rpc_list:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    res = await client.post(rpc, json=payload)
                    res.raise_for_status()
                    data = res.json()
                    if data.get("error") or "result" not in data:
                        raise RuntimeError(str(data.get("error") or "result kosong"))
                    lamports = int(data["result"]["value"])
                    return float(lamports / 10**9)
            except Exception as e:
                last_error = e
                logger.warning(f"Gagal mengambil saldo Solana via {rpc}: {e}")
                continue
        raise RuntimeError(f"Semua RPC Solana gagal membaca saldo SOL: {last_error}")

    async def _get_spl_token_balance(self, mint_address: str) -> float:
        """
        Mengambil saldo token SPL milik wallet bot.

        Memakai akun ATA + getAccountInfo (bukan query indexed yang diblok di
        sebagian RPC gratis). Bila ATA melaporkan 0, diverifikasi silang dengan
        query mint di endpoint resmi agar tidak ada "0 palsu" yang memblokir stok.
        """
        if not self.wallet_address:
            raise RuntimeError("SOL_WALLET_ADDRESS belum dikonfigurasi.")

        try:
            from solders.pubkey import Pubkey
            from spl.token.instructions import get_associated_token_address

            ata = str(get_associated_token_address(
                Pubkey.from_string(self.wallet_address), Pubkey.from_string(mint_address)))
        except Exception as exc:
            raise RuntimeError(f"Gagal menurunkan alamat ATA SPL: {type(exc).__name__}")

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [ata, {"encoding": "jsonParsed"}],
        }
        last_error = None
        for rpc in self.rpc_list:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    res = await client.post(rpc, json=payload)
                    res.raise_for_status()
                    data = res.json()
                    if data.get("error") or "result" not in data:
                        raise RuntimeError(str(data.get("error") or "result kosong"))
                    value = data["result"]["value"]
                    if value is None:
                        cross_check = await self._spl_balance_by_mint(mint_address)
                        return cross_check
                    parsed = value["data"]["parsed"]["info"]
                    if parsed.get("mint") != mint_address:
                        raise ValueError("Mint akun token tidak sesuai permintaan.")
                    token_amount = parsed["tokenAmount"]
                    return float(int(token_amount["amount"]) / (10 ** int(token_amount["decimals"])))
            except Exception as e:
                last_error = e
                logger.warning(f"Gagal mengambil saldo SPL token via {rpc}: {e}")
                continue
        raise RuntimeError(f"Semua RPC Solana gagal membaca saldo SPL: {last_error}")

    async def _spl_balance_by_mint(self, mint_address: str) -> float:
        """Verifikasi silang saldo token lewat query mint (endpoint resmi, rate-limit ketat)."""
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
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.post("https://api.mainnet-beta.solana.com", json=payload)
                res.raise_for_status()
                data = res.json()
                if data.get("error") or "result" not in data:
                    raise RuntimeError(str(data.get("error") or "result kosong"))
                total = 0.0
                for item in data["result"]["value"]:
                    token_amount = item["account"]["data"]["parsed"]["info"]["tokenAmount"]
                    total += int(token_amount["amount"]) / (10 ** int(token_amount["decimals"]))
                return total
        except Exception as exc:
            logger.warning("Verifikasi silang saldo SPL gagal (%s); saldo ATA dipakai apa adanya.",
                           type(exc).__name__)
            return 0.0

    async def _rpc(self, method, params):
        """Panggil JSON-RPC Solana dengan rotasi endpoint bila salah satu gagal."""
        last_error = None
        for rpc in self.rpc_list:
            try:
                async with httpx.AsyncClient(timeout=12) as client:
                    response = await client.post(rpc, json={
                        "jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                    response.raise_for_status()
                    data = response.json()
                if data.get("error") or "result" not in data:
                    raise RuntimeError("RPC Solana menolak/belum mengonfirmasi transaksi.")
                return data["result"]
            except Exception as exc:
                last_error = exc
                logger.warning("RPC Solana %s gagal via %s: %s", method, rpc, type(exc).__name__)
                continue
        raise RuntimeError(f"Semua RPC Solana gagal untuk {method}: {type(last_error).__name__}")

    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Sign once; a signature exists before broadcast, and success needs finality."""
        if not SOLANA_LIB_AVAILABLE:
            return SendResult(False, error_message="MANUAL_REVIEW: SDK Solana tidak tersedia.")
        tx_hash = ""
        broadcast_attempted = False
        try:
            sym = symbol.upper()
            quantity = Decimal(str(amount))
            if sym not in {"SOL", *SPL_TOKENS} or not quantity.is_finite() or quantity <= 0:
                return SendResult(False, error_message="MANUAL_REVIEW: Aset/nominal Solana tidak valid.")
            if not self.validate_address(to_address):
                return SendResult(False, error_message="MANUAL_REVIEW: Alamat Solana tidak valid.")
            keypair = Keypair.from_bytes(base58.b58decode(self.private_key_b58))
            owner, recipient = keypair.pubkey(), Pubkey.from_string(to_address)
            if str(owner) != self.wallet_address:
                return SendResult(False, error_message="MANUAL_REVIEW: Key Solana tidak cocok dengan alamat stok.")
            # Reject a accidentally configured devnet/testnet RPC before signing.
            if await self._rpc("getGenesisHash", []) != SOLANA_MAINNET_GENESIS_HASH:
                return SendResult(False, error_message="MANUAL_REVIEW: RPC bukan Solana mainnet.")
            instructions = []
            decimals = 9 if sym == "SOL" else 6
            units = int(quantity * 10**decimals)
            if units <= 0:
                return SendResult(False, error_message="MANUAL_REVIEW: Nominal di bawah unit minimum.")
            if sym == "SOL":
                instructions.append(transfer(TransferParams(
                    from_pubkey=owner, to_pubkey=recipient, lamports=units)))
            else:
                mint = Pubkey.from_string(SPL_TOKENS[sym])
                source = get_associated_token_address(owner, mint)
                destination = get_associated_token_address(recipient, mint)
                account = await self._rpc("getAccountInfo", [str(destination), {"encoding": "base64"}])
                if account["value"] is None:
                    instructions.append(create_associated_token_account(owner, recipient, mint))
                instructions.append(transfer_checked(TransferCheckedParams(
                    program_id=TOKEN_PROGRAM_ID, source=source, mint=mint, dest=destination,
                    owner=owner, amount=units, decimals=decimals, signers=[])))
            recent = await self._rpc("getLatestBlockhash", [{"commitment": "finalized"}])
            message = Message.new_with_blockhash(instructions, owner, Hash.from_string(recent["value"]["blockhash"]))
            transaction = VersionedTransaction(message, [keypair])
            tx_hash = str(transaction.signatures[0])
            encoded = base64.b64encode(bytes(transaction)).decode()
            broadcast_attempted = True
            returned = await self._rpc("sendTransaction", [encoded, {
                "encoding": "base64", "skipPreflight": False, "preflightCommitment": "finalized", "maxRetries": 0}])
            if returned != tx_hash:
                raise RuntimeError("Signature response tidak cocok.")
            for _ in range(40):
                status = (await self._rpc("getSignatureStatuses", [[tx_hash], {"searchTransactionHistory": True}]))["value"][0]
                if status and status.get("err") is not None:
                    return SendResult(False, tx_hash, "MANUAL_REVIEW: Transaksi Solana gagal on-chain.",
                                      f"{self.explorer_base}/tx/{tx_hash}")
                if status and status.get("confirmationStatus") == "finalized":
                    return SendResult(True, tx_hash, explorer_url=f"{self.explorer_base}/tx/{tx_hash}")
                await asyncio.sleep(1)
            raise RuntimeError("Receipt belum finalized.")
        except Exception as exc:
            logger.warning("Payout Solana belum selesai (%s)", type(exc).__name__)
            if broadcast_attempted:
                return SendResult(False, tx_hash,
                    "MANUAL_REVIEW: Broadcast/receipt Solana belum pasti; jangan kirim ulang.",
                    f"{self.explorer_base}/tx/{tx_hash}")
            return SendResult(False, error_message=f"MANUAL_REVIEW: Preflight Solana gagal ({type(exc).__name__}).")
