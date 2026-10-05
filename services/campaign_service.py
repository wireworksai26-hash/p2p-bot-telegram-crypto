"""
services/campaign_service.py — Core Engine Campaign & Giveaway Saldo Bot
========================================================================
Menyediakan template siap pakai, simulasi/dry-run, seleksi kandidat
(Milestone/Random/Bagi Rata), proteksi budget cap, dan eksekusi atomic.
"""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Any
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database.models import User, Order, AuditLog, Campaign, CampaignDistribution
from database import crud
from bot.utils.formatter import format_idr
from bot.utils.emojis import tg_emoji

logger = logging.getLogger(__name__)

# Template Preset Siap Pakai
CAMPAIGN_TEMPLATES: dict[str, dict[str, Any]] = {
    "tpl_split_all": {
        "key": "tpl_split_all",
        "title": "🎁 Bagi Rata Buyer Aktif",
        "description": "Bagi rata total pool hadiah ke user yang minimal sudah pernah transaksi 1 kali.",
        "mode": "EQUAL_SPLIT",
        "target_segment": "buyers",
        "default_pool": 500_000,
        "preset_pools": [100_000, 250_000, 500_000, 1_000_000],
        "default_notif": (
            f"{tg_emoji('GIFT', '🎁')} <b>KABAR GEMBIRA! BAGI-BAGI SALDO DARI BOT</b>\n\n"
            "Halo <b>{name}</b>, Anda mendapatkan hadiah saldo gratis dari event <b>{campaign_name}</b>!\n"
            f"{tg_emoji('MONEY_BAG', '💰')} <b>Reward:</b> <code>+{{reward}}</code>\n"
            f"{tg_emoji('DIAMOND', '💳')} <b>Saldo Baru Anda:</b> <code>{{new_balance}}</code>\n\n"
            f"{tg_emoji('ROCKET', '🚀')} Yuk langsung gunakan saldo ini untuk transaksi Beli/Swap crypto di bot tanpa perlu top-up!"
        ).replace("{name}", "{name}").replace("{campaign_name}", "{campaign_name}"),
    },
    "tpl_loyalty_buyers": {
        "key": "tpl_loyalty_buyers",
        "title": "🛒 Loyalty Buyer Reward",
        "description": "Reward untuk pelanggan setia dengan frekuensi/keaktifan transaksi terbanyak dalam interval waktu tertentu (Program Loyalitas).",
        "mode": "MILESTONE",
        "milestone_metric": "TX_COUNT",
        "days_lookback": 30,
        "target_segment": "buyers",
        "default_winners": 20,
        "preset_winners": [5, 10, 20, 30, 50],
        "default_pool": 500_000,
        "preset_pools": [100_000, 250_000, 500_000, 1_000_000],
        "default_notif": (
            f"{tg_emoji('PARTY', '🎉')} <b>SELAMAT! REWARD LOYALITAS ANDA CAIR!</b>\n\n"
            "Halo <b>{name}</b>, sebagai apresiasi atas keaktifan dan loyalitas transaksi Anda, Anda berhasil mendapatkan reward program Loyalty Buyer!\n"
            f"{tg_emoji('MONEY_BAG', '💰')} <b>Reward:</b> <code>+{{reward}}</code>\n"
            f"{tg_emoji('DIAMOND', '💳')} <b>Saldo Baru Anda:</b> <code>{{new_balance}}</code>\n\n"
            f"{tg_emoji('SPARKLES', '✨')} Terima kasih telah setia bertransaksi bersama kami. Saldo ini siap langsung digunakan untuk transaksi berikutnya!"
        ).replace("{name}", "{name}"),
    },
    "tpl_top_spenders": {
        "key": "tpl_top_spenders",
        "title": "🏆 Top Spender / Trader Milestone",
        "description": "Hadiah khusus untuk Top User dengan akumulasi transaksi terbesar.",
        "mode": "MILESTONE",
        "milestone_metric": "VOLUME_IDR",
        "target_segment": "buyers",
        "default_winners": 10,
        "preset_winners": [5, 10, 20, 30],
        "default_pool": 1_000_000,
        "preset_pools": [250_000, 500_000, 1_000_000, 2_000_000],
        "default_notif": (
            f"{tg_emoji('TROPHY', '🏆')} <b>SELAMAT! ANDA MASUK TOP TRADER!</b>\n\n"
            "Halo <b>{name}</b>, Anda berhasil masuk jajaran peringkat pemenang <b>{campaign_name}</b>!\n"
            "🥇 <b>Peringkat Anda:</b> <code>#{{rank}}</code>\n"
            f"{tg_emoji('MONEY_BAG', '💰')} <b>Reward:</b> <code>+{{reward}}</code>\n"
            f"{tg_emoji('DIAMOND', '💳')} <b>Saldo Baru Anda:</b> <code>{{new_balance}}</code>\n\n"
            f"{tg_emoji('FIRE', '🔥')} Pertahankan peringkat Anda dan nikmati kemudahan transaksi crypto di bot!"
        ).replace("{name}", "{name}").replace("{campaign_name}", "{campaign_name}"),
    },
    "tpl_flash_random": {
        "key": "tpl_flash_random",
        "title": "⚡ Flash Giveaway Acak",
        "description": "Undian kilat berhadiah untuk sejumlah user beruntung.",
        "mode": "RANDOM",
        "target_segment": "all",
        "default_winners": 25,
        "preset_winners": [10, 25, 50, 100],
        "default_pool": 250_000,
        "preset_pools": [100_000, 250_000, 500_000, 1_000_000],
        "default_notif": (
            f"{tg_emoji('FIRE', '⚡')} <b>FLASH GIVEAWAY DARI BOT!</b>\n\n"
            "Halo <b>{name}</b>, nomor akun Anda beruntung terpilih dalam undian Flash Giveaway!\n"
            f"{tg_emoji('MONEY_BAG', '💰')} <b>Reward:</b> <code>+{{reward}}</code>\n"
            f"{tg_emoji('DIAMOND', '💳')} <b>Saldo Baru Anda:</b> <code>{{new_balance}}</code>\n\n"
            f"{tg_emoji('ROCKET', '🚀')} Saldo langsung aktif dan dapat Anda gunakan untuk transaksi sekarang juga!"
        ).replace("{name}", "{name}"),
    },
}


