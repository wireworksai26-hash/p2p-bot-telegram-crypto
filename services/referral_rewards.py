"""
services/referral_rewards.py — Mesin reward referral (program v2)
==================================================================
Aturan (semua nilai bisa diubah admin, default sesuai ketentuan program):

  Pengundang
    • Rp 1.000 saat teman menyelesaikan transaksi pertama (beli/jual/convert)
    • Rp 500   saat teman menyelesaikan transaksi kedua
    • 7% dari fee setiap transaksi teman, maksimal 10 transaksi per teman
    • Semua reward masuk saldo bot setelah masa tahan 24 jam
  Teman yang diundang
    • Diskon fee Rp 1.000 di transaksi pertama (masuk saldo bot saat transaksi selesai)

Setiap hak dicatat di tabel referral_earnings dengan dedupe_key unik, jadi aman dipanggil
berulang oleh hook order, sweeper, atau proses paralel: satu order hanya dihitung sekali.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from database.models import AuditLog, Order, Referral, ReferralEarning, User

logger = logging.getLogger(__name__)

# name -> (config key, default, tipe). Dipakai juga oleh panel admin & /setreferral.
SETTING_DEFS = {
    "reward":    ("reward_first_tx_idr", 1000, int),     # reward pengundang, transaksi ke-1 teman
    "reward2":   ("reward_second_tx_idr", 500, int),     # reward pengundang, transaksi ke-2 teman
    "bonus":     ("referee_fee_discount_idr", 1000, int),  # diskon fee teman, transaksi pertamanya
    "share":     ("fee_share_pct", 7.0, float),          # % fee transaksi teman untuk pengundang
    "sharemax":  ("fee_share_max_tx", 10, int),          # maks transaksi per teman yang dibagi hasil
    "hold":      ("reward_hold_hours", 24, int),         # masa tahan reward pengundang
    "min_trade": ("min_trade_amount_idr", 0, int),       # minimal nominal transaksi yang dihitung
    "maxref":    ("max_referrals_per_user", 100, int),   # batas teman per pengundang
}

KIND_FIRST, KIND_SECOND, KIND_SHARE = "FIRST_TX", "SECOND_TX", "FEE_SHARE"
KIND_BONUS, KIND_COUNTED = "REFEREE_BONUS", "COUNTED"


def _cfg(db, key: str) -> Optional[str]:
    from database.crud import get_referral_config
    return get_referral_config(db, key)


def get_referral_settings(db) -> dict:
    """Seluruh pengaturan program dengan default; nilai rusak/negatif jatuh ke default / 0."""
    out = {}
    for name, (key, default, cast) in SETTING_DEFS.items():
        raw = _cfg(db, key)
        try:
            val = cast(float(raw)) if raw not in (None, "") else default
        except (TypeError, ValueError):
            val = default
        out[name] = max(cast(0), val)
    enabled = _cfg(db, "referral_enabled")
    out["enabled"] = enabled is None or enabled.lower() == "true"
    out["fee_guard"] = (_cfg(db, "referral_fee_guard") or "true").lower() != "false"
    out["share"] = min(out["share"], 100.0)
    return out


def _completion_ts(order: Order) -> datetime:
    return order.completed_at or order.updated_at or order.created_at or datetime.utcnow()


def _accrue_order(db, ref: Referral, order: Order, ts: datetime, s: dict) -> int:
    """
    Hitung satu order selesai milik teman: tandai, naikkan penghitung transaksi, catat hak
    reward. Satu transaksi database. Return jumlah baris reward yang dibuat (0 bila order
    sudah pernah dihitung atau tidak menghasilkan reward).
    """
    try:
        # Penanda unik per order = klaim atomik "order ini sudah dihitung".
        db.add(ReferralEarning(
            referral_id=ref.id, beneficiary_id=ref.referrer_id, referee_id=ref.referee_id,
            order_id=order.order_id, kind=KIND_COUNTED, amount_idr=0, status="RELEASED",
            dedupe_key=f"order:{order.order_id}", released_at=datetime.utcnow(),
            notified_at=datetime.utcnow(),
        ))
        db.flush()

        # Naikkan lalu baca penghitung di transaksi yang sama: tiap order mendapat nomor unik.
        db.query(Referral).filter(Referral.id == ref.id).update(
            {"tx_count": Referral.tx_count + 1}, synchronize_session=False)
        n = int(db.query(Referral.tx_count).filter(Referral.id == ref.id).scalar())

        fee = int(order.fee_idr or 0)
        budget = fee if s["fee_guard"] else 10 ** 12  # total payout satu order tidak melebihi fee
        release_at = ts + timedelta(hours=s["hold"])
        now = datetime.utcnow()
        referrer_total = 0
        rows = 0

        def grant(kind, beneficiary, amount, dedupe, immediate=False):
            nonlocal budget, rows
            amount = int(min(amount, budget))
            if amount <= 0:
                return 0
            budget -= amount
            db.add(ReferralEarning(
                referral_id=ref.id, beneficiary_id=beneficiary, referee_id=ref.referee_id,
                order_id=order.order_id, kind=kind, amount_idr=amount,
                status="RELEASED" if immediate else "HELD", dedupe_key=dedupe,
                release_at=None if immediate else release_at,
                released_at=now if immediate else None,
            ))
            rows += 1
            return amount

        if n == 1 and s["bonus"] > 0:
            got = grant(KIND_BONUS, ref.referee_id, s["bonus"], f"bonus:{ref.id}", immediate=True)
            if got:
                db.query(User).filter(User.telegram_id == ref.referee_id).update(
                    {User.balance_idr: func.coalesce(User.balance_idr, 0) + got}, synchronize_session=False)
                db.add(AuditLog(telegram_id=ref.referee_id, action="REFERRAL_BONUS", order_id=order.order_id,
                                details=f"Diskon fee transaksi pertama (diundang {ref.referrer_id}): +Rp {got:,}"))
        if n == 1:
            referrer_total += grant(KIND_FIRST, ref.referrer_id, s["reward"], f"first:{ref.id}")
        elif n == 2:
            referrer_total += grant(KIND_SECOND, ref.referrer_id, s["reward2"], f"second:{ref.id}")
        if s["share"] > 0 and n <= s["sharemax"]:
            referrer_total += grant(KIND_SHARE, ref.referrer_id, int(fee * s["share"] / 100),
                                    f"share:{order.order_id}")

        updates = {}
        if ref.status == "PENDING":
            updates.update(status="COMPLETED", completed_at=ts, reward_idr=referrer_total)
        elif referrer_total:
            updates["reward_idr"] = Referral.reward_idr + referrer_total
        if updates:
            db.query(Referral).filter(Referral.id == ref.id).update(updates, synchronize_session=False)
        db.commit()
        return rows
    except IntegrityError:
        db.rollback()  # order sudah dihitung oleh proses lain
        return 0
    except Exception:
        db.rollback()
        logger.error("Gagal menghitung reward referral order %s", getattr(order, "order_id", "?"), exc_info=True)
        return 0


def process_referral_for_referee(db, referee_id: int) -> int:
    """
    Hitung semua order selesai milik teman yang belum dihitung, urut waktu selesai.
    Idempotent. Dipanggil saat order selesai dan oleh sweeper (menyelesaikan referral
    yang tertinggal, termasuk transaksi lama dari referral yang masih PENDING).
    """
    ref = db.query(Referral).filter(Referral.referee_id == referee_id).first()
    if not ref:
        return 0
    s = get_referral_settings(db)
    if not s["enabled"]:
        return 0

    counted = {
        oid for (oid,) in db.query(ReferralEarning.order_id).filter(
            ReferralEarning.referral_id == ref.id, ReferralEarning.kind == KIND_COUNTED)
    }
    orders = (
        db.query(Order)
        .filter(Order.telegram_id == referee_id, func.lower(Order.status) == "completed")
        .all()
    )
    orders.sort(key=lambda o: (_completion_ts(o), o.id or 0))

    created = 0
    for o in orders:
        if o.order_id in counted:
            continue
        ts = _completion_ts(o)
        if ref.legacy_until and ts <= ref.legacy_until:
            continue  # sudah dibayar dengan skema lama
        if ref.created_at and ts < ref.created_at:
            continue  # hanya transaksi yang selesai setelah referral dibuat yang dihitung
        if s["min_trade"] > 0 and float(o.nominal_idr or o.total_idr or 0) < s["min_trade"]:
            continue
        db.refresh(ref)  # tx_count/status terbaru setelah order sebelumnya
        created += _accrue_order(db, ref, o, ts, s)
    return created


def release_due_earnings(db, now: Optional[datetime] = None) -> dict:
    """
    Pindahkan reward HELD yang masa tahannya habis ke saldo bot. Klaim atomik per baris
    (HELD -> RELEASED) bersamaan dengan kredit saldo dalam satu transaksi.
    Return {beneficiary_id: total_rupiah_yang_dirilis}.
    """
    now = now or datetime.utcnow()
    due_ids = [i for (i,) in db.query(ReferralEarning.id).filter(
        ReferralEarning.status == "HELD", ReferralEarning.release_at <= now,
    ).order_by(ReferralEarning.id.asc()).limit(200)]

    released: dict = {}
    for earning_id in due_ids:
        try:
            e = db.query(ReferralEarning).filter(ReferralEarning.id == earning_id).first()
            if not e:
                continue
            claimed = db.query(ReferralEarning).filter(
                ReferralEarning.id == earning_id, ReferralEarning.status == "HELD",
            ).update({"status": "RELEASED", "released_at": now}, synchronize_session=False)
            if claimed != 1:
                db.rollback()
                continue
            if not db.query(User.telegram_id).filter(User.telegram_id == e.beneficiary_id).first():
                db.add(User(telegram_id=e.beneficiary_id, balance_idr=0))
                db.flush()
            db.query(User).filter(User.telegram_id == e.beneficiary_id).update(
                {User.balance_idr: func.coalesce(User.balance_idr, 0) + int(e.amount_idr)},
                synchronize_session=False)
            db.add(AuditLog(telegram_id=e.beneficiary_id, action="REFERRAL_REWARD", order_id=e.order_id,
                            details=f"Reward referral {e.kind} (teman {e.referee_id}): +Rp {int(e.amount_idr):,}"))
            db.commit()
            released[e.beneficiary_id] = released.get(e.beneficiary_id, 0) + int(e.amount_idr)
        except Exception:
            db.rollback()
            logger.error("Gagal merilis reward referral #%s", earning_id, exc_info=True)
    return released
