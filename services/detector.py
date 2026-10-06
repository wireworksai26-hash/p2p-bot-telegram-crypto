"""
services/detector.py — Deteksi Deposit Crypto Otomatis (Full Auto, Tanpa Admin).
=================================================================================
Memantau transaksi masuk ke hot wallet untuk order dengan status
`WAITING_CRYPTO_DEPOSIT` (Sell maupun Convert/Swap).

Alur (bypass verifikasi admin -> full otomatis):
1. Verifikasi TX hash di blockchain (on-chain) via services.tx_verifier.
2. Jika tidak ada hash, auto-scan riwayat transaksi masuk wallet.
3. Terverifikasi -> status `CRYPTO_CONFIRMED` + notif user.
4. Order Swap/Convert -> eksekusi payout otomatis koin tujuan ke wallet buyer
   -> sukses: `COMPLETED` + notif (TX hash + explorer).
   -> gagal: `PAYOUT_QUEUED` + notif admin (kirim manual).
5. Order Sell -> notif admin untuk transfer Rupiah (bank tetap manual).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape as _esc
from weakref import WeakValueDictionary

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from config.settings import settings
from database.connection import SessionLocal
from database.models import Order, AuditLog, DepositClaim
from sqlalchemy.exc import IntegrityError
from services import tx_verifier
from bot.keyboards.main_menu import get_owner_button
from bot.utils.formatter import format_crypto, format_idr
from bot.utils.telegram_utils import safe_send_message, notify_admins

# Alasan verifikasi yang permanen: hash tidak akan pernah jadi deposit sah.
# Hash seperti ini dilepas dari order (sekali) agar tidak diverifikasi ulang
# terus-menerus oleh scan 20 detik.
# Kelebihan bayar yang masih dianggap pembulatan wajar pada hash kiriman user.
USER_HASH_OVERPAY_TOLERANCE = Decimal("0.005")

ALASAN_HASH_BATAL = (
    "Transfer ke diri sendiri bukan deposit.",
    "Penerima tidak cocok.",
    "Tidak ada transfer token masuk ke wallet deposit pada transaksi ini.",
    "Token tidak terdaftar.",
    "Transaksi gagal atau belum confirmed.",
)

logger = logging.getLogger(__name__)

_payout_locks = WeakValueDictionary()


class DepositDetector:
    def __init__(self):
        self.is_running = False
        self._processing_orders = set()
        self._lock = asyncio.Lock()

    # ---------------- Main ----------------
    async def scan_incoming_deposits(self, bot_app=None):
        """
        Memindai deposit masuk untuk order berstatus WAITING_CRYPTO_DEPOSIT.
        Dilengkapi in-memory locking agar tidak terjadi pemrosesan ganda / notifikasi dobel.
        Setiap task paralel menggunakan session DB-nya sendiri untuk thread safety.
        """
        db = SessionLocal()
        try:
            deposit_window_start = datetime.utcnow() - timedelta(
                minutes=settings.SELL_DEPOSIT_WINDOW_MINUTES * 2)
            pending_orders = db.query(Order).filter(
                Order.created_at >= deposit_window_start,
                Order.status.in_(["WAITING_CRYPTO_DEPOSIT", "PAYOUT_QUEUED"])
                | (
                    (Order.status == "expired")
                    & (Order.order_type.in_(["sell", "swap"]))
                )
            ).all()

            if not pending_orders:
                return

            # Filter order yang sedang diproses di cycle sebelumnya
            orders_to_process = []
            async with self._lock:
                for o in pending_orders:
                    if o.order_id not in self._processing_orders:
                        self._processing_orders.add(o.order_id)
                        orders_to_process.append(o.order_id)  # simpan ID, bukan ORM object

        except Exception as exc:
            logger.error("Error mengambil pending orders: %s", exc, exc_info=True)
            return
        finally:
            db.close()  # tutup session setelah ambil data awal

        if not orders_to_process:
            return

        # Proses paralel (max 5) — setiap task membuat session DB sendiri
        sem = asyncio.Semaphore(5)

        async def _proc(order_id):
            async with sem:
                task_db = SessionLocal()
                try:
                    order = task_db.query(Order).filter(Order.order_id == order_id).first()
                    if order:
                        await self._process_order(task_db, order, bot_app)
                except Exception as order_exc:
                    logger.error(
                        "Error memproses deposit order %s: %s",
                        order_id, order_exc,
                        exc_info=True,
                    )
                finally:
                    task_db.close()
                    async with self._lock:
                        self._processing_orders.discard(order_id)

        await asyncio.gather(*(_proc(oid) for oid in orders_to_process))

    # ---------------- Per order ----------------
    @staticmethod
    def deposit_deadline(order) -> datetime:
        """Batas verifikasi deposit: created_at + jendela (default 24 jam, sesuai info bot ke user)."""
        base = order.created_at or datetime.utcnow()
        return base + timedelta(minutes=settings.SELL_DEPOSIT_WINDOW_MINUTES)

    async def verifikasi_cepat(self, order_id, bot_app=None, attempts=12, interval=5):
        """Ulangi verifikasi deposit tiap 5 detik (maks ~1 menit) supaya user cepat
        dapat kabar. Notifikasi sukses tetap dikirim oleh _confirm_order."""
        for _ in range(attempts):
            await asyncio.sleep(interval)
            db = SessionLocal()
            try:
                order = db.query(Order).filter(Order.order_id == order_id).first()
                if not order or order.status != "WAITING_CRYPTO_DEPOSIT":
                    return
                await self._process_order(db, order, bot_app)
            except Exception as exc:
                logger.warning("Verifikasi cepat %s gagal: %s", order_id, exc)
            finally:
                db.close()

    @staticmethod
    def is_recoverable_expired(order) -> bool:
        """Order sell/swap yang sudah 'expired' tetap boleh diverifikasi selama depositnya sah."""
        return order.status == "expired" and order.order_type in ("sell", "swap")

    async def _process_order(self, db, order, bot_app, trusted=False):
        """trusted=True: admin sudah memeriksa manual (lewati guard hash user & quote kedaluwarsa)."""
        if order.status not in ("WAITING_CRYPTO_DEPOSIT", "PAYOUT_QUEUED") and not self.is_recoverable_expired(order):
            return
        expected_wallet = order.deposit_wallet or ""
        expected_amount = float(order.crypto_amount or 0)

        # Order yang payout-nya pernah gagal/crash (PAYOUT_QUEUED tanpa hash) -> retry langsung.
        if order.status == "PAYOUT_QUEUED":
            # A crashed worker may already have broadcast. Never blindly resend.
            if order.updated_at and (datetime.utcnow() - order.updated_at).total_seconds() > 120:
                order.status = "manual_review"
                order.failure_reason = "Payout terputus; periksa receipt sebelum mengirim ulang."
                db.commit()
            return

        tx_hash = (order.deposit_tx_hash or order.tx_hash or "").strip()
        if tx_hash.startswith("PHOTO:"):
            tx_hash = ""  # bukti foto -> andalkan auto-scan riwayat

        # 1. Verifikasi TX hash on-chain (jika ada)
        verified = None
        if tx_hash:
            verified = await tx_verifier.verify_deposit(
                network=order.network,
                symbol=order.crypto_symbol,
                tx_hash=tx_hash,
                expected_wallet=expected_wallet,
                expected_amount=expected_amount,
                not_before=order.created_at,
                not_after=self.deposit_deadline(order),
            )
            if (verified and not verified.get("verified")
                    and (verified.get("reason") or "") in ALASAN_HASH_BATAL):
                alasan = verified.get("reason")
                order.deposit_tx_hash = None
                order.tx_hash = None
                db.add(AuditLog(
                    telegram_id=order.telegram_id,
                    action="DEPOSIT_HASH_REJECTED",
                    order_id=order.order_id,
                    from_status=order.status,
                    to_status=order.status,
                    details=f"Hash {tx_hash} dilepas permanen: {alasan}",
                ))
                db.commit()
                logger.warning("Order %s: hash deposit %s ditolak permanen (%s)",
                               order.order_id, tx_hash, alasan)
                if bot_app:
                    await notify_admins(
                        bot_app,
                        f"\u26a0\ufe0f <b>HASH DEPOSIT DITOLAK</b>\n\n"
                        f"Order: <code>{order.order_id}</code>\n"
                        f"TX Hash: <code>{tx_hash}</code>\n"
                        f"Alasan: {alasan}\n\n"
                        f"Hash dilepas dari order; auto-scan tetap mencari deposit lain yang sah.",
                        kind="error", butuh_tindakan=True)
                tx_hash = ""
                verified = None

        # 1b. Hash kiriman user lolos on-chain — pastikan memang deposit order ini.
        if not trusted and verified and verified.get("verified"):
            review = self.user_hash_review_reason(db, order, verified)
            if review:
                await self.escalate_user_hash(db, order, tx_hash, review, bot_app)
                return

        # 2. Auto-scan riwayat transaksi masuk wallet (jika belum terverifikasi)
        if not verified or not verified.get("verified"):
            if expected_wallet:
                incoming = await tx_verifier.get_recent_incoming(
                    network=order.network,
                    symbol=order.crypto_symbol,
                    wallet=expected_wallet,
                    min_amount=expected_amount,
                    limit=20,
                    not_before=order.created_at,
                    not_after=self.deposit_deadline(order),
                )
                # Equal quotes on a shared address cannot be attributed safely.
                competing = db.query(Order).filter(
                    Order.order_id != order.order_id,
                    Order.status == "WAITING_CRYPTO_DEPOSIT",
                    Order.network == order.network,
                    Order.crypto_symbol == order.crypto_symbol,
                    Order.deposit_wallet == expected_wallet,
                    Order.crypto_amount == order.crypto_amount,
                ).first()
                if competing:
                    return
                for txn in incoming:
                    if not txn.get("tx_hash"):
                        continue
                    # Hindari double-claim hash dengan order lain
                    if self._is_hash_used(db, txn["tx_hash"], exclude_order=order.order_id):
                        continue
                    ver = await tx_verifier.verify_deposit(
                        network=order.network,
                        symbol=order.crypto_symbol,
                        tx_hash=txn["tx_hash"],
                        expected_wallet=expected_wallet,
                        expected_amount=expected_amount,
                        not_before=order.created_at,
                        not_after=self.deposit_deadline(order),
                    )
                    if ver.get("verified"):
                        tx_hash = ver["tx_hash"]
                        verified = ver
                        break

        if not verified or not verified.get("verified"):
            reason = (verified or {}).get("reason") or "-"
            logger.info(
                "Order %s: deposit belum terverifikasi (hash=%s, alasan=%s)",
                order.order_id, tx_hash or "-", reason,
            )
            return

        tx_hash = verified.get("tx_hash", tx_hash)

        # Convert dibayar otomatis: deposit yang masuk SETELAH quote berakhir tidak boleh
        # dibayar dengan kurs lama (dulu tetap auto-payout hingga 24 jam). Admin cek kurs dulu.
        if (not trusted and order.order_type == "swap" and order.quote_expires_at
                and verified.get("timestamp")
                and verified["timestamp"] > tx_verifier._timestamp(order.quote_expires_at) + 120):
            await self.escalate_user_hash(
                db, order, tx_hash,
                "Deposit masuk setelah masa quote convert berakhir — cek kurs terkini sebelum koin dikirim.",
                bot_app,
            )
            return

        # Guard anti reuse hash: hash yang sudah diklaim order lain tidak boleh
        # mengonfirmasi order ini (jalur verifikasi hash langsung maupun auto-scan).
        if tx_hash and self._is_hash_used(db, tx_hash, exclude_order=order.order_id):
            logger.warning(
                "Order %s: TX hash %s sudah dipakai order lain — tidak diklaim",
                order.order_id, tx_hash,
            )
            return

        # 3. Konfirmasi deposit
        await self._confirm_order(db, order, tx_hash, verified, bot_app)

    # ---------------- Confirm & Payout ----------------
    async def _confirm_order(self, db, order, tx_hash, verified, bot_app):
        claimable = ("WAITING_CRYPTO_DEPOSIT", "expired") if self.is_recoverable_expired(order) else ("WAITING_CRYPTO_DEPOSIT",)
        if order.status not in claimable:
            return
        # The direct user hash path and the background scanner share these guards.
        if not verified.get("verified") or not verified.get("timestamp") or not order.created_at:
            return
        if verified["timestamp"] < int(tx_verifier._timestamp(order.created_at)):
            return
        deadline = self.deposit_deadline(order)
        if verified["timestamp"] > tx_verifier._timestamp(deadline):
            return
        if not tx_verifier._amount_matches(verified.get("amount", 0), order.crypto_amount):
            return
        tx_hash = tx_verifier.normalize_tx_hash(order.network, verified.get("tx_hash") or tx_hash)
        if self._is_hash_used(db, tx_hash, exclude_order=order.order_id):
            return
        try:
            db.add(DepositClaim(network=order.network.upper(), tx_hash=tx_hash, order_id=order.order_id))
            db.flush()
            changed = db.query(Order).filter(Order.order_id == order.order_id,
                Order.status.in_(claimable)).update({"status": "CRYPTO_CONFIRMED"}, synchronize_session=False)
            if changed != 1:
                db.rollback()
                return
        except IntegrityError:
            db.rollback()
            return

        old_status = order.status
        order.status = "CRYPTO_CONFIRMED"
        if tx_hash:
            order.deposit_tx_hash = tx_hash
        db.add(AuditLog(
            telegram_id=order.telegram_id,
            action="DEPOSIT_CONFIRMED_ONCHAIN",
            order_id=order.order_id,
            from_status=old_status,
            to_status="CRYPTO_CONFIRMED",
            details=f"Deposit {verified.get('amount')} {order.crypto_symbol} ({order.network}) "
                    f"terverifikasi otomatis. Hash: {tx_hash}",
        ))
        db.commit()

        # Notif user: transaksi masuk terverifikasi
        if bot_app:
            try:
                user_msg = (
                    f"✅ <b>Transaksi Masuk Terverifikasi Otomatis!</b>\n\n"
                    f"ID Order: <code>{order.order_id}</code>\n"
                    f"Deposit: {format_crypto(verified.get('amount'), order.crypto_symbol)} ({order.network})\n"
                    f"TX Hash: <code>{tx_hash}</code>\n\n"
                )
                if order.order_type == "swap":
                    user_msg += (
                        f"🔄 Sedang mengirim <b>{order.target_crypto_symbol} "
                        f"({order.target_network})</b> ke walletmu..."
                    )
                else:
                    user_msg += (
                        "💰 Admin akan segera memproses pembayaran Rupiah "
                        "ke rekeningmu."
                    )
                keyboard = InlineKeyboardMarkup([[get_owner_button()]])
                await safe_send_message(bot_app, order.telegram_id, user_msg, reply_markup=keyboard)
            except Exception as exc:
                logger.warning("Gagal notif user %s: %s", order.telegram_id, exc)

        if order.order_type == "swap":
            await self._execute_payout(db, order, bot_app)
        else:
            # Sell: notif admin untuk transfer Rupiah
            if bot_app:
                try:
                    admin_msg = (
                        f"💰 <b>DEPOSIT CRYPTO TERVERIFIKASI (SELL)</b>\n\n"
                        f"Order: <code>{order.order_id}</code>\n"
                        f"User ID: <code>{order.telegram_id}</code>\n"
                        f"Deposit: {format_crypto(verified.get('amount'), order.crypto_symbol)} ({order.network})\n"
                        f"TX Hash: <code>{_esc(tx_hash)}</code>\n\n"
                        f"‼️ <b>TRANSFER RUPIAH SEGERA:</b> "
                        f"<b>{format_idr(order.total_idr)}</b> ke rekening:\n"
                        f"<code>{_esc(order.buyer_wallet)}</code>\n\n"
                        f"Setelah transfer, klik tombol di bawah atau ketik <code>/confirm {order.order_id}</code>."
                    )
                    admin_keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Sudah Ditransfer", callback_data=f"admin_confirm_sell_{order.order_id}"),
                            InlineKeyboardButton("📸 Upload Bukti Transfer", callback_data=f"admin_upload_proof_{order.order_id}")
                        ]
                    ])
                    await notify_admins(bot_app, admin_msg, reply_markup=admin_keyboard, order_type="sell", kind="jual")
                except Exception as exc:
                    logger.warning("Gagal notif admin sell: %s", exc)

    async def _execute_payout(self, db, order, bot_app):
        """Eksekusi pengiriman koin tujuan (swap) ke wallet buyer."""
        from services.payout_service import send_order_payout
        from database.crud import reserve_order_inventory, release_order_inventory

        # Guard anti double-payout: hash sudah ada berarti payout pernah sukses.
        if order.payout_tx_hash:
            return

        lock = _payout_locks.setdefault(order.order_id, asyncio.Lock())
        async with lock:
            db.refresh(order)
            if order.payout_tx_hash or order.status == "COMPLETED":
                return

            if order.status != "CRYPTO_CONFIRMED":
                return

            old_status = order.status
            # Cross-process claim: an in-memory lock alone cannot protect two workers.
            changed = db.query(Order).filter(Order.order_id == order.order_id,
                Order.status == "CRYPTO_CONFIRMED", Order.payout_tx_hash.is_(None)).update(
                    {"status": "PAYOUT_QUEUED", "updated_at": datetime.utcnow()}, synchronize_session=False)
            if changed != 1:
                db.rollback()
                return
            db.add(AuditLog(
                telegram_id=order.telegram_id,
                action="PAYOUT_QUEUED",
                order_id=order.order_id,
                from_status=old_status,
                to_status="PAYOUT_QUEUED",
                details=f"Auto-payout {order.target_crypto_amount} {order.target_crypto_symbol} "
                        f"({order.target_network}) ke {order.buyer_wallet}",
            ))
            db.commit()
            db.refresh(order)

            if not reserve_order_inventory(
                db,
                order.order_id,
                order.target_network,
                order.target_crypto_symbol,
                Decimal(str(order.target_crypto_amount)),
            ):
                result = {
                    "success": False,
                    "tx_hash": "",
                    "explorer_url": "",
                    "error_message": (
                        f"Stok {order.target_crypto_symbol} ({order.target_network}) tidak mencukupi. "
                        "Silakan proses manual melalui admin."
                    ),
                }
            else:
                result = await send_order_payout(order)

            if result.get("success"):
                order.status = "COMPLETED"
                order.payout_tx_hash = result.get("tx_hash")
                order.completed_at = datetime.utcnow()
                db.add(AuditLog(
                    telegram_id=order.telegram_id,
                    action="SWAP_COMPLETED",
                    order_id=order.order_id,
                    from_status="PAYOUT_QUEUED",
                    to_status="COMPLETED",
                    details=f"Payout sukses. Hash: {result.get('tx_hash')}",
                ))
                db.commit()
                release_order_inventory(db, order.order_id)
                from database.crud import auto_save_order_accounts
                auto_save_order_accounts(db, order)

                if bot_app:
                    try:
                        user_msg = (
                            f"🎉 <b>CONVERT BERHASIL!</b>\n\n"
                            f"ID Order: <code>{order.order_id}</code>\n"
                            f"Terima: {format_crypto(float(order.target_crypto_amount), order.target_crypto_symbol)} "
                            f"({order.target_network})\n"
                            f"TX Hash: <code>{result.get('tx_hash')}</code>\n"
                        )
                        if result.get("explorer_url"):
                            user_msg += f"\n🔗 <a href=\"{result['explorer_url']}\">Lihat di Explorer</a>"
                        user_msg += "\n\nTerima kasih sudah menggunakan layanan kami! 🙏"
                        menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")]])
                        await safe_send_message(bot_app, order.telegram_id, user_msg, reply_markup=menu_keyboard)
                    except Exception as exc:
                        logger.warning("Gagal notif payout sukses: %s", exc)
                if bot_app:
                    try:
                        admin_msg = (
                            f"\u2705 <b>CONVERT SELESAI (AUTO-PAYOUT)</b>\n\n"
                            f"Order: <code>{order.order_id}</code>\n"
                            f"User ID: <code>{order.telegram_id}</code>\n"
                            f"Kirim: {format_crypto(float(order.crypto_amount or 0), order.crypto_symbol)} ({order.network})\n"
                            f"Terima: {format_crypto(float(order.target_crypto_amount or 0), order.target_crypto_symbol)} ({order.target_network})\n"
                            f"Wallet Tujuan: <code>{order.buyer_wallet}</code>\n"
                            f"TX Payout: <code>{result.get('tx_hash')}</code>"
                        )
                        await notify_admins(bot_app, admin_msg, kind="convert")
                    except Exception as exc:
                        logger.warning("Gagal notif admin convert sukses: %s", exc)

                    # Referral reward trigger on completion
                    try:
                        from database.crud import complete_referral, get_referral_by_referee
                        trade_amt = float(getattr(order, "nominal_idr", 0) or getattr(order, "total_idr", 0) or 0)
                        ref_result = complete_referral(db, order.telegram_id, trade_amount_idr=trade_amt)
                        if ref_result:
                            ref = get_referral_by_referee(db, order.telegram_id)
                            if ref:
                                reward = ref.reward_idr or 0
                                await safe_send_message(
                                    bot_app,
                                    ref.referrer_id,
                                    f"🎉 <b>Referral Reward!</b>\n\n"
                                    f"User yang Anda ajak telah menyelesaikan transaksi.\n"
                                    f"Saldo Anda bertambah <b>Rp {reward:,}</b>!",
                                )
                    except Exception as exc:
                        logger.warning("Gagal proses referral reward user %s: %s", order.telegram_id, exc)
            else:
                order.status = "manual_review"
                if result.get("tx_hash"):
                    order.payout_tx_hash = result["tx_hash"]
                order.failure_reason = result.get("error_message") or "Auto-payout gagal"
                db.commit()
                if bot_app:
                    try:
                        admin_msg = (
                            f"🚨 <b>AUTO-PAYOUT GAGAL (CONVERT)</b>\n\n"
                            f"Order: <code>{order.order_id}</code>\n"
                            f"Kirim: {order.target_crypto_amount} {order.target_crypto_symbol} "
                            f"({order.target_network})\n"
                            f"Wallet: <code>{order.buyer_wallet}</code>\n"
                            f"Error: {order.failure_reason}\n\n"
                            f"TX payout: <code>{order.payout_tx_hash or '-'}</code>\n"
                            "Periksa receipt dan riwayat wallet terlebih dahulu. "
                            "Jangan kirim ulang jika status broadcast belum pasti."
                        )
                        await notify_admins(bot_app, admin_msg, kind="convert")
                        await safe_send_message(
                            bot_app,
                            order.telegram_id,
                            f"⏳ <b>Convert memerlukan bantuan admin</b>\n\n"
                            f"Order: <code>{order.order_id}</code>\n"
                            "Pembayaran/deposit sudah diterima, tetapi pengiriman koin tujuan "
                            "belum dapat dilakukan otomatis. Silakan hubungi admin. 🙏",
                        )
                    except Exception as exc:
                        logger.warning("Gagal notif admin payout gagal: %s", exc)

    # ---------------- Guard hash kiriman user ----------------
    @staticmethod
    def _deposit_fits(received, expected) -> bool:
        """Deposit cocok untuk order: tidak kurang, dan lebih paling banyak 0,5% (pembulatan)."""
        try:
            received, expected = Decimal(str(received)), Decimal(str(expected))
        except Exception:
            return False
        if not (received.is_finite() and expected.is_finite()) or expected <= 0:
            return False
        return expected <= received <= expected * (1 + USER_HASH_OVERPAY_TOLERANCE)

    def user_hash_review_reason(self, db, order, verified) -> str:
        """Alasan hash kiriman user TIDAK boleh dikonfirmasi otomatis ('' = aman).

        Hot wallet dipakai bersama: siapa pun bisa menempel hash deposit milik
        orang lain. Hash hanya auto-konfirmasi bila nominalnya pas untuk order ini
        DAN tidak juga pas untuk order lain yang sedang menunggu deposit.
        """
        received = verified.get("amount", 0)
        if not self._deposit_fits(received, order.crypto_amount):
            return (f"Nominal deposit {received} {order.crypto_symbol} tidak sesuai order "
                    f"({float(order.crypto_amount):g}).")
        others = db.query(Order).filter(
            Order.order_id != order.order_id,
            Order.status == "WAITING_CRYPTO_DEPOSIT",
            Order.network == order.network,
            Order.crypto_symbol == order.crypto_symbol,
            Order.deposit_wallet == order.deposit_wallet,
        ).all()
        for other in others:
            if self._deposit_fits(received, other.crypto_amount):
                return (f"Deposit juga cocok dengan order lain yang menunggu ({other.order_id}) — "
                        f"pemiliknya tidak bisa dipastikan otomatis.")
        return ""

    async def escalate_user_hash(self, db, order, tx_hash, reason, bot_app) -> None:
        """Minta admin memeriksa hash (sekali per order — scan berulang tidak spam)."""
        # Satu notifikasi per (order, hash): scan 20 dtk tidak spam, tapi hash BARU dari user
        # yang sama tetap sampai ke admin.
        already = db.query(AuditLog.id).filter(
            AuditLog.order_id == order.order_id, AuditLog.action == "DEPOSIT_HASH_NEEDS_REVIEW",
            AuditLog.details.like(f"Hash {tx_hash}:%"),
        ).first()
        if already:
            return
        db.add(AuditLog(
            telegram_id=order.telegram_id, action="DEPOSIT_HASH_NEEDS_REVIEW",
            order_id=order.order_id, from_status=order.status, to_status=order.status,
            details=f"Hash {tx_hash}: {reason}",
        ))
        db.commit()
        logger.warning("Order %s: hash %s butuh review admin (%s)", order.order_id, tx_hash, reason)
        if not bot_app:
            return
        if order.order_type == "swap":
            button = InlineKeyboardButton("✅ Proses Convert (sudah dicek)", callback_data=f"admin_approve_swap_{order.order_id}")
        else:
            button = InlineKeyboardButton("✅ Konfirmasi Deposit (sudah dicek)", callback_data=f"admin_force_sell_{order.order_id}")
        await notify_admins(
            bot_app,
            f"🕵️ <b>HASH DEPOSIT PERLU DICEK MANUAL</b>\n\n"
            f"Order: <code>{order.order_id}</code> ({order.order_type})\n"
            f"User: <code>{order.telegram_id}</code>\n"
            f"Order: {format_crypto(order.crypto_amount, order.crypto_symbol)} ({order.network})\n"
            f"TX Hash: <code>{_esc(tx_hash or '-')}</code>\n"
            f"Alasan: {_esc(reason)}\n\n"
            f"<i>Pastikan pengirim deposit memang user ini sebelum konfirmasi.</i>",
            reply_markup=InlineKeyboardMarkup([[button]]),
            kind="error", butuh_tindakan=True,
        )

    # ---------------- Helpers ----------------
    @staticmethod
    def _is_hash_used(db, tx_hash, exclude_order):
        if not tx_hash:
            return True
        if db.query(DepositClaim).filter(DepositClaim.tx_hash == tx_hash,
                                       DepositClaim.order_id != exclude_order).first():
            return True
        existing = (
            db.query(Order)
            .filter(Order.deposit_tx_hash == tx_hash)
            .filter(Order.order_id != exclude_order)
            .filter(Order.status != "WAITING_CRYPTO_DEPOSIT")
            .first()
        )
        return existing is not None

deposit_detector = DepositDetector()
