"""
services/testimony_service.py — Service Testimoni Channel Transaksi Otomatis (Phase 8)
========================================================================================
Mengirimkan postingan log transaksi otomatis ke channel testimoni Telegram (@TokoKoinID)
setiap kali order Beli, Jual, atau Swap berhasil diselesaikan (COMPLETED).
Username pengguna disensor untuk privasi.
"""

import os
import re
import html
import asyncio
import logging
from types import SimpleNamespace
from decimal import Decimal
from typing import Optional, Any

from bot.utils.formatter import format_idr, format_crypto
from config.settings import settings

logger = logging.getLogger(__name__)

# Channel testimoni default jika tidak disetel di environment
DEFAULT_TESTIMONY_CHANNEL = os.getenv("TESTIMONY_CHANNEL", "@TokoKoinID")


def anonymize_username(username: Optional[str], telegram_id: int) -> str:
    """
    Sensor username untuk privasi di channel publik.
    Contoh:
      - 'hendra_hidayat' -> '@he****at'
      - 'budi' -> '@b****i'
      - None / kosong -> '@User_87****77'
    """
    if username:
        u = username.strip().lstrip("@")
        if len(u) <= 2:
            return f"@{u[0]}****"
        elif len(u) <= 4:
            return f"@{u[0]}****{u[-1]}"
        else:
            return f"@{u[:2]}****{u[-2:]}"

    tid_str = str(telegram_id)
    if len(tid_str) >= 4:
        return f"@User_{tid_str[:2]}****{tid_str[-2:]}"
    return f"@User_{tid_str[0]}****"


def get_explorer_url_for_tx(network: Optional[str], tx_hash: Optional[str]) -> str:
    """Mendapatkan link explorer blockchain untuk sebuah tx_hash."""
    if not tx_hash or not isinstance(tx_hash, str) or not tx_hash.strip():
        return ""

    clean_hash = tx_hash.strip().removeprefix("msg:")
    net = (network or "").upper()

    # Coba ambil base explorer dari CryptoSenderFactory
    try:
        from services.crypto_sender import CryptoSenderFactory
        sender = CryptoSenderFactory.get_sender(net)
        base = (getattr(sender, "config", {}) or {}).get("explorer")
        if base:
            return f"{base}/tx/{clean_hash}"
    except Exception:
        pass

    # Fallback mapping manual
    explorers = {
        "BSC": f"https://bscscan.com/tx/{clean_hash}",
        "ETH": f"https://etherscan.io/tx/{clean_hash}",
        "POLYGON": f"https://polygonscan.com/tx/{clean_hash}",
        "ARBITRUM": f"https://arbiscan.io/tx/{clean_hash}",
        "BASE": f"https://basescan.org/tx/{clean_hash}",
        "SOLANA": f"https://solscan.io/tx/{clean_hash}",
        "TRC20": f"https://tronscan.org/#/transaction/{clean_hash}",
        "TRON": f"https://tronscan.org/#/transaction/{clean_hash}",
        "SUI": f"https://suiscan.xyz/mainnet/tx/{clean_hash}",
        "TON": f"https://tonviewer.com/transaction/{clean_hash}",
        "BTC": f"https://mempool.space/tx/{clean_hash}",
        "BITCOIN": f"https://mempool.space/tx/{clean_hash}",
    }
    return explorers.get(net, "")


def format_testimony_message(
    order_type: str,
    crypto_symbol: str,
    network: str,
    nominal_idr: int,
    username: Optional[str],
    telegram_id: int,
    tx_hash: Optional[str] = None,
    bot_username: str = "TokoKoinID_Bot",
    target_symbol: Optional[str] = None,
    target_network: Optional[str] = None,
) -> str:
    """
    Menyusun teks postingan testimoni transaksi sukses.
    Format:
    Transaksi Selesai
    - Jenis Transaksi : Beli
    - Jenis Coin : Usdt Bsc
    - Pengguna : @h****hy
    - Nominal : Rp520.000
    - Transaction Hash : Link
    - Bot order : @TokoKoinID_Bot
    """
    ot_upper = (order_type or "").upper()
    if ot_upper == "BUY" or ot_upper == "BELI":
        tx_type_label = "Beli"
        coin_label = f"{crypto_symbol.upper()} {network.upper()}".strip()
    elif ot_upper == "SELL" or ot_upper == "JUAL":
        tx_type_label = "Jual"
        coin_label = f"{crypto_symbol.upper()} {network.upper()}".strip()
    elif ot_upper == "SWAP" or ot_upper == "CONVERT":
        tx_type_label = "Swap"
        t_sym = (target_symbol or "").upper()
        t_net = (target_network or "").upper()
        coin_label = f"{crypto_symbol.upper()} -> {t_sym} ({t_net})".strip()
    else:
        tx_type_label = order_type.capitalize()
        coin_label = f"{crypto_symbol.upper()} {network.upper()}".strip()

    user_label = anonymize_username(username, telegram_id)
    nominal_formatted = format_idr(nominal_idr)

    # Link Txhash
    explorer_url = get_explorer_url_for_tx(network or target_network, tx_hash)
    if explorer_url:
        tx_display = f'<a href="{explorer_url}">Link</a>'
    elif tx_hash and len(tx_hash) > 10:
        tx_display = f"<code>{html.escape(tx_hash[:6])}...{html.escape(tx_hash[-4:])}</code>"
    else:
        tx_display = "-"

    bot_tag = f"@{bot_username.lstrip('@')}" if bot_username else "@TokoKoinID_Bot"

    msg = (
        f"✅ <b>Transaksi Selesai</b>\n"
        f"- Jenis Transaksi : {tx_type_label}\n"
        f"- Jenis Coin : {coin_label}\n"
        f"- Pengguna : {user_label}\n"
        f"- Nominal : {nominal_formatted}\n"
        f"- Transaction Hash : {tx_display}\n"
        f"- Bot order : {bot_tag}"
    )
    return msg


