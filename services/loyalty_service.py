"""
services/loyalty_service.py — Time-based Loyalty Reward (Phase 7)
==================================================================
Sistem reward loyalitas berbasis rentang waktu:
Jika user menyelesaikan >= N transaksi dalam M hari, otomatis mendapat reward saldo bot.
Semua parameter (N, M, reward) bisa dikonfigurasi admin via panel.
"""

import logging
from datetime import datetime
from decimal import Decimal

logger = logging.getLogger(__name__)


async def process_loyalty_after_order(
    telegram_id: int,
    order_amount_idr: int,
    bot,
    db,
) -> dict:
    """
    Hook utama: dipanggil setiap kali order berubah ke status COMPLETED.
    
    Flow:
    1. Ambil/buat loyalty window aktif user
    2. Increment tx count (jika nominal memenuhi syarat)
    3. Jika baru qualified: kredit reward, kirim notifikasi

    Args:
        telegram_id: ID Telegram user
        order_amount_idr: Nominal total transaksi (IDR)
        bot: Instance bot Telegram (untuk kirim notifikasi)
        db: SQLAlchemy Session

    Returns:
        dict: {action: str, qualified: bool, current_count: int, needed: int}
    """
    from database import crud

    try:
        # Cek apakah loyalty enabled
        enabled = crud.get_loyalty_config(db, "loyalty_enabled") or "true"
        if enabled.lower() != "true":
            return {"action": "disabled", "qualified": False}

        # Increment dan cek status
        result = crud.increment_loyalty_tx(db, telegram_id, order_amount_idr)

        if result.get("skipped"):
            logger.info(
                f"Loyalty: tx {telegram_id} dilewati "
                f"(nominal {order_amount_idr} di bawah minimum)"
            )
            return {
                "action": "skipped_low_amount",
                "qualified": result["qualified"],
                "current_count": result["current_count"],
                "needed": result["needed"],
            }

        if result.get("already_rewarded"):
            return {
                "action": "already_rewarded",
                "qualified": True,
                "current_count": result["current_count"],
                "needed": 0,
            }

        # Jika baru qualified di iterasi ini → beri reward
        if result.get("newly_qualified"):
            reward_idr = int(crud.get_loyalty_config(db, "reward_amount_idr") or 25_000)
            window_id = result["window_id"]
            await _give_loyalty_reward(bot, db, telegram_id, reward_idr, window_id, result["current_count"])
            return {
                "action": "rewarded",
                "qualified": True,
                "current_count": result["current_count"],
                "reward_idr": reward_idr,
                "needed": 0,
            }

        # Progress update: kirim notifikasi progress (silent, hanya pada tx ke-2 dan ke-4)
        count = result["current_count"]
        needed = result["needed"]
        min_tx = int(crud.get_loyalty_config(db, "min_tx_count") or 5)
        if count in (2, min_tx - 1) and needed > 0:
            window_end = result["window_end"]
            days_left = max(0, (window_end - datetime.utcnow()).days)
            await notify_loyalty_progress(bot, telegram_id, count, min_tx, days_left)

        return {
            "action": "progress",
            "qualified": False,
            "current_count": count,
            "needed": needed,
        }

    except Exception as e:
        logger.error(
            f"Error process_loyalty_after_order untuk {telegram_id}: {e}",
            exc_info=True,
        )
        return {"action": "error", "qualified": False}


