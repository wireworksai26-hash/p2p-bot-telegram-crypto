"""
services/crypto_sender/tron_sender.py — Sender untuk TRON (TRX).
==================================================================
Menangani pengiriman koin TRX native ke wallet customer menggunakan tronpy.
"""

import logging
import asyncio
from decimal import Decimal
from config.settings import settings
from services.crypto_sender import BaseCryptoSender, SendResult

# Tronpy imports
try:
    from tronpy import Tron
    from tronpy.keys import PrivateKey, to_hex_address
    from tronpy.providers.http import HTTPProvider
    TRONPY_AVAILABLE = True
except ImportError:
    TRONPY_AVAILABLE = False

logger = logging.getLogger(__name__)

# Alamat kontrak USDT TRC-20
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

class TronSender(BaseCryptoSender):
    def __init__(self):
        self.explorer_base = "https://tronscan.org"
        self.wallet_address = settings.TRX_WALLET_ADDRESS
        self.private_key_hex = settings.TRX_PRIVATE_KEY

    def validate_address(self, address: str) -> bool:
        """Validasi address TRON: dimulai dengan huruf T dan panjang 34 karakter."""
        try:
            if not address or len(address) != 34 or not address.startswith("T"):
                return False
            # Jika library tronpy terinstall, gunakan check internal
            if TRONPY_AVAILABLE:
                from tronpy.keys import is_address
                return is_address(address)
            return True
        except Exception:
            return False

    async def get_balance(self, symbol: str = "") -> float:
        """Mengambil saldo TRX native atau token TRC-20 (USDT)."""
        if not self.wallet_address:
            raise RuntimeError("TRX_WALLET_ADDRESS belum dikonfigurasi.")
        sym = symbol.upper() if symbol else ""
        if sym in ("USDT",):
            return await self._get_trc20_balance(USDT_TRC20)
        if sym not in ("", "TRX"):
            raise ValueError(f"Token '{sym}' tidak didukung pada TRON.")

        # Coba via TronGrid REST API (lebih cepat & bebas rate-limit 429)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.get(f"https://api.trongrid.io/v1/accounts/{self.wallet_address}")
                res.raise_for_status()
                payload = res.json()
                data = payload.get("data")
                if payload.get("success") is False or not isinstance(data, list):
                    raise ValueError("Respons saldo TRON tidak valid.")
                if data:
                    return int(data[0].get("balance", 0)) / 1e6
                return 0.0
        except Exception as api_err:
            logger.warning(f"TronGrid REST API get_balance gagal: {api_err}")

        if not TRONPY_AVAILABLE:
            raise RuntimeError("RPC TRON gagal membaca saldo dan SDK fallback tidak tersedia.")
        try:
            client = Tron()
            balance_sun = await asyncio.to_thread(client.get_account_balance, self.wallet_address)
            return float(balance_sun)
        except Exception as e:
            if e.__class__.__name__ == "AddressNotFound":
                return 0.0
            raise RuntimeError("RPC TRON gagal membaca saldo TRX.") from e

    async def _get_trc20_balance(self, contract_address: str) -> float:
        """
        Mengambil saldo token TRC-20 (USDT) milik wallet bot.
        """
        if not self.wallet_address:
            raise RuntimeError("TRX_WALLET_ADDRESS belum dikonfigurasi.")

        # Coba via TronGrid REST API
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.get(f"https://api.trongrid.io/v1/accounts/{self.wallet_address}")
                res.raise_for_status()
                payload = res.json()
                data = payload.get("data")
                if payload.get("success") is False or not isinstance(data, list):
                    raise ValueError("Respons saldo TRC20 tidak valid.")
                if data:
                    for item in data[0].get("trc20", []):
                        if contract_address in item:
                            return int(item[contract_address]) / 1e6
                return 0.0
        except Exception as api_err:
            logger.warning(f"TronGrid REST API _get_trc20_balance gagal: {api_err}")

        if not TRONPY_AVAILABLE:
            raise RuntimeError("RPC TRON gagal membaca saldo token dan SDK fallback tidak tersedia.")
        try:
            client = Tron(provider=HTTPProvider(timeout=20.0))
            contract = await asyncio.to_thread(client.get_contract, contract_address)
            raw_balance = await asyncio.to_thread(
                contract.functions.balanceOf, self.wallet_address
            )
            try:
                decimals = int(await asyncio.to_thread(contract.functions.decimals))
            except Exception:
                decimals = 6
            return float(int(raw_balance) / (10 ** decimals))
        except Exception as e:
            raise RuntimeError("RPC TRON gagal membaca saldo token.") from e

    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Mengirim TRX native atau USDT TRC-20."""
        if not TRONPY_AVAILABLE:
            return SendResult(
                success=False,
                error_message="Library tronpy tidak tersedia di server ini."
            )
        tx_hash = ""
        try:
            quantity = Decimal(str(amount))
            if symbol.upper() not in ("TRX", "USDT") or not quantity.is_finite() or quantity <= 0:
                return SendResult(False, error_message="MANUAL_REVIEW: Aset/nominal TRON tidak valid.")
            # Validasi input
            if not self.validate_address(to_address):
                return SendResult(success=False, error_message="Alamat wallet TRON tidak valid.")

            # Load private key
            try:
                priv_key = PrivateKey(bytes.fromhex(self.private_key_hex.removeprefix("0x")))
            except Exception as key_err:
                return SendResult(
                    success=False,
                    error_message=f"Gagal memuat TRON Private Key (pastikan format hex): {key_err}"
                )

            if priv_key.public_key.to_base58check_address() != self.wallet_address:
                return SendResult(False, error_message="MANUAL_REVIEW: Key TRON tidak cocok dengan alamat stok.")
            client = Tron(provider=HTTPProvider(endpoint_uri=settings.TRX_RPC,
                          api_key=settings.TRONGRID_API_KEY or None, timeout=20.0))
            symbol_upper = symbol.upper()

            if symbol_upper == "USDT":
                token_balance = await self.get_balance("USDT")
                if token_balance < amount:
                    return SendResult(
                        success=False,
                        error_message=(
                            f"Saldo USDT TRC-20 tidak cukup. Saldo: {token_balance}, "
                            f"Kebutuhan: {amount}"
                        ),
                    )
                contract = await asyncio.to_thread(client.get_contract, USDT_TRC20)

                try:
                    address_param = to_hex_address(to_address)[2:].lower().zfill(64)
                    amount_param = hex(int(amount * 1_000_000))[2:].zfill(64)
                    energy = await asyncio.to_thread(
                        client.get_estimated_energy,
                        self.wallet_address,
                        USDT_TRC20,
                        "transfer(address,uint256)",
                        address_param + amount_param,
                    )
                    chain_params = await asyncio.to_thread(client.get_chain_parameters)
                    energy_fee = next(
                        int(item["value"])
                        for item in chain_params
                        if item.get("key") == "getEnergyFee"
                    )
                    trx_cost = energy * energy_fee / 1_000_000
                    from services.price_service import price_service
                    price = await price_service.get_price("TRX")
                    if not price:
                        return SendResult(
                            success=False,
                            error_message="MANUAL_REVIEW: Harga TRX tidak tersedia untuk estimasi gas.",
                        )
                    gas_idr = trx_cost * float(price["buy_price_idr"])
                    if gas_idr > 2000:
                        return SendResult(
                            success=False,
                            error_message=(
                                "MANUAL_REVIEW: Estimasi biaya energi TRON "
                                f"{gas_idr:,.0f} IDR melebihi batas 2.000 IDR."
                            ),
                        )
                except Exception as gas_err:
                    logger.warning("Gagal estimasi energi TRC-20: %s", gas_err)
                    return SendResult(
                        success=False,
                        error_message="MANUAL_REVIEW: Estimasi energi TRC-20 tidak tersedia.",
                    )

                def _build_and_sign():
                    return (
                        contract.functions.transfer(
                            to_address, int(quantity * 1_000_000)
                        )
                        .with_owner(self.wallet_address)
                        .fee_limit(10_000_000)
                        .build()
                        .sign(priv_key)
                    )
            else:
                balance = await self.get_balance()
                if balance < amount:
                    return SendResult(
                        success=False,
                        error_message=f"Saldo TRX tidak cukup. Saldo: {balance} TRX, Kebutuhan: {amount} TRX"
                    )
                amount_sun = int(quantity * 1_000_000)

                def _build_and_sign():
                    return (
                        client.trx.transfer(self.wallet_address, to_address, amount_sun)
                        .build()
                        .sign(priv_key)
                    )

            txn = await asyncio.to_thread(_build_and_sign)

            # Save the deterministic id before the one and only broadcast attempt.
            tx_hash = txn.txid
            if not tx_hash:
                return SendResult(False, error_message="MANUAL_REVIEW: TXID TRON belum tersedia.")
            result = await asyncio.to_thread(txn.broadcast)
            
            # Cek hasil broadcast
            if result.get("txid") != tx_hash:
                raise RuntimeError("TXID broadcast tidak cocok.")
            receipt = await asyncio.to_thread(result.wait, timeout=45, solid=True)
            if receipt.get("id") != tx_hash or not receipt.get("blockNumber"):
                raise RuntimeError("Receipt TRON belum solid.")
            if receipt.get("result") == "FAILED" or receipt.get("receipt", {}).get("result") not in (None, "SUCCESS"):
                raise RuntimeError("Transaksi TRON gagal on-chain.")

            # Tunggu konfirmasi transaksi
            # Di TRON, broadcast yang sukses biasanya langsung masuk mempool.
            # Kita bisa asumsikan terkirim jika broadcast mengembalikan status SUCCESS
            # atau memiliki txid.
            logger.info(f"Transaksi TRON berhasil dikirim. Hash: {tx_hash}")
            explorer_url = f"{self.explorer_base}/#/transaction/{tx_hash}"
            
            return SendResult(
                success=True,
                tx_hash=tx_hash,
                explorer_url=explorer_url
            )

        except Exception as e:
            logger.warning("Pengiriman TRON belum selesai (%s)", type(e).__name__)
            return SendResult(
                success=False,
                tx_hash=tx_hash,
                error_message=f"MANUAL_REVIEW: Pengiriman TRON belum terkonfirmasi ({type(e).__name__}); cek receipt sebelum kirim ulang.",
                explorer_url=f"{self.explorer_base}/#/transaction/{tx_hash}" if tx_hash else ""
            )
