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
    get_referral_config,
    get_top_referrers,
)
from database.models import User
from bot.utils.formatter import format_idr

logger = logging.getLogger(__name__)


async def referral_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan menu referral untuk user (command /referral atau callback menu_referral)."""
    query = update.callback_query
    user = update.effective_user

    db = SessionLocal()
    try:
        stats = get_referral_stats(db, user.id)

        # Get reward config
        reward_str = get_referral_config(db, "reward_per_referral")
        reward_idr = int(reward_str) if reward_str else 5000

        # Get referee bonus/discount config
        bonus_str = get_referral_config(db, "referee_discount_idr")
        bonus_idr = int(bonus_str) if bonus_str else 0

        # Get minimum trade config
        min_trade_str = get_referral_config(db, "min_trade_amount_idr")
        min_trade_idr = int(min_trade_str) if min_trade_str else 0

        # Build referral link
        bot_username = (await context.bot.get_me()).username if context.bot else "Hsnpro_bot"
        ref_link = f"https://t.me/{bot_username}?start=ref_{user.id}"

        bonus_line = ""
        if bonus_idr > 0:
            bonus_line = f"• Teman Anda juga mendapat cashback/potongan <b>{format_idr(bonus_idr)}</b> di transaksi pertamanya!\n"

        min_trade_line = ""
        if min_trade_idr > 0:
            min_trade_line = f"• Syarat pencairan: Teman menyelesaikan pembelian minimal <b>{format_idr(min_trade_idr)}</b>.\n"

        text = (
            f"🔗 <b>PROGRAM REFERRAL HSN STORE</b>\n\n"
            f"🎁 <b>Link Referral Anda:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"📊 <b>Statistik Referral Anda:</b>\n"
            f"├── 👥 Total Ajakan   : <b>{stats['total']} orang</b>\n"
            f"├── ✅ Selesai Trade   : <b>{stats['completed']} orang</b>\n"
            f"├── ⏳ Belum Selesai   : <b>{stats['pending']} orang</b>\n"
            f"└── 💰 Total Reward    : <b>{format_idr(stats['total_reward'])}</b>\n\n"
            f"💡 <i>Bagikan link di atas ke teman Anda:\n"
            f"• Anda mendapat reward <b>{format_idr(reward_idr)}</b> untuk setiap teman yang menyelesaikan transaksi pertama!\n"
            f"{bonus_line}"
            f"{min_trade_line}</i>"
        )

        share_msg = f"Yuk beli dan jual crypto mudah, cepat & terpercaya di HSN Store! Daftar lewat link ini ya: {ref_link}"
        share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_msg)}"

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
