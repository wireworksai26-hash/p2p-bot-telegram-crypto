"""
services/referral_discount_service.py — Referral Discount (Phase 7)
=====================================================================
Menghitung dan menerapkan diskon 7% pada fee transaksi untuk referrer
yang berhasil mengundang user. Setiap referrer mendapat 10 slot diskon.
"""

import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

REFERRAL_DISCOUNT_PCT: float = 7.0   # Persen diskon default
REFERRAL_DISCOUNT_USES: int = 10       # Jumlah transaksi yang mendapat diskon


def calculate_discounted_fee(base_fee_idr: int, discount_pct: float) -> tuple[int, int]:
    """
    Hitung fee setelah diskon.

    Args:
        base_fee_idr: Fee asli sebelum diskon (IDR)
        discount_pct: Persen diskon (contoh: 7.0 untuk 7%)

    Returns:
        Tuple: (discounted_fee_idr, discount_amount_idr)
    """
    if base_fee_idr <= 0 or discount_pct <= 0:
        return base_fee_idr, 0

    discount_amount = int(base_fee_idr * discount_pct / 100)
    discounted_fee = max(0, base_fee_idr - discount_amount)
    return discounted_fee, discount_amount


async def apply_referral_discount_if_eligible(
    telegram_id: int,
    base_fee_idr: int,
    db,
) -> dict:
    """
    Cek apakah user memiliki diskon referral aktif.
    Jika ya, hitung fee setelah diskon dan deduct 1 slot.

    Args:
        telegram_id: ID Telegram user (buyer)
        base_fee_idr: Fee transaksi asli sebelum diskon
        db: SQLAlchemy Session

    Returns:
        dict: {
            applied: bool,
            discount_pct: float,
            discount_amount: int,
            final_fee: int,
            remaining_after: int
        }
    """
    from database import crud

    try:
        info = crud.get_referral_discount_info(db, telegram_id)

        if not info["active"]:
            return {
                "applied": False,
                "discount_pct": 0.0,
                "discount_amount": 0,
                "final_fee": base_fee_idr,
                "remaining_after": 0,
            }

        # Consume 1 slot diskon
        pct = crud.consume_referral_discount(db, telegram_id)
        if pct is None:
            return {
                "applied": False,
                "discount_pct": 0.0,
                "discount_amount": 0,
                "final_fee": base_fee_idr,
                "remaining_after": 0,
            }

        final_fee, discount_amount = calculate_discounted_fee(base_fee_idr, pct)

        # Cek sisa setelah dikonsumsi
        new_info = crud.get_referral_discount_info(db, telegram_id)
        remaining_after = new_info.get("remaining", 0)

        logger.info(
            f"Diskon referral diterapkan untuk {telegram_id}: "
            f"fee {base_fee_idr} -> {final_fee} (diskon {pct}%, sisa {remaining_after})"
        )

        return {
            "applied": True,
            "discount_pct": pct,
            "discount_amount": discount_amount,
            "final_fee": final_fee,
            "remaining_after": remaining_after,
        }

    except Exception as e:
        logger.error(f"Error apply_referral_discount untuk {telegram_id}: {e}", exc_info=True)
        return {
            "applied": False,
            "discount_pct": 0.0,
            "discount_amount": 0,
            "final_fee": base_fee_idr,
            "remaining_after": 0,
        }


async def notify_discount_used(
    bot,
    telegram_id: int,
    remaining: int,
    discount_amount: int,
) -> None:
    """
    Kirim notifikasi ke user bahwa 1 slot diskon referral digunakan.
    """
    from bot.utils.formatter import format_idr

    if remaining > 0:
        msg = (
            f"🎁 <b>Diskon Referral Digunakan!</b>\n\n"
            f"Transaksi Anda mendapat potongan biaya sebesar <b>{format_idr(discount_amount)}</b>.\n"
            f"Sisa kuota diskon: <b>{remaining} transaksi</b>\n\n"
            f"<i>Terima kasih sudah mengundang teman!</i>"
        )
    else:
        msg = (
            f"🎁 <b>Diskon Referral Digunakan!</b>\n\n"
            f"Transaksi Anda mendapat potongan biaya sebesar <b>{format_idr(discount_amount)}</b>.\n"
            f"Kuota diskon referral Anda sudah habis (10/10 transaksi).\n\n"
            f"<i>Undang lebih banyak teman untuk mendapatkan diskon lagi!</i>"
        )

    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=msg,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Gagal kirim notif discount_used ke {telegram_id}: {e}")


async def notify_discount_exhausted(bot, telegram_id: int) -> None:
    """
    Kirim notifikasi ke user bahwa semua kuota diskon referral sudah habis.
    Dipanggil terpisah jika ingin notif khusus saat slot terakhir habis.
    """
    msg = (
        "🎁 <b>Kuota Diskon Referral Habis!</b>\n\n"
        "Anda telah menggunakan semua 10 slot diskon 7% dari program referral.\n\n"
        "Undang lebih banyak teman dan pastikan mereka bertransaksi untuk mendapatkan kuota diskon baru! 🚀"
    )
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=msg,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Gagal kirim notif discount_exhausted ke {telegram_id}: {e}")


async def activate_discount_for_referrer(
    bot, referrer_id: int, db
) -> None:
    """
    Aktifkan diskon referral untuk referrer setelah referee berhasil transaksi pertama.
    Dipanggil dari referral reward hook.
    """
    from database import crud

    try:
        disc = crud.activate_referral_discount(
            db, referrer_id,
            uses=REFERRAL_DISCOUNT_USES,
            pct=REFERRAL_DISCOUNT_PCT,
        )
        msg = (
            f"🎉 <b>Selamat! Anda Mendapat Diskon Referral!</b>\n\n"
            f"Teman yang Anda undang baru saja menyelesaikan transaksi pertama.\n\n"
            f"🎁 <b>Hadiah:</b> Diskon <b>7%</b> dari biaya transaksi\n"
            f"📊 <b>Kuota:</b> {REFERRAL_DISCOUNT_USES} transaksi berikutnya\n\n"
            f"Diskon akan otomatis diterapkan setiap kali Anda bertransaksi. Nikmati! ✨"
        )
        await bot.send_message(
            chat_id=referrer_id,
            text=msg,
            parse_mode="HTML",
        )
        logger.info(f"Diskon referral diaktifkan untuk referrer {referrer_id}")
    except Exception as e:
        logger.error(f"Gagal aktivasi diskon referral untuk {referrer_id}: {e}", exc_info=True)
