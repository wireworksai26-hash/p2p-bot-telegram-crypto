"""Operasi database pesanan Telegram Stars / Premium (tabel stars_orders).

Semua perpindahan status memakai UPDATE bersyarat (claim atomik) supaya klik ganda, poller, dan admin
yang bersamaan tidak bisa membayar, menyelesaikan, atau me-refund pesanan yang sama dua kali.
"""
import logging
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from database.models import AuditLog, StarsOrder, User
from services.stars_catalog import package_price

logger = logging.getLogger(__name__)

# Pembayaran QRIS yang masuk setelah pesanan kedaluwarsa tetap dikenali selama jendela ini
# (ditandai late_payment agar admin mengecek sebelum memproses).
LATE_PAYMENT_WINDOW = timedelta(hours=2)


def new_order_id() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return f"STR-{datetime.now().strftime('%Y%m%d')}-" + "".join(secrets.choice(alphabet) for _ in range(8))


def _audit(db: Session, order: StarsOrder, action: str, from_status: str, to_status: str, details: str) -> None:
    db.add(AuditLog(telegram_id=order.telegram_id, action=action, order_id=order.order_id,
                    from_status=from_status, to_status=to_status, details=details[:1000]))


def get_order(db: Session, order_id: str) -> Optional[StarsOrder]:
    return db.query(StarsOrder).filter(StarsOrder.order_id == order_id).first()


def get_open_qris_order(db: Session, telegram_id: int) -> Optional[StarsOrder]:
    """Pesanan QRIS yang masih menunggu pembayaran (satu per user)."""
    return db.query(StarsOrder).filter(
        StarsOrder.telegram_id == telegram_id, StarsOrder.status == "pending",
        StarsOrder.payment_method == "GOPAY_QRIS",
        (StarsOrder.expired_at.is_(None)) | (StarsOrder.expired_at > datetime.utcnow()),
    ).first()


