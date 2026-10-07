"""
Main Entry Point — P2P Crypto Trading Bot
==========================================
Runs the Telegram bot (long-polling) in a single asyncio event loop.

Startup sequence:
  1. Configure logging
  2. Create database tables (if not exist) & seed defaults
  3. Build the Telegram Application + register all handlers
  4. Pass bot reference to runtime module (for sending Telegram messages)
  5. Start APScheduler background jobs
  6. Run bot polling
  7. Handle graceful shutdown on SIGINT / SIGTERM
"""

import asyncio
import html
import logging
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

from telegram import BotCommand, Update
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ---------- Project imports ----------
from config.settings import settings
from database.connection import SessionLocal, engine, Base
from database.models import PriceConfig

# Bot handlers — created by other agents
from bot.handlers.start import start_handler, menu_callback_handler
from bot.handlers.buy import buy_conversation_handler
from bot.handlers.sell import sell_conversation_handler
from bot.handlers.swap import swap_conv_handler
from bot.handlers.calculator import calculator_conversation_handler
from bot.handlers.balance import topup_conversation_handler, show_balance_menu
from bot.handlers.price import show_prices
from bot.handlers.admin import (
    admin_handler,
    setspread_handler,
    orders_handler,
    confirm_handler,
    broadcast_handler,
    ban_handler,
    unban_handler,
    stats_handler,
    refreshwallet_handler,
    verifysell_handler,
    getemoji_handler,
    syncpack_handler,
    setemoji_handler,
    listemojis_handler,
    resetemojis_handler,
    check_api_command,
    credit_balance_handler,
    bulkcredit_handler,
    setreferral_handler,
)
from bot.handlers.referral import referral_menu_handler
from bot.handlers.admin_campaign import campaign_command_handler

# FastAPI app & webhook bridge
from services.bot_runtime import bot_app, set_bot_app

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s | %(name)-25s | %(levelname)-7s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# State fallback auto-detect topup via GET /transactions (anti klaim ganda & rate-limit)
_topup_last_transactions_fetch = 0.0
# [FIX KRITIS-4] Gunakan dict {tx_id: timestamp} bukan set, agar cleanup berbasis waktu
_topup_matched_tx_ids: dict = {}  # tx_id -> unix timestamp saat dipakai

# State verifikasi massal order Beli via GET /transactions
_buy_matched_tx_ids: dict = {}  # tx_id -> unix timestamp saat dipakai


def _cleanup_matched_tx_ids(tx_dict: dict, max_age_seconds: int = 86400) -> None:
    """Hapus entry tx_id yang sudah lebih dari max_age_seconds (default 24 jam).
    Lebih aman dari .clear() karena tidak menghapus tx_id yang baru saja diproses.
    """
    cutoff = time.time() - max_age_seconds
    expired_keys = [k for k, ts in tx_dict.items() if ts < cutoff]
    for k in expired_keys:
        del tx_dict[k]


# =============================================================
# 1. DATABASE INITIALISATION & SEEDING
# =============================================================
QRIS_EXPIRY_GRACE_SECONDS = 120


def init_database():
    """
    Create all SQLAlchemy tables (no-op if they already exist)
    and seed default data for fee_tiers and price_config.
    """
    import database.models  # Pastikan semua model ter-load ke Base.metadata
    import database.connection as db_conn
    logger.info("Creating database tables (if not exist)...")
    try:
        Base.metadata.create_all(bind=db_conn.engine)
    except Exception as ddl_err:
        logger.error(
            f"⚠️ [DATABASE] Gagal DDL create_all pada engine aktif: {ddl_err}. "
            "Melakukan fallback ke SQLite lokal..."
        )
        if db_conn.engine.url.drivername != "sqlite":
            db_conn.DB_DEGRADED = True  # transaksi user dinonaktifkan (maintenance_guard)
        db_conn.engine = db_conn._create_sqlite_engine("sqlite:///./p2p_bot.db")
        db_conn.SessionLocal.configure(bind=db_conn.engine)
        Base.metadata.create_all(bind=db_conn.engine)

    # Migrasi schema (SQLite/Postgres): users, wallet_balances, inventory, orders, campaigns
    _migrate_users_schema()
    _migrate_wallet_balance_schema()
    _migrate_inventory_schema()
    _migrate_orders_schema()
    _migrate_campaign_schema()
    _migrate_phase7_schema()   # Phase 7: Referral Discount, Loyalty, Enhanced Wallet

    db = db_conn.SessionLocal()
    try:
        _seed_price_configs(db)
        from database.crud import sync_gopay_session_file
        sync_gopay_session_file()
        db.commit()
        logger.info("Database initialisation complete")
    except Exception as exc:
        db.rollback()
        logger.error("Error seeding database: %s", exc, exc_info=True)
    finally:
        db.close()


def _migrate_users_schema():
    """
    Migrasi tabel users: tambah kolom is_banned, total_orders, total_spent_idr,
    dan balance_idr jika belum ada (kompatibel SQLite & PostgreSQL).
    """
    from sqlalchemy import inspect
    from database.connection import engine
    try:
        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        if "users" in table_names:
            existing_cols = {c["name"] for c in inspector.get_columns("users")}
            new_cols = [
                ("is_banned", "BOOLEAN DEFAULT FALSE"),
                ("total_orders", "INTEGER DEFAULT 0"),
                ("total_spent_idr", "BIGINT DEFAULT 0"),
                ("balance_idr", "NUMERIC(15, 2) DEFAULT 0.0"),
            ]
            with engine.begin() as conn:
                for col, dtype in new_cols:
                    if col not in existing_cols:
                        conn.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {col} {dtype}")
                        logger.info("Migrasi users: kolom %s ditambahkan.", col)
                conn.exec_driver_sql("UPDATE users SET is_banned = FALSE WHERE is_banned IS NULL")
    except Exception as exc:
        logger.error("Migrasi users gagal: %s", exc, exc_info=True)


def _migrate_campaign_schema():
    """
    Memastikan tabel campaigns dan campaign_distributions terbuat
    (kompatibel SQLite & PostgreSQL).
    """
    from sqlalchemy import inspect
    from database.connection import engine, Base
    import database.models
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        if "campaigns" not in table_names or "campaign_distributions" not in table_names:
            Base.metadata.create_all(bind=engine)
            logger.info("Migrasi campaign: tabel campaigns & campaign_distributions berhasil dibuat.")
    except Exception as exc:
        logger.error("Migrasi campaign gagal: %s", exc, exc_info=True)