def get_template(template_key: str) -> Optional[dict[str, Any]]:
    """Ambil data template preset."""
    return CAMPAIGN_TEMPLATES.get(template_key)


def get_top_users_by_milestone(
    db: Session,
    metric: str = "VOLUME_IDR",
    limit: int = 10,
    min_value: int = 0,
    days: Optional[int] = None,
) -> list[dict[str, Any]]:
    """
    Mengambil daftar top user berdasarkan akumulasi volume atau jumlah transaksi.
    Hanya menghitung order yang statusnya COMPLETED dan user non-banned.
    """
    order_filter = [func.lower(Order.status) == "completed"]
    if days and days > 0:
        cutoff = datetime.utcnow() - timedelta(days=days)
        order_filter.append(Order.created_at >= cutoff)

    metric_col = (
        func.sum(Order.total_idr).label("metric_val")
        if metric.upper() == "VOLUME_IDR"
        else func.count(Order.id).label("metric_val")
    )

    query = (
        db.query(
            Order.telegram_id,
            User.username,
            User.balance_idr,
            metric_col,
        )
        .join(User, User.telegram_id == Order.telegram_id)
        .filter(or_(User.is_banned == False, User.is_banned.is_(None)), *order_filter)  # noqa: E712
        .group_by(Order.telegram_id, User.username, User.balance_idr)
        .having(metric_col >= min_value)
        .order_by(metric_col.desc())
        .limit(limit)
    )

    results = []
    for row in query.all():
        results.append({
            "telegram_id": row[0],
            "username": row[1] or f"User_{row[0]}",
            "balance_idr": row[2] or Decimal("0"),
            "metric_value": int(row[3] or 0),
        })
    return results


