"""
services/sender_wallet.py — Validasi alamat wallet PENGIRIM koin (Jual & Convert).
==================================================================================
User mendaftarkan alamat yang akan dipakai mengirim koin ke hot wallet. Setoran hanya sah bila
blockchain menunjukkan pengirimnya alamat itu (lihat expected_sender di services/tx_verifier).
Ini mencegah hash setoran orang lain ditempel ke order sendiri.
"""
from config.settings import settings

MAX_LEN = 250


def check_sender_address(network: str, address: str) -> str:
    """Return pesan error (Bahasa Indonesia) bila alamat tidak boleh dipakai; '' bila valid."""
    address = (address or "").strip()
    if not address or len(address) > MAX_LEN:
        return f"Alamat wallet pengirim tidak valid untuk jaringan {network}."

    # Validator statis dulu: format alamat tidak boleh bergantung pada koneksi RPC sender.
    # Sender hanya cadangan untuk jaringan yang tidak ditangani validator statis (TON, SUI, ...).
    try:
        from bot.utils.validator import validate_wallet_address
        valid = bool(validate_wallet_address(address, network))
    except Exception:
        valid = False
    hot_wallet = ""
    try:
        from services.crypto_sender import CryptoSenderFactory
        sender = CryptoSenderFactory.get_sender(network)
        hot_wallet = (getattr(sender, "wallet_address", "") or "").strip()
        if not valid:
            valid = bool(sender.validate_address(address))
    except Exception:
        pass
    if not valid:
        return f"Format alamat wallet {network} tidak valid. Periksa lagi lalu kirim ulang."

    if not hot_wallet:
        from config.assets import get_wallet_address
        hot_wallet = (get_wallet_address(network) or "").strip()
    if hot_wallet and address.lower() == hot_wallet.lower():
        return "Alamat itu milik bot sendiri. Masukkan alamat wallet pribadi Anda yang dipakai mengirim koin."
    if address.lower() in settings.OWNER_WALLET_ADDRESSES:
        return "Alamat itu tidak bisa dipakai sebagai wallet pengirim. Masukkan alamat wallet pribadi Anda."
    return ""
