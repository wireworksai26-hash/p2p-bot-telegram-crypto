"""
services/qris_generator.py — Generator QRIS Dinamis & Gambar QR Code EMVCo
===========================================================================
Menyediakan konversi QRIS statis menjadi dinamis dengan nominal tertera,
serta mengonversi payload QRIS menjadi stream gambar PNG BytesIO secara in-memory.
"""

import io
import logging
import os
import qrcode
from config.settings import settings
from config.assets import get_qris_static_image_path

logger = logging.getLogger(__name__)


def crc16_ccitt(payload: str) -> str:
    """Menghitung CRC16 CCITT (0xFFFF, Poly 0x1021) untuk checksum QRIS EMVCo."""
    crc = 0xFFFF
    for char in payload:
        crc ^= ord(char) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def generate_dynamic_qris_string(static_payload: str, amount: int) -> str:
    """Mengubah string QRIS statis menjadi string QRIS dinamis EMVCo dengan nominal terpasang."""
    if not static_payload:
        return ""
    payload = static_payload.strip()
    idx63 = payload.find("6304")
    if idx63 != -1:
        payload = payload[:idx63]

    tags = []
    i = 0
    try:
        while i < len(payload):
            tag = payload[i:i+2]
            length = int(payload[i+2:i+4])
            val = payload[i+4:i+4+length]
            tags.append((tag, val))
            i += 4 + length
    except Exception as e:
        logger.warning(f"Error parsing QRIS payload: {e}")
        return static_payload

    amount_str = str(int(amount))
    new_tags = []
    has_tag54 = False

    for tag, val in tags:
        if tag == "01":
            # 11 = Static, 12 = Dynamic
            new_tags.append(("01", "12"))
        elif tag == "54":
            new_tags.append(("54", amount_str))
            has_tag54 = True
        elif tag == "58" and not has_tag54:
            new_tags.append(("54", amount_str))
            has_tag54 = True
            new_tags.append((tag, val))
        else:
            new_tags.append((tag, val))

    if not has_tag54:
        new_tags.append(("54", amount_str))

    result = "".join(f"{tag}{len(val):02d}{val}" for tag, val in new_tags)
    result += "6304"
    checksum = crc16_ccitt(result)
    return result + checksum


# String QRIS Statis Merchant (Toko digital HSN, Digital - GoPay Merchant)
DEFAULT_QRIS_STATIC = "00020101021126610014COM.GO-JEK.WWW01189360091432922297020210G2922297020303UMI51440014ID.CO.QRIS.WWW0215ID10265038922870303UMI5204899953033605802ID5925Toko digital HSN, Digital6008SIDOARJO61056126162070703A016304A581"


def get_qris_image_stream(amount: int = 0) -> io.BytesIO | None:
    """
    Menghasilkan objek buffer BytesIO gambar QRIS.
    Jika amount > 0:
      Generate Dynamic QR code dengan nominal tersemat otomatis (sehingga user tidak perlu ketik manual di e-wallet/m-banking).
    Jika amount <= 0 atau jika pembuatan dinamis gagal:
      Fallback ke file gambar statis 'Qris statis.jpeg' atau QR Code statis default.
    """
    static_qris = getattr(settings, "QRIS_STATIC", "") or DEFAULT_QRIS_STATIC
    
    # 1. Jika ada nominal pembayaran, prioritaskan Dynamic QRIS
    if amount > 0 and static_qris:
        try:
            qris_payload = generate_dynamic_qris_string(static_qris, amount)
            img = qrcode.make(qris_payload)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf
        except Exception as e:
            logger.warning(f"Gagal generate dynamic QR image: {e}")

    # 2. Gambar statis merchant asli (jika amount <= 0 atau fallback)
    static_file = get_qris_static_image_path()
    if static_file and os.path.exists(static_file):
        try:
            with open(static_file, "rb") as f:
                buf = io.BytesIO(f.read())
                buf.seek(0)
                return buf
        except Exception as e:
            logger.warning(f"Gagal membaca static QR file: {e}")

    # 3. Fallback: generate QR statis dari payload jika file gambar tidak ditemukan
    if static_qris:
        try:
            img = qrcode.make(static_qris)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf
        except Exception as e:
            logger.warning(f"Gagal generate static QR image fallback: {e}")

    return None


def get_wallet_qr_stream(wallet_address: str) -> io.BytesIO | None:
    """
    Menghasilkan QR Code untuk alamat wallet crypto (EVM/Solana/Tron/TON)
    agar customer dapat langsung scan dari aplikasi wallet mereka (Trust Wallet, MetaMask, Binance, dll).
    """
    if not wallet_address:
        return None
    try:
        img = qrcode.make(wallet_address.strip())
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        logger.warning(f"Gagal generate wallet QR image: {e}")
        return None