def simulate_campaign(
    db: Session,
    mode: str,
    total_pool: int,
    target_segment: str = "all",
    max_winners: Optional[int] = None,
    milestone_metric: str = "VOLUME_IDR",
    min_metric_value: int = 0,
    days_lookback: Optional[int] = None,
) -> dict[str, Any]:
    """
    Melakukan simulasi/dry-run pemilihan pemenang dan kalkulasi pembagian budget.
    Menjamin total distributed tidak akan melebihi total_pool (Hard Budget Cap).
    """
    if total_pool <= 0:
        raise ValueError("Total budget pool harus lebih besar dari 0.")

    mode = mode.upper()
    candidates: list[dict[str, Any]] = []

    if mode == "MILESTONE":
        limit = max_winners or 10
        top_candidates = get_top_users_by_milestone(
            db=db,
            metric=milestone_metric,
            limit=limit,
            min_value=min_metric_value,
            days=days_lookback,
        )
        candidates = top_candidates

    elif mode in ("RANDOM", "EQUAL_SPLIT"):
        users = crud.get_users_by_segment(db, target_segment)
        users = [u for u in users if not u.is_banned]

        if mode == "RANDOM":
            limit = max_winners or 10
            if len(users) > limit:
                # Cryptographically secure random sample
                chosen_users = secrets.SystemRandom().sample(users, limit)
            else:
                chosen_users = users

            candidates = [
                {
                    "telegram_id": u.telegram_id,
                    "username": u.username or f"User_{u.telegram_id}",
                    "balance_idr": u.balance_idr or Decimal("0"),
                    "metric_value": 0,
                }
                for u in chosen_users
            ]
        else:  # EQUAL_SPLIT
            candidates = [
                {
                    "telegram_id": u.telegram_id,
                    "username": u.username or f"User_{u.telegram_id}",
                    "balance_idr": u.balance_idr or Decimal("0"),
                    "metric_value": 0,
                }
                for u in users
            ]

    winner_count = len(candidates)
    if winner_count == 0:
        return {
            "eligible_count": 0,
            "winner_count": 0,
            "reward_per_winner": 0,
            "total_pool": total_pool,
            "total_distributed": 0,
            "remaining_pool": total_pool,
            "winners": [],
            "error": "Tidak ada user yang memenuhi kriteria campaign saat ini.",
        }

    # Hitung reward per user dengan pembulatan ke bawah agar tidak overspending
    reward_per_winner = total_pool // winner_count
    if reward_per_winner < 100:
        return {
            "eligible_count": winner_count,
            "winner_count": winner_count,
            "reward_per_winner": reward_per_winner,
            "total_pool": total_pool,
            "total_distributed": 0,
            "remaining_pool": total_pool,
            "winners": [],
            "error": f"Budget {format_idr(total_pool)} terlalu kecil untuk dibagi ke {winner_count} user (minimal Rp 100/user).",
        }

    total_distributed = reward_per_winner * winner_count
    remaining_pool = total_pool - total_distributed

    winners = []
    for idx, c in enumerate(candidates, start=1):
        winners.append({
            "telegram_id": c["telegram_id"],
            "username": c["username"],
            "amount": reward_per_winner,
            "rank": idx if mode == "MILESTONE" else None,
            "metric_value": c.get("metric_value", 0),
        })

    return {
        "eligible_count": winner_count,
        "winner_count": winner_count,
        "reward_per_winner": reward_per_winner,
        "total_pool": total_pool,
        "total_distributed": total_distributed,
        "remaining_pool": remaining_pool,
        "winners": winners,
        "error": None,
    }


def format_custom_notification(
    template: str,
    name: str,
    reward_amount: int,
    new_balance: Decimal,
    campaign_name: str,
    rank: Optional[int] = None,
    bot_username: str = "",
) -> str:
    """Mengganti placeholder dinamis pada template notifikasi."""
    text = template or CAMPAIGN_TEMPLATES["tpl_split_all"]["default_notif"]
    replacements = {
        "{name}": name or "Sobat Crypto",
        "{reward}": format_idr(reward_amount),
        "{new_balance}": format_idr(int(new_balance)),
        "{campaign_name}": campaign_name,
        "{rank}": str(rank) if rank else "-",
        "{bot_username}": str(bot_username) if isinstance(bot_username, str) else "",
    }
    for placeholder, val in replacements.items():
        text = text.replace(placeholder, str(val))
    return text


