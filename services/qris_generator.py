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


def _parse_tlv(payload: str):
    """Parse string EMVCo: pasangan tag 2 digit + panjang 2 digit + nilai."""
    tags, i = [], 0
    while i + 4 <= len(payload):
        tag = payload[i:i + 2]
        ls = payload[i + 2:i + 4]
        if not ls.isdigit():
            raise ValueError(f"Panjang tag {tag} bukan angka pada posisi {i}")
        ln = int(ls)
        val = payload[i + 4:i + 4 + ln]
        if len(val) < ln:
            raise ValueError(f"Nilai tag {tag} terpotong pada posisi {i}")
        tags.append((tag, val))
        i += 4 + ln
    return tags, i


def validasi_qris_payload(payload: str) -> None:
    """Validasi string QRIS EMVCo: TLV harus habis terbaca dan CRC-nya benar.

    Kasus nyata 23 Sep 2026: template QRIS baru copy-paste-nya kehilangan 1
    karakter di 2 field, sehingga TLV bergeser, CRC salah, dan semua e-wallet
    menolak scan dengan 'QR sedang tidak tersedia'.
    """
    if not payload or len(payload) < 12:
        raise ValueError("Payload QRIS kosong/terlalu pendek")
    crc_terlampir = payload[-4:]
    crc_benar = crc16_ccitt(payload[:-4])
    if crc_benar != crc_terlampir:
        raise ValueError(
            f"CRC QRIS tidak cocok (terlampir {crc_terlampir}, seharusnya {crc_benar})")
    tags, _ = _parse_tlv(payload)
    if payload[:4] != "0002" or not any(t == "63" for t, _ in tags):
        raise ValueError("Payload bukan QRIS EMVCo lengkap (tag 00/63 rusak)")


def generate_dynamic_qris_string(static_payload: str, amount: int) -> str:
    """Mengubah string QRIS statis menjadi string QRIS dinamis EMVCo dengan nominal terpasang.

    Melempar ValueError bila template tidak valid, supaya tidak pernah
    menghasilkan QR rusak yang ditolak e-wallet saat di-scan.
    """
    if not static_payload:
        raise ValueError("Template QRIS kosong")
    payload = static_payload.strip()
    validasi_qris_payload(payload)
    if not 0 < int(amount) <= 10_000_000:
        raise ValueError("Nominal QRIS di luar batas Rp 1 - Rp 10.000.000")

    tags, _ = _parse_tlv(payload)
    amount_str = str(int(amount))
    new_tags = []
    has_tag54 = False

    for tag, val in tags:
        if tag == "63":
            continue
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

    result = "".join(f"{tag}{len(val):02d}{val}" for tag, val in new_tags) + "6304"
    result += crc16_ccitt(result)
    validasi_qris_payload(result)
    return result


def get_qris_image_stream(amount: int = 0) -> io.BytesIO | None:
    """
    Menghasilkan objek buffer BytesIO gambar QRIS.
    Jika amount > 0: QRIS dinamis dengan nominal tersemat.
    Jika template rusak/gagal: fallback ke gambar statis merchant
    ('Qris statis.jpeg'). QR tidak valid TIDAK PERNAH diterbitkan.
    """
    static_qris = (getattr(settings, "QRIS_STATIC", "") or "").strip()

    # 1. QRIS dinamis dengan nominal
    if amount > 0 and static_qris:
        try:
            qris_payload = generate_dynamic_qris_string(static_qris, amount)
            img = qrcode.make(qris_payload)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf
        except Exception as e:
            logger.error(f"QRIS dinamis gagal dibuat ({e}); fallback ke gambar statis merchant")

    # 2. Gambar statis merchant asli
    static_file = get_qris_static_image_path()
    if static_file and os.path.exists(static_file):
        try:
            with open(static_file, "rb") as f:
                data = f.read()
            if len(data) > 1024:
                buf = io.BytesIO(data)
                buf.seek(0)
                return buf
            logger.warning(
                f"File QRIS statis terlalu kecil ({len(data)} byte, kemungkinan pointer Git LFS), dilewati")
        except Exception as e:
            logger.warning(f"Gagal membaca static QR file: {e}")

    # 3. Terakhir: QR statis dari payload, hanya bila payload valid
    if static_qris:
        try:
            validasi_qris_payload(static_qris)
            img = qrcode.make(static_qris)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf
        except Exception as e:
            logger.error(f"Payload QRIS tidak valid, QR tidak diterbitkan: {e}")

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
