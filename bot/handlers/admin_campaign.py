"""
bot/handlers/admin_campaign.py — Interactive Admin Campaign & Giveaway Center
=============================================================================
Menyediakan wizard 1-click template, pemilihan budget instan, simulasi dry-run,
kustomisasi pesan notifikasi, dan eksekusi atomic dengan hard budget cap.
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal
from html import escape as _esc

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config.settings import settings
from database.connection import SessionLocal, engine, Base
import database.models
from database.models import Campaign, CampaignDistribution
from bot.handlers.admin import is_admin
from bot.utils.formatter import format_idr
from services.campaign_service import (
    CAMPAIGN_TEMPLATES,
    get_template,
    simulate_campaign,
    execute_campaign,
    format_custom_notification,
)

logger = logging.getLogger(__name__)

# Memastikan tabel campaign terdaftar dan dibuat bila belum ada
try:
    Base.metadata.create_all(bind=engine)
except Exception as _init_err:
    logger.warning("Auto DDL create_all campaign: %s", _init_err)


def get_campaign_main_keyboard() -> InlineKeyboardMarkup:
    """Keyboard menu utama pusat campaign."""
    keyboard = [
        [
            InlineKeyboardButton("🎁 Bagi Rata Semua User", callback_data="camp_tpl_tpl_split_all"),
        ],
        [
            InlineKeyboardButton("🛒 Loyalty Buyer Giveaway", callback_data="camp_tpl_tpl_loyalty_buyers"),
        ],
        [
            InlineKeyboardButton("🏆 Top Spender Milestone", callback_data="camp_tpl_tpl_top_spenders"),
        ],
        [
            InlineKeyboardButton("⚡ Flash Giveaway Acak", callback_data="camp_tpl_tpl_flash_random"),
        ],
        [
            InlineKeyboardButton("📋 Riwayat Campaign", callback_data="camp_history"),
            InlineKeyboardButton("🔙 Panel Utama", callback_data="admin_panel_main"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_budget_selection_keyboard(tpl_key: str) -> InlineKeyboardMarkup:
    """Keyboard pilihan budget cepat untuk template."""
    tpl = get_template(tpl_key)
    presets = tpl.get("preset_pools", [100_000, 250_000, 500_000, 1_000_000]) if tpl else [100_000, 250_000, 500_000, 1_000_000]

    row1 = [
        InlineKeyboardButton(format_idr(p), callback_data=f"camp_sim_{tpl_key}_{p}")
        for p in presets[:2]
    ]
    row2 = [
        InlineKeyboardButton(format_idr(p), callback_data=f"camp_sim_{tpl_key}_{p}")
        for p in presets[2:4]
    ]

    keyboard = [
        row1,
        row2,
        [
            InlineKeyboardButton("⌨️ Ketik Nominal Lain", callback_data=f"camp_custom_budget_{tpl_key}"),
        ],
        [
            InlineKeyboardButton("🔙 Batal / Pilih Template Lain", callback_data="admin_panel_campaign"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_preview_action_keyboard(campaign_id: int) -> InlineKeyboardMarkup:
    """Keyboard aksi pada halaman preview simulasi."""
    keyboard = [
        [
            InlineKeyboardButton("🚀 Eksekusi & Bagikan Sekarang", callback_data=f"camp_confirm_exec_{campaign_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Ubah Pesan Notifikasi", callback_data=f"camp_edit_msg_{campaign_id}"),
            InlineKeyboardButton("🔄 Ganti Budget", callback_data=f"camp_reselect_budget_{campaign_id}"),
        ],
        [
            InlineKeyboardButton("❌ Batalkan Campaign", callback_data=f"camp_cancel_{campaign_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_campaign_main_view() -> str:
    """Teks tampilan utama pusat campaign & giveaway."""
    return (
        "🎁 <b>PUSAT CAMPAIGN & GIVEAWAY BOT</b>\n\n"
        "Fitur ini memungkinkan Anda membuat event giveaway saldo bot dengan "
        "<b>proteksi batas anggaran (Hard Budget Cap)</b> dan sistem seleksi otomatis.\n\n"
        "✨ <b>Pilih Template Siap Pakai (1-Click):</b>\n"
        "1. <b>🎁 Bagi Rata Semua User</b> — Total budget dibagi sama rata ke seluruh user aktif.\n"
        "2. <b>🛒 Loyalty Buyer Giveaway</b> — Undian acak khusus pelanggan yang pernah transaksi.\n"
        "3. <b>🏆 Top Spender Milestone</b> — Reward khusus Top Trader dengan volume terbesar.\n"
        "4. <b>⚡ Flash Giveaway Acak</b> — Bagi-bagi hadiah kilat untuk sejumlah user acak.\n\n"
        "🛡️ <i>Sistem menjamin total saldo keluar tidak akan pernah melebihi budget yang Anda tetapkan.</i>"
    )


async def campaign_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler untuk command /campaign."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    text = build_campaign_main_view()
    keyboard = get_campaign_main_keyboard()

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def campaign_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router callback interaktif untuk alur campaign."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    data = query.data
    db = SessionLocal()

    try:
        # Menu utama campaign
        if data in ("admin_panel_campaign", "camp_main"):
            text = build_campaign_main_view()
            await query.edit_message_text(text, reply_markup=get_campaign_main_keyboard(), parse_mode="HTML")
            return

        # Pemilihan template -> Tampilkan tombol budget
        if data.startswith("camp_tpl_"):
            tpl_key = data.replace("camp_tpl_", "")
            tpl = get_template(tpl_key)
            if not tpl:
                try:
                    await query.answer("Template tidak ditemukan.", show_alert=True)
                except Exception:
                    pass
                return

            text = (
                f"{tpl['title']}\n\n"
                f"📝 <b>Deskripsi:</b> {tpl['description']}\n"
                f"🎯 <b>Mode:</b> <code>{tpl['mode']}</code>\n"
                f"👥 <b>Target:</b> <code>{tpl['target_segment']}</code>\n\n"
                f"💰 <b>Pilih Total Anggaran Hadiah (Pool):</b>\n"
                f"<i>Silakan klik salah satu nominal cepat di bawah:</i>"
            )
            await query.edit_message_text(text, reply_markup=get_budget_selection_keyboard(tpl_key), parse_mode="HTML")
            return

        # Pemilihan budget -> Jalankan simulasi & buat DRAFT
        if data.startswith("camp_sim_"):
            parts = data.split("_")
            # format: camp_sim_<tpl_key>_<pool>
            tpl_key = "_".join(parts[2:-1])
            pool = int(parts[-1])
            tpl = get_template(tpl_key)
            if not tpl:
                try:
                    await query.answer("Template tidak valid.", show_alert=True)
                except Exception:
                    pass
                return

            sim = simulate_campaign(
                db=db,
                mode=tpl["mode"],
                total_pool=pool,
                target_segment=tpl.get("target_segment", "all"),
                max_winners=tpl.get("default_winners"),
                milestone_metric=tpl.get("milestone_metric", "VOLUME_IDR"),
            )

            if sim.get("error"):
                text_err = (
                    f"⚠️ <b>Simulasi Campaign Dibatalkan:</b>\n\n"
                    f"{sim['error']}\n\n"
                    f"<i>Silakan pilih nominal atau template lain di bawah:</i>"
                )
                keyboard_err = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Pilih Template Lain", callback_data="admin_panel_campaign")],
                    [InlineKeyboardButton("🏠 Dashboard Utama", callback_data="admin_panel_main")],
                ])
                await query.edit_message_text(text_err, reply_markup=keyboard_err, parse_mode="HTML")
                return

            # Buat record Campaign status DRAFT
            import uuid
            code = f"CMP-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
            camp = Campaign(
                campaign_code=code,
                title=tpl["title"],
                template_type=tpl_key,
                mode=tpl["mode"],
                target_segment=tpl.get("target_segment", "all"),
                total_pool=pool,
                max_winners=tpl.get("default_winners"),
                milestone_metric=tpl.get("milestone_metric"),
                custom_message=tpl.get("default_notif"),
                status="DRAFT",
                created_by=user_id,
            )
            db.add(camp)
            db.commit()

            # Tampilkan Preview Simulasi
            text = _build_preview_text(camp, sim)
            try:
                await query.edit_message_text(
                    text,
                    reply_markup=get_preview_action_keyboard(camp.id),
                    parse_mode="HTML",
                )
            except Exception as edit_err:
                logger.warning("Gagal edit_message_text preview HTML: %s, fallback tanpa blockquote", edit_err)
                clean_text = text.replace("<blockquote>", "\n---\n").replace("</blockquote>", "\n---\n")
                await query.edit_message_text(
                    clean_text,
                    reply_markup=get_preview_action_keyboard(camp.id),
                    parse_mode="HTML",
                )
            return

        # Input budget manual
        if data.startswith("camp_custom_budget_"):
            tpl_key = data.replace("camp_custom_budget_", "")
            context.user_data["awaiting_campaign_custom_budget"] = tpl_key
            keyboard_cancel = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Batal & Pilih Budget Cepat", callback_data=f"camp_tpl_{tpl_key}")]
            ])
            await query.edit_message_text(
                "⌨️ <b>Ketik Nominal Anggaran:</b>\n\n"
                "Kirim pesan angka nominal total hadiah yang ingin dibagikan.\n"
                "Contoh: <code>750000</code> atau <code>1500000</code>\n\n"
                "<i>Minimal: Rp 1.000</i>",
                reply_markup=keyboard_cancel,
                parse_mode="HTML",
            )
            return

        # Ubah Pesan Notifikasi Custom
        if data.startswith("camp_edit_msg_"):
            camp_id = int(data.replace("camp_edit_msg_", ""))
            camp = db.query(Campaign).filter_by(id=camp_id).first()
            if not camp:
                await query.answer("Campaign tidak ditemukan.", show_alert=True)
                return

            context.user_data["awaiting_campaign_custom_msg_id"] = camp_id
            keyboard_cancel_msg = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Batal (Tetap Pakai Pesan Lama)", callback_data=f"camp_back_preview_{camp_id}")]
            ])
            await query.edit_message_text(
                "✏️ <b>Kustomisasi Pesan Notifikasi Pemenang</b>\n\n"
                "Ketik dan kirim teks notifikasi baru yang akan diterima user.\n"
                "Anda dapat menggunakan placeholder berikut:\n"
                "• <code>{name}</code> — Nama / username penerima\n"
                "• <code>{reward}</code> — Nominal hadiah (contoh: Rp 10.000)\n"
                "• <code>{new_balance}</code> — Saldo baru user setelah klaim\n"
                "• <code>{campaign_name}</code> — Judul campaign\n"
                "• <code>{rank}</code> — Peringkat (khusus milestone)\n\n"
                "<i>Kirim pesan sekarang melalui chat ini...</i>",
                reply_markup=keyboard_cancel_msg,
                parse_mode="HTML",
            )
            return

        # Kembali ke Preview Simulasi
        if data.startswith("camp_back_preview_"):
            camp_id = int(data.replace("camp_back_preview_", ""))
            context.user_data.pop("awaiting_campaign_custom_msg_id", None)
            camp = db.query(Campaign).filter_by(id=camp_id).first()
            if not camp:
                await query.answer("Campaign tidak ditemukan.", show_alert=True)
                return

            sim = simulate_campaign(
                db=db,
                mode=camp.mode,
                total_pool=camp.total_pool,
                target_segment=camp.target_segment,
                max_winners=camp.max_winners,
                milestone_metric=camp.milestone_metric or "VOLUME_IDR",
            )
            text = _build_preview_text(camp, sim)
            await query.edit_message_text(
                text,
                reply_markup=get_preview_action_keyboard(camp.id),
                parse_mode="HTML",
            )
            return

        # Ganti Budget pada Draft
        if data.startswith("camp_reselect_budget_"):
            camp_id = int(data.replace("camp_reselect_budget_", ""))
            camp = db.query(Campaign).filter_by(id=camp_id).first()
            if camp:
                if camp.status == "DRAFT":
                    camp.status = "CANCELLED"
                    db.commit()
                await query.edit_message_text(
                    "💰 <b>Pilih Ulang Anggaran Hadiah:</b>",
                    reply_markup=get_budget_selection_keyboard(camp.template_type),
                    parse_mode="HTML",
                )
            return

        # Batalkan Draft Campaign
        if data.startswith("camp_cancel_"):
            camp_id = int(data.replace("camp_cancel_", ""))
            camp = db.query(Campaign).filter_by(id=camp_id).first()
            if camp and camp.status == "DRAFT":
                camp.status = "CANCELLED"
                db.commit()
            await query.edit_message_text(
                "❌ Campaign telah dibatalkan.",
                reply_markup=get_campaign_main_keyboard(),
                parse_mode="HTML",
            )
            return

        # Konfirmasi Eksekusi
        if data.startswith("camp_confirm_exec_"):
            camp_id = int(data.replace("camp_confirm_exec_", ""))
            camp = db.query(Campaign).filter_by(id=camp_id).first()
            if not camp:
                await query.answer("Campaign tidak ditemukan.", show_alert=True)
                return

            await query.edit_message_text(
                f"⏳ <b>SEDANG MEMPROSES CAMPAIGN...</b>\n\n"
                f"Event: <b>{camp.title}</b>\n"
                f"Mengalokasikan saldo ke masing-masing akun user dan mengirim notifikasi...\n"
                f"<i>Mohon tunggu sebentar.</i>",
                parse_mode="HTML",
            )

            bot_user = await context.bot.get_me()
            bot_username = bot_user.username or ""

            result = execute_campaign(
                db=db,
                campaign_id=camp.id,
                admin_id=user_id,
                bot=context.bot,
                bot_username=bot_username,
            )

            text_done = (
                f"🎉 <b>CAMPAIGN BERHASIL DISELESAIKAN!</b>\n\n"
                f"🎁 <b>Event:</b> {result['title']}\n"
                f"👥 <b>Penerima:</b> <code>{result['distributed_count']} user</code>\n"
                f"💸 <b>Total Terdistribusi:</b> <code>{format_idr(result['distributed_amount'])}</code>\n"
                f"📨 <b>Notifikasi Telegram:</b> <code>{result['notif_success']} terkirim</code>\n"
                f"🛡️ <b>Budget Cap:</b> Terpenuhi aman (tidak overbudget).\n\n"
                f"<i>Saldo penerima sudah aktif di akun bot masing-masing dan siap dipakai untuk transaksi.</i>"
            )
            keyboard_done = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Buat Campaign Lagi", callback_data="admin_panel_campaign")],
                [InlineKeyboardButton("🔙 Panel Utama Admin", callback_data="admin_panel_main")],
            ])
            await query.edit_message_text(text_done, reply_markup=keyboard_done, parse_mode="HTML")
            return

        # Riwayat Campaign
        if data == "camp_history":
            campaigns = db.query(Campaign).order_by(Campaign.id.desc()).limit(10).all()
            if not campaigns:
                text_hist = "📋 <b>Riwayat Campaign:</b>\n\nBelum ada riwayat campaign yang dibuat."
            else:
                lines = ["📋 <b>10 CAMPAIGN TERAKHIR:</b>\n"]
                for c in campaigns:
                    status_icon = "✅" if c.status == "COMPLETED" else ("⏳" if c.status == "DRAFT" else "❌")
                    lines.append(
                        f"{status_icon} <b>{_esc(c.title)}</b> (ID: <code>{c.id}</code>)\n"
                        f"   💰 Budget: <code>{format_idr(c.total_pool)}</code> | "
                        f"Penerima: <code>{c.distributed_count} user</code>\n"
                        f"   Status: <code>{c.status}</code> | Tgl: <code>{c.created_at.strftime('%d/%m/%Y')}</code>\n"
                    )
                text_hist = "\n".join(lines)

            keyboard_hist = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Buat Campaign Baru", callback_data="admin_panel_campaign")],
                [InlineKeyboardButton("🔙 Panel Utama", callback_data="admin_panel_main")],
            ])
            await query.edit_message_text(text_hist, reply_markup=keyboard_hist, parse_mode="HTML")
            return

    except Exception as exc:
        logger.error(f"Error campaign_callback_handler: {exc}", exc_info=True)
        try:
            await query.edit_message_text(
                f"⚠️ <b>Terjadi kendala pada sistem campaign:</b>\n\n"
                f"<code>{_esc(str(exc))}</code>\n\n"
                f"<i>Silakan pilih menu di bawah untuk melanjutkan:</i>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎁 Menu Campaign", callback_data="admin_panel_campaign")],
                    [InlineKeyboardButton("🏠 Dashboard Utama", callback_data="admin_panel_main")],
                ]),
                parse_mode="HTML",
            )
        except Exception:
            pass
    finally:
        db.close()


async def campaign_text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Menangani input teks admin untuk custom budget atau custom notification message.
    Return True bila pesan ditangani di sini.
    """
    message = update.message
    if not message or not message.text:
        return False

    user_id = update.effective_user.id
    if not is_admin(user_id):
        return False

    raw_text = message.text.strip()
    db = SessionLocal()

    try:
        # Menangani input budget manual
        if "awaiting_campaign_custom_budget" in context.user_data:
            tpl_key = context.user_data.pop("awaiting_campaign_custom_budget")
            tpl = get_template(tpl_key)
            if not tpl:
                return False

            clean_digits = "".join(ch for ch in raw_text if ch.isdigit())
            if not clean_digits or int(clean_digits) < 1000:
                await message.reply_text("⚠️ Nominal tidak valid. Masukkan minimal Rp 1.000.")
                return True

            pool = int(clean_digits)
            sim = simulate_campaign(
                db=db,
                mode=tpl["mode"],
                total_pool=pool,
                target_segment=tpl.get("target_segment", "all"),
                max_winners=tpl.get("default_winners"),
                milestone_metric=tpl.get("milestone_metric", "VOLUME_IDR"),
            )

            if sim.get("error"):
                await message.reply_text(f"⚠️ {sim['error']}")
                return True

            import uuid
            code = f"CMP-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
            camp = Campaign(
                campaign_code=code,
                title=tpl["title"],
                template_type=tpl_key,
                mode=tpl["mode"],
                target_segment=tpl.get("target_segment", "all"),
                total_pool=pool,
                max_winners=tpl.get("default_winners"),
                milestone_metric=tpl.get("milestone_metric"),
                custom_message=tpl.get("default_notif"),
                status="DRAFT",
                created_by=user_id,
            )
            db.add(camp)
            db.commit()

            text = _build_preview_text(camp, sim)
            await message.reply_text(
                text,
                reply_markup=get_preview_action_keyboard(camp.id),
                parse_mode="HTML",
            )
            return True

        # Menangani input custom notification message
        if "awaiting_campaign_custom_msg_id" in context.user_data:
            camp_id = context.user_data.pop("awaiting_campaign_custom_msg_id")
            camp = db.query(Campaign).filter_by(id=camp_id).first()
            if not camp:
                await message.reply_text("⚠️ Campaign tidak ditemukan.")
                return True

            camp.custom_message = raw_text
            db.commit()

            sim = simulate_campaign(
                db=db,
                mode=camp.mode,
                total_pool=camp.total_pool,
                target_segment=camp.target_segment,
                max_winners=camp.max_winners,
                milestone_metric=camp.milestone_metric or "VOLUME_IDR",
            )

            text = _build_preview_text(camp, sim)
            await message.reply_text(
                "✅ <b>Pesan notifikasi berhasil diperbarui!</b>\n\n" + text,
                reply_markup=get_preview_action_keyboard(camp.id),
                parse_mode="HTML",
            )
            return True

    finally:
        db.close()

    return False


