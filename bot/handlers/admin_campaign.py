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
from database import crud
from bot.handlers.admin import is_admin
from bot.utils.formatter import format_idr
from bot.utils.emojis import tg_emoji
from services.campaign_service import (
    CAMPAIGN_TEMPLATES,
    get_template,
    simulate_campaign,
    execute_campaign,
    format_custom_notification,
    TreasuryInsufficient,
)

logger = logging.getLogger(__name__)

# Memastikan tabel campaign terdaftar dan dibuat bila belum ada
try:
    Base.metadata.create_all(bind=engine)
except Exception as _init_err:
    logger.warning("Auto DDL create_all campaign: %s", _init_err)

async def _safe_edit(query, text: str, reply_markup=None, parse_mode="HTML", **kwargs):
    """Mengedit pesan dengan fallback sanitasi HTML jika Telegram menolak."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except Exception as err:
        err_str = str(err).lower()
        if "document_invalid" in err_str or "can't parse entities" in err_str:
            clean_text = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', text)
            clean_text = clean_text.replace("<blockquote expandable>", "\n---\n").replace("<blockquote>", "\n---\n").replace("</blockquote>", "\n---\n")
            await query.edit_message_text(clean_text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
        else:
            raise



def get_campaign_main_keyboard() -> InlineKeyboardMarkup:
    """Keyboard menu utama pusat campaign, giveaway, dan loyalty."""
    keyboard = [
        [
            InlineKeyboardButton("🎁 Bagi Rata Buyer Aktif", callback_data="camp_tpl_tpl_split_all"),
        ],
        [
            InlineKeyboardButton("🛒 Loyalty Buyer Reward", callback_data="camp_tpl_tpl_loyalty_buyers"),
        ],
        [
            InlineKeyboardButton("🏆 Top Spender Leaderboard", callback_data="admin_panel_top_spenders"),
            InlineKeyboardButton("🎲 Undi Pemenang Acak", callback_data="admin_panel_random_draw"),
        ],
        [
            InlineKeyboardButton("⏳ Pengaturan Loyalty Reward", callback_data="admin_panel_loyalty"),
            InlineKeyboardButton("⚡ Flash Giveaway Acak", callback_data="camp_tpl_tpl_flash_random"),
        ],
        [
            InlineKeyboardButton("🎁 Kirim Reward ke User Pilihan", callback_data="admin_panel_reward"),
        ],
        [
            InlineKeyboardButton("🏦 Dompet & Kas Bot", callback_data="camp_treasury_view"),
            InlineKeyboardButton("📋 Riwayat Campaign", callback_data="camp_history"),
        ],
        [
            InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main"),
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


def build_campaign_main_view(db=None) -> str:
    """Teks tampilan utama pusat campaign & giveaway."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        treasury_bal = crud.get_bot_treasury_balance(db)
    except Exception:
        treasury_bal = 0
    finally:
        if close_db:
            db.close()

    return (
        "🎁 <b>PUSAT CAMPAIGN & GIVEAWAY BOT (LOYALTY HUB)</b>\n\n"
        f"🏦 <b>Saldo Kas Dompet Bot:</b> <code>{format_idr(treasury_bal)}</code>\n\n"
        "Pusat pengelolaan event giveaway saldo bot, ranking Top Spender, "
        "undian acak, dan program loyalty otomatis dengan <b>proteksi batas anggaran (Hard Budget Cap)</b>.\n\n"
        "✨ <b>Menu & Template Event Siap Pakai:</b>\n"
        "1. <b>🎁 Bagi Rata Buyer Aktif</b> — Total budget dibagi sama rata ke user pembeli aktif.\n"
        "2. <b>🛒 Loyalty Buyer Reward</b> — Reward untuk pelanggan setia yang mencapai target transaksi dalam rentang waktu tertentu.\n"
        "3. <b>🏆 Top Spender Leaderboard</b> — Reward leaderboard untuk Top Trader dengan volume terbesar.\n"
        "4. <b>🎲 Undi Pemenang Acak</b> — Undi pemenang instan per kategori target user.\n"
        "5. <b>⏳ Pengaturan Loyalty Reward</b> — Reward otomatis per X transaksi dalam window waktu.\n"
        "6. <b>⚡ Flash Giveaway Acak</b> — Bagi-bagi saldo kilat untuk sejumlah user acak.\n\n"
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
        await _safe_edit(update.callback_query, text, reply_markup=keyboard, parse_mode="HTML")
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
            await _safe_edit(query, text, reply_markup=get_campaign_main_keyboard(), parse_mode="HTML")
            return

        # Dompet & Kas Bot
        if data in ("camp_treasury_view", "admin_panel_treasury"):
            from bot.handlers.admin import build_admin_treasury_view
            text, markup = build_admin_treasury_view(
                db, admin_id=user_id, chat_id=query.message.chat_id,
                message_id=query.message.message_id,
            )
            await _safe_edit(query, text=text, reply_markup=markup, parse_mode="HTML")
            return

        # Top Spender Leaderboard
        if data == "admin_panel_top_spenders" or data.startswith("admin_top_spender_p_"):
            from bot.handlers.admin import build_admin_top_spenders_view, build_admin_top_spenders_keyboard, top_spender_funding
            period = 30
            if data.startswith("admin_top_spender_p_"):
                try:
                    period = int(data.replace("admin_top_spender_p_", ""))
                except Exception:
                    period = 30
            text = build_admin_top_spenders_view(db, period_days=period)
            markup = build_admin_top_spenders_keyboard(
                period_days=period, shortfall=top_spender_funding(db, period)[2],
                db=db, admin_id=user_id, chat_id=query.message.chat_id,
                message_id=query.message.message_id,
            )
            await _safe_edit(query, text=text, reply_markup=markup, parse_mode="HTML")
            return

        # Routing ke handler admin_panel untuk Undi Pemenang, Loyalty Setting, atau Dashboard Utama
        if data in ("admin_panel_random_draw", "admin_panel_loyalty", "admin_panel_main") or data.startswith("admin_draw_") or data.startswith("admin_loyalty_"):
            from bot.handlers.admin import admin_panel_callback
            await admin_panel_callback(update, context)
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
            await _safe_edit(query, text, reply_markup=get_budget_selection_keyboard(tpl_key), parse_mode="HTML")
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
                days_lookback=tpl.get("days_lookback"),
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
                await _safe_edit(query, text_err, reply_markup=keyboard_err, parse_mode="HTML")
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
                await _safe_edit(query, 
                    text,
                    reply_markup=get_preview_action_keyboard(camp.id),
                    parse_mode="HTML",
                )
            except Exception as edit_err:
                logger.warning("Gagal edit_message_text preview HTML: %s, fallback tanpa blockquote", edit_err)
                clean_text = text.replace("<blockquote>", "\n---\n").replace("</blockquote>", "\n---\n")
                await _safe_edit(query, 
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
            await _safe_edit(query, 
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
                [InlineKeyboardButton("📋 Pakai Template Standar Bawaan", callback_data=f"camp_set_default_msg_{camp_id}")],
                [InlineKeyboardButton("🔙 Batal (Tetap Pakai Pesan Lama)", callback_data=f"camp_back_preview_{camp_id}")],
            ])
            edit_prompt_text = (
                "✏️ <b>KUSTOMISASI PESAN NOTIFIKASI PEMENANG</b>\n\n"
                "Pesan ini akan otomatis dikirimkan bot langsung ke DM Telegram setiap pemenang saat tombol <b>Eksekusi</b> ditekan.\n\n"
                "📌 <b>Daftar Tag / Placeholder Otomatis:</b>\n"
                "• <code>{name}</code> ➔ Nama / username pemenang (contoh: <i>@budi</i>)\n"
                "• <code>{reward}</code> ➔ Nominal hadiah yang didapat (contoh: <i>Rp 25.000</i>)\n"
                "• <code>{new_balance}</code> ➔ Total saldo akun baru user (contoh: <i>Rp 75.000</i>)\n"
                "• <code>{campaign_name}</code> ➔ Judul event campaign ini\n"
                "• <code>{rank}</code> ➔ Posisi juara (khusus event Top Spender / Rank)\n"
                "• <code>{bot_username}</code> ➔ Username bot Anda (contoh: <i>@TokoKoinID_Bot</i>)\n\n"
                "📋 <b>Contoh Template Siap Copy (Salin & Edit Sesuai Selera):</b>\n"
                "<code>🎉 <b>SELAMAT {name}!</b>\n\n"
                "Anda terpilih memenangkan hadiah saldo gratis dari event <b>{campaign_name}</b>!\n\n"
                "💰 <b>Hadiah:</b> {reward}\n"
                "💳 <b>Saldo Baru Anda:</b> {new_balance}\n\n"
                "Saldo sudah aktif dan siap langsung digunakan untuk transaksi di @{bot_username} 🚀</code>\n\n"
                "👇 <i>Ketik pesan notifikasi kustom Anda sekarang di chat ini...</i>"
            )
            await _safe_edit(query, 
                edit_prompt_text,
                reply_markup=keyboard_cancel_msg,
                parse_mode="HTML",
            )
            return

        # Kembalikan ke Template Notifikasi Default
        if data.startswith("camp_set_default_msg_"):
            camp_id = int(data.replace("camp_set_default_msg_", ""))
            context.user_data.pop("awaiting_campaign_custom_msg_id", None)
            camp = db.query(Campaign).filter_by(id=camp_id).first()
            if camp:
                tpl = get_template(camp.template_type) or get_template("tpl_split_all")
                camp.custom_message = tpl.get("default_notif") if tpl else None
                db.commit()
                await query.answer("✅ Pesan notifikasi dikembalikan ke template standar bawaan.", show_alert=True)
                tpl = get_template(camp.template_type) if camp.template_type else None
                sim = simulate_campaign(
                    db=db,
                    mode=camp.mode,
                    total_pool=camp.total_pool,
                    target_segment=camp.target_segment,
                    max_winners=camp.max_winners,
                    milestone_metric=camp.milestone_metric or "VOLUME_IDR",
                    days_lookback=tpl.get("days_lookback") if tpl else None,
                )
                text = _build_preview_text(camp, sim)
                await _safe_edit(query, 
                    text,
                    reply_markup=get_preview_action_keyboard(camp.id),
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

            tpl = get_template(camp.template_type) if camp.template_type else None
            sim = simulate_campaign(
                db=db,
                mode=camp.mode,
                total_pool=camp.total_pool,
                target_segment=camp.target_segment,
                max_winners=camp.max_winners,
                milestone_metric=camp.milestone_metric or "VOLUME_IDR",
                days_lookback=tpl.get("days_lookback") if tpl else None,
            )
            text = _build_preview_text(camp, sim)
            await _safe_edit(query, 
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
                await _safe_edit(query, 
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
            await _safe_edit(query, 
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

            await _safe_edit(query, 
                f"⏳ <b>SEDANG MEMPROSES CAMPAIGN...</b>\n\n"
                f"Event: <b>{camp.title}</b>\n"
                f"Mengalokasikan saldo ke masing-masing akun user dan mengirim notifikasi...\n"
                f"<i>Mohon tunggu sebentar.</i>",
                parse_mode="HTML",
            )

            bot_username = ""
            try:
                bot_user = await context.bot.get_me()
                if hasattr(bot_user, "username") and isinstance(bot_user.username, str):
                    bot_username = bot_user.username
            except Exception:
                bot_username = ""

            try:
                result = execute_campaign(
                    db=db,
                    campaign_id=camp.id,
                    admin_id=user_id,
                    bot=context.bot,
                    bot_username=bot_username,
                )
            except TreasuryInsufficient as short:
                # Draft tetap utuh; admin isi Kas Bot lalu kembali ke preview yang sama.
                await _safe_edit(query, 
                    f"⚠️ <b>KAS BOT KURANG</b>\n\n"
                    f"Event: <b>{_esc(camp.title)}</b>\n"
                    f"💰 Dana dibutuhkan: <code>{format_idr(short.needed)}</code>\n"
                    f"🏦 Saldo Kas Bot: <code>{format_idr(short.balance)}</code>\n"
                    f"❗ Kurang: <b>{format_idr(short.shortfall)}</b>\n\n"
                    f"Hadiah milestone dibayar dari Kas Bot. Isi dulu via QRIS, lalu kembali ke preview.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📲 Isi Kas Bot via QRIS (Uang Asli)", callback_data="admin_treasury_qris_menu")],
                        [InlineKeyboardButton("🔙 Kembali ke Preview", callback_data=f"camp_back_preview_{camp.id}")],
                    ]),
                    parse_mode="HTML",
                )
                return

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
            await _safe_edit(query, text_done, reply_markup=keyboard_done, parse_mode="HTML")
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
            await _safe_edit(query, text_hist, reply_markup=keyboard_hist, parse_mode="HTML")
            return

    except Exception as exc:
        logger.error(f"Error campaign_callback_handler: {exc}", exc_info=True)
        try:
            await _safe_edit(query, 
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
                days_lookback=tpl.get("days_lookback"),
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

            tpl = get_template(camp.template_type) if camp.template_type else None
            sim = simulate_campaign(
                db=db,
                mode=camp.mode,
                total_pool=camp.total_pool,
                target_segment=camp.target_segment,
                max_winners=camp.max_winners,
                milestone_metric=camp.milestone_metric or "VOLUME_IDR",
                days_lookback=tpl.get("days_lookback") if tpl else None,
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
        if camp.milestone_metric == "TX_COUNT":
            metric_str = f" ({w['metric_value']} Transaksi)" if w.get("metric_value") else ""
        else:
            metric_str = f" (Vol: {format_idr(w['metric_value'])})" if w.get("metric_value") else ""
        winners_preview.append(f"  • {rank_str}<b>{_esc(w['username'])}</b> — <code>+{format_idr(w['amount'])}</code>{metric_str}")

    funding_str = ""
    if (camp.mode or "").upper() == "MILESTONE":
        # Hadiah milestone selalu dari Kas Bot (keputusan client).
        _db = SessionLocal()
        try:
            balance = crud.get_bot_treasury_balance(_db)
        finally:
            _db.close()
        shortfall = max(0, int(sim["total_distributed"]) - balance)
        funding_str = f"🏦 <b>Kas Bot:</b> <code>{format_idr(balance)}</code>\n"
        if shortfall:
            funding_str += (f"⚠️ <b>Kas Bot kurang {format_idr(shortfall)}</b> — isi dulu via QRIS "
                            f"(Dompet &amp; Kas Bot) sebelum eksekusi.\n")
        funding_str += "\n"
    else:
        funding_str = "\n"

    more_str = f"\n  <i>...dan {len(sim['winners']) - 5} pemenang lainnya.</i>" if len(sim["winners"]) > 5 else ""
    winners_list_str = "\n".join(winners_preview) if winners_preview else "  <i>Belum ada user yang memenuhi kriteria</i>"

    return (
        f"📊 <b>PREVIEW CAMPAIGN: {camp.title}</b>\n\n"
        f"🎯 <b>Mode:</b> <code>{camp.mode}{metric_info}</code>\n"
        f"👥 <b>Target Segmen:</b> <code>{camp.target_segment}</code>\n"
        f"👥 <b>Total Pemenang:</b> <code>{sim['winner_count']} user</code>\n"
        f"💵 <b>Hadiah per User:</b> <code>{format_idr(sim['reward_per_winner'])}</code>\n"
        f"💰 <b>Total Anggaran Keluar:</b> <code>{format_idr(sim['total_distributed'])}</code>\n"
        f"🛡️ <b>Maksimal Anggaran (Cap):</b> <code>{format_idr(camp.total_pool)}</code>\n"
        f"{funding_str}"
        f"📋 <b>Daftar Pemenang Terpilih:</b>\n"
        f"{winners_list_str}{more_str}\n\n"
        f"✉️ <b>Pratinjau Notifikasi ke Pemenang:</b>\n"
        f"<blockquote>{notif_sample}</blockquote>\n\n"
        f"<i>Klik '🚀 Eksekusi' untuk langsung membagikan saldo ke akun user, atau edit pesan notifikasi di bawah.</i>"
    )