def _migrate_phase7_schema():
    """
    Phase 7: Buat tabel baru dan tambah kolom ke tabel yang sudah ada.
    - Tabel baru: referral_discounts, loyalty_rewards, loyalty_config
    - Kolom baru di orders: referral_discount_applied, referral_discount_pct, discount_amount_idr
    - Kolom baru di user_saved_wallets: chain_type, is_default, auto_detected
    - Seed default loyalty_config rows
    """
    from sqlalchemy import inspect
    from database.connection import engine, Base
    import database.models  # noqa: F401 — ensure models are loaded

    try:
        # Buat tabel baru yang belum ada (idempotent via create_all)
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)

        # ── Kolom baru di tabel orders ──────────────────────────────
        if "orders" in inspector.get_table_names():
            existing_order_cols = {c["name"] for c in inspector.get_columns("orders")}
            new_order_cols = [
                ("referral_discount_applied", "BOOLEAN DEFAULT FALSE"),
                ("referral_discount_pct", "NUMERIC(5, 2)"),
                ("discount_amount_idr", "BIGINT DEFAULT 0"),
            ]
            with engine.begin() as conn:
                for col, dtype in new_order_cols:
                    if col not in existing_order_cols:
                        conn.exec_driver_sql(
                            f"ALTER TABLE orders ADD COLUMN {col} {dtype}"
                        )
                        logger.info("Phase7 migration: orders.%s ditambahkan.", col)

        # ── Kolom baru di tabel user_saved_wallets ──────────────────
        if "user_saved_wallets" in inspector.get_table_names():
            existing_wallet_cols = {c["name"] for c in inspector.get_columns("user_saved_wallets")}
            new_wallet_cols = [
                ("chain_type", "VARCHAR(20)"),
                ("is_default", "BOOLEAN DEFAULT FALSE"),
                ("auto_detected", "BOOLEAN DEFAULT FALSE"),
            ]
            with engine.begin() as conn:
                for col, dtype in new_wallet_cols:
                    if col not in existing_wallet_cols:
                        conn.exec_driver_sql(
                            f"ALTER TABLE user_saved_wallets ADD COLUMN {col} {dtype}"
                        )
                        logger.info("Phase7 migration: user_saved_wallets.%s ditambahkan.", col)

        # ── Seed default loyalty_config ─────────────────────────────
        from database.connection import SessionLocal
        from database.models import LoyaltyConfig
        DEFAULT_LOYALTY = {
            "loyalty_enabled": "true",
            "window_days": "5",
            "min_tx_count": "5",
            "reward_amount_idr": "25000",
            "min_tx_amount_idr": "50000",
        }
        _db = SessionLocal()
        try:
            for key, val in DEFAULT_LOYALTY.items():
                exists = _db.query(LoyaltyConfig).filter(LoyaltyConfig.key == key).first()
                if not exists:
                    _db.add(LoyaltyConfig(key=key, value=val))
            _db.commit()
            logger.info("Phase7 migration: loyalty_config defaults selesai di-seed.")
        except Exception as seed_err:
            _db.rollback()
            logger.warning("Phase7 migration: seed loyalty_config gagal: %s", seed_err)
        finally:
            _db.close()

        logger.info("Phase 7 schema migration selesai.")
    except Exception as exc:
        logger.error("Phase 7 migration gagal: %s", exc, exc_info=True)



def _migrate_orders_schema():
    """
    Migrasi tabel orders & topup_orders: tambah kolom payment_method & unique_code
    jika belum ada (kompatibel SQLite & PostgreSQL).
    """
    from sqlalchemy import inspect

    from database.connection import engine
    try:
        inspector = inspect(engine)
        table_names = inspector.get_table_names()

        if "orders" in table_names:
            existing_orders = {c["name"] for c in inspector.get_columns("orders")}
            new_columns_orders = [
                ("payment_method", "VARCHAR(30)"),
                ("unique_code", "INTEGER DEFAULT 0"),
                ("deposit_proof_file_id", "VARCHAR(500)"),
                ("mdr_idr", "BIGINT DEFAULT 0"),  # Pajak QRIS 0,3%
            ]
            with engine.begin() as conn:
                for col, dtype in new_columns_orders:
                    if col not in existing_orders:
                        conn.exec_driver_sql(f"ALTER TABLE orders ADD COLUMN {col} {dtype}")
                        logger.info("Migrasi orders: kolom %s ditambahkan.", col)

        if "topup_orders" in table_names:
            existing_topups = {c["name"] for c in inspector.get_columns("topup_orders")}
            if "unique_code" not in existing_topups:
                with engine.begin() as conn:
                    conn.exec_driver_sql("ALTER TABLE topup_orders ADD COLUMN unique_code INTEGER DEFAULT 0")
                    logger.info("Migrasi topup_orders: kolom unique_code ditambahkan.")
            if "mdr_idr" not in existing_topups:
                with engine.begin() as conn:
                    conn.exec_driver_sql("ALTER TABLE topup_orders ADD COLUMN mdr_idr BIGINT DEFAULT 0")
                    logger.info("Migrasi topup_orders: kolom mdr_idr ditambahkan.")
    except Exception as exc:
        logger.error("Migrasi orders gagal: %s", exc, exc_info=True)


def _migrate_inventory_schema():
    """Tambah kolom reservation dan kesehatan sinkronisasi wallet."""
    from sqlalchemy import inspect
    from database.connection import engine
    try:
        inspector = inspect(engine)
        if "wallet_balances" in inspector.get_table_names():
            columns = {c["name"] for c in inspector.get_columns("wallet_balances")}
            new_columns = [
                ("reserved_balance", "NUMERIC(36, 18) DEFAULT 0"),
                ("sync_status", "VARCHAR(20) DEFAULT 'UNKNOWN' NOT NULL"),
                ("last_error", "VARCHAR(500)"),
                ("last_checked_at", "TIMESTAMP"),
                ("last_success_at", "TIMESTAMP"),
            ]
            with engine.begin() as conn:
                for name, definition in new_columns:
                    if name not in columns:
                        conn.exec_driver_sql(
                            f"ALTER TABLE wallet_balances ADD COLUMN {name} {definition}"
                        )
                        logger.info("Migrasi wallet_balances: kolom %s ditambahkan.", name)
                conn.exec_driver_sql(
                    "UPDATE wallet_balances SET reserved_balance = 0 WHERE reserved_balance IS NULL"
                )
                conn.exec_driver_sql(
                    "UPDATE wallet_balances SET sync_status = 'UNKNOWN' "
                    "WHERE sync_status IS NULL OR sync_status = ''"
                )
    except Exception as exc:
        logger.error("Migrasi inventory gagal: %s", exc, exc_info=True)


