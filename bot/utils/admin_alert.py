"""Detail order standar untuk notifikasi admin.

Semua alert yang menyebut order memakai blok yang sama, supaya admin langsung tahu jenis
transaksi (Beli / Jual / Convert + pasangan koin), siapa user-nya (@username, nama, ID), dan
nominal yang diharapkan tanpa membuka database. Dulu tiap alert menulis field sendiri-sendiri:
ada yang hanya "USDC (BASE)" tanpa koin tujuan dan hanya ID user, sehingga ambigu.
"""
from html import escape as _esc

from bot.utils.formatter import display_symbol, format_crypto_copy, format_datetime, format_idr

_PAYMENT_LABEL = {"GOPAY_QRIS": "QRIS GoPay", "BOT_BALANCE": "Saldo Bot"}


def _load_user(telegram_id, db=None):
    from database.connection import SessionLocal
    from database.models import User
    session = db or SessionLocal()
    try:
        return session.query(User).filter(User.telegram_id == telegram_id).first()
    except Exception:
        return None
    finally:
        if db is None:
            session.close()


def user_label(telegram_id, db=None, user=None) -> str:
    """'@username · Nama · ID 123' (HTML aman). Bagian yang tidak diketahui dilewati."""
    if user is None and telegram_id is not None:
        user = _load_user(telegram_id, db)
    parts = []
    username = (getattr(user, "username", None) or "").strip().lstrip("@")
    if username:
        parts.append(_esc(f"@{username}"))
    full_name = (getattr(user, "full_name", None) or "").strip()
    if full_name:
        parts.append(_esc(full_name))
    parts.append(f"ID <code>{telegram_id}</code>")
    return " · ".join(parts)


def idr_copy(amount) -> str:
    """'Rp 49.503 (salin: 49503)': angka mentah sendirian di <code> supaya sekali tap bisa ditempel ke m-banking."""
    try:
        value = int(amount or 0)
    except (TypeError, ValueError):
        value = 0
    return f"<b>{format_idr(value)}</b> (salin: <code>{value}</code>)"


def split_bank(buyer_wallet) -> tuple:
    """(bank, no_rekening, atas_nama) dari 'Bank | No | Nama'; None bila formatnya tidak lengkap."""
    parts = [p.strip() for p in str(buyer_wallet or "").split("|")]
    if len(parts) >= 3 and parts[1]:
        return parts[0], parts[1], parts[2]
    return None


def bank_lines(buyer_wallet) -> str:
    """Rekening tujuan untuk admin: nomor rekening SAJA di <code>, bank dan nama di luar agar tidak ikut tersalin."""
    bank = split_bank(buyer_wallet)
    if not bank:
        return f"Rekening: <code>{_esc(str(buyer_wallet or '-'))}</code>"
    name, number, holder = bank
    return (f"Bank/E-Wallet: <b>{_esc(name)}</b>\n"
            f"No. Rekening: <code>{_esc(number)}</code>\n"
            f"Atas Nama: <b>{_esc(holder)}</b>")


def order_title(order) -> str:
    """Jenis transaksi + pasangan koin, mis. 'CONVERT USDC (BASE) → BNB (BSC)'."""
    kind = (order.order_type or "").lower()
    source = f"{display_symbol(order.crypto_symbol)} ({order.network})"
    if kind == "swap":
        target = f"{display_symbol(order.target_crypto_symbol or '-')} ({order.target_network or '-'})"
        return f"🔄 CONVERT {source} → {target}"
    if kind == "sell":
        return f"💵 JUAL {source} → Rupiah"
    if kind == "buy":
        payment = _PAYMENT_LABEL.get(order.payment_method or "", order.payment_method or "-")
        return f"🛒 BELI {source} · bayar {payment}"
    return f"{kind.upper() or 'ORDER'} {source}"


def order_detail_block(order, db=None) -> str:
    """Blok HTML detail order untuk ditempel di alert admin."""
    kind = (order.order_type or "").lower()
    lines = [
        f"<b>{_esc(order_title(order))}</b>",
        f"Order: <code>{_esc(str(order.order_id))}</code>",
        f"User: {user_label(order.telegram_id, db)}",
    ]
    source = (f"<b>{format_crypto_copy(order.crypto_amount or 0, order.crypto_symbol, exact=True)}</b> "
              f"({_esc(str(order.network))})")
    wallet = f"<code>{_esc(str(order.buyer_wallet or '-'))}</code>"
    if kind == "swap":
        target = format_crypto_copy(order.target_crypto_amount or 0, order.target_crypto_symbol or "-", exact=True)
        lines += [
            f"Deposit user (sesuai order): {source}",
            f"Dikirim ke user: <b>{target}</b> ({_esc(str(order.target_network or '-'))})",
            f"Wallet tujuan: {wallet}",
        ]
    elif kind == "sell":
        lines += [
            f"Deposit user (sesuai order): {source}",
            f"Rupiah ke user: {idr_copy(order.total_idr)}",
            bank_lines(order.buyer_wallet),
        ]
    else:
        payment = _PAYMENT_LABEL.get(order.payment_method or "", order.payment_method or "-")
        lines += [
            f"Koin ke user: {source}",
            f"Dibayar user: <b>{format_idr(int(order.total_idr or 0))}</b> ({_esc(payment)})",
            f"Wallet tujuan: {wallet}",
        ]
    lines.append(f"Status: <code>{_esc(str(order.status or '-'))}</code> · dibuat {format_datetime(order.created_at)}")
    return "\n".join(lines)
