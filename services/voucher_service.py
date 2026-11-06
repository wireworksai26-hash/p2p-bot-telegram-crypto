"""Voucher potongan fee transaksi (Beli / Jual / Convert).

Aturan:
  * Kode dibandingkan tanpa peduli huruf besar/kecil dan spasi.
  * Potongan mengurangi FEE, tidak pernah melebihi fee itu sendiri (fee tidak bisa negatif).
  * Batas pemakaian per user dan total dihitung dari order yang masih hidup; order yang kedaluwarsa,
    dibatalkan, atau ditolak mengembalikan jatah vouchernya.
  * Pemakaian dicatat saat order dibuat (redeem), bukan saat voucher dimasukkan di layar ringkasan.
"""
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database.models import Order, Voucher, VoucherRedemption

logger = logging.getLogger(__name__)

FLOWS = ("buy", "sell", "swap")
FLOW_LABEL = {"buy": "Beli", "sell": "Jual", "swap": "Convert"}
_DEAD_STATUSES = ("expired", "cancelled", "canceled", "rejected", "failed")

DEFAULT_VOUCHERS = (
    # (kode, potongan fee Rp, maks per user, maks total pemakaian, masa berlaku hari sejak dipasang)
    # Dibatasi sejak awal untuk mencegah penyalahgunaan; admin bisa mengubahnya lewat /voucher add.
    ("tokokoin1111", 500, 1, 200, 30),
)

WIB = timedelta(hours=7)


def parse_validity(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Batas waktu berlaku (UTC naive) dari '7d' / '12h' / '2w' (sejak sekarang) atau 'YYYY-MM-DD'
    (berlaku sampai akhir hari itu WIB). '0' atau '-' = tanpa batas waktu (None). ValueError bila tidak dikenali."""
    now = now or datetime.utcnow()
    value = str(text or "").strip().lower()
    if value in ("0", "-", "tanpa", "none"):
        return None
    match = re.fullmatch(r"(\d{1,4})([hdw])", value)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        if amount <= 0:
            raise ValueError("durasi harus lebih dari 0")
        return now + {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        end_wib = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), 23, 59, 59)
        return end_wib - WIB
    raise ValueError("format masa berlaku tidak dikenali")


def format_validity(valid_until: Optional[datetime]) -> str:
    """'s/d 12-12-2026 23:59 WIB' atau 'tanpa batas waktu'."""
    if not valid_until:
        return "tanpa batas waktu"
    return "s/d " + (valid_until + WIB).strftime("%d-%m-%Y %H:%M") + " WIB"


def normalize_code(text) -> str:
    return "".join(str(text or "").split()).lower()


def get_voucher(db: Session, code: str) -> Optional[Voucher]:
    return db.query(Voucher).filter(Voucher.code == normalize_code(code)).first()


def _live_redemptions(db: Session, code: str, telegram_id: int = None) -> int:
    query = db.query(func.count(VoucherRedemption.id)).outerjoin(
        Order, Order.order_id == VoucherRedemption.order_id,
    ).filter(
        VoucherRedemption.voucher_code == code,
        or_(Order.id.is_(None), func.lower(Order.status).notin_(_DEAD_STATUSES)),
    )
    if telegram_id is not None:
        query = query.filter(VoucherRedemption.telegram_id == telegram_id)
    return int(query.scalar() or 0)


def check_voucher(db: Session, code: str, telegram_id: int, flow: str) -> Tuple[Optional[Voucher], Optional[str]]:
    """(voucher, None) bila boleh dipakai; (None, pesan_alasan) bila tidak."""
    normalized = normalize_code(code)
    if not normalized:
        return None, "Kode voucher kosong. Ketik kode vouchernya."
    voucher = get_voucher(db, normalized)
    if voucher is None:
        return None, "Kode voucher tidak ditemukan. Periksa penulisannya lalu coba lagi."
    if not voucher.active:
        return None, "Voucher ini sudah tidak aktif."
    if voucher.valid_until and voucher.valid_until < datetime.utcnow():
        return None, "Voucher ini sudah kedaluwarsa."
    if flow not in [f.strip() for f in (voucher.applies_to or "").split(",")]:
        allowed = ", ".join(FLOW_LABEL.get(f.strip(), f.strip()) for f in voucher.applies_to.split(",") if f.strip())
        return None, f"Voucher ini hanya berlaku untuk transaksi {allowed}."
    if voucher.max_per_user and _live_redemptions(db, voucher.code, telegram_id) >= voucher.max_per_user:
        return None, "Voucher ini sudah kamu pakai (batas pemakaian per akun tercapai)."
    if voucher.max_total and _live_redemptions(db, voucher.code) >= voucher.max_total:
        return None, "Kuota voucher ini sudah habis."
    return voucher, None


def applied_discount(fee_idr: int, voucher_discount: int) -> int:
    """Potongan yang benar-benar berlaku: tidak melebihi fee."""
    return max(0, min(int(fee_idr), int(voucher_discount)))


def redeem(db: Session, code: str, telegram_id: int, order_id: str, discount_idr: int) -> None:
    """Catat pemakaian voucher pada order yang baru dibuat. Tidak pernah melempar (order sudah jadi)."""
    try:
        db.add(VoucherRedemption(voucher_code=normalize_code(code), telegram_id=telegram_id,
                                 order_id=order_id, discount_idr=int(discount_idr)))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Gagal mencatat pemakaian voucher %s untuk order %s", code, order_id)


def ensure_default_vouchers(db: Session) -> int:
    """Pasang voucher bawaan bila belum ada (tidak menimpa yang sudah diubah admin). Return jumlah baru."""
    created = 0
    for code, discount, per_user, total, days in DEFAULT_VOUCHERS:
        if get_voucher(db, code) is None:
            db.add(Voucher(code=code, discount_idr=discount, max_per_user=per_user, max_total=total,
                           valid_until=datetime.utcnow() + timedelta(days=days) if days else None,
                           note="voucher bawaan"))
            created += 1
    if created:
        db.commit()
    return created


# ───────────── pengelolaan admin ─────────────

def upsert_voucher(db: Session, code: str, discount_idr: int, max_per_user: int = 1, max_total: int = 0,
                   applies_to: str = "buy,sell,swap", valid_until: Optional[datetime] = None) -> Voucher:
    code = normalize_code(code)
    voucher = get_voucher(db, code)
    if voucher is None:
        voucher = Voucher(code=code, discount_idr=int(discount_idr))
        db.add(voucher)
    voucher.discount_idr = int(discount_idr)
    voucher.max_per_user = int(max_per_user)
    voucher.max_total = int(max_total)
    voucher.applies_to = applies_to
    voucher.valid_until = valid_until
    voucher.active = True
    db.commit()
    db.refresh(voucher)
    return voucher


def set_active(db: Session, code: str, active: bool) -> bool:
    voucher = get_voucher(db, code)
    if voucher is None:
        return False
    voucher.active = bool(active)
    db.commit()
    return True


def list_vouchers(db: Session) -> list:
    """[(Voucher, jumlah pemakaian hidup)]."""
    return [(v, _live_redemptions(db, v.code)) for v in db.query(Voucher).order_by(Voucher.id.asc()).all()]
