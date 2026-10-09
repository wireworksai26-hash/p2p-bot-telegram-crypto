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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Any

from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

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
        s_net = (network or "").upper()
        src_label = f"{crypto_symbol.upper()} ({s_net})" if s_net else crypto_symbol.upper()
        dst_label = f"{t_sym} ({t_net})" if t_net else t_sym
        coin_label = f"{src_label} -> {dst_label}".strip()
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


def _resolve_bot(sender):
    """
    Kembalikan objek yang punya send_message. Modul detector/watchdog/buy meneruskan
    telegram Application (services.bot_runtime.bot_app), yang tidak punya send_message
    sendiri — objek Bot-nya ada di `.bot`. Tanpa ini testimoni gagal diam-diam.
    """
    if sender is None:
        return None
    if callable(getattr(sender, "send_message", None)):
        return sender
    inner = getattr(sender, "bot", None)
    if inner is not None and callable(getattr(inner, "send_message", None)):
        return inner
    return None


def _mark_posted(order_id: Optional[str]) -> None:
    """Tandai di DB bahwa testimoni order sudah terkirim (dipakai sweeper agar tidak dobel)."""
    if not order_id:
        return
    try:
        from database.connection import SessionLocal
        from database.models import Order
        db = SessionLocal()
        try:
            db.query(Order).filter(Order.order_id == order_id).update(
                {"testimony_posted_at": datetime.utcnow(), "updated_at": Order.updated_at},
                synchronize_session=False,
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Gagal menandai testimoni terkirim untuk order {order_id}: {e}")


_bot_username_cache: Optional[str] = None
_SEND_ATTEMPTS = 3


async def _send_with_retry(bot, **kwargs):
    """
    Kirim ke channel dengan retry untuk gangguan sementara: flood limit (tunggu sesuai
    permintaan Telegram) dan timeout/jaringan (jeda 1-3 detik). Error permanen (bot bukan admin
    channel, channel tidak ada) langsung dilempar agar sweeper/admin tahu penyebabnya.
    """
    for attempt in range(1, _SEND_ATTEMPTS + 1):
        try:
            return await bot.send_message(**kwargs)
        except RetryAfter as exc:
            if attempt == _SEND_ATTEMPTS:
                raise
            wait = min(float(getattr(exc, "retry_after", 1) or 1) + 1, 30)
            logger.warning("Testimoni kena flood limit, menunggu %.0f detik (percobaan %d)", wait, attempt)
            await asyncio.sleep(wait)
        except (BadRequest, TimedOut):
            # BadRequest turunan NetworkError di PTB tetapi permanen (channel salah / bot bukan admin).
            # TimedOut: pesan bisa saja sudah terkirim, kirim ulang berisiko dobel di channel publik.
            raise
        except NetworkError as exc:
            if attempt == _SEND_ATTEMPTS:
                raise
            logger.warning("Kirim testimoni gagal sementara (%s), coba lagi (percobaan %d)", type(exc).__name__, attempt)
            await asyncio.sleep(1 if attempt == 1 else 3)


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
    bot = _resolve_bot(bot)
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

        global _bot_username_cache
        if not bot_username:
            if _bot_username_cache is None:
                try:
                    me = await bot.get_me()
                    if me and me.username:
                        _bot_username_cache = me.username  # cache hanya bila berhasil
                except Exception:
                    pass
            bot_username = _bot_username_cache or "TokoKoinID_Bot"

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

        await _send_with_retry(
            bot,
            chat_id=target_channel,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(f"Testimoni transaksi {order.order_id} berhasil dikirim ke {target_channel}")
        _mark_posted(getattr(order, "order_id", None))
        return True

    except Exception as e:
        _last_error[getattr(order, "order_id", "?")] = f"{type(e).__name__}: {e}"
        logger.error(f"Gagal mengirim testimoni transaksi {getattr(order, 'order_id', '?')} ke {target_channel}: {e}")
        return False


_TESTIMONY_FIELDS = (
    "order_id", "order_type", "crypto_symbol", "network", "target_crypto_symbol",
    "target_network", "total_idr", "nominal_idr", "payout_tx_hash", "tx_hash",
    "deposit_tx_hash", "telegram_id", "user_username",
)
_scheduled_order_ids: set = set()

# Sweeper: jaring pengaman untuk order COMPLETED yang testimoninya belum terkirim
# (gagal kirim, bot restart, jalur completion yang lupa memicu testimoni).
_SWEEP_LOOKBACK_DAYS = 3
_SWEEP_BATCH = 10
_SWEEP_MAX_ATTEMPTS = 20          # sweeper jalan tiap 10 detik, jadi ~3 menit usaha ulang per order
_sweep_attempts: dict = {}
_last_error: dict = {}            # order_id -> error terakhir (untuk alarm admin)
_alerted_orders: set = set()


def _snapshot_order(order, db=None) -> SimpleNamespace:
    """Salin field yang dibutuhkan testimoni (+ username dari tabel User bila belum ada)."""
    snap = SimpleNamespace(**{f: getattr(order, f, None) for f in _TESTIMONY_FIELDS})
    if not snap.user_username and db and snap.telegram_id:
        from database.models import User
        user_obj = db.query(User).filter(User.telegram_id == int(snap.telegram_id)).first()
        if user_obj and user_obj.username:
            snap.user_username = user_obj.username
    return snap


async def _alert_admins_testimony_stuck(bot, order_id: str) -> None:
    """Satu kali per order: kabari admin bahwa testimoni gagal terus, lengkap dengan penyebabnya."""
    if order_id in _alerted_orders:
        return
    _alerted_orders.add(order_id)
    try:
        from bot.utils.telegram_utils import notify_admins
        error = html.escape(_last_error.get(order_id, "tidak diketahui"))
        await notify_admins(
            bot,
            f"⚠️ <b>TESTIMONI GAGAL TERKIRIM</b>\n\n"
            f"Order: <code>{html.escape(order_id)}</code>\n"
            f"Sudah dicoba {_SWEEP_MAX_ATTEMPTS}x. Penyebab terakhir:\n<code>{error}</code>\n\n"
            f"Cek bahwa bot adalah <b>admin channel testimoni</b> dengan izin posting, lalu kirim manual: "
            f"<code>/posttesti {html.escape(order_id)}</code>",
            kind="error", butuh_tindakan=True,
        )
    except Exception as exc:
        logger.warning("Gagal mengirim alarm testimoni %s: %s", order_id, exc)


async def sweep_unposted_testimonies(bot) -> int:
    """
    Posting testimoni untuk order COMPLETED yang belum pernah terkirim ke channel
    (testimony_posted_at kosong). Dipanggil periodik oleh scheduler. Return jumlah terkirim.
    """
    if not _resolve_bot(bot):
        return 0

    from sqlalchemy import func
    from database.connection import SessionLocal
    from database.models import Order

    cutoff = datetime.utcnow() - timedelta(days=_SWEEP_LOOKBACK_DAYS)
    db = SessionLocal()
    try:
        rows = (
            db.query(Order)
            .filter(
                func.lower(Order.status) == "completed",
                Order.testimony_posted_at.is_(None),
                func.coalesce(Order.completed_at, Order.updated_at) >= cutoff,
            )
            .order_by(Order.id.asc())
            .limit(_SWEEP_BATCH * 3)
            .all()
        )
        pending = [
            _snapshot_order(o, db) for o in rows
            if o.order_id not in _scheduled_order_ids
            and _sweep_attempts.get(o.order_id, 0) < _SWEEP_MAX_ATTEMPTS
        ][:_SWEEP_BATCH]
    finally:
        db.close()

    sent = 0
    for snap in pending:
        if snap.order_id in _scheduled_order_ids:
            continue
        _scheduled_order_ids.add(snap.order_id)
        if await post_transaction_testimony(bot, snap):
            sent += 1
            _sweep_attempts.pop(snap.order_id, None)
        else:
            _scheduled_order_ids.discard(snap.order_id)
            _sweep_attempts[snap.order_id] = _sweep_attempts.get(snap.order_id, 0) + 1
            if _sweep_attempts[snap.order_id] >= _SWEEP_MAX_ATTEMPTS:
                await _alert_admins_testimony_stuck(bot, snap.order_id)
        await asyncio.sleep(0.4)
    if sent:
        logger.info(f"Sweeper testimoni: {sent} testimoni tertunda berhasil dikirim.")
    return sent


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

        snap = _snapshot_order(order, db)

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
