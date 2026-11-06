"""Pemenuhan otomatis pesanan Stars/Premium lewat Fragment.

Aturan keselamatan uang (sama semangatnya dengan payout koin):
  * Nonaktif bawaan (STARS_AUTOFULFILL=false) dan hanya jalan bila sesi Fragment + wallet khusus lengkap.
  * Satu pekerja per pesanan (klaim atomik paid -> processing); tidak pernah mencoba ulang pembayaran TON.
  * Kegagalan PASTI sebelum uang keluar  -> kembali ke 'paid' (admin proses manual) + alarm.
  * Hasil TIDAK PASTI setelah broadcast   -> 'review' (admin cek on-chain; jangan kirim ulang otomatis).
  * Username tidak ditemukan Fragment     -> pesanan ditolak dan dana kembali ke Saldo Bot user.
  * Batas per pesanan, batas harian, dan margin: biaya TON harus <= harga jual x (1 - margin minimum).
Semua dependensi (klien Fragment, wallet, harga TON) bisa diganti lewat parameter untuk pengujian.
"""
import logging
from decimal import Decimal
from html import escape as _esc
from typing import Optional

from config.settings import settings
from database import stars_crud
from database.connection import SessionLocal
from bot.utils.emojis import E_CROSS, E_TAG, E_VERIFIED
from services.stars_catalog import package_label

logger = logging.getLogger(__name__)

NANO = Decimal(10**9)
STALE_PROCESSING_MINUTES = 10


def _idr(value) -> str:
    """Rp 1.234.567 (pemisah titik)."""
    return "Rp " + f"{int(round(float(value))):,}".replace(",", ".")


def autofulfill_enabled() -> bool:
    return bool(settings.STARS_AUTOFULFILL)


def missing_config() -> list:
    """Daftar pengaturan yang belum lengkap (kosong = siap)."""
    missing = []
    if not settings.FRAGMENT_COOKIE:
        missing.append("FRAGMENT_COOKIE")
    if not settings.STARS_TON_PRIVATE_KEY:
        missing.append("STARS_TON_PRIVATE_KEY")
    if not settings.STARS_TON_WALLET_ADDRESS:
        missing.append("STARS_TON_WALLET_ADDRESS")
    return missing


def build_client():
    from services.fragment_client import FragmentClient
    return FragmentClient(settings.FRAGMENT_HASH, settings.FRAGMENT_COOKIE)


def build_wallet():
    from services.crypto_sender.fragment_wallet import FragmentWalletSender
    return FragmentWalletSender()


async def ton_price_idr() -> Optional[float]:
    """Harga TON (IDR) untuk menghitung biaya; None bila tidak tersedia (pesanan lalu diproses manual)."""
    from services.price_service import price_service
    data = await price_service.get_price("TON")
    return float(data["buy_price_idr"]) if data else None


async def _notify_admin(bot, text: str, order_id: str, kind: str = "alarm") -> None:
    from bot.utils.telegram_utils import notify_admins
    try:
        await notify_admins(bot, text, kind=kind, butuh_tindakan=True)
    except Exception:
        logger.exception("Gagal mengabari admin soal pesanan %s", order_id)


async def _tell_user(bot, telegram_id: int, text: str) -> None:
    from bot.utils.telegram_utils import safe_send_message
    await safe_send_message(bot, telegram_id, text)


def _brief(order) -> str:
    return (f"<code>{_esc(order.order_id)}</code> · {package_label(order.product.lower(), order.package)} → "
            f"<code>@{_esc(order.recipient_username)}</code> · {_idr(order.price_idr)}")


