"""
database/crud.py — Semua operasi CRUD untuk database P2P Crypto Bot.

Module ini berisi fungsi-fungsi untuk Create, Read, Update data
di database menggunakan SQLAlchemy sessions.
Semua fungsi menerima `db` (SQLAlchemy Session) sebagai parameter pertama.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database.models import (
    User,
    Order,
    WalletBalance,
    InventoryReservation,
    PriceConfig,
    TopupOrder,
    MonthlyReport,
    GopaySession,
)

logger = logging.getLogger(__name__)


# ============================================================
# USER CRUD — Operasi untuk tabel users
# ============================================================

def create_user(db: Session, telegram_id: int, username: str = None, full_name: str = None) -> User:
    """
    Buat user baru atau return user yang sudah ada.
    Kalau user dengan telegram_id sudah exist, langsung return yang existing.
    """
    try:
        # Cek apakah user sudah ada
        existing = db.query(User).filter(User.telegram_id == telegram_id).first()
        if existing:
            logger.info(f"User {telegram_id} sudah terdaftar, return existing.")
            return existing

        # Buat user baru
        new_user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        logger.info(f"User baru dibuat: {telegram_id} ({username})")
        return new_user

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal create user {telegram_id}: {e}")
        raise


def get_user(db: Session, telegram_id: int) -> Optional[User]:
    """Ambil data user berdasarkan telegram_id. Return None jika tidak ditemukan."""
    try:
        return db.query(User).filter(User.telegram_id == telegram_id).first()
    except Exception as e:
        logger.error(f"Gagal get user {telegram_id}: {e}")
        raise


# ============================================================
# ORDER CRUD — Operasi untuk tabel orders
# ============================================================

def create_order(db: Session, order_data: dict) -> Order:
    """
    Buat order baru dari dictionary data.
    order_data harus berisi minimal: order_id, telegram_id, crypto_symbol,
    network, crypto_amount, price_per_unit, nominal_idr, fee_idr, total_idr.
    """
    try:
        # Validasi field wajib
        required_fields = [
            "order_id", "telegram_id", "crypto_symbol", "network",
            "crypto_amount", "price_per_unit", "nominal_idr", "fee_idr", "total_idr"
        ]
        for field in required_fields:
            if field not in order_data:
                raise ValueError(f"Field wajib '{field}' tidak ada di order_data")

        new_order = Order(**order_data)
        db.add(new_order)

        # Update user stats (total_orders + total_spent)
        user = db.query(User).filter(User.telegram_id == order_data["telegram_id"]).first()
        if user:
            user.total_orders = (user.total_orders or 0) + 1
            user.total_spent_idr = (user.total_spent_idr or 0) + order_data["total_idr"]

        db.commit()
        db.refresh(new_order)
        logger.info(f"Order baru dibuat: {new_order.order_id} untuk user {new_order.telegram_id}")
        return new_order

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal create order: {e}")
        raise


def get_order_by_id(db: Session, order_id: str) -> Optional[Order]:
    """Cari order berdasarkan order_id (string unik, bukan auto-increment id)."""
    try:
        return db.query(Order).filter(Order.order_id == order_id).first()
    except Exception as e:
        logger.error(f"Gagal get order {order_id}: {e}")
        raise


def get_orders_by_user(db: Session, telegram_id: int, limit: int = 10) -> list[Order]:
    """Ambil list order milik user tertentu, diurutkan dari terbaru. Default limit 10."""
    try:
        return (
            db.query(Order)
            .filter(Order.telegram_id == telegram_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
            .all()
        )
    except Exception as e:
        logger.error(f"Gagal get orders for user {telegram_id}: {e}")
        raise


def update_order_status(
    db: Session,
    order_id: str,
    new_status: str,
    **extra_fields
) -> Optional[Order]:
    """
    Update status order dan field tambahan lainnya.

    Contoh penggunaan:
        update_order_status(db, "ORD-123", "paid", paid_at=datetime.utcnow())
        update_order_status(db, "ORD-123", "completed", tx_hash="0xabc...", completed_at=datetime.utcnow())
    """
    try:
        order = db.query(Order).filter(Order.order_id == order_id).first()
        if not order:
            logger.warning(f"Order {order_id} tidak ditemukan untuk update status.")
            return None

        old_status = order.status
        order.status = new_status

        # Set extra fields yang dikirim (misal: paid_at, tx_hash, dll)
        for key, value in extra_fields.items():
            if hasattr(order, key):
                setattr(order, key, value)
            else:
                logger.warning(f"Field '{key}' tidak ada di model Order, di-skip.")

        db.commit()
        db.refresh(order)
        logger.info(f"Order {order_id} status updated: {old_status} -> {new_status}")
        return order

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal update order {order_id}: {e}")
        raise


def claim_order_paid(db: Session, order_id: str) -> bool:
    """
    Atomic claim order: pending -> paid.
    Hanya satu pemanggil yang menang (rowcount == 1); pemanggil lain dapat False.
    Mencegah double payout saat job polling & handler callback berjalan bersamaan.
    """
    from sqlalchemy import update
    try:
        result = db.execute(
            update(Order)
            .where(Order.order_id == order_id, Order.status == "pending")
            .values(status="paid", paid_at=datetime.utcnow(), updated_at=datetime.utcnow())
        )
        db.commit()
        return result.rowcount == 1
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal claim order {order_id}: {e}")
        raise


def claim_order_payout_processing(
    db: Session,
    order_id: str,
    allowed_statuses: tuple[str, ...] = ("paid",),
) -> bool:
    """Atomic claim payout: hanya satu worker boleh mengirim crypto."""
    from sqlalchemy import update
    try:
        result = db.execute(
            update(Order)
            .where(
                Order.order_id == order_id,
                Order.status.in_(allowed_statuses),
                Order.payout_tx_hash.is_(None),
            )
            .values(status="payout_processing", updated_at=datetime.utcnow())
        )
        db.commit()
        return result.rowcount == 1
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal claim payout order {order_id}: {e}")
        raise


def claim_stale_payout_processing(db: Session, order_id: str, stale_seconds: int = 120) -> bool:
    """Re-claim payout_processing yang stale setelah process crash."""
    from sqlalchemy import update
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=stale_seconds)
        result = db.execute(
            update(Order)
            .where(
                Order.order_id == order_id,
                Order.status == "payout_processing",
                Order.payout_tx_hash.is_(None),
                Order.updated_at <= cutoff,
            )
            .values(updated_at=datetime.utcnow())
        )
        db.commit()
        return result.rowcount == 1
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal reclaim payout order {order_id}: {e}")
        raise


def claim_topup_success(db: Session, topup_id: str) -> bool:
    """
    Atomic claim topup: PENDING -> SUCCESS.
    Hanya satu pemanggil yang menang; cegah double credit saldo.
    """
    from sqlalchemy import update
    try:
        result = db.execute(
            update(TopupOrder)
            .where(TopupOrder.topup_id == topup_id, TopupOrder.status == "PENDING")
            .values(status="SUCCESS", paid_at=datetime.utcnow())
        )
        db.commit()
        return result.rowcount == 1
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal claim topup {topup_id}: {e}")
        raise


def get_gopay_resume_orders(db: Session) -> list[Order]:
    """
    Order GOPAY_QRIS berstatus 'paid' tanpa payout_tx_hash.
    Artinya payout pernah gagal/crash sebelum selesai — layak di-retry otomatis.
    """
    return (
        db.query(Order)
        .filter(
            Order.payment_method == "GOPAY_QRIS",
            Order.status.in_(("paid", "payout_processing")),
            Order.payout_tx_hash.is_(None),
        )
        .all()
    )


def expire_stale_orders(db: Session, minutes: int = 30) -> int:
    """
    Menandai order yang kedaluwarsa menjadi 'expired':
      - Order 'pending' lama (created_at > cutoff).
      - Order 'WAITING_CRYPTO_DEPOSIT' (sell/swap) yang expired_at /
        quote_expires_at sudah lewat.
    Mengembalikan jumlah order yang ter-expire.
    """
    try:
        now = datetime.utcnow()
        cutoff_time = now - timedelta(minutes=minutes)
        pending = (
            db.query(Order)
            .filter(
                Order.status == "pending",
                Order.created_at <= cutoff_time
            )
            .all()
        )
        deposit_waiting = (
            db.query(Order)
            .filter(
                Order.status == "WAITING_CRYPTO_DEPOSIT",
                or_(
                    Order.expired_at.isnot(None),
                    Order.quote_expires_at.isnot(None),
                ),
                or_(
                    Order.expired_at <= now,
                    Order.quote_expires_at <= now,
                ),
            )
            .all()
        )
        expired_orders = pending + deposit_waiting
        if not expired_orders:
            return 0

        for order in expired_orders:
            order.status = "expired"

        db.commit()
        return len(expired_orders)
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal memproses expire stale orders: {e}")
        raise



# ============================================================
# PRICE CONFIG CRUD — Operasi untuk tabel price_config
# ============================================================

def get_price_config(db: Session, symbol: str) -> Optional[PriceConfig]:
    """Ambil konfigurasi harga (spread_pct) untuk symbol tertentu."""
    try:
        return db.query(PriceConfig).filter(PriceConfig.symbol == symbol.upper()).first()
    except Exception as e:
        logger.error(f"Gagal get price config for {symbol}: {e}")
        raise


def update_price_config(db: Session, symbol: str, spread_pct: float) -> Optional[PriceConfig]:
    """Update spread_pct untuk symbol tertentu. Return None jika symbol tidak ditemukan."""
    try:
        config = db.query(PriceConfig).filter(PriceConfig.symbol == symbol.upper()).first()
        if not config:
            logger.warning(f"Price config untuk {symbol} tidak ditemukan.")
            return None

        config.spread_pct = Decimal(str(spread_pct))
        config.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(config)
        logger.info(f"Price config {symbol} updated: spread_pct = {spread_pct}%")
        return config

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal update price config {symbol}: {e}")
        raise


def get_all_price_configs(db: Session) -> list[PriceConfig]:
    """Ambil semua price config yang aktif."""
    try:
        return db.query(PriceConfig).filter(PriceConfig.is_active == True).all()  # noqa: E712
    except Exception as e:
        logger.error(f"Gagal get all price configs: {e}")
        raise


# ============================================================
# WALLET BALANCE CRUD — Operasi untuk tabel wallet_balances
# ============================================================

def update_wallet_balance(
    db: Session,
    network: str,
    balance: float,
    symbol: str = None,
    address: str = None
) -> WalletBalance:
    """
    Update atau insert wallet balance untuk network tertentu.
    Jika network sudah ada, update balance-nya. Jika belum, buat baru.
    """
    try:
        network_upper = network.upper()
        symbol_upper = (symbol or "").upper()
        wallet = db.query(WalletBalance).filter(
            WalletBalance.network == network_upper,
            WalletBalance.symbol == symbol_upper,
        ).first()

        # Fallback mappings for symbol and address if not provided
        if not symbol:
            symbol_map = {
                "BSC": "USDT", "ETH": "USDT", "AVAX": "USDT", "POLYGON": "USDT",
                "BASE": "USDT", "ARB": "USDT", "GRAVITY": "USDT",
                "SOLANA": "SOL", "TRON": "TRX",
            }
            symbol_upper = symbol_map.get(network_upper, "USDT").upper()

        if not address:
            from config.settings import settings
            if network_upper in ["BSC", "ETH", "AVAX", "POLYGON", "BASE", "ARB", "GRAVITY",
                                 "OPTIMISM", "ROBINHOOD", "KAIA", "BERA", "HYPEREVM"]:
                address = settings.EVM_WALLET_ADDRESS
            elif network_upper == "SOLANA":
                address = settings.SOL_WALLET_ADDRESS
            elif network_upper == "TRON":
                address = settings.TRX_WALLET_ADDRESS
            elif network_upper == "TON":
                address = settings.TON_WALLET_ADDRESS
            elif network_upper == "SUI":
                address = settings.SUI_WALLET_ADDRESS
            elif network_upper == "APTOS":
                address = settings.APTOS_WALLET_ADDRESS
            else:
                address = "Unknown"

        if wallet:
            # Update existing
            wallet.balance = Decimal(str(balance))
            wallet.symbol = symbol_upper
            wallet.address = address
            wallet.updated_at = datetime.utcnow()
        else:
            # Create new
            wallet = WalletBalance(
                network=network_upper,
                symbol=symbol_upper,
                balance=Decimal(str(balance)),
                address=address,
            )
            db.add(wallet)

        db.commit()
        db.refresh(wallet)
        logger.info(f"Wallet balance updated: {network_upper} ({symbol_upper}) = {balance}")
        return wallet

    except Exception as e:
        db.rollback()
        logger.error(f"Gagal update wallet balance {network}: {e}")
        raise


def get_all_wallet_balances(db: Session) -> list[WalletBalance]:
    """Ambil semua wallet balance records."""
    try:
        return db.query(WalletBalance).all()
    except Exception as e:
        logger.error(f"Gagal get all wallet balances: {e}")
        raise


def get_available_inventory(db: Session, network: str, symbol: str) -> Optional[Decimal]:
    """Saldo aset yang belum di-reserve untuk payout lain."""
    wallet = (
        db.query(WalletBalance)
        .filter(
            WalletBalance.network == network.upper(),
            WalletBalance.symbol == symbol.upper(),
        )
        .first()
    )
    if not wallet:
        return None
    balance = Decimal(str(wallet.balance or 0))
    reserved = Decimal(str(wallet.reserved_balance or 0))
    return max(Decimal("0"), balance - reserved)


def reserve_order_inventory(
    db: Session,
    order_id: str,
    network: str,
    symbol: str,
    amount: Decimal,
) -> bool:
    """Atomically reserve inventory untuk satu payout order."""
    from sqlalchemy import update

    amount = Decimal(str(amount))
    existing = (
        db.query(InventoryReservation)
        .filter(
            InventoryReservation.order_id == order_id,
            InventoryReservation.status == "RESERVED",
        )
        .first()
    )
    if existing:
        return True

    try:
        result = db.execute(
            update(WalletBalance)
            .where(
                WalletBalance.network == network.upper(),
                WalletBalance.symbol == symbol.upper(),
                (WalletBalance.balance - WalletBalance.reserved_balance) >= amount,
            )
            .values(reserved_balance=WalletBalance.reserved_balance + amount)
        )
        if result.rowcount != 1:
            db.rollback()
            return False

        db.add(
            InventoryReservation(
                order_id=order_id,
                network=network.upper(),
                symbol=symbol.upper(),
                amount=amount,
                status="RESERVED",
            )
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("Gagal reserve inventory order %s", order_id)
        raise


def release_order_inventory(db: Session, order_id: str) -> bool:
    """Lepas reservation setelah payout sukses atau order dibatalkan."""
    reservation = (
        db.query(InventoryReservation)
        .filter(
            InventoryReservation.order_id == order_id,
            InventoryReservation.status == "RESERVED",
        )
        .first()
    )
    if not reservation:
        return False

    wallet = (
        db.query(WalletBalance)
        .filter(
            WalletBalance.network == reservation.network,
            WalletBalance.symbol == reservation.symbol,
        )
        .first()
    )
    try:
        if wallet:
            current = Decimal(str(wallet.reserved_balance or 0))
            wallet.reserved_balance = max(Decimal("0"), current - Decimal(str(reservation.amount)))
        reservation.status = "RELEASED"
        reservation.released_at = datetime.utcnow()
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("Gagal release inventory order %s", order_id)
        raise


def prune_wallet_balances(db: Session, valid_pairs: list) -> int:
    """
    Hapus baris wallet_balances yang (network, symbol) tidak termasuk daftar valid.
    Digunakan setelah sinkronisasi untuk membersihkan data lama yang tidak relevan.
    """
    try:
        valid = {(n.upper(), s.upper()) for s, n in valid_pairs}
        count = 0
        for row in db.query(WalletBalance).all():
            if (row.network.upper(), row.symbol.upper()) not in valid:
                db.delete(row)
                count += 1
        if count:
            db.commit()
            logger.info(f"Pruned {count} stale wallet balance rows")
        return count
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal prune wallet balances: {e}")
        return 0


def get_low_balance_wallets(db: Session) -> list[WalletBalance]:
    """
    Mengecek saldo wallet dan mengembalikan list wallet yang saldonya di bawah threshold.
    """
    try:
        thresholds = {
            "USDT": 10.0,
            "USDC": 10.0,
            "ETH": 0.002,
            "BNB": 0.005,
            "SOL": 0.05,
            "AVAX": 0.05,
            "TRX": 20.0,
            "MATIC": 5.0,
            "POLYGON": 5.0,
            "TON": 1.0,
            "SUI": 1.0,
            "APT": 0.2,
            "HYPE": 0.1,
            "G": 10.0,
            "ARB": 5.0,
        }
        
        all_wallets = db.query(WalletBalance).all()
        low_wallets = []
        
        for wallet in all_wallets:
            symbol = (wallet.symbol or "").upper()
            # Hanya periksa koin yang memiliki threshold terdaftar (abaikan koin tidak terdaftar)
            if symbol not in thresholds:
                continue
            limit = thresholds[symbol]
            if float(wallet.balance or 0) < limit:
                low_wallets.append(wallet)
                
        return low_wallets
    except Exception as e:
        logger.error(f"Gagal get low balance wallets: {e}")
        raise


def generate_unique_payment_code(db: Session, min_code: int = 1, max_code: int = 150) -> int:
    """
    Menghasilkan kode unik kecil (1..150, di bawah 100-200 perak) yang belum dipakai oleh order/topup PENDING lainnya.
    Mencegah selisih pembayaran terlalu jauh dari nominal asli transaksi.
    """
    import random
    try:
        pending_order_codes = {
            r[0] for r in db.query(Order.unique_code).filter(
                Order.status == "pending",
                Order.unique_code > 0
            ).all()
        }
        pending_topup_codes = {
            r[0] for r in db.query(TopupOrder.unique_code).filter(
                TopupOrder.status == "PENDING",
                TopupOrder.unique_code > 0
            ).all()
        }
        used_codes = pending_order_codes.union(pending_topup_codes)

        # Cari kode unik yang belum terpakai di rentang kecil (1..150)
        available = [c for c in range(min_code, max_code + 1) if c not in used_codes]
        if available:
            return random.choice(available)
        
        # Fallback jika ada >150 order pending bersamaan
        wider_available = [c for c in range(1, 1000) if c not in used_codes]
        if wider_available:
            return random.choice(wider_available)
        return random.randint(min_code, max_code)
    except Exception as e:
        logger.error(f"Gagal generate unique code: {e}")
        return random.randint(min_code, max_code)




# ============================================================
# STATISTICS — Fungsi statistik dan reporting
# ============================================================

def get_user_count(db: Session) -> int:
    """Hitung total user yang terdaftar."""
    try:
        return db.query(func.count(User.telegram_id)).scalar() or 0
    except Exception as e:
        logger.error(f"Gagal get user count: {e}")
        raise


def get_completed_order_count(db: Session) -> int:
    """Jumlah total order yang berhasil (status completed) seumur hidup bot."""
    try:
        return db.query(func.count(Order.id)).filter(Order.status == "completed").scalar() or 0
    except Exception as e:
        logger.error(f"Gagal get completed order count: {e}")
        raise


def get_monthly_report(db: Session, period: str) -> Optional[MonthlyReport]:
    """Ambil laporan bulanan berdasarkan period 'YYYY-MM'. Return None jika belum ada."""
    return db.query(MonthlyReport).filter(MonthlyReport.period == period).first()


def build_monthly_report(db: Session, year: int, month: int) -> MonthlyReport:
    """
    Hitung laporan keuangan untuk (year, month):
      - Order completed (buy/sell/swap): jumlah, volume IDR, pendapatan fee.
      - Topup SUCCESS: jumlah & nominal IDR masuk.
    Batas bulan dihitung dalam WIB (UTC+7), lalu dikonversi ke UTC-naive agar
    cocok dengan created_at (datetime.utcnow). Hasil BELUM disimpan ke DB.
    """
    start_wib = datetime(year, month, 1)
    end_wib = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    start_utc = start_wib - timedelta(hours=7)
    end_utc = end_wib - timedelta(hours=7)

    orders = (
        db.query(Order)
        .filter(
            Order.status == "completed",
            Order.created_at >= start_utc,
            Order.created_at < end_utc,
        )
        .all()
    )
    topups = (
        db.query(TopupOrder)
        .filter(
            TopupOrder.status == "SUCCESS",
            TopupOrder.created_at >= start_utc,
            TopupOrder.created_at < end_utc,
        )
        .all()
    )

    volume_idr = sum(int(o.total_idr or 0) for o in orders)
    fee_idr = sum(int(o.fee_idr or 0) for o in orders)
    topup_idr = sum(int(t.amount_idr or 0) for t in topups)

    return MonthlyReport(
        period=f"{year:04d}-{month:02d}",
        order_count=len(orders),
        order_buy=sum(1 for o in orders if o.order_type == "buy"),
        order_sell=sum(1 for o in orders if o.order_type == "sell"),
        order_swap=sum(1 for o in orders if o.order_type == "swap"),
        volume_idr=volume_idr,
        fee_idr=fee_idr,
        topup_count=len(topups),
        topup_idr=topup_idr,
        total_idr=volume_idr + fee_idr + topup_idr,
    )


def get_daily_stats(db: Session) -> dict:
    """
    Ambil statistik harian: total orders, total volume IDR, completed orders hari ini.
    """
    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Total orders hari ini (semua status)
        total_orders_today = (
            db.query(func.count(Order.id))
            .filter(Order.created_at >= today_start)
            .scalar() or 0
        )

        # Total volume IDR hari ini (dari orders yang completed)
        total_volume_idr_today = (
            db.query(func.coalesce(func.sum(Order.total_idr), 0))
            .filter(
                Order.created_at >= today_start,
                Order.status == "completed"
            )
            .scalar() or 0
        )

        # Jumlah completed orders hari ini
        completed_orders_today = (
            db.query(func.count(Order.id))
            .filter(
                Order.created_at >= today_start,
                Order.status == "completed"
            )
            .scalar() or 0
        )

        return {
            "total_orders_today": total_orders_today,
            "total_volume_idr_today": int(total_volume_idr_today),
            "completed_orders_today": completed_orders_today,
        }

    except Exception as e:
        logger.error(f"Gagal get daily stats: {e}")
        raise


# ==========================================
# USER BALANCE & TOPUP ORDER CRUD HELPERS
# ==========================================

def get_user_balance(db: Session, telegram_id: int) -> float:
    """Mengambil sisa saldo IDR pengguna dari database."""
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return 0.0
    return float(user.balance_idr or 0.0)


def credit_user_balance(db: Session, telegram_id: int, amount_idr: float) -> float:
    """Menambahkan (mengkreditkan) saldo IDR pengguna."""
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            user = create_user(db, telegram_id)
            
        current_bal = float(user.balance_idr or 0.0)
        new_bal = current_bal + float(amount_idr)
        user.balance_idr = Decimal(str(new_bal))
        db.commit()
        db.refresh(user)
        logger.info(f"User {telegram_id} balance credited +Rp {amount_idr:,.0f} -> New Balance: Rp {new_bal:,.0f}")
        return new_bal
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal credit saldo user {telegram_id}: {e}")
        raise


def deduct_user_balance(db: Session, telegram_id: int, amount_idr: float) -> bool:
    """Memotong (debit) saldo IDR pengguna jika saldo mencukupi."""
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            return False
            
        current_bal = float(user.balance_idr or 0.0)
        if current_bal < amount_idr:
            logger.warning(f"User {telegram_id} saldo tidak cukup. Saldo: {current_bal}, Butuh: {amount_idr}")
            return False
            
        new_bal = current_bal - float(amount_idr)
        user.balance_idr = Decimal(str(new_bal))
        db.commit()
        db.refresh(user)
        logger.info(f"User {telegram_id} balance deducted -Rp {amount_idr:,.0f} -> Sisa: Rp {new_bal:,.0f}")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal deduct saldo user {telegram_id}: {e}")
        raise


def create_topup_order(
    db: Session,
    topup_id: str,
    telegram_id: int,
    amount_idr: int,
    expires_at: datetime = None
) -> TopupOrder:
    """Membuat record TopupOrder baru untuk deposit QRIS."""
    try:
        topup = TopupOrder(
            topup_id=topup_id,
            telegram_id=telegram_id,
            amount_idr=amount_idr,
            status="PENDING",
            created_at=datetime.utcnow(),
            expires_at=expires_at
        )
        db.add(topup)
        db.commit()
        db.refresh(topup)
        logger.info(f"TopupOrder {topup_id} created for user {telegram_id} ({amount_idr} IDR)")
        return topup
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal membuat TopupOrder {topup_id}: {e}")
        raise


def get_topup_order_by_id(db: Session, topup_id: str) -> Optional[TopupOrder]:
    """Mengambil data TopupOrder berdasarkan topup_id."""
    return db.query(TopupOrder).filter(TopupOrder.topup_id == topup_id).first()


def update_topup_status(db: Session, topup_id: str, status: str, paid_at: datetime = None) -> Optional[TopupOrder]:
    """Memperbarui status TopupOrder (e.g. SUCCESS, EXPIRED, CANCELLED)."""
    try:
        topup = db.query(TopupOrder).filter(TopupOrder.topup_id == topup_id).first()
        if not topup:
            return None
        topup.status = status
        if paid_at:
            topup.paid_at = paid_at
        db.commit()
        db.refresh(topup)
        logger.info(f"TopupOrder {topup_id} status updated -> {status}")
        return topup
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal update status TopupOrder {topup_id}: {e}")
        raise


def get_pending_topup_orders(db: Session) -> list[TopupOrder]:
    """Mengambil seluruh TopupOrder dengan status PENDING."""
    return db.query(TopupOrder).filter(TopupOrder.status == "PENDING").all()


def get_pending_gopay_orders(db: Session) -> list[Order]:
    """Mengambil semua Order Beli via GoPay QRIS yang masih berstatus pending."""
    return (
        db.query(Order)
        .filter(
            Order.payment_method == "GOPAY_QRIS",
            Order.status == "pending",
        )
        .all()
    )


def get_pending_gopay_order_for_user(db: Session, telegram_id: int) -> Optional[Order]:
    """Order Beli GoPay QRIS pending terbaru milik seorang user (untuk klaim bukti transfer)."""
    return (
        db.query(Order)
        .filter(
            Order.payment_method == "GOPAY_QRIS",
            Order.status == "pending",
            Order.telegram_id == telegram_id,
        )
        .order_by(Order.created_at.desc())
        .first()
    )


# ============================================================
# GOPAY SESSION CRUD (PostgreSQL & Multi-Deploy Persistence)
# ============================================================

def get_gopay_session(db: Session, key: str = "active_session") -> Optional[dict]:
    """Mengambil session data GoPay dari database."""
    import json
    try:
        row = db.query(GopaySession).filter(GopaySession.key == key).first()
        if row and row.session_data:
            return json.loads(row.session_data)
        return None
    except Exception as e:
        logger.warning(f"Gagal get_gopay_session dari DB: {e}")
        return None


def save_gopay_session(db: Session, session_data: dict, key: str = "active_session") -> bool:
    """Menyimpan atau memperbarui session data GoPay di database."""
    import json
    try:
        data_str = json.dumps(session_data)
        row = db.query(GopaySession).filter(GopaySession.key == key).first()
        if not row:
            row = GopaySession(key=key, session_data=data_str)
            db.add(row)
        else:
            row.session_data = data_str
            row.updated_at = datetime.utcnow()
        db.commit()
        logger.info(f"Sesi GoPay berhasil disimpan ke database (key={key}).")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Gagal save_gopay_session ke DB: {e}")
        return False


def sync_gopay_session_file(session_file_path: str = None) -> bool:
    """
    Sinkronisasi dua arah file sesi .GOPAY_SESI_JANGAN_DIHAPUS.json dengan database:
    1. Jika file lokal tidak ada / kosong, muat dari database PostgreSQL dan tulis file.
    2. Jika file lokal ada dan valid, pastikan database selalu ter-update dengan isi file tersebut.
    """
    import os
    import json
    from database.connection import SessionLocal

    if not session_file_path:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        session_file_path = os.path.join(base_dir, "gopay-gateway", ".GOPAY_SESI_JANGAN_DIHAPUS.json")

    db = SessionLocal()
    try:
        file_exists = os.path.exists(session_file_path)
        file_session = None
        if file_exists:
            try:
                with open(session_file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        file_session = json.loads(content)
            except Exception as fe:
                logger.warning(f"Error membaca file sesi lokal: {fe}")

        db_session = get_gopay_session(db)

        if not file_session and db_session:
            # File kosong / hilang setelah redeploy container -> Pulihkan dari DB!
            os.makedirs(os.path.dirname(session_file_path), exist_ok=True)
            with open(session_file_path, "w", encoding="utf-8") as f:
                json.dump(db_session, f, indent=2)
            logger.info("🔑 [GOPAY] Berhasil memulihkan sesi GoPay dari database PostgreSQL ke file lokal.")
            return True

        elif file_session:
            # File lokal ada -> Pastikan DB selalu sinkron
            if not db_session or (file_session.get("updated_at") != db_session.get("updated_at")):
                save_gopay_session(db, file_session)
                logger.info("💾 [GOPAY] Sesi lokal tersimpan & tersinkronisasi ke database PostgreSQL.")
            return True

        return False
    except Exception as exc:
        logger.error(f"Gagal sync_gopay_session_file: {exc}")
        return False
    finally:
        db.close()
