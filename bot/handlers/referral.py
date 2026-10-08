"""
bot/handlers/referral.py — User-Facing Referral Program Menu
=============================================================
Menampilkan link referral, statistik, dan leaderboard.
"""

import logging
from html import escape as _esc
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import settings
from database.connection import SessionLocal
from database.crud import (
    get_referral_stats,
    get_top_referrers,
    get_referral_discount_info,
)
from database.models import User
from bot.utils.formatter import format_idr
from database.crud import WITHDRAW_MIN_IDR
from services.referral_rewards import get_referral_settings

logger = logging.getLogger(__name__)


def build_referral_rules_text(cfg: dict) -> str:
    """Ketentuan program referral untuk user, diambil dari pengaturan admin (bukan teks tetap)."""
    hold = f"melewati masa tahan {cfg['hold']} jam" if cfg["hold"] else "tanpa masa tahan"
    return (
        "👥 <b>Mengundang Teman</b>\n"
        f"• {format_idr(cfg['reward'])} saat teman menyelesaikan transaksi pertama (beli/jual/convert)\n"
        f"• {format_idr(cfg['reward2'])} saat teman menyelesaikan transaksi kedua\n\n"
        "🎉 <b>Untuk Teman yang Diundang</b>\n"
        f"• Diskon fee {format_idr(cfg['bonus'])} di transaksi pertama\n\n"
        "💸 <b>Bonus dari Transaksi Teman</b>\n"
        f"• {cfg['share']:g}% dari fee setiap transaksi temanmu masuk ke saldo bot kamu "
        f"(maks. {cfg['sharemax']} transaksi teman)\n\n"
        "💰 <b>Saldo Referral</b>\n"
        "• Masuk ke saldo bot: bisa untuk transaksi atau ditarik ke rekening/e-wallet "
        f"(min. {format_idr(WITHDRAW_MIN_IDR)})\n"
        f"• Reward masuk setelah transaksi selesai dan {hold}"
    )


async def referral_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan menu referral untuk user (command /referral atau callback menu_referral)."""
    query = update.callback_query
    user = update.effective_user

    db = SessionLocal()
    try:
        stats = get_referral_stats(db, user.id)
        cfg = get_referral_settings(db)
        disc_info = get_referral_discount_info(db, user.id)

        # Build referral link
        bot_username = (await context.bot.get_me()).username if context.bot else "Hsnpro_bot"
        ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"

        # Discount status block
        if disc_info.get("active"):
            discount_status_text = (
                f"🎁 <b>Status Diskon Transaksi Anda:</b>\n"
                f"├── Status : ✅ <b>AKTIF</b>\n"
                f"├── Diskon : <b>{disc_info['discount_pct']:.0f}%</b> dari biaya transaksi\n"
                f"└── Sisa   : <b>{disc_info['remaining']} transaksi</b>\n"
                f"💡 <i>Diskon otomatis diterapkan saat Anda membuat order.</i>\n\n"
            )
        else:
            # Diskon 10x transaksi untuk pengundang sudah diganti bagi hasil fee (lihat ketentuan di bawah).
            discount_status_text = ""

        held_note = (
            f" <i>({format_idr(stats['held_reward'])} menunggu masa tahan)</i>" if stats.get("held_reward") else ""
        )
        text = (
            f"🔗 <b>PROGRAM REFERRAL HSN STORE</b>\n\n"
            f"🎁 <b>Link Referral Anda:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"📊 <b>Statistik Referral Anda:</b>\n"
            f"├── 👥 Total Ajakan   : <b>{stats['total']} orang</b>\n"
            f"├── ✅ Selesai Trade   : <b>{stats['completed']} orang</b>\n"
            f"├── ⏳ Belum Selesai   : <b>{stats['pending']} orang</b>\n"
            f"└── 💰 Total Reward    : <b>{format_idr(stats['total_reward'])}</b>{held_note}\n\n"
            f"{discount_status_text}"
            f"{build_referral_rules_text(cfg)}"
        )

        share_text = f"Yuk beli dan jual crypto mudah, cepat & terpercaya di HSN Store! Daftar lewat link ini ya:\n{ref_link}"
        share_url = f"https://t.me/share/url?url={quote(share_text)}"

        keyboard = [
            [InlineKeyboardButton("📤 Bagikan Link ke Teman", url=share_url)],
            [InlineKeyboardButton("🏆 Leaderboard", callback_data="referral_leaderboard")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_back")],
        ]

        if query:
            try:
                await query.edit_message_text(
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML",
                )
            except Exception:
                await query.message.reply_text(
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML",
                )
        else:
            await update.message.reply_text(
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"Error referral_menu_handler: {e}", exc_info=True)
        msg = "⚠️ Terjadi kesalahan saat memuat menu referral."
        if query:
            await query.message.reply_text(msg)
        else:
            await update.message.reply_text(msg)
    finally:
        db.close()


async def referral_leaderboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan leaderboard top referrers."""
    query = update.callback_query
    if query:
        await query.answer()

    db = SessionLocal()
    try:
        top = get_top_referrers(db, limit=10)

        lines = ["🏆 <b>LEADERBOARD REFERRAL</b>\n"]
        if top:
            for i, row in enumerate(top, 1):
                user = db.query(User).filter(User.telegram_id == row.referrer_id).first()
                name = f"@{user.username}" if user and user.username else str(row.referrer_id)
                total_reward = row.total_reward or 0
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                lines.append(
                    f"{medal} {name} — <b>{row.total}</b> referral "
                    f"({format_idr(total_reward)})"
                )
        else:
            lines.append("<i>Belum ada data referral.</i>")

        keyboard = [
            [InlineKeyboardButton("🔙 Kembali", callback_data="menu_referral")],
            [InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_back")],
        ]

        text = "\n".join(lines)
        if query:
            try:
                await query.edit_message_text(
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML",
                )
            except Exception:
                await query.message.reply_text(
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML",
                )
        else:
            await update.message.reply_text(
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"Error referral_leaderboard: {e}", exc_info=True)
    finally:
        db.close()
