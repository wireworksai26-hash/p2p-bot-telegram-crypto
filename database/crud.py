"""
database/crud.py — Semua operasi CRUD untuk database P2P Crypto Bot.

Module ini berisi fungsi-fungsi untuk Create, Read, Update data
di database menggunakan SQLAlchemy sessions.
Semua fungsi menerima `db` (SQLAlchemy Session) sebagai parameter pertama.
"""

import logging
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from config.assets import STOCK_MAX_AGE_SECONDS

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database.models import (
    User,
    Order,
    WalletBalance,
    InventoryReservation,
    PriceConfig,
    TopupOrder,
    MonthlyReport,
    GopaySession,
    AuditLog,
    Referral,
    ReferralConfig,
    ReferralDiscount,
    LoyaltyReward,
    LoyaltyConfig,
    AdminActionToken,
    CampaignActionLock,
)

logger = logging.getLogger(__name__)


# ============================================================
# USER CRUD — Operasi untuk tabel users
# ============================================================

def create_user(db: Session, telegram_id: int, username: str = None, full_name: str = None) -> User:
    """
    Buat user baru atau return user yang sudah ada.
    Kalau user dengan telegram_id sudah exist, update profilnya jika ada perubahan dan return existing.
    """
    try:
        # Cek apakah user sudah ada
        existing = db.query(User).filter(User.telegram_id == telegram_id).first()
        if existing:
            updated = False
            if username and existing.username != username:
                existing.username = username
                updated = True
            if full_name and existing.full_name != full_name:
                existing.full_name = full_name
                updated = True
            if updated:
                db.commit()
                db.refresh(existing)
            logger.info(f"User {telegram_id} sudah terdaftar, return existing.")
            return existing

        # Buat user baru
        new_user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        logger.info(f"User baru dibuat: {telegram_id} ({username})")
        return new_user

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal create user {telegram_id}: {e}")
        raise



def get_user(db: Session, telegram_id: int) -> Optional[User]:
    """Ambil data user berdasarkan telegram_id. Return None jika tidak ditemukan."""
    try:
        return db.query(User).filter(User.telegram_id == telegram_id).first()
    except Exception as e:
        logger.error(f"Gagal get user {telegram_id}: {e}")
        raise


def get_user_by_identifier(db: Session, identifier: str) -> Optional[User]:
    """
    Mencari user berdasarkan @username (case-insensitive) atau telegram_id numerik.
    Mendukung format: '@alice', 'alice', '123456789'.
    """
    if not identifier:
        return None

    clean = str(identifier).strip()
    if not clean:
        return None

    # 1. Jika numerik murni -> cari by telegram_id
    if clean.isdigit():
        try:
            tid = int(clean)
            user_by_id = db.query(User).filter(User.telegram_id == tid).first()
            if user_by_id:
                return user_by_id
        except Exception:
            pass

    # 2. Cari by username (dengan atau tanpa '@')
    username_clean = clean.lstrip("@").strip()
    if username_clean:
        try:
            return (
                db.query(User)
                .filter(func.lower(User.username) == username_clean.lower())
                .first()
            )
        except Exception as e:
            logger.error(f"Gagal get_user_by_identifier '{identifier}': {e}")
            return None

    return None



# ============================================================
# ORDER CRUD — Operasi untuk tabel orders
# ============================================================

def create_order(db: Session, order_data: dict) -> Order:
    """
    Buat order baru dari dictionary data.
    order_data harus berisi minimal: order_id, telegram_id, crypto_symbol,
    network, crypto_amount, price_per_unit, nominal_idr, fee_idr, total_idr.
    """
    try:
        # Validasi field wajib
        required_fields = [
            "order_id", "telegram_id", "crypto_symbol", "network",
            "crypto_amount", "price_per_unit", "nominal_idr", "fee_idr", "total_idr"
        ]
        for field in required_fields:
            if field not in order_data:
                raise ValueError(f"Field wajib '{field}' tidak ada di order_data")

        new_order = Order(**order_data)
        db.add(new_order)

        # Update user stats (total_orders + total_spent)
        user = db.query(User).filter(User.telegram_id == order_data["telegram_id"]).first()
        if user:
            user.total_orders = (user.total_orders or 0) + 1
            user.total_spent_idr = (user.total_spent_idr or 0) + order_data["total_idr"]

        db.commit()
        db.refresh(new_order)
        logger.info(f"Order baru dibuat: {new_order.order_id} untuk user {new_order.telegram_id}")
        return new_order

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal create order: {e}")
        raise


def create_bot_balance_order(db: Session, order_data: dict) -> Optional[Order]:
    """Buat order Saldo Bot, debit saldo, statistik, dan paid_at dalam satu transaksi."""
    from sqlalchemy import update

    if order_data.get("payment_method") != "BOT_BALANCE":
        raise ValueError("create_bot_balance_order hanya untuk payment_method BOT_BALANCE.")
    amount = _positive_amount(order_data.get("total_idr"))
    if amount <= 0:
        raise ValueError("Nominal order Saldo Bot harus lebih besar dari nol.")

    try:
        order = Order(**order_data)
        db.add(order)
        db.flush()
        spent = int(order_data["total_idr"])
        changed = db.execute(
            update(User)
            .where(
                User.telegram_id == int(order_data["telegram_id"]),
                func.coalesce(User.balance_idr, 0) >= amount,
            )
            .values(
                balance_idr=func.coalesce(User.balance_idr, 0) - amount,
                total_orders=func.coalesce(User.total_orders, 0) + 1,
                total_spent_idr=func.coalesce(User.total_spent_idr, 0) + spent,
            )
        )
        if changed.rowcount != 1:
            db.rollback()
            return None

        order.paid_at = datetime.utcnow()
        db.commit()
        db.refresh(order)
        return order
    except Exception:
        db.rollback()
        logger.exception("Gagal membuat order Saldo Bot atomik")
        raise


def reject_and_refund_bot_balance_order(db: Session, order_id: str, admin_id: int) -> dict:
    """Tolak order beli Saldo Bot dan refund satu kali dalam transaksi DB yang sama."""
    from sqlalchemy import update

    order = db.query(Order).filter(Order.order_id == order_id).first()
    if not order or order.order_type != "buy" or order.payment_method != "BOT_BALANCE":
        return {"refunded": False, "reason": "not_bot_balance_order"}
    if order.status != "pending":
        return {"refunded": False, "reason": "status_changed", "status": order.status}
    if order.paid_at is None:
        return {"refunded": False, "reason": "debit_not_confirmed"}

    amount = int(order.total_idr or 0)
    if amount <= 0:
        return {"refunded": False, "reason": "invalid_refund_amount"}

    try:
        now = datetime.utcnow()
        changed = db.execute(
            update(Order)
            .where(
                Order.order_id == order_id,
                Order.order_type == "buy",
                Order.payment_method == "BOT_BALANCE",
                Order.status == "pending",
                Order.paid_at.isnot(None),
                Order.total_idr > 0,
            )
            .values(
                status="rejected",
                failure_reason="Ditolak oleh admin (Saldo di-refund)",
                updated_at=now,
            )
        )
        if changed.rowcount != 1:
            db.rollback()
            return {"refunded": False, "reason": "status_changed"}

        credited = db.execute(
            update(User)
            .where(User.telegram_id == order.telegram_id)
            .values(balance_idr=func.coalesce(User.balance_idr, 0) + amount)
        )
        if credited.rowcount != 1:
            raise RuntimeError(f"User {order.telegram_id} tidak ditemukan saat refund {order_id}")

        db.add(AuditLog(
            telegram_id=order.telegram_id,
            action="BOT_BALANCE_ORDER_REFUND",
            order_id=order_id,
            from_status="pending",
            to_status="rejected",
            details=f"Admin {admin_id} menolak order Saldo Bot dan mengembalikan Rp {amount:,}.",
        ))
        db.commit()
        return {"refunded": True, "telegram_id": int(order.telegram_id), "amount": amount}
    except Exception:
        db.rollback()
        logger.exception("Gagal menolak/refund order Saldo Bot %s", order_id)
        raise


def get_order_by_id(db: Session, order_id: str) -> Optional[Order]:
    """Cari order berdasarkan order_id (string unik, bukan auto-increment id)."""
    try:
        return db.query(Order).filter(Order.order_id == order_id).first()
    except Exception as e:
        logger.error(f"Gagal get order {order_id}: {e}")
        raise


def get_orders_by_user(db: Session, telegram_id: int, limit: int = 10) -> list[Order]:
    """Ambil list order milik user tertentu, diurutkan dari terbaru. Default limit 10."""
    try:
        return (
            db.query(Order)
            .filter(Order.telegram_id == telegram_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
            .all()
        )
    except Exception as e:
        logger.error(f"Gagal get orders for user {telegram_id}: {e}")
        raise


def update_order_status(
    db: Session,
    order_id: str,
    new_status: str,
    **extra_fields
) -> Optional[Order]:
    """
    Update status order dan field tambahan lainnya.

    Contoh penggunaan:
        update_order_status(db, "ORD-123", "paid", paid_at=datetime.utcnow())
        update_order_status(db, "ORD-123", "completed", tx_hash="0xabc...", completed_at=datetime.utcnow())
    """
    try:
        order = db.query(Order).filter(Order.order_id == order_id).first()
        if not order:
            logger.warning(f"Order {order_id} tidak ditemukan untuk update status.")
            return None

        old_status = order.status
        # COMPLETED final: koin/Rupiah sudah berpindah, membatalkan/mengubahnya merusak
        # pembukuan dan bisa memicu proses ulang (bayar dua kali).
        if (str(old_status or "").lower() == "completed"
                and str(new_status or "").lower() != "completed"):
            logger.warning(f"Tolak transisi order {order_id}: {old_status} -> {new_status} (order sudah selesai)")
            return None
        order.status = new_status

        # Set extra fields yang dikirim (misal: paid_at, tx_hash, dll)
        for key, value in extra_fields.items():
            if hasattr(order, key):
                setattr(order, key, value)
            else:
                logger.warning(f"Field '{key}' tidak ada di model Order, di-skip.")

        db.commit()
        db.refresh(order)
        logger.info(f"Order {order_id} status updated: {old_status} -> {new_status}")

        if new_status and str(new_status).lower() == "completed":
            auto_save_order_accounts(db, order)
            try:
                amt = float(getattr(order, "nominal_idr", 0) or getattr(order, "total_idr", 0) or 0)
                complete_referral(db, order.telegram_id, trade_amount_idr=amt,
                                  fee_idr=float(order.fee_idr or 0))
            except Exception as ref_err:
                logger.warning(f"Error completing referral on order {order_id}: {ref_err}")

        return order

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal update order {order_id}: {e}")
        raise


def claim_order_paid(db: Session, order_id: str, allow_expired_qris: bool = False) -> bool:
    """
    Atomic claim order: pending -> paid.
    Hanya satu pemanggil yang menang (rowcount == 1); pemanggil lain dapat False.
    Mencegah double payout saat job polling & handler callback berjalan bersamaan.
    """
    from sqlalchemy import update
    try:
        claimable_status = Order.status == "pending"
        if allow_expired_qris:
            claimable_status = or_(
                claimable_status,
                (Order.status == "expired") & (Order.payment_method == "GOPAY_QRIS") & Order.paid_at.is_(None),
            )
        result = db.execute(
            update(Order)
            .where(Order.order_id == order_id, claimable_status)
            .values(status="paid", paid_at=datetime.utcnow(), updated_at=datetime.utcnow())
        )
        db.commit()
        return result.rowcount == 1
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal claim order {order_id}: {e}")
        raise


def claim_order_payout_processing(
    db: Session,
    order_id: str,
    allowed_statuses: tuple[str, ...] = ("paid",),
) -> bool:
    """Atomic claim payout: hanya satu worker boleh mengirim crypto."""
    from sqlalchemy import update
    try:
        result = db.execute(
            update(Order)
            .where(
                Order.order_id == order_id,
                Order.status.in_(allowed_statuses),
                Order.payout_tx_hash.is_(None),
            )
            .values(status="payout_processing", updated_at=datetime.utcnow())
        )
        db.commit()
        return result.rowcount == 1
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal claim payout order {order_id}: {e}")
        raise


def claim_stale_payout_processing(db: Session, order_id: str, stale_seconds: int = 120) -> bool:
    """Re-claim payout_processing yang stale setelah process crash."""
    from sqlalchemy import update
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=stale_seconds)
        result = db.execute(
            update(Order)
            .where(
                Order.order_id == order_id,
                Order.status == "payout_processing",
                Order.payout_tx_hash.is_(None),
                Order.updated_at <= cutoff,
            )
            .values(updated_at=datetime.utcnow())
        )
        db.commit()
        return result.rowcount == 1
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal reclaim payout order {order_id}: {e}")
        raise


