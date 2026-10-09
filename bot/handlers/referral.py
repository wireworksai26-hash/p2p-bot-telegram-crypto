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
    get_referrer_rank,
    get_referral_discount_info,
)
from database.models import User
from bot.utils.formatter import format_idr, mask_public_name
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
        "• Withdraw: maks. Rp 100.000 per hari, syarat sudah menyelesaikan 1 transaksi "
        "(beli/jual/convert), biaya gratis selama promo\n"
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
        bot_username = (await context.bot.get_me()).username if context.bot else "TokoKoinID_bot"
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
        viewer_id = update.effective_user.id
        top = get_top_referrers(db, limit=10)

        lines = ["🏆 <b>LEADERBOARD REFERRAL</b>\n"]
        if top:
            for i, row in enumerate(top, 1):
                user = db.query(User).filter(User.telegram_id == row.referrer_id).first()
                raw_name = (user.username or user.full_name) if user else None
                # Nama publik disensor (Ox***un) tanpa '@' supaya tidak bisa diklik/di-scrape.
                name = _esc(mask_public_name(raw_name or row.referrer_id))
                total_reward = row.total_reward or 0
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                you = " 👈 <i>Kamu</i>" if row.referrer_id == viewer_id else ""
                lines.append(
                    f"{medal} {name} — <b>{row.total}</b> referral "
                    f"({format_idr(total_reward)}){you}"
                )
        else:
            lines.append("<i>Belum ada data referral.</i>")

        me = get_referrer_rank(db, viewer_id)
        if me["rank"] is not None and me["rank"] <= 10:
            rank_text = f"#{me['rank']}"
        elif me["rank"] is not None:
            rank_text = f"Belum Masuk Top 10 (#{me['rank']})"
        else:
            rank_text = "Belum Masuk Top 10"
        lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("📍 <b>Posisi Anda Saat Ini:</b>")
        lines.append(
            f"Peringkat: <b>{rank_text}</b> | Total: <b>{me['total']}</b> Referral "
            f"({format_idr(me['total_reward'])})"
        )
        lines.append("\n📢 <i>Bagikan link referral Anda untuk mendaki leaderboard</i>")

        keyboard = []
        try:
            bot_username = (await context.bot.get_me()).username
            share_text = (f"Yuk beli dan jual crypto mudah, cepat & terpercaya di HSN Store! "
                          f"Daftar lewat link ini ya:\nhttps://t.me/{bot_username}?start=ref_{viewer_id}")
            keyboard.append([InlineKeyboardButton(
                "📤 Bagikan Link ke Teman", url=f"https://t.me/share/url?url={quote(share_text)}")])
        except Exception as exc:
            logger.debug("Tombol bagikan leaderboard dilewati: %s", exc)
        keyboard += [
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