def execute_campaign(
    db: Session,
    campaign_id: int,
    admin_id: int,
    bot=None,
    bot_username: str = "",
) -> dict[str, Any]:
    """
    Mengeksekusi campaign secara atomic:
    1. Memverifikasi status DRAFT
    2. Menambahkan saldo user di DB
    3. Mencatat record CampaignDistribution & AuditLog
    4. Mengirim notifikasi Telegram ke tiap pemenang
    """
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise ValueError("Campaign tidak ditemukan.")

    if campaign.status != "DRAFT":
        raise ValueError(f"Campaign tidak dalam status DRAFT (status saat ini: {campaign.status}).")

    # Jalankan simulasi ulang untuk mendapatkan snapshot kandidat terkini
    tpl = get_template(campaign.template_type) if campaign.template_type else None
    days_lookback = tpl.get("days_lookback") if tpl else None
    sim = simulate_campaign(
        db=db,
        mode=campaign.mode,
        total_pool=campaign.total_pool,
        target_segment=campaign.target_segment,
        max_winners=campaign.max_winners,
        milestone_metric=campaign.milestone_metric or "VOLUME_IDR",
        min_metric_value=campaign.min_metric_value or 0,
        days_lookback=days_lookback,
    )

    if sim.get("error"):
        raise ValueError(sim["error"])

    winners = sim["winners"]
    if not winners:
        raise ValueError("Tidak ada pemenang yang dapat diproses.")

    # Proteksi Hard Budget Cap
    if sim["total_distributed"] > campaign.total_pool:
        raise ValueError("Kalkulasi distribusi melebihi batas budget yang ditentukan!")

    successful_distributions = []
    total_given = 0

    try:
        campaign.status = "RUNNING"
        db.commit()

        for w in winners:
            t_id = w["telegram_id"]
            amount = w["amount"]

            # Cek apakah sudah pernah menerima di campaign ini (idempotency guard)
            already_claimed = (
                db.query(CampaignDistribution)
                .filter_by(campaign_id=campaign.id, telegram_id=t_id)
                .first()
            )
            if already_claimed:
                continue

            user = db.query(User).filter(User.telegram_id == t_id).first()
            if not user or user.is_banned:
                continue

            # Update saldo user
            old_balance = user.balance_idr or Decimal("0")
            user.balance_idr = old_balance + Decimal(amount)
            new_balance = user.balance_idr

            # Buat record distribusi
            dist = CampaignDistribution(
                campaign_id=campaign.id,
                telegram_id=t_id,
                amount_idr=amount,
                rank=w.get("rank"),
                metric_value=w.get("metric_value"),
                status="SUCCESS",
                notified=False,
            )
            db.add(dist)

            # Audit Log
            audit = AuditLog(
                telegram_id=t_id,
                action="CAMPAIGN_REWARD",
                details=(
                    f"Campaign '{campaign.title}' (ID {campaign.id}): "
                    f"+{format_idr(amount)} (saldo: {format_idr(int(old_balance))} -> {format_idr(int(new_balance))}) "
                    f"oleh admin {admin_id}"
                ),
            )
            db.add(audit)

            total_given += amount
            successful_distributions.append({
                "telegram_id": t_id,
                "name": user.username or f"User_{t_id}",
                "amount": amount,
                "new_balance": new_balance,
                "rank": w.get("rank"),
                "dist_id": dist,
            })

        # Update status campaign menjadi COMPLETED
        campaign.status = "COMPLETED"
        campaign.distributed_amount = total_given
        campaign.distributed_count = len(successful_distributions)
        campaign.executed_at = datetime.utcnow()
        db.commit()

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal mengeksekusi campaign {campaign_id}: {e}", exc_info=True)
        campaign.status = "FAILED"
        try:
            db.commit()
        except Exception:
            pass
        raise e

    # Pengiriman notifikasi ke penerima (background asynchronous)
    notif_success = 0
    notif_fail = 0
    if bot:
        custom_msg_tpl = campaign.custom_message or (
            CAMPAIGN_TEMPLATES.get(campaign.template_type, {}).get("default_notif")
            or CAMPAIGN_TEMPLATES["tpl_split_all"]["default_notif"]
        )

        for item in successful_distributions:
            msg_text = format_custom_notification(
                template=custom_msg_tpl,
                name=item["name"],
                reward_amount=item["amount"],
                new_balance=item["new_balance"],
                campaign_name=campaign.title,
                rank=item.get("rank"),
                bot_username=bot_username,
            )
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    asyncio.create_task(
                        _notify_winner_safe(bot, item["telegram_id"], msg_text)
                    )
                notif_success += 1
            except Exception as exc:
                logger.warning("Gagal schedule notif winner %s: %s", item["telegram_id"], exc)
                notif_fail += 1

    return {
        "campaign_id": campaign.id,
        "title": campaign.title,
        "status": "COMPLETED",
        "distributed_amount": total_given,
        "distributed_count": len(successful_distributions),
        "notif_success": notif_success,
        "notif_fail": notif_fail,
    }


