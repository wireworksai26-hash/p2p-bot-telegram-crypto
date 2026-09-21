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
                minutes=settings.SELL_DEPOSIT_WINDOW_MINUTES)
            pending_orders = db.query(Order).filter(
                Order.status.in_(["WAITING_CRYPTO_DEPOSIT", "PAYOUT_QUEUED"])
                | (
                    (Order.status == "expired")
                    & (Order.order_type == "sell")
                    & (Order.created_at >= deposit_window_start)
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

    @staticmethod
    def is_recoverable_expired(order) -> bool:
        """Order sell/swap yang sudah 'expired' tetap boleh diverifikasi selama depositnya sah."""
        return order.status == "expired" and order.order_type in ("sell", "swap")

    async def _process_order(self, db, order, bot_app):
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
                        f"TX Hash: <code>{tx_hash}</code>\n\n"
                        f"‼️ <b>TRANSFER RUPIAH SEGERA:</b> "
                        f"<b>{format_idr(order.total_idr)}</b> ke rekening:\n"
                        f"<code>{order.buyer_wallet}</code>\n\n"
                        f"Setelah transfer, klik tombol di bawah atau ketik <code>/confirm {order.order_id}</code>."
                    )
                    admin_keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Sudah Ditransfer", callback_data=f"admin_confirm_sell_{order.order_id}"),
                            InlineKeyboardButton("📸 Upload Bukti Transfer", callback_data=f"admin_upload_proof_{order.order_id}")
                        ]
                    ])
                    await notify_admins(bot_app, admin_msg, reply_markup=admin_keyboard, order_type="sell")
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
                        await notify_admins(bot_app, admin_msg)
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