def claim_topup_success(db: Session, topup_id: str, allow_expired: bool = False) -> bool:
    """
    Atomic claim topup: PENDING -> SUCCESS (juga EXPIRED bila allow_expired, khusus
    persetujuan admin untuk user yang membayar di menit terakhir).
    Hanya satu pemanggil yang menang; cegah double credit saldo.
    """
    claimable = ["PENDING", "EXPIRED"] if allow_expired else ["PENDING"]
    from sqlalchemy import update
    try:
        result = db.execute(
            update(TopupOrder)
            .where(TopupOrder.topup_id == topup_id, TopupOrder.status.in_(claimable))
            .values(status="SUCCESS", paid_at=datetime.utcnow())
        )
        db.commit()
        return result.rowcount == 1
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal claim topup {topup_id}: {e}")
        raise


def is_treasury_topup(topup_id: str) -> bool:
    tid = str(topup_id or "")
    return tid.startswith("TREASURY-") or tid.startswith("TOPUP-TREASURY-")


def credit_claimed_topup(db: Session, topup) -> tuple[bool, int, float]:
    """Kreditkan topup yang SUDAH di-claim (claim_topup_success) — satu aturan untuk semua jalur.

    Yang masuk = amount_idr - pajak QRIS (mdr_idr); topup TREASURY masuk kas bot,
    bukan saldo pribadi admin. Returns (is_treasury, net_amount, new_balance).
    """
    net_amt = int(topup.amount_idr) - int(topup.mdr_idr or 0)
    if is_treasury_topup(topup.topup_id):
        new_bal = topup_bot_treasury(db, net_amt, admin_id=topup.telegram_id, note=f"QRIS Topup {topup.topup_id}")
        return True, net_amt, new_bal
    return False, net_amt, credit_user_balance(db, topup.telegram_id, net_amt)


def claim_qris_payment(db: Session, tx_id: str, ref_id: str, kind: str, amount_idr: int = None) -> bool:
    """Klaim atomik transaksi QRIS untuk satu order/topup (UNIQUE tx_id di DB).

    True bila transaksi baru diklaim untuk ref_id ini, ATAU sudah diklaim oleh
    ref_id yang sama (idempoten untuk re-check). False bila sudah dipakai
    order/topup lain — pembayaran itu tidak boleh melunasi ref_id ini.
    """
    from sqlalchemy.exc import IntegrityError
    from database.models import QrisPaymentClaim

    tx_id = str(tx_id or "").strip()
    if not tx_id:
        return False
    existing = db.query(QrisPaymentClaim).filter(QrisPaymentClaim.tx_id == tx_id).first()
    if existing:
        return existing.ref_id == ref_id
    try:
        db.add(QrisPaymentClaim(tx_id=tx_id, ref_id=ref_id, kind=kind, amount_idr=amount_idr))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()  # balapan: proses lain menang duluan
        existing = db.query(QrisPaymentClaim).filter(QrisPaymentClaim.tx_id == tx_id).first()
        return bool(existing and existing.ref_id == ref_id)


ADMIN_ACTION_TOKEN_TTL_SECONDS = 600


def issue_admin_action_token(
    db: Session,
    admin_id: int,
    action: str,
    payload: str,
    *,
    chat_id: int = None,
    message_id: int = None,
    ttl_seconds: int = ADMIN_ACTION_TOKEN_TTL_SECONDS,
) -> str:
    """Terbitkan token callback admin yang terikat ke admin, aksi, payload, dan pesan."""
    now = datetime.utcnow()
    token = secrets.token_hex(12)
    try:
        bound_chat_id = int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        bound_chat_id = None
    try:
        bound_message_id = int(message_id) if message_id is not None else None
    except (TypeError, ValueError):
        bound_message_id = None
    try:
        db.query(AdminActionToken).filter(AdminActionToken.expires_at <= now).delete(
            synchronize_session=False
        )
        db.add(AdminActionToken(
            token=token,
            admin_id=int(admin_id),
            action=str(action),
            payload=str(payload),
            chat_id=bound_chat_id,
            message_id=bound_message_id,
            created_at=now,
            expires_at=now + timedelta(seconds=max(1, int(ttl_seconds))),
        ))
        db.commit()
        return token
    except Exception:
        db.rollback()
        raise


def claim_admin_action_token(
    db: Session,
    token: str,
    admin_id: int,
    action: str,
    payload: str = None,
    *,
    chat_id: int = None,
    message_id: int = None,
) -> Optional[str]:
    """Klaim token secara atomik; perubahan ikut commit/rollback aksi pemanggil."""
    from sqlalchemy import update

    now = datetime.utcnow()
    query = db.query(AdminActionToken).filter(
        AdminActionToken.token == str(token or ""),
        AdminActionToken.admin_id == int(admin_id),
        AdminActionToken.action == str(action),
        AdminActionToken.consumed_at.is_(None),
        AdminActionToken.expires_at > now,
    )
    entry = query.first()
    if not entry or (payload is not None and entry.payload != str(payload)):
        return None
    if entry.chat_id is not None and (chat_id is None or entry.chat_id != int(chat_id)):
        return None
    if entry.message_id is not None and (message_id is None or entry.message_id != int(message_id)):
        return None

    conditions = [
        AdminActionToken.token == entry.token,
        AdminActionToken.admin_id == int(admin_id),
        AdminActionToken.action == str(action),
        AdminActionToken.payload == entry.payload,
        AdminActionToken.consumed_at.is_(None),
        AdminActionToken.expires_at > now,
    ]
    if entry.chat_id is not None:
        conditions.append(AdminActionToken.chat_id == int(chat_id))
    if entry.message_id is not None:
        conditions.append(AdminActionToken.message_id == int(message_id))
    result = db.execute(
        update(AdminActionToken)
        .where(*conditions)
        .values(consumed_at=now)
    )
    return entry.payload if result.rowcount == 1 else None


def claim_campaign_action_lock(db: Session, action: str, cooldown_seconds: int) -> bool:
    """Ambil lock campaign lintas worker hingga cooldown berakhir, tanpa commit sendiri."""
    from sqlalchemy import update

    dialect = db.get_bind().dialect.name
    values = {"action": str(action)}
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        db.execute(insert(CampaignActionLock).values(**values).on_conflict_do_nothing(
            index_elements=[CampaignActionLock.action]
        ))
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(CampaignActionLock).values(**values).on_conflict_do_nothing(
            index_elements=[CampaignActionLock.action]
        ))
    else:
        if not db.query(CampaignActionLock.action).filter_by(action=action).first():
            db.add(CampaignActionLock(action=action))
            db.flush()

    now = datetime.utcnow()
    result = db.execute(
        update(CampaignActionLock)
        .where(
            CampaignActionLock.action == str(action),
            or_(CampaignActionLock.locked_until.is_(None), CampaignActionLock.locked_until <= now),
        )
        .values(
            locked_until=now + timedelta(seconds=max(1, int(cooldown_seconds))),
            updated_at=now,
        )
    )
    return result.rowcount == 1


def is_qris_payment_claimed(db: Session, tx_id: str) -> bool:
    from database.models import QrisPaymentClaim
    tx_id = str(tx_id or "").strip()
    return bool(tx_id) and db.query(QrisPaymentClaim.id).filter(QrisPaymentClaim.tx_id == tx_id).first() is not None


def get_gopay_resume_orders(db: Session) -> list[Order]:
    """
    Order GOPAY_QRIS berstatus 'paid' tanpa payout_tx_hash.
    Artinya payout pernah gagal/crash sebelum selesai — layak di-retry otomatis.
    """
    gopay_stuck = (
        db.query(Order)
        .filter(
            Order.payment_method == "GOPAY_QRIS",
            Order.status.in_(("paid", "payout_processing")),
            Order.payout_tx_hash.is_(None),
        )
        .all()
    )
    # Beli pakai Saldo Bot: saldo sudah dipotong (paid_at terisi) tapi order masih
    # 'pending' bila task payout hilang (crash/restart sebelum finalize jalan).
    balance_paid = (
        db.query(Order)
        .filter(
            Order.payment_method == "BOT_BALANCE",
            Order.status == "pending",
            Order.paid_at.isnot(None),
            Order.payout_tx_hash.is_(None),
        )
        .all()
    )
    return gopay_stuck + balance_paid


def expire_stale_orders(db: Session, minutes: int = 15, exclude_order_ids: set[str] = None) -> int:
    """
    Menandai order yang kedaluwarsa menjadi 'expired':
      - Order 'pending' lama (created_at > cutoff).
      - Order 'WAITING_CRYPTO_DEPOSIT' (sell/swap) yang expired_at /
        quote_expires_at sudah lewat.
      - Order deposit yang macet melewati 2x jendela deposit.
    Mengembalikan jumlah order yang ter-expire.
    """
    try:
        now = datetime.utcnow()
        cutoff_time = now - timedelta(minutes=minutes)
        pending_query = db.query(Order).filter(
                Order.status == "pending",
                Order.created_at <= cutoff_time,
                # Sudah dibayar (Saldo Bot) — jangan expire, biarkan resume payout.
                Order.paid_at.is_(None),
            )
        if exclude_order_ids:
            pending_query = pending_query.filter(~Order.order_id.in_(set(exclude_order_ids)))
        pending = pending_query.all()
        deposit_waiting = (
            db.query(Order)
            .filter(
                Order.status == "WAITING_CRYPTO_DEPOSIT",
                or_(
                    Order.expired_at.isnot(None),
                    Order.quote_expires_at.isnot(None),
                ),
                or_(
                    Order.expired_at <= now,
                    Order.quote_expires_at <= now,
                ),
            )
            .all()
        )
        from config.settings import settings
        stuck_deposit = (
            db.query(Order)
            .filter(
                Order.status == "WAITING_CRYPTO_DEPOSIT",
                Order.created_at <= now - timedelta(minutes=settings.SELL_DEPOSIT_WINDOW_MINUTES * 2),
            )
            .all()
        )
        expired_orders = list({o.order_id: o for o in pending + deposit_waiting + stuck_deposit}.values())
        if not expired_orders:
            return 0

        # UPDATE bersyarat per order: order yang dibayar/diproses sesudah SELECT tadi tidak
        # boleh ikut di-expire (dulu status ditimpa dari objek ORM yang sudah basi).
        count = 0
        for order in expired_orders:
            guard = Order.status == order.status
            if order.status == "pending":
                guard = guard & Order.paid_at.is_(None)
            count += db.query(Order).filter(Order.order_id == order.order_id, guard).update(
                {"status": "expired"}, synchronize_session=False)
        db.commit()
        return count
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal memproses expire stale orders: {e}")
        raise



# ============================================================
# PRICE CONFIG CRUD — Operasi untuk tabel price_config
# ============================================================

def get_price_config(db: Session, symbol: str) -> Optional[PriceConfig]:
    """Ambil konfigurasi harga (spread_pct) untuk symbol tertentu."""
    try:
        return db.query(PriceConfig).filter(PriceConfig.symbol == symbol.upper()).first()
    except Exception as e:
        logger.error(f"Gagal get price config for {symbol}: {e}")
        raise


def update_price_config(db: Session, symbol: str, spread_pct: float) -> Optional[PriceConfig]:
    """Update spread_pct untuk symbol tertentu. Return None jika symbol tidak ditemukan."""
    try:
        config = db.query(PriceConfig).filter(PriceConfig.symbol == symbol.upper()).first()
        if not config:
            logger.warning(f"Price config untuk {symbol} tidak ditemukan.")
            return None

        config.spread_pct = Decimal(str(spread_pct))
        config.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(config)
        logger.info(f"Price config {symbol} updated: spread_pct = {spread_pct}%")
        return config

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal update price config {symbol}: {e}")
        raise


def get_all_price_configs(db: Session) -> list[PriceConfig]:
    """Ambil semua price config yang aktif."""
    try:
        return db.query(PriceConfig).filter(PriceConfig.is_active == True).all()  # noqa: E712
    except Exception as e:
        logger.error(f"Gagal get all price configs: {e}")
        raise


# ============================================================
# WALLET BALANCE CRUD — Operasi untuk tabel wallet_balances
# ============================================================