def _build_preview_text(camp: Campaign, sim: dict) -> str:
    """Membangun teks preview simulasi campaign sebelum dieksekusi."""
    notif_sample = format_custom_notification(
        template=camp.custom_message,
        name="UserPemenang",
        reward_amount=sim["reward_per_winner"],
        new_balance=Decimal("25000"),
        campaign_name=camp.title,
        rank=1,
        bot_username=settings.OWNER_USERNAME,
    )

    metric_info = f" ({camp.milestone_metric})" if camp.milestone_metric else ""
    winners_preview = []
    for w in sim["winners"][:5]:
        rank_str = f"#{w['rank']} " if w.get("rank") else ""
        metric_str = f" (Vol: {format_idr(w['metric_value'])})" if w.get("metric_value") else ""
        winners_preview.append(f"  • {rank_str}<b>{_esc(w['username'])}</b> — <code>+{format_idr(w['amount'])}</code>{metric_str}")

    more_str = f"\n  <i>...dan {len(sim['winners']) - 5} pemenang lainnya.</i>" if len(sim["winners"]) > 5 else ""

    return (
        f"📊 <b>PREVIEW CAMPAIGN: {camp.title}</b>\n\n"
        f"🎯 <b>Mode:</b> <code>{camp.mode}{metric_info}</code>\n"
        f"👥 <b>Target Segmen:</b> <code>{camp.target_segment}</code>\n"
        f"👥 <b>Total Pemenang:</b> <code>{sim['winner_count']} user</code>\n"
        f"💵 <b>Hadiah per User:</b> <code>{format_idr(sim['reward_per_winner'])}</code>\n"
        f"💰 <b>Total Anggaran Keluar:</b> <code>{format_idr(sim['total_distributed'])}</code>\n"
        f"🛡️ <b>Maksimal Anggaran (Cap):</b> <code>{format_idr(camp.total_pool)}</code>\n\n"
        f"📋 <b>Daftar Pemenang Terpilih:</b>\n"
        f"{''.join(winners_preview)}{more_str}\n\n"
        f"✉️ <b>Pratinjau Notifikasi ke Pemenang:</b>\n"
        f"<blockquote>{notif_sample}</blockquote>\n\n"
        f"<i>Klik '🚀 Eksekusi' untuk langsung membagikan saldo ke akun user, atau edit pesan notifikasi di bawah.</i>"
    )