def create_qris_order(db: Session, telegram_id: int, product: str, package: int, recipient: str,
                      expire_minutes: int) -> Optional[StarsOrder]:
    """Pesanan QRIS: tagihan = harga + kode unik. None bila paket tidak ada atau kode unik habis."""
    from database.crud import generate_unique_payment_code

    price = package_price(product, package)
    if price <= 0:
        return None
    code = generate_unique_payment_code(db, base_amount=price)
    if code is None:
        return None
    now = datetime.utcnow()
    order = StarsOrder(
        order_id=new_order_id(), telegram_id=telegram_id, product=product.upper(), package=int(package),
        recipient_username=recipient, price_idr=price, unique_code=code, total_idr=price + code,
        payment_method="GOPAY_QRIS", status="pending", created_at=now,
        expired_at=now + timedelta(minutes=expire_minutes),
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def create_balance_order(db: Session, telegram_id: int, product: str, package: int, recipient: str) -> Optional[StarsOrder]:
    """Pesanan Saldo Bot: saldo dipotong dan pesanan langsung 'paid' dalam satu transaksi. None bila saldo kurang."""
    price = package_price(product, package)
    if price <= 0:
        return None
    try:
        changed = db.execute(
            update(User).where(User.telegram_id == telegram_id, func.coalesce(User.balance_idr, 0) >= price)
            .values(balance_idr=func.coalesce(User.balance_idr, 0) - price)
        )
        if changed.rowcount != 1:
            db.rollback()
            return None
        now = datetime.utcnow()
        order = StarsOrder(
            order_id=new_order_id(), telegram_id=telegram_id, product=product.upper(), package=int(package),
            recipient_username=recipient, price_idr=price, unique_code=0, total_idr=price,
            payment_method="BOT_BALANCE", status="paid", created_at=now, paid_at=now,
        )
        db.add(order)
        db.flush()
        _audit(db, order, "STARS_ORDER_PAID", "-", "paid", f"Saldo Bot Rp {price:,} {product} {package} -> @{recipient}")
        db.commit()
        db.refresh(order)
        return order
    except Exception:
        db.rollback()
        logger.exception("Gagal membuat pesanan Stars/Premium dengan Saldo Bot")
        raise


def get_pending_qris(db: Session) -> list:
    """Pesanan QRIS yang dicek poller: pending, atau baru kedaluwarsa (bayar telat masih dikenali)."""
    now = datetime.utcnow()
    return db.query(StarsOrder).filter(
        StarsOrder.payment_method == "GOPAY_QRIS",
        (StarsOrder.status == "pending")
        | ((StarsOrder.status == "expired") & (StarsOrder.expired_at >= now - LATE_PAYMENT_WINDOW)),
    ).all()


def mark_paid(db: Session, order_id: str) -> Optional[StarsOrder]:
    """pending/expired -> paid (sekali). Pesanan yang sudah kedaluwarsa ditandai late_payment."""
    try:
        order = get_order(db, order_id)
        if order is None or order.status not in ("pending", "expired"):
            return None
        late = order.status == "expired"
        changed = db.query(StarsOrder).filter(
            StarsOrder.order_id == order_id, StarsOrder.status.in_(("pending", "expired")),
        ).update({"status": "paid", "paid_at": datetime.utcnow(), "late_payment": late}, synchronize_session=False)
        if changed != 1:
            db.rollback()
            return None
        order = get_order(db, order_id)
        _audit(db, order, "STARS_ORDER_PAID", "expired" if late else "pending", "paid",
               f"QRIS Rp {int(order.total_idr):,}{' (TELAT)' if late else ''}")
        db.commit()
        db.refresh(order)
        return order
    except Exception:
        db.rollback()
        logger.exception("Gagal menandai pesanan Stars/Premium %s dibayar", order_id)
        raise


def cancel_pending(db: Session, order_id: str, telegram_id: int) -> bool:
    """User membatalkan pesanan yang belum dibayar (pending -> cancelled)."""
    changed = db.query(StarsOrder).filter(
        StarsOrder.order_id == order_id, StarsOrder.telegram_id == telegram_id, StarsOrder.status == "pending",
    ).update({"status": "cancelled"}, synchronize_session=False)
    db.commit()
    return changed == 1


def expire_pending(db: Session) -> int:
    """Pesanan pending yang lewat batas waktu -> expired."""
    changed = db.query(StarsOrder).filter(
        StarsOrder.status == "pending", StarsOrder.expired_at.isnot(None), StarsOrder.expired_at < datetime.utcnow(),
    ).update({"status": "expired"}, synchronize_session=False)
    db.commit()
    return int(changed or 0)


def complete(db: Session, order_id: str, admin_id: int, allowed=("paid", "review")) -> Optional[StarsOrder]:
    """paid/review -> completed (sekali). Pesanan 'processing' sedang dikerjakan sistem: admin tidak bisa menyelesaikannya."""
    try:
        changed = db.query(StarsOrder).filter(StarsOrder.order_id == order_id, StarsOrder.status.in_(allowed)).update(
            {"status": "completed", "completed_at": datetime.utcnow(), "handled_by": admin_id,
             "fulfill_mode": "manual"},
            synchronize_session=False)
        if changed != 1:
            db.rollback()
            return None
        order = get_order(db, order_id)
        _audit(db, order, "STARS_ORDER_COMPLETED", "paid", "completed", f"Admin {admin_id} mengirim ke @{order.recipient_username}")
        db.commit()
        db.refresh(order)
        return order
    except Exception:
        db.rollback()
        logger.exception("Gagal menyelesaikan pesanan Stars/Premium %s", order_id)
        raise


def reject_and_refund(db: Session, order_id: str, admin_id: int, allowed=("paid", "review")) -> Optional[StarsOrder]:
    """paid/review -> rejected dan dana (total yang dibayar, termasuk kode unik QRIS) kembali ke Saldo Bot."""
    try:
        changed = db.query(StarsOrder).filter(StarsOrder.order_id == order_id, StarsOrder.status.in_(allowed)).update(
            {"status": "rejected", "completed_at": datetime.utcnow(), "handled_by": admin_id},
            synchronize_session=False)
        if changed != 1:
            db.rollback()
            return None
        order = get_order(db, order_id)
        refund = int(order.total_idr)  # semua yang dibayar user, termasuk kode unik
        db.query(User).filter(User.telegram_id == order.telegram_id).update(
            {User.balance_idr: func.coalesce(User.balance_idr, 0) + refund}, synchronize_session=False)
        _audit(db, order, "STARS_ORDER_REFUNDED", "paid", "rejected", f"Admin {admin_id} menolak, refund Rp {refund:,} ke Saldo Bot")
        db.commit()
        db.refresh(order)
        return order
    except Exception:
        db.rollback()
        logger.exception("Gagal me-refund pesanan Stars/Premium %s", order_id)
        raise


def get_paid_orders(db: Session, limit: int = 8) -> list:
    """Pesanan yang sudah dibayar dan menunggu dikirim admin (termasuk 'review'), yang terlama dulu."""
    return db.query(StarsOrder).filter(StarsOrder.status.in_(("paid", "review"))).order_by(
        StarsOrder.paid_at.asc(), StarsOrder.id.asc()).limit(limit).all()


def paid_summary(db: Session) -> tuple:
    """(jumlah, total Rupiah) pesanan menunggu dikirim."""
    count, total = db.query(func.count(StarsOrder.id), func.coalesce(func.sum(StarsOrder.total_idr), 0)).filter(
        StarsOrder.status.in_(("paid", "review"))).one()
    return int(count or 0), int(total or 0)


def get_user_orders(db: Session, telegram_id: int, limit: int = 5) -> list:
    return db.query(StarsOrder).filter(StarsOrder.telegram_id == telegram_id).order_by(
        StarsOrder.created_at.desc(), StarsOrder.id.desc()).limit(limit).all()


# ───────────── pemenuhan otomatis (Fragment) ─────────────

def claim_fulfillment(db: Session, order_id: str) -> Optional[StarsOrder]:
    """paid -> processing (sekali): hanya satu pekerja otomatis yang boleh menangani pesanan ini."""
    changed = db.query(StarsOrder).filter(StarsOrder.order_id == order_id, StarsOrder.status == "paid").update(
        {"status": "processing", "fulfill_mode": "auto", "processing_at": datetime.utcnow(),
         "fulfill_attempts": func.coalesce(StarsOrder.fulfill_attempts, 0) + 1}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        return None
    order = get_order(db, order_id)
    _audit(db, order, "STARS_AUTOFULFILL_START", "paid", "processing", "Pemenuhan otomatis dimulai")
    db.commit()
    db.refresh(order)
    return order


def release_to_manual(db: Session, order_id: str, error: str) -> Optional[StarsOrder]:
    """processing -> paid: kegagalan PASTI sebelum uang keluar; admin memprosesnya manual."""
    changed = db.query(StarsOrder).filter(StarsOrder.order_id == order_id, StarsOrder.status == "processing").update(
        {"status": "paid", "fulfill_mode": "manual", "fulfill_error": (error or "")[:300]}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        return None
    order = get_order(db, order_id)
    _audit(db, order, "STARS_AUTOFULFILL_MANUAL", "processing", "paid", (error or "")[:300])
    db.commit()
    db.refresh(order)
    return order


def mark_review(db: Session, order_id: str, ref: str, error: str, ton_nano: int = None) -> Optional[StarsOrder]:
    """processing -> review: uang mungkin sudah keluar tetapi hasilnya belum pasti (jangan kirim ulang otomatis)."""
    changed = db.query(StarsOrder).filter(StarsOrder.order_id == order_id, StarsOrder.status == "processing").update(
        {"status": "review", "fulfill_ref": (ref or "")[:120], "fulfill_error": (error or "")[:300],
         "ton_spent_nano": ton_nano}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        return None
    order = get_order(db, order_id)
    _audit(db, order, "STARS_AUTOFULFILL_REVIEW", "processing", "review", f"{ref} {error}"[:300])
    db.commit()
    db.refresh(order)
    return order


def complete_auto(db: Session, order_id: str, ref: str, ton_nano: int) -> Optional[StarsOrder]:
    """processing -> completed oleh sistem setelah pembayaran TON ke Fragment terbukti on-chain."""
    changed = db.query(StarsOrder).filter(StarsOrder.order_id == order_id, StarsOrder.status == "processing").update(
        {"status": "completed", "completed_at": datetime.utcnow(), "handled_by": 0, "fulfill_mode": "auto",
         "fulfill_ref": (ref or "")[:120], "ton_spent_nano": ton_nano, "fulfill_error": None}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        return None
    order = get_order(db, order_id)
    _audit(db, order, "STARS_AUTOFULFILL_DONE", "processing", "completed",
           f"TON {int(ton_nano or 0) / 1e9:.4f} ref {ref} -> @{order.recipient_username}")
    db.commit()
    db.refresh(order)
    return order


def auto_spent_today_idr(db: Session) -> int:
    """Total harga pesanan yang diproses otomatis hari ini (WIB): batas harian pengeluaran otomatis."""
    from database.crud import _wib_day_start_utc

    total = db.query(func.coalesce(func.sum(StarsOrder.price_idr), 0)).filter(
        StarsOrder.fulfill_mode == "auto", StarsOrder.status.in_(("processing", "completed", "review")),
        StarsOrder.processing_at >= _wib_day_start_utc()).scalar()
    return int(total or 0)


def get_unattempted_paid(db: Session, older_than_seconds: int = 20, limit: int = 5) -> list:
    """Pesanan paid yang belum pernah dicoba otomatis (pemicu langsung gagal/bot restart)."""
    cutoff = datetime.utcnow() - timedelta(seconds=older_than_seconds)
    return db.query(StarsOrder).filter(
        StarsOrder.status == "paid", func.coalesce(StarsOrder.fulfill_attempts, 0) == 0, StarsOrder.paid_at <= cutoff,
    ).order_by(StarsOrder.paid_at.asc()).limit(limit).all()


def get_stale_processing(db: Session, minutes: int = 10) -> list:
    """Pesanan 'processing' yang terlalu lama (proses mati di tengah jalan: uang mungkin sudah keluar)."""
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    return db.query(StarsOrder).filter(StarsOrder.status == "processing", StarsOrder.processing_at <= cutoff).all()
