"""
services/referral_service.py — Sweeper program referral
========================================================
Dijalankan periodik oleh scheduler (tiap 60 detik). Tiga tugas:

1. Hitung reward untuk transaksi teman yang belum terhitung (referral PENDING yang
   sudah punya transaksi selesai, jalur completion yang terlewat).
2. Rilis reward pengundang yang masa tahannya (default 24 jam) sudah habis ke saldo bot.
3. Beri tahu penerima: reward baru tercatat, bonus teman, dan reward yang sudah masuk saldo.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from bot.utils.formatter import format_idr
from bot.utils.telegram_utils import safe_send_message
from database.connection import SessionLocal
from database.models import Order, Referral, ReferralEarning
from services.referral_rewards import (
    KIND_BONUS, KIND_COUNTED, KIND_FIRST, KIND_SECOND, KIND_SHARE,
    get_referral_settings, process_referral_for_referee, release_due_earnings,
)

logger = logging.getLogger(__name__)

_BATCH = 100
_RECENT_DAYS = 3
_NOTIFY_LOOKBACK_DAYS = 7


def _referees_to_process(db) -> list:
    """Teman yang perlu dicek: referral PENDING + teman dengan order selesai beberapa hari terakhir."""
    ids = {rid for (rid,) in db.query(Referral.referee_id).filter(Referral.status == "PENDING").limit(_BATCH)}
    cutoff = datetime.utcnow() - timedelta(days=_RECENT_DAYS)
    recent = (
        db.query(Order.telegram_id)
        .join(Referral, Referral.referee_id == Order.telegram_id)
        .filter(
            func.lower(Order.status) == "completed",
            func.coalesce(Order.completed_at, Order.updated_at) >= cutoff,
            func.coalesce(Order.completed_at, Order.updated_at) >= Referral.created_at,
        )
        .distinct()
        .limit(_BATCH)
    )
    ids.update(rid for (rid,) in recent)
    return sorted(ids)


def accrue_pending(db) -> int:
    """Tugas 1. Return jumlah hak reward baru yang tercatat."""
    created = 0
    for referee_id in _referees_to_process(db):
        created += process_referral_for_referee(db, referee_id)
    return created


def _earning_label(kind: str) -> str:
    return {
        KIND_FIRST: "transaksi pertama teman",
        KIND_SECOND: "transaksi kedua teman",
        KIND_SHARE: "bagi hasil fee",
    }.get(kind, kind)


async def _notify_new_earnings(bot, db) -> int:
    """Tugas 3a: beri tahu penerima untuk hak baru (sekali per baris)."""
    cutoff = datetime.utcnow() - timedelta(days=_NOTIFY_LOOKBACK_DAYS)
    rows = (
        db.query(ReferralEarning)
        .filter(
            ReferralEarning.notified_at.is_(None),
            ReferralEarning.kind != KIND_COUNTED,
            ReferralEarning.created_at >= cutoff,
        )
        .order_by(ReferralEarning.id.asc())
        .limit(_BATCH)
        .all()
    )
    if not rows:
        return 0

    hold_hours = get_referral_settings(db)["hold"]
    claimed = []
    for e in rows:
        # Klaim atomik: hanya satu proses yang mengirim notifikasi untuk baris ini.
        n = db.query(ReferralEarning).filter(
            ReferralEarning.id == e.id, ReferralEarning.notified_at.is_(None),
        ).update({"notified_at": datetime.utcnow()}, synchronize_session=False)
        if n == 1:
            claimed.append(e)
    db.commit()

    by_user: dict = {}
    for e in claimed:
        by_user.setdefault(e.beneficiary_id, []).append(e)

    sent = 0
    for user_id, items in by_user.items():
        bonus = sum(int(e.amount_idr) for e in items if e.kind == KIND_BONUS)
        earned = [e for e in items if e.kind != KIND_BONUS]
        parts = []
        if bonus:
            parts.append(
                f"🎁 <b>Diskon fee referral</b> {format_idr(bonus)} dari transaksi pertama Anda "
                f"sudah masuk ke saldo bot."
            )
        if earned:
            lines = "\n".join(f"• {format_idr(int(e.amount_idr))} — {_earning_label(e.kind)}" for e in earned)
            total = sum(int(e.amount_idr) for e in earned)
            wait = f"{hold_hours} jam" if hold_hours else "beberapa saat"
            parts.append(
                f"🎉 <b>Referral Reward!</b>\n"
                f"Teman yang Anda ajak menyelesaikan transaksi.\n{lines}\n"
                f"Total <b>{format_idr(total)}</b> akan masuk ke saldo bot setelah masa tahan {wait}."
            )
        if parts:
            await safe_send_message(bot, user_id, "\n\n".join(parts))
            sent += 1
    return sent


async def _notify_released(bot, released: dict) -> None:
    """Tugas 3b: reward yang baru masuk saldo."""
    for user_id, total in released.items():
        await safe_send_message(
            bot, user_id,
            f"💰 <b>Reward Referral Masuk Saldo!</b>\n\n"
            f"<b>{format_idr(total)}</b> sudah ditambahkan ke saldo bot Anda. "
            f"Bisa dipakai untuk transaksi atau ditarik ke rekening/e-wallet.",
        )


async def process_referral_rewards(bot) -> dict:
    """Satu putaran sweeper. Return ringkasan {"accrued": n, "released": rupiah_total}."""
    db = SessionLocal()
    try:
        accrued = accrue_pending(db)
        released = release_due_earnings(db)
        if bot:
            try:
                await _notify_new_earnings(bot, db)
                await _notify_released(bot, released)
            except Exception as exc:
                logger.warning("Gagal mengirim notifikasi referral: %s", exc)
        return {"accrued": accrued, "released": sum(released.values())}
    finally:
        db.close()