def update_wallet_balance(
    db: Session,
    network: str,
    balance: float,
    symbol: str = None,
    address: str = None
) -> WalletBalance:
    """
    Update atau insert wallet balance untuk network tertentu.
    Jika network sudah ada, update balance-nya. Jika belum, buat baru.
    """
    try:
        network_upper = network.upper()
        symbol_upper = (symbol or "").upper()
        wallet = db.query(WalletBalance).filter(
            WalletBalance.network == network_upper,
            WalletBalance.symbol == symbol_upper,
        ).first()

        # Fallback mappings for symbol and address if not provided
        if not symbol:
            symbol_map = {
                "BSC": "USDT", "ETH": "USDT", "AVAX": "USDT", "POLYGON": "USDT",
                "BASE": "USDT", "ARB": "USDT", "ROBINHOOD": "USDG",
                "SOLANA": "SOL", "TRON": "TRX",
            }
            symbol_upper = symbol_map.get(network_upper, "USDT").upper()

        if not address:
            from config.settings import settings
            if network_upper in ["BSC", "ETH", "AVAX", "POLYGON", "BASE", "ARB",
                                 "OPTIMISM", "ROBINHOOD", "KAIA", "BERA", "HYPEREVM"]:
                address = settings.EVM_WALLET_ADDRESS
            elif network_upper == "SOLANA":
                address = settings.SOL_WALLET_ADDRESS
            elif network_upper == "TRON":
                address = settings.TRX_WALLET_ADDRESS
            elif network_upper == "TON":
                address = settings.TON_WALLET_ADDRESS
            elif network_upper == "SUI":
                address = settings.SUI_WALLET_ADDRESS
            elif network_upper == "APTOS":
                address = settings.APTOS_WALLET_ADDRESS
            else:
                address = "Unknown"

        now = datetime.utcnow()
        if wallet:
            # Update existing
            wallet.balance = Decimal(str(balance))
            wallet.symbol = symbol_upper
            wallet.address = address
            wallet.sync_status = "OK"
            wallet.last_error = None
            wallet.last_checked_at = now
            wallet.last_success_at = now
            wallet.updated_at = now
        else:
            # Create new
            wallet = WalletBalance(
                network=network_upper,
                symbol=symbol_upper,
                balance=Decimal(str(balance)),
                address=address,
                sync_status="OK",
                last_checked_at=now,
                last_success_at=now,
            )
            db.add(wallet)

        db.commit()
        db.refresh(wallet)
        logger.info(f"Wallet balance updated: {network_upper} ({symbol_upper}) = {balance}")
        return wallet

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal update wallet balance {network}: {e}")
        raise


def mark_wallet_balance_error(
    db: Session,
    network: str,
    symbol: str,
    error: str = None,
    address: str = None,
) -> WalletBalance:
    """Catat kegagalan sinkronisasi tanpa mengubah saldo terakhir yang valid."""
    network_upper = network.upper()
    symbol_upper = symbol.upper()
    wallet = db.query(WalletBalance).filter(
        WalletBalance.network == network_upper,
        WalletBalance.symbol == symbol_upper,
    ).first()

    if not address:
        from config.settings import settings
        if network_upper in {
            "BSC", "ETH", "AVAX", "POLYGON", "BASE", "ARB",
            "OPTIMISM", "ROBINHOOD", "KAIA", "BERA", "HYPEREVM",
        }:
            address = settings.EVM_WALLET_ADDRESS
        else:
            address = {
                "SOLANA": settings.SOL_WALLET_ADDRESS,
                "TRON": settings.TRX_WALLET_ADDRESS,
                "TON": settings.TON_WALLET_ADDRESS,
                "SUI": settings.SUI_WALLET_ADDRESS,
                "APTOS": settings.APTOS_WALLET_ADDRESS,
            }.get(network_upper, "")

    if not wallet:
        wallet = WalletBalance(
            network=network_upper,
            symbol=symbol_upper,
            balance=0,
            reserved_balance=0,
            address=address or "Unknown",
        )
        db.add(wallet)

    wallet.sync_status = "ERROR"
    wallet.last_error = (error or "Gagal membaca saldo dari RPC")[:500]
    wallet.last_checked_at = datetime.utcnow()
    # Do not label an old wallet's balance as belonging to a new address.
    if address and wallet.address != address:
        wallet.balance = Decimal("0")
        wallet.last_success_at = None
    if address:
        wallet.address = address
    db.commit()
    db.refresh(wallet)
    return wallet


def get_all_wallet_balances(db: Session) -> list[WalletBalance]:
    """Ambil semua wallet balance records."""
    try:
        return db.query(WalletBalance).all()
    except Exception as e:
        logger.error(f"Gagal get all wallet balances: {e}")
        raise


def wallet_balance_is_fresh(wallet, now=None) -> bool:
    now = now or datetime.utcnow()
    return (
        wallet.sync_status == "OK"
        and wallet.last_success_at is not None
        and wallet.last_success_at >= now - timedelta(seconds=STOCK_MAX_AGE_SECONDS)
    )


def get_available_inventory(db: Session, network: str, symbol: str) -> Optional[Decimal]:
    """Saldo aset yang belum di-reserve untuk payout lain."""
    if network.upper() == "POLYGON" and symbol.upper() == "POL":
        symbol = "MATIC"
    wallet = (
        db.query(WalletBalance)
        .filter(
            WalletBalance.network == network.upper(),
            WalletBalance.symbol == symbol.upper(),
        )
        .first()
    )
    if not wallet:
        return None
    if not wallet_balance_is_fresh(wallet):
        return None
    balance = Decimal(str(wallet.balance or 0))
    reserved = Decimal(str(wallet.reserved_balance or 0))
    return max(Decimal("0"), balance - reserved)


def reserve_order_inventory(
    db: Session,
    order_id: str,
    network: str,
    symbol: str,
    amount: Decimal,
) -> bool:
    """Atomically reserve inventory untuk satu payout order."""
    from sqlalchemy import update

    if network.upper() == "POLYGON" and symbol.upper() == "POL":
        symbol = "MATIC"
    amount = Decimal(str(amount))
    if not amount.is_finite() or amount <= 0:
        return False
    existing = (
        db.query(InventoryReservation)
        .filter(
            InventoryReservation.order_id == order_id,
            InventoryReservation.status == "RESERVED",
        )
        .first()
    )
    if existing:
        wallet = db.query(WalletBalance).filter_by(
            network=network.upper(), symbol=symbol.upper(),
        ).first()
        return bool(
            wallet and wallet_balance_is_fresh(wallet)
            and existing.network == network.upper()
            and existing.symbol == symbol.upper()
            and Decimal(str(existing.amount)) == amount
        )

    try:
        result = db.execute(
            update(WalletBalance)
            .where(
                WalletBalance.network == network.upper(),
                WalletBalance.symbol == symbol.upper(),
                WalletBalance.sync_status == "OK",
                WalletBalance.last_success_at >= datetime.utcnow() - timedelta(seconds=STOCK_MAX_AGE_SECONDS),
                (WalletBalance.balance - WalletBalance.reserved_balance) >= amount,
            )
            .values(reserved_balance=WalletBalance.reserved_balance + amount)
        )
        if result.rowcount != 1:
            db.rollback()
            return False

        db.add(
            InventoryReservation(
                order_id=order_id,
                network=network.upper(),
                symbol=symbol.upper(),
                amount=amount,
                status="RESERVED",
            )
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("Gagal reserve inventory order %s", order_id)
        raise


def release_order_inventory(db: Session, order_id: str, consumed: bool = False) -> bool:
    """Lepas reservasi inventory secara atomik.

    consumed=True: koin benar-benar keluar dari wallet (payout sukses / dikirim manual),
    jadi `balance` ikut dikurangi — kalau tidak, stok tampak utuh sampai sync berikutnya
    dan bot menjual koin yang sudah tidak ada. consumed=False: order batal/gagal sebelum
    broadcast, hanya reservasi yang dilepas.
    Semua lewat UPDATE bersyarat (bukan baca-ubah-tulis dari objek yang mungkin basi).
    """
    from sqlalchemy import case, update
    reservation = (
        db.query(InventoryReservation)
        .filter(
            InventoryReservation.order_id == order_id,
            InventoryReservation.status == "RESERVED",
        )
        .first()
    )
    if not reservation:
        return False

    amount = Decimal(str(reservation.amount))
    try:
        claimed = db.query(InventoryReservation).filter(
            InventoryReservation.id == reservation.id,
            InventoryReservation.status == "RESERVED",
        ).update({"status": "RELEASED", "released_at": datetime.utcnow()}, synchronize_session=False)
        if claimed != 1:
            db.rollback()
            return False  # proses lain sudah melepasnya
        values = {"reserved_balance": case(
            (WalletBalance.reserved_balance >= amount, WalletBalance.reserved_balance - amount), else_=0)}
        if consumed:
            values["balance"] = case(
                (WalletBalance.balance >= amount, WalletBalance.balance - amount), else_=0)
        db.execute(update(WalletBalance).where(
            WalletBalance.network == reservation.network,
            WalletBalance.symbol == reservation.symbol,
        ).values(**values))
        db.commit()
        for obj in list(db.identity_map.values()):
            if isinstance(obj, (WalletBalance, InventoryReservation)):
                db.refresh(obj)
        return True
    except Exception:
        db.rollback()
        logger.exception("Gagal release inventory order %s", order_id)
        raise


def prune_wallet_balances(db: Session, valid_pairs: list) -> int:
    """
    Hapus baris wallet_balances yang (network, symbol) tidak termasuk daftar valid.
    Digunakan setelah sinkronisasi untuk membersihkan data lama yang tidak relevan.
    """
    try:
        valid = {(n.upper(), s.upper()) for s, n in valid_pairs}
        count = 0
        for row in db.query(WalletBalance).all():
            if (row.network.upper(), row.symbol.upper()) not in valid:
                db.delete(row)
                count += 1
        if count:
            db.commit()
            logger.info(f"Pruned {count} stale wallet balance rows")
        return count
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal prune wallet balances: {e}")
        return 0


def get_low_balance_wallets(db: Session) -> list[WalletBalance]:
    """
    Mengecek saldo wallet dan mengembalikan list wallet yang saldonya di bawah threshold.
    """
    try:
        thresholds = {
            "USDT": 10.0,
            "USDC": 10.0,
            "ETH": 0.002,
            "BNB": 0.005,
            "SOL": 0.05,
            "AVAX": 0.05,
            "TRX": 20.0,
            "MATIC": 5.0,
            "POLYGON": 5.0,
            "TON": 1.0,
            "SUI": 1.0,
            "APT": 0.2,
            "HYPE": 0.1,
            "USDG": 5.0,
            "ARB": 5.0,
        }
        
        all_wallets = db.query(WalletBalance).all()
        low_wallets = []
        
        for wallet in all_wallets:
            symbol = (wallet.symbol or "").upper()
            # Hanya periksa koin yang memiliki threshold terdaftar (abaikan koin tidak terdaftar)
            if symbol not in thresholds:
                continue
            limit = thresholds[symbol]
            if float(wallet.balance or 0) < limit:
                low_wallets.append(wallet)
                
        return low_wallets
    except Exception as e:
        logger.error(f"Gagal get low balance wallets: {e}")
        raise


def generate_unique_payment_code(
    db: Session, base_amount: Optional[int] = None, min_code: int = 1, max_code: int = 400
) -> Optional[int]:
    """
    Kode unik kecil (001..400) untuk tagihan QRIS baru, atau None bila tidak aman.

    Pembayaran QRIS dicocokkan hanya lewat nominal, jadi total tagihan baru
    (base_amount + kode) tidak boleh sama dengan tagihan PENDING mana pun —
    kalau sama, pembayaran satu orang bisa melunasi tagihan orang lain.
    Total kelipatan 100 juga dihindari (ambigu sen/rupiah di gateway).
    Kode pending juga tidak dipakai ulang. Bila semua kode habis → None
    (pemanggil wajib menolak tagihan, jangan memakai kode kembar).
    """
    import secrets
    used_codes = {
        r[0] for r in db.query(Order.unique_code).filter(
            Order.status == "pending", Order.unique_code > 0).all()
    } | {
        r[0] for r in db.query(TopupOrder.unique_code).filter(
            TopupOrder.status == "PENDING", TopupOrder.unique_code > 0).all()
    }
    pending_totals = {
        int(r[0]) for r in db.query(Order.total_idr).filter(
            Order.status == "pending", Order.payment_method == "GOPAY_QRIS").all()
        if r[0] is not None
    } | {
        int(r[0]) for r in db.query(TopupOrder.amount_idr).filter(
            TopupOrder.status == "PENDING").all()
        if r[0] is not None
    }
    available = [
        c for c in range(min_code, max_code + 1)
        if c not in used_codes
        and (base_amount is None or (
            int(base_amount) + c not in pending_totals
            # Total kelipatan 100 ambigu di gateway (sen vs rupiah) — lihat gopay-gateway/amount.js.
            and (int(base_amount) + c) % 100 != 0
            # Di atas batas QRIS (BI Rp 10 jt) QRIS dinamis tidak bisa dibuat.
            and int(base_amount) + c <= 10_000_000
        ))
    ]
    if not available:
        logger.warning("Kode unik QRIS habis/bentrok untuk base %s — tagihan ditolak", base_amount)
        return None
    return secrets.choice(available)




# ============================================================
# STATISTICS — Fungsi statistik dan reporting
# ============================================================

def get_user_count(db: Session) -> int:
    """Hitung total user yang terdaftar."""
    try:
        return db.query(func.count(User.telegram_id)).scalar() or 0
    except Exception as e:
        logger.error(f"Gagal get user count: {e}")
        raise


def get_completed_order_count(db: Session) -> int:
    """Jumlah total order yang berhasil (status completed) seumur hidup bot."""
    try:
        return db.query(func.count(Order.id)).filter(func.lower(Order.status) == "completed").scalar() or 0
    except Exception as e:
        logger.error(f"Gagal get completed order count: {e}")
        raise


def get_monthly_report(db: Session, period: str) -> Optional[MonthlyReport]:
    """Ambil laporan bulanan berdasarkan period 'YYYY-MM'. Return None jika belum ada."""
    return db.query(MonthlyReport).filter(MonthlyReport.period == period).first()


def build_monthly_report(db: Session, year: int, month: int) -> MonthlyReport:
    """
    Hitung laporan keuangan untuk (year, month):
      - Order completed (buy/sell/swap): jumlah, volume IDR, pendapatan fee.
      - Topup SUCCESS: jumlah & nominal IDR masuk.
    Batas bulan dihitung dalam WIB (UTC+7), lalu dikonversi ke UTC-naive agar
    cocok dengan created_at (datetime.utcnow). Hasil BELUM disimpan ke DB.
    """
    start_wib = datetime(year, month, 1)
    end_wib = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    start_utc = start_wib - timedelta(hours=7)
    end_utc = end_wib - timedelta(hours=7)

    orders = (
        db.query(Order)
        .filter(
            func.lower(Order.status) == "completed",
            Order.created_at >= start_utc,
            Order.created_at < end_utc,
        )
        .all()
    )
    topups = (
        db.query(TopupOrder)
        .filter(
            TopupOrder.status == "SUCCESS",
            TopupOrder.created_at >= start_utc,
            TopupOrder.created_at < end_utc,
        )
        .all()
    )

    volume_idr = sum(int(o.total_idr or 0) for o in orders)
    fee_idr = sum(int(o.fee_idr or 0) for o in orders)
    topup_idr = sum(int(t.amount_idr or 0) for t in topups)

    return MonthlyReport(
        period=f"{year:04d}-{month:02d}",
        order_count=len(orders),
        order_buy=sum(1 for o in orders if o.order_type == "buy"),
        order_sell=sum(1 for o in orders if o.order_type == "sell"),
        order_swap=sum(1 for o in orders if o.order_type == "swap"),
        volume_idr=volume_idr,
        fee_idr=fee_idr,
        topup_count=len(topups),
        topup_idr=topup_idr,
        # total_idr order sudah mengandung fee; menambahkan fee_idr lagi = hitung ganda.
        total_idr=volume_idr + topup_idr,
    )


def get_daily_stats(db: Session) -> dict:
    """
    Ambil statistik harian: total orders, total volume IDR, completed orders hari ini.
    """
    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Total orders hari ini (semua status)
        total_orders_today = (
            db.query(func.count(Order.id))
            .filter(Order.created_at >= today_start)
            .scalar() or 0
        )

        # Total volume IDR hari ini (dari orders yang completed)
        total_volume_idr_today = (
            db.query(func.coalesce(func.sum(Order.total_idr), 0))
            .filter(
                Order.created_at >= today_start,
                func.lower(Order.status) == "completed"
            )
            .scalar() or 0
        )

        # Jumlah completed orders hari ini
        completed_orders_today = (
            db.query(func.count(Order.id))
            .filter(
                Order.created_at >= today_start,
                func.lower(Order.status) == "completed"
            )
            .scalar() or 0
        )

        return {
            "total_orders_today": total_orders_today,
            "total_volume_idr_today": int(total_volume_idr_today),
            "completed_orders_today": completed_orders_today,
        }

    except Exception as e:
        logger.error(f"Gagal get daily stats: {e}")
        raise


# ==========================================
# USER BALANCE & TOPUP ORDER CRUD HELPERS
# ==========================================

def get_user_balance(db: Session, telegram_id: int) -> float:
    """Mengambil sisa saldo IDR pengguna dari database."""
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return 0.0
    return float(user.balance_idr or 0.0)


def _positive_amount(amount_idr) -> Decimal:
    amount = Decimal(str(amount_idr))
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"Nominal saldo tidak valid: {amount_idr}")
    return amount


