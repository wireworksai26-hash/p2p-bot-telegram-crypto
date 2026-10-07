"""
bot/utils/validator.py — Fungsi Validasi Input untuk P2P Crypto Bot.
====================================================================
Menangani validasi alamat wallet berdasarkan blockchain network (EVM, Solana, Tron, LTC)
serta validasi nominal rupiah dan crypto.
"""

import math
import re
import base58
from web3 import Web3

def validate_wallet_address(address: str, network: str) -> bool:
    """
    Validasi alamat wallet berdasarkan network.
    
    Networks:
      - BSC, ETH, AVAX, POLYGON, BASE, ARB, ROBINHOOD (EVM)
      - SOLANA
      - TRON
    """
    if not address:
        return False
        
    address = address.strip()
    net = network.upper()

    # --- 1. EVM Chains ---
    evm_chains = [
        "BSC", "ETH", "AVAX", "POLYGON", "BASE", "ARB",
        "OPTIMISM", "ROBINHOOD", "KAIA", "BERA", "HYPEREVM", "ERC20", "BEP20",
    ]
    if net in evm_chains:
        try:
            return Web3.is_address(address)
        except Exception:
            return False

    # --- 2. Solana ---
    elif net == "SOLANA":
        try:
            if not (32 <= len(address) <= 44):
                return False
            # Cek decoding base58
            decoded = base58.b58decode(address)
            return len(decoded) == 32
        except Exception:
            return False

    # --- 3. TRON ---
    elif net == "TRON":
        try:
            if len(address) != 34 or not address.startswith("T"):
                return False
            # Checksum base58check: salah ketik satu huruf = koin hilang (tidak bisa dipulihkan).
            raw = base58.b58decode_check(address)
            return len(raw) == 21 and raw[0] == 0x41
        except Exception:
            return False

    # --- 4. TON ---
    elif net == "TON":
        if re.fullmatch(r"(?:0|-1):[0-9a-fA-F]{64}", address):
            return True
        if not re.fullmatch(r"(?:EQ|UQ)[A-Za-z0-9_-]{46}", address):
            return False
        try:
            from tonsdk.utils import Address
            Address(address).to_string(is_user_friendly=False)
            return True
        except Exception:
            return False

    # --- 5. SUI ---
    elif net == "SUI":
        return bool(re.fullmatch(r"0x[0-9a-fA-F]{64}", address))

    # --- 6. APTOS ---
    elif net == "APTOS":
        return bool(re.fullmatch(r"0x[0-9a-fA-F]{60,64}", address))

    return False


def validate_amount_idr(amount_str: str, min_amount: int = 5000, max_amount: int = 5_000_000) -> tuple[bool, int]:
    """
    Memvalidasi dan memparse string nominal rupiah.
    Mendukung format:
      - "50000"
      - "50.000"
      - "Rp 50.000" / "rp 50000" / "Rp. 50.000"
      - "50k" / "50K" / "50rb"
      - "50,000"
      
    Menolak secara ketat jika:
      - Terdeteksi sebagai alamat wallet (0x..., T..., Base58 panjang, dll.)
      - Mengandung huruf acak atau teks non-nominal
      - Kurang dari min_amount (default Rp 5.000)
      - Lebih dari max_amount (default Rp 5.000.000)
    
    Returns:
        tuple: (is_valid: bool, parsed_amount: int)
    """
    if not amount_str or not isinstance(amount_str, str):
        return False, 0

    text = amount_str.strip()

    # Tolak digit non-ASCII (Arab-Indic/fullwidth) yang lolos regex \d Unicode
    if not text.isascii():
        return False, 0

    # 1. Cek langsung jika input mirip alamat wallet blockchain
    if (
        text.startswith("0x")
        or text.startswith("0X")
        or (text.startswith("T") and len(text) == 34)
        or (text.startswith(("EQ", "UQ")) and len(text) >= 46)
        or len(text) > 30
    ):
        return False, 0

    # 2. Hapus awalan "Rp", "Rp.", "IDR"
    cleaned = re.sub(r"^(?:rp\.?|idr)\s*", "", text, flags=re.IGNORECASE).strip()

    # 3. Dukung singkatan umum seperti "50k", "50K", "50rb", "50ribu"
    k_match = re.match(r"^(\d+)\s*(?:k|rb|ribu)$", cleaned, flags=re.IGNORECASE)
    if k_match:
        try:
            val = int(k_match.group(1)) * 1000
            if val < min_amount or val > max_amount:
                return False, val
            return True, val
        except Exception:
            return False, 0

    # 4. Validasi pola angka murni Rupiah
    # Hanya boleh angka dengan pemisah ribuan titik/koma atau angka polos
    if not re.match(r"^(\d{1,3}(?:[.,]\d{3})+|\d+)$", cleaned):
        return False, 0

    try:
        raw_num = cleaned.replace(".", "").replace(",", "").strip()
        amount = int(raw_num)

        # Cek batas minimal dan batas maksimal
        if amount < min_amount or amount > max_amount:
            return False, amount

        return True, amount
    except Exception:
        return False, 0


def validate_crypto_amount(amount_str: str) -> tuple[bool, float]:
    """
    Memvalidasi dan memparse string nominal crypto (untuk sell flow).
    Mendukung format desimal menggunakan titik atau koma.
    
    Returns:
        tuple: (is_valid: bool, parsed_amount: float)
    """
    try:
        # Ganti koma dengan titik untuk desimal standar Python
        cleaned = amount_str.replace(",", ".").strip()
        
        # Validasi format float
        if not re.match(r"^\d+(\.\d+)?$", cleaned):
            return False, 0.0
            
        amount = float(cleaned)
        if not math.isfinite(amount) or amount <= 0:
            return False, 0.0
            
        return True, amount
    except Exception:
        return False, 0.0


def parse_idr_amount(text: str) -> int:
    """
    Parse nominal Rupiah ketikan user (untuk Jual dengan Rupiah).
    Menerima: 5000, 5.000, 5,000, Rp5000, Rp 5.000, 5k, 5rb, 50 ribu, 1.5jt, 2 juta.
    Mengembalikan Rupiah bulat (> 0) atau melempar ValueError.
    """
    raw = (text or "").strip().lower()
    raw = re.sub(r"^(?:rp|idr)\.?\s*", "", raw).strip()
    m = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)\s*(k|rb|ribu|jt|juta)", raw)
    if m:
        number = float(m.group(1).replace(",", "."))
        multiplier = 1_000 if m.group(2) in ("k", "rb", "ribu") else 1_000_000
        value = int(round(number * multiplier))
    elif re.fullmatch(r"[0-9]{1,3}(?:[.,][0-9]{3})+", raw):
        value = int(re.sub(r"[.,]", "", raw))
    elif re.fullmatch(r"[0-9]+", raw):
        value = int(raw)
    else:
        raise ValueError("Format nominal Rupiah tidak dikenali")
    if value <= 0:
        raise ValueError("Nominal Rupiah harus lebih besar dari 0")
    return value


def looks_like_idr(text: str) -> bool:
    """True bila ketikan jelas Rupiah (awalan Rp/IDR atau akhiran k/rb/ribu/jt/juta)."""
    raw = (text or "").strip().lower()
    return bool(re.match(r"^(?:rp|idr)\.?\s*[0-9]", raw)
                or re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?\s*(?:k|rb|ribu|jt|juta)", raw))

