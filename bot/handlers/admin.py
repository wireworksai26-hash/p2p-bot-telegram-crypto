"""
bot/handlers/admin.py — Handler Panel Administrator.
===================================================
Berisi perintah dan kontrol administratif khusus untuk owner/admin bot.
Termasuk broadcast, statistik, set spread, un/ban, list pending order, dan konfirmasi order.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import RetryAfter, BadRequest
from telegram.ext import ContextTypes

from config.settings import settings
from database.connection import SessionLocal
from database.models import User, Order, WalletBalance, PriceConfig, AuditLog, TopupOrder
from database import crud
from html import escape as _esc
from services.crypto_sender import CryptoSenderFactory
from bot.utils.formatter import format_idr, format_crypto
from bot.utils.emojis import (
    CUSTOM_EMOJI_IDS,
    CUSTOM_EMOJI_ALTS,
    set_custom_emoji,
    reset_custom_emojis,
    sync_from_stickers,
    tg_emoji,
)

logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    """Mengecek apakah user_id terdaftar dalam ADMIN_CHAT_IDS."""
    return user_id in settings.ADMIN_CHAT_IDS


def get_admin_dashboard_keyboard(pending_count: int = 0) -> InlineKeyboardMarkup:
    """Membuat inline keyboard navigasi utama Admin Dashboard."""
    order_label = f"📥 Antrean Order ({pending_count})" if pending_count > 0 else "📥 Antrean Order (0)"
    
    keyboard = [
        [InlineKeyboardButton("📥 Dashboard Jual Crypto", callback_data="admin_sellorders_0")],
        [
            InlineKeyboardButton("📊 Statistik & Volume", callback_data="admin_panel_stats"),
            InlineKeyboardButton("📑 Rekap Mingguan (Sheets)", callback_data="admin_panel_weekly_report"),
        ],
        [
            InlineKeyboardButton("💳 Kirim Saldo User", callback_data="admin_panel_send_balance"),
            InlineKeyboardButton("🏦 Dompet & Kas Bot", callback_data="admin_panel_treasury"),
        ],
        [
            InlineKeyboardButton(order_label, callback_data="admin_panel_orders"),
            InlineKeyboardButton("👥 Kelola User", callback_data="admin_panel_users"),
        ],
        [
            InlineKeyboardButton("🎁 Pusat Campaign, Giveaway & Loyalty", callback_data="admin_panel_campaign"),
        ],
        [
            InlineKeyboardButton("🔗 Referral Program", callback_data="admin_panel_referral"),
            InlineKeyboardButton("⚙️ Pengaturan Spread", callback_data="admin_panel_spread"),
        ],
        [
            InlineKeyboardButton("💼 Hot Wallets & Saldo", callback_data="admin_panel_wallets"),
            InlineKeyboardButton("🔄 Sync On-Chain", callback_data="admin_panel_sync_wallets"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast Pesan", callback_data="admin_panel_broadcast"),
            InlineKeyboardButton("📜 Audit Trail Log", callback_data="admin_panel_audit"),
        ],
        [
            InlineKeyboardButton("🎨 Custom Emoji 3D", callback_data="admin_panel_emojis"),
            InlineKeyboardButton("📡 Status API & RPC", callback_data="admin_panel_check_apis"),
        ],
        [
            InlineKeyboardButton("❌ Tutup Panel", callback_data="admin_panel_close"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_admin_dashboard_text(db) -> str:
    """Membangun teks ringkasan eksekutif Admin Dashboard."""
    stats = crud.get_daily_stats(db)
    total_users = crud.get_user_count(db)
    pending_count = crud.get_pending_orders_count(db)
    completed_all = crud.get_completed_order_count(db)
    bot_treasury = crud.get_bot_treasury_balance(db)
    
    now_str = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M UTC")
    
    status_indicator = "🔴 <b>Perlu Tindakan!</b>" if pending_count > 0 else "🟢 <b>Semua Sistem Lancar</b>"

    text = (
        "👑 <b>ADMIN EXECUTIVE CONTROL CENTER</b>\n"
        f"🕒 <i>Status Update: {now_str}</i>\n\n"
        f"🚦 <b>Kondisi Operasional:</b> {status_indicator}\n"
        f"├── 👥 <b>Total Pengguna:</b> <code>{total_users:,} Member</code>\n"
        f"├── 🏦 <b>Kas Dompet Bot:</b> <code>{format_idr(bot_treasury)}</code>\n"
        f"├── 🛒 <b>Order Hari Ini:</b> <code>{stats['total_orders_today']} Order</code>\n"
        f"├── ✅ <b>Total Sukses (All-Time):</b> <code>{completed_all:,} Transaksi</code>\n"
        f"├── 💳 <b>Volume Hari Ini:</b> <code>{format_idr(stats['total_volume_idr_today'])}</code>\n"
        f"└── ⏳ <b>Antrean Pending:</b> <b>{pending_count} Order</b>\n\n"
        "💡 <i>Pilih menu di bawah ini untuk inspeksi & tindakan administratif cepat:</i>"
    )
    return text


async def sellorders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A dedicated, paginated sell queue; group membership never grants admin rights."""
    from html import escape
    if not is_admin(update.effective_user.id):
        if update.callback_query:
            await update.callback_query.answer("Akses ditolak.", show_alert=True)
        return
    query = update.callback_query
    page = 0
    if query:
        await query.answer()
        suffix = query.data.removeprefix("admin_sellorders_")
        page = max(0, int(suffix)) if suffix.isdigit() else 0
    db = SessionLocal()
    try:
        orders = db.query(Order).filter(Order.order_type == "sell",
            Order.status.in_(["WAITING_CRYPTO_DEPOSIT", "CRYPTO_CONFIRMED", "manual_review", "MANUAL_REVIEW"]))
        count = orders.count()
        page = min(page, max(0, (count - 1) // 8))
        rows = orders.order_by(Order.created_at.desc()).offset(page * 8).limit(8).all()
        lines = ["📥 <b>DASHBOARD JUAL CRYPTO</b>", f"Antrean aktif: {count}",
                 "Transfer Rupiah hanya setelah status DEPOSIT TERVERIFIKASI.\n"]
        keyboard = []
        for order in rows:
            ready = order.status == "CRYPTO_CONFIRMED"
            status = "✅ DEPOSIT TERVERIFIKASI" if ready else "⏳ BELUM TERVERIFIKASI"
            lines.append(f"<code>{escape(order.order_id)}</code> — {status}\n"
                         f"{format_crypto(float(order.crypto_amount), order.crypto_symbol)} ({escape(order.network)}) · {format_idr(order.total_idr)}")
            if ready:
                keyboard.append([InlineKeyboardButton(f"✅ Bayar {order.order_id[-6:]}", callback_data=f"admin_confirm_sell_{order.order_id}"),
                                 InlineKeyboardButton("📸 Bukti pembayaran", callback_data=f"admin_upload_proof_{order.order_id}")])
        navigation = [InlineKeyboardButton("🔄 Refresh", callback_data=f"admin_sellorders_{page}")]
        if page:
            navigation.insert(0, InlineKeyboardButton("←", callback_data=f"admin_sellorders_{page - 1}"))
        if (page + 1) * 8 < count:
            navigation.append(InlineKeyboardButton("→", callback_data=f"admin_sellorders_{page + 1}"))
        keyboard.append(navigation)
        if query:
            from bot.utils.telegram_utils import safe_edit_message
            await safe_edit_message(query, "\n\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text("\n\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    finally:
        db.close()


def build_admin_stats_text(db) -> str:
    """Membangun teks laporan analitik dan statistik lengkap."""
    stats = crud.get_daily_stats(db)
    total_users = crud.get_user_count(db)
    completed_all = crud.get_completed_order_count(db)
    
    # Hitung breakdown tipe order
    buy_count = db.query(Order).filter(Order.order_type == "buy", Order.status == "completed").count()
    sell_count = db.query(Order).filter(Order.order_type == "sell", Order.status == "completed").count()
    swap_count = db.query(Order).filter(Order.order_type == "swap", Order.status == "completed").count()
    
    # Hitung total volume all-time
    from sqlalchemy import func
    total_vol_row = db.query(func.sum(Order.total_idr)).filter(Order.status == "completed").scalar()
    total_vol_all = int(total_vol_row or 0)

    # Hitung total profit fee all-time
    total_fee_row = db.query(func.sum(Order.fee_idr)).filter(Order.status == "completed").scalar()
    total_fee_all = int(total_fee_row or 0)

    text = (
        "📊 <b>LAPORAN STATISTIK & ANALITIK BOT</b>\n"
        f"📅 <i>Tanggal: {datetime.now(timezone.utc).strftime('%d-%m-%Y %H:%M UTC')}</i>\n\n"
        "📈 <b>Performa Hari Ini:</b>\n"
        f"• Total Order: <code>{stats['total_orders_today']}</code>\n"
        f"• Order Selesai: <code>{stats['completed_orders_today']}</code>\n"
        f"• Volume Transaksi: <b>{format_idr(stats['total_volume_idr_today'])}</b>\n\n"
        "🏛 <b>Akumulasi Keseluruhan (All-Time):</b>\n"
        f"• Total Pengguna Terdaftar: <code>{total_users:,} User</code>\n"
        f"• Total Transaksi Sukses: <code>{completed_all:,} Transaksi</code>\n"
        f"  ├── 🛒 Beli Koin: <code>{buy_count:,}x</code>\n"
        f"  ├── 💵 Jual Koin: <code>{sell_count:,}x</code>\n"
        f"  └── 💱 Swap / Convert: <code>{swap_count:,}x</code>\n"
        f"• Total Akumulasi Volume: <b>{format_idr(total_vol_all)}</b>\n"
        f"• Estimasi Akumulasi Fee: <b>{format_idr(total_fee_all)}</b>\n"
    )
    return text


def build_admin_orders_view(db) -> tuple[str, InlineKeyboardMarkup]:
    """Membangun teks antrean order pending beserta tombol aksi interaktif."""
    orders = (
        db.query(Order)
        .filter(Order.status.in_(["pending", "paid", "payout_processing", "manual_review", "WAITING_CRYPTO_DEPOSIT", "PAYOUT_QUEUED"]))
        .order_by(Order.created_at.desc())
        .limit(10)
        .all()
    )

    if not orders:
        text = (
            "📥 <b>ANTREAN ORDER AKTIF & PENDING</b>\n\n"
            "✨ <b>Semua antrean bersih!</b>\n"
            "Tidak ada order tertunda yang memerlukan tinjauan manual admin saat ini."
        )
        buttons = [
            [InlineKeyboardButton("🔄 Refresh Antrean", callback_data="admin_panel_orders")],
            [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
        ]
        return text, InlineKeyboardMarkup(buttons)

    text_lines = [
        f"📥 <b>ANTREAN ORDER AKTIF ({len(orders)} Terdeteksi)</b>\n",
        "<i>Menampilkan maks 10 order tertunda terbaru:</i>\n"
    ]

    action_buttons = []
    for idx, o in enumerate(orders, 1):
        o_type = "🛒 BELI" if o.order_type == "buy" else ("💵 JUAL" if o.order_type == "sell" else "💱 SWAP")
        crypto_str = format_crypto(float(o.crypto_amount or 0), o.crypto_symbol)
        
        text_lines.append(
            f"<b>{idx}. {o.order_id}</b> ({o_type})\n"
            f"   🚦 Status: <code>{o.status.upper()}</code>\n"
            f"   🪙 Koin: <code>{crypto_str} ({o.network})</code>\n"
            f"   💳 Nilai: <code>{format_idr(o.total_idr or 0)}</code>\n"
            f"   👤 User ID: <code>{o.telegram_id}</code>\n"
        )

        # Tambahkan tombol aksi per order
        if o.order_type == "sell" and o.status in ["paid", "pending", "manual_review", "WAITING_CRYPTO_DEPOSIT"]:
            action_buttons.append([
                InlineKeyboardButton(f"📸 Upload Bukti {o.order_id[-6:]}", callback_data=f"admin_upload_proof_{o.order_id}"),
                InlineKeyboardButton(f"✅ Konfirmasi {o.order_id[-6:]}", callback_data=f"admin_confirm_sell_{o.order_id}"),
            ])
        elif o.order_type == "buy" and o.status in ["paid", "pending", "manual_review"]:
            action_buttons.append([
                InlineKeyboardButton(f"✅ Approve {o.order_id[-6:]}", callback_data=f"admin_approve_buy_{o.order_id}"),
                InlineKeyboardButton(f"❌ Reject {o.order_id[-6:]}", callback_data=f"admin_reject_buy_{o.order_id}"),
            ])
        elif o.order_type == "swap" and o.status in ["paid", "pending", "manual_review", "WAITING_CRYPTO_DEPOSIT"]:
            action_buttons.append([
                InlineKeyboardButton(f"✅ Approve Swap {o.order_id[-6:]}", callback_data=f"admin_approve_swap_{o.order_id}"),
                InlineKeyboardButton(f"❌ Reject Swap {o.order_id[-6:]}", callback_data=f"admin_reject_swap_{o.order_id}"),
            ])

    action_buttons.append([
        InlineKeyboardButton("🔄 Refresh Antrean", callback_data="admin_panel_orders"),
        InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main"),
    ])
    return "\n".join(text_lines), InlineKeyboardMarkup(action_buttons)


def build_admin_wallets_view(db) -> str:
    """Membangun teks status saldo hot wallet dan kesiapan gas fee seluruh chain."""
    wallets = db.query(WalletBalance).order_by(WalletBalance.network, WalletBalance.symbol).all()
    
    text_lines = [
        "💼 <b>MONITORING HOT WALLETS & GAS FEE</b>\n",
        "<i>Ringkasan saldo cadangan sistem di database:</i>\n"
    ]

    if not wallets:
        text_lines.append("⚠️ <i>Belum ada data saldo di database. Tekan tombol <b>Sync On-Chain</b> di bawah.</i>\n")
    else:
        current_net = None
        for w in wallets:
            if w.network != current_net:
                current_net = w.network
                text_lines.append(f"\n🌐 <b>Jaringan {current_net}:</b>")
            
            bal_val = float(w.balance or 0.0)
            res_val = float(w.reserved_balance or 0.0)
            avail_val = max(0.0, bal_val - res_val)
            
            # Status indicator
            status_icon = "🟢" if avail_val > 0 else "🔴"
            
            text_lines.append(
                f"• {status_icon} <b>{w.symbol}</b>: <code>{bal_val:,.6f}</code> (Tersedia: <code>{avail_val:,.6f}</code>)"
            )

    text_lines.append("\n💡 <i>Klik <b>Sync On-Chain</b> untuk mengecek saldo live langsung dari blockchain.</i>")
    return "\n".join(text_lines)


def build_admin_audit_view(db) -> str:
    """Membangun teks 10 log audit event sistem terbaru."""
    logs = crud.get_recent_audit_logs(db, limit=10)
    
    text_lines = [
        "📜 <b>AUDIT TRAIL LOG SISTEM (10 Terbaru)</b>\n"
    ]

    if not logs:
        text_lines.append("ℹ️ <i>Belum ada catatan audit log tersimpan.</i>")
    else:
        for log in logs:
            time_str = log.created_at.strftime("%H:%M:%S") if log.created_at else "-"
            text_lines.append(
                f"• <b>[{time_str}] {log.action}</b>\n"
                f"  Order: <code>{log.order_id or '-'}</code> | {log.from_status or '-'} ➔ <b>{log.to_status or '-'}</b>\n"
                f"  Detail: <i>{log.details or '-'}</i>\n"
            )

    return "\n".join(text_lines)


def build_admin_users_view(db) -> str:
    """Membangun teks manajemen dan statistik pengguna."""
    total_users = db.query(User).count()
    banned_users = db.query(User).filter(User.is_banned == True).all() # noqa: E712
    active_users = total_users - len(banned_users)

    text_lines = [
        "👥 <b>MANAJEMEN PENGGUNA BOT</b>\n",
        f"• <b>Total Pengguna Terdaftar:</b> <code>{total_users:,} User</code>",
        f"• <b>Pengguna Aktif:</b> <code>{active_users:,} User</code>",
        f"• <b>Pengguna Terblokir (Banned):</b> <code>{len(banned_users)} User</code>\n",
    ]

    if banned_users:
        text_lines.append("🚫 <b>Daftar User Banned:</b>")
        for u in banned_users[:10]:
            uname = f"@{u.username}" if u.username else u.full_name or "Tanpa Nama"
            text_lines.append(f"• ID <code>{u.telegram_id}</code> ({uname})")
        text_lines.append("")

    text_lines.extend([
        "⚙️ <b>Perintah Cepat Kelola User:</b>",
        "• <code>/ban [USER_ID]</code> — Blokir akses transaksi user",
        "• <code>/unban [USER_ID]</code> — Buka blokir akses user"
    ])
    return "\n".join(text_lines)


def build_admin_spread_view(db) -> str:
    """Membangun teks konfigurasi spread harga koin."""
    configs = db.query(PriceConfig).order_by(PriceConfig.symbol).all()

    text_lines = [
        "⚙️ <b>PENGATURAN SPREAD HARGA KOIN</b>\n",
        "<i>Kebijakan harga saat ini: <b>Harga Pasar Murni (0.0% Spread)</b></i>\n",
        "<b>Status Spread Koin Saat Ini:</b>"
    ]

    if not configs:
        text_lines.append("• Default Global: <code>0.0% (Live Market)</code>")
    else:
        for c in configs:
            text_lines.append(f"• <b>{c.symbol}</b>: <code>{c.spread_pct}%</code> (Aktif: {'✅' if c.is_active else '❌'})")

    text_lines.extend([
        "\n💡 <b>Cara Mengubah Spread:</b>",
        "Gunakan perintah: <code>/setspread [SYMBOL] [PERSEN]</code>",
        "<i>Contoh:</i> <code>/setspread USDT 1.5</code> atau <code>/setspread ETH 0.0</code>"
    ])
    return "\n".join(text_lines)


def build_admin_broadcast_view() -> str:
    """Membangun panduan dan template broadcast."""
    text = (
        "📢 <b>PUSAT PENGIRIMAN BROADCAST / PENGUMUMAN</b>\n\n"
        "Fitur ini memungkinkan Anda mengirimkan siaran pesan resmi ke seluruh pengguna bot secara serentak.\n\n"
        "📝 <b>Format Perintah:</b>\n"
        "<code>/broadcast [PESAN PENGUMUMAN]</code>\n\n"
        "🎯 <b>Broadcast ke Segmen Tertentu:</b>\n"
        "<code>/broadcast --all [PESAN]</code>  — Semua user\n"
        "<code>/broadcast --active [PESAN]</code> — User aktif 30 hari\n"
        "<code>/broadcast --buyers [PESAN]</code> — User yang pernah transaksi\n"
        "<code>/broadcast --balance [PESAN]</code> — User yang punya saldo\n\n"
        "🪙 <b>Siaran Otomatis Koin Ready:</b>\n"
        "<code>/broadcast --ready [JARINGAN]</code>\n"
        "Contoh: <code>/broadcast --ready Morph</code> atau <code>/broadcast --ready Base</code>\n"
        "<i>Bot otomatis menyusun daftar koin aktif untuk jaringan tersebut.</i>\n\n"
        "🖼️ <b>Siaran Bergambar (Poster / Logo):</b>\n"
        "• Kirim poster sebagai FOTO dengan caption diawali <code>/broadcast ...</code>, ATAU\n"
        "• Reply foto poster dengan <code>/broadcast ...</code>.\n"
        "Contoh caption: <code>/broadcast --ready Morph</code>\n\n"
        "⚠️ <b>Catatan Penting:</b>\n"
        "• Pesan dikirim bersih tanpa teks header otomatis.\n"
        "• Anda dapat menggunakan tag HTML seperti <code>&lt;b&gt;tebal&lt;/b&gt;</code>, <code>&lt;i&gt;miring&lt;/i&gt;</code>, dan <code>&lt;code&gt;kode&lt;/code&gt;</code>.\n"
        "• User yang memblokir bot akan otomatis dilewati tanpa menghentikan broadcast."
    )
    return text


def build_admin_credit_view() -> str:
    """Membangun panduan isi saldo user."""
    return (
        "💳 <b>ISI SALDO USER (ADMIN CREDIT)</b>\n\n"
        "Fitur ini memungkinkan admin mengisi saldo IDR ke user tertentu.\n"
        "Digunakan untuk campaign giveaway, reward, atau kompensasi.\n\n"
        "📝 <b>Satu User:</b>\n"
        "<code>/credit [telegram_id] [jumlah_idr]</code>\n"
        "Contoh: <code>/credit 123456789 10000</code>\n\n"
        "📝 <b>Banyak User Sekaligus:</b>\n"
        "<code>/bulkcredit [jumlah_idr] [id1] [id2] ...</code>\n"
        "Contoh: <code>/bulkcredit 10000 123456789 987654321</code>\n\n"
        "⚠️ <b>Catatan:</b>\n"
        "• Minimum: Rp 1.000 | Maksimum: Rp 10.000.000 per operasi\n"
        "• Setiap kredit tercatat di Audit Log\n"
        "• User otomatis mendapat notifikasi saldo bertambah"
    )


def build_admin_referral_view(db) -> str:
    """Membangun tampilan manajemen konfigurasi & statistik referral untuk admin."""
    try:
        from database.models import Referral, ReferralConfig
        from database.crud import get_referral_config
        from sqlalchemy import func as sa_func, Integer

        total = db.query(Referral).count()
        completed = db.query(Referral).filter(Referral.status == "COMPLETED").count()
        pending = db.query(Referral).filter(Referral.status == "PENDING").count()

        # Configs
        reward_cfg = get_referral_config(db, "reward_per_referral")
        reward_idr = int(reward_cfg) if reward_cfg else 5000

        bonus_cfg = get_referral_config(db, "referee_discount_idr")
        bonus_idr = int(bonus_cfg) if bonus_cfg else 0

        min_trade_cfg = get_referral_config(db, "min_trade_amount_idr")
        min_trade_idr = int(min_trade_cfg) if min_trade_cfg else 0
        min_trade_display = f"Rp {min_trade_idr:,}" if min_trade_idr > 0 else "Tanpa Minimal (Semua Order)"

        enabled_cfg = get_referral_config(db, "referral_enabled")
        is_enabled = enabled_cfg is None or enabled_cfg.lower() == "true"

        max_cfg = get_referral_config(db, "max_referrals_per_user")
        max_refs = int(max_cfg) if max_cfg else 100

        # Total reward paid out
        total_payout = (
            db.query(sa_func.sum(sa_func.cast(Referral.reward_idr, Integer)))
            .filter(Referral.status == "COMPLETED")
            .scalar() or 0
        )

        status_badge = "🟢 <b>AKTIF</b>" if is_enabled else "🔴 <b>NONAKTIF</b>"

        # Top 10 referrers
        top = (
            db.query(
                Referral.referrer_id,
                sa_func.count(Referral.id).label("cnt"),
                sa_func.sum(
                    sa_func.cast(Referral.status == "COMPLETED", Integer)
                ).label("done"),
            )
            .group_by(Referral.referrer_id)
            .order_by(sa_func.count(Referral.id).desc())
            .limit(10)
            .all()
        )

        lines = [
            "🔗 <b>MANAJEMEN PROGRAM REFERRAL</b>\n",
            f"⚙️ <b>Status Program:</b> {status_badge}",
            f"💰 <b>Reward Pengundang:</b> Rp {reward_idr:,}",
            f"🎁 <b>Potongan/Bonus Teman:</b> Rp {bonus_idr:,}",
            f"🛒 <b>Min. Pembelian Teman:</b> {min_trade_display}",
            f"🎯 <b>Maksimal per User:</b> {max_refs} teman\n",
            "📊 <b>Statistik Akumulatif:</b>",
            f"├── 👥 Total Ajakan    : <b>{total}</b>",
            f"├── ✅ Selesai Transaksi: <b>{completed}</b>",
            f"├── ⏳ Belum Transaksi  : <b>{pending}</b>",
            f"└── 💸 Total Reward Cair: <b>Rp {total_payout:,}</b>\n",
            "🏆 <b>Top 10 Pengundang Terbanyak:</b>",
        ]
        if top:
            for i, row in enumerate(top, 1):
                user = db.query(User).filter(User.telegram_id == row.referrer_id).first()
                name = f"@{user.username}" if user and user.username else str(row.referrer_id)
                done = row.done or 0
                lines.append(f"{i}. {name} — {row.cnt} ajakan ({done} selesai)")
        else:
            lines.append("<i>Belum ada data referral.</i>")

        lines.append(
            "\n💡 <b>Pengaturan Cepat:</b>\n"
            "Gunakan tombol di bawah untuk toggle status, atur reward, potongan, atau minimal pembelian secara instan."
        )

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error build_admin_referral_view: {e}", exc_info=True)
        return (
            "🔗 <b>REFERRAL PROGRAM</b>\n\n"
            "<i>Tabel referral belum siap atau terjadi kendala database.</i>"
        )


def build_admin_referral_keyboard(db) -> InlineKeyboardMarkup:
    """Membuat inline keyboard manajemen referral interaktif untuk admin."""
    from database.crud import get_referral_config
    enabled_cfg = get_referral_config(db, "referral_enabled")
    is_enabled = enabled_cfg is None or enabled_cfg.lower() == "true"
    toggle_text = "🔴 Nonaktifkan Program" if is_enabled else "🟢 Aktifkan Program"

    buttons = [
        [InlineKeyboardButton(toggle_text, callback_data="admin_ref_toggle_enabled")],
        [
            InlineKeyboardButton("💵 Atur Reward Pengundang", callback_data="admin_ref_pick_reward"),
            InlineKeyboardButton("🎁 Atur Potongan Teman", callback_data="admin_ref_pick_bonus"),
        ],
        [
            InlineKeyboardButton("🛒 Atur Min. Pembelian", callback_data="admin_ref_pick_min_trade"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Data", callback_data="admin_panel_referral"),
            InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_pick_reward_keyboard() -> InlineKeyboardMarkup:
    """Keyboard pilihan cepat nominal reward pengundang."""
    buttons = [
        [
            InlineKeyboardButton("Rp 2.000", callback_data="admin_ref_set_reward_2000"),
            InlineKeyboardButton("Rp 5.000", callback_data="admin_ref_set_reward_5000"),
            InlineKeyboardButton("Rp 10.000", callback_data="admin_ref_set_reward_10000"),
        ],
        [
            InlineKeyboardButton("Rp 25.000", callback_data="admin_ref_set_reward_25000"),
            InlineKeyboardButton("Rp 50.000", callback_data="admin_ref_set_reward_50000"),
            InlineKeyboardButton("✏️ Nominal Kustom", callback_data="admin_ref_custom_reward"),
        ],
        [
            InlineKeyboardButton("🔙 Kembali ke Kelola Referral", callback_data="admin_panel_referral"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_pick_bonus_keyboard() -> InlineKeyboardMarkup:
    """Keyboard pilihan cepat nominal potongan/bonus transaksi pertama teman."""
    buttons = [
        [
            InlineKeyboardButton("Rp 0 (Nonaktif)", callback_data="admin_ref_set_bonus_0"),
            InlineKeyboardButton("Rp 2.500", callback_data="admin_ref_set_bonus_2500"),
            InlineKeyboardButton("Rp 5.000", callback_data="admin_ref_set_bonus_5000"),
        ],
        [
            InlineKeyboardButton("Rp 10.000", callback_data="admin_ref_set_bonus_10000"),
            InlineKeyboardButton("Rp 20.000", callback_data="admin_ref_set_bonus_20000"),
            InlineKeyboardButton("✏️ Nominal Kustom", callback_data="admin_ref_custom_bonus"),
        ],
        [
            InlineKeyboardButton("🔙 Kembali ke Kelola Referral", callback_data="admin_panel_referral"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_pick_min_trade_keyboard() -> InlineKeyboardMarkup:
    """Keyboard pilihan cepat syarat minimal pembelian teman untuk referral."""
    buttons = [
        [
            InlineKeyboardButton("Rp 0 (Bebas)", callback_data="admin_ref_set_min_trade_0"),
            InlineKeyboardButton("Rp 10.000", callback_data="admin_ref_set_min_trade_10000"),
            InlineKeyboardButton("Rp 25.000", callback_data="admin_ref_set_min_trade_25000"),
        ],
        [
            InlineKeyboardButton("Rp 50.000", callback_data="admin_ref_set_min_trade_50000"),
            InlineKeyboardButton("Rp 100.000", callback_data="admin_ref_set_min_trade_100000"),
            InlineKeyboardButton("✏️ Nominal Kustom", callback_data="admin_ref_custom_min_trade"),
        ],
        [
            InlineKeyboardButton("🔙 Kembali ke Kelola Referral", callback_data="admin_panel_referral"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_emojis_view() -> str:
    """Membangun panduan otomatisasi dan status custom emoji 3D."""
    text = (
        "🎨 <b>OTOMATISASI CUSTOM EMOJI 3D PREMIUM</b>\n\n"
        "Bot dilengkapi integrasi penuh dengan Animated Emoji Telegram Premium!\n\n"
        "✨ <b>Perintah-Perintah Otomasi:</b>\n"
        "• <code>/syncpack [NAMA_PACK_ATAU_URL]</code> — Sinkronisasi 1 paket emoji Telegram otomatis\n"
        "• <code>/setemoji [KEY] [EMOJI]</code> — Ganti 1 emoji custom secara spesifik\n"
        "• <code>/listemojis</code> — Tampilkan seluruh custom emoji yang sedang aktif\n"
        "• <code>/getemoji [EMOJI]</code> — Deteksi ID custom emoji dari pesan\n"
        "• <code>/resetemojis</code> — Reset ke emoji default bawaan Telegram\n\n"
        "💡 <i>Semua perubahan langsung aktif realtime di bot tanpa perlu restart server!</i>"
    )
    return text


# ─────────────────────────────────────────────────────────
#  Phase 7: Top Spender, Random Winner & Loyalty Config Views
# ─────────────────────────────────────────────────────────

def build_admin_top_spenders_view(db, period_days: int = 30) -> str:
    """Membangun teks leaderboard Top Spender."""
    from services.campaign_service import TOP_SPENDER_REWARDS
    from bot.utils.formatter import format_idr

    top_spenders = crud.get_top_spenders(db, limit=10, period_days=period_days)
    total_pool = sum(TOP_SPENDER_REWARDS.get(i, 0) for i in range(1, 11))

    lines = [
        "🏆 <b>TOP SPENDER — LEADERBOARD TRANSAKSI</b>",
        f"📅 Periode: <b>{period_days} Hari Terakhir</b> | Total Hadiah: <b>{format_idr(total_pool)}</b>\n",
    ]

    if not top_spenders:
        lines.append("<i>Belum ada data transaksi pembelian selesai pada periode ini.</i>\n")
    else:
        for u in top_spenders:
            rank = u["rank"]
            reward = TOP_SPENDER_REWARDS.get(rank, 0)
            medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"#{rank}"
            lines.append(
                f"{medal} <b>@{u['username']}</b> (ID: <code>{u['telegram_id']}</code>)\n"
                f"   ├── Volume : <b>{format_idr(u['total_spent_idr'])}</b>\n"
                f"   └── Hadiah : <code>+{format_idr(reward)}</code>"
            )

    lines.append("\n💡 <i>Klik tombol di bawah untuk membagikan saldo hadiah langsung ke akun para pemenang:</i>")
    return "\n".join(lines)


def build_admin_top_spenders_keyboard(period_days: int = 30) -> InlineKeyboardMarkup:
    """Keyboard navigasi Top Spender."""
    buttons = [
        [
            InlineKeyboardButton("💰 Eksekusi & Bagikan Hadiah ke Top 10", callback_data=f"admin_top_spender_exec_{period_days}"),
        ],
        [
            InlineKeyboardButton("📅 7 Hari", callback_data="admin_top_spender_p_7"),
            InlineKeyboardButton("📅 30 Hari", callback_data="admin_top_spender_p_30"),
            InlineKeyboardButton("📅 90 Hari", callback_data="admin_top_spender_p_90"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Data", callback_data=f"admin_top_spender_p_{period_days}"),
        ],
        [
            InlineKeyboardButton("🔙 Kembali ke Campaign & Giveaway", callback_data="admin_panel_campaign"),
        ],
        [
            InlineKeyboardButton("🏠 Dashboard Utama", callback_data="admin_panel_main"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_random_draw_view(db, pool_segment: str = "ACTIVE_30D") -> str:
    """Membangun teks menu Undian Acak (Flash Giveaway)."""
    seg_names = {
        "ALL": "Semua User Bot",
        "BUYERS": "User Pernah Beli (Completed)",
        "ACTIVE_30D": "User Aktif 30 Hari Terakhir",
    }
    seg_label = seg_names.get(pool_segment, pool_segment)
    
    # Hitung jumlah kandidat pool
    pool_candidates = crud.get_random_winners(db, pool_segment=pool_segment, count=1000)
    pool_count = len(pool_candidates)

    text = (
        "🎲 <b>UNDI PEMENANG ACAK (FLASH GIVEAWAY)</b>\n\n"
        f"🎯 <b>Pool Peserta:</b> <b>{seg_label}</b>\n"
        f"👥 <b>Total Kandidat Tersedia:</b> <code>{pool_count} user</code>\n\n"
        "Sistem akan memilih pemenang secara acak dan langsung mengkreditkan saldo bot ke pemenang serta mengirim notifikasi kemenangan otomatis.\n\n"
        "👇 <b>Pilih Target Pool atau Eksekusi Preset di bawah:</b>"
    )
    return text


def build_admin_random_draw_keyboard(pool_segment: str = "ACTIVE_30D") -> InlineKeyboardMarkup:
    """Keyboard navigasi Undi Pemenang Acak."""
    buttons = [
        [
            InlineKeyboardButton(
                f"{'🔘' if pool_segment == 'ACTIVE_30D' else '⚪'} Aktif 30 Hari",
                callback_data="admin_draw_pool_ACTIVE_30D"
            ),
            InlineKeyboardButton(
                f"{'🔘' if pool_segment == 'BUYERS' else '⚪'} Pernah Beli",
                callback_data="admin_draw_pool_BUYERS"
            ),
            InlineKeyboardButton(
                f"{'🔘' if pool_segment == 'ALL' else '⚪'} Semua User",
                callback_data="admin_draw_pool_ALL"
            ),
        ],
        [
            InlineKeyboardButton("🎲 Undi 5 Orang @ Rp 25.000", callback_data=f"admin_draw_exec_{pool_segment}_5_25000"),
        ],
        [
            InlineKeyboardButton("🎲 Undi 5 Orang @ Rp 50.000", callback_data=f"admin_draw_exec_{pool_segment}_5_50000"),
        ],
        [
            InlineKeyboardButton("🎲 Undi 10 Orang @ Rp 20.000", callback_data=f"admin_draw_exec_{pool_segment}_10_20000"),
        ],
        [
            InlineKeyboardButton("🎲 Undi 20 Orang @ Rp 10.000", callback_data=f"admin_draw_exec_{pool_segment}_20_10000"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Pool", callback_data=f"admin_draw_pool_{pool_segment}"),
        ],
        [
            InlineKeyboardButton("🔙 Kembali ke Campaign & Giveaway", callback_data="admin_panel_campaign"),
        ],
        [
            InlineKeyboardButton("🏠 Dashboard Utama", callback_data="admin_panel_main"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_loyalty_view(db) -> str:
    """Membangun teks status dan konfigurasi Loyalty Reward."""
    from bot.utils.formatter import format_idr

    enabled_str = crud.get_loyalty_config(db, "loyalty_enabled") or "true"
    is_enabled = enabled_str.lower() == "true"
    window_days = int(crud.get_loyalty_config(db, "window_days") or 5)
    min_tx = int(crud.get_loyalty_config(db, "min_tx_count") or 5)
    reward_idr = int(crud.get_loyalty_config(db, "reward_amount_idr") or 25_000)
    min_tx_amt = int(crud.get_loyalty_config(db, "min_tx_amount_idr") or 50_000)

    status_icon = "🟢" if is_enabled else "🔴"
    status_text = "AKTIF" if is_enabled else "NONAKTIF"

    eligible = crud.get_loyalty_eligible_users(db)
    eligible_count = len(eligible)

    text = (
        "⏳ <b>PENGATURAN LOYALTY REWARD (TIME-WINDOW)</b>\n\n"
        f"Status Program: {status_icon} <b>{status_text}</b>\n\n"
        "⚙️ <b>Aturan Loyalty Saat Ini:</b>\n"
        f"├── ⏱️ <b>Window Waktu:</b> <code>{window_days} Hari</code>\n"
        f"├── 🔢 <b>Syarat Transaksi:</b> <code>{min_tx}x Transaksi Selesai</code>\n"
        f"├── 💰 <b>Reward Saldo:</b> <code>{format_idr(reward_idr)} per user</code>\n"
        f"└── 🛒 <b>Min. Nominal Transaksi:</b> <code>{format_idr(min_tx_amt)} / order</code>\n\n"
        f"📊 <b>User Eligible Menunggu Reward:</b> <code>{eligible_count} user</code>\n\n"
        f"💡 <i>User yang menyelesaikan minimal {min_tx}x transaksi dalam rentang {window_days} hari "
        f"akan otomatis mendapat reward saldo {format_idr(reward_idr)}.</i>"
    )
    return text


def build_admin_loyalty_keyboard(db) -> InlineKeyboardMarkup:
    """Keyboard navigasi Loyalty Config."""
    enabled_str = crud.get_loyalty_config(db, "loyalty_enabled") or "true"
    is_enabled = enabled_str.lower() == "true"
    toggle_text = "🔴 Nonaktifkan Program" if is_enabled else "🟢 Aktifkan Program"

    buttons = [
        [InlineKeyboardButton(toggle_text, callback_data="admin_loyalty_toggle")],
        [
            InlineKeyboardButton("⏱️ Ganti Window Hari", callback_data="admin_loyalty_pick_window"),
            InlineKeyboardButton("🔢 Ganti Min. Tx", callback_data="admin_loyalty_pick_mintx"),
        ],
        [
            InlineKeyboardButton("💰 Ganti Reward IDR", callback_data="admin_loyalty_pick_reward"),
            InlineKeyboardButton("🛒 Min. Nominal Order", callback_data="admin_loyalty_pick_minamt"),
        ],
        [
            InlineKeyboardButton("📊 Cek User Eligible", callback_data="admin_loyalty_check_eligible"),
            InlineKeyboardButton("🔄 Refresh", callback_data="admin_panel_loyalty"),
        ],
        [
            InlineKeyboardButton("🔙 Kembali ke Campaign & Giveaway", callback_data="admin_panel_campaign"),
        ],
        [
            InlineKeyboardButton("🏠 Dashboard Utama", callback_data="admin_panel_main"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_pick_loyalty_window_keyboard() -> InlineKeyboardMarkup:
    """Pilihan cepat rentang hari window loyalty."""
    buttons = [
        [
            InlineKeyboardButton("3 Hari", callback_data="admin_loyalty_set_window_3"),
            InlineKeyboardButton("5 Hari", callback_data="admin_loyalty_set_window_5"),
            InlineKeyboardButton("7 Hari", callback_data="admin_loyalty_set_window_7"),
        ],
        [
            InlineKeyboardButton("14 Hari", callback_data="admin_loyalty_set_window_14"),
            InlineKeyboardButton("30 Hari", callback_data="admin_loyalty_set_window_30"),
        ],
        [InlineKeyboardButton("🔙 Kembali ke Loyalty Menu", callback_data="admin_panel_loyalty")],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_pick_loyalty_mintx_keyboard() -> InlineKeyboardMarkup:
    """Pilihan cepat jumlah transaksi minimal loyalty."""
    buttons = [
        [
            InlineKeyboardButton("3x Order", callback_data="admin_loyalty_set_mintx_3"),
            InlineKeyboardButton("5x Order", callback_data="admin_loyalty_set_mintx_5"),
            InlineKeyboardButton("7x Order", callback_data="admin_loyalty_set_mintx_7"),
            InlineKeyboardButton("10x Order", callback_data="admin_loyalty_set_mintx_10"),
        ],
        [InlineKeyboardButton("🔙 Kembali ke Loyalty Menu", callback_data="admin_panel_loyalty")],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_pick_loyalty_reward_keyboard() -> InlineKeyboardMarkup:
    """Pilihan cepat nominal reward loyalty."""
    buttons = [
        [
            InlineKeyboardButton("Rp 10.000", callback_data="admin_loyalty_set_reward_10000"),
            InlineKeyboardButton("Rp 25.000", callback_data="admin_loyalty_set_reward_25000"),
            InlineKeyboardButton("Rp 50.000", callback_data="admin_loyalty_set_reward_50000"),
        ],
        [
            InlineKeyboardButton("Rp 75.000", callback_data="admin_loyalty_set_reward_75000"),
            InlineKeyboardButton("Rp 100.000", callback_data="admin_loyalty_set_reward_100000"),
        ],
        [InlineKeyboardButton("🔙 Kembali ke Loyalty Menu", callback_data="admin_panel_loyalty")],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_pick_loyalty_minamt_keyboard() -> InlineKeyboardMarkup:
    """Pilihan cepat minimal nominal order per transaksi loyalty."""
    buttons = [
        [
            InlineKeyboardButton("Rp 0 (Bebas)", callback_data="admin_loyalty_set_minamt_0"),
            InlineKeyboardButton("Rp 25.000", callback_data="admin_loyalty_set_minamt_25000"),
            InlineKeyboardButton("Rp 50.000", callback_data="admin_loyalty_set_minamt_50000"),
        ],
        [
            InlineKeyboardButton("Rp 100.000", callback_data="admin_loyalty_set_minamt_100000"),
            InlineKeyboardButton("Rp 250.000", callback_data="admin_loyalty_set_minamt_250000"),
        ],
        [InlineKeyboardButton("🔙 Kembali ke Loyalty Menu", callback_data="admin_panel_loyalty")],
    ]
    return InlineKeyboardMarkup(buttons)


# ─────────────────────────────────────────────────────────
#  Phase 8: Weekly Report & Spreadsheet Export Views
# ─────────────────────────────────────────────────────────

def build_admin_weekly_report_view(db, days: int = 7) -> tuple[str, InlineKeyboardMarkup]:
    """Membangun tampilan ringkasan laporan transaksi mingguan dan keyboard aksi export."""
    from services.report_service import (
        get_weekly_transactions_data,
        calculate_weekly_summary,
        format_weekly_report_telegram_message,
    )
    transactions = get_weekly_transactions_data(db, days=days)
    summary = calculate_weekly_summary(transactions, days=days)
    text = format_weekly_report_telegram_message(summary)

    buttons = [
        [
            InlineKeyboardButton("📥 Download File Spreadsheet (.CSV)", callback_data=f"admin_export_csv_{days}"),
        ],
        [
            InlineKeyboardButton(f"{'🔘' if days == 7 else '⚪'} 7 Hari", callback_data="admin_weekly_p_7"),
            InlineKeyboardButton(f"{'🔘' if days == 14 else '⚪'} 14 Hari", callback_data="admin_weekly_p_14"),
            InlineKeyboardButton(f"{'🔘' if days == 30 else '⚪'} 30 Hari", callback_data="admin_weekly_p_30"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Data", callback_data=f"admin_weekly_p_{days}"),
            InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main"),
        ],
    ]
    return text, InlineKeyboardMarkup(buttons)


# ─────────────────────────────────────────────────────────
#  Phase 8: Admin Send Balance & Bot Treasury Views
# ─────────────────────────────────────────────────────────

def build_admin_send_balance_user_prompt() -> tuple[str, InlineKeyboardMarkup]:
    """Tampilan instruksi pencarian pengguna untuk transfer saldo admin."""
    text = (
        f"{tg_emoji('CARD', '💳')} <b>KIRIM SALDO KE PENGGUNA</b>\n\n"
        "Silakan kirimkan <b>@username</b> atau <b>Telegram User ID</b> pengguna yang ingin Anda kirimkan saldo:\n\n"
        "📌 <b>Contoh Input:</b>\n"
        "• <code>@johndoe</code>\n"
        "• <code>123456789</code>\n\n"
        "<i>Sistem akan mencari data profil pengguna secara instan di database.</i>"
    )
    keyboard = [
        [InlineKeyboardButton("🔙 Batal & Dashboard Utama", callback_data="admin_panel_main")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def build_admin_send_balance_amount_view(user: User) -> tuple[str, InlineKeyboardMarkup]:
    """Tampilan pemilihan nominal saldo untuk pengguna tertentu."""
    uname = f"@{user.username}" if user.username else f"User_{user.telegram_id}"
    full_name = f" ({_esc(user.full_name)})" if user.full_name else ""
    current_bal = format_idr(int(user.balance_idr or 0))
    total_orders = user.total_orders or 0
    total_spent = format_idr(int(user.total_spent_idr or 0))

    text = (
        f"👤 <b>PROFIL PENERIMA DITEMUKAN</b>\n\n"
        f"• <b>Pengguna:</b> <b>{uname}</b>{full_name}\n"
        f"• <b>Telegram ID:</b> <code>{user.telegram_id}</code>\n"
        f"• <b>Saldo Saat Ini:</b> <b>{current_bal}</b>\n"
        f"• <b>Riwayat Transaksi:</b> <code>{total_orders}x Order ({total_spent})</code>\n\n"
        f"👇 <i>Pilih nominal saldo yang ingin dikirimkan ke akun di atas:</i>"
    )

    buttons = [
        [
            InlineKeyboardButton("+Rp 10.000", callback_data=f"admin_send_bal_amt_10000"),
            InlineKeyboardButton("+Rp 25.000", callback_data=f"admin_send_bal_amt_25000"),
        ],
        [
            InlineKeyboardButton("+Rp 50.000", callback_data=f"admin_send_bal_amt_50000"),
            InlineKeyboardButton("+Rp 100.000", callback_data=f"admin_send_bal_amt_100000"),
        ],
        [
            InlineKeyboardButton("+Rp 250.000", callback_data=f"admin_send_bal_amt_250000"),
            InlineKeyboardButton("+Rp 500.000", callback_data=f"admin_send_bal_amt_500000"),
        ],
        [
            InlineKeyboardButton("✏️ Ketik Nominal Kustom", callback_data="admin_send_bal_custom_amt"),
        ],
        [
            InlineKeyboardButton("🔄 Ganti Penerima", callback_data="admin_panel_send_balance"),
            InlineKeyboardButton("🔙 Dashboard", callback_data="admin_panel_main"),
        ],
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_admin_send_balance_confirm_view(user: User, amount: int) -> tuple[str, InlineKeyboardMarkup]:
    """Tampilan konfirmasi eksekusi transfer saldo admin."""
    uname = f"@{user.username}" if user.username else f"User_{user.telegram_id}"
    old_bal = int(user.balance_idr or 0)
    new_bal = old_bal + amount

    text = (
        f"⚠️ <b>KONFIRMASI PENGIRIMAN SALDO USER</b>\n\n"
        f"• <b>Penerima:</b> <b>{uname}</b> (<code>{user.telegram_id}</code>)\n"
        f"• <b>Nominal Kirim:</b> <code>+{format_idr(amount)}</code>\n"
        f"• <b>Saldo Sebelumnya:</b> <code>{format_idr(old_bal)}</code>\n"
        f"• <b>Saldo Setelah Kirim:</b> <b>{format_idr(new_bal)}</b>\n\n"
        f"<i>User akan otomatis menerima notifikasi saldo masuk 3D dan tercatat di Audit Log.</i>\n\n"
        f"Apakah Anda yakin ingin memproses transfer saldo ini sekarang?"
    )

    buttons = [
        [
            InlineKeyboardButton("🚀 Ya, Kirim Saldo Sekarang!", callback_data=f"admin_send_bal_confirm_{user.telegram_id}_{amount}"),
        ],
        [
            InlineKeyboardButton("🔙 Batal / Ganti Nominal", callback_data=f"admin_send_bal_user_{user.telegram_id}"),
        ],
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_admin_treasury_view(db) -> tuple[str, InlineKeyboardMarkup]:
    """Tampilan manajemen Kas & Dompet Bot (Campaign Pool)."""
    treasury_bal = crud.get_bot_treasury_balance(db)

    text = (
        f"{tg_emoji('BANK', '🏦')} <b>KAS & DOMPET CAMPAIGN BOT</b>\n\n"
        f"💰 <b>Saldo Kas Bot Saat Ini:</b> <b>{format_idr(treasury_bal)}</b>\n\n"
        "📌 <b>Fungsi Dompet Kas Bot:</b>\n"
        "• Menyimpan cadangan dana untuk event Giveaway & Campaign\n"
        "• Sumber dana otomatisasi reward Loyalty & Milestone Top Spender\n"
        "• Memastikan kelancaran distribusi hadiah bagi para pemenang\n\n"
        "👇 <i>Pilih tombol cepat di bawah untuk Top Up kas bot atau atur nominal:</i>"
    )

    buttons = [
        [
            InlineKeyboardButton("+Rp 100.000", callback_data="admin_treasury_topup_100000"),
            InlineKeyboardButton("+Rp 250.000", callback_data="admin_treasury_topup_250000"),
        ],
        [
            InlineKeyboardButton("+Rp 500.000", callback_data="admin_treasury_topup_500000"),
            InlineKeyboardButton("+Rp 1.000.000", callback_data="admin_treasury_topup_1000000"),
        ],
        [
            InlineKeyboardButton("+Rp 2.500.000", callback_data="admin_treasury_topup_2500000"),
            InlineKeyboardButton("+Rp 5.000.000", callback_data="admin_treasury_topup_5000000"),
        ],
        [
            InlineKeyboardButton("✏️ Top Up Nominal Kustom", callback_data="admin_treasury_custom"),
            InlineKeyboardButton("🔄 Atur Saldo Manual", callback_data="admin_treasury_set_manual"),
        ],
        [
            InlineKeyboardButton("🎁 Buka Wizard Campaign", callback_data="admin_panel_campaign"),
            InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main"),
        ],
    ]
    return text, InlineKeyboardMarkup(buttons)



async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan Executive Admin Dashboard Control Center."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Anda tidak memiliki akses ke menu administrator.")
        return

    db = SessionLocal()
    try:
        pending_count = crud.get_pending_orders_count(db)
        dashboard_text = build_admin_dashboard_text(db)
        reply_markup = get_admin_dashboard_keyboard(pending_count)
        
        await update.message.reply_text(
            text=dashboard_text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    finally:
        db.close()


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler routing untuk semua tombol interaktif Admin Dashboard."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    data = query.data
    logger.info(f"Admin panel callback received: {data} from {user_id}")

    db = SessionLocal()
    try:
        if data == "admin_panel_main":
            pending_count = crud.get_pending_orders_count(db)
            text = build_admin_dashboard_text(db)
            markup = get_admin_dashboard_keyboard(pending_count)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer("Dashboard diperbarui.")

        elif data == "admin_panel_stats":
            text = build_admin_stats_text(db)
            buttons = [
                [InlineKeyboardButton("🔄 Refresh Data", callback_data="admin_panel_stats")],
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            await query.answer("Statistik dimuat.")

        elif data == "admin_panel_orders":
            text, markup = build_admin_orders_view(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer("Antrean order dimuat.")

        elif data == "admin_panel_wallets":
            text = build_admin_wallets_view(db)
            buttons = [
                [InlineKeyboardButton("🔄 Sync On-Chain Sekarang", callback_data="admin_panel_sync_wallets")],
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            await query.answer("Hot wallets dimuat.")

        elif data == "admin_panel_sync_wallets":
            await query.answer("⏳ Sedang menyinkronkan saldo on-chain...", show_alert=False)
            from config.assets import STOCK_ASSETS
            success_list = []
            fail_list = []
            for sym, net in STOCK_ASSETS:
                try:
                    sender = CryptoSenderFactory.get_sender(net)
                    balance = await sender.get_balance(symbol=sym)
                    addr = getattr(sender, "wallet_address", None)
                    crud.update_wallet_balance(db, network=net, symbol=sym, balance=balance, address=addr)
                    success_list.append(f"{sym} ({net})")
                except Exception as sync_err:
                    logger.warning(f"Admin callback sync fail {sym} ({net}): {sync_err}")
                    fail_list.append(f"{sym} ({net})")

            text = (
                f"✅ <b>SINKRONISASI ON-CHAIN SELESAI!</b>\n\n"
                f"• Berhasil Disinkronkan: <code>{len(success_list)} Asset</code>\n"
                f"• Gagal: <code>{len(fail_list)} Asset</code>\n\n"
            ) + build_admin_wallets_view(db)
            
            buttons = [
                [InlineKeyboardButton("🔄 Refresh Ulang", callback_data="admin_panel_sync_wallets")],
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

        elif data == "admin_panel_check_apis":
            await query.answer("Memeriksa status API & RPC koin...", show_alert=False)
            from services.coin_api_monitor import coin_api_monitor
            results = await coin_api_monitor.check_all()
            text = coin_api_monitor.format_admin_dashboard(results)
            buttons = [
                [InlineKeyboardButton("Refresh Scan", callback_data="admin_panel_check_apis")],
                [InlineKeyboardButton("Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

        elif data == "admin_panel_audit":
            text = build_admin_audit_view(db)
            buttons = [
                [InlineKeyboardButton("🔄 Refresh Log", callback_data="admin_panel_audit")],
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            await query.answer("Audit log dimuat.")

        elif data == "admin_panel_spread":
            text = build_admin_spread_view(db)
            buttons = [
                [InlineKeyboardButton("🔄 Refresh Spread", callback_data="admin_panel_spread")],
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            await query.answer("Pengaturan spread dimuat.")

        elif data == "admin_panel_users":
            text = build_admin_users_view(db)
            buttons = [
                [InlineKeyboardButton("🔄 Refresh Data", callback_data="admin_panel_users")],
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            await query.answer("Manajemen pengguna dimuat.")

        elif data == "admin_panel_broadcast":
            text = build_admin_broadcast_view()
            buttons = [
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            await query.answer("Panduan broadcast dimuat.")

        elif data == "admin_panel_campaign":
            from bot.handlers.admin_campaign import campaign_callback_handler
            await campaign_callback_handler(update, context)
            return

        elif data == "admin_panel_credit":
            text = build_admin_credit_view()
            buttons = [
                [InlineKeyboardButton("💳 Kirim Saldo Interaktif", callback_data="admin_panel_send_balance")],
                [InlineKeyboardButton("🎁 Buka Campaign & Giveaway Wizard", callback_data="admin_panel_campaign")],
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            await query.answer("Panduan isi saldo dimuat.")

        # ─── SEND SALDO ADMIN (Phase 8) ──────────────────────
        elif data == "admin_panel_send_balance":
            context.user_data["admin_awaiting_send_bal_user"] = True
            context.user_data.pop("admin_send_bal_target_id", None)
            context.user_data.pop("admin_awaiting_send_bal_custom_amt", None)
            text, markup = build_admin_send_balance_user_prompt()
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer("Ketik username/ID user di chat.")

        elif data.startswith("admin_send_bal_user_"):
            target_id = int(data.replace("admin_send_bal_user_", ""))
            target_user = db.query(User).filter(User.telegram_id == target_id).first()
            if not target_user:
                await query.answer("User tidak ditemukan.", show_alert=True)
                return
            context.user_data["admin_send_bal_target_id"] = target_id
            context.user_data.pop("admin_awaiting_send_bal_user", None)
            context.user_data.pop("admin_awaiting_send_bal_custom_amt", None)
            text, markup = build_admin_send_balance_amount_view(target_user)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data.startswith("admin_send_bal_amt_"):
            amount = int(data.replace("admin_send_bal_amt_", ""))
            target_id = context.user_data.get("admin_send_bal_target_id")
            if not target_id:
                await query.answer("Target user belum dipilih.", show_alert=True)
                return
            target_user = db.query(User).filter(User.telegram_id == target_id).first()
            if not target_user:
                await query.answer("User tidak ditemukan di DB.", show_alert=True)
                return
            text, markup = build_admin_send_balance_confirm_view(target_user, amount)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data == "admin_send_bal_custom_amt":
            target_id = context.user_data.get("admin_send_bal_target_id")
            if not target_id:
                await query.answer("Target user belum dipilih.", show_alert=True)
                return
            context.user_data["admin_awaiting_send_bal_custom_amt"] = True
            await query.answer()
            await query.message.reply_text(
                "✏️ <b>Ketik Nominal Saldo yang Ingin Dikirim</b>\n\n"
                "Kirim pesan angka nominal dalam Rupiah (contoh: <code>75000</code> atau <code>150000</code>):\n"
                "<i>Minimal: Rp 1.000 | Maksimal: Rp 10.000.000</i>",
                parse_mode="HTML",
            )

        elif data.startswith("admin_send_bal_confirm_"):
            parts = data.replace("admin_send_bal_confirm_", "").split("_")
            target_id = int(parts[0])
            amount = int(parts[1])

            target_user = db.query(User).filter(User.telegram_id == target_id).first()
            if not target_user:
                await query.answer("User tidak ditemukan.", show_alert=True)
                return

            old_bal = float(target_user.balance_idr or 0)
            new_bal = crud.credit_user_balance(db, target_id, float(amount))

            # Audit log
            db.add(AuditLog(
                telegram_id=target_id,
                action="ADMIN_SEND_BALANCE",
                details=f"Admin {user_id} kirim saldo Rp {amount:,} ke {target_id}. Saldo: Rp {old_bal:,.0f} -> Rp {new_bal:,.0f}",
            ))
            db.commit()

            # Bersihkan state
            context.user_data.pop("admin_send_bal_target_id", None)
            context.user_data.pop("admin_awaiting_send_bal_user", None)
            context.user_data.pop("admin_awaiting_send_bal_custom_amt", None)

            # Kirim notifikasi animated 3D ke penerima
            uname_target = f"@{target_user.username}" if target_user.username else str(target_id)
            user_notif_text = (
                f"{tg_emoji('PARTY', '🎉')} <b>SALDO BERTAMBAH DARI ADMIN!</b>\n\n"
                f"Halo <b>{uname_target}</b>, Anda menerima penambahan saldo bot langsung dari Tim Admin!\n\n"
                f"{tg_emoji('MONEY_BAG', '💰')} <b>Nominal:</b> <code>+{format_idr(amount)}</code>\n"
                f"{tg_emoji('DIAMOND', '💳')} <b>Saldo Baru Anda:</b> <code>{format_idr(int(new_bal))}</code>\n"
                f"{tg_emoji('HISTORY', '📝')} <b>Keterangan:</b> Top up / Bonus Saldo Admin\n\n"
                f"{tg_emoji('ROCKET', '🚀')} <i>Saldo ini sudah aktif dan siap langsung digunakan untuk transaksi Beli/Swap crypto di bot!</i>"
            )
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=user_notif_text,
                    parse_mode="HTML",
                )
            except Exception as notif_err:
                logger.warning(f"Gagal kirim notif send saldo ke {target_id}: {notif_err}")

            success_text = (
                f"✅ <b>SALDO BERHASIL DIKIRIM!</b>\n\n"
                f"👤 <b>Penerima:</b> {uname_target} (<code>{target_id}</code>)\n"
                f"💰 <b>Nominal Terkirim:</b> <code>+{format_idr(amount)}</code>\n"
                f"💳 <b>Saldo Baru User:</b> <b>{format_idr(int(new_bal))}</b>\n"
                f"📨 <b>Notifikasi Telegram:</b> Berhasil diteruskan ke user.\n"
                f"📜 <b>Audit Trail:</b> Tercatat aman di sistem."
            )
            keyboard_done = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Kirim Saldo User Lain", callback_data="admin_panel_send_balance")],
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ])
            await query.edit_message_text(success_text, reply_markup=keyboard_done, parse_mode="HTML")
            await query.answer("Saldo berhasil dikirim!", show_alert=True)

        # ─── BOT TREASURY / DOMPET BOT (Phase 8) ─────────────
        elif data == "admin_panel_treasury" or data == "camp_treasury_view":
            text, markup = build_admin_treasury_view(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer("Kas bot dimuat.")

        elif data.startswith("admin_treasury_topup_"):
            amount = int(data.replace("admin_treasury_topup_", ""))
            new_bal = crud.topup_bot_treasury(db, amount, admin_id=user_id, note="Admin Panel Preset Topup")
            await query.answer(f"✅ Kas bot berhasil di-topup +{format_idr(amount)}!\nSaldo sekarang: {format_idr(new_bal)}", show_alert=True)
            text, markup = build_admin_treasury_view(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_treasury_custom":
            context.user_data["admin_awaiting_treasury_custom"] = True
            context.user_data["admin_awaiting_treasury_set_manual"] = False
            await query.answer()
            await query.message.reply_text(
                f"{tg_emoji('BANK', '🏦')} <b>Top Up Saldo Kas Bot Kustom</b>\n\n"
                "Ketik nominal saldo yang ingin Anda tambahkan ke Kas Bot (contoh: <code>1500000</code>):",
                parse_mode="HTML"
            )

        elif data == "admin_treasury_set_manual":
            context.user_data["admin_awaiting_treasury_set_manual"] = True
            context.user_data["admin_awaiting_treasury_custom"] = False
            curr_bal = crud.get_bot_treasury_balance(db)
            await query.answer()
            await query.message.reply_text(
                f"⚙️ <b>Atur Ulang Saldo Kas Bot Manual</b>\n\n"
                f"Saldo saat ini: <b>{format_idr(curr_bal)}</b>\n\n"
                "Ketik angka saldo baru yang diinginkan (contoh: <code>5000000</code> atau <code>0</code>):",
                parse_mode="HTML"
            )

        elif data == "admin_panel_referral":
            text = build_admin_referral_view(db)
            markup = build_admin_referral_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer("Manajemen referral dimuat.")

        elif data == "admin_ref_toggle_enabled":
            curr = crud.get_referral_config(db, "referral_enabled")
            is_on = curr is None or curr.lower() == "true"
            new_val = "false" if is_on else "true"
            crud.set_referral_config(db, "referral_enabled", new_val)
            status_str = "dinonaktifkan" if new_val == "false" else "diaktifkan"
            await query.answer(f"Program referral berhasil {status_str}!", show_alert=True)
            text = build_admin_referral_view(db)
            markup = build_admin_referral_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_ref_pick_reward":
            curr_reward = crud.get_referral_config(db, "reward_per_referral") or "5000"
            text = (
                "💵 <b>PILIH REWARD PENGUNDANG (REFERRER)</b>\n\n"
                f"Nominal reward saat ini: <b>Rp {int(curr_reward):,}</b> per teman yang selesai transaksi pertama.\n\n"
                "Pilih salah satu nominal cepat di bawah atau klik tombol kustom:"
            )
            markup = build_admin_pick_reward_keyboard()
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data.startswith("admin_ref_set_reward_"):
            val_str = data.replace("admin_ref_set_reward_", "")
            val_int = int(val_str)
            crud.set_referral_config(db, "reward_per_referral", str(val_int))
            await query.answer(f"Reward pengundang diset ke Rp {val_int:,}!", show_alert=True)
            text = build_admin_referral_view(db)
            markup = build_admin_referral_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_ref_custom_reward":
            context.user_data["admin_awaiting_ref_custom_reward"] = True
            context.user_data["admin_awaiting_ref_custom_bonus"] = False
            await query.answer()
            await query.message.reply_text(
                "✏️ <b>Ketik Nominal Reward Pengundang</b>\n\n"
                "Silakan ketik nominal reward baru dalam Rupiah (contoh: <code>7500</code> atau <code>15000</code>):",
                parse_mode="HTML"
            )

        elif data == "admin_ref_pick_bonus":
            curr_bonus = crud.get_referral_config(db, "referee_discount_idr") or "0"
            text = (
                "🎁 <b>PILIH POTONGAN / CASHBACK TEMAN (REFEREE)</b>\n\n"
                f"Nominal potongan saat ini: <b>Rp {int(curr_bonus):,}</b> untuk teman pada transaksi pertama.\n\n"
                "Pilih salah satu nominal cepat di bawah atau klik tombol kustom:"
            )
            markup = build_admin_pick_bonus_keyboard()
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data.startswith("admin_ref_set_bonus_"):
            val_str = data.replace("admin_ref_set_bonus_", "")
            val_int = int(val_str)
            crud.set_referral_config(db, "referee_discount_idr", str(val_int))
            await query.answer(f"Potongan teman diset ke Rp {val_int:,}!", show_alert=True)
            text = build_admin_referral_view(db)
            markup = build_admin_referral_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_ref_custom_bonus":
            context.user_data["admin_awaiting_ref_custom_bonus"] = True
            context.user_data["admin_awaiting_ref_custom_reward"] = False
            context.user_data["admin_awaiting_ref_custom_min_trade"] = False
            await query.answer()
            await query.message.reply_text(
                "✏️ <b>Ketik Nominal Potongan Teman (Referee)</b>\n\n"
                "Silakan ketik nominal potongan/cashback transaksi pertama baru dalam Rupiah (contoh: <code>3000</code> atau <code>5000</code>):",
                parse_mode="HTML"
            )

        elif data == "admin_ref_pick_min_trade":
            curr_min = crud.get_referral_config(db, "min_trade_amount_idr") or "0"
            min_val = int(curr_min)
            desc_min = f"Rp {min_val:,}" if min_val > 0 else "Tanpa Minimal (Semua Order)"
            text = (
                "🛒 <b>ATUR MINIMAL PEMBELIAN / TRANSAKSI TEMAN</b>\n\n"
                f"Aturan saat ini: <b>{desc_min}</b>\n\n"
                "Teman yang diundang harus menyelesaikan transaksi minimal sebesar nominal ini agar reward pengundang & potongan teman dapat dicairkan.\n\n"
                "Pilih salah satu nominal cepat di bawah atau klik tombol kustom:"
            )
            markup = build_admin_pick_min_trade_keyboard()
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data.startswith("admin_ref_set_min_trade_"):
            val_str = data.replace("admin_ref_set_min_trade_", "")
            val_int = int(val_str)
            crud.set_referral_config(db, "min_trade_amount_idr", str(val_int))
            msg_alert = f"Min. pembelian diset ke Rp {val_int:,}!" if val_int > 0 else "Min. pembelian dinonaktifkan (bebas nominal)!"
            await query.answer(msg_alert, show_alert=True)
            text = build_admin_referral_view(db)
            markup = build_admin_referral_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_ref_custom_min_trade":
            context.user_data["admin_awaiting_ref_custom_min_trade"] = True
            context.user_data["admin_awaiting_ref_custom_reward"] = False
            context.user_data["admin_awaiting_ref_custom_bonus"] = False
            await query.answer()
            await query.message.reply_text(
                "✏️ <b>Ketik Nominal Minimal Pembelian Teman</b>\n\n"
                "Silakan ketik nominal minimal transaksi baru dalam Rupiah (contoh: <code>50000</code> atau <code>100000</code>, ketik <code>0</code> untuk tanpa minimal):",
                parse_mode="HTML"
            )

        elif data == "admin_panel_emojis":
            text = build_admin_emojis_view()
            buttons = [
                [InlineKeyboardButton("📋 Tampilkan List Emoji (/listemojis)", callback_data="admin_panel_list_emojis")],
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            await query.answer("Manajemen emoji dimuat.")

        elif data == "admin_panel_list_emojis":
            lines = ["✨ <b>DAFTAR CUSTOM EMOJI AKTIF BOT</b>\n"]
            for key, cid in sorted(CUSTOM_EMOJI_IDS.items()):
                alt = CUSTOM_EMOJI_ALTS.get(key, "✨")
                preview = tg_emoji(key, alt)
                lines.append(f"• <b>{key}:</b> {preview} (ID: <code>{cid}</code>)")
            lines.append("\n💡 <i>Gunakan <code>/setemoji [KEY] [EMOJI]</code> atau <code>/syncpack [PACK]</code> untuk mengubah.</i>")
            
            buttons = [
                [InlineKeyboardButton("🔙 Kembali ke Kelola Emoji", callback_data="admin_panel_emojis")],
                [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
            ]
            await query.edit_message_text(text="\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            await query.answer("Daftar emoji aktif dimuat.")

        # ─── TOP SPENDER (Phase 7) ──────────────────────────
        elif data == "admin_panel_top_spenders" or data.startswith("admin_top_spender_p_"):
            period = 30
            if data.startswith("admin_top_spender_p_"):
                try:
                    period = int(data.replace("admin_top_spender_p_", ""))
                except Exception:
                    period = 30
            text = build_admin_top_spenders_view(db, period_days=period)
            markup = build_admin_top_spenders_keyboard(period_days=period)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer(f"Top Spender ({period} hari) dimuat.")

        elif data.startswith("admin_top_spender_exec_"):
            period = 30
            try:
                period = int(data.replace("admin_top_spender_exec_", ""))
            except Exception:
                period = 30

            await query.answer("⏳ Sedang memproses pembagian reward Top Spender...", show_alert=False)
            from services.campaign_service import execute_top_spender_campaign
            bot_me = await context.bot.get_me() if context.bot else None
            bot_username = bot_me.username if bot_me else "Hsnpro_bot"

            result = await execute_top_spender_campaign(
                db=db,
                bot=context.bot,
                admin_id=user_id,
                period_days=period,
                bot_username=bot_username,
            )

            if result.get("error"):
                await query.answer(f"⚠️ {result['error']}", show_alert=True)
            else:
                from bot.utils.formatter import format_idr
                cnt = result.get("distributed_count", 0)
                tot = result.get("total_amount", 0)
                ns = result.get("notif_success", 0)
                await query.answer(
                    f"✅ Sukses! {cnt} pemenang menerima reward (Total: {format_idr(tot)}). Notif: {ns}/{cnt}",
                    show_alert=True,
                )

            text = build_admin_top_spenders_view(db, period_days=period)
            markup = build_admin_top_spenders_keyboard(period_days=period)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        # ─── RANDOM DRAW / UNDI PEMENANG (Phase 7) ───────────
        elif data == "admin_panel_random_draw" or data.startswith("admin_draw_pool_"):
            pool_seg = "ACTIVE_30D"
            if data.startswith("admin_draw_pool_"):
                pool_seg = data.replace("admin_draw_pool_", "")
            text = build_admin_random_draw_view(db, pool_segment=pool_seg)
            markup = build_admin_random_draw_keyboard(pool_segment=pool_seg)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer(f"Pool {pool_seg} dimuat.")

        elif data.startswith("admin_draw_exec_"):
            # format: admin_draw_exec_{pool_segment}_{winner_count}_{reward_per_winner}
            parts = data.replace("admin_draw_exec_", "").split("_")
            if len(parts) >= 3:
                pool_seg = parts[0]
                winner_cnt = int(parts[1])
                reward_amt = int(parts[2])

                await query.answer("🎲 Mengundi & membagikan saldo pemenang...", show_alert=False)
                from services.campaign_service import execute_random_winner_campaign
                bot_me = await context.bot.get_me() if context.bot else None
                bot_username = bot_me.username if bot_me else "Hsnpro_bot"

                res = await execute_random_winner_campaign(
                    db=db,
                    bot=context.bot,
                    admin_id=user_id,
                    pool_segment=pool_seg,
                    winner_count=winner_cnt,
                    reward_per_winner=reward_amt,
                    bot_username=bot_username,
                )

                if res.get("error"):
                    await query.answer(f"⚠️ {res['error']}", show_alert=True)
                else:
                    from bot.utils.formatter import format_idr
                    cnt = res.get("distributed_count", 0)
                    tot = res.get("total_amount", 0)
                    ns = res.get("notif_success", 0)
                    await query.answer(
                        f"🎉 {cnt} pemenang acak terpilih! Total: {format_idr(tot)}. Notif: {ns}/{cnt}",
                        show_alert=True,
                    )

                text = build_admin_random_draw_view(db, pool_segment=pool_seg)
                markup = build_admin_random_draw_keyboard(pool_segment=pool_seg)
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        # ─── LOYALTY CONFIG (Phase 7) ─────────────────────────
        elif data == "admin_panel_loyalty":
            text = build_admin_loyalty_view(db)
            markup = build_admin_loyalty_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer("Pengaturan loyalty dimuat.")

        elif data == "admin_loyalty_toggle":
            curr = crud.get_loyalty_config(db, "loyalty_enabled") or "true"
            is_on = curr.lower() == "true"
            new_val = "false" if is_on else "true"
            crud.set_loyalty_config(db, "loyalty_enabled", new_val)
            status_str = "dinonaktifkan" if new_val == "false" else "diaktifkan"
            await query.answer(f"Program loyalty berhasil {status_str}!", show_alert=True)
            text = build_admin_loyalty_view(db)
            markup = build_admin_loyalty_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_loyalty_pick_window":
            text = (
                "⏱️ <b>ATUR RENTANG WAKTU (WINDOW) LOYALTY</b>\n\n"
                "Pilih batas durasi hari untuk menghitung target transaksi user:\n"
                "<i>(Contoh: 5 hari = user harus menyelesaikan transaksi dalam kurun waktu 5 hari)</i>"
            )
            markup = build_admin_pick_loyalty_window_keyboard()
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data.startswith("admin_loyalty_set_window_"):
            val = int(data.replace("admin_loyalty_set_window_", ""))
            crud.set_loyalty_config(db, "window_days", str(val))
            await query.answer(f"Window loyalty diset ke {val} hari!", show_alert=True)
            text = build_admin_loyalty_view(db)
            markup = build_admin_loyalty_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_loyalty_pick_mintx":
            text = (
                "🔢 <b>ATUR TARGET JUMLAH TRANSAKSI LOYALTY</b>\n\n"
                "Pilih berapa transaksi selesai yang harus dicapai user dalam window waktu:"
            )
            markup = build_admin_pick_loyalty_mintx_keyboard()
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data.startswith("admin_loyalty_set_mintx_"):
            val = int(data.replace("admin_loyalty_set_mintx_", ""))
            crud.set_loyalty_config(db, "min_tx_count", str(val))
            await query.answer(f"Target transaksi loyalty diset ke {val}x order!", show_alert=True)
            text = build_admin_loyalty_view(db)
            markup = build_admin_loyalty_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_loyalty_pick_reward":
            text = (
                "💰 <b>ATUR BESARAN REWARD LOYALTY</b>\n\n"
                "Pilih jumlah saldo bot yang akan dikreditkan otomatis saat user lolos target:"
            )
            markup = build_admin_pick_loyalty_reward_keyboard()
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data.startswith("admin_loyalty_set_reward_"):
            val = int(data.replace("admin_loyalty_set_reward_", ""))
            crud.set_loyalty_config(db, "reward_amount_idr", str(val))
            await query.answer(f"Reward loyalty diset ke Rp {val:,}!", show_alert=True)
            text = build_admin_loyalty_view(db)
            markup = build_admin_loyalty_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_loyalty_pick_minamt":
            text = (
                "🛒 <b>ATUR MINIMAL NOMINAL TRANSAKSI PER ORDER</b>\n\n"
                "Hanya order dengan nominal >= nilai ini yang akan dihitung ke progress loyalty:"
            )
            markup = build_admin_pick_loyalty_minamt_keyboard()
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data.startswith("admin_loyalty_set_minamt_"):
            val = int(data.replace("admin_loyalty_set_minamt_", ""))
            crud.set_loyalty_config(db, "min_tx_amount_idr", str(val))
            msg = f"Min. nominal diset ke Rp {val:,}!" if val > 0 else "Min. nominal order dinonaktifkan (bebas nominal)!"
            await query.answer(msg, show_alert=True)
            text = build_admin_loyalty_view(db)
            markup = build_admin_loyalty_keyboard(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_loyalty_check_eligible":
            eligible = crud.get_loyalty_eligible_users(db)
            if not eligible:
                await query.answer("ℹ️ Saat ini belum ada user yang menunggu reward loyalty.", show_alert=True)
            else:
                lines = [f"📊 <b>DAFTAR USER QUALIFIED ({len(eligible)} User):</b>\n"]
                for item in eligible[:15]:
                    lines.append(f"• User ID <code>{item['telegram_id']}</code>: {item['tx_count']} transaksi selesai")
                buttons = [
                    [InlineKeyboardButton("🔙 Kembali ke Loyalty", callback_data="admin_panel_loyalty")],
                    [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
                ]
                await query.edit_message_text(text="\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
                await query.answer()

        # ─── WEEKLY REPORT (Phase 8) ──────────────────────────
        elif data == "admin_panel_weekly_report" or data.startswith("admin_weekly_p_"):
            days = 7
            if data.startswith("admin_weekly_p_"):
                try:
                    days = int(data.replace("admin_weekly_p_", ""))
                except Exception:
                    days = 7
            text, markup = build_admin_weekly_report_view(db, days=days)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer(f"Rekap transaksi {days} hari dimuat.")

        elif data.startswith("admin_export_csv_"):
            days = 7
            try:
                days = int(data.replace("admin_export_csv_", ""))
            except Exception:
                days = 7

            await query.answer("⏳ Menyiapkan file spreadsheet (.CSV)...", show_alert=False)
            from services.report_service import (
                get_weekly_transactions_data,
                generate_weekly_report_csv_buffer,
            )
            transactions = get_weekly_transactions_data(db, days=days)
            csv_buf = generate_weekly_report_csv_buffer(transactions)
            filename = f"laporan_transaksi_{days}hari_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
            caption = (
                f"📑 <b>File Laporan Transaksi ({days} Hari Terakhir)</b>\n"
                f"Total: <b>{len(transactions)} baris data transaksi</b>.\n\n"
                f"💡 <i>Dapat langsung di-import ke Google Sheets atau dibuka di Microsoft Excel.</i>"
            )
            await context.bot.send_document(
                chat_id=user_id,
                document=csv_buf,
                filename=filename,
                caption=caption,
                parse_mode="HTML",
            )
            await query.answer("✅ File laporan .CSV berhasil dikirim!", show_alert=True)

        elif data == "admin_panel_close":
            await query.answer("Panel ditutup.")
            await query.message.delete()

    except Exception as exc:
        logger.error(f"Error in admin_panel_callback ({data}): {exc}", exc_info=True)
        await query.answer(f"❌ Error: {exc}", show_alert=True)
    finally:
        db.close()


async def weekly_report_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /weeklyreport atau /report: kirim ringkasan & file export CSV mingguan ke Admin."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    days = 7
    if context.args:
        try:
            days = int(context.args[0].lower().replace("d", "").replace("hari", ""))
        except Exception:
            days = 7

    db = SessionLocal()
    try:
        from services.report_service import (
            get_weekly_transactions_data,
            calculate_weekly_summary,
            generate_weekly_report_csv_buffer,
            format_weekly_report_telegram_message,
        )
        transactions = get_weekly_transactions_data(db, days=days)
        summary = calculate_weekly_summary(transactions, days=days)
        text = format_weekly_report_telegram_message(summary)
        csv_buf = generate_weekly_report_csv_buffer(transactions)
        filename = f"laporan_transaksi_{days}hari_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

        await update.message.reply_text(text, parse_mode="HTML")
        await update.message.reply_document(
            document=csv_buf,
            filename=filename,
            caption=f"📑 <b>Export Laporan {days} Hari</b> ({len(transactions)} data transaksi)",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error in weekly_report_command_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal membuat laporan mingguan.")
    finally:
        db.close()


async def test_testimony_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /testtesti untuk admin: uji kirim postingan contoh ke channel testimoni."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    from services.testimony_service import DEFAULT_TESTIMONY_CHANNEL, format_testimony_message
    channel_target = os.getenv("TESTIMONY_CHANNEL") or getattr(settings, "TESTIMONY_CHANNEL_ID", None) or DEFAULT_TESTIMONY_CHANNEL
    
    bot_username = (await context.bot.get_me()).username if context.bot else "TokoKoinID_Bot"

    sample_msg = (
        "🧪 <b>[TEST KONEKSI BOT]</b>\n"
        + format_testimony_message(
            order_type="buy",
            crypto_symbol="USDT",
            network="BSC",
            nominal_idr=520000,
            username="test_buyer",
            telegram_id=12345678,
            tx_hash="0x1234567890abcdef1234567890abcdef12345678",
            bot_username=bot_username,
        )
    )

    try:
        sent = await context.bot.send_message(
            chat_id=channel_target,
            text=sample_msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await update.message.reply_text(
            f"✅ <b>Koneksi Channel Berhasil!</b>\n\n"
            f"Pesan uji coba berhasil diposting ke <code>{channel_target}</code> (Message ID: {sent.message_id}).",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error test_testimony: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ <b>Gagal Kirim ke Channel ({channel_target}):</b>\n\n"
            f"<code>{html.escape(str(e))}</code>\n\n"
            f"💡 <b>Langkah Perbaikan:</b>\n"
            f"1. Buka channel <code>{channel_target}</code> di Telegram.\n"
            f"2. Buka menu <b>Administrators (Pengurus)</b> ➔ <b>Add Administrator</b>.\n"
            f"3. Cari username bot Anda (<code>@{bot_username}</code>) dan tambahkan sebagai Admin dengan izin <b>Post Messages (Posting Pesan)</b>.",
            parse_mode="HTML",
        )



async def syncpack_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Otomatisasi: Sinkronisasi satu set emoji pack Telegram ke bot secara instan.
    Format: /syncpack [NAMA_PACK_ATAU_URL]
    Contoh: /syncpack Crypto3DEmoji
            /syncpack https://t.me/addemoji/Crypto3DEmoji
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    msg = update.effective_message
    if not msg:
        return

    args = context.args or []
    if not args:
        await msg.reply_text(
            "⚡️ <b>OTOMASI SINKRONISASI EMOJI PACK</b>\n\n"
            "Kirimkan tautan atau nama paket emoji Telegram Anda:\n\n"
            "<b>Format:</b> <code>/syncpack [NAMA_PACK_ATAU_URL]</code>\n"
            "<b>Contoh:</b>\n"
            "• <code>/syncpack Crypto3DAnimated</code>\n"
            "• <code>/syncpack https://t.me/addemoji/Crypto3DAnimated</code>\n\n"
            "<i>Bot akan otomatis menarik seluruh custom_emoji_id dari pack tersebut dan langsung mengaktifkannya di bot tanpa perlu restart!</i>",
            parse_mode="HTML"
        )
        return

    pack_input = args[0].strip()
    pack_name = pack_input.split("/")[-1].replace("t.me/addemoji/", "").strip()

    status_msg = await msg.reply_text(f"⏳ Sedang membaca emoji pack Telegram: <code>{pack_name}</code>...", parse_mode="HTML")

    try:
        sticker_set = await context.bot.get_sticker_set(name=pack_name)
        if not sticker_set or not sticker_set.stickers:
            await status_msg.edit_text(f"❌ Emoji pack <code>{pack_name}</code> tidak ditemukan atau kosong.", parse_mode="HTML")
            return

        synced = sync_from_stickers(sticker_set.stickers)
        if not synced:
            await status_msg.edit_text(
                f"⚠️ Berhasil membaca pack <b>{sticker_set.title}</b> ({len(sticker_set.stickers)} item), "
                "namun tidak ada Custom Emoji ID yang valid ditemukan.",
                parse_mode="HTML"
            )
            return

        lines = [
            f"🎉 <b>BERHASIL SINKRONISASI EMOJI PACK!</b>",
            f"📦 <b>Pack:</b> {sticker_set.title} (<code>{pack_name}</code>)",
            f"✨ <b>Total Disinkronkan:</b> {len(synced)} emoji\n",
            "<b>Daftar Emoji yang Diperbarui:</b>"
        ]

        for key, (cid, alt) in synced.items():
            preview = tg_emoji(key, alt)
            lines.append(f"• <b>{key}:</b> {preview} (ID: <code>{cid}</code>)")

        lines.append("\n✅ <i>Semua menu bot kini otomatis menggunakan emoji dari pack baru Anda!</i>")
        await status_msg.edit_text("\n".join(lines), parse_mode="HTML")

    except Exception as exc:
        logger.error("Error in syncpack_handler: %s", exc, exc_info=True)
        await status_msg.edit_text(f"❌ Gagal menyinkronkan emoji pack: <code>{str(exc)}</code>", parse_mode="HTML")


async def setemoji_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Otomatisasi: Daftarkan atau ganti satu custom emoji secara instan.
    Format: /setemoji [KEY] [EMOJI_CUSTOM]
    Contoh: /setemoji BOT 🤖
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    msg = update.effective_message
    if not msg:
        return

    args = context.args or []
    if not args or len(args) < 1:
        await msg.reply_text(
            "⚡️ <b>SET SINGLE CUSTOM EMOJI</b>\n\n"
            "<b>Format:</b> <code>/setemoji [KEY] [EMOJI_PREMIUM]</code>\n\n"
            "<b>Contoh:</b> <code>/setemoji BOT 🤖</code>\n\n"
            "<b>Daftar KEY yang Tersedia:</b>\n"
            "<code>BOT, USER, CROWN, VERIFIED, CHART, MONEY_BAG, DOLLAR, CARD, COIN, CART, BOX, SWAP, CHECK, CROSS, WARNING, PHONE, CHAT, HISTORY, FIRE, ROCKET, DIAMOND, SPARKLES, STAR, PARTY, CALENDAR, WAVE</code>",
            parse_mode="HTML"
        )
        return

    key = args[0].upper().strip()
    entities = msg.entities or []
    custom_emojis = [e for e in entities if getattr(e, "type", None) == "custom_emoji" or str(getattr(e, "type", "")) == "MessageEntityType.CUSTOM_EMOJI"]

    if not custom_emojis:
        await msg.reply_text(
            f"❌ Tidak ada custom emoji premium yang terdeteksi di pesan.\n\n"
            f"Pastikan Anda mengirim emoji dari custom emoji pack Telegram: <code>/setemoji {key} [EMOJI]</code>",
            parse_mode="HTML"
        )
        return

    target_entity = custom_emojis[0]
    emoji_id = getattr(target_entity, "custom_emoji_id", "")
    offset = getattr(target_entity, "offset", 0)
    length = getattr(target_entity, "length", 1)
    emoji_char = msg.text[offset:offset+length] if msg.text else "✨"

    set_custom_emoji(key, emoji_id, emoji_char)
    preview = tg_emoji(key, emoji_char)

    await msg.reply_text(
        f"✅ <b>CUSTOM EMOJI BERHASIL DIPERBARUI!</b>\n\n"
        f"• <b>Key:</b> <code>{key}</code>\n"
        f"• <b>Preview:</b> {preview}\n"
        f"• <b>ID:</b> <code>{emoji_id}</code>\n"
        f"• <b>Alt:</b> {emoji_char}\n\n"
        f"<i>Perubahan langsung aktif di seluruh menu bot!</i>",
        parse_mode="HTML"
    )


async def listemojis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan daftar seluruh custom emoji yang sedang aktif di bot."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    lines = ["✨ <b>DAFTAR CUSTOM EMOJI AKTIF BOT</b>\n"]
    for key, cid in sorted(CUSTOM_EMOJI_IDS.items()):
        alt = CUSTOM_EMOJI_ALTS.get(key, "✨")
        preview = tg_emoji(key, alt)
        lines.append(f"• <b>{key}:</b> {preview} (ID: <code>{cid}</code>)")

    lines.append("\n💡 <i>Gunakan <code>/setemoji [KEY] [EMOJI]</code> atau <code>/syncpack [PACK]</code> untuk mengubah.</i>")
    lines.append("🔄 <i>Gunakan <code>/resetemojis</code> untuk mereset ke default bawaan.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def resetemojis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mereset seluruh custom emoji kembali ke default bawaan."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    reset_custom_emojis()
    await update.message.reply_text("🔄 <b>Custom emoji berhasil direset ke konfigurasi default bawaan Telegram!</b>", parse_mode="HTML")


async def getemoji_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Mendeteksi custom_emoji_id dari pesan untuk dimasukkan ke bot/utils/emojis.py.
    Cara pakai: Kirim custom emoji 3D premium ke bot dengan command /getemoji [EMOJI]
    """
    msg = update.effective_message
    if not msg:
        return
    
    entities = msg.entities or []
    custom_emojis = [e for e in entities if getattr(e, "type", None) == "custom_emoji" or str(getattr(e, "type", "")) == "MessageEntityType.CUSTOM_EMOJI"]
    
    if not custom_emojis:
        await msg.reply_text(
            "💡 <b>CARA MENDAPATKAN ID EMOJI 3D PREMIUM:</b>\n\n"
            "Ketik <code>/getemoji</code> lalu sertakan emoji premium 3D dari sticker/emoji pack Telegram Anda.\n\n"
            "<i>Contoh:</i> Kirim pesan <code>/getemoji [EMOJI_PREMIUM]</code>",
            parse_mode="HTML"
        )
        return

    res_lines = ["✨ <b>CUSTOM EMOJI 3D TERDETEKSI:</b>\n"]
    for i, e in enumerate(custom_emojis, 1):
        emoji_id = getattr(e, "custom_emoji_id", "")
        offset = getattr(e, "offset", 0)
        length = getattr(e, "length", 1)
        emoji_char = msg.text[offset:offset+length] if msg.text else "✨"
        
        res_lines.append(
            f"<b>{i}. Emoji:</b> {emoji_char}\n"
            f"• <b>Custom Emoji ID:</b> <code>{emoji_id}</code>\n"
            f"• <b>Format HTML:</b>\n"
            f"<code>&lt;tg-emoji emoji-id=\"{emoji_id}\"&gt;{emoji_char}&lt;/tg-emoji&gt;</code>\n"
        )
    
    res_lines.append("💡 <i>Gunakan perintah <code>/setemoji [KEY] [EMOJI]</code> untuk langsung memasangnya ke bot.</i>")
    await msg.reply_text("\n".join(res_lines), parse_mode="HTML")


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan statistik harian transaksi bot."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    db = SessionLocal()
    try:
        stats = crud.get_daily_stats(db)
        total_users = crud.get_user_count(db)
        
        stats_text = (
            "📊 <b>STATISTIK HARIAN BOT</b>\n"
            f"📅 Tanggal: {datetime.now(timezone.utc).strftime('%d-%m-%Y')}\n\n"
            f"👤 <b>Total Pengguna:</b> {total_users} member\n"
            f"🛒 <b>Order Hari Ini:</b> {stats['total_orders_today']} order\n"
            f"✅ <b>Order Sukses Hari Ini:</b> {stats['completed_orders_today']} order\n"
            f"💳 <b>Volume Transaksi Hari Ini:</b> {format_idr(stats['total_volume_idr_today'])}\n"
        )
        await update.message.reply_text(stats_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error stats_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memuat statistik.")
    finally:
        db.close()


async def setspread_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mengupdate markup spread persentase untuk symbol tertentu."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    # Validasi argument: /setspread USDT 1.2
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Format salah. Contoh penggunaan:\n"
            "<code>/setspread USDT 1.5</code>",
            parse_mode="HTML"
        )
        return

    symbol = context.args[0].upper()
    try:
        spread_pct = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Nilai persentase spread harus berupa angka desimal.")
        return

    db = SessionLocal()
    try:
        config = crud.update_price_config(db, symbol, spread_pct)
        if config:
            await update.message.reply_text(
                f"✅ Spread harga untuk <b>{symbol}</b> berhasil diperbarui menjadi <b>{spread_pct}%</b>.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(f"❌ Koin/Token <b>{symbol}</b> tidak didukung atau tidak aktif.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error setspread: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal mengupdate spread harga.")
    finally:
        db.close()


async def orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan list order yang berstatus pending atau paid (butuh review admin)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    db = SessionLocal()
    try:
        # Ambil order yang statusnya pending, paid, payout_processing, atau manual_review
        orders = (
            db.query(Order)
            .filter(Order.status.in_(["pending", "paid", "payout_processing", "manual_review"]))
            .order_by(Order.created_at.desc())
            .limit(20)
            .all()
        )
        
        if not orders:
            await update.message.reply_text("📥 <b>Tidak ada order aktif/tertunda saat ini.</b>", parse_mode="HTML")
            return

        text_lines = ["📥 <b>DAFTAR ORDER AKTIF (PENDING/PAID)</b>\n"]
        for o in orders:
            o_type = "🛒 BELI" if o.order_type == "buy" else "💵 JUAL"
            crypto_str = format_crypto(float(o.crypto_amount), o.crypto_symbol)
            
            text_lines.append(
                f"• <b>{o.order_id}</b> ({o_type})\n"
                f"  🚦 Status: <b>{o.status.upper()}</b>\n"
                f"  🪙 Koin: <code>{crypto_str} ({o.network})</code>\n"
                f"  💳 IDR: <code>{format_idr(o.total_idr)}</code>\n"
                f"  👤 User ID: <code>{o.telegram_id}</code>\n"
            )
        
        await update.message.reply_text("\n".join(text_lines), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error orders_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memuat daftar order.")
    finally:
        db.close()


async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Mengonfirmasi penyelesaian order secara manual oleh admin.
    Digunakan jika:
      - Sell order: Admin sudah mengirim Rupiah ke rekening customer.
      - Buy order: Crypto gagal terkirim otomatis lalu dikirim manual oleh admin.
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Format salah. Sertakan ID Order. Contoh:\n"
            "<code>/confirm ORD-20260527-XYZ</code>",
            parse_mode="HTML"
        )
        return

    order_id = context.args[0].strip()
    
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await update.message.reply_text(f"❌ Order <code>{order_id}</code> tidak ditemukan.", parse_mode="HTML")
            return

        if order.order_type == "sell" and order.status not in ("CRYPTO_CONFIRMED", "completed", "COMPLETED"):
            await update.message.reply_text("⚠️ Deposit belum terverifikasi. Jangan transfer Rupiah/selesaikan order dahulu.")
            return
        was_already_completed = order.status.lower() == "completed"

        if not was_already_completed:
            # Update status order ke completed jika belum completed
            crud.update_order_status(
                db, 
                order_id, 
                new_status="completed", 
                completed_at=datetime.utcnow()
            )
            crud.release_order_inventory(db, order_id)
        
        # Kirim notifikasi sukses ke user
        from bot.utils.telegram_utils import safe_send_message
        from bot.utils.messages import build_sell_completion_message, build_buy_completion_message
        
        if order.order_type == "sell":
            user_msg = build_sell_completion_message(order)
        else:
            user_msg = build_buy_completion_message(order)
            
        menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]])
        sent = await safe_send_message(context.bot, order.telegram_id, user_msg, reply_markup=menu_keyboard)

        if was_already_completed:
            status_text = "sudah berstatus COMPLETED sebelumnya"
            notif_text = "Notifikasi berhasil dikirim ulang ke user" if sent else "Gagal mengirim notifikasi ke user"
            await update.message.reply_text(
                f"ℹ️ Order <code>{order_id}</code> {status_text}.\n"
                f"📬 <b>{notif_text}</b> (User ID: <code>{order.telegram_id}</code>).",
                parse_mode="HTML"
            )
        else:
            notif_text = "notifikasi sukses telah dikirim ke user" if sent else "namun pengiriman notifikasi ke user gagal"
            await update.message.reply_text(
                f"✅ Order <code>{order_id}</code> berhasil dikonfirmasi sebagai <b>COMPLETED</b> dan {notif_text} (User ID: <code>{order.telegram_id}</code>).",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"Error confirm_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memproses konfirmasi order.")
    finally:
        db.close()


async def _reverify_sell_deposit(db, order):
    """Verifikasi ulang deposit sell dengan jendela resmi (default 24 jam)."""
    from services import tx_verifier

    tx_hash = (order.deposit_tx_hash or order.tx_hash or "").strip()
    if not tx_hash or tx_hash.startswith("PHOTO:"):
        return None
    from services.detector import DepositDetector
    if DepositDetector._is_hash_used(db, tx_hash, exclude_order=order.order_id):
        return {"verified": False, "reason": "TX hash sudah diklaim order lain (indikasi replay)."}
    base = order.created_at or datetime.utcnow()
    return await tx_verifier.verify_deposit(
        network=order.network,
        symbol=order.crypto_symbol,
        tx_hash=tx_hash,
        expected_wallet=order.deposit_wallet or "",
        expected_amount=order.crypto_amount,
        not_before=order.created_at,
        not_after=base + timedelta(minutes=settings.SELL_DEPOSIT_WINDOW_MINUTES),
    )


async def _finish_sell_order(db, order, query, bot) -> None:
    """Selesaikan order sell: status completed, notifikasi user, tanda di pesan admin."""
    from bot.utils.telegram_utils import safe_send_message
    from bot.utils.messages import build_sell_completion_message

    was_already_completed = order.status.lower() == "completed"
    if not was_already_completed:
        crud.update_order_status(db, order.order_id, new_status="completed", completed_at=datetime.utcnow())
        crud.release_order_inventory(db, order.order_id)

    menu_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Menu Utama", callback_data="menu_back",
                             icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
    ]])
    sent = await safe_send_message(
        bot, order.telegram_id, build_sell_completion_message(order), reply_markup=menu_keyboard)

    alert_text = ("✅ Berhasil konfirmasi & notifikasi terkirim ke user!" if sent
                  else "⚠️ Order COMPLETED namun gagal mengirim notifikasi ke user.")
    if was_already_completed:
        alert_text = ("ℹ️ Order sudah COMPLETED. Notifikasi dikirim ulang ke user." if sent
                      else "ℹ️ Order sudah COMPLETED.")
    await query.answer(alert_text, show_alert=True)

    msg = query.message
    completion_tag = "\n\n✅ <b>RUPIAH SUDAH DITRANSFER OLEH ADMIN (COMPLETED)</b>"
    if msg.caption and completion_tag not in msg.caption:
        await query.edit_message_caption(caption=f"{msg.caption}{completion_tag}", parse_mode="HTML")
    elif msg.text and completion_tag not in msg.text:
        await query.edit_message_text(text=f"{msg.text}{completion_tag}", parse_mode="HTML")


async def admin_confirm_sell_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Konfirmasi transfer Rupiah untuk order Sell."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_confirm_sell_", "")
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await query.answer("❌ Order tidak ditemukan.", show_alert=True)
            return

        if order.order_type != "sell":
            await query.answer("❌ Order ini bukan order jual.", show_alert=True)
            return

        if order.status not in ("CRYPTO_CONFIRMED", "completed", "COMPLETED"):
            # Coba verifikasi ulang dengan jendela resmi sebelum menolak.
            verified = await _reverify_sell_deposit(db, order)
            if verified and verified.get("verified"):
                crud.update_order_status(db, order_id, new_status="CRYPTO_CONFIRMED")
                db.refresh(order)
            else:
                reason = (verified or {}).get("reason") or "Tidak ada TX hash yang bisa diverifikasi on-chain."
                await query.message.reply_text(
                    (
                        f"⛔ <b>Belum bisa diselesaikan otomatis</b>\n\n"
                        f"Order: <code>{order_id}</code>\n"
                        f"Status: <b>{order.status}</b>\n"
                        f"TX Hash: <code>{order.deposit_tx_hash or order.tx_hash or '-'}</code>\n"
                        f"Alasan verifikasi: <i>{reason}</i>\n\n"
                        f"Periksa mutasi wallet. Bila koin benar-benar sudah masuk, gunakan tombol "
                        f"<b>Selesaikan Manual</b> (tercatat di audit log)."
                    ),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⚠️ Selesaikan Manual (Admin)", callback_data=f"admin_force_sell_{order_id}")
                    ]]),
                    parse_mode="HTML",
                )
                await query.answer("Deposit belum terverifikasi otomatis.", show_alert=True)
                return

        await _finish_sell_order(db, order, query, context.bot)
    except Exception as e:
        logger.error(f"Error admin_confirm_sell_callback {order_id}: {e}", exc_info=True)
        await query.answer("❌ Gagal memproses konfirmasi.", show_alert=True)
    finally:
        db.close()


async def admin_force_sell_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin menandai deposit sell terverifikasi manual (override tercatat di audit log)."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_force_sell_", "").strip()
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order or order.order_type != "sell":
            await query.answer("❌ Order jual tidak ditemukan.", show_alert=True)
            return
        if order.status.lower() == "completed":
            await query.answer("ℹ️ Order sudah COMPLETED.", show_alert=True)
            return

        old_status = order.status
        crud.update_order_status(db, order_id, new_status="CRYPTO_CONFIRMED")
        db.add(AuditLog(
            telegram_id=order.telegram_id,
            action="SELL_MANUAL_OVERRIDE",
            order_id=order_id,
            from_status=old_status,
            to_status="CRYPTO_CONFIRMED",
            details=(
                f"Admin {user_id} menandai deposit terverifikasi manual. "
                f"TX hash: {order.deposit_tx_hash or order.tx_hash or '-'}"
            ),
        ))
        db.commit()
        db.refresh(order)
        await query.answer("✅ Deposit ditandai terverifikasi manual.", show_alert=False)
        await _finish_sell_order(db, order, query, context.bot)
    except Exception as e:
        logger.error(f"Error admin_force_sell_callback {order_id}: {e}", exc_info=True)
        await query.answer("❌ Gagal memproses override manual.", show_alert=True)
    finally:
        db.close()


async def verifysell_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /verifysell <order_id>: verifikasi ulang deposit order jual (admin)."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Format: <code>/verifysell &lt;order_id&gt;</code>", parse_mode="HTML")
        return

    order_id = context.args[0].strip()
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await update.message.reply_text(f"❌ Order <code>{order_id}</code> tidak ditemukan.", parse_mode="HTML")
            return

        verified = await _reverify_sell_deposit(db, order)
        if verified and verified.get("verified"):
            if order.status.lower() != "crypto_confirmed":
                crud.update_order_status(db, order_id, new_status="CRYPTO_CONFIRMED")
            await update.message.reply_text(
                (
                    f"✅ <b>Deposit terverifikasi</b>\n\n"
                    f"Order: <code>{order_id}</code>\n"
                    f"Nominal: {verified.get('amount')} {order.crypto_symbol} ({order.network})\n"
                    f"Status sekarang: <b>CRYPTO_CONFIRMED</b>. Silakan klik tombol "
                    f"<b>Sudah Ditransfer</b> untuk menyelesaikan order."
                ),
                parse_mode="HTML",
            )
        else:
            reason = (verified or {}).get("reason") or "Tidak ada TX hash yang bisa diverifikasi on-chain."
            await update.message.reply_text(
                (
                    f"⚠️ <b>Belum terverifikasi otomatis</b>\n\n"
                    f"Order: <code>{order_id}</code>\n"
                    f"Status: <b>{order.status}</b>\n"
                    f"Alasan: <i>{reason}</i>\n\n"
                    f"Jika dana sudah masuk, gunakan tombol <b>Selesaikan Manual (Admin)</b> pada "
                    f"notifikasi order (tercatat di audit log)."
                ),
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"Error verifysell {order_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memverifikasi order.")
    finally:
        db.close()


async def admin_upload_proof_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Inisiasi upload bukti transfer gambar untuk order Sell."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_upload_proof_", "").strip()
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await query.answer("❌ Order tidak ditemukan.", show_alert=True)
            return

        # Simpan state di user_data admin
        context.user_data["admin_awaiting_proof_order_id"] = order_id
        await query.answer("📸 Silakan kirimkan foto bukti transfer.", show_alert=False)

        prompt_msg = (
            f"📸 <b>UPLOAD BUKTI TRANSFER PEMBAYARAN</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"Total Rupiah: <b>{format_idr(int(order.total_idr or 0))}</b>\n"
            f"Rekening Tujuan: <code>{order.buyer_wallet}</code>\n\n"
            f"👉 <b>Silakan kirimkan FOTO / SCREENSHOT bukti transfer ke chat ini sekarang.</b>\n"
            f"Bot akan otomatis meneruskan bukti foto tersebut langsung ke pembeli dan menyelesaikan order."
        )
        await query.message.reply_text(prompt_msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error admin_upload_proof_callback {order_id}: {e}", exc_info=True)
        await query.answer("❌ Terjadi kesalahan.", show_alert=True)
    finally:
        db.close()


async def handle_admin_upload_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menangani foto bukti transfer yang dikirim oleh admin, meneruskannya ke user, dan menyelesaikan order."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    order_id = context.user_data.pop("admin_awaiting_proof_order_id", None)
    if not order_id or not update.message or not update.message.photo:
        return

    # Ambil resolusi foto tertinggi
    photo_file_id = update.message.photo[-1].file_id

    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await update.message.reply_text("❌ Order tidak ditemukan di database.")
            return

        if order.order_type != "sell" or order.status not in ("CRYPTO_CONFIRMED", "completed", "COMPLETED"):
            await update.message.reply_text("⚠️ Deposit belum terverifikasi. Bukti pembayaran tidak boleh menyelesaikan order ini.")
            return
        # Update status order ke completed
        crud.update_order_status(
            db,
            order_id,
            new_status="completed",
            completed_at=datetime.utcnow(),
        )
        crud.release_order_inventory(db, order_id)

        # Buat caption menarik untuk user
        from bot.utils.messages import format_sell_bank_info
        from bot.utils.formatter import format_crypto, format_idr
        import html

        bank_info_str = format_sell_bank_info(order.buyer_wallet or "")
        crypto_amount_val = float(order.crypto_amount) if order.crypto_amount else 0.0
        crypto_str = format_crypto(crypto_amount_val, order.crypto_symbol)
        nominal_str = format_idr(int(order.total_idr or 0))

        user_caption = (
            f"🎉 <b>DANA TELAH DITRANSFER OLEH ADMIN!</b>\n\n"
            f"Halo kak! Pembayaran dana hasil penjualan crypto Anda telah berhasil dikirimkan oleh admin ke rekening Anda:\n\n"
            f"📝 <b>ID Order:</b> <code>{html.escape(order.order_id)}</code>\n"
            f"🪙 <b>Koin Terjual:</b> <b>{crypto_str}</b> ({html.escape(order.network)})\n"
            f"💵 <b>Dana Diterima:</b> <b>{nominal_str}</b>\n\n"
            f"🏦 <b>Rekening Tujuan:</b>\n"
            f"{bank_info_str}\n\n"
            f"📸 <i>Bukti transfer pembayaran terlampir di atas.</i>\n\n"
            f"✅ <b>Status: SELESAI / COMPLETED</b>\n\n"
            f"Silakan periksa saldo / mutasi rekening Anda.\n\n"
            f"Terimakasih sudah bertransaksi di sini, Lancar selalu 🙏🙏\n"
            f"Testimoni : t.me/TokoKoinID\n"
            f"Channel : t.me/ROBHSN_STORE_SELLER"
        )

        menu_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))
        ]])

        sent_to_user = False
        try:
            await context.bot.send_photo(
                chat_id=order.telegram_id,
                photo=photo_file_id,
                caption=user_caption,
                reply_markup=menu_keyboard,
                parse_mode="HTML"
            )
            sent_to_user = True
        except Exception as send_err:
            logger.error(f"Gagal mengirim foto bukti ke user {order.telegram_id}: {send_err}")

        # Post testimony ke channel (Phase 8)
        try:
            from services.testimony_service import post_transaction_testimony
            asyncio.create_task(post_transaction_testimony(context.bot, order, db=db))
        except Exception as texc:
            logger.warning(f"Gagal trigger testimony sell order {order.order_id}: {texc}")

        notif_admin = (
            f"✅ <b>BUKTI TRANSFER BERHASIL DITERUSKAN!</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"User ID: <code>{order.telegram_id}</code>\n"
            f"Status Order: <b>COMPLETED</b>\n"
            f"Pengiriman ke user: {'Sukses' if sent_to_user else 'Gagal (User memblokir bot/chat error)'}"
        )
        await update.message.reply_text(notif_admin, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error handle_admin_upload_proof {order_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memproses bukti transfer.")
    finally:
        db.close()


# Jeda tiap N pesan broadcast agar tidak kena flood-limit Telegram.
BROADCAST_PACE_EVERY = 20
BROADCAST_PACE_SECONDS = 1.0


def _retry_after_seconds(exc: Exception) -> float:
    """Delay aman dari RetryAfter (int maupun timedelta)."""
    try:
        value = getattr(exc, "retry_after", 1)
        if hasattr(value, "total_seconds"):
            value = value.total_seconds()
        return float(value) + 1
    except Exception:
        return 2.0


async def _send_broadcast_to_user(bot, telegram_id: int, text: str,
                                 photo_file_id: str | None = None,
                                 is_document: bool = False) -> bool:
    """Kirim 1 pesan broadcast (teks / foto / dokumen gambar); retry saat flood-limit atau format fallback."""
    caption_val = text.strip() if text else None
    for attempt in (1, 2):
        try:
            if photo_file_id:
                if is_document:
                    if caption_val and len(caption_val) > 1024:
                        await bot.send_document(chat_id=telegram_id, document=photo_file_id)
                        await bot.send_message(chat_id=telegram_id, text=caption_val, parse_mode="HTML")
                    elif caption_val:
                        await bot.send_document(
                            chat_id=telegram_id, document=photo_file_id,
                            caption=caption_val, parse_mode="HTML",
                        )
                    else:
                        await bot.send_document(chat_id=telegram_id, document=photo_file_id)
                else:
                    if caption_val and len(caption_val) > 1024:
                        await bot.send_photo(chat_id=telegram_id, photo=photo_file_id)
                        await bot.send_message(chat_id=telegram_id, text=caption_val, parse_mode="HTML")
                    elif caption_val:
                        await bot.send_photo(
                            chat_id=telegram_id, photo=photo_file_id,
                            caption=caption_val, parse_mode="HTML",
                        )
                    else:
                        await bot.send_photo(chat_id=telegram_id, photo=photo_file_id)
            else:
                await bot.send_message(chat_id=telegram_id, text=text, parse_mode="HTML")
            return True
        except RetryAfter as flood:
            await asyncio.sleep(_retry_after_seconds(flood))
            if attempt == 2:
                return False
        except BadRequest as bad_req:
            err_str = str(bad_req).lower()
            if "can't parse entities" in err_str or "entity" in err_str:
                # Fallback format plain text jika ada tag HTML / simbol tidak valid
                try:
                    if photo_file_id:
                        if is_document:
                            if caption_val and len(caption_val) > 1024:
                                await bot.send_document(chat_id=telegram_id, document=photo_file_id)
                                await bot.send_message(chat_id=telegram_id, text=caption_val, parse_mode=None)
                            elif caption_val:
                                await bot.send_document(
                                    chat_id=telegram_id, document=photo_file_id,
                                    caption=caption_val, parse_mode=None,
                                )
                            else:
                                await bot.send_document(chat_id=telegram_id, document=photo_file_id)
                        else:
                            if caption_val and len(caption_val) > 1024:
                                await bot.send_photo(chat_id=telegram_id, photo=photo_file_id)
                                await bot.send_message(chat_id=telegram_id, text=caption_val, parse_mode=None)
                            elif caption_val:
                                await bot.send_photo(
                                    chat_id=telegram_id, photo=photo_file_id,
                                    caption=caption_val, parse_mode=None,
                                )
                            else:
                                await bot.send_photo(chat_id=telegram_id, photo=photo_file_id)
                    else:
                        await bot.send_message(chat_id=telegram_id, text=text, parse_mode=None)
                    return True
                except Exception as fallback_err:
                    logger.warning("Broadcast fallback gagal ke %s: %s", telegram_id, fallback_err)
                    return False
            logger.warning("BadRequest broadcast ke %s: %s", telegram_id, bad_req)
            return False
        except Exception as exc:
            logger.warning("Gagal kirim broadcast ke %s: %s", telegram_id, exc)
            return False
    return False


def _parse_broadcast_segment(text: str) -> tuple[str, str]:
    """Parse segment flag dari teks broadcast. Return (segment, clean_message)."""
    segments = {"--all": "all", "--active": "active", "--buyers": "buyers", "--balance": "balance"}
    for flag, seg in segments.items():
        if text.startswith(flag + " ") or text == flag:
            return seg, text[len(flag):].strip()
    return "all", text


def _segment_label(segment: str) -> str:
    """Human-readable label untuk segment."""
    labels = {
        "all": "👥 Semua User",
        "active": "🔄 User Aktif 30 Hari",
        "buyers": "🛒 User Pernah Transaksi",
        "balance": "💰 User Bersaldo",
    }
    return labels.get(segment, "👥 Semua User")


NETWORK_ALIASES: dict[str, tuple[str, list[str]]] = {
    "MORPH": ("Morph", ["USDC", "ETH"]),
    "BASE": ("Base", ["USDC", "ETH"]),
    "BSC": ("BSC", ["USDT", "USDC", "BNB"]),
    "BEP20": ("BSC", ["USDT", "USDC", "BNB"]),
    "ARB": ("Arbitrum", ["USDT", "USDC", "ETH", "ARB"]),
    "ARBITRUM": ("Arbitrum", ["USDT", "USDC", "ETH", "ARB"]),
    "POLYGON": ("Polygon", ["USDT", "USDC", "POL"]),
    "POL": ("Polygon", ["USDT", "USDC", "POL"]),
    "MATIC": ("Polygon", ["USDT", "USDC", "POL"]),
    "SOLANA": ("Solana", ["SOL", "USDT", "USDC"]),
    "SOL": ("Solana", ["SOL", "USDT", "USDC"]),
    "TRON": ("TRON", ["TRX", "USDT"]),
    "TRX": ("TRON", ["TRX", "USDT"]),
    "TRC20": ("TRON", ["TRX", "USDT"]),
    "TON": ("TON", ["TON", "USDT"]),
    "SUI": ("Sui", ["SUI"]),
    "APTOS": ("Aptos", ["APT"]),
    "APT": ("Aptos", ["APT"]),
    "ETH": ("Ethereum", ["USDT", "USDC", "ETH"]),
    "ETHEREUM": ("Ethereum", ["USDT", "USDC", "ETH"]),
    "ERC20": ("Ethereum", ["USDT", "USDC", "ETH"]),
    "OPTIMISM": ("Optimism", ["ETH"]),
    "OP": ("Optimism", ["ETH"]),
    "ROBINHOOD": ("Robinhood", ["ETH", "USDG"]),
    "HYPEREVM": ("HyperEVM", ["HYPE"]),
    "HYPE": ("HyperEVM", ["HYPE"]),
    "AVAX": ("Avalanche", ["AVAX"]),
    "AVALANCHE": ("Avalanche", ["AVAX"]),
    "KAIA": ("Kaia", ["KAIA"]),
    "BERA": ("Berachain", ["BERA"]),
    "BERACHAIN": ("Berachain", ["BERA"]),
}


def get_available_networks_list() -> list[str]:
    """Mengembalikan daftar nama canonical jaringan yang didukung."""
    return [
        "Morph", "Base", "BSC", "Arbitrum", "Polygon", "Solana",
        "TRON", "TON", "Sui", "Aptos", "Ethereum", "Optimism",
        "Robinhood", "HyperEVM", "Avalanche", "Kaia", "Berachain",
    ]


def get_network_coins(network_query: str) -> tuple[str, list[str]]:
    """
    Mengambil nama canonical jaringan dan daftar simbol koin yang tersedia.
    Secara dinamis mencari dari BUY_NETWORKS_BY_SYMBOL dan STOCK_ASSETS.
    """
    from bot.keyboards.crypto_select import BUY_NETWORKS_BY_SYMBOL
    from config.assets import STOCK_ASSETS

    q_upper = network_query.strip().upper()
    if q_upper in NETWORK_ALIASES:
        canonical_name, base_coins = NETWORK_ALIASES[q_upper]
    else:
        canonical_name = network_query.strip().capitalize()
        base_coins = []

    coins: list[str] = list(base_coins)
    for sym, nets in BUY_NETWORKS_BY_SYMBOL.items():
        if any(n.upper() in (q_upper, canonical_name.upper()) for n in nets):
            if sym not in coins:
                coins.append(sym)

    for sym, net in STOCK_ASSETS:
        if net.upper() in (q_upper, canonical_name.upper()):
            if sym not in coins:
                coins.append(sym)

    return canonical_name, coins


def build_ready_broadcast_message(network_name: str, coins: list[str]) -> str:
    """Membangun teks siaran koin ready otomatis."""
    coin_lines = "\n".join(f"✅ {c:<6} ({network_name}🪙)" if len(c) <= 4 else f"✅ {c} ({network_name}🪙)" for c in coins)
    return (
        f"{network_name} Ready For Now🪙\n\n"
        f"{coin_lines}\n\n"
        f"Silakan /start bot untuk Beli/Jual/Swap token."
    )


async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Siaran ke user terdaftar dengan segment targeting.

    Cara pakai (admin only):
    - Teks: /broadcast [--all|--active|--buyers|--balance] Halo member...
    - Koin Ready: /broadcast --ready Morph (otomatis format koin ready)
    - Foto: kirim poster dengan caption diawali /broadcast ... (atau reply
      foto poster dengan /broadcast ...).
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    message = update.message
    if message is None:
        return

    photo_file_id = None
    is_document = False
    if message.photo:
        photo_file_id = message.photo[-1].file_id
        raw = message.caption or ""
    elif message.document and isinstance(getattr(message.document, "mime_type", None), str) and message.document.mime_type.startswith("image/"):
        photo_file_id = message.document.file_id
        is_document = True
        raw = message.caption or ""
    elif message.reply_to_message and getattr(message.reply_to_message, "photo", None):
        photo_file_id = message.reply_to_message.photo[-1].file_id
        raw = message.text or ""
    elif message.reply_to_message and getattr(message.reply_to_message, "document", None) and isinstance(getattr(message.reply_to_message.document, "mime_type", None), str) and message.reply_to_message.document.mime_type.startswith("image/"):
        photo_file_id = message.reply_to_message.document.file_id
        is_document = True
        raw = message.text or ""
    else:
        raw = message.text or ""

    clean_raw = re.sub(r"^/broadcast(?:@\w+)?\s*", "", raw, flags=re.IGNORECASE).strip()
    if not clean_raw and not photo_file_id:
        await message.reply_text(
            "⚠️ Format salah.\n"
            "Teks: <code>/broadcast Halo member...</code>\n"
            "Koin Ready: <code>/broadcast --ready Morph</code>\n"
            "Segment: <code>/broadcast --buyers Promo...</code>\n"
            "Foto: kirim poster dengan caption <code>/broadcast ...</code> "
            "atau reply foto poster dengan <code>/broadcast ...</code>.",
            parse_mode="HTML",
        )
        return

    segment, broadcast_msg = _parse_broadcast_segment(clean_raw)

    # Jika mereply foto tanpa teks tambahan di reply-nya, pakai caption asli foto tersebut bila ada
    if not broadcast_msg and message.reply_to_message and message.reply_to_message.caption:
        broadcast_msg = message.reply_to_message.caption.strip()

    if not broadcast_msg and not photo_file_id:
        await message.reply_text(
            "⚠️ Pesan broadcast kosong setelah flag segment.",
            parse_mode="HTML",
        )
        return

    # Deteksi format otomatis koin ready: --ready [JARINGAN] atau ready [JARINGAN]
    ready_pattern = re.compile(
        r"(?:^|\s)--ready(?:\s+([a-zA-Z0-9_\-]+))?|^ready\s+([a-zA-Z0-9_\-]+)$",
        re.IGNORECASE,
    )
    ready_match = ready_pattern.search(broadcast_msg) if broadcast_msg else None
    if ready_match:
        target_net = ready_match.group(1) or ready_match.group(2)
        if not target_net:
            nets_str = ", ".join(get_available_networks_list())
            await message.reply_text(
                "ℹ️ <b>Format Siaran Koin Ready Otomatis:</b>\n"
                "<code>/broadcast --ready [NAMA_JARINGAN]</code>\n\n"
                "Contoh:\n"
                "• <code>/broadcast --ready Morph</code>\n"
                "• <code>/broadcast --ready Base</code>\n"
                "• <code>/broadcast --ready BSC</code>\n"
                "• <code>/broadcast --buyers --ready Morph</code> <i>(khusus member pembeli)</i>\n\n"
                f"📌 <b>Jaringan Tersedia:</b>\n<code>{nets_str}</code>\n\n"
                "💡 <i>Anda juga bisa mengirim foto poster dengan caption <code>/broadcast --ready Morph</code>.</i>",
                parse_mode="HTML",
            )
            return

        canon_name, coins = get_network_coins(target_net)
        if not coins:
            nets_str = ", ".join(get_available_networks_list())
            await message.reply_text(
                f"⚠️ Koin untuk jaringan <b>{target_net}</b> belum terdaftar.\n\n"
                f"📌 Jaringan yang tersedia:\n<code>{nets_str}</code>",
                parse_mode="HTML",
            )
            return

        broadcast_msg = build_ready_broadcast_message(canon_name, coins)

    full_text = broadcast_msg or ""

    db = SessionLocal()
    try:
        users = crud.get_users_by_segment(db, segment)
        if not users:
            await message.reply_text(f"ℹ️ Tidak ada user pada segment {_segment_label(segment)}.")
            return

        if photo_file_id and broadcast_msg:
            mode = "Dokumen Gambar + Teks" if is_document else "Foto + Teks"
        elif photo_file_id:
            mode = "Dokumen Gambar" if is_document else "Foto"
        else:
            mode = "Teks"

        import time as _time
        start_ts = _time.monotonic()

        await message.reply_text(
            f"⏳ Mengirim siaran ({mode}) ke {len(users)} pengguna...\n"
            f"📌 Segment: {_segment_label(segment)}"
        )

        success_count = 0
        fail_count = 0

        for index, u in enumerate(users, start=1):
            if await _send_broadcast_to_user(
                context.bot, u.telegram_id, full_text, photo_file_id, is_document=is_document
            ):
                success_count += 1
            else:
                fail_count += 1
            if index % BROADCAST_PACE_EVERY == 0:
                await asyncio.sleep(BROADCAST_PACE_SECONDS)

        elapsed = _time.monotonic() - start_ts

        await message.reply_text(
            f"📊 <b>LAPORAN BROADCAST SELESAI</b>\n\n"
            f"📌 Segment: {_segment_label(segment)}\n"
            f"📤 Total target   : <code>{len(users)} user</code>\n"
            f"✅ Terkirim        : <code>{success_count} user</code>\n"
            f"❌ Gagal (blocked) : <code>{fail_count} user</code>\n"
            f"⏱  Durasi         : <code>{elapsed:.1f} detik</code>\n"
            f"🔤 Mode           : <code>{mode}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error broadcast: {e}", exc_info=True)
        await message.reply_text("❌ Terjadi kesalahan saat mengirim broadcast.")
    finally:
        db.close()


async def ban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Memblokir user agar tidak bisa bertransaksi."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Contoh: `/ban 123456789`")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID harus berupa angka.")
        return

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == target_id).first()
        if not user:
            await update.message.reply_text("❌ Pengguna tidak ditemukan di database.")
            return

        user.is_banned = True
        db.commit()
        await update.message.reply_text(f"✅ Pengguna dengan ID <code>{target_id}</code> berhasil DI-BAN.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error ban_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memblokir pengguna.")
    finally:
        db.close()


async def unban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Membuka blokir user."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Contoh: `/unban 123456789`")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID harus berupa angka.")
        return

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == target_id).first()
        if not user:
            await update.message.reply_text("❌ Pengguna tidak ditemukan di database.")
            return

        user.is_banned = False
        db.commit()
        await update.message.reply_text(f"✅ Blokir pengguna dengan ID <code>{target_id}</code> berhasil DIBUKA.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error unban_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal membuka blokir pengguna.")
    finally:
        db.close()


async def refreshwallet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Memicu sinkronisasi saldo blockchain secara manual."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    await update.message.reply_text("⏳ <i>Melakukan sinkronisasi saldo on-chain wallet...</i>", parse_mode="HTML")
    
    from services.wallet_sync import sync_wallet_balances
    try:
        report = await sync_wallet_balances()
        success_list, fail_list = report["success"], report["failed"]
        status_msg = (
            "✅ <b>SINKRONISASI WALLET SELESAI</b>\n\n"
            f"• Sukses: <code>{', '.join(success_list)}</code>\n"
            f"• Gagal: <code>{', '.join(fail_list) if fail_list else 'Tidak ada'}</code>"
        )
        await update.message.reply_text(status_msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error refreshwallet: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal menyinkronisasikan wallet.")


async def admin_approve_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Approve order Buy & trigger auto-send crypto."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_approve_buy_", "")
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await query.answer("❌ Order tidak ditemukan.", show_alert=True)
            return

        if order.payout_tx_hash or order.status == "completed":
            await query.answer("ℹ️ Order ini sudah COMPLETED.", show_alert=True)
            return

        if order.status not in ("pending", "paid", "manual_review", "expired"):
            await query.answer(
                f"ℹ️ Order berstatus {order.status.upper()} — tidak bisa di-approve.", show_alert=True
            )
            return

        await query.answer("⏳ Memproses pengiriman crypto otomatis...", show_alert=True)
        import asyncio
        from bot.handlers.buy import _run_finalize_background
        asyncio.create_task(
            _run_finalize_background(order.order_id, context.bot, allow_admin=True)
        )

        caption_now = query.message.caption or ""
        await query.edit_message_caption(
            caption=f"{caption_now}\n\n✅ <b>APPROVED & DIESEKUSI OTOMATIS OLEH ADMIN</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error admin_approve_buy_callback {order_id}: {e}", exc_info=True)
        await query.answer("❌ Gagal memproses approval.", show_alert=True)
    finally:
        db.close()


async def admin_reject_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Reject order Buy."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_reject_buy_", "")
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if order:
            crud.update_order_status(db, order_id, new_status="rejected", failure_reason="Ditolak oleh admin")
            crud.release_order_inventory(db, order_id)
            from bot.utils.telegram_utils import safe_send_message
            await safe_send_message(
                context.bot, order.telegram_id,
                f"❌ <b>Order {order_id} Ditolak</b>\n"
                f"Bukti pembayaran Anda tidak dapat diverifikasi oleh admin. Silakan hubungi admin jika ada kendala."
            )
        await query.answer("Order berhasil ditolak.")
        caption_now = query.message.caption or ""
        await query.edit_message_caption(
            caption=f"{caption_now}\n\n❌ <b>DITOLAK OLEH ADMIN</b>",
            parse_mode="HTML"
        )
    finally:
        db.close()


async def admin_approve_topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Approve topup saldo IDR."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    topup_id = query.data.replace("admin_approve_topup_", "")
    db = SessionLocal()
    try:
        topup = crud.get_topup_order_by_id(db, topup_id)
        if not topup:
            await query.answer("❌ Topup tidak ditemukan.", show_alert=True)
            return

        if topup.status == "SUCCESS":
            await query.answer("ℹ️ Topup ini sudah LUNAS.", show_alert=True)
            return

        if not crud.claim_topup_success(db, topup_id):
            await query.answer("ℹ️ Topup ini sudah diproses sistem.", show_alert=True)
            return

        new_bal = crud.credit_user_balance(db, topup.telegram_id, topup.amount_idr)

        from bot.utils.telegram_utils import safe_send_message
        menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]])
        await safe_send_message(
            context.bot, topup.telegram_id,
            f"✅ <b>PEMBAYARAN TOPUP TERVERIFIKASI ADMIN!</b>\n\n"
            f"🎉 Topup saldo sebesar <b>{format_idr(topup.amount_idr)}</b> telah berhasil di-approve!\n"
            f"💳 <b>Total Saldo Bot Anda Saat Ini</b>: <b>{format_idr(int(new_bal))}</b>",
            reply_markup=menu_keyboard
        )

        await query.answer("Topup berhasil di-approve!")
        caption_now = query.message.caption or ""
        await query.edit_message_caption(
            caption=f"{caption_now}\n\n✅ <b>APPROVED & SALDO DITAMBAHKAN OLEH ADMIN</b>",
            parse_mode="HTML"
        )
    finally:
        db.close()


async def admin_reject_topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Reject topup saldo IDR."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    topup_id = query.data.replace("admin_reject_topup_", "")
    db = SessionLocal()
    try:
        crud.update_topup_status(db, topup_id, "CANCELLED")
        topup = crud.get_topup_order_by_id(db, topup_id)
        if topup:
            from bot.utils.telegram_utils import safe_send_message
            await safe_send_message(
                context.bot, topup.telegram_id,
                f"❌ <b>Top-up {topup_id} Ditolak</b>\n"
                f"Bukti pembayaran Anda tidak dapat diverifikasi oleh admin."
            )
        await query.answer("Topup berhasil ditolak.")
        caption_now = query.message.caption or ""
        await query.edit_message_caption(
            caption=f"{caption_now}\n\n❌ <b>DITOLAK OLEH ADMIN</b>",
            parse_mode="HTML"
        )
    finally:
        db.close()


async def admin_approve_swap_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Approve & eksekusi pengiriman koin tujuan Swap."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_approve_swap_", "")
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await query.answer("❌ Order tidak ditemukan.", show_alert=True)
            return

        if order.status == "COMPLETED" or order.payout_tx_hash:
            await query.answer("Order selesai atau sudah memiliki referensi broadcast. Periksa status/receipt; jangan kirim ulang.", show_alert=True)
            return

        if order.order_type != "swap" or order.status not in ("WAITING_CRYPTO_DEPOSIT", "CRYPTO_CONFIRMED"):
            await query.answer("Order tidak dapat diproses otomatis dalam status ini.", show_alert=True)
            return
        await query.answer("Memeriksa ulang deposit on-chain...")
        from services.detector import deposit_detector
        if order.status == "WAITING_CRYPTO_DEPOSIT":
            await deposit_detector._process_order(db, order, context.application)
        else:
            await deposit_detector._execute_payout(db, order, context.application)
        db.refresh(order)
        await query.message.reply_text(f"Status order {order.order_id}: {order.status}. Foto saja tidak mengesahkan deposit.")
    except Exception as e:
        logger.error(f"Error admin_approve_swap_callback {order_id}: {e}", exc_info=True)
        await query.answer(f"❌ Error: {e}", show_alert=True)
    finally:
        db.close()


async def admin_reject_swap_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Tolak pesanan swap."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_reject_swap_", "")
    db = SessionLocal()
    try:
        crud.update_order_status(db, order_id, "CANCELLED")
        order = crud.get_order_by_id(db, order_id)
        if order:
            from bot.utils.telegram_utils import safe_send_message
            await safe_send_message(
                context.bot, order.telegram_id,
                f"❌ <b>Pesanan Swap {order_id} Dibatalkan</b>\n\n"
                f"Bukti transfer deposit tidak dapat diverifikasi oleh admin. "
                f"Silakan hubungi admin jika terdapat kekeliruan."
            )
        await query.answer("Swap berhasil ditolak/dibatalkan.")
        caption_now = query.message.caption or ""
        text_now = query.message.text or ""
        if caption_now:
            await query.edit_message_caption(
                caption=f"{caption_now}\n\n❌ <b>DITOLAK OLEH ADMIN</b>",
                parse_mode="HTML"
            )
        elif text_now:
            await query.edit_message_text(
                text=f"{text_now}\n\n❌ <b>DITOLAK OLEH ADMIN</b>",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error admin_reject_swap_callback {order_id}: {e}", exc_info=True)
    finally:
        db.close()


async def check_api_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /checkapi atau /cekurl untuk memeriksa status seluruh URL/API koin."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Akses ditolak. Khusus admin.")
        return

    msg = await update.message.reply_text("Sedang melakukan diagnosa & ping ke seluruh API & RPC koin...", parse_mode="HTML")
    from services.coin_api_monitor import coin_api_monitor
    results = await coin_api_monitor.check_all()
    text = coin_api_monitor.format_admin_dashboard(results)
    buttons = [
        [InlineKeyboardButton("Refresh Scan", callback_data="admin_panel_check_apis")],
        [InlineKeyboardButton("Buka Admin Dashboard", callback_data="admin_panel_main")],
    ]
    await msg.edit_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")



async def settarget_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /settarget <jenis> - pasang topik grup sebagai tujuan notifikasi (ketik di dalam topik)."""
    from bot.utils.telegram_utils import normalisasi_kind
    from database.models import NotificationTarget
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Pakai: /settarget beli|jual|convert|error|alarm|topup|ops\n"
            "Jalankan perintah ini di dalam topik tujuan (mis. topik 'Beli Crypto').")
        return
    kind = normalisasi_kind(context.args[0])
    chat_id = str(update.effective_chat.id)
    thread_id = update.message.message_thread_id
    judul = update.effective_chat.title or "chat"
    if thread_id:
        judul = f"{judul} / thread {thread_id}"
    db = SessionLocal()
    try:
        row = db.query(NotificationTarget).filter(NotificationTarget.kind == kind).first()
        if row is None:
            row = NotificationTarget(kind=kind, chat_id=chat_id,
                                     thread_id=str(thread_id) if thread_id else None, title=judul)
            db.add(row)
        else:
            row.chat_id = chat_id
            row.thread_id = str(thread_id) if thread_id else None
            row.title = judul
        db.commit()
    finally:
        db.close()
    await update.message.reply_text(
        f"✅ Target notif <b>{kind}</b> dipasang di: {judul}\n"
        f"chat <code>{chat_id}</code> | thread <code>{thread_id or '-'}</code>",
        parse_mode="HTML")


async def unsettarget_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /unsettarget <jenis> - lepas tujuan notifikasi (kembali ke DM admin)."""
    from bot.utils.telegram_utils import normalisasi_kind
    from database.models import NotificationTarget
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Pakai: /unsettarget beli|jual|convert|error|alarm|topup|ops")
        return
    kind = normalisasi_kind(context.args[0])
    db = SessionLocal()
    try:
        n = db.query(NotificationTarget).filter(NotificationTarget.kind == kind).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    await update.message.reply_text(
        f"{'✅' if n else 'ℹ️'} Target <b>{kind}</b> {'dilepas' if n else 'memang belum dipasang'}; notif kembali ke DM admin.",
        parse_mode="HTML")


async def targets_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /targets - lihat status semua tujuan notifikasi."""
    from bot.utils.telegram_utils import KIND_RESMI
    from database.models import NotificationTarget
    if not is_admin(update.effective_user.id):
        return
    db = SessionLocal()
    try:
        rows = {r.kind: r for r in db.query(NotificationTarget).all()}
    finally:
        db.close()
    lines = ["📋 <b>TARGET NOTIFIKASI</b>\n"]
    for kind in KIND_RESMI:
        r = rows.get(kind)
        if r:
            tujuan = f"{r.title} (chat <code>{r.chat_id}</code>, thread <code>{r.thread_id or '-'}</code>)"
        else:
            tujuan = "belum dipasang (fallback DM admin)"
        lines.append(f"• <b>{kind}</b>: {tujuan}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def chatid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /chatid - tampilkan id chat & thread (untuk /settarget)."""
    if not is_admin(update.effective_user.id):
        return
    chat = update.effective_chat
    thread = update.message.message_thread_id
    await update.message.reply_text(
        f"Chat ID: <code>{chat.id}</code>\n"
        f"Thread/Topic ID: <code>{thread if thread else '-'}</code>\n"
        f"Judul: {chat.title or 'chat pribadi'}\n\n"
        f"Jalankan /settarget beli (dll) di topik yang dituju.",
        parse_mode="HTML")


# ============================================================
# ADMIN CREDIT BALANCE — Isi saldo IDR ke user
# ============================================================

async def credit_balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Isi saldo IDR ke user tertentu.

    Format: /credit <telegram_id> <jumlah_idr> [keterangan]
    Contoh: /credit 123456789 10000 Giveaway Winner Oktober
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Format: <code>/credit [telegram_id] [jumlah_idr] [keterangan]</code>\n"
            "Contoh: <code>/credit 123456789 10000 Giveaway Winner</code>",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Telegram ID harus berupa angka.")
        return

    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Jumlah IDR harus berupa angka.")
        return

    if amount < 1000:
        await update.message.reply_text("❌ Minimum kredit adalah Rp 1.000.")
        return
    if amount > 10_000_000:
        await update.message.reply_text("❌ Maksimum kredit adalah Rp 10.000.000 per operasi.")
        return

    keterangan = " ".join(context.args[2:]) if len(context.args) > 2 else "Admin credit"

    db = SessionLocal()
    try:
        target_user = db.query(User).filter(User.telegram_id == target_id).first()
        if not target_user:
            await update.message.reply_text(
                f"❌ User dengan ID <code>{target_id}</code> tidak ditemukan di database.",
                parse_mode="HTML",
            )
            return

        old_bal = float(target_user.balance_idr or 0)
        new_bal = crud.credit_user_balance(db, target_id, float(amount))

        # Audit log
        db.add(AuditLog(
            telegram_id=target_id,
            action="ADMIN_CREDIT_BALANCE",
            details=f"Admin {user_id} credit Rp {amount:,} ke {target_id}. "
                    f"Saldo: Rp {old_bal:,.0f} → Rp {new_bal:,.0f}. Keterangan: {keterangan}",
        ))
        db.commit()

        target_name = f"@{target_user.username}" if target_user.username else str(target_id)

        # Notifikasi ke user penerima
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"🎉 <b>Saldo Anda Bertambah!</b>\n\n"
                    f"💰 Jumlah : <b>{format_idr(amount)}</b>\n"
                    f"📝 Keterangan: {_esc(keterangan)}\n"
                    f"💳 Saldo Sekarang: <b>{format_idr(int(new_bal))}</b>\n\n"
                    f"Gunakan saldo ini untuk membeli crypto di bot! 🚀"
                ),
                parse_mode="HTML",
            )
        except Exception as notif_err:
            logger.warning(f"Gagal kirim notifikasi credit ke {target_id}: {notif_err}")

        await update.message.reply_text(
            f"✅ <b>Berhasil Isi Saldo</b>\n\n"
            f"👤 User: {target_name} (<code>{target_id}</code>)\n"
            f"💰 Jumlah: <b>{format_idr(amount)}</b>\n"
            f"💳 Saldo Baru: <b>{format_idr(int(new_bal))}</b>\n"
            f"📝 Keterangan: {_esc(keterangan)}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error credit_balance_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal mengisi saldo user.")
    finally:
        db.close()


async def bulkcredit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Isi saldo IDR ke banyak user sekaligus.

    Format: /bulkcredit <jumlah_idr> <id1> <id2> <id3> ...
    Contoh: /bulkcredit 10000 123456789 987654321 555666777
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Format: <code>/bulkcredit [jumlah_idr] [id1] [id2] ...</code>\n"
            "Contoh: <code>/bulkcredit 10000 123456789 987654321</code>",
            parse_mode="HTML",
        )
        return

    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Jumlah IDR harus berupa angka (argumen pertama).")
        return

    if amount < 1000:
        await update.message.reply_text("❌ Minimum kredit adalah Rp 1.000.")
        return
    if amount > 10_000_000:
        await update.message.reply_text("❌ Maksimum kredit adalah Rp 10.000.000 per operasi.")
        return

    target_ids = []
    for arg in context.args[1:]:
        try:
            target_ids.append(int(arg))
        except ValueError:
            await update.message.reply_text(f"❌ ID <code>{arg}</code> bukan angka valid.", parse_mode="HTML")
            return

    if not target_ids:
        await update.message.reply_text("❌ Tidak ada user ID yang diberikan.")
        return

    await update.message.reply_text(
        f"⏳ Memproses bulk credit {format_idr(amount)} ke {len(target_ids)} user..."
    )

    db = SessionLocal()
    try:
        success_count = 0
        fail_count = 0
        results = []

        for tid in target_ids:
            target_user = db.query(User).filter(User.telegram_id == tid).first()
            if not target_user:
                fail_count += 1
                results.append(f"❌ {tid} — tidak ditemukan")
                continue

            try:
                new_bal = crud.credit_user_balance(db, tid, float(amount))
                db.add(AuditLog(
                    telegram_id=tid,
                    action="ADMIN_CREDIT_BALANCE",
                    details=f"Bulk credit oleh admin {user_id}. Rp {amount:,}. Saldo baru: Rp {new_bal:,.0f}",
                ))
                db.commit()

                name = f"@{target_user.username}" if target_user.username else str(tid)
                success_count += 1
                results.append(f"✅ {name} — saldo baru: {format_idr(int(new_bal))}")

                # Notifikasi ke user
                try:
                    await context.bot.send_message(
                        chat_id=tid,
                        text=(
                            f"🎉 <b>Saldo Anda Bertambah!</b>\n\n"
                            f"💰 Jumlah: <b>{format_idr(amount)}</b>\n"
                            f"💳 Saldo Sekarang: <b>{format_idr(int(new_bal))}</b>\n\n"
                            f"Gunakan saldo ini untuk membeli crypto di bot! 🚀"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            except Exception as e:
                fail_count += 1
                results.append(f"❌ {tid} — error: {e}")

        total_credited = amount * success_count
        report = "\n".join(results[:20])  # Max 20 lines
        if len(results) > 20:
            report += f"\n... dan {len(results) - 20} lainnya"

        await update.message.reply_text(
            f"📊 <b>LAPORAN BULK CREDIT</b>\n\n"
            f"💰 Nominal per user: <b>{format_idr(amount)}</b>\n"
            f"✅ Sukses: <b>{success_count}/{len(target_ids)}</b>\n"
            f"❌ Gagal: <b>{fail_count}/{len(target_ids)}</b>\n"
            f"💵 Total dikreditkan: <b>{format_idr(total_credited)}</b>\n\n"
            f"<b>Detail:</b>\n{report}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error bulkcredit_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memproses bulk credit.")
    finally:
        db.close()


# ============================================================
# ADMIN REFERRAL CONFIG — /setreferral
# ============================================================

async def setreferral_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Konfigurasi referral program.

    Format:
      /setreferral reward <jumlah>     — Set reward per referral (Rp)
      /setreferral bonus <jumlah>      — Set potongan/bonus transaksi pertama teman (Rp)
      /setreferral enabled true|false  — Aktifkan/nonaktifkan program
      /setreferral max <jumlah>        — Batas maksimal referral per user
    """
    if not is_admin(update.effective_user.id):
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ <b>Format Pengaturan Referral:</b>\n\n"
            "• <code>/setreferral reward [JUMLAH]</code> — Set reward pengundang\n"
            "• <code>/setreferral bonus [JUMLAH]</code> — Set potongan/bonus teman\n"
            "• <code>/setreferral min [JUMLAH]</code> — Set min. pembelian teman (0 = tanpa min)\n"
            "• <code>/setreferral enabled true|false</code> — Aktifkan/nonaktifkan program\n"
            "• <code>/setreferral max [JUMLAH]</code> — Batas maksimal teman/user\n\n"
            "<i>Atau gunakan menu interaktif di Dashboard Admin ➔ Kelola Referral.</i>",
            parse_mode="HTML",
        )
        return

    key = context.args[0].lower()
    value = context.args[1]

    valid_keys = {
        "reward": "reward_per_referral",
        "enabled": "referral_enabled",
        "bonus": "referee_discount_idr",
        "discount": "referee_discount_idr",
        "potongan": "referee_discount_idr",
        "max": "max_referrals_per_user",
        "min": "min_trade_amount_idr",
        "minimal": "min_trade_amount_idr",
        "min_trade": "min_trade_amount_idr",
        "minorder": "min_trade_amount_idr",
    }
    if key not in valid_keys:
        await update.message.reply_text(
            f"❌ Key tidak valid: <code>{key}</code>.\n"
            "Gunakan salah satu: <code>reward</code>, <code>bonus</code>, <code>min</code>, <code>enabled</code>, <code>max</code>",
            parse_mode="HTML"
        )
        return

    config_key = valid_keys[key]

    if key in ("reward", "bonus", "discount", "potongan"):
        try:
            val = int(value)
            if val < 0 or val > 1_000_000:
                await update.message.reply_text("❌ Nilai harus antara 0 - 1.000.000")
                return
            value = str(val)
        except ValueError:
            await update.message.reply_text("❌ Nilai harus berupa angka.")
            return
    elif key in ("min", "minimal", "min_trade", "minorder"):
        try:
            val = int(value)
            if val < 0 or val > 50_000_000:
                await update.message.reply_text("❌ Nilai minimal harus antara 0 - 50.000.000 (0 = tanpa min)")
                return
            value = str(val)
        except ValueError:
            await update.message.reply_text("❌ Nilai minimal harus berupa angka.")
            return
    elif key == "max":
        try:
            val = int(value)
            if val < 1 or val > 100_000:
                await update.message.reply_text("❌ Batas maksimal harus antara 1 - 100.000")
                return
            value = str(val)
        except ValueError:
            await update.message.reply_text("❌ Nilai max harus berupa angka.")
            return
    elif key == "enabled":
        if value.lower() not in ("true", "false"):
            await update.message.reply_text("❌ Nilai harus 'true' atau 'false'.")
            return
        value = value.lower()

    db = SessionLocal()
    try:
        crud.set_referral_config(db, config_key, value)
        await update.message.reply_text(
            f"✅ <b>Referral Config Diperbarui!</b>\n\n"
            f"⚙️ <code>{config_key}</code> = <b>{value}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error setreferral: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal update konfigurasi referral.")
    finally:
        db.close()


async def admin_interactive_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Router terpusat untuk seluruh input teks interaktif admin:
    - Pencarian pengguna & nominal kirim saldo admin
    - Top up & atur ulang saldo kas bot (Bot Treasury)
    - Konfigurasi nominal kustom referral (reward, bonus, min trade)
    """
    if not update.message or not update.message.text:
        return False

    user_id = update.effective_user.id
    if not is_admin(user_id):
        return False

    raw_text = update.message.text.strip()
    db = SessionLocal()

    try:
        # 1. Admin Send Balance — Input Target User (@username atau ID)
        if context.user_data.get("admin_awaiting_send_bal_user"):
            target_user = crud.get_user_by_identifier(db, raw_text)
            if not target_user:
                await update.message.reply_text(
                    f"❌ <b>Pengguna Tidak Ditemukan!</b>\n\n"
                    f"Tidak ditemukan pengguna dengan username atau ID: <code>{_esc(raw_text)}</code>.\n"
                    f"Pastikan pengguna sudah pernah memulai bot (<code>/start</code>).\n\n"
                    f"<i>Silakan ketik ulang @username atau ID Telegram yang benar:</i>",
                    parse_mode="HTML",
                )
                return True

            context.user_data.pop("admin_awaiting_send_bal_user", None)
            context.user_data["admin_send_bal_target_id"] = target_user.telegram_id
            text, markup = build_admin_send_balance_amount_view(target_user)
            await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            return True

        # 2. Admin Send Balance — Input Nominal Kustom
        if context.user_data.get("admin_awaiting_send_bal_custom_amt"):
            clean_digits = "".join(ch for ch in raw_text if ch.isdigit())
            if not clean_digits:
                await update.message.reply_text("❌ Mohon ketik angka nominal yang valid (contoh: <code>50000</code>):", parse_mode="HTML")
                return True

            amount = int(clean_digits)
            if amount < 1000 or amount > 10_000_000:
                await update.message.reply_text(
                    "❌ Nominal harus antara <b>Rp 1.000</b> sampai <b>Rp 10.000.000</b>.\nSilakan ketik angka kembali:",
                    parse_mode="HTML"
                )
                return True

            target_id = context.user_data.get("admin_send_bal_target_id")
            if not target_id:
                await update.message.reply_text("⚠️ Sesi transfer kedaluwarsa. Silakan mulai kembali dari menu Kirim Saldo.")
                context.user_data.pop("admin_awaiting_send_bal_custom_amt", None)
                return True

            target_user = db.query(User).filter(User.telegram_id == target_id).first()
            if not target_user:
                await update.message.reply_text("❌ Pengguna tidak ditemukan di database.")
                context.user_data.pop("admin_awaiting_send_bal_custom_amt", None)
                return True

            context.user_data.pop("admin_awaiting_send_bal_custom_amt", None)
            text, markup = build_admin_send_balance_confirm_view(target_user, amount)
            await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            return True

        # 3. Bot Treasury — Topup Saldo Kas Bot Kustom
        if context.user_data.get("admin_awaiting_treasury_custom"):
            clean_digits = "".join(ch for ch in raw_text if ch.isdigit())
            if not clean_digits or int(clean_digits) < 1000:
                await update.message.reply_text("❌ Nominal minimal top up adalah Rp 1.000. Silakan ketik angka kembali:")
                return True

            amount = int(clean_digits)
            new_bal = crud.topup_bot_treasury(db, amount, admin_id=user_id, note="Admin Custom Topup")
            context.user_data.pop("admin_awaiting_treasury_custom", None)

            await update.message.reply_text(
                f"✅ <b>TOP UP KAS BOT BERHASIL!</b>\n\n"
                f"💰 <b>Nominal Ditambahkan:</b> <code>+{format_idr(amount)}</code>\n"
                f"🏦 <b>Total Saldo Kas Bot Sekarang:</b> <b>{format_idr(new_bal)}</b>\n\n"
                f"<i>Saldo siap digunakan untuk alokasi campaign, giveaway, dan reward loyalitas.</i>",
                parse_mode="HTML"
            )
            text, markup = build_admin_treasury_view(db)
            await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            return True

        # 4. Bot Treasury — Atur Ulang Saldo Manual
        if context.user_data.get("admin_awaiting_treasury_set_manual"):
            clean_digits = "".join(ch for ch in raw_text if ch.isdigit())
            if not clean_digits:
                await update.message.reply_text("❌ Mohon masukkan angka nominal yang valid (contoh: <code>5000000</code> atau <code>0</code>):", parse_mode="HTML")
                return True

            amount = int(clean_digits)
            new_bal = crud.set_bot_treasury_balance(db, amount, admin_id=user_id, note="Admin Set Manual")
            context.user_data.pop("admin_awaiting_treasury_set_manual", None)

            await update.message.reply_text(
                f"✅ <b>SALDO KAS BOT DIPERBARUI!</b>\n\n"
                f"🏦 <b>Saldo Kas Baru:</b> <b>{format_idr(new_bal)}</b>",
                parse_mode="HTML"
            )
            text, markup = build_admin_treasury_view(db)
            await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            return True

        # 5. Referral Configurations (Reward, Bonus, Min Trade)
        is_reward = context.user_data.get("admin_awaiting_ref_custom_reward")
        is_bonus = context.user_data.get("admin_awaiting_ref_custom_bonus")
        is_min_trade = context.user_data.get("admin_awaiting_ref_custom_min_trade")

        if is_reward or is_bonus or is_min_trade:
            clean_val = raw_text.replace(".", "").replace(",", "").replace("Rp", "").replace("rp", "").strip()
            try:
                val = int(clean_val)
                max_limit = 50_000_000 if is_min_trade else 1_000_000
                if val < 0 or val > max_limit:
                    await update.message.reply_text(f"❌ Nominal harus antara Rp 0 sampai Rp {max_limit:,}. Silakan ketik angka kembali:")
                    return True
            except ValueError:
                await update.message.reply_text("❌ Mohon masukkan angka nominal yang valid (contoh: 50000):")
                return True

            if is_reward:
                crud.set_referral_config(db, "reward_per_referral", str(val))
                context.user_data["admin_awaiting_ref_custom_reward"] = False
                await update.message.reply_text(
                    f"✅ <b>Reward Pengundang Berhasil Diperbarui!</b>\n\n"
                    f"💰 Nominal reward baru: <b>Rp {val:,}</b> per teman yang selesai transaksi.",
                    parse_mode="HTML"
                )
            elif is_bonus:
                crud.set_referral_config(db, "referee_discount_idr", str(val))
                context.user_data["admin_awaiting_ref_custom_bonus"] = False
                await update.message.reply_text(
                    f"✅ <b>Potongan / Bonus Teman Berhasil Diperbarui!</b>\n\n"
                    f"🎁 Nominal potongan baru: <b>Rp {val:,}</b> untuk transaksi pertama teman.",
                    parse_mode="HTML"
                )
            elif is_min_trade:
                crud.set_referral_config(db, "min_trade_amount_idr", str(val))
                context.user_data["admin_awaiting_ref_custom_min_trade"] = False
                desc_val = f"Rp {val:,}" if val > 0 else "Tanpa Minimal (Bebas)"
                await update.message.reply_text(
                    f"✅ <b>Syarat Minimal Pembelian Diperbarui!</b>\n\n"
                    f"🛒 Minimal pembelian teman baru: <b>{desc_val}</b>.",
                    parse_mode="HTML"
                )

            view_text = build_admin_referral_view(db)
            markup = build_admin_referral_keyboard(db)
            await update.message.reply_text(
                text=view_text,
                reply_markup=markup,
                parse_mode="HTML"
            )
            return True

        return False
    except Exception as e:
        logger.error(f"Error in admin_interactive_text_router: {e}", exc_info=True)
        await update.message.reply_text("❌ Terjadi kesalahan saat memproses input admin.")
        return True
    finally:
        db.close()


# Alias untuk backwards compatibility
admin_referral_text_handler = admin_interactive_text_router


async def topup_bot_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /topupbot <nominal> atau /saldobot untuk melihat dan topup saldo kas bot."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    db = SessionLocal()
    try:
        if not context.args:
            text, markup = build_admin_treasury_view(db)
            await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            return

        clean_digits = "".join(ch for ch in context.args[0] if ch.isdigit())
        if not clean_digits or int(clean_digits) < 1000:
            await update.message.reply_text("⚠️ Nominal topup kas bot minimal Rp 1.000.")
            return

        amount = int(clean_digits)
        note = " ".join(context.args[1:]) if len(context.args) > 1 else "Command Topup Bot"
        new_bal = crud.topup_bot_treasury(db, amount, admin_id=user_id, note=note)

        await update.message.reply_text(
            f"✅ <b>TOP UP KAS BOT BERHASIL!</b>\n\n"
            f"💰 <b>Nominal Ditambahkan:</b> <code>+{format_idr(amount)}</code>\n"
            f"🏦 <b>Total Saldo Kas Bot:</b> <b>{format_idr(new_bal)}</b>\n"
            f"📝 <b>Keterangan:</b> {_esc(note)}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error in topup_bot_command_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memproses topup kas bot.")
    finally:
        db.close()