def _migrate_wallet_balance_schema():
    """
    Migrasi tabel wallet_balances (khusus SQLite legacy) agar unik per pasangan
    (network, symbol), bukan per network saja.
    Jika bukan SQLite atau sudah sesuai, fungsi ini menjadi no-op.
    """
    from database.connection import engine
    if engine.dialect.name != "sqlite":
        return
    try:
        with engine.begin() as conn:
            index_rows = conn.exec_driver_sql(
                "PRAGMA index_list('wallet_balances')"
            ).fetchall()

            unique_cols = set()
            for idx in index_rows:
                name, is_unique = idx[1], idx[2]
                if not is_unique:
                    continue
                info = conn.exec_driver_sql(
                    f"PRAGMA index_info('{name}')"
                ).fetchall()
                cols = {r[2] for r in info}
                if cols:
                    unique_cols = cols
                    break

            if unique_cols == {"network"}:
                logger.info(
                    "Migrasi wallet_balances: unique (network) -> (network, symbol) ..."
                )
                conn.exec_driver_sql(
                    """
                    CREATE TABLE wallet_balances_new (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        network VARCHAR(30) NOT NULL,
                        symbol VARCHAR(20) NOT NULL,
                        balance NUMERIC(36, 18) DEFAULT 0.0,
                        address VARCHAR(200) NOT NULL,
                        updated_at DATETIME,
                        CONSTRAINT uq_wallet_network_symbol UNIQUE (network, symbol)
                    )
                    """
                )
                conn.exec_driver_sql(
                    """
                    INSERT INTO wallet_balances_new (network, symbol, balance, address, updated_at)
                    SELECT network, symbol, balance, address, updated_at FROM wallet_balances
                    """
                )
                conn.exec_driver_sql("DROP TABLE wallet_balances")
                conn.exec_driver_sql(
                    "ALTER TABLE wallet_balances_new RENAME TO wallet_balances"
                )
                logger.info("Migrasi wallet_balances selesai.")
            else:
                logger.info("Schema wallet_balances sudah sesuai (network, symbol).")
    except Exception as exc:
        logger.error("Migrasi wallet_balances gagal: %s", exc, exc_info=True)


def _seed_price_configs(db):
    """
    Insert default price configs jika tabel kosong.
    Jika DEFAULT_SPREAD_PCT diset 0.0 (keputusan client: 0% spread / pure harga real time market),
    sinkronkan semua config agar tidak ada markup/markdown tersembunyi.
    """
    target_spread = float(settings.DEFAULT_SPREAD_PCT)
    if db.query(PriceConfig).count() == 0:
        symbols = [
            "USDT", "USDC", "ETH", "SOL",
            "TRX", "BNB", "SUI", "TON",
            "POL", "MATIC", "ARB", "AVAX",
            "KAIA", "BERA", "APT", "HYPE",
            "USDG",
        ]
        configs = [
            PriceConfig(symbol=sym, spread_pct=target_spread, is_active=True)
            for sym in symbols
        ]
        db.add_all(configs)
        logger.info("Seeded %d price configs with spread %.2f%%", len(configs), target_spread)
    elif target_spread == 0.0:
        # Reset baris lama yang masih menyimpan spread > 0
        updated = db.query(PriceConfig).filter(PriceConfig.spread_pct > 0).update({PriceConfig.spread_pct: 0.0})
        if updated:
            logger.info("Sinkronisasi spread 0%%: %d koin diupdate ke 0.0%% (pure harga pasar realtime)", updated)


# =============================================================
# 2. TELEGRAM BOT SETUP
# =============================================================
BOT_COMMAND_MENU = [
    ("start", "Memulai bot"),
    ("cancel", "Membatalkan transaksi"),
]


async def set_bot_commands(application: Application) -> None:
    """Daftarkan menu command (tombol Menu di Telegram) — idempotent tiap startup."""
    import asyncio
    from database import connection as db_conn
    if db_conn.DB_DEGRADED:
        from bot.utils.telegram_utils import notify_admins
        try:
            await notify_admins(
                application.bot,
                "🚨 <b>DATABASE UTAMA TIDAK TERJANGKAU</b>\n\n"
                "Bot berjalan di SQLite sementara dalam <b>MODE DARURAT</b>: semua transaksi user "
                "ditolak. Periksa DATABASE_URL / layanan Postgres lalu restart bot.",
                kind="error", butuh_tindakan=True)
        except Exception as exc:
            logger.error("Gagal kirim peringatan mode darurat DB: %s", exc)
    for attempt in (1, 2, 3):
        try:
            await application.bot.set_my_commands(
                [BotCommand(cmd, desc) for cmd, desc in BOT_COMMAND_MENU]
            )
            logger.info("Menu command Telegram terdaftar: %s", [c for c, _ in BOT_COMMAND_MENU])
            return
        except Exception as exc:
            logger.warning("Gagal mendaftarkan menu command (percobaan %d): %s", attempt, exc)
            await asyncio.sleep(5)