async def try_auto_fulfill(bot, order_id: str, client=None, wallet=None, price_fn=None) -> str:
    """Coba memenuhi satu pesanan 'paid'. Hasil: disabled / skipped / manual / completed / review / refunded."""
    if not autofulfill_enabled():
        return "disabled"
    gaps = missing_config()
    if gaps:
        logger.warning("Auto-fulfill Stars aktif tetapi belum lengkap: %s", ", ".join(gaps))
        return "skipped"

    db = SessionLocal()
    try:
        order = stars_crud.get_order(db, order_id)
        if order is None or order.status != "paid":
            return "skipped"

        # Batas pengeluaran otomatis: di atas batas, admin yang memproses.
        reason = None
        if int(order.price_idr) > settings.STARS_MAX_ORDER_IDR:
            reason = f"nilai pesanan di atas batas otomatis {_idr(settings.STARS_MAX_ORDER_IDR)}"
        elif stars_crud.auto_spent_today_idr(db) + int(order.price_idr) > settings.STARS_DAILY_LIMIT_IDR:
            reason = f"batas harian otomatis {_idr(settings.STARS_DAILY_LIMIT_IDR)} terlampaui"
        if reason:
            await _notify_admin(bot, f"ℹ️ Pesanan {_brief(order)} diproses MANUAL: {reason}.", order_id, kind="stars")
            claimed = stars_crud.claim_fulfillment(db, order_id)  # tandai sudah dipertimbangkan agar sweeper tidak mengulang
            if claimed:
                stars_crud.release_to_manual(db, order_id, reason)
            return "manual"

        claimed = stars_crud.claim_fulfillment(db, order_id)
        if claimed is None:
            return "skipped"
        client = client or build_client()
        wallet = wallet or build_wallet()
        price_fn = price_fn or ton_price_idr
        return await _run(bot, db, claimed, client, wallet, price_fn)
    except Exception:
        logger.exception("Auto-fulfill pesanan %s gagal tak terduga", order_id)
        try:
            db.rollback()
            current = stars_crud.get_order(db, order_id)
            if current is not None and current.status == "processing":
                # Tak diketahui sudah sampai mana: uang mungkin sudah keluar -> admin cek, jangan diulang.
                stars_crud.mark_review(db, order_id, "", "Kesalahan tak terduga saat pemenuhan otomatis")
                await _notify_admin(bot, f"🚨 Pesanan {_brief(current)} butuh DICEK: kesalahan tak terduga saat otomatis. "
                                         "Cek wallet Fragment/tonviewer sebelum kirim ulang atau refund.", order_id)
        except Exception:
            logger.exception("Gagal menandai review pesanan %s", order_id)
        return "review"
    finally:
        db.close()


