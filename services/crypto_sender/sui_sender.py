"""
services/crypto_sender/sui_sender.py — Sender untuk Sui Network.
============================================================
Saldo dibaca via JSON-RPC Sui. Pengiriman SUI native memakai
ProgrammableTransactionBuilder dan BCS/signing dari pysui, dengan dry-run
sebelum broadcast dan verifikasi effects on-chain.
"""

import base64
import hashlib
import logging
import re
from decimal import Decimal

import base58
import httpx
from config.settings import settings
from services.crypto_sender import BaseCryptoSender, SendResult

logger = logging.getLogger(__name__)

SUI_COIN_TYPE = "0x2::sui::SUI"
MIST_PER_SUI = 10**9
# Sisa gas budget selalu direfund; 0.02 SUI cukup untuk transfer sederhana.
GAS_BUDGET_MIST = 20_000_000
MAX_GAS_COINS = 50

class SuiSender(BaseCryptoSender):
    def __init__(self):
        self.network = "SUI"
        raw_rpcs = [
            settings.SUI_RPC,
            "https://sui-mainnet-endpoint.blockvision.org",
        ]
        self.rpc_list = list(dict.fromkeys(r.strip() for r in raw_rpcs if r and r.strip()))
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

    async def _rpc_call(self, base_url: str, method: str, params: list) -> object:
        """Satu panggilan JSON-RPC; raise bila node menolak."""
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.post(
                base_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            res.raise_for_status()
            data = res.json()
        if data.get("error"):
            raise RuntimeError(f"{method}: {str(data['error'])[:180]}")
        return data.get("result")

    async def _pick_endpoint(self) -> str:
        """Pilih RPC pertama yang menjawab; dipakai untuk seluruh alur kirim."""
        last_error = None
        for rpc in self.rpc_list:
            try:
                await self._rpc_call(rpc, "sui_getLatestCheckpointSequenceNumber", [])
                return rpc
            except Exception as exc:
                last_error = exc
                logger.warning("RPC Sui %s tidak siap: %s", rpc, type(exc).__name__)
        raise RuntimeError(f"Semua RPC Sui gagal: {type(last_error).__name__}")

    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Kirim SUI native: dry-run dulu, broadcast sekali, sukses hanya setelah effects on-chain."""
        try:
            from pysui.sui.sui_bcs import bcs as sui_bcs
            from pysui.sui.sui_common.txn_transaction_builder import (
                ProgrammableTransactionBuilder,
                PureInput,
            )
        except ImportError:
            return SendResult(
                success=False,
                error_message="MANUAL_REVIEW: Library pysui tidak tersedia di server ini.",
            )

        if symbol.upper() != "SUI":
            return SendResult(False, error_message="MANUAL_REVIEW: Aset Sui tidak valid.")
        if not self.validate_address(to_address):
            return SendResult(False, error_message="MANUAL_REVIEW: Alamat Sui tidak valid.")
        quantity = Decimal(str(amount))
        if not quantity.is_finite() or quantity <= 0:
            return SendResult(False, error_message="MANUAL_REVIEW: Nominal SUI tidak valid.")
        units = int(quantity * MIST_PER_SUI)
        if units <= 0:
            return SendResult(False, error_message="MANUAL_REVIEW: Nominal di bawah unit minimum.")

        if not settings.SUI_PRIVATE_KEY:
            return SendResult(
                success=False,
                error_message="MANUAL_REVIEW: SUI_PRIVATE_KEY belum di-set di server.",
            )
        try:
            key_bytes = bytes.fromhex(settings.SUI_PRIVATE_KEY.removeprefix("0x").removeprefix("0X"))
            from nacl.signing import SigningKey

            public_key = bytes(SigningKey(key_bytes).verify_key)
            from pysui.sui.sui_crypto import keypair_from_keystring

            keypair = keypair_from_keystring(base64.b64encode(b"\x00" + key_bytes).decode())
        except Exception:
            return SendResult(
                success=False,
                error_message="MANUAL_REVIEW: Sender Sui gagal memuat SUI_PRIVATE_KEY (format kunci).",
            )
        derived_address = "0x" + hashlib.blake2b(b"\x00" + public_key, digest_size=32).hexdigest()
        if derived_address != str(self.wallet_address).lower():
            return SendResult(
                success=False,
                error_message="MANUAL_REVIEW: Key Sui tidak cocok dengan alamat stok.",
            )

        from services.crypto_sender.evm_sender import _get_network_send_lock

        async with _get_network_send_lock("SUI"):
            reference_digest = ""
            try:
                base_url = await self._pick_endpoint()
                needed = units + GAS_BUDGET_MIST
                coins = await self._rpc_call(
                    base_url, "suix_getCoins", [self.wallet_address, SUI_COIN_TYPE, None, MAX_GAS_COINS]
                )
                coin_rows = (coins or {}).get("data") or []
                payment, total = [], 0
                for row in coin_rows:
                    payment.append(
                        sui_bcs.ObjectReference(
                            sui_bcs.Address.from_str(row["coinObjectId"]),
                            int(row["version"]),
                            sui_bcs.Digest.from_str(row["digest"]),
                        )
                    )
                    total += int(row["balance"])
                    if total >= needed:
                        break
                if total < needed:
                    return SendResult(
                        success=False,
                        error_message=(
                            f"Saldo SUI tidak cukup. Saldo: {total / MIST_PER_SUI}, "
                            f"Kebutuhan: {amount} SUI + gas."
                        ),
                    )

                gas_price = int(await self._rpc_call(base_url, "suix_getReferenceGasPrice", []))
                builder = ProgrammableTransactionBuilder()
                builder.transfer_sui(
                    recipient=PureInput.as_input(sui_bcs.Address.from_str(to_address)),
                    from_coin=sui_bcs.Argument("GasCoin"),
                    amount=PureInput.as_input(sui_bcs.SuiU64(units)),
                )
                tx_data = sui_bcs.TransactionData(
                    "V1",
                    sui_bcs.TransactionDataV1(
                        builder.finish_for_inspect(),
                        sui_bcs.Address.from_str(self.wallet_address),
                        sui_bcs.GasData(
                            payment,
                            sui_bcs.Address.from_str(self.wallet_address),
                            gas_price,
                            GAS_BUDGET_MIST,
                        ),
                        sui_bcs.TransactionExpiration("None", None),
                    ),
                )
                raw_tx = bytes(tx_data.serialize())
                tx_b64 = base64.b64encode(raw_tx).decode()
                reference_digest = base58.b58encode(
                    hashlib.blake2b(b"TransactionData::" + raw_tx, digest_size=32).digest()
                ).decode()

                dry_run = await self._rpc_call(base_url, "sui_dryRunTransactionBlock", [tx_b64])
                effects = (dry_run or {}).get("effects") or {}
                status = (effects.get("status") or {}).get("status")
                if status != "success":
                    detail = (effects.get("status") or {}).get("error") or dry_run
                    logger.warning("Dry-run Sui gagal: %s", str(detail)[:200])
                    return SendResult(
                        success=False,
                        error_message=f"MANUAL_REVIEW: Dry-run transfer SUI ditolak ({str(detail)[:160]}).",
                    )
                gas_used = (effects.get("gasUsed") or {})
                total_gas = (
                    int(gas_used.get("computationCost", 0))
                    + int(gas_used.get("storageCost", 0))
                    - int(gas_used.get("storageRebate", 0))
                )
                if total_gas > GAS_BUDGET_MIST:
                    return SendResult(
                        success=False,
                        error_message="MANUAL_REVIEW: Estimasi gas SUI melebihi budget; tidak dibroadcast.",
                    )

                signature = keypair.new_sign_secure(tx_b64)
                executed = await self._rpc_call(
                    base_url,
                    "sui_executeTransactionBlock",
                    [tx_b64, [signature], {"showEffects": True}, "WaitForLocalExecution"],
                )
                final_status = ((executed or {}).get("effects") or {}).get("status") or {}
                digest = (executed or {}).get("digest") or reference_digest
                if final_status.get("status") != "success":
                    logger.warning("Transaksi Sui gagal on-chain: %s", str(final_status)[:200])
                    return SendResult(
                        success=False,
                        tx_hash=digest,
                        error_message=f"MANUAL_REVIEW: Transaksi SUI gagal on-chain ({str(final_status.get('error'))[:140]}).",
                        explorer_url=f"{self.explorer_base}/mainnet/tx/{digest}",
                    )
                return SendResult(
                    success=True,
                    tx_hash=digest,
                    explorer_url=f"{self.explorer_base}/mainnet/tx/{digest}",
                )
            except Exception as exc:
                logger.warning("Pengiriman Sui gagal (%s): %s", type(exc).__name__, str(exc)[:200])
                return SendResult(
                    success=False,
                    tx_hash=reference_digest,
                    error_message=f"MANUAL_REVIEW: Pengiriman SUI gagal ({type(exc).__name__}).",
                    explorer_url=f"{self.explorer_base}/mainnet/tx/{reference_digest}" if reference_digest else "",
                )
