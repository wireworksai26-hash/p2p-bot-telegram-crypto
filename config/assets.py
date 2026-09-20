"""
config/assets.py — Master Daftar Aset (Symbol, Network) yang Dipantau Stoknya.
==============================================================================
Satu sumber kebenaran untuk sinkronisasi saldo hot wallet per (network, symbol).
Semua pasangan yang bisa dibeli/dijual user harus terdaftar di sini agar
stoknya muncul dengan nominal asli di fitur "Cek Stok".
"""

import os

STOCK_MAX_AGE_SECONDS = 15 * 60
# Emergency brake: jaringan di sini ditolak di alur beli/convert dan payout-nya
# manual. SUI dan APTOS sudah auto-payout (sender-nya aktif dan teruji).
MANUAL_PAYOUT_NETWORKS: set[str] = set()


def get_wallet_address(network: str) -> str:
    from config.settings import settings
    non_evm = {
        "SOLANA": settings.SOL_WALLET_ADDRESS,
        "TRON": settings.TRX_WALLET_ADDRESS,
        "TON": settings.TON_WALLET_ADDRESS,
        "SUI": settings.SUI_WALLET_ADDRESS,
        "APTOS": settings.APTOS_WALLET_ADDRESS,
    }
    return non_evm.get(network.upper(), settings.EVM_WALLET_ADDRESS)

# Pasangan (SYMBOL, NETWORK) yang stoknya disinkronkan dari blockchain.
# Termasuk native coin tiap chain + token yang diperjualbelikan bot.
STOCK_ASSETS = [
    # --- EVM: USDT / USDC / Native ---
    ("USDT", "BSC"),
    ("USDT", "POLYGON"),
    ("USDT", "ARB"),
    ("USDT", "ETH"),
    ("USDC", "BASE"),
    ("USDC", "ETH"),
    ("USDC", "BSC"),
    ("USDC", "ARB"),
    ("USDC", "POLYGON"),
    ("BNB", "BSC"),
    ("ETH", "BASE"),
    ("ETH", "ARB"),
    ("ETH", "OPTIMISM"),
    ("ETH", "ROBINHOOD"),
    ("ETH", "ETH"),
    ("MATIC", "POLYGON"),
    ("ARB", "ARB"),
    ("AVAX", "AVAX"),
    ("KAIA", "KAIA"),
    ("BERA", "BERA"),
    ("HYPE", "HYPEREVM"),
    ("G", "GRAVITY"),
    # --- Non-EVM: native + token ---
    ("SOL", "SOLANA"),
    ("USDT", "SOLANA"),
    ("USDC", "SOLANA"),
    ("TRX", "TRON"),
    ("USDT", "TRON"),
    ("TON", "TON"),
    ("USDT", "TON"),
    ("SUI", "SUI"),
    ("APT", "APTOS"),
]

# Alamat kontrak token untuk network non-EVM (untuk cek saldo token).
NON_EVM_TOKENS = {
    "SOLANA": {
        "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    },
    "TRON": {
        "USDT": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
    },
    "TON": {
        "USDT": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
    },
}

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get_qris_static_image_path() -> str | None:
    """Mencari path absolut file gambar QRIS statis."""
    candidates = [
        os.path.join(_BASE_DIR, "Qris statis.jpeg"),
        os.path.join(_BASE_DIR, "Qris statis.jpg"),
        os.path.join(_BASE_DIR, "qris_statis.jpeg"),
        os.path.join(_BASE_DIR, "qris statis.jpeg"),
        "Qris statis.jpeg",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None

# Gambar QRIS Statis merchant (fallback path)
QRIS_STATIC_IMAGE = get_qris_static_image_path() or "Qris statis.jpeg"
