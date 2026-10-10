"""
bot/handlers/admin.py — Handler Panel Administrator.
===================================================
Berisi perintah dan kontrol administratif khusus untuk owner/admin bot.
Termasuk broadcast, statistik, set spread, un/ban, list pending order, dan konfirmasi order.
"""

import os
import html
import asyncio
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardMarkup, ForceReply
from bot.utils.animated import AnimatedButton as InlineKeyboardButton  # emoji di label jadi icon animasi
from telegram.error import RetryAfter, BadRequest
from telegram.ext import ContextTypes
from sqlalchemy import func

from config.settings import settings
from database.connection import SessionLocal
from database.models import User, Order, WalletBalance, PriceConfig, AuditLog, TopupOrder, RewardBatch
from database import crud
from html import escape as _esc
from services.crypto_sender import CryptoSenderFactory
from bot.utils.formatter import format_idr, format_crypto, format_crypto_copy
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
            InlineKeyboardButton("🎁 Kirim Reward ke User Pilihan", callback_data="admin_panel_reward"),
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
            InlineKeyboardButton("🛠 Maintenance Chain/Koin", callback_data="admin_panel_maint"),
        ],
        [
            InlineKeyboardButton("📖 Panduan Admin", callback_data="admin_panel_guide"),
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
                         f"{format_crypto_copy(order.crypto_amount, order.crypto_symbol, exact=True)} ({escape(order.network)}) · {format_idr(order.total_idr)}")
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
    buy_count = db.query(Order).filter(Order.order_type == "buy", func.lower(Order.status) == "completed").count()
    sell_count = db.query(Order).filter(Order.order_type == "sell", func.lower(Order.status) == "completed").count()
    swap_count = db.query(Order).filter(Order.order_type == "swap", func.lower(Order.status) == "completed").count()
    
    # Hitung total volume all-time
    total_vol_row = db.query(func.sum(Order.total_idr)).filter(func.lower(Order.status) == "completed").scalar()
    total_vol_all = int(total_vol_row or 0)

    # Hitung total profit fee all-time
    total_fee_row = db.query(func.sum(Order.fee_idr)).filter(func.lower(Order.status) == "completed").scalar()
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
        crypto_str = format_crypto_copy(o.crypto_amount or 0, o.crypto_symbol, exact=True)

        text_lines.append(
            f"<b>{idx}. {o.order_id}</b> ({o_type})\n"
            f"   🚦 Status: <code>{o.status.upper()}</code>\n"
            f"   🪙 Koin: {crypto_str} ({o.network})\n"
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
            if o.status == "manual_review":
                action_buttons.append([InlineKeyboardButton(f"📸 SS Transfer Manual {o.order_id[-6:]}", callback_data=f"admin_manual_sent_{o.order_id}")])
        elif o.order_type == "swap" and o.status in ["paid", "pending", "manual_review", "WAITING_CRYPTO_DEPOSIT"]:
            action_buttons.append([
                InlineKeyboardButton(f"✅ Approve Swap {o.order_id[-6:]}", callback_data=f"admin_approve_swap_{o.order_id}"),
                InlineKeyboardButton(f"❌ Reject Swap {o.order_id[-6:]}", callback_data=f"admin_reject_swap_{o.order_id}"),
            ])
            if o.status == "manual_review":
                action_buttons.append([InlineKeyboardButton(f"📸 SS Transfer Manual {o.order_id[-6:]}", callback_data=f"admin_manual_sent_{o.order_id}")])

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
                f"  Order: <code>{_esc(log.order_id or '-')}</code> | {_esc(log.from_status or '-')} ➔ <b>{_esc(log.to_status or '-')}</b>\n"
                f"  Detail: <i>{_esc(log.details or '-')}</i>\n"
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
            text_lines.append(f"• ID <code>{u.telegram_id}</code> ({_esc(uname)})")
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
        "Contoh: <code>/broadcast --ready Base</code> atau <code>/broadcast --ready Solana</code>\n"
        "<i>Bot otomatis menyusun daftar koin aktif untuk jaringan tersebut.</i>\n\n"
        "🖼️ <b>Siaran Bergambar (Poster / Logo):</b>\n"
        "• Kirim poster sebagai FOTO dengan caption diawali <code>/broadcast ...</code>, ATAU\n"
        "• Reply foto poster dengan <code>/broadcast ...</code>.\n"
        "Contoh caption: <code>/broadcast --ready Base</code>\n\n"
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


REFERRAL_ADMIN_SETTINGS = {
    "reward": {
        "title": "Reward Pengundang — Transaksi ke-1 Teman", "icon": "💵", "short": "Reward Tx ke-1",
        "unit": "idr", "presets": [500, 1000, 1500, 2000, 5000], "max": 1_000_000,
        "help": "Diterima pengundang saat teman menyelesaikan transaksi pertama (beli/jual/convert).",
    },
    "reward2": {
        "title": "Reward Pengundang — Transaksi ke-2 Teman", "icon": "💵", "short": "Reward Tx ke-2",
        "unit": "idr", "presets": [0, 250, 500, 1000, 2000], "max": 1_000_000,
        "help": "Diterima pengundang saat teman menyelesaikan transaksi kedua.",
    },
    "bonus": {
        "title": "Diskon Fee Teman yang Diundang", "icon": "🎁", "short": "Diskon Teman",
        "unit": "idr", "presets": [0, 500, 1000, 1500, 2000], "max": 1_000_000,
        "help": "Diskon fee di transaksi pertama teman; masuk ke saldo bot teman saat transaksi selesai.",
    },
    "share": {
        "title": "Bagi Hasil Fee untuk Pengundang", "icon": "💸", "short": "Bagi Hasil %",
        "unit": "pct", "presets": [0, 3, 5, 7, 10], "max": 100,
        "help": "Persen dari fee setiap transaksi teman yang masuk ke saldo pengundang (0 = nonaktif).",
    },
    "sharemax": {
        "title": "Maks. Transaksi Bagi Hasil per Teman", "icon": "🔢", "short": "Maks Tx Bagi Hasil",
        "unit": "tx", "presets": [0, 5, 10, 15, 20], "max": 1000,
        "help": "Bagi hasil fee hanya untuk sekian transaksi pertama dari tiap teman.",
    },
    "hold": {
        "title": "Masa Tahan Reward Pengundang", "icon": "⏳", "short": "Masa Tahan",
        "unit": "jam", "presets": [0, 12, 24, 48, 72], "max": 720,
        "help": "Reward pengundang baru masuk saldo bot setelah masa tahan ini (0 = langsung).",
    },
    "min_trade": {
        "title": "Minimal Nominal Transaksi Teman", "icon": "🛒", "short": "Min. Transaksi",
        "unit": "idr", "presets": [0, 10000, 25000, 50000, 100000], "max": 50_000_000,
        "help": "Transaksi teman di bawah nominal ini tidak dihitung sebagai transaksi referral (0 = semua).",
    },
    "maxref": {
        "title": "Maks. Teman per Pengundang", "icon": "🎯", "short": "Maks Teman",
        "unit": "orang", "presets": [10, 50, 100, 500, 1000], "max": 100_000, "min": 1,
        "help": "Batas jumlah teman yang bisa diundang satu pengundang.",
    },
}

_REF_UNIT_FMT = {
    "idr": lambda v: "Rp " + f"{int(v):,}".replace(",", "."),
    "pct": lambda v: f"{float(v):g}%",
    "tx": lambda v: f"{int(v)} transaksi",
    "jam": lambda v: f"{int(v)} jam" if int(v) else "langsung (tanpa masa tahan)",
    "orang": lambda v: f"{int(v)} teman",
}


_REF_UNIT_LABEL = {"idr": "Rupiah", "pct": "persen", "tx": "jumlah transaksi", "jam": "jam", "orang": "jumlah teman"}


def format_ref_setting(name: str, value) -> str:
    return _REF_UNIT_FMT[REFERRAL_ADMIN_SETTINGS[name]["unit"]](value)


def parse_ref_setting_value(name: str, raw: str) -> tuple[bool, object]:
    """Validasi input admin. Return (True, nilai) atau (False, pesan_error)."""
    meta = REFERRAL_ADMIN_SETTINGS[name]
    text = (raw or "").strip().lower()
    for token in ("rp", "%", "jam", "tx", "orang", " "):
        text = text.replace(token, "")
    if not text.isascii():
        return False, "Masukkan angka yang valid."
    try:
        if meta["unit"] == "pct":
            val = float(text.replace(",", "."))
            if not math.isfinite(val):
                return False, "Masukkan angka yang valid."
        elif meta["unit"] == "idr":
            if "," in text:
                return False, "Gunakan angka bulat tanpa koma (contoh: 7500 atau 7.500)."
            val = int(text.replace(".", ""))
        else:
            if "." in text or "," in text:
                return False, "Gunakan angka bulat tanpa titik/koma."
            val = int(text)
    except ValueError:
        return False, "Masukkan angka yang valid."
    lo, hi = meta.get("min", 0), meta["max"]
    if val < lo or val > hi:
        return False, f"Nilai harus antara {_REF_UNIT_FMT[meta['unit']](lo)} dan {_REF_UNIT_FMT[meta['unit']](hi)}."
    return True, val


def build_admin_referral_view(db) -> str:
    """Membangun tampilan manajemen konfigurasi & statistik referral untuk admin."""
    try:
        from database.models import Referral, ReferralEarning
        from services.referral_rewards import get_referral_settings, KIND_FIRST, KIND_SECOND, KIND_SHARE, KIND_BONUS
        from sqlalchemy import func as sa_func, Integer

        cfg = get_referral_settings(db)
        total = db.query(Referral).count()
        completed = db.query(Referral).filter(Referral.status == "COMPLETED").count()
        pending = db.query(Referral).filter(Referral.status == "PENDING").count()

        def _sum(statuses, kinds):
            return int(
                db.query(sa_func.coalesce(sa_func.sum(ReferralEarning.amount_idr), 0))
                .filter(ReferralEarning.status.in_(statuses), ReferralEarning.kind.in_(kinds))
                .scalar() or 0
            )

        referrer_kinds = (KIND_FIRST, KIND_SECOND, KIND_SHARE)
        held = _sum(("HELD",), referrer_kinds)
        released = _sum(("RELEASED",), referrer_kinds)
        bonus_paid = _sum(("RELEASED",), (KIND_BONUS,))

        status_badge = "🟢 <b>AKTIF</b>" if cfg["enabled"] else "🔴 <b>NONAKTIF</b>"
        f = format_ref_setting

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
            f"⚙️ <b>Status Program:</b> {status_badge}\n",
            "👥 <b>Pengundang</b>",
            f"├── Transaksi ke-1 teman : <b>{f('reward', cfg['reward'])}</b>",
            f"├── Transaksi ke-2 teman : <b>{f('reward2', cfg['reward2'])}</b>",
            f"├── Bagi hasil fee       : <b>{f('share', cfg['share'])}</b> (maks. {f('sharemax', cfg['sharemax'])}/teman)",
            f"└── Masa tahan reward    : <b>{f('hold', cfg['hold'])}</b>\n",
            "🎉 <b>Teman yang Diundang</b>",
            f"└── Diskon fee transaksi pertama: <b>{f('bonus', cfg['bonus'])}</b>\n",
            f"🛒 <b>Min. Transaksi Dihitung:</b> {f('min_trade', cfg['min_trade']) if cfg['min_trade'] else 'Tanpa minimal'}",
            f"🎯 <b>Maksimal per Pengundang:</b> {f('maxref', cfg['maxref'])}\n",
            "📊 <b>Statistik Akumulatif:</b>",
            f"├── 👥 Total Ajakan      : <b>{total}</b>",
            f"├── ✅ Sudah Transaksi   : <b>{completed}</b>",
            f"├── ⏳ Belum Transaksi   : <b>{pending}</b>",
            f"├── ⏳ Reward Ditahan    : <b>Rp {held:,}</b>",
            f"├── 💸 Reward Cair       : <b>Rp {released:,}</b>",
            f"└── 🎁 Diskon Teman Cair : <b>Rp {bonus_paid:,}</b>\n",
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
            "\n💡 <b>Pengaturan:</b> pilih tombol di bawah untuk mengubah tiap ketentuan. "
            "Perubahan berlaku untuk transaksi teman berikutnya; reward yang sudah tercatat tidak berubah."
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
    from services.referral_rewards import get_referral_settings
    is_enabled = get_referral_settings(db)["enabled"]
    toggle_text = "🔴 Nonaktifkan Program" if is_enabled else "🟢 Aktifkan Program"

    def _btn(name):
        meta = REFERRAL_ADMIN_SETTINGS[name]
        return InlineKeyboardButton(f"{meta['icon']} {meta['short']}", callback_data=f"admin_ref_pick_{name}")

    buttons = [
        [InlineKeyboardButton(toggle_text, callback_data="admin_ref_toggle_enabled")],
        [_btn("reward"), _btn("reward2")],
        [_btn("bonus"), _btn("share")],
        [_btn("sharemax"), _btn("hold")],
        [_btn("min_trade"), _btn("maxref")],
        [
            InlineKeyboardButton("🔄 Refresh Data", callback_data="admin_panel_referral"),
            InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_admin_ref_setting_view(db, name: str) -> tuple[str, InlineKeyboardMarkup]:
    """Layar pilihan nilai (preset + kustom) untuk satu ketentuan referral."""
    from services.referral_rewards import get_referral_settings
    meta = REFERRAL_ADMIN_SETTINGS[name]
    current = get_referral_settings(db)[name]
    text = (
        f"{meta['icon']} <b>{meta['title'].upper()}</b>\n\n"
        f"Nilai saat ini: <b>{format_ref_setting(name, current)}</b>\n"
        f"<i>{meta['help']}</i>\n\n"
        "Pilih nilai cepat di bawah atau klik tombol kustom:"
    )
    preset_buttons = [
        InlineKeyboardButton(format_ref_setting(name, v).replace("langsung (tanpa masa tahan)", "0 (langsung)"),
                             callback_data=f"admin_ref_set_{name}_{v}")
        for v in meta["presets"]
    ]
    rows = [preset_buttons[i:i + 3] for i in range(0, len(preset_buttons), 3)]
    rows.append([InlineKeyboardButton("✏️ Nilai Kustom", callback_data=f"admin_ref_custom_{name}")])
    rows.append([InlineKeyboardButton("🔙 Kembali ke Kelola Referral", callback_data="admin_panel_referral")])
    return text, InlineKeyboardMarkup(rows)


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
    """Membangun teks leaderboard Top Spender yang transparan memuat daftar Top 10."""
    from services.campaign_service import TOP_SPENDER_REWARDS
    from bot.utils.formatter import format_idr
    from html import escape as _esc

    top_spenders = crud.get_top_spenders(db, limit=10, period_days=period_days)
    total_pool = sum(TOP_SPENDER_REWARDS.get(i, 0) for i in range(1, 11))
    period_label = f"{period_days} Hari Terakhir" if period_days > 0 else "Semua Waktu (All-Time)"

    lines = [
        f"{tg_emoji('TROPHY', '🏆')} <b>TOP SPENDER — LEADERBOARD TRANSAKSI TERBANYAK</b>",
        f"📅 <b>Periode:</b> <code>{period_label}</code> | 💰 <b>Total Hadiah:</b> <code>{format_idr(total_pool)}</code>",
        "<i>Volume = akumulasi semua transaksi selesai (beli + jual + convert).</i>\n",
    ]

    excluded_count = len(crud.get_milestone_excluded_ids(db))
    if excluded_count:
        lines.append(f"🚫 <i>{excluded_count} user dikecualikan dari milestone (tetap bisa transaksi). "
                     f"Peringkat tetap Top 10 dari user lain.</i>\n")

    if not top_spenders:
        lines.append("<i>Belum ada data transaksi selesai pada periode ini.</i>\n")
    else:
        lines.append(f"📊 <b>Daftar Peringkat Top {len(top_spenders)} Spender Terbesar:</b>\n")
        for u in top_spenders:
            rank = u["rank"]
            reward = TOP_SPENDER_REWARDS.get(rank, 0)
            medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"<b>#{rank}</b>"
            name_str = f" ({_esc(u['full_name'])})" if u.get("full_name") else ""
            tx_str = f" • {u['tx_count']}x Transaksi" if u.get("tx_count") else ""
            uname = f"@{_esc(u['username'])}" if u.get("username") and not u['username'].startswith("User_") else f"ID: {u['telegram_id']}"
            lines.append(
                f"{medal} <b>{uname}</b>{name_str} (<code>{u['telegram_id']}</code>)\n"
                f"   ├── 💸 <b>Total Volume :</b> <code>{format_idr(u['total_spent_idr'])}</code>{tx_str}\n"
                f"   └── 🎁 <b>Alokasi Hadiah:</b> <code>+{format_idr(reward)}</code>\n"
            )

    needed, balance, shortfall = top_spender_funding(db, period_days)
    lines.append(f"🏦 <b>Kas Bot:</b> <code>{format_idr(balance)}</code> — hadiah dibayar dari Kas Bot "
                 f"(butuh <code>{format_idr(needed)}</code>)")
    if shortfall > 0:
        lines.append(f"\n⚠️ <b>Kas Bot kurang {format_idr(shortfall)}.</b> Isi Kas Bot dulu via QRIS, "
                     "lalu kembali ke sini untuk membagikan hadiah.")
    else:
        lines.append("\n💡 <i>Klik tombol di bawah untuk membagikan saldo hadiah langsung ke akun para pemenang:</i>")
    return "\n".join(lines)


def top_spender_funding(db, period_days: int = 30) -> tuple[int, int, int]:
    """(dana dibutuhkan, saldo Kas Bot, kekurangan) untuk membayar Top Spender saat ini."""
    from services.campaign_service import TOP_SPENDER_REWARDS
    top = crud.get_top_spenders(db, limit=10, period_days=period_days)
    needed = sum(TOP_SPENDER_REWARDS.get(u["rank"], 0) for u in top)
    balance = crud.get_bot_treasury_balance(db)
    return needed, balance, max(0, needed - balance)


def build_admin_top_spenders_keyboard(
    period_days: int = 30,
    shortfall: int = 0,
    *,
    db=None,
    admin_id: int = None,
    chat_id: int = None,
    message_id: int = None,
) -> InlineKeyboardMarkup:
    """Keyboard navigasi Top Spender. Kas Bot kurang -> tombol bagi diganti tombol isi Kas Bot (QRIS)."""
    if shortfall > 0:
        first_row = [InlineKeyboardButton("📲 Isi Kas Bot via QRIS (Uang Asli)", callback_data="admin_treasury_qris_menu")]
    elif db is not None and admin_id is not None:
        token = crud.issue_admin_action_token(
            db, admin_id, "top_spender", str(int(period_days)),
            chat_id=chat_id, message_id=message_id,
        )
        first_row = [InlineKeyboardButton(
            "💰 Eksekusi & Bagikan Hadiah ke Top 10",
            callback_data=f"admin_top_spender_exec_{period_days}_{token}",
        )]
    else:
        first_row = None
    buttons = [
        [
            InlineKeyboardButton("📅 7 Hari", callback_data="admin_top_spender_p_7"),
            InlineKeyboardButton("📅 30 Hari", callback_data="admin_top_spender_p_30"),
            InlineKeyboardButton("📅 90 Hari", callback_data="admin_top_spender_p_90"),
            InlineKeyboardButton("♾️ Semua", callback_data="admin_top_spender_p_0"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Data", callback_data=f"admin_top_spender_p_{period_days}"),
            InlineKeyboardButton("🚫 Pengecualian", callback_data="admin_milestone_excl"),
        ],
        [
            InlineKeyboardButton("🔙 Kembali ke Campaign & Giveaway", callback_data="admin_panel_campaign"),
        ],
        [
            InlineKeyboardButton("🏠 Dashboard Utama", callback_data="admin_panel_main"),
        ],
    ]
    if first_row:
        buttons.insert(0, first_row)
    return InlineKeyboardMarkup(buttons)


def build_admin_random_draw_view(db, pool_segment: str = "ACTIVE_30D") -> str:
    """Membangun teks menu Undian Acak (Flash Giveaway)."""
    seg_names = {
        "ALL": "Semua User (min. 1 transaksi selesai)",
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


def build_admin_random_draw_keyboard(
    pool_segment: str = "ACTIVE_30D",
    *,
    db=None,
    admin_id: int = None,
    chat_id: int = None,
    message_id: int = None,
) -> InlineKeyboardMarkup:
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
            InlineKeyboardButton("🔄 Refresh Pool", callback_data=f"admin_draw_pool_{pool_segment}"),
        ],
        [
            InlineKeyboardButton("🔙 Kembali ke Campaign & Giveaway", callback_data="admin_panel_campaign"),
        ],
        [
            InlineKeyboardButton("🏠 Dashboard Utama", callback_data="admin_panel_main"),
        ],
    ]
    if db is not None and admin_id is not None:
        presets = (
            (5, 25_000, "🎲 Undi 5 Orang @ Rp 25.000"),
            (5, 50_000, "🎲 Undi 5 Orang @ Rp 50.000"),
            (10, 20_000, "🎲 Undi 10 Orang @ Rp 20.000"),
            (20, 10_000, "🎲 Undi 20 Orang @ Rp 10.000"),
        )
        exec_rows = []
        for winner_count, reward, label in presets:
            payload = f"{pool_segment}|{winner_count}|{reward}"
            token = crud.issue_admin_action_token(
                db, admin_id, "random_draw", payload,
                chat_id=chat_id, message_id=message_id,
            )
            exec_rows.append([InlineKeyboardButton(
                label,
                callback_data=f"admin_draw_exec_{pool_segment}_{winner_count}_{reward}_{token}",
            )])
        buttons[1:1] = exec_rows
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


def _callback_chat_id(update) -> int | None:
    chat = getattr(update, "effective_chat", None)
    return getattr(chat, "id", None) if chat is not None else None


def build_admin_send_balance_confirm_view(
    user: User,
    amount: int,
    *,
    db=None,
    admin_id: int = None,
    chat_id: int = None,
    message_id: int = None,
) -> tuple[str, InlineKeyboardMarkup]:
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

    token = ""
    if db is not None and admin_id is not None:
        token = crud.issue_admin_action_token(
            db, admin_id, "send_balance", f"{int(user.telegram_id)}:{int(amount)}",
            chat_id=chat_id, message_id=message_id,
        )
    buttons = []
    if token:
        buttons.append([
            InlineKeyboardButton("🚀 Ya, Kirim Saldo Sekarang!", callback_data=f"admin_send_bal_confirm_{token}"),
        ])
    buttons.append([
        InlineKeyboardButton("🔙 Batal / Ganti Nominal", callback_data=f"admin_send_bal_user_{user.telegram_id}"),
    ])
    return text, InlineKeyboardMarkup(buttons)


def build_admin_treasury_view(
    db,
    *,
    admin_id: int = None,
    chat_id: int = None,
    message_id: int = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Tampilan manajemen Kas & Dompet Bot (Campaign Pool)."""
    treasury_bal = crud.get_bot_treasury_balance(db)

    text = (
        f"{tg_emoji('BANK', '🏦')} <b>KAS & DOMPET CAMPAIGN BOT</b>\n\n"
        f"💰 <b>Saldo Kas Bot Saat Ini:</b> <b>{format_idr(treasury_bal)}</b>\n\n"
        "📌 <b>Fungsi Dompet Kas Bot:</b>\n"
        "• Menyimpan cadangan dana uang asli untuk event Giveaway & Campaign\n"
        "• Sumber dana otomatisasi reward Loyalty & Milestone Top Spender\n"
        "• Memastikan kelancaran distribusi hadiah bagi para pemenang\n\n"
        "👇 <i>Pilih Top-Up via QRIS Uang Asli atau gunakan tombol instan di bawah:</i>"
    )

    buttons = [
        [
            InlineKeyboardButton("📲 Top-Up Kas Bot via QRIS (Uang Asli)", callback_data="admin_treasury_qris_menu"),
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
    if admin_id is not None:
        presets = (100_000, 250_000, 500_000, 1_000_000, 2_500_000, 5_000_000)
        preset_buttons = [
            InlineKeyboardButton(
                f"+{format_idr(amount)}",
                callback_data=(
                    f"admin_treasury_topup_{amount}_"
                    f"{crud.issue_admin_action_token(db, admin_id, 'treasury_preset', str(amount), chat_id=chat_id, message_id=message_id)}"
                ),
            )
            for amount in presets
        ]
        buttons[1:1] = [preset_buttons[i:i + 2] for i in range(0, len(preset_buttons), 2)]
    return text, InlineKeyboardMarkup(buttons)


def build_admin_treasury_qris_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Menu pilihan nominal Top-Up Kas Bot via QRIS (Uang Asli)."""
    text = (
        f"{tg_emoji('BANK', '🏦')} <b>TOP-UP KAS BOT VIA QRIS (UANG ASLI)</b>\n\n"
        "Silakan pilih nominal deposit saldo Kas Bot yang ingin Anda bayar via QRIS:\n\n"
        "📌 <i>Metode Pembayaran Didukung:</i>\n"
        "• <b>Mobile Banking:</b> BCA, Mandiri, BRI, BNI, CIMB, Permata, dll.\n"
        "• <b>E-Wallet:</b> GoPay, OVO, DANA, ShopeePay, LinkAja.\n\n"
        "<i>Uang asli langsung masuk ke akun merchant GoPay/Bank Anda dan saldo Kas Bot otomatis terisi seketika.</i>"
    )
    buttons = [
        [
            InlineKeyboardButton("Rp 50.000", callback_data="admin_treasury_qris_50000"),
            InlineKeyboardButton("Rp 100.000", callback_data="admin_treasury_qris_100000"),
        ],
        [
            InlineKeyboardButton("Rp 250.000", callback_data="admin_treasury_qris_250000"),
            InlineKeyboardButton("Rp 500.000", callback_data="admin_treasury_qris_500000"),
        ],
        [
            InlineKeyboardButton("Rp 1.000.000", callback_data="admin_treasury_qris_1000000"),
            InlineKeyboardButton("Rp 2.500.000", callback_data="admin_treasury_qris_2500000"),
        ],
        [
            InlineKeyboardButton("Rp 5.000.000", callback_data="admin_treasury_qris_5000000"),
            InlineKeyboardButton("✏️ Nominal Kustom", callback_data="admin_treasury_qris_custom"),
        ],
        [
            InlineKeyboardButton("🔙 Batal / Kembali ke Kas Bot", callback_data="camp_treasury_view"),
        ],
    ]
    return text, InlineKeyboardMarkup(buttons)


async def generate_and_send_treasury_qris(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int) -> None:
    """Menyajikan invoice QRIS Dinamis untuk Top-Up Kas Bot Admin (mendukung Chat Pribadi maupun Grup/Topik Forum)."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    # Tentukan tujuan pengiriman (chat_id dan topic message_thread_id)
    target_chat_id = chat.id if chat else (context.user_data.get("admin_treasury_qris_chat_id") if context and context.user_data else None)
    if not target_chat_id and user:
        target_chat_id = user.id

    thread_id = None
    if message and getattr(message, "message_thread_id", None):
        thread_id = message.message_thread_id
    elif update.callback_query and update.callback_query.message and getattr(update.callback_query.message, "message_thread_id", None):
        thread_id = update.callback_query.message.message_thread_id
    elif context and context.user_data and context.user_data.get("admin_treasury_qris_thread_id"):
        thread_id = context.user_data.get("admin_treasury_qris_thread_id")

    status_msg = None
    try:
        if update.callback_query:
            status_msg = await update.callback_query.edit_message_text("⏳ <i>Menyiapkan invoice QRIS Kas Bot...</i>", parse_mode="HTML")
        elif update.message:
            status_msg = await update.message.reply_text("⏳ <i>Menyiapkan invoice QRIS Kas Bot...</i>", parse_mode="HTML")
        elif target_chat_id:
            msg_kw = {"chat_id": target_chat_id, "text": "⏳ <i>Menyiapkan invoice QRIS Kas Bot...</i>", "parse_mode": "HTML"}
            if thread_id:
                msg_kw["message_thread_id"] = thread_id
            status_msg = await context.bot.send_message(**msg_kw)
    except Exception as st_err:
        logger.debug("Info status invoice tidak dapat ditampilkan: %s", st_err)

    import secrets
    topup_id = f"TREASURY-{int(datetime.utcnow().timestamp())}-{secrets.token_hex(3).upper()}"
    expires_at = datetime.utcnow() + timedelta(minutes=settings.ORDER_EXPIRE_MINUTES)

    db = SessionLocal()
    try:
        from database.crud import generate_unique_payment_code, create_topup_order
        from services.fee_service import calculate_qris_mdr
        mdr_idr = calculate_qris_mdr(amount)
        from services.fee_service import qris_max_nominal
        if amount > qris_max_nominal():
            err_text = (
                f"❌ Top-up Kas Bot via QRIS maksimal {format_idr(qris_max_nominal())} per invoice "
                "(batas QRIS Rp 10.000.000 termasuk pajak & kode unik). Bagi menjadi beberapa invoice."
            )
            if status_msg and hasattr(status_msg, "edit_text"):
                await status_msg.edit_text(err_text, parse_mode="HTML")
            else:
                await context.bot.send_message(chat_id=target_chat_id, text=err_text, parse_mode="HTML", message_thread_id=thread_id)
            return

        unique_code = generate_unique_payment_code(db, base_amount=amount + mdr_idr)
        if unique_code is None:
            err_text = (
                "⏳ Antrean QRIS sedang penuh (nominal bentrok dengan tagihan lain). "
                "Coba lagi beberapa menit lagi atau ubah nominal."
            )
            if status_msg and hasattr(status_msg, "edit_text"):
                await status_msg.edit_text(err_text, parse_mode="HTML")
            else:
                await context.bot.send_message(chat_id=target_chat_id, text=err_text, parse_mode="HTML", message_thread_id=thread_id)
            return

        final_amount = amount + mdr_idr + unique_code
        topup_order = create_topup_order(
            db=db,
            topup_id=topup_id,
            telegram_id=user.id,
            amount_idr=final_amount,
            expires_at=expires_at,
        )
        topup_order.unique_code = unique_code
        topup_order.mdr_idr = mdr_idr
        db.commit()
    finally:
        db.close()

    if context and context.user_data is not None:
        context.user_data["active_topup_id"] = topup_id
        context.user_data.pop("admin_awaiting_treasury_qris_custom", None)
        context.user_data.pop("admin_treasury_qris_chat_id", None)
        context.user_data.pop("admin_treasury_qris_thread_id", None)

    mdr_line = f"\n🧾 <b>Biaya QRIS 0,3%</b>: +{format_idr(mdr_idr)}" if mdr_idr else ""
    caption_text = (
        f"{tg_emoji('BANK', '🏦')} <b>INVOICE TOP-UP KAS BOT (QRIS UANG ASLI)</b>\n\n"
        f"🎫 <b>ID Transaksi:</b> <code>{topup_id}</code>\n"
        f"💵 <b>Nominal Masuk Kas:</b> <b>{format_idr(amount)}</b>\n"
        f"💰 <b>Total Transfer:</b> <b>{format_idr(final_amount)}</b>"
        f"{mdr_line}\n"
        f"⏰ <b>Batas Waktu:</b> {settings.ORDER_EXPIRE_MINUTES} Menit\n\n"
        f"📌 <b>Cara Bayar:</b>\n"
        f"1. Scan QRIS di atas dengan <b>BCA, Mandiri, BRI, BNI, GoPay, OVO, DANA, ShopeePay</b>, dll.\n"
        f"2. Nominal <b>{format_idr(final_amount)}</b> akan terisi otomatis.\n"
        f"3. Selesaikan transfer di aplikasi Bank / E-Wallet Anda.\n"
        f"4. Saldo Kas Bot akan <b>otomatis bertambah</b> seketika setelah pembayaran terverifikasi!\n\n"
        f"ℹ️ <i>Pastikan nominal transfer tepat ({format_idr(final_amount)}) agar verifikasi instan.</i>"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Saya Sudah Transfer", callback_data=f"check_topup_{topup_id}")],
        [InlineKeyboardButton("❌ Batalkan Invoice", callback_data=f"cancel_topup_{topup_id}")],
        [InlineKeyboardButton("🔙 Kembali ke Kas Bot", callback_data="camp_treasury_view")],
    ]

    try:
        if update.callback_query:
            await update.callback_query.message.delete()
        elif status_msg:
            await status_msg.delete()
    except Exception:
        pass

    from services.qris_generator import get_qris_image_stream
    qris_stream = get_qris_image_stream(final_amount)
    sent = False

    send_kwargs = {
        "chat_id": target_chat_id,
        "parse_mode": "HTML",
        "reply_markup": InlineKeyboardMarkup(keyboard),
    }
    if thread_id:
        send_kwargs["message_thread_id"] = thread_id

    if qris_stream:
        try:
            await context.bot.send_photo(
                photo=qris_stream.getvalue(),
                caption=caption_text,
                **send_kwargs,
            )
            sent = True
        except Exception as err:
            logger.warning("Gagal kirim QRIS photo treasury ke %s (thread %s): %s", target_chat_id, thread_id, err)

    if not sent:
        try:
            await context.bot.send_message(
                text=caption_text,
                **send_kwargs,
            )
            sent = True
        except Exception as err:
            logger.warning("Gagal kirim QRIS text treasury ke %s (thread %s): %s", target_chat_id, thread_id, err)
            # Fallback ke DM pribadi admin jika chat grup/channel membatasi bot
            if user and target_chat_id != user.id:
                try:
                    dm_kwargs = {
                        "chat_id": user.id,
                        "parse_mode": "HTML",
                        "reply_markup": InlineKeyboardMarkup(keyboard),
                    }
                    if qris_stream:
                        await context.bot.send_photo(photo=qris_stream.getvalue(), caption=caption_text, **dm_kwargs)
                    else:
                        await context.bot.send_message(text=caption_text, **dm_kwargs)
                except Exception as dm_err:
                    logger.error("Gagal fallback kirim QRIS ke DM admin %s: %s", user.id, dm_err)


async def topup_qris_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /topupqris <nominal> atau /qriskas untuk generate invoice QRIS Kas Bot langsung di chat/grup."""
    if not update.effective_user:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    # Jika admin menyertakan nominal langsung (misal: /topupqris 50000, /topupqris 5k, /topupqris 1.5jt)
    if context.args:
        arg_str = context.args[0].lower().replace("k", "000").replace("jt", "000000").replace(".", "").replace(",", "")
        clean_digits = "".join(ch for ch in arg_str if ch.isdigit())
        if not clean_digits or int(clean_digits) < 5000:
            await update.message.reply_text("⚠️ Nominal top-up QRIS Kas Bot minimal Rp 5.000 (contoh: <code>/topupqris 50000</code>).", parse_mode="HTML")
            return
        amount = int(clean_digits)
        if amount > 10_000_000:
            await update.message.reply_text("⚠️ Nominal maksimal per transaksi QRIS adalah Rp 10.000.000 (Limit BI).", parse_mode="HTML")
            return
        await generate_and_send_treasury_qris(update, context, amount)
        return

    # Tanpa argumen -> kirim menu pilihan nominal QRIS Kas Bot
    text, markup = build_admin_treasury_qris_menu()
    thread_id = update.message.message_thread_id if update.message else None
    if context and context.user_data is not None:
        context.user_data["admin_treasury_qris_chat_id"] = update.effective_chat.id if update.effective_chat else None
        context.user_data["admin_treasury_qris_thread_id"] = thread_id

    send_kw = {
        "text": text,
        "reply_markup": markup,
        "parse_mode": "HTML",
    }
    if thread_id:
        send_kw["message_thread_id"] = thread_id

    await update.message.reply_text(**send_kw)



async def guide_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/panduan: admin -> panduan fitur admin; user biasa -> panduan cara transaksi."""
    if is_admin(update.effective_user.id):
        from bot.utils.admin_guide import guide_index
    else:
        from bot.utils.user_guide import guide_index
    text, markup = guide_index()
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def user_guide_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bantuan: panduan cara transaksi (Beli / Jual / Convert) untuk siapa saja, termasuk admin."""
    from bot.utils.user_guide import guide_index
    text, markup = guide_index()
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def refresh_menu_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/refreshmenu: pasang ulang menu ☰ admin (chat ini + semua admin) dan laporkan hasilnya."""
    if not is_admin(update.effective_user.id):
        return
    from bot.utils.command_menu import admin_menu_chat_ids, apply_admin_command_menu

    chats = list(dict.fromkeys([update.effective_chat.id] + admin_menu_chat_ids()))
    lines = []
    for chat_id in chats:
        try:
            count = await apply_admin_command_menu(context.bot, chat_id)
            lines.append(f"✅ <code>{chat_id}</code>: {count} perintah terpasang")
        except Exception as exc:
            lines.append(f"❌ <code>{chat_id}</code>: {html.escape(str(exc))}")
    await update.effective_message.reply_text(
        "🔄 <b>Menu ☰ admin dipasang ulang</b>\n\n" + "\n".join(lines) + "\n\n"
        "<i>Telegram menyimpan daftar perintah di aplikasi. Tutup lalu buka lagi chat ini "
        "(atau ketik / ) agar daftar baru muncul. Chat berstatus ❌ biasanya karena admin belum "
        "pernah /start ke bot.</i>",
        parse_mode="HTML",
    )


async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan Executive Admin Dashboard Control Center."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Anda tidak memiliki akses ke menu administrator.")
        return
    _clear_reward_flags(context)
    # Pastikan menu ☰ admin terpasang di chat ini (sekali per proses; tidak pernah melempar).
    from bot.utils.command_menu import ensure_admin_menu
    await ensure_admin_menu(context.bot, update.effective_chat.id)

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


# ─────────────────────────────────────────────────────────
#  Kirim Reward ke User Pilihan Admin  &  Pengecualian Top Milestone
# ─────────────────────────────────────────────────────────

WIZARD_TTL_SECONDS = 600
_REWARD_WIZARD_FLAGS = ("admin_awaiting_reward_list", "admin_awaiting_reward_msg",
                        "admin_awaiting_milestone_excl")


def _clear_reward_flags(context) -> None:
    """Batalkan SEMUA prompt teks admin yang menunggu (reward / pengecualian / kirim saldo / kas bot / …).

    Dipanggil untuk setiap tombol baru dan /admin: satu prompt aktif pada satu waktu, sehingga
    teks berikutnya tidak 'ditelan' wizard lama yang sudah ditinggalkan admin.
    """
    for key in list(context.user_data):
        if key.startswith("admin_awaiting_") or key in ("admin_reward_batch_id", "admin_reward_skipped",
                                                         "admin_wizard_ts"):
            context.user_data.pop(key, None)


def _arm_reward_wizard(context, *flags: str) -> None:
    """Pasang flag wizard baru (setelah membersihkan yang lama) + cap waktu untuk kedaluwarsa."""
    import time
    _clear_reward_flags(context)
    for flag in flags:
        context.user_data[flag] = True
    context.user_data["admin_wizard_ts"] = time.time()


def _expire_stale_reward_wizard(context) -> None:
    import time
    if any(context.user_data.get(f) for f in _REWARD_WIZARD_FLAGS):
        if time.time() - context.user_data.get("admin_wizard_ts", 0) > WIZARD_TTL_SECONDS:
            for f in _REWARD_WIZARD_FLAGS:
                context.user_data.pop(f, None)


def _fmt_skipped(skipped: list, errors: list, limit: int = 8) -> str:
    """Ringkasan baris yang dilewati (maks `limit` baris supaya pesan tidak panjang)."""
    lines = []
    for err in errors:
        lines.append(f"• baris {err['line']}: {_esc(err['reason'][:120])}")
    for sk in skipped:
        lines.append(f"• <code>{_esc(sk['target'][:40])}</code>: {_esc(sk['reason'][:120])}")
    if not lines:
        return ""
    more = f"\n… dan {len(lines) - limit} lainnya" if len(lines) > limit else ""
    return "⚠️ <b>Dilewati:</b>\n" + "\n".join(lines[:limit]) + more + "\n\n"


def build_reward_intro_view(db, admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from services import reward_service as rs
    treasury = crud.get_bot_treasury_balance(db)
    text = (
        "🎁 <b>KIRIM REWARD KE USER PILIHAN</b>\n\n"
        "Kirim saldo bot ke beberapa orang sekaligus. Nominal <b>bebas beda tiap orang</b>, "
        "pesan boleh santai/custom, dananya diambil dari <b>Kas Bot</b>.\n"
        f"🏦 <b>Kas Bot saat ini:</b> <code>{format_idr(treasury)}</code>\n\n"
        "<b>Cara:</b>\n"
        "1️⃣ Tekan <b>Susun Daftar Reward</b>\n"
        "2️⃣ Ketik daftar — satu orang per baris:\n"
        "<code>@budi 50000\n123456789 25k\n@adminchannel 100.000 | pesan khusus orang ini</code>\n"
        "3️⃣ Tulis pesan untuk penerima (atau pakai pesan standar)\n"
        "4️⃣ Cek ringkasan lalu <b>Kirim</b>\n\n"
        f"<i>Nominal {format_idr(rs.MIN_REWARD_IDR)} – {format_idr(rs.MAX_REWARD_IDR)} per orang · "
        f"maks {rs.MAX_RECIPIENTS} penerima per batch · penerima harus sudah pernah /start bot.</i>"
    )
    buttons = [[InlineKeyboardButton("✏️ Susun Daftar Reward", callback_data="admin_reward_start")]]
    draft = db.query(RewardBatch).filter(
        RewardBatch.created_by == admin_id, RewardBatch.status == "DRAFT"
    ).order_by(RewardBatch.id.desc()).first()
    if draft:
        buttons.append([InlineKeyboardButton(
            f"▶️ Lanjutkan Draft #{draft.id} ({draft.recipient_count} orang · {format_idr(draft.total_amount)})",
            callback_data=f"admin_reward_preview_{draft.id}")])
    buttons += [
        [InlineKeyboardButton("📜 Riwayat Reward", callback_data="admin_reward_history"),
         InlineKeyboardButton("🏦 Kas Bot", callback_data="camp_treasury_view")],
        [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
    ]
    return text, InlineKeyboardMarkup(buttons)


def build_reward_preview_view(db, batch, skipped_text: str = "") -> tuple[str, InlineKeyboardMarkup]:
    from services import reward_service as rs
    items = rs.get_batch_items(batch)
    treasury = crud.get_bot_treasury_balance(db)
    total = sum(i["amount"] for i in items)
    rows = []
    for n, it in enumerate(items, start=1):
        mark = " 💬" if it.get("message") else ""
        rows.append(f"{n}. {_esc(it['label'][:40])} — <b>{format_idr(it['amount'])}</b>{mark}")
    lines = [f"🎁 <b>KONFIRMASI REWARD #{batch.id}</b>\n", f"👥 <b>Penerima ({len(items)}):</b>", "{ROWS}"]
    msg = batch.default_message
    msg_preview = _esc(msg if len(msg) <= 160 else msg[:157] + "…") if msg else "pesan standar"
    lines += [
        "",
        f"💬 <b>Pesan:</b> <i>{msg_preview}</i>" + ("\n<i>💬 = punya pesan khusus sendiri</i>" if any(i.get("message") for i in items) else ""),
        "",
        (skipped_text.rstrip() + "\n") if skipped_text.strip() else None,
        f"💰 <b>Total:</b> <code>{format_idr(total)}</code>",
        f"🏦 <b>Kas Bot:</b> <code>{format_idr(treasury)}</code>",
    ]
    lines = [ln for ln in lines if ln is not None]
    buttons = []
    if treasury < total:
        lines.append(
            f"\n⚠️ <b>Kas Bot kurang {format_idr(total - treasury)}.</b>\n"
            "Isi Kas Bot dulu via QRIS, lalu kembali ke sini (draft tersimpan)."
        )
        buttons.append([InlineKeyboardButton("📲 Isi Kas Bot via QRIS (Uang Asli)", callback_data="admin_treasury_qris_menu")])
        buttons.append([InlineKeyboardButton("🔄 Cek Ulang Kas Bot", callback_data=f"admin_reward_preview_{batch.id}")])
    else:
        lines.append(f"📉 <b>Sisa Kas Bot setelah kirim:</b> <code>{format_idr(treasury - total)}</code>")
        buttons.append([InlineKeyboardButton(f"🚀 Kirim Reward Sekarang ({format_idr(total)})",
                                             callback_data=f"admin_reward_exec_{batch.id}")])
    buttons.append([InlineKeyboardButton("✏️ Ubah Pesan", callback_data=f"admin_reward_editmsg_{batch.id}"),
                    InlineKeyboardButton("❌ Batalkan", callback_data=f"admin_reward_cancel_{batch.id}")])
    text = "\n".join(lines)
    # Batas Telegram 4096: potong daftar penerima (total & tombol tetap utuh).
    shown = len(rows)
    while True:
        block = "\n".join(rows[:shown]) + (f"\n… dan {len(rows) - shown} penerima lainnya" if shown < len(rows) else "")
        candidate = text.replace("{ROWS}", block)
        if len(candidate) <= 3900 or shown <= 3:
            return candidate[:4000], InlineKeyboardMarkup(buttons)
        shown = max(3, shown - 5)


def build_reward_result_text(result: dict) -> str:
    lines = [
        f"✅ <b>REWARD TERKIRIM — Batch #{result['batch_id']}</b>\n",
        f"💸 <b>{result['paid_count']} penerima</b> · total <code>{format_idr(result['paid_total'])}</code>",
        f"🏦 Kas Bot: {format_idr(result['treasury_before'])} → <b>{format_idr(result['treasury_after'])}</b>",
        f"🔔 Notifikasi terkirim: {len(result['notif_ok'])}/{result['paid_count']}",
    ]
    if result["notif_fail"]:
        lines.append("\n📭 <b>Notifikasi gagal</b> (saldo tetap masuk; mereka belum chat / memblokir bot):\n"
                     + ", ".join(_esc(x[:40]) for x in result["notif_fail"]))
    if result["failed"]:
        lines.append("\n⚠️ <b>Gagal dikredit &amp; dikembalikan ke Kas Bot:</b> "
                     + ", ".join(_esc(r["label"][:40]) for r in result["failed"]))
    if result["dropped"]:
        lines.append("\n⏭ <b>Dilewati saat eksekusi:</b> "
                     + ", ".join(_esc(d["label"][:40]) for d in result["dropped"]))
    text = "\n".join(lines)
    # Batas Telegram 4096 (maks 30 penerima × nama panjang bisa melewatinya).
    return text if len(text) <= 3900 else text[:3880].rsplit("\n", 1)[0] + "\n… (daftar dipotong)"


def build_reward_history_view(db, admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = db.query(RewardBatch).filter(RewardBatch.status.in_(("COMPLETED", "CANCELLED", "DRAFT", "RUNNING"))
                                        ).order_by(RewardBatch.id.desc()).limit(10).all()
    lines = ["📜 <b>RIWAYAT REWARD (10 terakhir)</b>\n"]
    if not rows:
        lines.append("<i>Belum ada batch reward.</i>")
    labels = {"COMPLETED": "✅ Terkirim", "CANCELLED": "❌ Dibatalkan", "DRAFT": "📝 Draft", "RUNNING": "⏳ Berjalan"}
    for b in rows:
        when = (b.executed_at or b.created_at)
        lines.append(f"<b>#{b.id}</b> · {when.strftime('%d %b %H:%M')} · {b.recipient_count} orang · "
                     f"{format_idr(b.total_amount)} · {labels.get(b.status, b.status)}")
    buttons = [[InlineKeyboardButton("🎁 Kirim Reward Baru", callback_data="admin_panel_reward")],
               [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")]]
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def build_milestone_exclusion_view(db) -> tuple[str, InlineKeyboardMarkup]:
    rows = crud.list_milestone_exclusions(db)
    lines = [
        "🚫 <b>PENGECUALIAN TOP MILESTONE</b>\n",
        "User di daftar ini <b>tidak ikut peringkat Top Milestone</b> (mis. admin channel airdrop yang "
        "sudah pasti menang). Mereka <b>tetap bisa bertransaksi normal</b> di bot. Peringkat tetap Top 10 — "
        "yang dikecualikan dilewati, urutan berikutnya naik.\n",
        "<i>Hanya user yang menyelesaikan minimal 1 transaksi yang bisa masuk peringkat.</i>\n",
    ]
    buttons = []
    if not rows:
        lines.append("<i>Belum ada user yang dikecualikan.</i>")
    for r in rows[:25]:   # batas pesan Telegram & jumlah tombol
        user = db.query(User).filter(User.telegram_id == r.telegram_id).first()
        name = f"@{user.username}" if user and user.username else (user.full_name if user and user.full_name else "—")
        note = f" — {_esc(r.note[:60])}" if r.note else ""
        lines.append(f"• <code>{r.telegram_id}</code> {_esc(name[:40])}{note}")
        buttons.append([InlineKeyboardButton(f"🗑 Cabut {r.telegram_id}",
                                             callback_data=f"admin_milestone_excl_rm_{r.telegram_id}")])
    if len(rows) > 25:
        lines.append(f"<i>… dan {len(rows) - 25} lainnya (25 terbaru ditampilkan)</i>")
    buttons.insert(0, [InlineKeyboardButton("➕ Tambah Pengecualian", callback_data="admin_milestone_excl_add")])
    buttons.append([InlineKeyboardButton("🔙 Kembali ke Top Spender", callback_data="admin_panel_top_spenders")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


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
            try:
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            except BadRequest as edit_err:
                if "not modified" not in str(edit_err).lower():
                    raise
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
            text, markup = build_admin_send_balance_confirm_view(
                target_user, amount, db=db, admin_id=user_id,
                chat_id=query.message.chat_id, message_id=query.message.message_id,
            )
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
            token = data.removeprefix("admin_send_bal_confirm_")
            payload = crud.claim_admin_action_token(
                db, token, user_id, "send_balance",
                chat_id=query.message.chat_id, message_id=query.message.message_id,
            )
            if payload is None:
                await query.answer("ℹ️ Konfirmasi ini sudah diproses, bukan milik admin ini, atau kedaluwarsa.", show_alert=True)
                return
            try:
                target_text, amount_text = payload.split(":", 1)
                target_id, amount = int(target_text), int(amount_text)
            except (TypeError, ValueError):
                db.rollback()
                await query.answer("Tombol tidak valid. Ulangi dari menu Kirim Saldo.", show_alert=True)
                return
            if not (1_000 <= amount <= 10_000_000):
                db.rollback()
                await query.answer("Nominal di luar batas Rp 1.000 – Rp 10.000.000.", show_alert=True)
                return

            target_user = db.query(User).filter(User.telegram_id == target_id).first()
            if not target_user:
                db.rollback()
                await query.answer("User tidak ditemukan.", show_alert=True)
                return

            old_bal = float(target_user.balance_idr or 0)
            new_bal = crud.credit_user_balance(db, target_id, amount)

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
            context.user_data.pop("admin_awaiting_treasury_custom", None)
            context.user_data.pop("admin_awaiting_treasury_set_manual", None)
            context.user_data.pop("admin_awaiting_treasury_qris_custom", None)
            text, markup = build_admin_treasury_view(
                db, admin_id=user_id, chat_id=query.message.chat_id,
                message_id=query.message.message_id,
            )
            if query.message and query.message.photo:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await query.message.reply_text(text=text, reply_markup=markup, parse_mode="HTML")
            else:
                from bot.utils.telegram_utils import safe_edit_message
                await safe_edit_message(query, text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer("Kas bot dimuat.")

        elif data.startswith("admin_treasury_topup_"):
            try:
                amount_text, token = data.removeprefix("admin_treasury_topup_").rsplit("_", 1)
                amount = int(amount_text)
            except (TypeError, ValueError):
                await query.answer("Tombol topup tidak valid. Buka ulang menu Kas Bot.", show_alert=True)
                return
            if amount not in {100_000, 250_000, 500_000, 1_000_000, 2_500_000, 5_000_000}:
                await query.answer("Nominal preset tidak valid.", show_alert=True)
                return
            if crud.claim_admin_action_token(
                db, token, user_id, "treasury_preset", str(amount),
                chat_id=query.message.chat_id, message_id=query.message.message_id,
            ) is None:
                await query.answer("Tombol ini sudah diproses atau kedaluwarsa.", show_alert=True)
                return
            new_bal = crud.topup_bot_treasury(db, amount, admin_id=user_id, note="Admin Panel Preset Topup")
            await query.answer(f"✅ Kas bot berhasil di-topup +{format_idr(amount)}!\nSaldo sekarang: {format_idr(new_bal)}", show_alert=True)
            text, markup = build_admin_treasury_view(
                db, admin_id=user_id, chat_id=query.message.chat_id,
                message_id=query.message.message_id,
            )
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_treasury_custom":
            context.user_data["admin_awaiting_treasury_custom"] = True
            context.user_data["admin_awaiting_treasury_set_manual"] = False
            await query.answer()
            cancel_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Batal", callback_data="camp_treasury_view")]
            ])
            await query.message.reply_text(
                f"{tg_emoji('BANK', '🏦')} <b>Top Up Saldo Kas Bot Kustom</b>\n\n"
                "Ketik nominal saldo yang ingin Anda tambahkan ke Kas Bot (contoh: <code>1500000</code>):\n\n"
                "<i>Minimal: Rp 1.000</i>",
                reply_markup=cancel_markup,
                parse_mode="HTML"
            )

        elif data == "admin_treasury_set_manual":
            context.user_data["admin_awaiting_treasury_set_manual"] = True
            context.user_data["admin_awaiting_treasury_custom"] = False
            curr_bal = crud.get_bot_treasury_balance(db)
            await query.answer()
            cancel_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Batal", callback_data="camp_treasury_view")]
            ])
            await query.message.reply_text(
                f"⚙️ <b>Atur Ulang Saldo Kas Bot Manual</b>\n\n"
                f"Saldo saat ini: <b>{format_idr(curr_bal)}</b>\n\n"
                "Ketik angka saldo baru yang diinginkan (contoh: <code>5000000</code> atau <code>0</code>):",
                reply_markup=cancel_markup,
                parse_mode="HTML"
            )

        elif data == "admin_treasury_qris_menu":
            text, markup = build_admin_treasury_qris_menu()
            if query.message and query.message.photo:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await query.message.reply_text(text=text, reply_markup=markup, parse_mode="HTML")
            else:
                from bot.utils.telegram_utils import safe_edit_message
                await safe_edit_message(query, text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer("Menu QRIS Kas Bot dimuat.")

        elif data == "admin_treasury_qris_custom":
            context.user_data["admin_awaiting_treasury_qris_custom"] = True
            chat_id = update.effective_chat.id if update.effective_chat else None
            thread_id = query.message.message_thread_id if query.message else None
            context.user_data["admin_treasury_qris_chat_id"] = chat_id
            context.user_data["admin_treasury_qris_thread_id"] = thread_id
            await query.answer()

            is_group = bool(update.effective_chat and getattr(update.effective_chat, "type", "") in ("group", "supergroup", "channel"))
            tip_group = ""
            if is_group:
                tip_group = (
                    "\n\n💡 <b>Petunjuk Top-Up di Grup / Topik:</b>\n"
                    "• <b>Reply (Balas)</b> pesan ini dengan angka nominal (contoh: <code>5000</code>), ATAU\n"
                    "• Ketik langsung perintah: <code>/topupqris 5000</code>\n\n"
                    "<i>(Ketik /cancel untuk membatalkan)</i>"
                )

            cancel_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Batal", callback_data="admin_treasury_qris_menu")]
            ])
            # Menggunakan ForceReply jika di dalam grup agar klien Telegram admin otomatis membuka mode Reply,
            # sehingga pesan angka dari admin dijamin 100% diterima bot walaupun Telegram Group Privacy aktif!
            reply_markup = ForceReply(selective=True) if is_group else cancel_markup

            prompt_text = (
                f"{tg_emoji('BANK', '🏦')} <b>Top Up Kas Bot via QRIS (Nominal Kustom)</b>\n\n"
                "Ketik nominal Rupiah (Uang Asli) yang ingin Anda bayar via QRIS (contoh: <code>750000</code>):\n\n"
                f"<i>Minimal: Rp 5.000 (Maksimal: Rp 10.000.000)</i>"
                f"{tip_group}"
            )
            await query.message.reply_text(
                prompt_text,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )

        elif data.startswith("admin_treasury_qris_"):
            amount = int(data.replace("admin_treasury_qris_", ""))
            await generate_and_send_treasury_qris(update, context, amount)

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

        elif data.startswith("admin_ref_pick_"):
            name = data[len("admin_ref_pick_"):]
            if name not in REFERRAL_ADMIN_SETTINGS:
                await query.answer("Pengaturan tidak dikenal.", show_alert=True)
            else:
                text, markup = build_admin_ref_setting_view(db, name)
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
                await query.answer()

        elif data.startswith("admin_ref_set_"):
            m = re.match(r"admin_ref_set_(.+)_(\d+(?:\.\d+)?)$", data)
            if not m or m.group(1) not in REFERRAL_ADMIN_SETTINGS:
                await query.answer("Pengaturan tidak dikenal.", show_alert=True)
            else:
                name, raw = m.group(1), m.group(2)
                ok, val = parse_ref_setting_value(name, raw)
                if not ok:
                    await query.answer(f"❌ {val}", show_alert=True)
                else:
                    from services.referral_rewards import SETTING_DEFS
                    crud.set_referral_config(db, SETTING_DEFS[name][0], f"{val:g}" if isinstance(val, float) else str(val))
                    await query.answer(
                        f"{REFERRAL_ADMIN_SETTINGS[name]['short']} diset ke {format_ref_setting(name, val)}!",
                        show_alert=True,
                    )
                    text = build_admin_referral_view(db)
                    markup = build_admin_referral_keyboard(db)
                    await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data.startswith("admin_ref_custom_"):
            name = data[len("admin_ref_custom_"):]
            if name not in REFERRAL_ADMIN_SETTINGS:
                await query.answer("Pengaturan tidak dikenal.", show_alert=True)
            else:
                meta = REFERRAL_ADMIN_SETTINGS[name]
                context.user_data["admin_awaiting_ref_cfg"] = name
                await query.answer()
                await query.message.reply_text(
                    f"✏️ <b>Ketik Nilai Baru: {meta['title']}</b>\n\n"
                    f"Satuan: <b>{_REF_UNIT_LABEL[meta['unit']]}</b> "
                    f"(batas {format_ref_setting(name, meta.get('min', 0))} – {format_ref_setting(name, meta['max'])}).",
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

        # ─── KIRIM REWARD KE USER PILIHAN ──────────────────
        elif data == "admin_panel_reward":
            _clear_reward_flags(context)
            text, markup = build_reward_intro_view(db, user_id)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data == "admin_reward_start":
            _arm_reward_wizard(context, "admin_awaiting_reward_list")
            await query.answer()
            await query.message.reply_text(
                "✏️ <b>Ketik daftar penerima</b> — satu orang per baris:\n\n"
                "<code>@budi 50000\n"
                "123456789 25k\n"
                "@adminchannel 100.000 | Makasih ya kak udah mau nerima bot kami 🙏</code>\n\n"
                "• Penerima: <b>@username</b> atau <b>ID Telegram</b>\n"
                "• Nominal: <code>50000</code>, <code>50.000</code>, <code>50k</code>, <code>1,5jt</code>\n"
                "• Setelah tanda <code>|</code> (opsional) = pesan khusus untuk orang itu\n\n"
                "<i>Kirim /cancel untuk membatalkan.</i>",
                parse_mode="HTML",
            )

        elif data.startswith("admin_reward_preview_"):
            from services import reward_service as rs
            batch = db.query(RewardBatch).filter(
                RewardBatch.id == int(data.rsplit("_", 1)[1]), RewardBatch.created_by == user_id,
                RewardBatch.status == "DRAFT").first()
            if not batch:
                await query.answer("Batch tidak ditemukan / sudah diproses.", show_alert=True)
            else:
                skipped = context.user_data.get("admin_reward_skipped", "") if \
                    context.user_data.get("admin_reward_batch_id") == batch.id else ""
                text, markup = build_reward_preview_view(db, batch, skipped)
                context.user_data.pop("admin_awaiting_reward_msg", None)
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
                await query.answer()

        elif data.startswith("admin_reward_editmsg_"):
            batch_id = int(data.rsplit("_", 1)[1])
            if not db.query(RewardBatch.id).filter(RewardBatch.id == batch_id, RewardBatch.created_by == user_id,
                                                   RewardBatch.status == "DRAFT").first():
                await query.answer("Draft tidak ditemukan / bukan milik Anda.", show_alert=True)
                return
            _arm_reward_wizard(context, "admin_awaiting_reward_msg")
            context.user_data["admin_reward_batch_id"] = batch_id
            await query.answer()
            await query.message.reply_text(
                "💬 <b>Ketik pesan untuk para penerima</b> (boleh santai, tidak harus formal).\n"
                "Placeholder opsional: <code>{nama}</code> <code>{nominal}</code> <code>{saldo}</code>\n\n"
                "Info nominal &amp; saldo ditambahkan otomatis di bawah pesan.\n"
                "<i>Penerima yang punya pesan khusus di daftarnya tetap memakai pesan khususnya.</i>",
                parse_mode="HTML",
            )

        elif data.startswith("admin_reward_cancel_"):
            from services import reward_service as rs
            rs.cancel_batch(db, int(data.rsplit("_", 1)[1]), user_id)
            _clear_reward_flags(context)
            await query.answer("Draft reward dibatalkan.")
            text, markup = build_reward_intro_view(db, user_id)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data.startswith("admin_reward_exec_"):
            from services import reward_service as rs
            batch_id = int(data.rsplit("_", 1)[1])
            await query.answer("⏳ Mengirim reward...")
            result = await rs.execute_batch(db, context.bot, batch_id, user_id)
            _clear_reward_flags(context)
            if result["ok"]:
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎁 Kirim Reward Lagi", callback_data="admin_panel_reward")],
                    [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
                ])
                await query.edit_message_text(text=build_reward_result_text(result), reply_markup=markup, parse_mode="HTML")
            elif result.get("error_code") == "treasury":
                batch = db.query(RewardBatch).filter(RewardBatch.id == batch_id).first()
                text, markup = build_reward_preview_view(db, batch)
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
                await query.answer(f"Kas Bot kurang {format_idr(result['shortfall'])}.", show_alert=True)
            else:
                await query.answer(result["error"], show_alert=True)
                text, markup = build_reward_intro_view(db, user_id)
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        elif data == "admin_reward_history":
            text, markup = build_reward_history_view(db, user_id)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        # ─── PENGECUALIAN TOP MILESTONE ──────────────────────
        elif data == "admin_milestone_excl":
            text, markup = build_milestone_exclusion_view(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer()

        elif data == "admin_milestone_excl_add":
            _arm_reward_wizard(context, "admin_awaiting_milestone_excl")
            await query.answer()
            await query.message.reply_text(
                "➕ <b>Ketik user yang dikecualikan</b> dari Top Milestone — satu per baris, catatan opsional:\n\n"
                "<code>@adminchannelA admin channel airdrop\n123456789</code>\n\n"
                "<i>Mereka tetap bisa bertransaksi normal. Kirim /cancel untuk membatalkan.</i>",
                parse_mode="HTML",
            )

        elif data.startswith("admin_milestone_excl_rm_"):
            removed = crud.remove_milestone_exclusion(db, int(data.rsplit("_", 1)[1]), removed_by=user_id)
            await query.answer("Pengecualian dicabut." if removed else "Sudah tidak ada di daftar.")
            text, markup = build_milestone_exclusion_view(db)
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        # ─── TOP SPENDER (Phase 7) ──────────────────────────
        elif data == "admin_panel_top_spenders" or data.startswith("admin_top_spender_p_"):
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
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer(f"Top Spender ({period} hari) dimuat.")

        elif data.startswith("admin_top_spender_exec_"):
            try:
                period_text, token = data.removeprefix("admin_top_spender_exec_").rsplit("_", 1)
                period = int(period_text)
            except (TypeError, ValueError):
                await query.answer("Tombol Top Spender tidak valid. Buka ulang menunya.", show_alert=True)
                return
            if period not in {0, 7, 30, 90}:
                await query.answer("Periode Top Spender tidak valid.", show_alert=True)
                return

            await query.answer("⏳ Sedang memproses pembagian reward Top Spender...", show_alert=False)
            from services.campaign_service import execute_top_spender_campaign
            bot_me = await context.bot.get_me() if context.bot else None
            bot_username = bot_me.username if bot_me else "TokoKoinID_bot"

            result = await execute_top_spender_campaign(
                db=db,
                bot=context.bot,
                admin_id=user_id,
                period_days=period,
                bot_username=bot_username,
                action_token=token,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
            )

            if result.get("error"):
                await query.answer(f"⚠️ {result['error']}", show_alert=True)
            else:
                cnt = result.get("distributed_count", 0)
                tot = result.get("total_amount", 0)
                ns = result.get("notif_success", 0)
                await query.answer(
                    f"✅ Sukses! {cnt} pemenang menerima reward (Total: {format_idr(tot)}). Notif: {ns}/{cnt}",
                    show_alert=True,
                )

            text = build_admin_top_spenders_view(db, period_days=period)
            markup = build_admin_top_spenders_keyboard(
                period_days=period, shortfall=top_spender_funding(db, period)[2],
                db=db, admin_id=user_id, chat_id=query.message.chat_id,
                message_id=query.message.message_id,
            )
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")

        # ─── RANDOM DRAW / UNDI PEMENANG (Phase 7) ───────────
        elif data == "admin_panel_random_draw" or data.startswith("admin_draw_pool_"):
            pool_seg = "ACTIVE_30D"
            if data.startswith("admin_draw_pool_"):
                pool_seg = data.replace("admin_draw_pool_", "")
            text = build_admin_random_draw_view(db, pool_segment=pool_seg)
            markup = build_admin_random_draw_keyboard(
                pool_segment=pool_seg, db=db, admin_id=user_id,
                chat_id=query.message.chat_id, message_id=query.message.message_id,
            )
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            await query.answer(f"Pool {pool_seg} dimuat.")

        elif data.startswith("admin_draw_exec_"):
            try:
                pool_seg, winner_text, reward_text, token = data.removeprefix("admin_draw_exec_").rsplit("_", 3)
                winner_cnt = int(winner_text)
                reward_amt = int(reward_text)
            except (TypeError, ValueError):
                await query.answer("Tombol undian tidak valid. Buka ulang menu undian.", show_alert=True)
                return
            if pool_seg not in {"ALL", "BUYERS", "ACTIVE_30D"}:
                await query.answer("Pool undian tidak valid.", show_alert=True)
                return
            if not (1 <= winner_cnt <= 100 and 1 <= reward_amt <= 10_000_000):
                await query.answer("Jumlah pemenang atau nominal hadiah tidak valid.", show_alert=True)
                return

            await query.answer("🎲 Mengundi & membagikan saldo pemenang...", show_alert=False)
            from services.campaign_service import execute_random_winner_campaign
            bot_me = await context.bot.get_me() if context.bot else None
            bot_username = bot_me.username if bot_me else "TokoKoinID_bot"

            res = await execute_random_winner_campaign(
                db=db,
                bot=context.bot,
                admin_id=user_id,
                pool_segment=pool_seg,
                winner_count=winner_cnt,
                reward_per_winner=reward_amt,
                bot_username=bot_username,
                action_token=token,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
            )

            if res.get("error"):
                await query.answer(f"⚠️ {res['error']}", show_alert=True)
            else:
                cnt = res.get("distributed_count", 0)
                tot = res.get("total_amount", 0)
                ns = res.get("notif_success", 0)
                await query.answer(
                    f"🎉 {cnt} pemenang acak terpilih! Total: {format_idr(tot)}. Notif: {ns}/{cnt}",
                    show_alert=True,
                )

            text = build_admin_random_draw_view(db, pool_segment=pool_seg)
            markup = build_admin_random_draw_keyboard(
                pool_segment=pool_seg, db=db, admin_id=user_id,
                chat_id=query.message.chat_id, message_id=query.message.message_id,
            )
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
            try:
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
            except BadRequest as exc:
                # Refresh dengan data identik: Telegram menolak edit, tapi file tetap harus dikirim.
                if "not modified" not in str(exc).lower():
                    raise
            await query.answer(f"Rekap {days} hari dimuat, file .CSV dikirim di bawah.")
            # File langsung muncul di bawah pesan rekap, tanpa perlu klik tombol download.
            await _send_weekly_report_csv(context.bot, query.message.chat_id, db, days)

        elif data.startswith("admin_export_csv_"):
            days = 7
            try:
                days = int(data.replace("admin_export_csv_", ""))
            except Exception:
                days = 7

            await query.answer("⏳ Menyiapkan file spreadsheet (.CSV)...", show_alert=False)
            await _send_weekly_report_csv(context.bot, query.message.chat_id, db, days)

        elif data == "admin_panel_maint" or data.startswith("admin_panel_mt_"):
            from bot.handlers.admin_maintenance import handle_maintenance_callback
            await handle_maintenance_callback(query, data, user_id)

        elif data == "admin_panel_guide" or data.startswith("admin_panel_guide_"):
            from bot.utils.admin_guide import guide_index, guide_topic
            page = guide_index() if data == "admin_panel_guide" else guide_topic(data[len("admin_panel_guide_"):])
            if page is None:
                await query.answer("Topik panduan tidak ditemukan.", show_alert=True)
            else:
                text, markup = page
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
                await query.answer()

        elif data == "admin_panel_close":
            await query.answer("Panel ditutup.")
            await query.message.delete()

    except Exception as exc:
        logger.error(f"Error in admin_panel_callback ({data}): {exc}", exc_info=True)
        await query.answer(f"❌ Error: {exc}", show_alert=True)
    finally:
        db.close()


async def _send_weekly_report_csv(bot, chat_id, db, days: int) -> None:
    """Kirim file rekap transaksi .CSV sebagai pesan baru di chat (muncul di bawah rekap)."""
    from services.report_service import (
        get_weekly_transactions_data,
        generate_weekly_report_csv_buffer,
    )
    transactions = get_weekly_transactions_data(db, days=days)
    csv_buf = generate_weekly_report_csv_buffer(transactions)
    now_wib = datetime.utcnow() + timedelta(hours=7)
    filename = f"laporan_transaksi_{days}hari_{now_wib.strftime('%Y%m%d_%H%M%S')}.csv"
    caption = (
        f"📑 <b>File Laporan Transaksi ({days} Hari Terakhir)</b>\n"
        f"Total: <b>{len(transactions)} baris data transaksi</b>.\n\n"
        f"💡 <i>Dapat langsung di-import ke Google Sheets atau dibuka di Microsoft Excel. "
        f"Semua waktu dalam WIB.</i>"
    )
    await bot.send_document(
        chat_id=chat_id,
        document=csv_buf,
        filename=filename,
        caption=caption,
        parse_mode="HTML",
    )


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


async def resend_testimony_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /posttesti atau /resendtesti <ORDER_ID> untuk admin: kirim testimoni transaksi order ke channel."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "📌 <b>Format Perintah:</b>\n"
            "<code>/posttesti &lt;ORDER_ID&gt;</code>\n\n"
            "Contoh:\n"
            "<code>/posttesti ORD-20261004-479</code>\n\n"
            "💡 <i>Gunakan perintah ini untuk memposting testimoni order lama yang belum sempat masuk ke channel.</i>",
            parse_mode="HTML",
        )
        return

    order_id = args[0].strip()
    db = SessionLocal()
    try:
        from database.models import Order
        from services.testimony_service import post_transaction_testimony, DEFAULT_TESTIMONY_CHANNEL

        order = db.query(Order).filter(Order.order_id == order_id).first()
        if not order:
            await update.message.reply_text(f"❌ Order <code>{html.escape(order_id)}</code> tidak ditemukan di database.", parse_mode="HTML")
            return

        channel_target = os.getenv("TESTIMONY_CHANNEL") or getattr(settings, "TESTIMONY_CHANNEL_ID", None) or DEFAULT_TESTIMONY_CHANNEL
        success = await post_transaction_testimony(context.bot, order, db=db, channel=channel_target)
        if success:
            await update.message.reply_text(
                f"✅ <b>Testimoni Terkirim!</b>\n\n"
                f"Order <code>{html.escape(order.order_id)}</code> ({order.crypto_amount} {order.crypto_symbol}) berhasil diposting ke channel <code>{channel_target}</code>.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"❌ <b>Gagal Mengirim Testimoni:</b>\n\n"
                f"Pastikan bot sudah menjadi Administrator di channel <code>{channel_target}</code> dengan hak akses <b>Post Messages</b>.",
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"Error resend testimony {order_id}: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Terjadi kesalahan: {html.escape(str(e))}")
    finally:
        db.close()


async def resend_recent_testimonies_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /postlasttesti [limit] untuk memposting transaksi completed terakhir ke channel testimoni."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    args = context.args or []
    limit = 5
    if args and args[0].isdigit():
        limit = min(max(1, int(args[0])), 20)

    db = SessionLocal()
    try:
        from database.models import Order
        from services.testimony_service import post_transaction_testimony, DEFAULT_TESTIMONY_CHANNEL

        orders = (
            db.query(Order)
            .filter(func.lower(Order.status) == "completed")
            .order_by(Order.id.desc())
            .limit(limit)
            .all()
        )

        if not orders:
            await update.message.reply_text("ℹ️ Tidak ada order completed yang ditemukan di database.")
            return

        channel_target = os.getenv("TESTIMONY_CHANNEL") or getattr(settings, "TESTIMONY_CHANNEL_ID", None) or DEFAULT_TESTIMONY_CHANNEL
        sent_count = 0
        for o in reversed(orders):
            ok = await post_transaction_testimony(context.bot, o, db=db, channel=channel_target)
            if ok:
                sent_count += 1
            await asyncio.sleep(0.5)

        await update.message.reply_text(
            f"✅ <b>Selesai!</b>\n\n"
            f"Berhasil memposting <b>{sent_count}/{len(orders)}</b> testimoni transaksi ke channel <code>{channel_target}</code>.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error resend recent testimonies: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Terjadi kesalahan: {html.escape(str(e))}")
    finally:
        db.close()



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
            crypto_str = format_crypto_copy(o.crypto_amount, o.crypto_symbol, exact=True)

            text_lines.append(
                f"• <b>{o.order_id}</b> ({o_type})\n"
                f"  🚦 Status: <b>{o.status.upper()}</b>\n"
                f"  🪙 Koin: {crypto_str} ({o.network})\n"
                f"  💳 IDR: <code>{format_idr(o.total_idr)}</code>\n"
                f"  👤 User ID: <code>{o.telegram_id}</code>\n"
            )
        
        await update.message.reply_text("\n".join(text_lines), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error orders_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memuat daftar order.")
    finally:
        db.close()


def _confirmable_payout_order(order) -> bool:
    """Order Beli/Convert yang user-nya SUDAH membayar, sehingga admin boleh menyelesaikannya
    setelah mengirim koin manual. Belum bayar/expired/cancelled tidak termasuk."""
    status = (order.status or "").lower()
    if status == "completed":
        return True
    if status in ("paid", "payout_processing", "manual_review", "failed",
                  "crypto_confirmed", "payout_queued"):
        return True
    # Beli pakai Saldo Bot: status tetap "pending" tetapi saldo sudah dipotong (paid_at terisi).
    return (status == "pending" and order.order_type == "buy"
            and order.payment_method == "BOT_BALANCE" and order.paid_at is not None)


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
        if order.order_type != "sell" and not _confirmable_payout_order(order):
            await update.message.reply_text(
                f"⛔ Order <code>{html.escape(order_id)}</code> berstatus <b>{html.escape(order.status)}</b> — "
                "pembayaran/deposit user belum diterima, jadi tidak bisa diselesaikan manual.",
                parse_mode="HTML")
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
            crud.release_order_inventory(db, order_id, consumed=True)
            from services.testimony_service import schedule_transaction_testimony
            schedule_transaction_testimony(context.bot, order, db=db)
        
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
        from services.testimony_service import schedule_transaction_testimony
        schedule_transaction_testimony(bot, order, db=db)

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


async def admin_verify_sell_deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tombol eskalasi hash: admin menyatakan deposit sell sah — BUKAN Rupiah sudah dikirim.

    Order menjadi CRYPTO_CONFIRMED (hash dikunci ke order ini), user diberi tahu deposit
    masuk, dan admin menerima langkah berikutnya: transfer Rupiah lalu "Sudah Ditransfer".
    """
    from html import escape as _esc
    from sqlalchemy.exc import IntegrityError
    from database.models import DepositClaim
    from services import tx_verifier
    from services.detector import DepositDetector
    from bot.utils.telegram_utils import safe_send_message

    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_verify_sell_deposit_", "").strip()
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order or order.order_type != "sell":
            await query.answer("❌ Order jual tidak ditemukan.", show_alert=True)
            return
        if order.status.lower() in ("completed", "cancelled", "rejected"):
            await query.answer(f"ℹ️ Order sudah {order.status}. Tidak ada yang diubah.", show_alert=True)
            return

        if order.status != "CRYPTO_CONFIRMED":
            raw_hash = (order.deposit_tx_hash or order.tx_hash or "").strip()
            tx_hash = ""
            if raw_hash and not raw_hash.startswith("PHOTO:"):
                try:
                    tx_hash = tx_verifier.normalize_tx_hash(order.network, raw_hash)
                except ValueError:
                    tx_hash = raw_hash
            if tx_hash and DepositDetector._is_hash_used(db, tx_hash, exclude_order=order_id):
                await query.answer("⛔ Hash ini sudah dipakai order lain. Periksa manual.", show_alert=True)
                return
            old_status = order.status
            try:
                if tx_hash and not db.query(DepositClaim).filter(
                        DepositClaim.tx_hash == tx_hash, DepositClaim.order_id == order_id).first():
                    db.add(DepositClaim(network=order.network.upper(), tx_hash=tx_hash, order_id=order_id))
                    db.flush()
                changed = db.query(Order).filter(
                    Order.order_id == order_id, Order.status == old_status,
                ).update({"status": "CRYPTO_CONFIRMED", "deposit_tx_hash": tx_hash or order.deposit_tx_hash},
                         synchronize_session=False)
                if changed != 1:
                    db.rollback()
                    await query.answer("ℹ️ Status order berubah, coba muat ulang.", show_alert=True)
                    return
                db.add(AuditLog(
                    telegram_id=order.telegram_id, action="SELL_DEPOSIT_MANUAL_VERIFIED",
                    order_id=order_id, from_status=old_status, to_status="CRYPTO_CONFIRMED",
                    details=f"Admin {user_id} memverifikasi deposit manual. Hash: {tx_hash or '-'}",
                ))
                db.commit()
            except IntegrityError:
                db.rollback()
                await query.answer("⛔ Hash ini sudah dipakai order lain. Periksa manual.", show_alert=True)
                return
            db.refresh(order)
            await safe_send_message(
                context.bot, order.telegram_id,
                f"✅ <b>Deposit Order <code>{_esc(order_id)}</code> Terverifikasi</b>\n\n"
                "Admin akan segera memproses pembayaran Rupiah ke rekeningmu.\n"
                "⏳ Mohon tunggu transfer admin (estimasi maksimal 20 menit pada jam layanan 08.00 - 23.59 WIB).")

        late = db.query(AuditLog.id).filter(
            AuditLog.order_id == order_id, AuditLog.action == "DEPOSIT_HASH_NEEDS_REVIEW",
            AuditLog.details.like("%masa berlaku order Jual%")).first()
        late_warning = (
            "⚠️ <b>DEPOSIT TELAT</b> — angka terkunci di bawah sudah tidak berlaku bila harga turun. "
            "Transfer sesuai harga terkini (lihat pesan eskalasi).\n\n" if late else "")

        await query.answer("✅ Deposit ditandai terverifikasi. Lanjutkan transfer Rupiah.", show_alert=False)
        await query.message.reply_text(
            f"💰 <b>DEPOSIT SELL TERVERIFIKASI (MANUAL)</b>\n\n{late_warning}"
            f"Order: <code>{_esc(order_id)}</code>\n"
            f"User ID: <code>{order.telegram_id}</code>\n"
            f"‼️ <b>TRANSFER RUPIAH:</b> <b>{format_idr(order.total_idr)}</b> ke rekening:\n"
            f"<code>{_esc(order.buyer_wallet or '-')}</code>\n\n"
            f"Setelah transfer, tekan tombol di bawah.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Sudah Ditransfer", callback_data=f"admin_confirm_sell_{order_id}"),
                InlineKeyboardButton("📸 Upload Bukti Transfer", callback_data=f"admin_upload_proof_{order_id}"),
            ]]),
        )
    except Exception as e:
        logger.error(f"Error admin_verify_sell_deposit_callback {order_id}: {e}", exc_info=True)
        await query.answer("❌ Gagal memproses verifikasi deposit.", show_alert=True)
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
        context.user_data.pop("admin_awaiting_payout_proof_order_id", None)
        context.user_data["admin_awaiting_proof_order_id"] = order_id
        await query.answer("📸 Silakan kirimkan foto bukti transfer.", show_alert=False)

        prompt_msg = (
            f"📸 <b>UPLOAD BUKTI TRANSFER PEMBAYARAN</b>\n\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"Total Rupiah: <b>{format_idr(int(order.total_idr or 0))}</b>\n"
            f"Rekening Tujuan: <code>{_esc(order.buyer_wallet or '-')}</code>\n\n"
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
        crud.release_order_inventory(db, order_id, consumed=True)

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
            from services.testimony_service import schedule_transaction_testimony
            schedule_transaction_testimony(context.bot, order, db=db)
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


async def admin_manual_payout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tombol Admin: payout crypto gagal (mis. RPC error) → admin kirim manual lalu upload SS transfer."""
    from bot.utils.manual_payout import CALLBACK_PREFIX, manual_payout_block_reason

    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.removeprefix(CALLBACK_PREFIX).strip()
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        reason = manual_payout_block_reason(order)
        if reason:
            await query.answer(reason, show_alert=True)
            return

        context.user_data.pop("admin_awaiting_proof_order_id", None)
        context.user_data["admin_awaiting_payout_proof_order_id"] = order_id
        await query.answer("📸 Kirim foto SS transfer ke chat ini.", show_alert=False)

        if order.order_type == "swap":
            amount_str = f"{format_crypto_copy(order.target_crypto_amount or 0, order.target_crypto_symbol or '', exact=True)} ({order.target_network})"
        else:
            amount_str = f"{format_crypto_copy(order.crypto_amount or 0, order.crypto_symbol or '', exact=True)} ({order.network})"

        warning = ""
        if order.payout_tx_hash:
            warning = (
                f"\n⚠️ <b>Order ini sudah punya TX hash broadcast:</b> <code>{_esc(order.payout_tx_hash)}</code>\n"
                f"Cek explorer dulu. Jika koin sebenarnya sudah masuk ke wallet tujuan, JANGAN kirim ulang.\n"
            )
        await query.message.reply_text(
            f"📸 <b>KIRIM SS TRANSFER MANUAL</b>\n\n"
            f"Order ID: <code>{_esc(order_id)}</code>\n"
            f"Koin: <b>{amount_str}</b>\n"
            f"Wallet Tujuan: <code>{_esc(order.buyer_wallet or '-')}</code>\n"
            f"{warning}\n"
            f"👉 <b>Kirim koin dari wallet Anda, lalu kirim FOTO / SCREENSHOT bukti transfer ke chat ini.</b>\n"
            f"Bot akan meneruskan foto ke user dengan catatan transaksi berhasil ditransfer oleh admin, "
            f"dan order otomatis menjadi COMPLETED.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error admin_manual_payout_callback {order_id}: {e}", exc_info=True)
        await query.answer("❌ Terjadi kesalahan.", show_alert=True)
    finally:
        db.close()


async def handle_admin_payout_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Foto SS transfer manual dari admin: teruskan ke user dan selesaikan order buy/convert."""
    from bot.utils.manual_payout import manual_payout_block_reason

    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    order_id = context.user_data.pop("admin_awaiting_payout_proof_order_id", None)
    if not order_id or not update.message or not update.message.photo:
        return

    photo_file_id = update.message.photo[-1].file_id

    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        reason = manual_payout_block_reason(order)
        if reason:
            await update.message.reply_text(f"⚠️ {reason} Bukti tidak diteruskan ke user.")
            return

        # Klaim atomik: cegah balapan dengan Approve/finalize otomatis yang berjalan bersamaan.
        old_status = order.status
        claimed = db.query(Order).filter(
            Order.order_id == order_id, Order.status == old_status,
        ).update({"status": "payout_processing", "updated_at": datetime.utcnow()}, synchronize_session=False)
        db.commit()
        if claimed != 1:
            await update.message.reply_text("ℹ️ Status order baru saja berubah. Muat ulang order lalu coba lagi.")
            return
        db.refresh(order)
        crud.update_order_status(
            db, order_id, new_status="completed",
            completed_at=datetime.utcnow(),
            failure_reason=None,
        )
        crud.release_order_inventory(db, order_id, consumed=True)
        db.add(AuditLog(
            telegram_id=order.telegram_id,
            action="PAYOUT_MANUAL_PROOF",
            order_id=order_id,
            from_status=old_status,
            to_status="completed",
            details=f"Admin {user_id} mengirim crypto manual dan mengunggah SS transfer.",
        ))
        db.commit()
        db.refresh(order)

        if order.order_type == "swap":
            crypto_str = format_crypto(float(order.target_crypto_amount or 0), order.target_crypto_symbol or "")
            network = order.target_network
        else:
            crypto_str = format_crypto(float(order.crypto_amount or 0), order.crypto_symbol or "")
            network = order.network

        user_caption = (
            f"🎉 <b>CRYPTO TELAH DITRANSFER OLEH ADMIN!</b>\n\n"
            f"Transaksi Anda berhasil. Pengiriman otomatis sempat terkendala, sehingga koin "
            f"dikirim <b>manual oleh admin</b> ke wallet Anda.\n\n"
            f"📝 <b>ID Order:</b> <code>{html.escape(order.order_id)}</code>\n"
            f"🪙 <b>Jumlah:</b> <b>{crypto_str}</b> ({html.escape(str(network or '-'))})\n"
            f"🏦 <b>Wallet Tujuan:</b> <code>{html.escape(order.buyer_wallet or '-')}</code>\n\n"
            f"📸 <i>Screenshot bukti transfer terlampir di atas.</i>\n\n"
            f"✅ <b>Status: SELESAI / COMPLETED</b>\n\n"
            f"Silakan periksa saldo wallet Anda.\n\n"
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
                chat_id=order.telegram_id, photo=photo_file_id,
                caption=user_caption, reply_markup=menu_keyboard, parse_mode="HTML",
            )
            sent_to_user = True
        except Exception as send_err:
            logger.error(f"Gagal mengirim SS transfer manual ke user {order.telegram_id}: {send_err}")

        try:
            from services.testimony_service import schedule_transaction_testimony
            schedule_transaction_testimony(context.bot, order, db=db)
        except Exception as texc:
            logger.warning(f"Gagal trigger testimony order {order.order_id}: {texc}")

        await update.message.reply_text(
            f"✅ <b>SS TRANSFER MANUAL DITERUSKAN!</b>\n\n"
            f"Order ID: <code>{_esc(order_id)}</code>\n"
            f"User ID: <code>{order.telegram_id}</code>\n"
            f"Status Order: <b>COMPLETED</b>\n"
            f"Pengiriman ke user: {'Sukses' if sent_to_user else 'Gagal (User memblokir bot/chat error)'}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error handle_admin_payout_proof {order_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memproses SS transfer manual.")
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
    "TRON": ("TRON", ["TRX"]),
    "TRX": ("TRON", ["TRX"]),
    "TRC20": ("TRON", ["TRX"]),
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
        "Base", "BSC", "Arbitrum", "Polygon", "Solana",
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
    - Koin Ready: /broadcast --ready Base (otomatis format koin ready)
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
            "Koin Ready: <code>/broadcast --ready Base</code>\n"
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
                "• <code>/broadcast --ready Base</code>\n"
                "• <code>/broadcast --ready Solana</code>\n"
                "• <code>/broadcast --ready BSC</code>\n"
                "• <code>/broadcast --buyers --ready Base</code> <i>(khusus member pembeli)</i>\n\n"
                f"📌 <b>Jaringan Tersedia:</b>\n<code>{nets_str}</code>\n\n"
                "💡 <i>Anda juga bisa mengirim foto poster dengan caption <code>/broadcast --ready Base</code>.</i>",
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


async def _finish_admin_action(query, tag: str) -> None:
    """Tandai pesan admin setelah approve/reject. Pesan foto -> caption, notifikasi teks -> teks;
    bila pesannya Antrean Order, antrean dimuat ulang (bukan diganti satu order)."""
    try:
        msg = query.message
        if msg is None:
            return
        if msg.caption is not None:
            await query.edit_message_caption(caption=f"{msg.caption_html}\n\n{tag}", parse_mode="HTML")
        elif "ANTREAN ORDER AKTIF" in (msg.text or ""):
            db = SessionLocal()
            try:
                text, markup = build_admin_orders_view(db)
            finally:
                db.close()
            await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
        else:
            await query.edit_message_text(text=f"{msg.text_html or ''}\n\n{tag}", parse_mode="HTML")
    except Exception as edit_err:
        logger.debug(f"Tandai pesan admin gagal (aksi tetap berjalan): {edit_err}")


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

        await _finish_admin_action(query, "✅ <b>APPROVED &amp; DIEKSEKUSI OTOMATIS OLEH ADMIN</b>")
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
    order_id_confirmed = order_id.startswith("yes_")
    if order_id_confirmed:
        order_id = order_id[4:]
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order or order.order_type != "buy":
            await query.answer("❌ Order beli tidak ditemukan.", show_alert=True)
            return

        # Order yang sedang atau sudah payout tidak boleh ditolak dari sini
        if order.status in ("paid", "payout_processing", "completed"):
            await query.answer(
                f"⛔ Order berstatus {order.status} dan sedang/sudah diproses — tidak bisa ditolak.",
                show_alert=True,
            )
            return

        # B9: Order bertipe Saldo Bot yang ditolak admin -> otomatis refund ke saldo bot user
        if order.payment_method == "BOT_BALANCE":
            if order.status == "manual_review" and not order_id_confirmed:
                await query.answer()
                await query.message.reply_text(
                    f"⚠️ <b>KONFIRMASI TOLAK &amp; REFUND</b>\n\n"
                    f"Order: <code>{html.escape(order_id)}</code>\n"
                    f"Saldo {format_idr(order.total_idr)} akan dikembalikan ke user. "
                    f"Pastikan koin <b>BELUM terkirim</b> ke wallet user (cek explorer) agar tidak bayar dua kali.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Ya, Refund Saldo", callback_data=f"admin_reject_buy_yes_{order_id}"),
                    ]]),
                )
                return
            refund = crud.reject_and_refund_bot_balance_order(db, order_id, user_id)
            if not refund.get("refunded"):
                reason = refund.get("reason")
                if reason == "payout_may_be_sent":
                    message = ("⛔ Order ini punya jejak pengiriman/terputus — koin mungkin sudah terkirim. "
                               "Cek explorer; jangan refund otomatis.")
                elif reason == "debit_not_confirmed":
                    message = "⛔ Debit saldo order ini tidak tercatat. Tidak dilakukan refund otomatis agar saldo tidak bertambah tanpa dasar."
                elif reason == "invalid_refund_amount":
                    message = "⛔ Nominal refund tidak valid. Order tidak diubah; periksa data order."
                else:
                    message = f"ℹ️ Order tidak dapat ditolak otomatis (status: {refund.get('status', order.status)})."
                await query.answer(message, show_alert=True)
                return

            refund_amount = int(refund["amount"])
            db.refresh(order)
            crud.release_order_inventory(db, order_id)

            from bot.utils.telegram_utils import safe_send_message
            await safe_send_message(
                context.bot, order.telegram_id,
                f"❌ <b>Order {order_id} Ditolak</b>\n\n"
                f"Order beli Anda ditolak oleh admin.\n"
                f"Dana sebesar <b>{format_idr(refund_amount)}</b> telah otomatis dikembalikan ke <b>Saldo Bot</b> Anda.",
            )
            await query.answer("Order ditolak & Saldo Bot berhasil di-refund ke user.")
            await _finish_admin_action(query, "❌ <b>DITOLAK OLEH ADMIN (SALDO DI-REFUND)</b>")
            return

        if order.payment_method == "GOPAY_QRIS":
            if order.status != "pending" or order.paid_at:
                await query.answer(
                    f"⛔ Order berstatus {order.status} ({order.payment_method or '-'}) dan sudah dibayar/diproses — "
                    "tidak bisa ditolak dari sini.", show_alert=True)
                return
            changed = db.query(Order).filter(Order.order_id == order_id, Order.status == "pending").update(
                {"status": "rejected", "failure_reason": "Ditolak oleh admin"}, synchronize_session=False)
            db.commit()
            if changed != 1:
                await query.answer("ℹ️ Status order sudah berubah.", show_alert=True)
                return
            crud.release_order_inventory(db, order_id)
            from bot.utils.telegram_utils import safe_send_message
            await safe_send_message(
                context.bot, order.telegram_id,
                f"❌ <b>Order {order_id} Ditolak</b>\n"
                f"Bukti pembayaran Anda tidak dapat diverifikasi oleh admin. Silakan hubungi admin jika ada kendala."
            )
            await query.answer("Order berhasil ditolak.")
            await _finish_admin_action(query, "❌ <b>DITOLAK OLEH ADMIN</b>")
            return

        await query.answer(
            f"⛔ Order berstatus {order.status} ({order.payment_method or '-'}) tidak dapat ditolak dari sini.",
            show_alert=True,
        )
    except Exception as e:
        logger.error(f"Error admin_reject_buy_callback {order_id}: {e}", exc_info=True)
        try:
            await query.answer("❌ Terjadi kesalahan saat memproses penolakan order.", show_alert=True)
        except Exception:
            pass
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

        # Admin sudah memeriksa bukti: topup EXPIRED (bayar di menit terakhir) tetap bisa dikredit.
        settled = crud.claim_and_credit_topup(db, topup_id, allow_expired=True)
        if settled is None:
            await query.answer("ℹ️ Topup ini sudah diproses sistem.", show_alert=True)
            return

        # Net (tanpa pajak QRIS) & TREASURY ke kas bot — sama dengan jalur otomatis.
        is_treasury, net_amt, new_bal = settled
        label = "Kas Bot" if is_treasury else "Saldo Bot Anda"

        from bot.utils.telegram_utils import safe_send_message
        menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]])
        await safe_send_message(
            context.bot, topup.telegram_id,
            f"✅ <b>PEMBAYARAN TOPUP TERVERIFIKASI ADMIN!</b>\n\n"
            f"🎉 Topup sebesar <b>{format_idr(net_amt)}</b> telah berhasil di-approve!\n"
            f"💳 <b>Total {label} Saat Ini</b>: <b>{format_idr(int(new_bal))}</b>",
            reply_markup=menu_keyboard
        )

        await query.answer("Topup berhasil di-approve!")
        caption_now = query.message.caption or ""
        await query.edit_message_caption(
            caption=f"{caption_now}\n\n✅ <b>APPROVED & SALDO DITAMBAHKAN OLEH ADMIN</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error admin_approve_topup_callback {topup_id}: {e}", exc_info=True)
        try:
            await query.answer("❌ Gagal memproses approval topup.", show_alert=True)
        except Exception:
            pass
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
        # Hanya topup yang masih menunggu; topup SUCCESS (saldo sudah masuk) tidak boleh dibatalkan.
        changed = db.query(TopupOrder).filter(
            TopupOrder.topup_id == topup_id, TopupOrder.status.in_(["PENDING", "EXPIRED"]),
        ).update({"status": "CANCELLED"}, synchronize_session=False)
        db.commit()
        if changed != 1:
            await query.answer("⛔ Topup sudah diproses/lunas — tidak bisa ditolak.", show_alert=True)
            return
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
    except Exception as e:
        logger.error(f"Error admin_reject_topup_callback {topup_id}: {e}", exc_info=True)
        try:
            await query.answer("❌ Gagal memproses penolakan topup.", show_alert=True)
        except Exception:
            pass
    finally:
        db.close()


