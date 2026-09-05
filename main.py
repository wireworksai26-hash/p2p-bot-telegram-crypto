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
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

from telegram import Update
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
)

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
_topup_matched_tx_ids = set()

# State verifikasi massal order Beli via GET /transactions (anti klaim ganda antar order)
_buy_matched_tx_ids = set()


# =============================================================
# 1. DATABASE INITIALISATION & SEEDING
# =============================================================
def init_database():
    """
    Create all SQLAlchemy tables (no-op if they already exist)
    and seed default data for fee_tiers and price_config.
    """
    logger.info("Creating database tables (if not exist)...")
    Base.metadata.create_all(bind=engine)

    # Migrasi schema (SQLite): wallet_balances & orders
    _migrate_wallet_balance_schema()
    _migrate_inventory_schema()
    _migrate_orders_schema()

    db = SessionLocal()
    try:
        _seed_price_configs(db)
        db.commit()
        logger.info("Database initialisation complete")
    except Exception as exc:
        db.rollback()
        logger.error("Error seeding database: %s", exc, exc_info=True)
    finally:
        db.close()


def _migrate_orders_schema():
    """
    Migrasi tabel orders & topup_orders (SQLite): tambah kolom payment_method & unique_code
    jika belum ada.
    """
    from database.connection import engine
    try:
        with engine.begin() as conn:
            existing_orders = {
                r[1] for r in conn.exec_driver_sql("PRAGMA table_info('orders')").fetchall()
            }
            new_columns_orders = [
                ("payment_method", "VARCHAR(30)"),
                ("unique_code", "INTEGER DEFAULT 0"),
            ]
            for col, dtype in new_columns_orders:
                if col not in existing_orders:
                    conn.exec_driver_sql(f"ALTER TABLE orders ADD COLUMN {col} {dtype}")
                    logger.info("Migrasi orders: kolom %s ditambahkan.", col)

            existing_topups = {
                r[1] for r in conn.exec_driver_sql("PRAGMA table_info('topup_orders')").fetchall()
            }
            if "unique_code" not in existing_topups:
                conn.exec_driver_sql("ALTER TABLE topup_orders ADD COLUMN unique_code INTEGER DEFAULT 0")
                logger.info("Migrasi topup_orders: kolom unique_code ditambahkan.")
    except Exception as exc:
        logger.error("Migrasi orders gagal: %s", exc, exc_info=True)


def _migrate_inventory_schema():
    """Tambah kolom reservation pada wallet_balances database lama."""
    try:
        with engine.begin() as conn:
            columns = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info('wallet_balances')").fetchall()
            }
            if "reserved_balance" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE wallet_balances ADD COLUMN reserved_balance NUMERIC(36, 18) DEFAULT 0"
                )
                logger.info("Migrasi wallet_balances: kolom reserved_balance ditambahkan.")
            conn.exec_driver_sql(
                "UPDATE wallet_balances SET reserved_balance = 0 WHERE reserved_balance IS NULL"
            )
    except Exception as exc:
        logger.error("Migrasi inventory gagal: %s", exc, exc_info=True)



def _migrate_wallet_balance_schema():
    """
    Migrasi tabel wallet_balances (khusus SQLite) agar unik per pasangan
    (network, symbol), bukan per network saja.
    Jika tabel sudah sesuai, fungsi ini menjadi no-op.
    """
    from database.connection import engine
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
    """Insert default price configs (spread per symbol) if the table is empty."""
    if db.query(PriceConfig).count() > 0:
        logger.info("Price configs already exist — skipping seed")
        return

    symbols = {
        "USDT": 1.5, "ETH": 1.5, "BNB": 1.5, "SOL": 1.5,
        "AVAX": 1.5, "TRX": 1.5, "MATIC": 1.5, "LTC": 1.5,
        "G": 2.0,     # Gravity — less liquid, wider spread
        "BASE": 1.5, "ARB": 1.5,
    }
    configs = [
        PriceConfig(symbol=sym, spread_pct=spread, is_active=True)
        for sym, spread in symbols.items()
    ]
    db.add_all(configs)
    logger.info("Seeded %d price configs", len(configs))