async def _give_loyalty_reward(
    bot,
    db,
    telegram_id: int,
    reward_idr: int,
    window_id: int,
    tx_count: int,
) -> None:
    """
    Internal: Kredit saldo ke user dan tandai window sebagai sudah direward.
    """
    from database import crud
    from database.models import User, AuditLog
    from bot.utils.formatter import format_idr

    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user or user.is_banned:
            logger.warning(f"Loyalty reward skip: user {telegram_id} tidak ditemukan atau banned")
            return

        # Kredit saldo
        old_balance = user.balance_idr or Decimal("0")
        user.balance_idr = old_balance + Decimal(reward_idr)
        new_balance = user.balance_idr

        # Tandai window sebagai sudah direward
        crud.mark_loyalty_rewarded(db, window_id, reward_idr)

        # Audit log
        audit = AuditLog(
            telegram_id=telegram_id,
            action="LOYALTY_REWARD",
            details=(
                f"Loyalty reward: +{format_idr(reward_idr)} "
                f"({tx_count} transaksi dalam window). "
                f"Saldo: {format_idr(int(old_balance))} -> {format_idr(int(new_balance))}"
            ),
        )
        db.add(audit)
        db.commit()

        logger.info(
            f"Loyalty reward diberikan ke {telegram_id}: "
            f"+{format_idr(reward_idr)} (total saldo: {format_idr(int(new_balance))})"
        )

        # Kirim notifikasi
        await notify_loyalty_reward(bot, telegram_id, reward_idr, tx_count)

    except Exception as e:
        logger.error(f"Gagal memberikan loyalty reward ke {telegram_id}: {e}", exc_info=True)
        db.rollback()


async def notify_loyalty_reward(
    bot,
    telegram_id: int,
    reward_idr: int,
    tx_count: int,
) -> None:
    """Kirim notifikasi ke user bahwa reward loyalty diterima."""
    from bot.utils.formatter import format_idr

    msg = (
        f"🎉 <b>SELAMAT! REWARD LOYALITAS DITERIMA!</b>\n\n"
        f"Anda berhasil menyelesaikan <b>{tx_count} transaksi</b> dalam rentang waktu yang ditentukan.\n\n"
        f"💰 <b>Reward:</b> <code>+{format_idr(reward_idr)}</code> saldo bot\n\n"
        f"Saldo langsung aktif dan siap digunakan untuk transaksi berikutnya! 🚀\n"
        f"Terus bertransaksi untuk mendapatkan reward loyalitas berikutnya."
    )
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=msg,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Gagal kirim notif loyalty_reward ke {telegram_id}: {e}")


async def notify_loyalty_progress(
    bot,
    telegram_id: int,
    current: int,
    needed: int,
    days_left: int,
) -> None:
    """
    Kirim notifikasi progress menuju loyalty reward.
    Dipanggil pada tx ke-2 dan tx ke-(N-1) saja agar tidak spam.
    """
    remaining_tx = needed
    msg = (
        f"⏳ <b>Progress Loyalty Reward</b>\n\n"
        f"Anda sudah menyelesaikan <b>{current} transaksi</b> dalam periode ini.\n"
        f"Kurang <b>{remaining_tx} transaksi lagi</b> dalam <b>{days_left} hari</b> "
        f"untuk mendapatkan reward loyalitas!\n\n"
        f"<i>Terus bertransaksi dan dapatkan hadiahnya!</i> 🔥"
    )
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=msg,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Gagal kirim notif loyalty_progress ke {telegram_id}: {e}")


async def run_loyalty_check_job(bot, db) -> dict:
    """
    Job scheduler: cek semua loyalty windows yang qualified tapi belum di-reward.
    Jalankan secara periodik (misal setiap 1 jam) sebagai safety net.
    Return: {checked: int, rewarded: int, errors: int}
    """
    from database import crud

    enabled = crud.get_loyalty_config(db, "loyalty_enabled") or "true"
    if enabled.lower() != "true":
        return {"checked": 0, "rewarded": 0, "errors": 0}

    eligible = crud.get_loyalty_eligible_users(db)
    reward_idr = int(crud.get_loyalty_config(db, "reward_amount_idr") or 25_000)

    rewarded = 0
    errors = 0

    for item in eligible:
        try:
            await _give_loyalty_reward(
                bot=bot,
                db=db,
                telegram_id=item["telegram_id"],
                reward_idr=reward_idr,
                window_id=item["window_id"],
                tx_count=item["tx_count"],
            )
            rewarded += 1
        except Exception as e:
            logger.error(
                f"loyalty_check_job: gagal reward untuk {item['telegram_id']}: {e}"
            )
            errors += 1

    logger.info(
        f"Loyalty check job selesai: {len(eligible)} eligible, "
        f"{rewarded} rewarded, {errors} errors"
    )
    return {"checked": len(eligible), "rewarded": rewarded, "errors": errors}