def _refresh_balance(db: Session, telegram_id: int) -> float:
    # Objek User yang sudah dimuat sesi ini dimuat ulang agar tidak memegang saldo basi.
    for obj in list(db.identity_map.values()):
        if isinstance(obj, User) and obj.telegram_id == telegram_id:
            db.refresh(obj)
    value = db.query(User.balance_idr).filter(User.telegram_id == telegram_id).scalar()
    return float(value or 0)


def credit_user_balance(db: Session, telegram_id: int, amount_idr: float) -> float:
    """Menambahkan saldo IDR pengguna secara atomik (UPDATE balance = balance + n)."""
    amount = _positive_amount(amount_idr)
    try:
        if not db.query(User.telegram_id).filter(User.telegram_id == telegram_id).first():
            create_user(db, telegram_id)
        db.query(User).filter(User.telegram_id == telegram_id).update(
            {User.balance_idr: func.coalesce(User.balance_idr, 0) + amount},
            synchronize_session=False)
        db.commit()
        new_bal = _refresh_balance(db, telegram_id)
        logger.info(f"User {telegram_id} balance credited +Rp {amount:,.0f} -> New Balance: Rp {new_bal:,.0f}")
        return new_bal
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal credit saldo user {telegram_id}: {e}")
        raise


def deduct_user_balance(db: Session, telegram_id: int, amount_idr: float) -> bool:
    """Memotong saldo IDR secara atomik; False bila saldo tidak cukup.

    Satu UPDATE ... WHERE balance >= n: dua potongan bersamaan (atau sesi yang memegang
    saldo basi) tidak bisa sama-sama lolos.
    """
    amount = _positive_amount(amount_idr)
    try:
        changed = db.query(User).filter(
            User.telegram_id == telegram_id,
            func.coalesce(User.balance_idr, 0) >= amount,
        ).update({User.balance_idr: func.coalesce(User.balance_idr, 0) - amount},
                 synchronize_session=False)
        db.commit()
        if changed != 1:
            logger.warning(f"User {telegram_id} saldo tidak cukup untuk potongan Rp {amount:,.0f}")
            _refresh_balance(db, telegram_id)
            return False
        new_bal = _refresh_balance(db, telegram_id)
        logger.info(f"User {telegram_id} balance deducted -Rp {amount:,.0f} -> Sisa: Rp {new_bal:,.0f}")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal deduct saldo user {telegram_id}: {e}")
        raise


WITHDRAW_MIN_IDR = 10_000


def create_withdraw_request(db: Session, telegram_id: int, bank, amount_idr: int):
    """Potong saldo dan buat permintaan withdraw dalam SATU transaksi.

    Return WithdrawRequest, atau None bila saldo tidak cukup / nominal di bawah minimum.
    """
    from database.models import WithdrawRequest

    amount = int(amount_idr)
    if amount < WITHDRAW_MIN_IDR:
        return None
    try:
        changed = db.query(User).filter(
            User.telegram_id == telegram_id,
            func.coalesce(User.balance_idr, 0) >= amount,
        ).update({User.balance_idr: func.coalesce(User.balance_idr, 0) - amount},
                 synchronize_session=False)
        if changed != 1:
            db.rollback()
            return None
        req = WithdrawRequest(
            telegram_id=telegram_id,
            amount_idr=amount,
            bank_name=bank.bank_name,
            account_number=bank.account_number,
            account_name=bank.account_name,
        )
        db.add(req)
        db.add(AuditLog(
            telegram_id=telegram_id,
            action="WITHDRAW_REQUEST",
            to_status="PENDING",
            details=f"Withdraw Rp {amount:,} ke {bank.bank_name} {bank.account_number}.",
        ))
        db.commit()
        db.refresh(req)
        return req
    except Exception:
        db.rollback()
        logger.exception("Gagal membuat withdraw request user %s", telegram_id)
        raise


def get_withdraw_request(db: Session, request_id: int):
    from database.models import WithdrawRequest

    return db.query(WithdrawRequest).filter(WithdrawRequest.id == request_id).first()


def settle_withdraw_request(db: Session, request_id: int, admin_id: int, approve: bool) -> bool:
    """Tandai withdraw PAID, atau REJECTED + refund saldo. False bila sudah diproses (idempoten)."""
    from database.models import WithdrawRequest

    try:
        changed = db.query(WithdrawRequest).filter(
            WithdrawRequest.id == request_id,
            WithdrawRequest.status == "PENDING",
        ).update({
            "status": "PAID" if approve else "REJECTED",
            "handled_by": admin_id,
            "handled_at": datetime.utcnow(),
        }, synchronize_session=False)
        if changed != 1:
            db.rollback()
            return False
        req = db.query(WithdrawRequest).filter(WithdrawRequest.id == request_id).first()
        if not approve:
            db.query(User).filter(User.telegram_id == req.telegram_id).update(
                {User.balance_idr: func.coalesce(User.balance_idr, 0) + int(req.amount_idr)},
                synchronize_session=False)
        db.add(AuditLog(
            telegram_id=req.telegram_id,
            action="WITHDRAW_PAID" if approve else "WITHDRAW_REJECTED_REFUND",
            from_status="PENDING",
            to_status="PAID" if approve else "REJECTED",
            details=f"Admin {admin_id} {'mencairkan' if approve else 'menolak & refund'} withdraw #{request_id} Rp {int(req.amount_idr):,}.",
        ))
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("Gagal settle withdraw #%s", request_id)
        raise


def create_topup_order(
    db: Session,
    topup_id: str,
    telegram_id: int,
    amount_idr: int,
    expires_at: datetime = None
) -> TopupOrder:
    """Membuat record TopupOrder baru untuk deposit QRIS."""
    try:
        topup = TopupOrder(
            topup_id=topup_id,
            telegram_id=telegram_id,
            amount_idr=amount_idr,
            status="PENDING",
            created_at=datetime.utcnow(),
            expires_at=expires_at
        )
        db.add(topup)
        db.commit()
        db.refresh(topup)
        logger.info(f"TopupOrder {topup_id} created for user {telegram_id} ({amount_idr} IDR)")
        return topup
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal membuat TopupOrder {topup_id}: {e}")
        raise


def get_topup_order_by_id(db: Session, topup_id: str) -> Optional[TopupOrder]:
    """Mengambil data TopupOrder berdasarkan topup_id."""
    return db.query(TopupOrder).filter(TopupOrder.topup_id == topup_id).first()