async def admin_approve_swap_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Approve & eksekusi pengiriman koin tujuan Swap."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    raw = query.data.replace("admin_approve_swap_", "")
    # Tombol ini MENGIRIM koin (melewati cek pemilik deposit), jadi butuh konfirmasi kedua.
    confirmed = raw.startswith("yes_")
    if raw.startswith("no_"):
        await query.answer("Dibatalkan. Tidak ada koin yang dikirim.")
        await _finish_admin_action(query, "↩️ <b>DIBATALKAN</b> — tidak ada koin dikirim.")
        return
    order_id = raw[4:] if confirmed else raw
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
        if not confirmed:
            await query.answer()
            await query.message.reply_text(
                f"⚠️ <b>KONFIRMASI KIRIM KOIN</b>\n\n"
                f"Order: <code>{html.escape(order_id)}</code>\n"
                f"Kirim: {format_crypto_copy(order.target_crypto_amount, order.target_crypto_symbol)} "
                f"({html.escape(str(order.target_network))}) ke <code>{html.escape(str(order.buyer_wallet))}</code>\n\n"
                f"Apakah Anda yakin transaksi ini <b>sah dan bukan milik pihak ketiga</b>? "
                f"Pengaman pemilik deposit akan dilewati dan koin toko langsung dikirim.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Ya, Kirim Koin", callback_data=f"admin_approve_swap_yes_{order_id}"),
                    InlineKeyboardButton("↩️ Batal", callback_data=f"admin_approve_swap_no_{order_id}"),
                ]]),
            )
            return
        await query.answer("Memeriksa ulang deposit on-chain...")
        from services.detector import deposit_detector
        if order.status == "WAITING_CRYPTO_DEPOSIT":
            # trusted: admin sudah memastikan pemilik deposit / kurs (eskalasi detector).
            await deposit_detector._process_order(db, order, context.application, trusted=True)
        else:
            await deposit_detector._execute_payout(db, order, context.application)
        db.refresh(order)
        await query.message.reply_text(f"Status order {order.order_id}: {order.status}. Foto saja tidak mengesahkan deposit.")
    except Exception as e:
        logger.error(f"Error admin_approve_swap_callback {order_id}: {e}", exc_info=True)
        await query.answer(f"❌ Error: {e}", show_alert=True)
    finally:
        db.close()