async def _notify_winner_safe(bot, telegram_id: int, message_text: str) -> bool:
    """Mengirim pesan notifikasi ke pemenang dengan safety try-catch."""
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=message_text,
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        logger.info(f"Tidak dapat mengirim notif campaign ke {telegram_id} (mungkin bot di-block): {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Phase 7 — Top Spender Milestone & Random Winner Execute
# ─────────────────────────────────────────────────────────────────────────────

# Tier reward Top Spender (IDR)
TOP_SPENDER_REWARDS: dict[int, int] = {
    1:  150_000,
    2:  100_000,
    3:   50_000,
    4:   30_000,
    5:   25_000,
    6:   20_000,
    7:   20_000,
    8:   20_000,
    9:   20_000,
    10:  20_000,
}


async def execute_top_spender_campaign(
    db,
    bot,
    admin_id: int,
    period_days: int = 30,
    bot_username: str = "",
) -> dict:
    """
    Eksekusi campaign Top Spender: ambil top 10 spender, kredit reward tiered.

    Flow:
    1. Ambil top 10 spenders via crud.get_top_spenders()
    2. Assign reward sesuai tier TOP_SPENDER_REWARDS
    3. Kredit saldo & buat CampaignDistribution records
    4. Kirim notifikasi ke masing-masing pemenang

    Return:
        dict: {distributed_count, total_amount, notif_success, notif_fail, winners: [...]}
    """
    from database import crud
    from database.models import User, Campaign, CampaignDistribution, AuditLog

    top_spenders = crud.get_top_spenders(db, limit=10, period_days=period_days)
    if not top_spenders:
        return {
            "distributed_count": 0,
            "total_amount": 0,
            "notif_success": 0,
            "notif_fail": 0,
            "winners": [],
            "error": "Tidak ada user yang memenuhi kriteria top spender.",
        }

    # Buat campaign record
    campaign_code = f"TOP_SPENDER_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    total_pool = sum(TOP_SPENDER_REWARDS.get(s["rank"], 0) for s in top_spenders)
    campaign = Campaign(
        campaign_code=campaign_code,
        title=f"🏆 Top Spender Milestone ({period_days}D)",
        template_type="tpl_top_spenders",
        mode="MILESTONE",
        target_segment="BUYERS",
        total_pool=total_pool,
        max_winners=len(top_spenders),
        milestone_metric="VOLUME_IDR",
        status="RUNNING",
        created_by=admin_id,
    )
    db.add(campaign)
    db.flush()  # Dapat ID campaign tanpa commit

    total_given = 0
    winners_result = []
    notif_success = 0
    notif_fail = 0

    for spender in top_spenders:
        rank = spender["rank"]
        reward = TOP_SPENDER_REWARDS.get(rank, 0)
        if reward <= 0:
            continue

        t_id = spender["telegram_id"]
        user = db.query(User).filter(User.telegram_id == t_id).first()
        if not user or user.is_banned:
            continue

        old_balance = user.balance_idr or Decimal("0")
        user.balance_idr = old_balance + Decimal(reward)
        new_balance = user.balance_idr

        dist = CampaignDistribution(
            campaign_id=campaign.id,
            telegram_id=t_id,
            amount_idr=reward,
            rank=rank,
            metric_value=spender["total_spent_idr"],
            status="SUCCESS",
            notified=False,
        )
        db.add(dist)

        audit = AuditLog(
            telegram_id=t_id,
            action="TOP_SPENDER_REWARD",
            details=(
                f"Top Spender Rank #{rank}: +{format_idr(reward)} "
                f"(volume: {format_idr(spender['total_spent_idr'])}) "
                f"oleh admin {admin_id}"
            ),
        )
        db.add(audit)
        total_given += reward

        winners_result.append({
            "rank": rank,
            "telegram_id": t_id,
            "username": spender["username"],
            "total_spent_idr": spender["total_spent_idr"],
            "reward": reward,
            "new_balance": new_balance,
        })

    campaign.status = "COMPLETED"
    campaign.distributed_amount = total_given
    campaign.distributed_count = len(winners_result)
    campaign.executed_at = datetime.utcnow()
    db.commit()

    # Kirim notifikasi ke pemenang
    tpl = CAMPAIGN_TEMPLATES["tpl_top_spenders"]["default_notif"]
    for w in winners_result:
        msg = format_custom_notification(
            template=tpl,
            name=w["username"],
            reward_amount=w["reward"],
            new_balance=w["new_balance"],
            campaign_name=campaign.title,
            rank=w["rank"],
            bot_username=bot_username,
        )
        if bot:
            sent = await _notify_winner_safe(bot, w["telegram_id"], msg)
            if sent:
                notif_success += 1
            else:
                notif_fail += 1

    logger.info(
        f"Top Spender campaign selesai: {len(winners_result)} pemenang, "
        f"total {format_idr(total_given)}"
    )

    return {
        "campaign_id": campaign.id,
        "distributed_count": len(winners_result),
        "total_amount": total_given,
        "notif_success": notif_success,
        "notif_fail": notif_fail,
        "winners": winners_result,
        "error": None,
    }


async def execute_random_winner_campaign(
    db,
    bot,
    admin_id: int,
    pool_segment: str,
    winner_count: int,
    reward_per_winner: int,
    custom_title: str = "",
    bot_username: str = "",
) -> dict:
    """
    Eksekusi undian random: pilih N pemenang dari pool, kredit saldo.

    Args:
        pool_segment: 'ALL' | 'BUYERS' | 'ACTIVE_30D'
        winner_count: Jumlah pemenang yang dipilih
        reward_per_winner: Reward per pemenang (IDR)

    Return:
        dict: {distributed_count, total_amount, notif_success, notif_fail, winners: [...]}
    """
    from database import crud
    from database.models import User, Campaign, CampaignDistribution, AuditLog

    if winner_count <= 0 or reward_per_winner <= 0:
        return {"error": "winner_count dan reward_per_winner harus > 0", "distributed_count": 0}

    candidates = crud.get_random_winners(db, pool_segment, winner_count)
    if not candidates:
        return {
            "error": f"Tidak ada user di pool segmen '{pool_segment}'.",
            "distributed_count": 0,
        }

    total_pool = len(candidates) * reward_per_winner
    title = custom_title or f"⚡ Flash Giveaway — {pool_segment} ({winner_count} Pemenang)"
    campaign_code = f"RANDOM_{pool_segment}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    campaign = Campaign(
        campaign_code=campaign_code,
        title=title,
        template_type="tpl_flash_random",
        mode="RANDOM",
        target_segment=pool_segment,
        total_pool=total_pool,
        max_winners=winner_count,
        reward_per_winner=reward_per_winner,
        status="RUNNING",
        created_by=admin_id,
    )
    db.add(campaign)
    db.flush()

    total_given = 0
    winners_result = []
    notif_success = 0
    notif_fail = 0

    for candidate in candidates:
        t_id = candidate["telegram_id"]
        user = db.query(User).filter(User.telegram_id == t_id).first()
        if not user or user.is_banned:
            continue

        old_balance = user.balance_idr or Decimal("0")
        user.balance_idr = old_balance + Decimal(reward_per_winner)
        new_balance = user.balance_idr

        dist = CampaignDistribution(
            campaign_id=campaign.id,
            telegram_id=t_id,
            amount_idr=reward_per_winner,
            status="SUCCESS",
            notified=False,
        )
        db.add(dist)

        audit = AuditLog(
            telegram_id=t_id,
            action="RANDOM_WINNER_REWARD",
            details=(
                f"Random Winner '{title}': +{format_idr(reward_per_winner)} "
                f"pool={pool_segment} oleh admin {admin_id}"
            ),
        )
        db.add(audit)
        total_given += reward_per_winner

        winners_result.append({
            "telegram_id": t_id,
            "username": candidate["username"],
            "reward": reward_per_winner,
            "new_balance": new_balance,
        })

    campaign.status = "COMPLETED"
    campaign.distributed_amount = total_given
    campaign.distributed_count = len(winners_result)
    campaign.executed_at = datetime.utcnow()
    db.commit()

    # Kirim notifikasi ke pemenang
    tpl = CAMPAIGN_TEMPLATES["tpl_flash_random"]["default_notif"]
    for w in winners_result:
        msg = format_custom_notification(
            template=tpl,
            name=w["username"],
            reward_amount=w["reward"],
            new_balance=w["new_balance"],
            campaign_name=campaign.title,
            bot_username=bot_username,
        )
        if bot:
            sent = await _notify_winner_safe(bot, w["telegram_id"], msg)
            if sent:
                notif_success += 1
            else:
                notif_fail += 1

    logger.info(
        f"Random Winner campaign selesai: {len(winners_result)} pemenang, "
        f"total {format_idr(total_given)}"
    )

    return {
        "campaign_id": campaign.id,
        "distributed_count": len(winners_result),
        "total_amount": total_given,
        "notif_success": notif_success,
        "notif_fail": notif_fail,
        "winners": winners_result,
        "error": None,
    }
