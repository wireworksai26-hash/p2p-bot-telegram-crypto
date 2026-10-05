"""
services/wallet_detector.py — Auto-detect Blockchain Network dari Format Alamat Wallet
========================================================================================
Mendeteksi jaringan blockchain dari format/pola alamat wallet yang diinput user.
Mendukung: EVM (BSC/ETH/Polygon/Arbitrum/Base), Solana, Tron, SUI, TON, Bitcoin.
"""

import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
#  Pola Regex per Jaringan
# ─────────────────────────────────────────────────────────

NETWORK_PATTERNS: dict[str, dict] = {
    "EVM": {
        "chain_type": "EVM",
        "chains": ["BSC", "ETH", "POLYGON", "ARBITRUM", "BASE"],
        "pattern": re.compile(r"^0x[a-fA-F0-9]{40}$"),
        "emoji": "🔷",
        "label": "EVM (BSC / ETH / Polygon / Arbitrum / Base)",
    },
    "SOLANA": {
        "chain_type": "SOLANA",
        "chains": ["SOLANA"],
        "pattern": re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
        "emoji": "🟣",
        "label": "Solana",
    },
    "TRON": {
        "chain_type": "TRON",
        "chains": ["TRC20"],
        "pattern": re.compile(r"^T[a-zA-Z0-9]{33}$"),
        "emoji": "🔴",
        "label": "Tron (TRC20)",
    },
    "SUI": {
        "chain_type": "SUI",
        "chains": ["SUI"],
        "pattern": re.compile(r"^0x[a-fA-F0-9]{64}$"),
        "emoji": "🔵",
        "label": "SUI",
    },
    "TON": {
        "chain_type": "TON",
        "chains": ["TON"],
        "pattern": re.compile(r"^(EQ|UQ)[a-zA-Z0-9_-]{46}$"),
        "emoji": "💎",
        "label": "TON",
    },
    "BITCOIN": {
        "chain_type": "BITCOIN",
        "chains": ["BTC"],
        "pattern": re.compile(r"^(bc1[a-z0-9]{6,87}|[13][a-zA-HJ-NP-Z0-9]{25,34})$"),
        "emoji": "🟠",
        "label": "Bitcoin (BTC)",
    },
}

# Urutan deteksi: SUI sebelum EVM (karena keduanya 0x), BITCOIN/TON/TRON sebelum SOLANA (karena Base58)
DETECTION_ORDER = ["SUI", "EVM", "TRON", "TON", "BITCOIN", "SOLANA"]

# EVM sub-chains yang bisa dipilih user
EVM_CHAINS = ["BSC", "ETH", "POLYGON", "ARBITRUM", "BASE"]

# Network label untuk display
NETWORK_DISPLAY = {
    "BSC":      "BSC (BNB Smart Chain)",
    "ETH":      "Ethereum (ERC20)",
    "POLYGON":  "Polygon (MATIC)",
    "ARBITRUM": "Arbitrum",
    "BASE":     "Base",
    "SOLANA":   "Solana",
    "TRC20":    "Tron (TRC20)",
    "SUI":      "SUI",
    "TON":      "TON",
    "BTC":      "Bitcoin",
}


def detect_wallet_network(address: str) -> Optional[dict]:
    """
    Mendeteksi chain_type dari format alamat wallet.

    Returns:
        dict dengan keys: chain_type, chains, emoji, label — atau None jika tidak dikenal.

    Example:
        detect_wallet_network("0xAbC1234...") -> {
            "chain_type": "EVM",
            "chains": ["BSC", "ETH", "POLYGON", "ARBITRUM", "BASE"],
            "emoji": "🔷",
            "label": "EVM (BSC / ETH / Polygon / Arbitrum / Base)"
        }
    """
    if not address or not isinstance(address, str):
        return None

    address = address.strip()
    if not address:
        return None

    for chain_type in DETECTION_ORDER:
        meta = NETWORK_PATTERNS[chain_type]
        if meta["pattern"].match(address):
            return {
                "chain_type": chain_type,
                "chains": meta["chains"],
                "emoji": meta["emoji"],
                "label": meta["label"],
            }

    return None


def validate_wallet_address(address: str, network: str) -> bool:
    """
    Validasi apakah format alamat sesuai dengan jaringan yang dipilih.

    Args:
        address: Alamat wallet yang akan divalidasi.
        network: Jaringan target (BSC, ETH, SOLANA, TRC20, SUI, TON, BTC, dll.)

    Returns:
        True jika format valid, False jika tidak.
    """
    if not address or not network:
        return False

    address = address.strip()
    network = network.upper()

    # Mapping network ke chain_type
    network_to_chain = {
        "BSC": "EVM", "ETH": "EVM", "POLYGON": "EVM", "ARBITRUM": "EVM", "BASE": "EVM",
        "ERC20": "EVM",
        "SOLANA": "SOLANA",
        "TRC20": "TRON", "TRON": "TRON",
        "SUI": "SUI",
        "TON": "TON",
        "BTC": "BITCOIN", "BITCOIN": "BITCOIN",
    }

    chain_type = network_to_chain.get(network)
    if not chain_type:
        logger.warning(f"validate_wallet_address: network tidak dikenal: {network}")
        return False

    meta = NETWORK_PATTERNS.get(chain_type)
    if not meta:
        return False

    return bool(meta["pattern"].match(address))


def get_chain_type_for_network(network: str) -> Optional[str]:
    """
    Mendapatkan chain_type dari nama jaringan spesifik.
    Contoh: 'BSC' -> 'EVM', 'SOLANA' -> 'SOLANA', 'TRC20' -> 'TRON'
    """
    network = network.upper()
    mapping = {
        "BSC": "EVM", "ETH": "EVM", "POLYGON": "EVM", "ARBITRUM": "EVM", "BASE": "EVM",
        "ERC20": "EVM",
        "SOLANA": "SOLANA",
        "TRC20": "TRON", "TRON": "TRON",
        "SUI": "SUI",
        "TON": "TON",
        "BTC": "BITCOIN", "BITCOIN": "BITCOIN",
    }
    return mapping.get(network)


def format_wallet_short(address: str, chars: int = 6) -> str:
    """
    Format alamat wallet menjadi versi pendek: 0xABCD...1234
    Berguna untuk tampilan di Telegram.
    """
    if not address or len(address) <= chars * 2:
        return address or ""
    return f"{address[:chars]}...{address[-chars:]}"


def get_networks_for_chain_type(chain_type: str) -> list[str]:
    """
    Mendapatkan daftar jaringan yang tersedia untuk chain_type tertentu.
    Contoh: 'EVM' -> ['BSC', 'ETH', 'POLYGON', 'ARBITRUM', 'BASE']
    """
    meta = NETWORK_PATTERNS.get(chain_type.upper())
    if meta:
        return meta["chains"]
    return []
