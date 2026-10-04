"""services/payout_watchdog.py — Rekonsiliasi payout "broadcast, receipt belum ada".

Order yang sempat gagal dengan tx_hash (broadcast sudah dicoba) tidak boleh
nyangkut di manual_review selamanya. Job ini cek receipt on-chain tiap siklus:
- receipt sukses  -> COMPLETED + notifikasi user (pulih otomatis)
- receipt revert  -> alert admin (butuh keputusan manual, sekali saja)
- belum terlihat  -> tunggu; setelah > MAX_AGE_HOURS alert admin sekali
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta

from database.connection import SessionLocal
from database.models import Order
from database import crud
from bot.utils.telegram_utils import notify_admins, safe_send_message
from bot.utils.formatter import format_crypto

logger = logging.getLogger(__name__)

# Status yang masih menunggu penyelesaian receipt (pasca broadcast).
WATCH_STATUSES = ("manual_review", "MANUAL_REVIEW", "payout_processing", "payout_broadcasted")
MAX_AGE_HOURS = 6
TON_API = "https://tonapi.io/v2"

# order_id yang sudah di-alert, cegah spam; proses restart aman (alert ulang maks 1x).
_alerted: set = set()


def _target_network(order) -> str:
    if order.order_type == "swap":
        return (order.target_network or order.network or "").upper()
    return (order.network or "").upper()


def _target_symbol(order) -> str:
    if order.order_type == "swap":
        return (order.target_crypto_symbol or order.crypto_symbol or "").upper()
    return (order.crypto_symbol or "").upper()


def _explorer_url(network: str, tx_hash: str) -> str:
    if not tx_hash:
        return ""
    if network == "TON":
        return f"https://tonviewer.com/transaction/{tx_hash.removeprefix('msg:')}"
    try:
        from services.crypto_sender import CryptoSenderFactory
        sender = CryptoSenderFactory.get_sender(network)
        base = (getattr(sender, "config", {}) or {}).get("explorer")
        if base:
            return f"{base}/tx/{tx_hash}"
    except Exception:
        pass
    return ""


async def _receipt_state(network: str, tx_hash: str) -> str:
    """'success' | 'reverted' | 'pending' | 'unknown' (evm + ton; lain => unknown)."""
    tx_hash = (tx_hash or "").strip()
    if not tx_hash:
        return "unknown"

    if network == "TON":
        return await _ton_state(tx_hash)

    try:
        from services.crypto_sender import CryptoSenderFactory
        sender = CryptoSenderFactory.get_sender(network)
    except Exception:
        return "unknown"
    if not hasattr(sender, "_create_w3_instance") or not getattr(sender, "rpc_list", None):
        return "unknown"

    try:
        from web3.exceptions import TransactionNotFound
    except Exception:  # pragma: no cover
        TransactionNotFound = Exception

    for rpc in list(sender.rpc_list)[:3]:
        try:
            w3 = sender._create_w3_instance(rpc)
            receipt = await asyncio.to_thread(w3.eth.get_transaction_receipt, tx_hash)
            return "success" if receipt.get("status") == 1 else "reverted"
        except TransactionNotFound:
            return "pending"
        except Exception as exc:
            logger.debug("Receipt %s via %s gagal: %s", tx_hash, rpc, exc)
            continue
    return "pending"


async def _ton_state(tx_ref: str) -> str:
    """Resolve hash/reference TON: pesan -> transaksi, lalu cek status."""
    import httpx

    ref = tx_ref.removeprefix("msg:")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", ref):
        return "unknown"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{TON_API}/blockchain/messages/{ref}/transaction")
            if resp.status_code == 404:
                # Bukan message hash — mungkin sudah transaction hash.
                resp = await client.get(f"{TON_API}/blockchain/transactions/{ref}")
            if resp.status_code == 404:
                return "pending"
            resp.raise_for_status()
            data = resp.json()
            if data.get("success") is False:
                return "reverted"
            return "success"
    except Exception as exc:
        logger.debug("Cek TON %s gagal: %s", ref, exc)
        return "pending"


async def _notify_user(bot, order, url: str) -> None:
    if order.order_type == "swap":
        text = (
            f"🎉 <b>CONVERT BERHASIL!</b>\n\n"
            f"ID Order: <code>{order.order_id}</code>\n"
            f"Terima: {format_crypto(float(order.target_crypto_amount or 0), order.target_crypto_symbol or '')} "
            f"({order.target_network})\n"
            f"TX Hash: <code>{order.payout_tx_hash}</code>"
        )
    else:
        text = (
            f"✅ <b>Crypto Terkirim!</b>\n\n"
            f"<b>Order:</b> <code>{order.order_id}</code>\n"
            f"🪙 <b>Jumlah:</b> <code>{format_crypto(float(order.crypto_amount or 0), order.crypto_symbol or '')} "
            f"({order.network})</code>\n"
            f"🔗 <b>TX:</b> <code>{order.payout_tx_hash}</code>"
        )
    if url:
        text += f"\n\n🌐 <a href=\"{url}\">Lihat di Explorer</a>"
    text += (
        "\n\nTerimakasih sudah bertransaksi di sini, Lancar selalu 🙏🙏\n"
        "Testimoni : t.me/TokoKoinID\n"
        "Channel : t.me/ROBHSN_STORE_SELLER"
    )
    await safe_send_message(bot, order.telegram_id, text)


async def reconcile_broadcasted_payouts(bot=None) -> int:
    """Selesaikan order ber-hash yang belum terkonfirmasi. Return jumlah COMPLETED."""
    completed = 0
    db = SessionLocal()
    try:
        rows = (
            db.query(Order)
            .filter(
                Order.payout_tx_hash.isnot(None),
                Order.status.in_(WATCH_STATUSES),
            )
            .all()
        )
        now = datetime.utcnow()
        for order in rows:
            network = _target_network(order)
            tx_hash = (order.payout_tx_hash or "").strip()
            state = await _receipt_state(network, tx_hash)
            url = _explorer_url(network, tx_hash)

            if state == "success":
                crud.update_order_status(
                    db,
                    order.order_id,
                    new_status="completed",
                    tx_hash=tx_hash,
                    completed_at=now,
                )
                try:
                    crud.release_order_inventory(db, order.order_id)
                except Exception as exc:
                    logger.warning("Gagal release inventory %s: %s", order.order_id, exc)
                _alerted.discard(order.order_id)
                completed += 1
                logger.info("Payout watchdog: %s COMPLETED (tx %s)", order.order_id, tx_hash)
                if bot:
                    try:
                        await _notify_user(bot, order, url)
                    except Exception as exc:
                        logger.warning("Gagal notif user %s: %s", order.telegram_id, exc)
                    try:
                        await notify_admins(
                            bot,
                            (
                                f"✅ <b>PAYOUT DIPULIHKAN OTOMATIS</b>\n\n"
                                f"Order: <code>{order.order_id}</code>\n"
                                f"TX: <code>{tx_hash}</code>\n"
                                f"Receipt on-chain sukses; status kini COMPLETED."
                            ),
                            kind="ops",
                        )
                    except Exception as exc:
                        logger.warning("Gagal notif admin %s: %s", order.order_id, exc)

                    # Referral reward trigger
                    try:
                        trade_amt = float(getattr(order, "nominal_idr", 0) or getattr(order, "total_idr", 0) or 0)
                        ref_result = crud.complete_referral(db, order.telegram_id, trade_amount_idr=trade_amt)
                        if ref_result:
                            ref = crud.get_referral_by_referee(db, order.telegram_id)
                            if ref:
                                reward = ref.reward_idr or 0
                                from bot.utils.formatter import format_idr as _fmt_idr
                                # Aktifkan referral discount untuk referrer (Phase 7)
                                from services.referral_discount_service import activate_discount_for_referrer
                                await activate_discount_for_referrer(bot, ref.referrer_id, db)
                                await bot.send_message(
                                    ref.referrer_id,
                                    f"🎉 <b>Referral Reward!</b>\n\n"
                                    f"User yang Anda ajak telah menyelesaikan transaksi.\n"
                                    f"Saldo Anda bertambah <b>{_fmt_idr(reward)}</b>!",
                                    parse_mode="HTML",
                                )
                    except Exception as exc:
                        logger.warning("Gagal proses referral reward user %s: %s", order.telegram_id, exc)

                    # Loyalty time-window tracking (Phase 7)
                    try:
                        from services.loyalty_service import process_loyalty_after_order
                        order_total = int(getattr(order, "total_idr", 0) or 0)
                        await process_loyalty_after_order(
                            telegram_id=order.telegram_id,
                            order_amount_idr=order_total,
                            bot=bot,
                            db=db,
                        )
                    except Exception as exc:
                        logger.warning("Gagal proses loyalty untuk user %s: %s", order.telegram_id, exc)

                    # Post testimony ke channel (Phase 8)
                    try:
                        from services.testimony_service import post_transaction_testimony
                        asyncio.create_task(post_transaction_testimony(bot, order, db=db))
                    except Exception as exc:
                        logger.warning("Gagal kirim testimoni watchdog order %s: %s", order.order_id, exc)


            elif state == "reverted":
                if order.order_id not in _alerted:
                    _alerted.add(order.order_id)
                    logger.warning("Payout watchdog: %s REVERTED (tx %s)", order.order_id, tx_hash)
                    if bot:
                        try:
                            await notify_admins(
                                bot,
                                (
                                    f"🚨 <b>PAYOUT REVERT ON-CHAIN</b>\n\n"
                                    f"Order: <code>{order.order_id}</code>\n"
                                    f"TX: <code>{tx_hash}</code>{' — ' + url if url else ''}\n"
                                    f"Kirim ulang secara manual setelah verifikasi."
                                ),
                                kind="error",
                                butuh_tindakan=True,
                            )
                        except Exception as exc:
                            logger.warning("Gagal notif admin revert %s: %s", order.order_id, exc)

            else:  # pending / unknown
                age = now - (order.updated_at or order.created_at or now)
                if age >= timedelta(hours=MAX_AGE_HOURS) and order.order_id not in _alerted:
                    _alerted.add(order.order_id)
                    logger.warning(
                        "Payout watchdog: %s belum terkonfirmasi >%sh (tx %s)",
                        order.order_id, MAX_AGE_HOURS, tx_hash,
                    )
                    if bot:
                        try:
                            await notify_admins(
                                bot,
                                (
                                    f"⚠️ <b>PAYOUT BELUM TERKONFIRMASI > {MAX_AGE_HOURS} JAM</b>\n\n"
                                    f"Order: <code>{order.order_id}</code>\n"
                                    f"TX: <code>{tx_hash}</code>{' — ' + url if url else ''}\n"
                                    f"Cek manual sebelum mengambil tindakan."
                                ),
                                kind="error",
                                butuh_tindakan=True,
                            )
                        except Exception as exc:
                            logger.warning("Gagal notif admin pending %s: %s", order.order_id, exc)
    finally:
        db.close()
    return completed