# =============================================================
# 2. TELEGRAM BOT SETUP
# =============================================================
def build_bot_application() -> Application:
    """
    Build the python-telegram-bot Application and register
    all command handlers, conversation handlers, and the
    global error handler.
    """
    logger.info("Building Telegram bot application...")

    application = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )

    # --- Command Handlers ---
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("price", show_prices))
    application.add_handler(CommandHandler("harga", show_prices))
    application.add_handler(CommandHandler("balance", show_balance_menu))
    application.add_handler(CommandHandler("admin", admin_handler))
    application.add_handler(CommandHandler("setspread", setspread_handler))
    application.add_handler(CommandHandler("orders", orders_handler))
    application.add_handler(CommandHandler("confirm", confirm_handler))
    application.add_handler(CommandHandler("broadcast", broadcast_handler))
    application.add_handler(CommandHandler("ban", ban_handler))
    application.add_handler(CommandHandler("unban", unban_handler))
    application.add_handler(CommandHandler("stats", stats_handler))
    application.add_handler(CommandHandler("refreshwallet", refreshwallet_handler))

    # --- Conversation Handlers (multi-step flows) ---
    # ConversationHandlers have higher priority than standalone commands
    # so they intercept messages during an active conversation.
    application.add_handler(topup_conversation_handler)
    application.add_handler(buy_conversation_handler)
    application.add_handler(sell_conversation_handler)
    application.add_handler(swap_conv_handler)
    application.add_handler(calculator_conversation_handler)

    # --- Bukti Transfer QRIS (foto) ---
    # Satu router: foto diarahkan ke alur Buy ATAU Topup (hindari forward ganda ke admin).
    application.add_handler(MessageHandler(filters.PHOTO, _route_transfer_proof))

    # --- Callback Query Handler (catch-all for inline keyboard buttons) ---
    # Handles menu_* callbacks and any other inline-button presses.
    application.add_handler(CallbackQueryHandler(menu_callback_handler))

    # --- Global Error Handler ---
    application.add_error_handler(error_handler)

    logger.info("All handlers registered")
    return application


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
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)

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
            f"🚨 <b>Bot Error</b>\n\n<code>{context.error}</code>",
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
        name="Expire pending orders older than 30 min",
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

    # --- Monthly Financial Report (hari terakhir bulan, 09:00 WIB) ---
    from apscheduler.triggers.cron import CronTrigger
    scheduler.add_job(
        _job_send_monthly_report,
        CronTrigger(day="last", hour=9, minute=0),
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
            for order in stale_gopay:
                try:
                    pay_res = await gopay_service.check_payment(
                        int(order.total_idr), order.order_id
                    )
                    if pay_res.get("paid"):
                        await finalize_gopay_buy_payment(db, order)
                except Exception as exc:
                    logger.warning("Final check order %s gagal: %s", order.order_id, exc)

            expired_count = expire_stale_orders(db, minutes=settings.ORDER_EXPIRE_MINUTES)
            if expired_count > 0:
                logger.info("Expired %d stale orders", expired_count)
        finally:
            db.close()
    except Exception as exc:
        logger.error("Order expiry job failed: %s", exc, exc_info=True)


async def _job_sync_wallet_balances():
    """Query on-chain balances for all (network, symbol) pairs and update wallet_balances table."""
    from services.crypto_sender import CryptoSenderFactory
    from database.crud import update_wallet_balance, prune_wallet_balances
    from config.assets import STOCK_ASSETS

    async def fetch_balance(network, symbol):
        try:
            sender = CryptoSenderFactory.get_sender(network)
            balance = await sender.get_balance(symbol=symbol)
            addr = getattr(sender, "wallet_address", None)
            return balance, addr
        except Exception as exc:
            logger.warning("Balance sync failed for %s (%s): %s", network, symbol, exc)
            return None, None

    tasks = [fetch_balance(net, sym) for sym, net in STOCK_ASSETS]
    results = await asyncio.gather(*tasks)

    db = SessionLocal()
    try:
        for (sym, net), (balance, addr) in zip(STOCK_ASSETS, results):
            if balance is not None:
                update_wallet_balance(
                    db, network=net, symbol=sym, balance=balance, address=addr
                )
        prune_wallet_balances(db, STOCK_ASSETS)
        logger.debug("Wallet balances synced")
    finally:
        db.close()


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

                    await notify_admins(bot_app, alert_msg)
        finally:
            db.close()
    except Exception as exc:
        logger.error("Low balance alert job failed: %s", exc, exc_info=True)



async def _complete_topup(db, topup):
    """Tandai topup SUCCESS (atomic claim), credit saldo user, dan kirim notifikasi Telegram."""
    from services.bot_runtime import bot_app
    from database.crud import claim_topup_success, credit_user_balance
    from bot.utils.formatter import format_idr

    if not claim_topup_success(db, topup.topup_id):
        return
    new_bal = credit_user_balance(db, topup.telegram_id, topup.amount_idr)

    if bot_app:
        try:
            msg = (
                f"✅ <b>PEMBAYARAN QRIS TERVERIFIKASI (OTOMATIS)!</b>\n\n"
                f"🎉 Topup saldo sebesar <b>{format_idr(topup.amount_idr)}</b> telah berhasil!\n"
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


def _match_transaction(txn: dict, amount: int, created_at, used_ids: set) -> bool:
    """Cocokkan satu transaksi riwayat mutasi dengan order/topup PENDING (nominal + waktu)."""
    tx_id = str(txn.get("transaction_id") or txn.get("id") or "")
    if tx_id and tx_id in used_ids:
        return False

    try:
        if int(txn.get("amount")) != int(amount):
            return False
    except (TypeError, ValueError):
        return False

    # Transaksi harus terjadi setelah order/topup dibuat (dengan toleransi 5 menit untuk clock drift)
    t_time = txn.get("transaction_time") or txn.get("time") or txn.get("created_at") or ""
    if t_time and created_at:
        try:
            tx_dt = datetime.fromisoformat(str(t_time).replace("Z", "+00:00"))
            if tx_dt.tzinfo:
                tx_dt = tx_dt.replace(tzinfo=None)
            from datetime import timedelta
            if tx_dt < (created_at.replace(tzinfo=None) - timedelta(minutes=5)):
                return False
        except ValueError:
            pass
    return True


def _match_transaction_for_topup(txn: dict, topup, used_ids: set) -> bool:
    """Cocokkan satu transaksi riwayat mutasi dengan topup PENDING."""
    return _match_transaction(txn, int(topup.amount_idr), topup.created_at, used_ids)


async def _job_check_pending_topups():
    """Poll GoPay API gateway for pending QRIS topup orders and auto-credit balances."""
    global _topup_last_transactions_fetch, _topup_matched_tx_ids
    from services.gopay_service import gopay_service
    from database.crud import get_pending_topup_orders, update_topup_status

    db = SessionLocal()
    try:
        pending_topups = get_pending_topup_orders(db)
        if not pending_topups:
            return

        # Cek paralel (max 10) — satu tick tidak boleh antri N x timeout gateway.
        sem = asyncio.Semaphore(10)

        async def _check(topup):
            async with sem:
                # Expire topup if past expires_at
                if topup.expires_at and datetime.utcnow() > topup.expires_at:
                    update_topup_status(db, topup.topup_id, "EXPIRED")
                    return None

                pay_res = await gopay_service.check_payment(topup.amount_idr, topup.topup_id)
                if pay_res.get("paid"):
                    await _complete_topup(db, topup)
                    return None
                return topup

        results = await asyncio.gather(*(_check(t) for t in pending_topups))
        unmatched = [t for t in results if t]

        if len(_topup_matched_tx_ids) > 2000:
            _topup_matched_tx_ids.clear()

        # Fallback auto-detect via GET /transactions (throttle 60s, anti rate-limit)
        if unmatched and (time.time() - _topup_last_transactions_fetch) >= 60:
            _topup_last_transactions_fetch = time.time()
            txns = await gopay_service.get_recent_transactions(page_size=100)
            for topup in list(unmatched):
                for txn in txns:
                    if _match_transaction_for_topup(txn, topup, _topup_matched_tx_ids):
                        tx_id = str(txn.get("transaction_id") or txn.get("id") or "")
                        if tx_id:
                            _topup_matched_tx_ids.add(tx_id)
                        await _complete_topup(db, topup)
                        break
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
    from database.crud import get_pending_gopay_orders, get_gopay_resume_orders
    from bot.handlers.buy import _run_finalize_background

    db = SessionLocal()
    try:
        orders = get_pending_gopay_orders(db)
        to_process = [o.order_id for o in get_gopay_resume_orders(db)]

        if orders:
            txns = await gopay_service.get_recent_transactions(page_size=100)
            for order in orders:
                for txn in txns:
                    if _match_transaction(txn, int(order.total_idr), order.created_at, _buy_matched_tx_ids):
                        tx_id = str(txn.get("transaction_id") or txn.get("id") or "")
                        if tx_id:
                            _buy_matched_tx_ids.add(tx_id)
                        to_process.append(order.order_id)
                        break

        if len(_buy_matched_tx_ids) > 2000:
            _buy_matched_tx_ids.clear()

        # Finalize paralel (max 5 payout bersamaan); tiap task pakai session DB sendiri.
        sem = asyncio.Semaphore(5)

        async def _finalize_one(order_id):
            async with sem:
                await _run_finalize_background(order_id, allow_recovery=True)

        if to_process:
            await asyncio.gather(*(_finalize_one(oid) for oid in to_process))
    except Exception as exc:
        logger.error("GoPay buy polling job error: %s", exc, exc_info=True)
    finally:
        db.close()


async def _job_send_monthly_report():
    """
    Kirim laporan keuangan bulanan ke admin di hari terakhir bulan (09:00 WIB).
    Laporan disimpan ke tabel monthly_reports; guard anti-ganda per period.
    """
    try:
        from database.crud import build_monthly_report, get_monthly_report
        from bot.utils.telegram_utils import notify_admins
        from bot.utils.formatter import format_idr

        now = datetime.now()  # waktu lokal (WIB)
        period = f"{now.year:04d}-{now.month:02d}"

        db = SessionLocal()
        try:
            if get_monthly_report(db, period):
                logger.info("Laporan %s sudah tercatat — skip (anti ganda).", period)
                return

            report = build_monthly_report(db, now.year, now.month)
            db.add(report)
            db.commit()
            db.refresh(report)
            logger.info("Laporan bulanan %s dibuat: %d order, fee %s",
                        period, report.order_count, format_idr(report.fee_idr))

            msg = (
                f"📊 <b>LAPORAN KEUANGAN — {now.strftime('%B %Y')}</b>\n\n"
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
                await notify_admins(bot_app, msg)
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
        )

        logger.info("=" * 50)
        logger.info("  P2P Crypto Bot is running! 🚀")
        logger.info("  Telegram: polling")
        logger.info("=" * 50)

        # Trigger initial wallet balance sync immediately on startup
        asyncio.create_task(_job_sync_wallet_balances())

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
