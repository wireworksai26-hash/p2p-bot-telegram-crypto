"""
services/crypto_sender/evm_sender.py — Sender untuk Rantai EVM.
============================================================
Mengintegrasikan pengiriman token native & ERC20 (USDT) di BSC, ETH, AVAX,
POLYGON, BASE, ARB, dan GRAVITY menggunakan Web3.py.
"""

import logging
import asyncio
from decimal import Decimal
from web3 import Web3
from config.settings import settings
from services.crypto_sender import BaseCryptoSender, SendResult

logger = logging.getLogger(__name__)

# Standard ERC-20 ABI minimal untuk check decimal, balance, dan transfer
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "transfer",
        "outputs": [{"name": "success", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    }
]

_network_send_locks = {}

def _get_network_send_lock(network: str) -> asyncio.Lock:
    if network not in _network_send_locks:
        _network_send_locks[network] = asyncio.Lock()
    return _network_send_locks[network]


class EVMSender(BaseCryptoSender):
    GAS_REVIEW_LIMIT_IDR = 15000
    # Konfigurasi tiap chain EVM beserta daftar RPC Fallback
    EVM_CHAINS = {
        "ETH": {
            "rpc_list": [
                settings.ETH_RPC,
                "https://ethereum-rpc.publicnode.com",
                "https://1rpc.io/eth",
                "https://eth.drpc.org",
                "https://cloudflare-eth.com",
            ],
            "chain_id": 1,
            "explorer": "https://etherscan.io",
            "native_symbol": "ETH",
            "tokens": {
                "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
                "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
            }
        },
        "BSC": {
            "rpc_list": [
                settings.BSC_RPC,
                "https://bsc-dataseed.bnbchain.org",
                "https://bsc-dataseed1.bnbchain.org",
                "https://bsc-rpc.publicnode.com",
                "https://1rpc.io/bnb",
            ],
            "chain_id": 56,
            "explorer": "https://bscscan.com",
            "native_symbol": "BNB",
            "tokens": {
                "USDT": "0x55d398326f99059fF775485246999027B3197955",
                "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32CD580d"
            }
        },
        "AVAX": {
            "rpc_list": [
                settings.AVAX_RPC,
                "https://api.avax.network/ext/bc/C/rpc",
                "https://avalanche-c-chain-rpc.publicnode.com",
                "https://1rpc.io/avax/c",
            ],
            "chain_id": 43114,
            "explorer": "https://snowscan.xyz",
            "native_symbol": "AVAX",
            "tokens": {
                "USDT": "0x97082348230b92f14910e17d061f37fa62241f8c"
            }
        },
        "POLYGON": {
            "rpc_list": [
                settings.POLYGON_RPC,
                "https://polygon-bor-rpc.publicnode.com",
                "https://1rpc.io/matic",
            ],
            "chain_id": 137,
            "explorer": "https://polygonscan.com",
            "native_symbol": "MATIC",
            "tokens": {
                "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
                "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            }
        },
        "BASE": {
            "rpc_list": [
                settings.BASE_RPC,
                "https://mainnet.base.org",
                "https://base-rpc.publicnode.com",
                "https://1rpc.io/base",
            ],
            "chain_id": 8453,
            "explorer": "https://basescan.org",
            "native_symbol": "ETH",
            "tokens": {
                "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
            }
        },
        "ARB": {
            "rpc_list": [
                settings.ARB_RPC,
                "https://arb1.arbitrum.io/rpc",
                "https://arbitrum-one-rpc.publicnode.com",
                "https://1rpc.io/arb",
            ],
            "chain_id": 42161,
            "explorer": "https://arbiscan.io",
            "native_symbol": "ETH",
            "tokens": {
                "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
                "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
                "ARB": "0x912CE59144191C1204E64559FE8253a0e49E6548"
            }
        },
        "OPTIMISM": {
            "rpc_list": [
                settings.OPTIMISM_RPC,
                "https://mainnet.optimism.io",
                "https://optimism-rpc.publicnode.com",
                "https://1rpc.io/op",
            ],
            "chain_id": 10,
            "explorer": "https://optimistic.etherscan.io",
            "native_symbol": "ETH",
            "tokens": {}
        },
        "ROBINHOOD": {
            "rpc_list": [
                settings.ROBINHOOD_RPC,
                "https://rpc.mainnet.chain.robinhood.com",
            ],
            "chain_id": 4663,
            "explorer": "https://robinhoodchain.blockscout.com",
            "native_symbol": "ETH",
            "tokens": {}
        },
        "KAIA": {
            "rpc_list": [
                settings.KAIA_RPC,
                "https://public-en.node.kaia.io",
                "https://klaytn.drpc.org",
            ],
            "chain_id": 8217,
            "explorer": "https://kaiascan.io",
            "native_symbol": "KAIA",
            "tokens": {}
        },
        "BERA": {
            "rpc_list": [
                settings.BERA_RPC,
                "https://rpc.berachain.com",
                "https://berachain.drpc.org",
            ],
            "chain_id": 80094,
            "explorer": "https://berascan.com",
            "native_symbol": "BERA",
            "tokens": {}
        },
        "HYPEREVM": {
            "rpc_list": [
                settings.HYPEREVM_RPC,
                "https://rpc.hyperliquid.xyz/evm",
            ],
            "chain_id": 999,
            "explorer": "https://hyperevm.cloud",
            "native_symbol": "HYPE",
            "tokens": {}
        },
        "GRAVITY": {
            "rpc_list": [
                settings.GRAVITY_RPC,
                "https://rpc.gravity.xyz",
            ],
            "chain_id": 1625,
            "explorer": "https://gravityscan.com",
            "native_symbol": "G",
            "tokens": {
                "USDT": "0x2cBE28e83344199aa567DDe9F6e33E0b1A7f3aB8"
            }
        }
    }

    def __init__(self, network: str):
        self.network = network.upper()
        self.config = self.EVM_CHAINS.get(self.network)
        if not self.config:
            raise ValueError(f"EVM network '{network}' tidak terdaftar di EVMSender.")

        # Inisialisasi daftar RPC (membersihkan duplikat & nilai kosong)
        raw_rpcs = self.config.get("rpc_list", [])
        self.rpc_list = list(dict.fromkeys(r.strip() for r in raw_rpcs if r and r.strip()))
        if not self.rpc_list:
            raise ValueError(f"RPC {self.network} belum dikonfigurasi.")

        self.current_rpc_index = 0
        self.w3 = self._create_w3_instance(self.rpc_list[0])
        self.private_key = settings.EVM_PRIVATE_KEY
        self.wallet_address = Web3.to_checksum_address(settings.EVM_WALLET_ADDRESS)

    def _create_w3_instance(self, rpc_url: str) -> Web3:
        """Membuat instance Web3 dengan timeout 6 detik."""
        return Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 6.0}))

    def _rotate_rpc(self) -> Web3:
        """Rotasi ke RPC berikutnya dalam daftar fallback."""
        if len(self.rpc_list) <= 1:
            return self.w3
        self.current_rpc_index = (self.current_rpc_index + 1) % len(self.rpc_list)
        next_rpc = self.rpc_list[self.current_rpc_index]
        logger.info(f"[{self.network}] Beralih ke RPC fallback: {next_rpc}")
        self.w3 = self._create_w3_instance(next_rpc)
        return self.w3

    def validate_address(self, address: str) -> bool:
        """Validasi format alamat EVM."""
        try:
            return self.w3.is_address(address)
        except Exception:
            return False

    async def get_balance(self, symbol: str = "") -> float:
        """
        Ambil saldo wallet dengan auto-fallback RPC jika terjadi kegagalan koneksi.
        """
        symbol_upper = symbol.upper() if symbol else ""
        if self.network == "POLYGON" and symbol_upper == "POL":
            symbol_upper = "MATIC"
        native_sym = self.config["native_symbol"]
        token_address = None
        if symbol_upper and symbol_upper != native_sym:
            token_address = self.config["tokens"].get(symbol_upper)
            if not token_address:
                raise ValueError(
                    f"Token '{symbol_upper}' tidak terdaftar pada network {self.network}."
                )

        # Coba setiap RPC yang ada dalam daftar
        last_error = None
        for attempt in range(len(self.rpc_list)):
            try:
                # Never accept a balance from a misconfigured Robinhood/testnet RPC.
                if self.network in ("ROBINHOOD", "HYPEREVM"):
                    actual_chain_id = await asyncio.to_thread(lambda: self.w3.eth.chain_id)
                    if actual_chain_id != self.config["chain_id"]:
                        raise ValueError(f"Chain ID RPC {self.network} tidak sesuai mainnet.")
                # Jika symbol kosong atau merupakan native coin
                if not symbol_upper or symbol_upper == native_sym:
                    balance_wei = await asyncio.to_thread(self.w3.eth.get_balance, self.wallet_address)
                    balance = self.w3.from_wei(balance_wei, 'ether')
                    return float(balance)

                # Jika token ERC20 (misal USDT/USDC)
                checksum_token = Web3.to_checksum_address(token_address)
                contract = self.w3.eth.contract(address=checksum_token, abi=ERC20_ABI)

                decimals = await asyncio.to_thread(contract.functions.decimals().call)
                balance_raw = await asyncio.to_thread(contract.functions.balanceOf(self.wallet_address).call)

                balance = Decimal(balance_raw) / Decimal(10 ** decimals)
                return float(balance)

            except Exception as e:
                last_error = e
                if self.network == "ROBINHOOD":
                    logger.debug(f"Robinhood RPC unreachable: {e}")
                else:
                    logger.warning(f"Gagal mengambil saldo {symbol} di {self.network} via {self.rpc_list[self.current_rpc_index]}: {e}")
                if attempt < len(self.rpc_list) - 1:
                    self._rotate_rpc()
        raise RuntimeError(
            f"Semua RPC {self.network} gagal membaca saldo {symbol_upper or native_sym}: "
            f"{last_error or 'respons tidak valid'}"
        )

    async def _gas_review_reason(self, gas_limit: int, gas_price: int) -> str:
        """Return alasan manual review jika estimasi gas ETH L1 melewati batas."""
        if self.network != "ETH":
            return ""
        try:
            from services.price_service import price_service

            price = await price_service.get_price(self.config["native_symbol"])
            if not price:
                return "MANUAL_REVIEW: Harga ETH tidak tersedia untuk estimasi gas."
            gas_native = float(self.w3.from_wei(gas_limit * gas_price, "ether"))
            gas_idr = gas_native * float(price["buy_price_idr"])
            if gas_idr > self.GAS_REVIEW_LIMIT_IDR:
                return (
                    "MANUAL_REVIEW: Estimasi gas ETH L1 "
                    f"{gas_idr:,.0f} IDR melebihi batas {self.GAS_REVIEW_LIMIT_IDR:,} IDR."
                )
            return ""
        except Exception as exc:
            logger.warning("Gagal estimasi gas ETH L1: %s", exc)
            return "MANUAL_REVIEW: Estimasi gas ETH L1 tidak tersedia."

    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """
        Mengirim koin native atau token ERC20 (USDT).
        """
        try:
            if not Decimal(str(amount)).is_finite() or Decimal(str(amount)) <= 0:
                return SendResult(False, error_message="MANUAL_REVIEW: Nominal payout tidak valid.")
            if Web3().eth.account.from_key(self.private_key).address.lower() != self.wallet_address.lower():
                return SendResult(False, error_message="MANUAL_REVIEW: Key EVM tidak cocok dengan alamat stok.")
            to_checksum = Web3.to_checksum_address(to_address)
            symbol_upper = symbol.upper()
            if self.network == "POLYGON" and symbol_upper == "POL":
                symbol_upper = "MATIC"
            native_sym = self.config["native_symbol"]
            
            explorer_base = self.config["explorer"]
            
            # Cek balance dulu
            curr_balance = await self.get_balance(symbol_upper)
            if curr_balance < amount:
                return SendResult(
                    success=False,
                    error_message=f"Saldo hot wallet tidak cukup. Saldo: {curr_balance} {symbol_upper}, Kebutuhan: {amount} {symbol_upper}"
                )

            # Cek native balance untuk gas fee
            native_balance = await self.get_balance(native_sym)
            if native_balance <= 0:
                return SendResult(
                    success=False,
                    error_message=f"Saldo gas fee ({native_sym}) habis atau 0."
                )

            # Gunakan mutex lock per-network agar saat 100+ transaksi bersamaan,
            # pengambilan nonce 'pending', penandatanganan, dan broadcast berjalan berurutan
            # tanpa tabrakan nomor urut nonce (nonce collision).
            lock = _get_network_send_lock(self.network)
            async with lock:
                max_rpc_attempts = min(len(self.rpc_list), 3)
                tx_hash_bytes = None
                tx_hash_str = None
                last_rpc_error = None

                for rpc_attempt in range(max_rpc_attempts):
                    try:
                        chain_id = await asyncio.to_thread(lambda: self.w3.eth.chain_id)
                        if chain_id != self.config["chain_id"]:
                            raise ValueError(f"Chain ID RPC {self.network} tidak sesuai mainnet.")
                        def _get_nonce():
                            try:
                                return self.w3.eth.get_transaction_count(self.wallet_address, 'pending')
                            except Exception:
                                return self.w3.eth.get_transaction_count(self.wallet_address)

                        nonce = await asyncio.to_thread(_get_nonce)
                        
                        # Dapatkan gas price saat ini dengan buffer 10%
                        def _get_gas_price():
                            return self.w3.eth.gas_price
                        gas_price_raw = await asyncio.to_thread(_get_gas_price)
                        gas_price = int(gas_price_raw * 1.1)

                        gas_estimation_failed = False
                        if symbol_upper == native_sym:
                            # --- Kirim Native Coin (BNB, ETH, AVAX, MATIC, G) ---
                            amount_wei = self.w3.to_wei(amount, 'ether')
                            
                            tx = {
                                'nonce': nonce,
                                'to': to_checksum,
                                'value': amount_wei,
                                'gas': 21000,
                                'gasPrice': gas_price,
                                'chainId': self.config["chain_id"]
                            }
                            # Contract destinations and newer EVM chains may need
                            # more than the 21k EOA transfer minimum.
                            gas_estimate = await asyncio.to_thread(self.w3.eth.estimate_gas,
                                {**{k: v for k, v in tx.items() if k != 'gas'}, 'from': self.wallet_address})
                            tx['gas'] = max(21000, int(gas_estimate * 1.2))
                        else:
                            # --- Kirim ERC-20 Token (USDT) ---
                            token_address = self.config["tokens"].get(symbol_upper)
                            if not token_address:
                                return SendResult(
                                    success=False,
                                    error_message=f"Token '{symbol_upper}' tidak terdaftar di network {self.network}"
                                )
                            
                            checksum_token = Web3.to_checksum_address(token_address)
                            contract = self.w3.eth.contract(address=checksum_token, abi=ERC20_ABI)
                            
                            decimals = await asyncio.to_thread(contract.functions.decimals().call)
                            # Parse amount ke format raw token unit berdasarkan decimals
                            raw_amount = int(Decimal(str(amount)) * Decimal(10 ** decimals))
                            
                            # Build contract transfer function call
                            tx_data = contract.functions.transfer(to_checksum, raw_amount)
                            
                            # Estimate gas limit
                            try:
                                gas_estimate = await asyncio.to_thread(tx_data.estimate_gas, {'from': self.wallet_address})
                                gas_limit = int(gas_estimate * 1.2) # 20% safety margin
                            except Exception:
                                return SendResult(success=False, error_message="MANUAL_REVIEW: Simulasi transfer token gagal; tidak dibroadcast.")
                            
                            tx = tx_data.build_transaction({
                                'chainId': self.config["chain_id"],
                                'gas': gas_limit,
                                'gasPrice': gas_price,
                                'nonce': nonce,
                            })

                        if self.network == "ETH" and gas_estimation_failed:
                            return SendResult(
                                success=False,
                                error_message="MANUAL_REVIEW: Estimasi gas ETH L1 gagal.",
                            )

                        gas_review = await self._gas_review_reason(tx.get('gas', 21000), gas_price)
                        if gas_review:
                            return SendResult(success=False, error_message=gas_review)

                        # Sign transaction
                        required_wei = int(tx.get('value', 0)) + int(tx['gas']) * gas_price
                        if await asyncio.to_thread(self.w3.eth.get_balance, self.wallet_address) < required_wei:
                            return SendResult(success=False, error_message="MANUAL_REVIEW: Saldo native tidak cukup untuk nominal dan gas.")
                        signed_tx = self.w3.eth.account.sign_transaction(tx, private_key=self.private_key)
                        
                        # Broadcast transaction (masuk ke mempool on-chain)
                        raw = getattr(signed_tx, "raw_transaction", None) or getattr(signed_tx, "rawTransaction")
                        # Once broadcast has been attempted, an RPC timeout is
                        # ambiguous. Preserve the deterministic hash, never sign
                        # another nonce in a retry and risk a duplicate payout.
                        candidate_hash = self.w3.to_hex(self.w3.keccak(raw))
                        try:
                            tx_hash_bytes = await asyncio.to_thread(self.w3.eth.send_raw_transaction, raw)
                        except Exception:
                            return SendResult(success=False, tx_hash=candidate_hash,
                                error_message="MANUAL_REVIEW: Status broadcast belum pasti; cek hash sebelum mengirim ulang.",
                                explorer_url=f"{explorer_base}/tx/{candidate_hash}")
                        tx_hash_str = self.w3.to_hex(tx_hash_bytes)
                        break  # Sukses broadcast, keluar dari loop RPC retry

                    except Exception as rpc_err:
                        last_rpc_error = rpc_err
                        logger.warning(f"[{self.network}] Broadcast via RPC index {self.current_rpc_index} gagal: {rpc_err}")
                        if rpc_attempt < max_rpc_attempts - 1:
                            self._rotate_rpc()
                        else:
                            return SendResult(
                                success=False,
                                error_message=f"Gagal broadcast transaksi di {self.network}: {rpc_err}"
                            )

                if not tx_hash_bytes or not tx_hash_str:
                    return SendResult(
                        success=False,
                        error_message=f"Gagal broadcast transaksi setelah rotasi RPC: {last_rpc_error}"
                    )

            # --- Di Luar Lock (Asynchronous Parallel): Tunggu Receipt ---
            # Lock dilepas setelah broadcast agar transaksi berikutnya langsung dapat nonce selanjutnya
            explorer_url = f"{explorer_base}/tx/{tx_hash_str}"
            
            # Tunggu receipt transaksi (max 60 detik)
            try:
                receipt = await asyncio.to_thread(self.w3.eth.wait_for_transaction_receipt, tx_hash_bytes, timeout=60)
                if receipt.status == 1:
                    logger.info(f"Transaksi {self.network} sukses! Hash: {tx_hash_str}")
                    return SendResult(
                        success=True,
                        tx_hash=tx_hash_str,
                        explorer_url=explorer_url
                    )
                else:
                    logger.error(f"Transaksi {self.network} revert/gagal di blockchain. Hash: {tx_hash_str}")
                    return SendResult(
                        success=False,
                        tx_hash=tx_hash_str,
                        error_message="Transaksi di-revert oleh network blockchain.",
                        explorer_url=explorer_url
                    )
            except Exception as wait_exc:
                # Transaksi terkirim tapi receipt belum didapat (delay network)
                logger.warning(f"Transaksi terkirim tapi receipt belum didapat: {wait_exc}")
                return SendResult(
                    success=False,
                    tx_hash=tx_hash_str,
                    error_message="MANUAL_REVIEW: Transaksi sudah dibroadcast, menunggu receipt. Jangan kirim ulang.",
                    explorer_url=explorer_url
                )

        except Exception as e:
            logger.error(f"Error saat mengirim crypto di {self.network}: {e}", exc_info=True)
            return SendResult(
                success=False,
                error_message=f"Exception saat pengiriman EVM: {str(e)}"
            )