async def admin_recheck_swap_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tombol "Cek Ulang Deposit": jalankan pemeriksaan STANDAR (sama dengan pemindai otomatis).

    Tidak melewati pengaman pemilik deposit/kurs: koin hanya terkirim bila deposit lolos semua
    pengecekan; bila mencurigakan, order tetap menunggu keputusan admin.
    """
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return
    order_id = query.data.replace("admin_recheck_swap_", "")
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order or order.order_type != "swap":
            await query.answer("❌ Order convert tidak ditemukan.", show_alert=True)
            return
        if order.status != "WAITING_CRYPTO_DEPOSIT":
            await query.answer(f"Status order: {order.status}. Tidak ada yang perlu dicek ulang.", show_alert=True)
            return
        from services.detector import deposit_detector
        await deposit_detector._process_order(db, order, context.application, trusted=False)
        db.refresh(order)
        # Hasil ditampilkan di pesan yang SAMA (blok status diperbarui), bukan pesan baru tiap klik.
        from bot.utils.telegram_utils import refresh_message_status, wib_clock
        stamp = wib_clock()
        status = (
            f"🔄 <b>Dicek {stamp} WIB</b> — status order: <code>{html.escape(str(order.status))}</code>\n"
            f"<i>Pengecekan standar saja; deposit yang mencurigakan tidak dikirim otomatis. "
            f"Foto tidak mengesahkan deposit.</i>"
        )
        if await refresh_message_status(query, status):
            await query.answer(f"Status: {order.status}")
        else:
            await query.answer(
                f"Status order {order.order_id}: {order.status} (dicek {stamp} WIB). "
                f"Pengecekan standar saja.", show_alert=True)
    except Exception as e:
        logger.error(f"Error admin_recheck_swap_callback {order_id}: {e}", exc_info=True)
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
        # Deposit yang sudah terkonfirmasi/sedang dibayar tidak boleh dibatalkan — koin user tertahan.
        changed = db.query(Order).filter(
            Order.order_id == order_id, Order.order_type == "swap",
            Order.status.in_(["WAITING_CRYPTO_DEPOSIT", "expired"]),
        ).update({"status": "CANCELLED"}, synchronize_session=False)
        db.commit()
        if changed != 1:
            await query.answer("⛔ Deposit convert sudah terkonfirmasi/diproses — tidak bisa ditolak. "
                               "Gunakan Approve atau proses manual.", show_alert=True)
            return
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
        await _finish_admin_action(query, "❌ <b>DITOLAK OLEH ADMIN</b>")
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
            tid = int(arg)
            if tid not in target_ids:  # ID ganda dikredit sekali saja
                target_ids.append(tid)
        except ValueError:
            await update.message.reply_text(f"❌ ID <code>{arg}</code> bukan angka valid.", parse_mode="HTML")
            return

    if not target_ids:
        await update.message.reply_text("❌ Tidak ada user ID yang diberikan.")
        return
    if len(target_ids) > 50:
        await update.message.reply_text("❌ Maksimal 50 user per bulk credit.")
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
    """Konfigurasi referral program (alternatif teks dari panel admin ➔ Kelola Referral).

    Format:
      /setreferral reward <Rp>      — reward pengundang, transaksi ke-1 teman
      /setreferral reward2 <Rp>     — reward pengundang, transaksi ke-2 teman
      /setreferral bonus <Rp>       — diskon fee transaksi pertama teman
      /setreferral share <persen>   — bagi hasil fee untuk pengundang
      /setreferral sharemax <n>     — maks. transaksi bagi hasil per teman
      /setreferral hold <jam>       — masa tahan reward pengundang
      /setreferral min <Rp>         — minimal nominal transaksi teman
      /setreferral max <n>          — batas teman per pengundang
      /setreferral enabled true|false
    """
    if not is_admin(update.effective_user.id):
        return

    if not context.args or len(context.args) < 2:
        lines = ["⚠️ <b>Format Pengaturan Referral:</b>\n"]
        from services.referral_rewards import get_referral_settings
        db = SessionLocal()
        try:
            cfg = get_referral_settings(db)
        finally:
            db.close()
        for name, meta in REFERRAL_ADMIN_SETTINGS.items():
            lines.append(
                f"• <code>/setreferral {name} [NILAI]</code> — {meta['title']} "
                f"(sekarang {format_ref_setting(name, cfg[name])})"
            )
        lines.append("• <code>/setreferral enabled true|false</code> — Aktifkan/nonaktifkan program\n")
        lines.append("<i>Atau gunakan menu interaktif di Dashboard Admin ➔ Kelola Referral.</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    key = context.args[0].lower()
    value = context.args[1]

    aliases = {
        "discount": "bonus", "potongan": "bonus", "persen": "share", "pct": "share",
        "tahan": "hold", "min": "min_trade", "minimal": "min_trade", "minorder": "min_trade",
        "max": "maxref",
    }
    name = aliases.get(key, key)

    from services.referral_rewards import SETTING_DEFS
    if name == "enabled":
        if value.lower() not in ("true", "false"):
            await update.message.reply_text("❌ Nilai harus 'true' atau 'false'.")
            return
        config_key, value = "referral_enabled", value.lower()
    elif name in REFERRAL_ADMIN_SETTINGS:
        ok, val = parse_ref_setting_value(name, value)
        if not ok:
            await update.message.reply_text(f"❌ {val}")
            return
        config_key = SETTING_DEFS[name][0]
        value = f"{val:g}" if isinstance(val, float) else str(val)
    else:
        await update.message.reply_text(
            f"❌ Key tidak valid: <code>{html.escape(key)}</code>.\n"
            "Gunakan salah satu: " + ", ".join(f"<code>{n}</code>" for n in REFERRAL_ADMIN_SETTINGS) +
            ", <code>enabled</code>",
            parse_mode="HTML"
        )
        return

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
        _expire_stale_reward_wizard(context)

        # 0a. Kirim Reward — daftar penerima + nominal (satu orang per baris)
        if context.user_data.get("admin_awaiting_reward_list"):
            from services import reward_service as rs
            rows, errors = rs.parse_reward_lines(raw_text)
            items, skipped = rs.resolve_recipients(db, rows)
            if not items:
                await update.message.reply_text(
                    "❌ <b>Belum ada penerima yang valid.</b>\n\n"
                    f"{_fmt_skipped(skipped, errors)}"
                    "Perbaiki lalu kirim ulang daftarnya, atau /cancel untuk batal.",
                    parse_mode="HTML",
                )
                return True
            batch = rs.create_batch(db, user_id, items)
            _arm_reward_wizard(context, "admin_awaiting_reward_msg")
            context.user_data["admin_reward_batch_id"] = batch.id
            context.user_data["admin_reward_skipped"] = _fmt_skipped(skipped, errors)
            await update.message.reply_text(
                f"✅ <b>{len(items)} penerima terbaca</b> · total <code>{format_idr(batch.total_amount)}</code>\n\n"
                f"{_fmt_skipped(skipped, errors)}"
                "💬 <b>Sekarang ketik pesan untuk para penerima</b> (boleh santai, tidak harus formal).\n"
                "Placeholder opsional: <code>{nama}</code> <code>{nominal}</code> <code>{saldo}</code>\n"
                "Info nominal &amp; saldo ditambahkan otomatis di bawah pesan.\n\n"
                "<i>Tidak mau menulis pesan? Tekan tombol di bawah.</i>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏭ Pakai Pesan Standar", callback_data=f"admin_reward_preview_{batch.id}")],
                    [InlineKeyboardButton("❌ Batalkan", callback_data=f"admin_reward_cancel_{batch.id}")],
                ]),
                parse_mode="HTML",
            )
            return True

        # 0b. Kirim Reward — pesan untuk penerima
        if context.user_data.get("admin_awaiting_reward_msg"):
            from services import reward_service as rs
            if len(raw_text) > rs.MAX_CUSTOM_MESSAGE_CHARS:
                await update.message.reply_text(
                    f"❌ Pesan terlalu panjang ({len(raw_text)} karakter, maks {rs.MAX_CUSTOM_MESSAGE_CHARS}). Persingkat lalu kirim ulang:")
                return True
            batch_id = context.user_data.get("admin_reward_batch_id")
            context.user_data.pop("admin_awaiting_reward_msg", None)
            if not batch_id or not rs.set_default_message(db, batch_id, user_id, raw_text):
                await update.message.reply_text("⚠️ Draft reward tidak ditemukan / sudah diproses. Mulai lagi dari menu Kirim Reward.")
                return True
            batch = db.query(RewardBatch).filter(RewardBatch.id == batch_id).first()
            text, markup = build_reward_preview_view(db, batch, context.user_data.get("admin_reward_skipped", ""))
            await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            return True

        # 0c. Pengecualian Top Milestone — daftar user (satu per baris, catatan opsional)
        if context.user_data.get("admin_awaiting_milestone_excl"):
            added, problems = [], []
            for raw in raw_text.splitlines():
                parts = raw.strip().split(None, 1)
                if not parts:
                    continue
                target, note = parts[0], (parts[1] if len(parts) > 1 else None)
                user = crud.get_user_by_identifier(db, target)
                if user:
                    t_id = user.telegram_id
                elif re.fullmatch(r"[0-9]{1,15}", target.lstrip("@")):
                    t_id = int(target.lstrip("@"))
                else:
                    problems.append(f"• <code>{_esc(target)}</code>: tidak ditemukan (belum pernah /start). Pakai ID numerik.")
                    continue
                created = crud.add_milestone_exclusion(db, t_id, note=note, created_by=user_id)
                added.append(f"• <code>{t_id}</code>{' (sudah ada, catatan diperbarui)' if not created else ''}")
            context.user_data.pop("admin_awaiting_milestone_excl", None)
            view, markup = build_milestone_exclusion_view(db)
            report = ""
            if added:
                report += "✅ <b>Dikecualikan:</b>\n" + "\n".join(added) + "\n\n"
            if problems:
                report += "⚠️ <b>Tidak diproses:</b>\n" + "\n".join(problems) + "\n\n"
            await update.message.reply_text(report + view, reply_markup=markup, parse_mode="HTML")
            return True

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
            text, markup = build_admin_send_balance_confirm_view(
                target_user, amount, db=db, admin_id=user_id,
                chat_id=_callback_chat_id(update),
            )
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
            text, markup = build_admin_treasury_view(
                db, admin_id=user_id, chat_id=_callback_chat_id(update),
            )
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
            text, markup = build_admin_treasury_view(
                db, admin_id=user_id, chat_id=_callback_chat_id(update),
            )
            await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            return True

        # 5. Bot Treasury — Topup QRIS Kustom (Uang Asli)
        if context.user_data.get("admin_awaiting_treasury_qris_custom"):
            clean_digits = "".join(ch for ch in raw_text if ch.isdigit())
            if not clean_digits or int(clean_digits) < 5000:
                await update.message.reply_text("❌ Minimal deposit QRIS adalah Rp 5.000. Silakan ketik angka kembali:")
                return True

            amount = int(clean_digits)
            if amount > 10_000_000:
                await update.message.reply_text("❌ Maksimal deposit QRIS adalah Rp 10.000.000 per transaksi (Limit BI). Silakan masukkan nominal yang lebih kecil:")
                return True

            context.user_data.pop("admin_awaiting_treasury_qris_custom", None)
            await generate_and_send_treasury_qris(update, context, amount)
            return True

        # 5. Referral Configurations (satu alur untuk semua ketentuan referral)
        ref_cfg_name = context.user_data.get("admin_awaiting_ref_cfg")
        if ref_cfg_name in REFERRAL_ADMIN_SETTINGS:
            ok, val = parse_ref_setting_value(ref_cfg_name, raw_text)
            if not ok:
                await update.message.reply_text(f"❌ {val} Silakan ketik ulang:")
                return True

            from services.referral_rewards import SETTING_DEFS
            crud.set_referral_config(db, SETTING_DEFS[ref_cfg_name][0], f"{val:g}" if isinstance(val, float) else str(val))
            context.user_data.pop("admin_awaiting_ref_cfg", None)
            meta = REFERRAL_ADMIN_SETTINGS[ref_cfg_name]
            await update.message.reply_text(
                f"✅ <b>{meta['title']} Diperbarui!</b>\n\n"
                f"{meta['icon']} Nilai baru: <b>{format_ref_setting(ref_cfg_name, val)}</b>",
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
            text, markup = build_admin_treasury_view(
                db, admin_id=user_id, chat_id=_callback_chat_id(update),
            )
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