async def post_transaction_testimony(
    bot,
    order,
    db=None,
    channel: Optional[str] = None,
    bot_username: Optional[str] = None,
) -> bool:
    """
    Kirim log transaksi ke channel testimoni Telegram secara asinkron.
    Tidak akan melempar exception agar alur order utama tidak pernah terganggu.
    """
    if not bot or not order:
        return False

    target_channel = channel or os.getenv("TESTIMONY_CHANNEL") or getattr(settings, "TESTIMONY_CHANNEL_ID", None) or DEFAULT_TESTIMONY_CHANNEL

    try:
        # Cari username jika belum ada di object order
        username = getattr(order, "user_username", None)
        telegram_id = int(getattr(order, "telegram_id", 0) or 0)

        if not username and db and telegram_id:
            from database.models import User
            user_obj = db.query(User).filter(User.telegram_id == telegram_id).first()
            if user_obj and user_obj.username:
                username = user_obj.username

        if not bot_username:
            try:
                me = await bot.get_me()
                bot_username = me.username if me else "TokoKoinID_Bot"
            except Exception:
                bot_username = "TokoKoinID_Bot"

        order_type = getattr(order, "order_type", "buy")
        crypto_symbol = getattr(order, "crypto_symbol", "") or ""
        network = getattr(order, "network", "") or ""
        target_symbol = getattr(order, "target_crypto_symbol", None)
        target_network = getattr(order, "target_network", None)
        nominal_idr = int(getattr(order, "total_idr", 0) or getattr(order, "nominal_idr", 0) or 0)
        tx_hash = getattr(order, "payout_tx_hash", None) or getattr(order, "tx_hash", None) or getattr(order, "deposit_tx_hash", None)

        text = format_testimony_message(
            order_type=order_type,
            crypto_symbol=crypto_symbol,
            network=network,
            nominal_idr=nominal_idr,
            username=username,
            telegram_id=telegram_id,
            tx_hash=tx_hash,
            bot_username=bot_username,
            target_symbol=target_symbol,
            target_network=target_network,
        )

        await bot.send_message(
            chat_id=target_channel,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(f"Testimoni transaksi {order.order_id} berhasil dikirim ke {target_channel}")
        return True

    except Exception as e:
        logger.error(f"Gagal mengirim testimoni transaksi {getattr(order, 'order_id', '?')} ke {target_channel}: {e}", exc_info=True)
        return False


_TESTIMONY_FIELDS = (
    "order_id", "order_type", "crypto_symbol", "network", "target_crypto_symbol",
    "target_network", "total_idr", "nominal_idr", "payout_tx_hash", "tx_hash",
    "deposit_tx_hash", "telegram_id", "user_username",
)
_scheduled_order_ids: set = set()


def schedule_transaction_testimony(bot, order, db=None) -> bool:
    """
    Jadwalkan posting testimoni di background untuk order yang baru COMPLETED.
    Data order di-snapshot SEKARANG (selagi session DB masih hidup), sehingga task
    tidak bergantung pada session yang mungkin sudah ditutup. Satu order hanya
    diposting sekali per proses.
    """
    try:
        order_id = getattr(order, "order_id", None)
        if not bot or not order_id or order_id in _scheduled_order_ids:
            return False

        snap = SimpleNamespace(**{f: getattr(order, f, None) for f in _TESTIMONY_FIELDS})
        if not snap.user_username and db and snap.telegram_id:
            from database.models import User
            user_obj = db.query(User).filter(User.telegram_id == int(snap.telegram_id)).first()
            if user_obj and user_obj.username:
                snap.user_username = user_obj.username

        _scheduled_order_ids.add(order_id)

        async def _run():
            ok = await post_transaction_testimony(bot, snap)
            if not ok:
                _scheduled_order_ids.discard(order_id)

        asyncio.get_running_loop().create_task(_run())
        return True
    except Exception as e:
        logger.error(f"Gagal menjadwalkan testimoni order {getattr(order, 'order_id', '?')}: {e}", exc_info=True)
        return False