def build_bot_application() -> Application:
    """
    Build the python-telegram-bot Application and register
    all command handlers, conversation handlers, and the
    global error handler.
    """
    logger.info("Building Telegram bot application...")

    # Konfigurasi request HTTPX yang tangguh untuk long-polling & auto-reconnect
    trequest = HTTPXRequest(
        connection_pool_size=100,
        read_timeout=30.0,
        write_timeout=20.0,
        connect_timeout=15.0,
        pool_timeout=15.0,
    )

    application = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .request(trequest)
        .post_init(set_bot_commands)
        .build()
    )

    # --- Gerbang user banned (sebelum semua handler) ---
    from telegram.ext import TypeHandler
    from bot.utils.ban_guard import ban_gate
    from bot.utils.maintenance_guard import maintenance_gate
    application.add_handler(TypeHandler(Update, maintenance_gate), group=-2)
    application.add_handler(TypeHandler(Update, ban_gate), group=-1)

    # --- Command Handlers ---
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("price", show_prices))
    application.add_handler(CommandHandler("harga", show_prices))
    application.add_handler(CommandHandler("balance", show_balance_menu))
    application.add_handler(CommandHandler("admin", admin_handler))
    application.add_handler(CommandHandler("setspread", setspread_handler))
    application.add_handler(CommandHandler("orders", orders_handler))
    from bot.handlers.admin import sellorders_handler
    application.add_handler(CommandHandler("sellorders", sellorders_handler))
    application.add_handler(CommandHandler("confirm", confirm_handler))
    application.add_handler(CommandHandler("broadcast", broadcast_handler))
    # Handler siaran foto / dokumen gambar dengan caption diawali /broadcast (agar tidak tertangkap oleh router bukti transfer)
    application.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE) & filters.CaptionRegex(re.compile(r"^/broadcast(\s|@|$)", re.IGNORECASE)),
            broadcast_handler,
        )
    )
    application.add_handler(CommandHandler("ban", ban_handler))
    application.add_handler(CommandHandler("unban", unban_handler))
    application.add_handler(CommandHandler("stats", stats_handler))
    application.add_handler(CommandHandler("refreshwallet", refreshwallet_handler))
    application.add_handler(CommandHandler("verifysell", verifysell_handler))
    from bot.handlers.admin import settarget_handler, unsettarget_handler, targets_handler, chatid_handler
    application.add_handler(CommandHandler("settarget", settarget_handler))
    application.add_handler(CommandHandler("unsettarget", unsettarget_handler))
    application.add_handler(CommandHandler("targets", targets_handler))
    application.add_handler(CommandHandler("chatid", chatid_handler))
    application.add_handler(CommandHandler("getemoji", getemoji_handler))
    application.add_handler(CommandHandler("syncpack", syncpack_handler))
    application.add_handler(CommandHandler("setemoji", setemoji_handler))
    application.add_handler(CommandHandler("listemojis", listemojis_handler))
    application.add_handler(CommandHandler("resetemojis", resetemojis_handler))
    application.add_handler(CommandHandler("checkapi", check_api_command))
    application.add_handler(CommandHandler("cekurl", check_api_command))
    application.add_handler(CommandHandler(["credit", "sendsaldo", "kirimsaldo"], credit_balance_handler))
    application.add_handler(CommandHandler("bulkcredit", bulkcredit_handler))
    application.add_handler(CommandHandler("campaign", campaign_command_handler))
    application.add_handler(CommandHandler("giveaway", campaign_command_handler))
    application.add_handler(CommandHandler("setreferral", setreferral_handler))
    application.add_handler(CommandHandler("referral", referral_menu_handler))
    from bot.handlers.saved_accounts import show_saved_wallets_menu, show_saved_banks_menu
    application.add_handler(CommandHandler(["wallet", "dompet"], show_saved_wallets_menu))
    from bot.handlers.admin import (
        weekly_report_command_handler,
        test_testimony_command_handler,
        topup_bot_command_handler,
        topup_qris_command_handler,
        resend_testimony_command_handler,
        resend_recent_testimonies_command_handler,
    )
    application.add_handler(CommandHandler(["weeklyreport", "report"], weekly_report_command_handler))
    application.add_handler(CommandHandler(["testtesti", "testchannel"], test_testimony_command_handler))
    application.add_handler(CommandHandler(["posttesti", "resendtesti"], resend_testimony_command_handler))
    application.add_handler(CommandHandler(["postlasttesti", "resendlasttesti"], resend_recent_testimonies_command_handler))
    application.add_handler(CommandHandler(["topupbot", "saldobot", "dompetbot"], topup_bot_command_handler))
    application.add_handler(CommandHandler(["topupqris", "qriskas", "topupkas"], topup_qris_command_handler))

    # --- Conversation Handlers (multi-step flows) ---
    # ConversationHandlers have higher priority than standalone commands
    # so they intercept messages during an active conversation.
    application.add_handler(topup_conversation_handler)
    application.add_handler(buy_conversation_handler)
    application.add_handler(sell_conversation_handler)
    application.add_handler(swap_conv_handler)
    from bot.handlers.deposit_hash import txhash_conversation_handler
    application.add_handler(txhash_conversation_handler)
    application.add_handler(calculator_conversation_handler)

    # /cancel saat tidak ada flow aktif — flow aktif sudah ditangkap fallback di atas.
    from bot.handlers.start import idle_cancel_handler
    application.add_handler(CommandHandler("cancel", idle_cancel_handler))

    # --- Bukti Transfer QRIS (foto) ---
    # Satu router: foto diarahkan ke alur Buy ATAU Topup (hindari forward ganda ke admin).
    application.add_handler(MessageHandler(filters.PHOTO, _route_transfer_proof))

    # --- Admin & Interactive User Input (Text) ---
    # Menangani input teks user (simpan wallet/rekening) dan admin (kirim saldo, kas bot, campaign, referral)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _route_admin_text))

    # --- Callback Query Handler (catch-all for inline keyboard buttons) ---
    # Handles menu_* callbacks and any other inline-button presses.
    application.add_handler(CallbackQueryHandler(menu_callback_handler))

    # --- Global Error Handler ---
    application.add_error_handler(error_handler)

    logger.info("All handlers registered")
    return application


async def _route_admin_text(update: Update, context) -> None:
    """
    Router pesan teks interaktif untuk input user (simpan wallet/rekening)
    dan input wizard admin (kirim saldo, kas bot, nominal budget campaign, notifikasi, referral).
    """
    if not update.message or not update.message.text:
        return

    # Routing input user untuk simpan wallet atau simpan rekening
    if context.user_data.get("awaiting_save_wallet") or context.user_data.get("awaiting_save_bank"):
        from bot.handlers.saved_accounts import handle_saved_account_text_input
        handled = await handle_saved_account_text_input(update, context)
        if handled:
            return

    if context.user_data.get("awaiting_withdraw_amount"):
        from bot.handlers.withdraw import handle_withdraw_amount_text
        if await handle_withdraw_amount_text(update, context):
            return

    user_id = update.effective_user.id
    from bot.handlers.admin import is_admin
    if not is_admin(user_id):
        return

    # Routing seluruh input interaktif admin (kirim saldo user, top up kas bot, config referral)
    from bot.handlers.admin import admin_interactive_text_router
    handled = await admin_interactive_text_router(update, context)
    if handled:
        return

    # Fallback ke wizard campaign / giveaway
    from bot.handlers.admin_campaign import campaign_text_input_handler
    await campaign_text_input_handler(update, context)