async def _run(bot, db, order, client, wallet, price_fn) -> str:
    from services.fragment_client import (
        FragmentAuthError, FragmentError, FragmentInvalidRecipient,
    )

    order_id = order.order_id
    brief = _brief(order)

    async def to_manual(reason: str, alarm: bool = True) -> str:
        stars_crud.release_to_manual(db, order_id, reason)
        if alarm:
            await _notify_admin(bot, f"⚠️ Pesanan {brief} dikembalikan ke PROSES MANUAL: {_esc(reason)}.", order_id)
        return "manual"

    # 1) Siapkan transaksi di Fragment (belum ada uang keluar).
    try:
        wallet_address = wallet.wallet_address
        try:
            public_key = wallet.public_key_hex()
        except Exception:
            public_key = None
        if order.product.upper() == "STARS":
            prepared = await client.prepare_stars(order.recipient_username, int(order.package), wallet_address, public_key)
        else:
            prepared = await client.prepare_premium(order.recipient_username, int(order.package), wallet_address, public_key)
    except FragmentInvalidRecipient:
        refunded = stars_crud.reject_and_refund(db, order_id, 0, allowed=("processing",))
        if refunded:
            await _tell_user(
                bot, refunded.telegram_id,
                f"{E_CROSS()} <b>PESANAN DIBATALKAN</b>\n\n{E_TAG()} <code>{_esc(order_id)}</code>\n"
                f"Username <b>@{_esc(order.recipient_username)}</b> tidak ditemukan. Dana "
                f"<b>{_idr(refunded.total_idr)}</b> dikembalikan ke Saldo Bot kamu. "
                "Pesan lagi dengan username yang benar.")
            await _notify_admin(bot, f"↩️ Pesanan {brief} otomatis di-refund: username tidak ditemukan di Fragment.",
                                order_id, kind="ops")
            return "refunded"
        return "skipped"
    except FragmentAuthError as exc:
        return await to_manual(f"sesi Fragment kedaluwarsa, perbarui FRAGMENT_COOKIE ({exc})")
    except FragmentError as exc:
        return await to_manual(f"Fragment gagal menyiapkan transaksi ({exc})")

    # 2) Pengaman biaya: jangan bayar bila rugi.
    total_ton = Decimal(prepared.total_nano) / NANO
    ton_idr = await price_fn()
    if not ton_idr:
        return await to_manual("harga TON tidak tersedia untuk cek margin")
    cost_idr = float(total_ton) * ton_idr
    allowed = int(order.price_idr) * (1 - settings.STARS_MIN_MARGIN_PCT / 100.0)
    if cost_idr > allowed:
        return await to_manual(
            f"biaya Fragment {total_ton:.4f} TON ≈ {_idr(cost_idr)} melebihi batas margin (maks {_idr(allowed)})")

    # 3) Bayar dari wallet khusus (sekali, tanpa ulang).
    spent = 0
    last = None
    for message in prepared.messages:
        result = await wallet.send_payload(message["address"], message["amount_nano"], message["payload_b64"])
        last = result
        if not result.success:
            break
        spent += int(message["amount_nano"])

    if last is not None and last.success and spent == prepared.total_nano:
        done = stars_crud.complete_auto(db, order_id, last.tx_hash, spent)
        if done:
            await _tell_user(
                bot, done.telegram_id,
                f"{E_VERIFIED()} <b>PESANAN SELESAI</b>\n\n{E_TAG()} <code>{_esc(order_id)}</code>\n"
                f"<b>{package_label(done.product.lower(), done.package)}</b> sudah dikirim ke "
                f"<b>@{_esc(done.recipient_username)}</b>.\nTerima kasih! 🙏")
            await _notify_admin(
                bot, f"✅ Pesanan {brief} SELESAI otomatis. Biaya {total_ton:.4f} TON ≈ {_idr(cost_idr)} · "
                     f"tx <code>{_esc(last.tx_hash)}</code>", order_id, kind="stars")
            try:
                from services.testimony_service import post_stars_testimony
                await post_stars_testimony(bot, done, db)
            except Exception:
                logger.exception("Gagal memposting testimoni Stars/Premium %s", order_id)
        return "completed"

    error = (last.error_message if last else "") or "pembayaran tidak terkonfirmasi"
    if last is not None and (last.tx_hash or spent):
        # Pesan mungkin sudah keluar: jangan ulangi, serahkan ke admin.
        stars_crud.mark_review(db, order_id, last.tx_hash or "", error, spent or None)
        await _notify_admin(
            bot, f"🚨 Pesanan {brief} butuh DICEK: pembayaran TON mungkin sudah keluar tetapi belum terkonfirmasi "
                 f"(ref <code>{_esc(last.tx_hash or '-')}</code>). Cek wallet Fragment/tonviewer dan status Stars di akun "
                 "tujuan, lalu tekan Sudah Dikirim atau Tolak. JANGAN kirim ulang sebelum yakin.", order_id)
        return "review"
    return await to_manual(f"pembayaran TON tidak terkirim ({error[:120]})")


async def sweep(bot) -> dict:
    """Dijalankan berkala: coba pesanan paid yang belum pernah dicoba; pesanan 'processing' yang macet -> review."""
    stats = {"tried": 0, "stale": 0}
    if not autofulfill_enabled():
        return stats
    db = SessionLocal()
    try:
        for order in stars_crud.get_stale_processing(db, STALE_PROCESSING_MINUTES):
            if stars_crud.mark_review(db, order.order_id, "", "Proses otomatis terhenti di tengah jalan"):
                stats["stale"] += 1
                await _notify_admin(
                    bot, f"🚨 Pesanan {_brief(order)} macet di status processing >{STALE_PROCESSING_MINUTES} menit "
                         "(bot restart?). Uang mungkin sudah keluar: cek wallet Fragment/tonviewer sebelum kirim ulang.",
                    order.order_id)
        todo = [o.order_id for o in stars_crud.get_unattempted_paid(db)]
    finally:
        db.close()
    for order_id in todo:
        await try_auto_fulfill(bot, order_id)
        stats["tried"] += 1
    return stats
