"""
services/crypto_sender/__init__.py — Interface Pengiriman Crypto Multi-chain.
=============================================================================
Mendefinisikan base interface untuk class pengirim crypto (EVM, Solana, Tron, Ton)
dan Factory pattern untuk mendapatkan instance sender yang tepat berdasarkan network.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class SendResult:
    success: bool
    tx_hash: str = ""
    error_message: str = ""
    explorer_url: str = ""


class BaseCryptoSender(ABC):
    @abstractmethod
    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """
        Kirim cryptocurrency ke alamat tujuan.
        
        Args:
            to_address (str): Alamat wallet penerima.
            amount (float): Jumlah koin/token yang dikirim.
            symbol (str): Simbol koin/token (e.g. 'USDT', 'ETH', 'BNB').

        Returns:
            SendResult: Hasil transaksi (sukses, hash, error, explorer url).
        """
        pass

    @abstractmethod
    async def get_balance(self, symbol: str = "") -> float:
        """
        Ambil saldo hot wallet bot pada network ini.
        
        Args:
            symbol (str): Simbol token opsional. Jika kosong, mengembalikan native token balance.

        Returns:
            float: Jumlah saldo.
        """
        pass

    @abstractmethod
    def validate_address(self, address: str) -> bool:
        """
        Validasi format alamat wallet tujuan.
        """
        pass


class CryptoSenderFactory:
    _instances = {}

    @classmethod
    def get_sender(cls, network: str) -> BaseCryptoSender:
        """
        Factory method untuk mendapatkan instance sender berdasarkan nama network.
        Network yang didukung: BSC, ETH, AVAX, POLYGON, BASE, ARB, ROBINHOOD, KAIA, BERA, HYPEREVM, SOLANA, TRON, TON, SUI, APTOS.
        """
        net_upper = network.upper()
        
        if net_upper in cls._instances:
            return cls._instances[net_upper]

        # EVM-based networks
        evm_networks = ["BSC", "ETH", "AVAX", "POLYGON", "BASE", "ARB", "OPTIMISM", "ROBINHOOD", "KAIA", "BERA", "HYPEREVM"]
        
        if net_upper in evm_networks:
            from services.crypto_sender.evm_sender import EVMSender
            cls._instances[net_upper] = EVMSender(network=net_upper)
            
        elif net_upper == "SOLANA":
            from services.crypto_sender.solana_sender import SolanaSender
            cls._instances[net_upper] = SolanaSender()
            
        elif net_upper == "TRON":
            from services.crypto_sender.tron_sender import TronSender
            cls._instances[net_upper] = TronSender()

        elif net_upper == "TON":
            from services.crypto_sender.ton_sender import TonSender
            cls._instances[net_upper] = TonSender()

        elif net_upper == "SUI":
            from services.crypto_sender.sui_sender import SuiSender
            cls._instances[net_upper] = SuiSender()

        elif net_upper == "APTOS":
            from services.crypto_sender.aptos_sender import AptosSender
            cls._instances[net_upper] = AptosSender()
            
        else:
            raise ValueError(f"Blockchain network '{network}' tidak didukung.")

        return cls._instances[net_upper]
