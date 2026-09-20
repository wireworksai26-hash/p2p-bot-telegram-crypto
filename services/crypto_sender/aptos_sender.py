"""
services/crypto_sender/aptos_sender.py — Sender untuk Aptos Network.
================================================================
Mengintegrasikan pengecekan saldo dan pengiriman koin APT di Aptos Network
memakai SDK resmi (aptos-sdk, async client): tanda tangan lokal, sequence
number otomatis, simulasi sebelum broadcast, dan verifikasi on-chain.
"""

import logging
import re
from decimal import Decimal

import httpx

from config.settings import settings
from services.crypto_sender import BaseCryptoSender, SendResult

logger = logging.getLogger(__name__)

OCTAS_PER_APT = 10**8


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
                    return int(data[0]) / OCTAS_PER_APT
            except Exception as exc:
                last_error = exc
                logger.warning("Gagal mengambil saldo Aptos via %s: %s", rpc, exc)
        raise RuntimeError(f"Semua endpoint Aptos gagal membaca saldo APT: {last_error}")

    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Kirim APT: simulasi dulu, broadcast sekali, sukses hanya setelah on-chain."""
        try:
            from aptos_sdk.account import Account
            from aptos_sdk.account_address import AccountAddress
            from aptos_sdk.async_client import ClientConfig, RestClient
            from aptos_sdk.transactions import (
                EntryFunction,
                Serializer,
                TransactionArgument,
                TransactionPayload,
            )
        except ImportError:
            return SendResult(
                success=False,
                error_message="MANUAL_REVIEW: Library aptos-sdk tidak tersedia di server ini.",
            )

        if symbol.upper() != "APT":
            return SendResult(False, error_message="MANUAL_REVIEW: Aset Aptos tidak valid.")
        if not self.validate_address(to_address):
            return SendResult(False, error_message="MANUAL_REVIEW: Alamat Aptos tidak valid.")
        quantity = Decimal(str(amount))
        if not quantity.is_finite() or quantity <= 0:
            return SendResult(False, error_message="MANUAL_REVIEW: Nominal APT tidak valid.")
        units = int(quantity * OCTAS_PER_APT)
        if units <= 0:
            return SendResult(False, error_message="MANUAL_REVIEW: Nominal di bawah unit minimum.")

        if not settings.APTOS_PRIVATE_KEY:
            return SendResult(
                success=False,
                error_message="MANUAL_REVIEW: APTOS_PRIVATE_KEY belum di-set di server.",
            )
        raw_key = settings.APTOS_PRIVATE_KEY.strip()
        if raw_key.startswith("ed25519-priv-"):
            key = raw_key
        else:
            key = "ed25519-priv-0x" + raw_key.removeprefix("0x").removeprefix("0X")
        try:
            account = Account.load_key(key)
        except Exception as exc:
            logger.warning("Gagal memuat APTOS_PRIVATE_KEY (%s)", type(exc).__name__)
            return SendResult(
                success=False,
                error_message="MANUAL_REVIEW: Sender Aptos gagal memuat APTOS_PRIVATE_KEY (format kunci).",
            )
        if str(account.address()).lower().lstrip("0") != str(self.wallet_address).lower().lstrip("0"):
            return SendResult(
                success=False,
                error_message="MANUAL_REVIEW: Key Aptos tidak cocok dengan alamat stok.",
            )

        from services.crypto_sender.evm_sender import _get_network_send_lock

        async with _get_network_send_lock("APTOS"):
            client = RestClient(
                self.rpc_list[0],
                client_config=ClientConfig(transaction_wait_in_seconds=40),
            )
            tx_hash = ""
            try:
                onchain = await client.account_balance(account.address())
                cfg = client.client_config
                fee_buffer = cfg.max_gas_amount * cfg.gas_unit_price
                if onchain < units + fee_buffer:
                    return SendResult(
                        success=False,
                        error_message=(
                            f"Saldo APT tidak cukup. Saldo: {onchain / OCTAS_PER_APT}, "
                            f"Kebutuhan: {amount} APT + gas."
                        ),
                    )

                payload = EntryFunction.natural(
                    "0x1::aptos_account",
                    "transfer",
                    [],
                    [
                        TransactionArgument(AccountAddress.from_str(to_address), Serializer.struct),
                        TransactionArgument(units, Serializer.u64),
                    ],
                )
                raw = await client.create_bcs_transaction(account, TransactionPayload(payload))

                try:
                    simulation = await client.simulate_transaction(raw, account, estimate_gas_usage=True)
                except Exception as exc:
                    logger.warning("Simulasi Aptos gagal (%s)", type(exc).__name__)
                    return SendResult(False, error_message="MANUAL_REVIEW: Simulasi transfer APT gagal; tidak dibroadcast.")
                if not simulation or not simulation[0].get("success"):
                    vm_status = (simulation or [{}])[0].get("vm_status", "tidak diketahui")
                    return SendResult(
                        success=False,
                        error_message=f"MANUAL_REVIEW: Simulasi transfer APT ditolak ({str(vm_status)[:180]}).",
                    )

                from aptos_sdk.transactions import SignedTransaction

                signed = SignedTransaction(raw, account.sign_transaction(raw))
                tx_hash = await client.submit_bcs_transaction(signed)
                try:
                    await client.wait_for_transaction(tx_hash)
                except Exception as exc:
                    logger.warning("Receipt Aptos belum pasti (%s)", type(exc).__name__)
                    return SendResult(
                        success=False,
                        tx_hash=tx_hash,
                        error_message="MANUAL_REVIEW: Transaksi APT sudah dibroadcast, receipt belum terkonfirmasi.",
                        explorer_url=f"{self.explorer_base}/txn/{tx_hash}?network=mainnet",
                    )
                return SendResult(
                    success=True,
                    tx_hash=tx_hash,
                    explorer_url=f"{self.explorer_base}/txn/{tx_hash}?network=mainnet",
                )
            except Exception as exc:
                logger.warning("Pengiriman Aptos gagal (%s): %s", type(exc).__name__, str(exc)[:200])
                return SendResult(
                    success=False,
                    tx_hash=tx_hash,
                    error_message=f"MANUAL_REVIEW: Pengiriman APT gagal ({type(exc).__name__}).",
                    explorer_url=f"{self.explorer_base}/txn/{tx_hash}?network=mainnet" if tx_hash else "",
                )
            finally:
                await client.close()
