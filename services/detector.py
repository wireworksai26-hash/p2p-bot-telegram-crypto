"""
services/detector.py — Deteksi Deposit Crypto Otomatis (Full Auto, Tanpa Admin).
=================================================================================
Memantau transaksi masuk ke hot wallet untuk order dengan status
`WAITING_CRYPTO_DEPOSIT` (Sell maupun Convert/Swap).

Alur (bypass verifikasi admin -> full otomatis):
1. Verifikasi TX hash kiriman user di blockchain (on-chain) via services.tx_verifier.
2. Tanpa hash order TIDAK dikonfirmasi: nominal koin tidak lagi berkode unik, jadi
   auto-scan riwayat wallet hanya jalan bila DEPOSIT_AUTOSCAN_ENABLED=true.
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
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from services import tx_verifier, quote_guard
from bot.keyboards.main_menu import get_owner_button
from bot.utils.formatter import format_crypto, format_crypto_copy, format_idr
from bot.utils.telegram_utils import safe_send_message, notify_admins
from bot.utils.manual_payout import manual_payout_button
from bot.utils.admin_alert import order_detail_block

# Alasan verifikasi yang permanen: hash tidak akan pernah jadi deposit sah.
# Hash seperti ini dilepas dari order (sekali) agar tidak diverifikasi ulang
# terus-menerus oleh scan 20 detik.
ALASAN_HASH_BATAL = (
    "Transfer ke diri sendiri bukan deposit.",
    "Penerima tidak cocok.",
    "Tidak ada transfer token masuk ke wallet deposit pada transaksi ini.",
    "Token tidak terdaftar.",
    "Transaksi gagal atau belum confirmed.",
)

logger = logging.getLogger(__name__)

# Payout dianggap macet hanya setelah melewati semua tunggu sender. Dulu 120 dtk: TRON menunggu
# solid sampai 150 dtk, sehingga payout yang masih berjalan dipindah ke manual_review.
PAYOUT_INFLIGHT_SECONDS = 900

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
            if order.updated_at and (datetime.utcnow() - order.updated_at).total_seconds() > PAYOUT_INFLIGHT_SECONDS:
                order.status = "manual_review"
                order.failure_reason = "Payout terputus; periksa receipt sebelum mengirim ulang."
                db.commit()
                if bot_app:
                    await notify_admins(
                        bot_app,
                        f"🚨 <b>PAYOUT TERPUTUS — CEK DULU SEBELUM KIRIM ULANG</b>\n\n"
                        f"{order_detail_block(order, db)}\n\n"
                        f"Bot berhenti saat mengirim, jadi koin <b>mungkin sudah terkirim</b>. Cek explorer "
                        f"wallet tujuan dulu. Bila belum masuk, kirim manual lalu tekan tombol di bawah "
                        f"dan kirim bukti (SS / TX hash).",
                        reply_markup=InlineKeyboardMarkup([[manual_payout_button(order.order_id)]]),
                        kind="error", butuh_tindakan=True)
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
                expected_sender=getattr(order, "sender_wallet", None),
            )
            if (verified and not verified.get("verified")
                    and ((verified.get("reason") or "") in ALASAN_HASH_BATAL
                         or (verified.get("reason") or "").startswith("Alamat pengirim tidak sesuai"))):
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
                        f"{order_detail_block(order, db)}\n\n"
                        f"TX Hash: <code>{_esc(tx_hash)}</code>\n"
                        f"Alasan: {_esc(alasan)}\n\n"
                        f"Hash ini bukan deposit sah untuk order di atas dan sudah dilepas. Order tetap "
                        f"menunggu deposit yang benar; tidak ada koin yang perlu dikirim.",
                        kind="error", butuh_tindakan=True)
                tx_hash = ""
                verified = None

        # 1a. Koin masuk ke wallet deposit tapi nominalnya tidak sesuai order (kurang / jauh lebih
        # besar). Koin user sudah di wallet, jadi admin wajib tahu. Dulu hanya order lama berkode
        # unik yang dieskalasi; sisanya diam saja sampai order expired.
        mismatch = str((verified or {}).get("reason") or "")
        if (not trusted and tx_hash and verified and not verified.get("verified")
                and mismatch.startswith("Nominal deposit")):
            reason = mismatch
            from services.deposit_amount import base_amount, deposit_code_of
            if mismatch.startswith("Nominal deposit kurang") and deposit_code_of(order.crypto_amount, order.crypto_symbol):
                base_check = await tx_verifier.verify_deposit(
                    network=order.network, symbol=order.crypto_symbol, tx_hash=tx_hash,
                    expected_wallet=expected_wallet,
                    expected_amount=float(base_amount(order.crypto_symbol, order.crypto_amount)),
                    not_before=order.created_at, not_after=self.deposit_deadline(order),
                )
                if base_check.get("verified"):
                    reason = (f"Deposit {base_check.get('amount')} {order.crypto_symbol} tanpa kode unik "
                              f"(order meminta {order.crypto_amount}). Pastikan pengirimnya user ini.")
            await self.escalate_user_hash(
                db, order, tx_hash, reason, bot_app,
                user_note=(
                    f"⚠️ <b>Nominal deposit tidak sesuai</b>\n\n"
                    f"Order: <code>{_esc(order.order_id)}</code>\n"
                    f"{_esc(mismatch)}\n\n"
                    f"Koinmu sudah masuk dan akan dicek manual oleh admin. <b>Jangan kirim ulang.</b> "
                    f"Admin akan mengabari hasilnya di sini. 🙏"
                ),
                amount_mismatch=True,
            )
            return

        # 1b. Hash kiriman user lolos on-chain — pastikan memang deposit order ini.
        if not trusted and verified and verified.get("verified"):
            review = self.user_hash_review_reason(db, order, verified)
            if review:
                await self.escalate_user_hash(db, order, tx_hash, review, bot_app, verified=verified)
                return

        # 2. Auto-scan riwayat transaksi masuk wallet (jika belum terverifikasi).
        # Default mati: tanpa kode unik, deposit tidak bisa dipastikan milik order ini.
        if (not verified or not verified.get("verified")) and settings.DEPOSIT_AUTOSCAN_ENABLED:
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
                # Equal quotes on a shared address cannot be attributed safely —
                # termasuk order expired yang depositnya masih bisa masuk (deposit telat).
                from services.deposit_amount import active_deposit_orders
                if any(tx_verifier.automatic_amount_matches(o.crypto_amount, order.crypto_amount)
                       for o in active_deposit_orders(db, order.network, order.crypto_symbol,
                                                      expected_wallet, exclude_order=order.order_id)):
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

        # Jual & Convert: deposit yang masuk SETELAH masa berlaku order (10 menit) tidak boleh
        # dibayar dengan harga terkunci — user bisa menunggu harga turun lalu baru mengirim koin.
        # Admin cek dulu dan membayar sesuai harga terkini.
        expiry = order.quote_expires_at or order.expired_at
        if (not trusted and order.order_type in ("swap", "sell") and expiry
                and verified.get("timestamp")
                and verified["timestamp"] > tx_verifier._timestamp(expiry) + quote_guard.LATE_DEPOSIT_GRACE_SECONDS):
            reason, user_note = await self._late_deposit_texts(order)
            await self.escalate_user_hash(db, order, tx_hash, reason, bot_app, user_note=user_note,
                                          verified=verified)
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
                        "ke rekeningmu.\n"
                        f"⏳ Mohon tunggu transfer admin (estimasi maksimal "
                        f"{quote_guard.SELL_PAYOUT_ETA_MINUTES} menit pada jam layanan 08.00 - 23.59 WIB)."
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
                        f"💰 <b>DEPOSIT CRYPTO TERVERIFIKASI (JUAL)</b>\n\n"
                        f"{order_detail_block(order, db)}\n\n"
                        f"Diterima on-chain: {format_crypto_copy(verified.get('amount'), order.crypto_symbol)} ({order.network})\n"
                        f"Pengirim: <code>{_esc(str(verified.get('from_address') or '-'))}</code>\n"
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
                release_order_inventory(db, order.order_id, consumed=True)
                from database.crud import auto_save_order_accounts
                auto_save_order_accounts(db, order)

                if bot_app:
                    from services.testimony_service import schedule_transaction_testimony
                    schedule_transaction_testimony(bot_app, order, db=db)

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
                            f"{order_detail_block(order, db)}\n\n"
                            f"TX Payout: <code>{_esc(str(result.get('tx_hash') or '-'))}</code>"
                        )
                        await notify_admins(bot_app, admin_msg, kind="convert")
                    except Exception as exc:
                        logger.warning("Gagal notif admin convert sukses: %s", exc)

                # Referral: hitung reward untuk transaksi selesai (notifikasi oleh sweeper referral)
                from database.crud import process_referral_rewards_for_user
                process_referral_rewards_for_user(db, order.telegram_id)
            else:
                order.status = "manual_review"
                if result.get("tx_hash"):
                    order.payout_tx_hash = result["tx_hash"]
                order.failure_reason = (result.get("error_message") or "Auto-payout gagal")[:480]
                db.commit()
                if bot_app:
                    try:
                        admin_msg = (
                            f"🚨 <b>AUTO-PAYOUT GAGAL (CONVERT)</b>\n\n"
                            f"{order_detail_block(order, db)}\n\n"
                            f"Deposit user sudah diterima; pengiriman koin tujuan gagal atau belum pasti.\n"
                            f"Error: {_esc(str(order.failure_reason or '-'))}\n"
                            f"TX payout: <code>{_esc(str(order.payout_tx_hash or '-'))}</code>\n\n"
                            "Periksa receipt dan riwayat wallet terlebih dahulu. "
                            "Jangan kirim ulang jika status broadcast belum pasti.\n"
                            "Jika koin dikirim manual, tekan tombol di bawah dan kirim bukti (SS / TX hash) "
                            "agar diteruskan ke user."
                        )
                        await notify_admins(
                            bot_app, admin_msg,
                            reply_markup=InlineKeyboardMarkup([[manual_payout_button(order.order_id)]]),
                            kind="convert", butuh_tindakan=True,
                        )
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
        """Deposit cocok untuk order: nominal PERSIS (nominal order sudah berkode unik).

        Dulu lebih bayar s/d 0,5% diterima — penyerang cukup membuat order sedikit
        di bawah transfer orang lain (mis. isi ulang stok) lalu menempel hash-nya.
        Kurang/lebih bayar kini selalu dicek admin.
        """
        try:
            received, expected = Decimal(str(received)), Decimal(str(expected))
        except Exception:
            return False
        if not (received.is_finite() and expected.is_finite()) or expected <= 0:
            return False
        return tx_verifier.automatic_amount_matches(received, expected)

    def user_hash_review_reason(self, db, order, verified) -> str:
        """Alasan hash kiriman user TIDAK boleh dikonfirmasi otomatis ('' = aman).

        Hot wallet dipakai bersama: siapa pun bisa menempel hash deposit milik
        orang lain. Hash hanya auto-konfirmasi bila nominalnya pas untuk order ini
        DAN tidak juga pas untuk order lain yang sedang menunggu deposit.
        """
        sender = str(verified.get("from_address") or "").strip().lower()
        if sender and sender in settings.OWNER_WALLET_ADDRESSES:
            return ("Pengirim deposit adalah wallet owner (isi ulang stok), bukan wallet user — "
                    "tidak boleh dikonfirmasi otomatis.")
        # Verifier mengisi from_address; kosong berarti pengirim tidak bisa dibaca dari chain,
        # jadi pemilik deposit tidak bisa dipastikan -> admin cek (hash orang lain bisa ditempel).
        if "from_address" in verified and not sender:
            return "Pengirim deposit tidak dapat dibaca dari blockchain — pastikan pengirimnya user ini."
        received = verified.get("amount", 0)
        if not self._deposit_fits(received, order.crypto_amount):
            return (f"Nominal deposit {received} {order.crypto_symbol} tidak sesuai order "
                    f"({float(order.crypto_amount):g}).")
        from services.deposit_amount import active_deposit_orders
        others = active_deposit_orders(db, order.network, order.crypto_symbol,
                                       order.deposit_wallet, exclude_order=order.order_id)
        for other in others:
            if self._deposit_fits(received, other.crypto_amount):
                return (f"Deposit juga cocok dengan order lain yang menunggu ({other.order_id}) — "
                        f"pemiliknya tidak bisa dipastikan otomatis.")
        return ""

    @staticmethod
    async def _late_deposit_texts(order):
        """(alasan untuk admin, kabar untuk user) saat deposit masuk setelah order kedaluwarsa."""
        minutes = quote_guard.QUOTE_MINUTES
        if order.order_type == "swap":
            return (
                f"Deposit masuk setelah masa quote convert ({minutes} menit) berakhir — "
                f"cek kurs terkini sebelum koin dikirim.",
                f"⏰ Deposit Order <code>{order.order_id}</code> masuk setelah batas {minutes} menit. "
                f"Kurs lama sudah tidak berlaku, jadi admin mengecek kurs terkini dulu sebelum koin dikirim. "
                f"Mohon tunggu.",
            )
        reason = (f"Deposit masuk setelah masa berlaku order Jual ({minutes} menit) — harga terkunci "
                  f"tidak berlaku. Bayar sesuai harga terkini, bukan harga terkunci.")
        try:
            from services.price_service import price_service
            info = await price_service.get_price(order.crypto_symbol)
            now_price = float((info or {}).get("sell_price_idr") or 0)
            locked = float(order.price_per_unit or 0)
            if now_price > 0 and locked > 0:
                pct = (now_price - locked) / locked * 100
                estimate = int(float(order.total_idr or 0) * now_price / locked)
                reason += (f" Harga terkunci {format_idr(locked)}/koin; sekarang {format_idr(now_price)}/koin "
                           f"({pct:+.2f}%). Terkunci: {format_idr(order.total_idr)}; "
                           f"perkiraan sesuai harga sekarang: ±{format_idr(estimate)}.")
        except Exception as exc:
            logger.warning("Harga terkini untuk deposit telat %s tak tersedia: %s", order.order_id, exc)
        user_note = (
            f"⏰ Deposit Order <code>{order.order_id}</code> masuk setelah batas {minutes} menit, jadi harga "
            f"awal sudah tidak berlaku. Admin akan mengecek dan membayar sesuai <b>harga terkini</b>. "
            f"Mohon tunggu (estimasi maksimal {quote_guard.SELL_PAYOUT_ETA_MINUTES} menit pada jam layanan)."
        )
        return reason, user_note

    async def escalate_user_hash(self, db, order, tx_hash, reason, bot_app, user_note=None,
                                 verified=None, amount_mismatch=False) -> None:
        """Minta admin memeriksa hash (sekali per order — scan berulang tidak spam).

        amount_mismatch: nominal di luar toleransi, sehingga "Proses Convert" otomatis pasti
        gagal; admin diarahkan ke Proses Manual. Order ini tetap tampil di Antrean Order
        (crud.deposit_review_filter) walau sudah expired.
        """
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
        await safe_send_message(bot_app, order.telegram_id, user_note or (
            f"🕵️ <b>Deposit Order <code>{_esc(order.order_id)}</code> sedang dicek admin</b>\n\n"
            "Transaksimu terdeteksi di blockchain, tetapi perlu dicocokkan manual oleh admin "
            "sebelum diproses. Kamu akan menerima notifikasi setelah selesai. 🙏"))

        received = ""
        if verified and verified.get("amount") is not None:
            received = (f"Diterima on-chain: {format_crypto_copy(verified.get('amount'), order.crypto_symbol)} "
                        f"({_esc(str(order.network))}) dari <code>{_esc(str(verified.get('from_address') or '-'))}</code>\n")
        if order.order_type == "swap":
            target = format_crypto_copy(order.target_crypto_amount or 0, order.target_crypto_symbol or "-", exact=True)
            reject = InlineKeyboardButton("❌ Tolak / Batalkan", callback_data=f"admin_reject_swap_{order.order_id}")
            manual = InlineKeyboardButton("🛠 Proses Manual", callback_data=f"admin_swap_manual_{order.order_id}")
            if amount_mismatch:
                buttons = [[manual, reject]]
                steps = ("Nominal di luar toleransi, jadi bot <b>tidak bisa</b> mengirim koin otomatis.\n"
                         "• Deposit milik user ini → tekan <b>🛠 Proses Manual</b>, kirim koin sendiri "
                         "(penuh / sesuai deposit diterima / refund), lalu kirim bukti (SS / TX hash).\n"
                         "• Bukan milik user ini → tekan <b>❌ Tolak</b>.")
            else:
                buttons = [[InlineKeyboardButton("✅ Proses Convert (sudah dicek)", callback_data=f"admin_approve_swap_{order.order_id}")],
                           [manual, reject]]
                steps = (f"• Deposit sah milik user ini → <b>✅ Proses Convert</b> (bot mengirim {target}).\n"
                         "• Perlu kirim nominal lain → <b>🛠 Proses Manual</b>.\n"
                         "• Bukan milik user ini → <b>❌ Tolak</b>.")
        else:
            # Hanya menandai deposit sah; Rupiah tetap lewat tombol "Sudah Ditransfer" sesudahnya.
            buttons = [[InlineKeyboardButton("✅ Konfirmasi Deposit (sudah dicek)", callback_data=f"admin_verify_sell_deposit_{order.order_id}")]]
            steps = ("• Deposit sah milik user ini → <b>✅ Konfirmasi Deposit</b>, lalu transfer Rupiah"
                     + (" <b>sesuai koin yang benar-benar diterima</b>." if amount_mismatch else ".")
                     + "\n• Bukan milik user ini → biarkan; jangan transfer Rupiah.")
        await notify_admins(
            bot_app,
            f"🕵️ <b>DEPOSIT PERLU DICEK MANUAL</b>\n\n"
            f"{order_detail_block(order, db)}\n\n"
            f"TX Hash: <code>{_esc(tx_hash or '-')}</code>\n"
            f"{received}"
            f"Alasan: {_esc(reason)}\n\n"
            f"<b>Langkah admin:</b>\n{steps}\n\n"
            f"<i>Order ini tetap ada di 📥 Antrean Order sampai diputuskan, walau sudah lewat batas waktu.</i>",
            reply_markup=InlineKeyboardMarkup(buttons),
            kind="error", butuh_tindakan=True,
        )

    # ---------------- Helpers ----------------
    @staticmethod
    def _is_hash_used(db, tx_hash, exclude_order):
        """True bila hash ini sudah DITERIMA sebagai deposit order lain.

        Dibandingkan tanpa peduli format (huruf besar/kecil, prefix 0x, URL explorer),
        karena hash yang sama dulu bisa lolos dua kali lewat format berbeda (B6).
        Hash yang hanya ditempel ke order yang tidak pernah terkonfirmasi (menunggu,
        expired, cancelled) tidak dihitung — penyerang tidak bisa memblokir deposit
        korban dengan menempelkan hash-nya (B7).
        """
        if not tx_hash:
            return True
        keys = _hash_keys(tx_hash)
        if db.query(DepositClaim).filter(func.lower(DepositClaim.tx_hash).in_(keys),
                                         DepositClaim.order_id != exclude_order).first():
            return True
        existing = (
            db.query(Order)
            .filter(func.lower(Order.deposit_tx_hash).in_(keys))
            .filter(Order.order_id != exclude_order)
            .filter(Order.status.in_(HASH_ACCEPTED_STATUSES))
            .first()
        )
        return existing is not None


# Status order yang berarti deposit-nya pernah diterima (hash terpakai).
HASH_ACCEPTED_STATUSES = ("CRYPTO_CONFIRMED", "PAYOUT_QUEUED", "completed", "COMPLETED",
                          "manual_review", "payout_processing", "paid", "failed")


def _hash_keys(tx_hash: str) -> list:
    """Semua bentuk huruf-kecil yang mungkin dipakai untuk menyimpan hash yang sama."""
    from urllib.parse import urlparse, unquote
    value = (tx_hash or "").strip()
    if "://" in value:
        value = unquote(urlparse(value).path.rstrip("/").split("/")[-1])
    bare = value.lower()
    if bare.startswith("0x"):
        bare = bare[2:]
    return list({value.lower(), bare, "0x" + bare})


deposit_detector = DepositDetector()