async def _route_transfer_proof(update: Update, context) -> None:
    """
    Router tunggal untuk foto bukti transfer.
    Prioritas: order Beli GoPay pending -> alur buy; jika tidak ada, topup pending -> alur topup.
    Mencegah foto yang sama diforward ke admin dua kali (buy + topup).
    """
    from database.crud import get_pending_gopay_order_for_user, get_pending_topup_orders
    from bot.handlers.buy import handle_transfer_proof
    from bot.handlers.balance import handle_topup_transfer_proof

    user_id = update.effective_user.id
    # Guard: jangan pernah tangani pesan /broadcast di router bukti transfer
    caption = (update.message.caption or "").strip() if update.message else ""
    if caption.lower().startswith("/broadcast"):
        return

    # Cek jika admin sedang mengirimkan foto bukti transfer untuk order Sell
    if context.user_data.get("admin_awaiting_proof_order_id"):
        from bot.handlers.admin import handle_admin_upload_proof, is_admin
        if is_admin(user_id):
            await handle_admin_upload_proof(update, context)
            return

    db = SessionLocal()
    try:
        if get_pending_gopay_order_for_user(db, user_id):
            await handle_transfer_proof(update, context)
            return
        if any(t.telegram_id == user_id for t in get_pending_topup_orders(db)):
            await handle_topup_transfer_proof(update, context)
    finally:
        db.close()


async def error_handler(update: object, context) -> None:
    """
    Global error handler for the Telegram bot.
    Logs the error and notifies the user + all admins.
    """
    from telegram.error import NetworkError, TimedOut, RetryAfter

    err = context.error
    # Abaikan error jaringan sementara (ReadError/Timeout) yang otomatis di-retry oleh updater
    if isinstance(err, (NetworkError, TimedOut, RetryAfter)):
        logger.warning("Transient Telegram NetworkError (auto-reconnect): %s", err)
        return

    logger.error("Unhandled exception: %s", err, exc_info=err)

    # Notify user (if we know who they are)
    if update and isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Terjadi kesalahan. Silakan coba lagi atau hubungi admin.",
            )
        except Exception:
            pass  # Don't let the error handler itself crash

    # Notify all admins
    from bot.utils.telegram_utils import notify_admins
    try:
        await notify_admins(
            context.bot,
            kind="error", butuh_tindakan=True,
            text=f"🚨 <b>Bot Error</b>\n\n<code>{html.escape(str(context.error)[:3500])}</code>",
        )
    except Exception:
        pass