def update_topup_status(db: Session, topup_id: str, status: str, paid_at: datetime = None) -> Optional[TopupOrder]:
    """Ubah topup yang masih PENDING agar aksi stale tidak menimpa pembayaran."""
    from sqlalchemy import update

    try:
        values = {"status": status}
        if paid_at:
            values["paid_at"] = paid_at
        result = db.execute(
            update(TopupOrder)
            .where(TopupOrder.topup_id == topup_id, TopupOrder.status == "PENDING")
            .values(**values)
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        logger.info(f"TopupOrder {topup_id} status updated -> {status}")
        return db.query(TopupOrder).filter(TopupOrder.topup_id == topup_id).first()
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal update status TopupOrder {topup_id}: {e}")
        raise


def expire_topup_if_pending(db: Session, topup_id: str, now: datetime = None) -> bool:
    """Expire topup hanya jika masih PENDING dan batas waktunya benar-benar lewat."""
    from sqlalchemy import update

    now = now or datetime.utcnow()
    try:
        result = db.execute(
            update(TopupOrder)
            .where(
                TopupOrder.topup_id == topup_id,
                TopupOrder.status == "PENDING",
                TopupOrder.expires_at.isnot(None),
                TopupOrder.expires_at <= now,
            )
            .values(status="EXPIRED")
        )
        db.commit()
        return result.rowcount == 1
    except Exception:
        db.rollback()
        logger.exception("Gagal expire topup %s", topup_id)
        raise


def get_pending_topup_orders(db: Session) -> list[TopupOrder]:
    """Mengambil seluruh TopupOrder dengan status PENDING."""
    return db.query(TopupOrder).filter(TopupOrder.status == "PENDING").all()


def get_pending_gopay_orders(db: Session) -> list[Order]:
    """Mengambil semua Order Beli via GoPay QRIS yang masih berstatus pending."""
    return (
        db.query(Order)
        .filter(
            Order.payment_method == "GOPAY_QRIS",
            Order.status == "pending",
        )
        .all()
    )


def get_pending_gopay_order_for_user(db: Session, telegram_id: int) -> Optional[Order]:
    """Order Beli GoPay QRIS pending terbaru milik seorang user (untuk klaim bukti transfer)."""
    return (
        db.query(Order)
        .filter(
            Order.payment_method == "GOPAY_QRIS",
            Order.status == "pending",
            Order.telegram_id == telegram_id,
        )
        .order_by(Order.created_at.desc())
        .first()
    )


# ============================================================
# GOPAY SESSION CRUD (PostgreSQL & Multi-Deploy Persistence)
# ============================================================

def get_gopay_session(db: Session, key: str = "active_session") -> Optional[dict]:
    """Mengambil session data GoPay dari database."""
    import json
    try:
        row = db.query(GopaySession).filter(GopaySession.key == key).first()
        if row and row.session_data:
            return json.loads(row.session_data)
        return None
    except Exception as e:
        logger.warning(f"Gagal get_gopay_session dari DB: {e}")
        return None


def save_gopay_session(db: Session, session_data: dict, key: str = "active_session") -> bool:
    """Menyimpan atau memperbarui session data GoPay di database."""
    import json
    try:
        data_str = json.dumps(session_data)
        row = db.query(GopaySession).filter(GopaySession.key == key).first()
        if not row:
            row = GopaySession(key=key, session_data=data_str)
            db.add(row)
        else:
            row.session_data = data_str
            row.updated_at = datetime.utcnow()
        db.commit()
        logger.info(f"Sesi GoPay berhasil disimpan ke database (key={key}).")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal save_gopay_session ke DB: {e}")
        return False


def sync_gopay_session_file(session_file_path: str = None) -> bool:
    """
    Sinkronisasi dua arah file sesi .GOPAY_SESI_JANGAN_DIHAPUS.json dengan database:
    1. Jika file lokal tidak ada / kosong, muat dari database PostgreSQL dan tulis file.
    2. Jika file lokal ada dan valid, pastikan database selalu ter-update dengan isi file tersebut.
    """
    import os
    import json
    from database.connection import SessionLocal

    if not session_file_path:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        session_file_path = os.path.join(base_dir, "gopay-gateway", ".GOPAY_SESI_JANGAN_DIHAPUS.json")

    db = SessionLocal()
    try:
        file_exists = os.path.exists(session_file_path)
        file_session = None
        if file_exists:
            try:
                with open(session_file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        file_session = json.loads(content)
            except Exception as fe:
                logger.warning(f"Error membaca file sesi lokal: {fe}")

        db_session = get_gopay_session(db)

        if not file_session and db_session:
            # File kosong / hilang setelah redeploy container -> Pulihkan dari DB!
            os.makedirs(os.path.dirname(session_file_path), exist_ok=True)
            with open(session_file_path, "w", encoding="utf-8") as f:
                json.dump(db_session, f, indent=2)
            logger.info("🔑 [GOPAY] Berhasil memulihkan sesi GoPay dari database PostgreSQL ke file lokal.")
            return True

        elif file_session:
            # File lokal ada -> Pastikan DB selalu sinkron
            if not db_session or (file_session.get("updated_at") != db_session.get("updated_at")):
                save_gopay_session(db, file_session)
                logger.info("💾 [GOPAY] Sesi lokal tersimpan & tersinkronisasi ke database PostgreSQL.")
            return True

        return False
    except Exception as exc:
        logger.error(f"Gagal sync_gopay_session_file: {exc}")
        return False
    finally:
        db.close()


def get_recent_audit_logs(db: Session, limit: int = 15) -> list:
    """Mengambil log audit sistem terbaru."""
    return db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).all()


def get_pending_orders_count(db: Session) -> int:
    """Menghitung total order yang berstatus pending/menunggu review."""
    return db.query(Order).filter(
        Order.status.in_(["pending", "paid", "payout_processing", "manual_review", "WAITING_CRYPTO_DEPOSIT", "PAYOUT_QUEUED"])
    ).count()


# ============================================================
# BROADCAST SEGMENT HELPERS
# ============================================================

def get_users_by_segment(db: Session, segment: str) -> list:
    """Return list User berdasarkan segment broadcast.

    Segments:
        all     — semua user non-banned
        active  — user yang punya order dalam 30 hari terakhir
        buyers  — user yang punya minimal 1 order COMPLETED
        balance — user yang punya saldo > 0
    """
    from sqlalchemy import or_
    query = db.query(User).filter(or_(User.is_banned == False, User.is_banned.is_(None)))  # noqa: E712

    if segment == "active":
        cutoff = datetime.utcnow() - timedelta(days=30)
        active_ids = (
            db.query(Order.telegram_id)
            .filter(Order.created_at >= cutoff)
            .distinct()
        )
        query = query.filter(User.telegram_id.in_(active_ids.scalar_subquery()))
    elif segment == "buyers":
        buyer_ids = (
            db.query(Order.telegram_id)
            .filter(func.lower(Order.status) == "completed")
            .distinct()
        )
        query = query.filter(User.telegram_id.in_(buyer_ids.scalar_subquery()))
    elif segment == "balance":
        query = query.filter(User.balance_idr > 0)
    # else: "all" — semua non-banned user

    return query.all()


def get_segment_count(db: Session, segment: str) -> int:
    """Return jumlah user pada segment tertentu."""
    return len(get_users_by_segment(db, segment))


# ============================================================
# REFERRAL CRUD
# ============================================================

def create_referral(db: Session, referrer_id: int, referee_id: int):
    """Buat record referral baru. Return Referral atau None jika sudah ada."""
    from database.models import Referral, ReferralConfig

    # Cek apakah referral sudah ada untuk referee ini
    existing = db.query(Referral).filter(Referral.referee_id == referee_id).first()
    if existing:
        return None

    # Cek apakah referral enabled
    enabled = db.query(ReferralConfig).filter(
        ReferralConfig.key == "referral_enabled"
    ).first()
    if enabled and enabled.value == "false":
        return None

    # Cek max referrals per user
    max_cfg = db.query(ReferralConfig).filter(
        ReferralConfig.key == "max_referrals_per_user"
    ).first()
    max_referrals = int(max_cfg.value) if max_cfg else 100
    current_count = db.query(Referral).filter(Referral.referrer_id == referrer_id).count()
    if current_count >= max_referrals:
        logger.info(f"Referrer {referrer_id} sudah mencapai batas {max_referrals} referral.")
        return None

    # Get reward amount
    reward_cfg = db.query(ReferralConfig).filter(
        ReferralConfig.key == "reward_per_referral"
    ).first()
    reward_idr = int(reward_cfg.value) if reward_cfg else 5000

    ref = Referral(
        referrer_id=referrer_id,
        referee_id=referee_id,
        status="PENDING",
        reward_idr=reward_idr,
    )
    db.add(ref)
    db.commit()
    db.refresh(ref)
    logger.info(f"Referral created: {referrer_id} → {referee_id} (reward Rp {reward_idr:,})")
    return ref


def get_referral_by_referee(db: Session, referee_id: int):
    """Ambil record referral berdasarkan referee_id."""
    from database.models import Referral
    return db.query(Referral).filter(Referral.referee_id == referee_id).first()


def complete_referral(db: Session, referee_id: int, trade_amount_idr: float = 0.0,
                      fee_idr: Optional[float] = None) -> bool:
    """Mark referral COMPLETED dan credit reward ke referrer serta potongan/bonus ke referee.

    Dipanggil saat referee menyelesaikan transaksi (detector, payout watchdog, dan
    update_order_status bisa memanggil bersamaan). Status diklaim atomik
    (PENDING -> COMPLETED dalam satu UPDATE) sebelum kredit, jadi reward hanya sekali.

    Guard fee (default aktif, config `referral_fee_guard`): reward + bonus hanya dibayar
    bila fee transaksi itu menutupinya — akun palsu yang belanja minimum tidak bisa
    menjadi sumber untung. Referral tetap PENDING sampai ada transaksi yang memenuhi.
    """
    from database.models import Referral, AuditLog

    ref = db.query(Referral).filter(
        Referral.referee_id == referee_id,
        Referral.status == "PENDING",
    ).first()

    if not ref:
        return False

    # Verifikasi syarat minimal transaksi (bila diatur oleh admin)
    min_trade_cfg = get_referral_config(db, "min_trade_amount_idr")
    min_trade = float(min_trade_cfg) if min_trade_cfg else 0.0
    if min_trade > 0 and trade_amount_idr < min_trade:
        logger.info(
            f"Referral pending for referee {referee_id}: trade amount Rp {trade_amount_idr:,.0f} < minimum requirement Rp {min_trade:,.0f}"
        )
        return False

    reward = int(ref.reward_idr or 0)
    bonus_cfg = get_referral_config(db, "referee_discount_idr")
    bonus_idr = int(bonus_cfg) if bonus_cfg else 0
    guard_on = (get_referral_config(db, "referral_fee_guard") or "true").lower() != "false"
    if guard_on and fee_idr is not None and float(fee_idr) < reward + bonus_idr:
        logger.info(
            f"Referral pending for referee {referee_id}: fee Rp {float(fee_idr):,.0f} "
            f"< reward+bonus Rp {reward + bonus_idr:,}"
        )
        return False

    try:
        claimed = db.query(Referral).filter(
            Referral.id == ref.id, Referral.status == "PENDING",
        ).update({"status": "COMPLETED", "completed_at": datetime.utcnow()}, synchronize_session=False)
        db.commit()
        db.refresh(ref)
        if claimed != 1:
            return False  # pemanggil lain sudah menyelesaikan referral ini
    except Exception as e:
        db.rollback()
        logger.error(f"Error claiming referral for {referee_id}: {e}")
        return False

    # Status sudah COMPLETED: kegagalan kredit di bawah dicatat untuk admin, tidak diulang otomatis.
    try:
        if reward > 0:
            credit_user_balance(db, ref.referrer_id, float(reward))
            db.add(AuditLog(
                telegram_id=ref.referrer_id,
                action="REFERRAL_REWARD",
                details=f"Reward referral dari transaksi user {referee_id}: +Rp {reward:,}",
            ))
        if bonus_idr > 0:
            credit_user_balance(db, referee_id, float(bonus_idr))
            db.add(AuditLog(
                telegram_id=referee_id,
                action="REFERRAL_BONUS",
                details=f"Bonus/Potongan transaksi pertama referral (diajak oleh {ref.referrer_id}): +Rp {bonus_idr:,}",
            ))
        db.commit()
        logger.info(
            f"Referral completed: {ref.referrer_id} ← {referee_id}. "
            f"Reward Rp {reward:,} (referrer), Bonus Rp {bonus_idr:,} (referee) credited."
        )
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Referral {referee_id} COMPLETED tetapi kredit gagal — perlu cek admin: {e}")
        try:
            db.add(AuditLog(telegram_id=ref.referrer_id, action="REFERRAL_CREDIT_FAILED",
                            details=f"Referee {referee_id}: reward Rp {reward:,} / bonus Rp {bonus_idr:,} gagal dikredit: {e}"))
            db.commit()
        except Exception:
            db.rollback()
        return False


def get_referral_stats(db: Session, referrer_id: int) -> dict:
    """Ambil statistik referral untuk seorang referrer."""
    from database.models import Referral

    referrals = db.query(Referral).filter(Referral.referrer_id == referrer_id).all()
    total = len(referrals)
    completed = sum(1 for r in referrals if r.status == "COMPLETED")
    pending = sum(1 for r in referrals if r.status == "PENDING")
    total_reward = sum(r.reward_idr or 0 for r in referrals if r.status == "COMPLETED")

    return {
        "total": total,
        "completed": completed,
        "pending": pending,
        "total_reward": total_reward,
    }


def get_top_referrers(db: Session, limit: int = 10) -> list:
    """Ambil leaderboard top referrers."""
    from database.models import Referral
    from sqlalchemy import Integer as SA_Integer

    results = (
        db.query(
            Referral.referrer_id,
            func.count(Referral.id).label("total"),
            func.sum(
                func.cast(Referral.reward_idr, SA_Integer)
            ).label("total_reward"),
        )
        .filter(Referral.status == "COMPLETED")
        .group_by(Referral.referrer_id)
        .order_by(func.count(Referral.id).desc())
        .limit(limit)
        .all()
    )
    return results


def get_referral_config(db: Session, key: str) -> Optional[str]:
    """Ambil nilai konfigurasi referral."""
    from database.models import ReferralConfig
    row = db.query(ReferralConfig).filter(ReferralConfig.key == key).first()
    return row.value if row else None


def set_referral_config(db: Session, key: str, value: str) -> None:
    """Set konfigurasi referral. Insert atau update."""
    from database.models import ReferralConfig
    row = db.query(ReferralConfig).filter(ReferralConfig.key == key).first()
    if row:
        row.value = value
    else:
        db.add(ReferralConfig(key=key, value=value))
    db.commit()
    logger.info(f"Referral config set: {key} = {value}")


# ============================================================
# USER SAVED WALLETS & BANKS CRUD
# ============================================================

def get_user_saved_wallets(db: Session, telegram_id: int, network: str = None) -> list:
    """Ambil daftar alamat wallet tersimpan milik user, bisa difilter per network."""
    from database.models import UserSavedWallet
    
    wallets = (
        db.query(UserSavedWallet)
        .filter(UserSavedWallet.telegram_id == telegram_id)
        .order_by(UserSavedWallet.updated_at.desc())
        .all()
    )
    if not network:
        return wallets

    from bot.utils.validator import validate_wallet_address
    net_upper = network.upper()
    matched = []
    for w in wallets:
        if w.network and w.network.upper() == net_upper:
            matched.append(w)
        elif not w.network or w.network.upper() in ["ALL", "EVM"]:
            if validate_wallet_address(w.wallet_address, net_upper):
                matched.append(w)
        else:
            if validate_wallet_address(w.wallet_address, net_upper):
                matched.append(w)
    return matched


def get_saved_wallet_by_id(db: Session, wallet_id: int, telegram_id: int = None):
    """Ambil satu wallet tersimpan berdasarkan ID."""
    from database.models import UserSavedWallet
    query = db.query(UserSavedWallet).filter(UserSavedWallet.id == wallet_id)
    if telegram_id is not None:
        query = query.filter(UserSavedWallet.telegram_id == telegram_id)
    return query.first()


# Alamat terkunci ke satu user HANYA setelah transaksi sukses (keputusan client).
# Menyimpan alamat di profil tidak mengunci — kalau mengunci, siapa pun bisa
# "menyimpan" alamat orang lain duluan dan memblokir pemilik aslinya.
_ADDRESS_LOCK_ORDER_TYPES = ("buy", "swap")


def _is_hex_address(addr: str) -> bool:
    """0x + hex (EVM 40, Sui/Aptos 64): hex tidak case-sensitive."""
    body = addr[2:]
    return addr[:2].lower() == "0x" and bool(body) and all(c in "0123456789abcdefABCDEF" for c in body)


def _normalize_wallet_address(wallet_address: str) -> str:
    """Strip spasi; alamat hex (EVM/Sui/Aptos) dibandingkan case-insensitive."""
    addr = (wallet_address or "").strip()
    return addr.lower() if _is_hex_address(addr) else addr


def is_wallet_address_taken_by_other(db: Session, wallet_address: str, current_telegram_id: int) -> bool:
    """True bila alamat sudah terkunci ke user LAIN lewat transaksi sukses (anti-fraud binding)."""
    addr = _normalize_wallet_address(wallet_address)
    if not addr:
        return False
    order_col = func.lower(Order.buyer_wallet) if _is_hex_address(addr) else Order.buyer_wallet
    return (
        db.query(Order.id)
        .filter(
            order_col == addr,
            Order.telegram_id != current_telegram_id,
            func.lower(Order.order_type).in_(_ADDRESS_LOCK_ORDER_TYPES),
            func.lower(Order.status) == "completed",
        )
        .first()
        is not None
    )


def normalize_account_number(account_number: str) -> str:
    """Nomor rekening / HP e-wallet: hanya huruf-angka, huruf besar (tanpa spasi/strip)."""
    return "".join(c for c in (account_number or "") if c.isalnum()).upper()


def account_number_keys(text: str) -> set[str]:
    """Semua 'nomor' (>= 6 digit) yang terbaca dari teks bebas, dinormalkan untuk pembandingan.

    Menangani spasi / titik / strip ("0812.3456.7890", "1234 5678 90"), awalan "+62"/"62"
    nomor HP (disamakan ke "0…"), dan teks tanpa koma ("1234567890 BCA Budi").
    """
    import re
    keys = set()
    for chunk in re.findall(r"\+?\d[\d .\-]{4,}\d", text or ""):
        digits = re.sub(r"\D", "", chunk)
        if digits.startswith("62") and len(digits) >= 10:
            digits = "0" + digits[2:]
        if len(digits) >= 6:
            keys.add(digits)
    return keys


def _bank_field_of(buyer_wallet: str) -> str:
    """Kolom nomor dari info bank order Jual "BANK | NOMOR | NAMA".

    Hanya kolom ke-2 yang dipakai: karakter "|" yang disisipkan di nama bank / pemilik
    tidak bisa membuat nomor orang lain tampak milik user tsb (poisoning).
    """
    parts = (buyer_wallet or "").split("|")
    return parts[1] if len(parts) >= 3 else (buyer_wallet or "")


def is_bank_account_taken_by_other(db: Session, account_number: str, current_telegram_id: int) -> bool:
    """True bila rekening/e-wallet sudah terkunci ke user LAIN lewat penjualan yang sukses.

    Aturan sama dengan alamat wallet (keputusan client): terkunci hanya setelah transaksi
    sukses. Dicocokkan per nomor (bukan nama bank, yang bisa ditulis macam-macam), memakai
    pembacaan angka yang toleran format (lihat account_number_keys).
    """
    wanted = account_number_keys(account_number)
    if not wanted:
        return False
    rows = (
        db.query(Order.buyer_wallet)
        .filter(
            func.lower(Order.order_type) == "sell",
            func.lower(Order.status) == "completed",
            Order.telegram_id != current_telegram_id,
            Order.buyer_wallet.isnot(None),
        )
        .all()
    )
    return any(wanted & account_number_keys(_bank_field_of(r[0])) for r in rows)


def auto_save_order_accounts(db: Session, order) -> None:
    """Setelah transaksi SUKSES: simpan otomatis alamat wallet (Beli/Convert) atau
    rekening pencairan (Jual) ke profil user. Idempoten; tidak pernah menggagalkan penyelesaian order."""
    try:
        otype = (order.order_type or "").lower()
        tid = order.telegram_id
        target = (order.buyer_wallet or "").strip()
        if not tid or not target:
            return
        if otype == "buy":
            save_user_wallet(db, tid, target, network=order.network)
        elif otype == "swap":
            save_user_wallet(db, tid, target, network=order.target_network)
        elif otype == "sell":
            parts = [p.strip() for p in target.split("|")]
            if len(parts) >= 3 and normalize_account_number(parts[1]):
                save_user_bank(db, tid, parts[0], parts[1], " ".join(parts[2:]))
    except Exception as exc:  # jangan ganggu penyelesaian order
        db.rollback()
        logger.warning("Auto-save wallet/rekening order %s gagal: %s", getattr(order, "order_id", "?"), exc)


def save_user_wallet(
    db: Session,
    telegram_id: int,
    wallet_address: str,
    network: str = None,
    label: str = None,
):
    """Simpan atau update alamat wallet tersimpan milik user."""
    from database.models import UserSavedWallet

    clean_addr = wallet_address.strip()
    existing = (
        db.query(UserSavedWallet)
        .filter(
            UserSavedWallet.telegram_id == telegram_id,
            UserSavedWallet.wallet_address == clean_addr,
        )
        .first()
    )
    if existing:
        if network:
            existing.network = network.upper()
        if label:
            existing.label = label
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        return existing

    new_wallet = UserSavedWallet(
        telegram_id=telegram_id,
        wallet_address=clean_addr,
        network=network.upper() if network else None,
        label=label,
    )
    db.add(new_wallet)
    db.commit()
    db.refresh(new_wallet)
    return new_wallet


def delete_user_saved_wallet(db: Session, wallet_id: int, telegram_id: int) -> bool:
    """Hapus alamat wallet tersimpan milik user."""
    from database.models import UserSavedWallet

    w = (
        db.query(UserSavedWallet)
        .filter(
            UserSavedWallet.id == wallet_id,
            UserSavedWallet.telegram_id == telegram_id,
        )
        .first()
    )
    if w:
        db.delete(w)
        db.commit()
        return True
    return False


EWALLET_IDENTIFIERS = {"GOPAY", "OVO", "DANA", "SHOPEEPAY", "LINKAJA", "ISAKU"}

def detect_account_type(bank_name: str) -> str:
    cleaned = bank_name.strip().upper().replace(" ", "").replace("-", "")
    for ew in EWALLET_IDENTIFIERS:
        if ew in cleaned:
            return "EWALLET"
    return "BANK"


def get_user_saved_banks(db: Session, telegram_id: int) -> list:
    """Ambil semua rekening bank & e-wallet tersimpan milik user."""
    from database.models import UserSavedBank

    return (
        db.query(UserSavedBank)
        .filter(UserSavedBank.telegram_id == telegram_id)
        .order_by(UserSavedBank.updated_at.desc())
        .all()
    )


def get_saved_bank_by_id(db: Session, bank_id: int, telegram_id: int = None):
    """Ambil satu rekening tersimpan berdasarkan ID."""
    from database.models import UserSavedBank

    query = db.query(UserSavedBank).filter(UserSavedBank.id == bank_id)
    if telegram_id is not None:
        query = query.filter(UserSavedBank.telegram_id == telegram_id)
    return query.first()


def save_user_bank(
    db: Session,
    telegram_id: int,
    bank_name: str,
    account_number: str,
    account_name: str,
    account_type: str = None,
):
    """Simpan atau update rekening bank / e-wallet pencairan user."""
    from database.models import UserSavedBank

    clean_bank = bank_name.strip().upper()
    clean_num = "".join(c for c in account_number if c.isdigit() or c.isalnum()).strip()
    clean_name = account_name.strip().upper()

    if not account_type:
        account_type = detect_account_type(clean_bank)

    existing = (
        db.query(UserSavedBank)
        .filter(
            UserSavedBank.telegram_id == telegram_id,
            UserSavedBank.account_number == clean_num,
        )
        .first()
    )
    if existing:
        existing.bank_name = clean_bank
        existing.account_name = clean_name
        existing.account_type = account_type
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        return existing

    new_bank = UserSavedBank(
        telegram_id=telegram_id,
        bank_name=clean_bank,
        account_number=clean_num,
        account_name=clean_name,
        account_type=account_type,
    )
    db.add(new_bank)
    db.commit()
    db.refresh(new_bank)
    return new_bank


def delete_user_saved_bank(db: Session, bank_id: int, telegram_id: int) -> bool:
    """Hapus rekening tersimpan milik user."""
    from database.models import UserSavedBank

    b = (
        db.query(UserSavedBank)
        .filter(
            UserSavedBank.id == bank_id,
            UserSavedBank.telegram_id == telegram_id,
        )
        .first()
    )
    if b:
        db.delete(b)
        db.commit()
        return True
    return False


# ============================================================
# PHASE 7 — REFERRAL DISCOUNT CRUD
# ============================================================

def get_referral_discount(db: Session, telegram_id: int) -> Optional["ReferralDiscount"]:
    """Ambil data diskon aktif milik user. Return None jika tidak ada atau remaining_uses=0."""
    disc = db.query(ReferralDiscount).filter(ReferralDiscount.telegram_id == telegram_id).first()
    if disc and disc.remaining_uses <= 0:
        return None
    return disc


def activate_referral_discount(
    db: Session, telegram_id: int, uses: int = 10, pct: float = 10.0
) -> "ReferralDiscount":
    """
    Aktifkan atau reset diskon referral untuk user.
    Jika sudah ada, reset remaining_uses ke nilai baru.
    """
    disc = db.query(ReferralDiscount).filter(ReferralDiscount.telegram_id == telegram_id).first()
    if disc:
        disc.remaining_uses = uses
        disc.discount_pct = Decimal(str(pct))
        disc.activated_at = datetime.utcnow()
        disc.updated_at = datetime.utcnow()
    else:
        disc = ReferralDiscount(
            telegram_id=telegram_id,
            remaining_uses=uses,
            discount_pct=Decimal(str(pct)),
        )
        db.add(disc)
    db.commit()
    db.refresh(disc)
    logger.info(f"Referral discount aktif untuk {telegram_id}: {uses}x {pct}%")
    return disc


def consume_referral_discount(db: Session, telegram_id: int) -> Optional[float]:
    """
    Gunakan 1 slot diskon. Kurangi remaining_uses.
    Return: nilai diskon (%) jika berhasil, None jika tidak ada diskon aktif.
    """
    disc = get_referral_discount(db, telegram_id)
    if not disc:
        return None
    pct = float(disc.discount_pct)
    disc.remaining_uses -= 1
    disc.updated_at = datetime.utcnow()
    db.commit()
    logger.info(f"Diskon referral dikonsumsi oleh {telegram_id}: sisa {disc.remaining_uses}")
    return pct


def get_referral_discount_info(db: Session, telegram_id: int) -> dict:
    """
    Informasi diskon referral user.
    Return: {active: bool, remaining: int, discount_pct: float}
    """
    disc = db.query(ReferralDiscount).filter(ReferralDiscount.telegram_id == telegram_id).first()
    if not disc or disc.remaining_uses <= 0:
        return {"active": False, "remaining": 0, "discount_pct": 0.0}
    return {
        "active": True,
        "remaining": disc.remaining_uses,
        "discount_pct": float(disc.discount_pct),
    }


# ============================================================
# PHASE 7 — LOYALTY REWARD CRUD
# ============================================================

DEFAULT_LOYALTY_CONFIG = {
    "loyalty_enabled": "true",
    "window_days": "5",
    "min_tx_count": "5",
    "reward_amount_idr": "25000",
    "min_tx_amount_idr": "50000",
}


def get_loyalty_config(db: Session, key: str) -> Optional[str]:
    """Ambil satu nilai konfigurasi loyalty. Return None jika tidak ada."""
    cfg = db.query(LoyaltyConfig).filter(LoyaltyConfig.key == key).first()
    if cfg:
        return cfg.value
    return DEFAULT_LOYALTY_CONFIG.get(key)


def set_loyalty_config(db: Session, key: str, value: str) -> None:
    """Simpan atau update nilai konfigurasi loyalty."""
    cfg = db.query(LoyaltyConfig).filter(LoyaltyConfig.key == key).first()
    if cfg:
        cfg.value = value
        cfg.updated_at = datetime.utcnow()
    else:
        cfg = LoyaltyConfig(key=key, value=value)
        db.add(cfg)
    db.commit()


def get_or_create_loyalty_window(
    db: Session, telegram_id: int, window_days: int = 5
) -> "LoyaltyReward":
    """
    Ambil loyalty window yang aktif (window_end >= now).
    Jika tidak ada atau sudah expired, buat window baru untuk siklus berikutnya.
    """
    now = datetime.utcnow()
    # Cari window aktif: window_end di masa depan
    active = (
        db.query(LoyaltyReward)
        .filter(
            LoyaltyReward.telegram_id == telegram_id,
            LoyaltyReward.window_end >= now,
        )
        .order_by(LoyaltyReward.window_start.desc())
        .first()
    )
    if active:
        return active

    # Buat window baru
    window_start = now
    window_end = now + timedelta(days=window_days)
    new_window = LoyaltyReward(
        telegram_id=telegram_id,
        window_start=window_start,
        window_end=window_end,
        tx_count_in_window=0,
        qualified=False,
    )
    db.add(new_window)
    db.commit()
    db.refresh(new_window)
    logger.info(f"Loyalty window baru dibuat untuk {telegram_id}: {window_start} - {window_end}")
    return new_window


def increment_loyalty_tx(
    db: Session, telegram_id: int, tx_amount_idr: int = 0
) -> dict:
    """
    Tambah 1 hitungan transaksi ke loyalty window aktif.
    Validasi minimum nominal transaksi (dari config).
    Return: {qualified: bool, current_count: int, needed: int, window_end: datetime, already_rewarded: bool}
    """
    window_days = int(get_loyalty_config(db, "window_days") or 5)
    min_tx_count = int(get_loyalty_config(db, "min_tx_count") or 5)
    min_tx_amount = int(get_loyalty_config(db, "min_tx_amount_idr") or 0)

    # Validasi minimum nominal per transaksi
    if tx_amount_idr < min_tx_amount:
        logger.info(f"Loyalty: transaksi {tx_amount_idr} < min {min_tx_amount}, tidak dihitung.")
        window = get_or_create_loyalty_window(db, telegram_id, window_days)
        return {
            "qualified": window.qualified,
            "current_count": window.tx_count_in_window,
            "needed": max(0, min_tx_count - window.tx_count_in_window),
            "window_end": window.window_end,
            "already_rewarded": window.rewarded_at is not None,
            "skipped": True,
        }

    window = get_or_create_loyalty_window(db, telegram_id, window_days)

    if window.rewarded_at is not None:
        # Sudah direward, tidak perlu increment
        return {
            "qualified": True,
            "current_count": window.tx_count_in_window,
            "needed": 0,
            "window_end": window.window_end,
            "already_rewarded": True,
        }

    window.tx_count_in_window += 1
    newly_qualified = False
    if not window.qualified and window.tx_count_in_window >= min_tx_count:
        window.qualified = True
        newly_qualified = True

    db.commit()
    db.refresh(window)
    logger.info(
        f"Loyalty tx increment untuk {telegram_id}: "
        f"{window.tx_count_in_window}/{min_tx_count} (qualified={window.qualified})"
    )
    return {
        "qualified": window.qualified,
        "newly_qualified": newly_qualified,
        "current_count": window.tx_count_in_window,
        "needed": max(0, min_tx_count - window.tx_count_in_window),
        "window_end": window.window_end,
        "already_rewarded": False,
        "window_id": window.id,
    }


def mark_loyalty_rewarded(
    db: Session, window_id: int, reward_idr: int
) -> bool:
    """Tandai window loyalty sebagai sudah direward dan catat jumlahnya."""
    window = db.query(LoyaltyReward).filter(LoyaltyReward.id == window_id).first()
    if not window:
        return False
    window.reward_idr = reward_idr
    window.rewarded_at = datetime.utcnow()
    db.commit()
    return True


def get_loyalty_eligible_users(db: Session) -> list[dict]:
    """
    Ambil semua loyalty windows yang qualified tapi belum di-reward.
    Berguna untuk job scheduler periodik.
    """
    qualified = (
        db.query(LoyaltyReward)
        .filter(
            LoyaltyReward.qualified == True,  # noqa: E712
            LoyaltyReward.rewarded_at.is_(None),
        )
        .all()
    )
    result = []
    for w in qualified:
        result.append({
            "window_id": w.id,
            "telegram_id": w.telegram_id,
            "tx_count": w.tx_count_in_window,
            "window_start": w.window_start,
            "window_end": w.window_end,
        })
    return result


# ============================================================
# PHASE 7 — TOP SPENDER CRUD
# ============================================================

def get_milestone_excluded_ids(db: Session) -> set[int]:
    """ID user yang dikecualikan dari peringkat Top Milestone (tetap bebas bertransaksi)."""
    from database.models import MilestoneExclusion
    return {row[0] for row in db.query(MilestoneExclusion.telegram_id).all()}


def list_milestone_exclusions(db: Session) -> list:
    from database.models import MilestoneExclusion
    return db.query(MilestoneExclusion).order_by(MilestoneExclusion.created_at.desc()).all()


def add_milestone_exclusion(db: Session, telegram_id: int, note: Optional[str] = None,
                            created_by: Optional[int] = None) -> bool:
    """Tambah pengecualian. True bila baru; False bila sudah ada (catatan diperbarui)."""
    from database.models import MilestoneExclusion
    existing = db.query(MilestoneExclusion).filter(MilestoneExclusion.telegram_id == telegram_id).first()
    if existing:
        if note:
            existing.note = note[:200]
            db.commit()
        return False
    db.add(MilestoneExclusion(telegram_id=telegram_id, note=(note or None) and note[:200], created_by=created_by))
    db.add(AuditLog(telegram_id=telegram_id, action="MILESTONE_EXCLUDE_ADD",
                    details=f"User {telegram_id} dikecualikan dari Top Milestone oleh admin {created_by}. {note or ''}".strip()))
    db.commit()
    return True


def remove_milestone_exclusion(db: Session, telegram_id: int, removed_by: Optional[int] = None) -> bool:
    from database.models import MilestoneExclusion
    deleted = db.query(MilestoneExclusion).filter(MilestoneExclusion.telegram_id == telegram_id).delete()
    if deleted:
        db.add(AuditLog(telegram_id=telegram_id, action="MILESTONE_EXCLUDE_REMOVE",
                        details=f"Pengecualian Top Milestone user {telegram_id} dicabut oleh admin {removed_by}."))
    db.commit()
    return bool(deleted)


# Volume Top Milestone = akumulasi SEMUA transaksi selesai: beli + jual + convert (keputusan client).
MILESTONE_ORDER_TYPES = ("buy", "sell", "swap")


def order_volume_idr():
    """Nilai satu order untuk volume milestone: total_idr (dasar yang sama dengan plan Phase 7).

    Catatan: total_idr beli = bayar (termasuk pajak QRIS + kode unik), jual = bersih setelah
    fee, convert = nilai IDR. Selisihnya kecil (<~3%); dasar hitung sengaja tidak diubah.
    Cukup ganti di sini bila client ingin nilai bruto (nominal_idr).
    """
    return Order.total_idr


def get_top_spenders(
    db: Session, limit: int = 10, period_days: Optional[int] = 30
) -> list[dict]:
    """
    Ambil top N spender berdasarkan total nominal transaksi COMPLETED dalam periode tertentu.
    Hanya user nyata yang menyelesaikan >= 1 transaksi, dan bukan user yang dikecualikan
    dari milestone (pengecualian dibuang SEBELUM limit, jadi daftar tetap terisi top N).
    Return: [{rank, telegram_id, username, full_name, total_spent_idr, tx_count}, ...]
    """
    filters = [
        func.lower(Order.status) == "completed",
        func.lower(Order.order_type).in_(MILESTONE_ORDER_TYPES),
        or_(User.is_banned == False, User.is_banned.is_(None)),  # noqa: E712
    ]
    excluded = get_milestone_excluded_ids(db)
    if excluded:
        filters.append(Order.telegram_id.notin_(excluded))
    if period_days and period_days > 0:
        cutoff = datetime.utcnow() - timedelta(days=period_days)
        filters.append(Order.created_at >= cutoff)

    rows = (
        db.query(
            Order.telegram_id,
            User.username,
            User.full_name,
            func.sum(order_volume_idr()).label("total_spent"),
            func.count(Order.id).label("tx_count"),
        )
        .join(User, User.telegram_id == Order.telegram_id)
        .filter(*filters)
        .group_by(Order.telegram_id, User.username, User.full_name)
        .order_by(func.sum(order_volume_idr()).desc(), Order.telegram_id.asc())  # tie-break deterministik
        .limit(limit)
        .all()
    )
    result = []
    for rank, row in enumerate(rows, start=1):
        result.append({
            "rank": rank,
            "telegram_id": row[0],
            "username": row[1] or f"User_{row[0]}",
            "full_name": row[2] or "",
            "total_spent_idr": int(row[3] or 0),
            "tx_count": int(row[4] or 0),
        })
    return result


# ============================================================
# PHASE 7 — RANDOM WINNER CRUD
# ============================================================

def get_random_winners(
    db: Session, pool_segment: str, count: int, min_tx_amount: int = 0
) -> list[dict]:
    """
    Pilih N pemenang random dari pool berdasarkan segmen.
    pool_segment: 'ALL' | 'BUYERS' | 'ACTIVE_30D'
    Return: [{telegram_id, username, full_name}, ...]
    """
    import secrets as _secrets

    segment = pool_segment.upper()
    query = db.query(User).filter(
        or_(User.is_banned == False, User.is_banned.is_(None))  # noqa: E712
    )

    if segment == "BUYERS":
        # User yang pernah punya order COMPLETED
        buyer_ids = (
            db.query(Order.telegram_id)
            .filter(func.lower(Order.status) == "completed")
            .distinct()
            .subquery()
        )
        query = query.filter(User.telegram_id.in_(buyer_ids))

    elif segment == "ACTIVE_30D":
        cutoff = datetime.utcnow() - timedelta(days=30)
        active_ids = (
            db.query(Order.telegram_id)
            .filter(
                func.lower(Order.status) == "completed",
                Order.created_at >= cutoff,
            )
            .distinct()
            .subquery()
        )
        query = query.filter(User.telegram_id.in_(active_ids))

    # else ALL: tidak ada filter tambahan

    pool = query.all()

    if not pool:
        return []

    # Filter minimal transaksi jika diperlukan
    if min_tx_amount > 0:
        pool = [u for u in pool if (u.total_spent_idr or 0) >= min_tx_amount]

    # Pilih N random tanpa duplikat
    n = min(count, len(pool))
    chosen = _secrets.SystemRandom().sample(pool, n)

    return [
        {
            "telegram_id": u.telegram_id,
            "username": u.username or f"User_{u.telegram_id}",
            "full_name": u.full_name or "",
        }
        for u in chosen
    ]


# ============================================================
# PHASE 7 — ENHANCED WALLET CRUD
# ============================================================

def get_saved_wallets_grouped(db: Session, telegram_id: int) -> dict:
    """
    Ambil semua wallet tersimpan user, dikelompokkan per chain_type.
    Return: {'EVM': [...], 'SOLANA': [...], 'TRON': [...], ...}
    """
    from database.models import UserSavedWallet
    from services.wallet_detector import get_chain_type_for_network

    wallets = (
        db.query(UserSavedWallet)
        .filter(UserSavedWallet.telegram_id == telegram_id)
        .order_by(UserSavedWallet.is_default.desc(), UserSavedWallet.updated_at.desc())
        .all()
    )

    grouped: dict = {}
    for w in wallets:
        # Gunakan chain_type yang tersimpan, atau deteksi dari network
        ct = w.chain_type
        if not ct and w.network:
            ct = get_chain_type_for_network(w.network)
        if not ct:
            ct = "OTHER"
        grouped.setdefault(ct, []).append(w)

    return grouped


def set_default_wallet(db: Session, wallet_id: int, telegram_id: int) -> bool:
    """
    Set wallet tertentu sebagai default untuk chain_type-nya.
    Un-set semua wallet lain yang sama chain_type-nya.
    Return: True jika berhasil.
    """
    from database.models import UserSavedWallet

    target = (
        db.query(UserSavedWallet)
        .filter(
            UserSavedWallet.id == wallet_id,
            UserSavedWallet.telegram_id == telegram_id,
        )
        .first()
    )
    if not target:
        return False

    chain_type = target.chain_type
    # Un-set default lainnya untuk chain_type yang sama
    if chain_type:
        db.query(UserSavedWallet).filter(
            UserSavedWallet.telegram_id == telegram_id,
            UserSavedWallet.chain_type == chain_type,
            UserSavedWallet.id != wallet_id,
        ).update({"is_default": False})

    target.is_default = True
    db.commit()
    return True


def save_user_wallet_v2(
    db: Session,
    telegram_id: int,
    wallet_address: str,
    network: str = None,
    chain_type: str = None,
    label: str = None,
    auto_detected: bool = False,
):
    """
    Simpan wallet dengan informasi chain_type (Phase 7 enhanced).
    Jika alamat yang sama sudah ada, update chain_type dan network-nya.
    """
    from database.models import UserSavedWallet

    clean_addr = wallet_address.strip()
    existing = (
        db.query(UserSavedWallet)
        .filter(
            UserSavedWallet.telegram_id == telegram_id,
            UserSavedWallet.wallet_address == clean_addr,
        )
        .first()
    )
    if existing:
        if network:
            existing.network = network.upper()
        if chain_type:
            existing.chain_type = chain_type.upper()
        if label:
            existing.label = label
        if auto_detected:
            existing.auto_detected = True
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        return existing

    new_wallet = UserSavedWallet(
        telegram_id=telegram_id,
        wallet_address=clean_addr,
        network=network.upper() if network else None,
        chain_type=chain_type.upper() if chain_type else None,
        label=label,
        auto_detected=auto_detected,
    )
    db.add(new_wallet)
    db.commit()
    db.refresh(new_wallet)
    return new_wallet


# ============================================================
# BOT TREASURY & USER IDENTIFIER LOOKUP CRUD
# ============================================================

def get_user_by_identifier(db: Session, identifier: str) -> Optional[User]:
    """
    Mencari data user berdasarkan username (@username atau username)
    atau berdasarkan Telegram ID numerik.
    """
    if not identifier:
        return None
    
    clean_id = str(identifier).strip()
    if clean_id.startswith("@"):
        clean_id = clean_id[1:].strip()

    # Cek jika numerik (Telegram ID)
    if clean_id.isdigit():
        t_id = int(clean_id)
        user = db.query(User).filter(User.telegram_id == t_id).first()
        if user:
            return user

    # Cari berdasarkan username (case-insensitive)
    user_by_uname = db.query(User).filter(func.lower(User.username) == clean_id.lower()).first()
    if user_by_uname:
        return user_by_uname

    return None


def get_bot_treasury_balance(db: Session) -> int:
    """
    Mengambil total saldo kas/dompet internal bot untuk alokasi campaign & giveaway.
    Default: 0 jika belum diset.
    """
    try:
        val = get_loyalty_config(db, "bot_treasury_balance_idr")
        if val is not None and str(val).strip().isdigit():
            return int(val)
        return 0
    except Exception as exc:
        logger.warning("Gagal membaca bot treasury balance: %s", exc)
        return 0


def topup_bot_treasury(
    db: Session,
    amount_idr: int,
    admin_id: Optional[int] = None,
    note: str = "Admin Topup Kas Bot"
) -> int:
    """
    Menambahkan saldo ke kas/dompet bot untuk event campaign & giveaway.
    Mencatat transaksi ke AuditLog.
    """
    if amount_idr <= 0:
        raise ValueError("Nominal topup kas bot harus lebih besar dari 0.")

    from sqlalchemy import BigInteger, String, cast, select, update
    now = datetime.utcnow()
    try:
        dialect = db.get_bind().dialect.name
        if dialect in {"postgresql", "sqlite"}:
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            stmt = insert(LoyaltyConfig).values(
                key="bot_treasury_balance_idr", value=str(amount_idr), updated_at=now,
            ).on_conflict_do_update(
                index_elements=[LoyaltyConfig.key],
                set_={
                    "value": cast(cast(LoyaltyConfig.value, BigInteger) + int(amount_idr), String),
                    "updated_at": now,
                },
            )
            db.execute(stmt)
        else:
            result = db.execute(
                update(LoyaltyConfig)
                .where(LoyaltyConfig.key == "bot_treasury_balance_idr")
                .values(
                    value=cast(cast(LoyaltyConfig.value, BigInteger) + int(amount_idr), String),
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                db.add(LoyaltyConfig(key="bot_treasury_balance_idr", value=str(amount_idr), updated_at=now))
                db.flush()

        stored = db.execute(
            select(LoyaltyConfig.value).where(LoyaltyConfig.key == "bot_treasury_balance_idr")
        ).scalar_one()
        new_bal = int(stored)
        old_bal = new_bal - int(amount_idr)
        db.add(AuditLog(
            telegram_id=admin_id,
            action="TOPUP_BOT_TREASURY",
            details=(
                f"Topup Kas Bot: +Rp {amount_idr:,} (Saldo: Rp {old_bal:,} -> Rp {new_bal:,}) "
                f"oleh admin {admin_id}. Note: {note}"
            ),
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Gagal topup kas bot")
        raise

    logger.info("Bot Treasury Topup: +Rp %d -> New Balance: Rp %d by Admin %s", amount_idr, new_bal, admin_id)
    return new_bal


def set_bot_treasury_balance(
    db: Session,
    amount_idr: int,
    admin_id: Optional[int] = None,
    note: str = "Admin Set Kas Bot"
) -> int:
    """
    Mengatur ulang saldo kas/dompet bot ke nilai tertentu.
    """
    if amount_idr < 0:
        raise ValueError("Nominal saldo kas bot tidak boleh negatif.")

    current_bal = get_bot_treasury_balance(db)
    set_loyalty_config(db, "bot_treasury_balance_idr", str(amount_idr))

    try:
        audit = AuditLog(
            telegram_id=admin_id,
            action="SET_BOT_TREASURY",
            details=(
                f"Set Kas Bot: Rp {current_bal:,} -> Rp {amount_idr:,} "
                f"oleh admin {admin_id}. Note: {note}"
            ),
        )
        db.add(audit)
        db.commit()
    except Exception as audit_err:
        logger.warning("Gagal mencatat audit set kas bot: %s", audit_err)

    return amount_idr


def try_deduct_bot_treasury(db: Session, amount_idr: int, admin_id: Optional[int] = None,
                            note: str = "Reward", *, commit: bool = True) -> Optional[int]:
    """Potong Kas Bot HANYA bila saldo cukup. Mengembalikan saldo baru, atau None bila kurang.

    Beda dengan deduct_bot_treasury (yang diam-diam membulatkan ke 0): reward ke user
    tidak boleh terkirim kalau dananya tidak ada.
    """
    if amount_idr <= 0:
        return get_bot_treasury_balance(db)
    from sqlalchemy import update, cast, BigInteger, String, select
    balance = cast(LoyaltyConfig.value, BigInteger)
    # Satu UPDATE bersyarat (saldo >= jumlah): dua proses/tap bersamaan tidak bisa sama-sama lolos
    # lalu membelanjakan Kas Bot melebihi saldo (baca-lalu-tulis lama bisa).
    result = db.execute(
        update(LoyaltyConfig)
        .where(LoyaltyConfig.key == "bot_treasury_balance_idr", balance >= amount_idr)
        .values(value=cast(balance - amount_idr, String), updated_at=datetime.utcnow())
    )
    if commit:
        db.commit()
    if result.rowcount != 1:
        return None
    stored = db.execute(
        select(LoyaltyConfig.value).where(LoyaltyConfig.key == "bot_treasury_balance_idr")
    ).scalar_one_or_none()
    new_bal = int(stored) if stored is not None else 0
    try:
        db.add(AuditLog(telegram_id=admin_id, action="DEDUCT_BOT_TREASURY",
                        details=f"Potong Kas Bot: -Rp {amount_idr:,} (saldo baru: Rp {new_bal:,}) oleh admin {admin_id}. Note: {note}"))
        if commit:
            db.commit()
    except Exception as audit_err:
        if not commit:
            raise
        db.rollback()
        logger.warning("Gagal mencatat audit potong kas bot: %s", audit_err)
    return new_bal


def deduct_bot_treasury(
    db: Session,
    amount_idr: int,
    admin_id: Optional[int] = None,
    note: str = "Campaign Execution"
) -> int:
    """
    Memotong saldo kas/dompet bot (misal saat campaign dieksekusi).
    Tidak boleh membuat saldo menjadi negatif (di-floor ke 0).
    """
    if amount_idr <= 0:
        return get_bot_treasury_balance(db)

    current_bal = get_bot_treasury_balance(db)
    new_bal = max(0, current_bal - amount_idr)
    set_loyalty_config(db, "bot_treasury_balance_idr", str(new_bal))

    try:
        audit = AuditLog(
            telegram_id=admin_id,
            action="DEDUCT_BOT_TREASURY",
            details=(
                f"Potong Kas Bot: -Rp {amount_idr:,} (Saldo: Rp {current_bal:,} -> Rp {new_bal:,}) "
                f"oleh admin {admin_id}. Note: {note}"
            ),
        )
        db.add(audit)
        db.commit()
    except Exception as audit_err:
        logger.warning("Gagal mencatat audit potong kas bot: %s", audit_err)

    return new_bal