# =============================================================
# 3. APSCHEDULER BACKGROUND JOBS
# =============================================================
def setup_scheduler():
    """
    Configure APScheduler background jobs:
      - Price refresh every 30 seconds
      - Order expiry check every 1 minute
      - Wallet balance sync every 5 minutes
      - Low balance alert every 15 minutes
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()

    # --- Price Refresh (every 30s) ---
    scheduler.add_job(
        _job_refresh_prices,
        "interval",
        seconds=30,
        id="price_refresh",
        name="Refresh crypto prices from Binance",
        next_run_time=datetime.now(timezone.utc),  # run immediately on startup
        max_instances=1,
        coalesce=True,
    )

    # --- Deposit Detector Job (every 20s) ---
    scheduler.add_job(
        _job_monitor_deposits,
        "interval",
        seconds=20,
        id="deposit_detector",
        name="Monitor on-chain incoming deposits",
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )

    # --- Payout Watchdog (every 60s) ---
    # Order yang sudah ter-broadcast tapi receipt-nya telat tidak boleh
    # nyangkut manual_review tanpa penyelesaian otomatis.
    scheduler.add_job(
        _job_reconcile_payouts,
        "interval",
        seconds=60,
        id="payout_watchdog",
        name="Reconcile broadcasted payouts without receipt",
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )

    # --- QRIS Topup Polling Job (every 20s) ---
    scheduler.add_job(
        _job_check_pending_topups,
        "interval",
        seconds=20,
        id="topup_polling",
        name="Poll GoPay API gateway for pending QRIS topup payments",
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )

    # --- GoPay Buy QRIS Polling Job (every 20s) ---
    scheduler.add_job(
        _job_check_pending_buy_payments,
        "interval",
        seconds=20,
        id="gopay_buy_polling",
        name="Poll GoPay QRIS pending buy orders and auto-send crypto",
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )

    # --- Order Expiry Check (every 1 min) ---
    scheduler.add_job(
        _job_expire_orders,
        "interval",
        minutes=1,
        id="order_expiry",
        name="Expire pending orders older than ORDER_EXPIRE_MINUTES",
        max_instances=1,
        coalesce=True,
    )

    # --- Coin API & RPC Health Monitor (every 5 min) ---
    scheduler.add_job(
        _job_check_coin_apis,
        "interval",
        minutes=5,
        id="coin_api_health_monitor",
        name="Monitor Coin APIs and RPC Endpoints",
        max_instances=1,
        coalesce=True,
    )

    # --- Wallet Balance Sync (every 5 min) ---
    scheduler.add_job(
        _job_sync_wallet_balances,
        "interval",
        minutes=5,
        id="wallet_sync",
        name="Sync on-chain wallet balances",
        max_instances=1,
        coalesce=True,
    )

    # --- Low Balance Alert (configurable, default every 6 hours) ---
    if getattr(settings, "ENABLE_LOW_BALANCE_ALERT", False):
        scheduler.add_job(
            _job_low_balance_alert,
            "interval",
            hours=int(getattr(settings, "LOW_BALANCE_ALERT_HOURS", 6)),
            id="low_balance_alert",
            name="Alert admins if wallet balance is low",
            max_instances=1,
            coalesce=True,
        )

    # --- Monthly Financial Report (tanggal 1, 00:05 WIB, untuk bulan yang baru selesai) ---
    scheduler.add_job(
        _job_send_monthly_report,
        _monthly_report_trigger(),
        id="monthly_report",
        name="Send monthly financial report to admins",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()

    logger.info("APScheduler started with background jobs")
    return scheduler


async def _job_refresh_prices():
    """Fetch latest prices from Binance and update the in-memory cache."""
    try:
        from services.price_service import price_service
        await price_service.refresh_all_prices()
        logger.debug("Prices refreshed successfully")
    except Exception as exc:
        logger.error("Price refresh job failed: %s", exc, exc_info=True)


async def _job_monitor_deposits():
    """Monitor incoming deposits across 16 networks."""
    try:
        from services.detector import deposit_detector
        from services.bot_runtime import bot_app
        await deposit_detector.scan_incoming_deposits(bot_app=bot_app)
    except Exception as exc:
        logger.error("Deposit detector job failed: %s", exc, exc_info=True)


async def _job_reconcile_payouts():
    """Watchdog payout: selesaikan order broadcast yang receipt-nya telat."""
    try:
        from services.payout_watchdog import reconcile_broadcasted_payouts
        from services.bot_runtime import bot_app
        jumlah = await reconcile_broadcasted_payouts(bot=bot_app)
        if jumlah:
            logger.info("Payout watchdog: %s order direkonsiliasi COMPLETED", jumlah)
    except Exception as exc:
        logger.error("Payout watchdog job failed: %s", exc, exc_info=True)


async def _job_expire_orders():
    """Mark PENDING orders older than ORDER_EXPIRE_MINUTES as EXPIRED."""
    try:
        from database.crud import expire_stale_orders
        from database.models import Order
        from services.gopay_service import gopay_service
        from bot.handlers.buy import finalize_gopay_buy_payment

        db = SessionLocal()
        try:
            # Cek sekali lagi sebelum expire: order GOPAY_QRIS yang user-nya sudah
            # transfer tapi gateway gagal deteksi (mis. sesi GoPay mati) tidak boleh expire.
            cutoff = datetime.utcnow() - timedelta(minutes=settings.ORDER_EXPIRE_MINUTES)
            stale_gopay = (
                db.query(Order)
                .filter(
                    Order.status == "pending",
                    Order.created_at <= cutoff,
                    Order.payment_method == "GOPAY_QRIS",
                )
                .all()
            )
            gateway_unknown_order_ids = set()
            for order in stale_gopay:
                try:
                    payment_state = await gopay_service.confirm_payment(
                        db, amount=int(order.total_idr), ref_id=order.order_id,
                        kind="buy", created_at=order.created_at,
                    )
                    if payment_state is True:
                        # [FIX MEDIUM-1] Pass bot_app agar notifikasi Telegram terkirim
                        from services.bot_runtime import bot_app as _bot_app
                        await finalize_gopay_buy_payment(
                            db, order, bot=_bot_app, allow_expired_payment=True,
                        )
                    elif payment_state is None:
                        gateway_unknown_order_ids.add(order.order_id)
                    elif datetime.utcnow() <= (
                        order.created_at
                        + timedelta(minutes=settings.ORDER_EXPIRE_MINUTES)
                        + timedelta(seconds=QRIS_EXPIRY_GRACE_SECONDS)
                    ):
                        gateway_unknown_order_ids.add(order.order_id)
                except Exception as exc:
                    logger.warning("Final check order %s gagal: %s", order.order_id, exc)
                    gateway_unknown_order_ids.add(order.order_id)

            expired_count = expire_stale_orders(
                db,
                minutes=settings.ORDER_EXPIRE_MINUTES,
                exclude_order_ids=gateway_unknown_order_ids,
            )
            if expired_count > 0:
                logger.info("Expired %d stale orders", expired_count)
        finally:
            db.close()
    except Exception as exc:
        logger.error("Order expiry job failed: %s", exc, exc_info=True)


async def _job_check_coin_apis():
    """Memeriksa kesehatan seluruh URL/API koin dan mengirim alarm ke admin jika ada yang down/berubah."""
    try:
        from services.coin_api_monitor import coin_api_monitor
        from services.bot_runtime import bot_app
        await coin_api_monitor.check_all_and_alert(bot_app)
    except Exception as exc:
        logger.error("Job pemantau API koin gagal: %s", exc, exc_info=True)


async def _job_sync_wallet_balances():
    """Query on-chain balances for all (network, symbol) pairs and update wallet_balances table."""
    from services.wallet_sync import sync_wallet_balances
    from database.crud import sync_gopay_session_file
    await sync_wallet_balances()
    sync_gopay_session_file()


async def _job_low_balance_alert():
    """Check wallet balances and alert admins if any are below threshold."""
    try:
        from database.crud import get_low_balance_wallets

        db = SessionLocal()
        try:
            low_wallets = get_low_balance_wallets(db)
            if low_wallets:
                from services.bot_runtime import bot_app
                from bot.utils.telegram_utils import notify_admins
                if bot_app:
                    msg_lines = ["⚠️ <b>Low Balance Alert</b>\n"]
                    for wallet in low_wallets:
                        try:
                            val = float(wallet.balance or 0)
                            formatted_val = f"{val:.4f}".rstrip("0").rstrip(".") if val != 0 else "0"
                        except Exception:
                            formatted_val = str(wallet.balance)
                        msg_lines.append(
                            f"• {wallet.network} ({wallet.symbol}): {formatted_val}"
                        )
                    alert_msg = "\n".join(msg_lines)

                    await notify_admins(bot_app, alert_msg, kind="topup")
        finally:
            db.close()
    except Exception as exc:
        logger.error("Low balance alert job failed: %s", exc, exc_info=True)



async def _complete_topup(db, topup):
    """Tandai topup SUCCESS (atomic claim), credit saldo user atau kas bot, dan kirim notifikasi Telegram."""
    from services.bot_runtime import bot_app
    from database.crud import claim_topup_success, credit_user_balance, topup_bot_treasury
    from bot.utils.formatter import format_idr
    from bot.utils.emojis import tg_emoji

    if not claim_topup_success(db, topup.topup_id, allow_expired=True):
        return

    topup_mdr = int(topup.mdr_idr or 0)
    net_amt = topup.amount_idr - topup_mdr

    if str(topup.topup_id).startswith("TREASURY-") or str(topup.topup_id).startswith("TOPUP-TREASURY-"):
        new_treasury_bal = topup_bot_treasury(db, net_amt, admin_id=topup.telegram_id, note=f"QRIS Topup {topup.topup_id}")
        if bot_app:
            try:
                msg = (
                    f"{tg_emoji('BANK', '🏦')} ✅ <b>PEMBAYARAN QRIS KAS BOT TERVERIFIKASI (OTOMATIS)!</b>\n\n"
                    f"🎉 Top up kas bot sebesar <b>{format_idr(net_amt)}</b> telah berhasil masuk!\n"
                    f"💰 <b>Total Saldo Kas Bot Sekarang:</b> <b>{format_idr(new_treasury_bal)}</b>\n\n"
                    f"<i>Saldo siap digunakan untuk alokasi campaign, giveaway, dan reward loyalitas.</i>"
                )
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                menu_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎁 Buka Wizard Campaign", callback_data="admin_panel_campaign")],
                    [InlineKeyboardButton("🏦 Dompet & Kas Bot", callback_data="camp_treasury_view")],
                    [InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main")],
                ])
                await bot_app.bot.send_message(
                    chat_id=topup.telegram_id,
                    text=msg,
                    reply_markup=menu_keyboard,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Gagal kirim notifikasi treasury topup ke admin {topup.telegram_id}: {e}")
        return

    new_bal = credit_user_balance(db, topup.telegram_id, net_amt)

    if bot_app:
        try:
            msg = (
                f"✅ <b>PEMBAYARAN QRIS TERVERIFIKASI (OTOMATIS)!</b>\n\n"
                f"🎉 Topup saldo sebesar <b>{format_idr(net_amt)}</b> telah berhasil!\n"
                f"💳 <b>Total Saldo Bot Anda Saat Ini</b>: <b>{format_idr(int(new_bal))}</b>\n\n"
                f"<i>Anda dapat langsung menggunakan saldo ini untuk membeli koin crypto secara instan.</i>"
            )
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")]])
            await bot_app.bot.send_message(
                chat_id=topup.telegram_id,
                text=msg,
                reply_markup=menu_keyboard,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Gagal kirim notifikasi topup ke user {topup.telegram_id}: {e}")


def _txn_id(txn: dict) -> str:
    """ID transaksi GoPay — sama dengan kunci klaim gateway (tx.id || order_id)."""
    return str(txn.get("transaction_id") or txn.get("id") or txn.get("order_id") or "")


def _match_transaction(txn: dict, amount: int, created_at, used_ids: set) -> bool:
    """Cocokkan satu transaksi riwayat mutasi dengan order/topup PENDING (nominal + waktu)."""
    tx_id = _txn_id(txn)
    if tx_id and tx_id in used_ids:
        return False

    try:
        if int(txn.get("amount")) != int(amount):
            return False
    except (TypeError, ValueError):
        return False

    # Refund / partial refund bukan pembayaran masuk.
    if "refund" in str(txn.get("status") or "").lower():
        return False

    # Transaksi harus terjadi SETELAH order/topup dibuat (toleransi jam 60 detik).
    # Tanpa waktu yang valid transaksi ditolak: lookback gateway 24 jam berarti
    # pembayaran lama bernominal sama bisa melunasi order baru.
    from services.gopay_service import payment_window_start, _to_utc_naive
    t_time = txn.get("transaction_time") or txn.get("time") or txn.get("created_at") or ""
    if not created_at or not t_time:
        return False
    try:
        tx_dt = _to_utc_naive(datetime.fromisoformat(str(t_time).replace("Z", "+00:00")))
    except ValueError:
        return False
    return tx_dt >= payment_window_start(created_at)


def _match_transaction_for_topup(txn: dict, topup, used_ids: set) -> bool:
    """Cocokkan satu transaksi riwayat mutasi dengan topup PENDING."""
    return _match_transaction(txn, int(topup.amount_idr), topup.created_at, used_ids)


async def _job_check_pending_topups():
    """Poll GoPay API gateway for pending QRIS topup orders and auto-credit balances."""
    global _topup_last_transactions_fetch, _topup_matched_tx_ids
    from services.gopay_service import gopay_service
    from database.crud import get_pending_topup_orders, expire_topup_if_pending, claim_qris_payment

    db = SessionLocal()
    try:
        pending_topups = get_pending_topup_orders(db)
        if not pending_topups:
            return

        # Cek paralel (max 10) — satu tick tidak boleh antri N x timeout gateway.
        sem = asyncio.Semaphore(10)

        async def _check(topup):
            async with sem:
                # Cek pembayaran DULU, baru expire: user yang bayar di menit
                # terakhir tetap dikredit (dulu di-expire sebelum dicek).
                payment_state = await gopay_service.confirm_payment(
                    db, amount=topup.amount_idr, ref_id=topup.topup_id,
                    kind="topup", created_at=topup.created_at,
                )
                if payment_state is True:
                    await _complete_topup(db, topup)
                    return None
                if payment_state is None:
                    return topup
                if topup.expires_at and datetime.utcnow() > (
                    topup.expires_at + timedelta(seconds=QRIS_EXPIRY_GRACE_SECONDS)
                ):
                    expire_topup_if_pending(db, topup.topup_id)
                    return None
                return topup

        results = await asyncio.gather(*(_check(t) for t in pending_topups))
        unmatched = [t for t in results if t]

        # Fallback auto-detect via GET /transactions (throttle 60s, anti rate-limit)
        if unmatched and (time.time() - _topup_last_transactions_fetch) >= 60:
            _topup_last_transactions_fetch = time.time()
            txns = await gopay_service.get_recent_transactions(page_size=100)
            for topup in list(unmatched):
                for txn in txns:
                    if _match_transaction(txn, int(topup.amount_idr), topup.created_at, set(_topup_matched_tx_ids.keys())):
                        tx_id = _txn_id(txn)
                        # Klaim di DB: transaksi yang sudah melunasi order/topup lain dilewati.
                        if not tx_id or not claim_qris_payment(db, tx_id, topup.topup_id, "topup", int(topup.amount_idr)):
                            continue
                        _topup_matched_tx_ids[tx_id] = time.time()
                        await _complete_topup(db, topup)
                        break
        # [FIX KRITIS-4] Rolling cleanup — hapus entry > 24 jam, bukan .clear() total
        _cleanup_matched_tx_ids(_topup_matched_tx_ids)
    except Exception as exc:
        logger.error("Topup polling job error: %s", exc, exc_info=True)
    finally:
        db.close()


async def _job_check_pending_buy_payments():
    """
    Poll GoPay QRIS buy orders via verifikasi massal GET /transactions
    (1 call upstream per jendela — gateway cache 25s membuatnya murah,
    tidak ada Nx /check-payment per tick) lalu auto-send crypto.
    Order 'paid' tanpa payout_tx_hash juga di-resume (crash recovery).
    """
    global _buy_matched_tx_ids
    from services.gopay_service import gopay_service
    from database.crud import get_pending_gopay_orders, get_gopay_resume_orders, claim_qris_payment
    from bot.handlers.buy import _run_finalize_background

    db = SessionLocal()
    try:
        orders = get_pending_gopay_orders(db)
        to_process = [o.order_id for o in get_gopay_resume_orders(db)]

        if orders:
            txns = await gopay_service.get_recent_transactions(page_size=100)
            for order in orders:
                for txn in txns:
                    if _match_transaction(txn, int(order.total_idr), order.created_at, set(_buy_matched_tx_ids.keys())):
                        tx_id = _txn_id(txn)
                        # Klaim di DB: transaksi yang sudah melunasi order/topup lain dilewati.
                        if not tx_id or not claim_qris_payment(db, tx_id, order.order_id, "buy", int(order.total_idr)):
                            continue
                        _buy_matched_tx_ids[tx_id] = time.time()
                        to_process.append(order.order_id)
                        break

        # [FIX KRITIS-4] Rolling cleanup — hapus entry > 24 jam, bukan .clear() total
        _cleanup_matched_tx_ids(_buy_matched_tx_ids)

        # Finalize paralel (max 5 payout bersamaan); tiap task pakai session DB sendiri.
        sem = asyncio.Semaphore(5)

        async def _finalize_one(order_id):
            async with sem:
                await _run_finalize_background(
                    order_id, allow_recovery=True, allow_expired_payment=True,
                )

        if to_process:
            await asyncio.gather(*(_finalize_one(oid) for oid in to_process))
    except Exception as exc:
        logger.error("GoPay buy polling job error: %s", exc, exc_info=True)
    finally:
        db.close()


WIB = timezone(timedelta(hours=7))


def _monthly_report_trigger():
    """Tanggal 1 pukul 00:05 WIB, eksplisit Asia/Jakarta.

    Dulu "hari terakhir 09:00" dalam zona container (UTC = 16:00 WIB) melaporkan bulan
    berjalan, sehingga transaksi jam-jam terakhir bulan tidak pernah tercatat.
    """
    from apscheduler.triggers.cron import CronTrigger
    return CronTrigger(day=1, hour=0, minute=5, timezone="Asia/Jakarta")


def _monthly_report_period(now_utc: datetime) -> tuple[int, int]:
    """(tahun, bulan) WIB yang baru saja selesai relatif terhadap `now_utc`."""
    now_wib = now_utc.astimezone(WIB)
    if now_wib.month == 1:
        return now_wib.year - 1, 12
    return now_wib.year, now_wib.month - 1


async def _job_send_monthly_report():
    """
    Kirim laporan keuangan bulan yang baru selesai ke admin (tanggal 1, 00:05 WIB).
    Laporan disimpan ke tabel monthly_reports; guard anti-ganda per period.
    """
    try:
        from database.crud import build_monthly_report, get_monthly_report
        from bot.utils.telegram_utils import notify_admins
        from bot.utils.formatter import format_idr

        year, month = _monthly_report_period(datetime.now(timezone.utc))
        period = f"{year:04d}-{month:02d}"

        db = SessionLocal()
        try:
            if get_monthly_report(db, period):
                logger.info("Laporan %s sudah tercatat — skip (anti ganda).", period)
                return

            report = build_monthly_report(db, year, month)
            db.add(report)
            db.commit()
            db.refresh(report)
            logger.info("Laporan bulanan %s dibuat: %d order, fee %s",
                        period, report.order_count, format_idr(report.fee_idr))

            msg = (
                f"📊 <b>LAPORAN KEUANGAN: {datetime(year, month, 1).strftime('%B %Y')}</b>\n\n"
                f"✅ <b>Order Berhasil:</b> {report.order_count} "
                f"(Beli {report.order_buy} · Jual {report.order_sell} · Swap {report.order_swap})\n"
                f"💰 <b>Volume IDR:</b> {format_idr(report.volume_idr)}\n"
                f"🔌 <b>Pendapatan Fee:</b> {format_idr(report.fee_idr)}\n"
                f"➕ <b>Topup Masuk:</b> {format_idr(report.topup_idr)} ({report.topup_count}x)\n"
                f"────────────────\n"
                f"💵 <b>TOTAL MASUK:</b> {format_idr(report.total_idr)}"
            )
            from services.bot_runtime import bot_app
            if bot_app:
                await notify_admins(bot_app, msg, kind="ops")
        finally:
            db.close()
    except Exception as exc:
        logger.error("Monthly report job failed: %s", exc, exc_info=True)


def ensure_gateway_running():
    """Memastikan GoPay Gateway (node server.js) berjalan di port 3005."""
    import os
    import socket
    import subprocess
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    is_open = sock.connect_ex(('127.0.0.1', 3005)) == 0
    sock.close()
    if not is_open:
        gateway_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gopay-gateway")
        if os.path.exists(os.path.join(gateway_dir, "server.js")):
            logger.info("🚀 [GATEWAY] Menjalankan GoPay Gateway (node server.js)...")
            try:
                subprocess.Popen(["node", "server.js"], cwd=gateway_dir)
            except Exception as e:
                logger.warning("Gagal auto-start gateway: %s", e)


# =============================================================
# 4. MAIN — BOT POLLING
# =============================================================
async def main():
    """
    Main coroutine: initialises everything and runs the Telegram bot
    until interrupted.
    """
    # --- 0. Ensure GoPay Gateway is running ---
    ensure_gateway_running()

    # --- 1. Init database ---
    init_database()

    # --- 2. Build Telegram bot application ---
    application = build_bot_application()

    # --- 3. Inject bot reference into runtime module ---
    set_bot_app(application)

    # --- 4. Start APScheduler ---
    scheduler = setup_scheduler()

    # --- 5. Run Telegram bot polling ---
    logger.info("Starting Telegram bot polling...")
    async with application:
        await application.start()
        await application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            bootstrap_retries=-1,
        )

        logger.info("=" * 50)
        logger.info("  P2P Crypto Bot is running! 🚀")
        logger.info("  Telegram: polling")
        logger.info("=" * 50)

        # Trigger initial wallet balance sync immediately on startup
        asyncio.create_task(_job_sync_wallet_balances())
        # Trigger initial coin API & RPC health check immediately on startup
        asyncio.create_task(_job_check_coin_apis())

        # Keep running until we receive a stop signal
        stop_event = asyncio.Event()

        # Register signal handlers for graceful shutdown
        def _signal_handler():
            logger.info("Shutdown signal received — stopping...")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                # Windows does not support add_signal_handler for SIGTERM
                # Fall back to signal.signal (works for SIGINT on Windows)
                signal.signal(sig, lambda s, f: _signal_handler())

        # Wait until stop signal
        await stop_event.wait()

        # --- Graceful shutdown ---
        logger.info("Shutting down gracefully...")

        # Stop the scheduler
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")

        # Stop bot polling
        await application.updater.stop()
        await application.stop()

    logger.info("Shutdown complete. Goodbye! 👋")


# =============================================================
# 6. ENTRY POINT
# =============================================================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — exiting")
        sys.exit(0)
