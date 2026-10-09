from sqlalchemy import Column, Integer, BigInteger, String, Boolean, Numeric, DateTime, ForeignKey, UniqueConstraint, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from database.connection import Base

class User(Base):
    __tablename__ = 'users'

    telegram_id = Column(BigInteger, primary_key=True)
    username = Column(String(100), nullable=True)
    full_name = Column(String(200), nullable=True)
    is_banned = Column(Boolean, default=False)
    total_orders = Column(Integer, default=0)
    total_spent_idr = Column(BigInteger, default=0)
    balance_idr = Column(Numeric(15, 2), default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    orders = relationship("Order", back_populates="user")
    topups = relationship("TopupOrder", back_populates="user")

    @property
    def balance(self):
        """Property alias for balance_idr."""
        return float(self.balance_idr or 0.0)


class TopupOrder(Base):
    __tablename__ = 'topup_orders'

    id = Column(Integer, primary_key=True, autoincrement=True)
    topup_id = Column(String(50), unique=True, nullable=False, index=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False)
    amount_idr = Column(BigInteger, nullable=False)
    mdr_idr = Column(BigInteger, default=0)  # Pajak QRIS 0,3% (0 bila tidak kena)
    unique_code = Column(Integer, default=0)
    status = Column(String(30), default='PENDING', nullable=False, index=True) # PENDING, SUCCESS, EXPIRED, CANCELLED
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="topups")

class Order(Base):
    __tablename__ = 'orders'

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(50), unique=True, nullable=False, index=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False)
    order_type = Column(String(15), default='buy')  # 'buy', 'sell', 'swap'
    
    # Source Asset & Network Info
    crypto_symbol = Column(String(20), nullable=False)  # USDT, ETH, BNB, SOL, etc.
    network = Column(String(30), nullable=False)        # BSC, ERC20, SOLANA, TRON, etc.
    crypto_amount = Column(Numeric(36, 18), nullable=False)
    
    # Target Asset & Network Info (For SWAP/Convert)
    target_crypto_symbol = Column(String(20), nullable=True)
    target_network = Column(String(30), nullable=True)
    target_crypto_amount = Column(Numeric(36, 18), nullable=True)

    price_per_unit = Column(BigInteger, nullable=False) # IDR price of 1 crypto unit
    nominal_idr = Column(BigInteger, nullable=False)    # Base value in IDR
    fee_idr = Column(BigInteger, nullable=False)        # Transaction fee in IDR
    mdr_idr = Column(BigInteger, default=0)             # Pajak QRIS 0,3% (0 bila tidak kena)
    unique_code = Column(Integer, default=0)           # Unique payment code (1..400)
    total_idr = Column(BigInteger, nullable=False)      # Total IDR user pays (buy) or gets (sell)

    fee_category = Column(String(15), default='ALTCOIN') # 'USD', 'ALTCOIN', 'CONVERT'
    
    # Wallet / Bank info
    buyer_wallet = Column(String(250), nullable=True)   # Destination address for BUY / SWAP / info bank SELL
    deposit_wallet = Column(String(250), nullable=True) # Seller deposit address

    # Payment info
    payment_method = Column(String(30), nullable=True)   # 'GOPAY_QRIS', 'BOT_BALANCE'
    
    # Referral Discount (Phase 7)
    referral_discount_applied = Column(Boolean, default=False)           # True jika diskon referral diterapkan
    referral_discount_pct = Column(Numeric(5, 2), nullable=True)         # Persen diskon yang diaplikasikan
    discount_amount_idr = Column(BigInteger, default=0)                  # Nilai diskon dalam IDR

    # Status Machine (15 States)
    # DRAFT, QUOTED, WAITING_IDR_PAYMENT, WAITING_IDR_VERIFICATION, WAITING_CRYPTO_DEPOSIT,
    # CRYPTO_DETECTED, CRYPTO_CONFIRMED, PAYOUT_QUEUED, PAYOUT_PROCESSING,
    # PAYOUT_BROADCASTED, COMPLETED,
    # MANUAL_REVIEW, REJECTED, CANCELLED, EXPIRED, FAILED
    status = Column(String(30), default='DRAFT', nullable=False, index=True)
    failure_reason = Column(String(500), nullable=True)
    
    # Hashes & Idempotency
    deposit_tx_hash = Column(String(250), nullable=True)
    deposit_proof_file_id = Column(String(500), nullable=True)
    payout_tx_hash = Column(String(250), nullable=True)
    tx_hash = Column(String(250), nullable=True)        # Legacy compatibility
    
    # Timestamps
    quoted_at = Column(DateTime, nullable=True)
    quote_expires_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    expired_at = Column(DateTime, nullable=True)
    testimony_posted_at = Column(DateTime, nullable=True)  # Terisi saat testimoni channel terkirim
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="orders")

class DepositClaim(Base):
    """A receipt may fund only one order, even with concurrent bot workers."""
    __tablename__ = "deposit_claims"
    __table_args__ = (UniqueConstraint("network", "tx_hash", name="uq_deposit_receipt"),)
    id = Column(Integer, primary_key=True)
    network = Column(String(30), nullable=False)
    tx_hash = Column(String(250), nullable=False)
    order_id = Column(String(50), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class QrisPaymentClaim(Base):
    """Satu transaksi GoPay/QRIS hanya boleh melunasi SATU order/topup.

    Dipakai semua jalur deteksi (poller /transactions, /check-payment, tombol
    "Saya Sudah Transfer", bukti foto, final-check expiry) — persisten di DB
    sehingga tetap berlaku setelah restart/deploy.
    """
    __tablename__ = "qris_payment_claims"
    id = Column(Integer, primary_key=True)
    tx_id = Column(String(120), nullable=False, unique=True)
    ref_id = Column(String(60), nullable=False, index=True)   # order_id / topup_id
    kind = Column(String(10), nullable=False)                 # "buy" / "topup"
    amount_idr = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AdminActionToken(Base):
    """Token satu kali untuk callback admin yang mengubah saldo atau kas bot."""
    __tablename__ = "admin_action_tokens"

    token = Column(String(32), primary_key=True)
    admin_id = Column(BigInteger, nullable=False, index=True)
    action = Column(String(60), nullable=False, index=True)
    payload = Column(String(250), nullable=False)
    chat_id = Column(BigInteger, nullable=True)
    message_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    consumed_at = Column(DateTime, nullable=True)


class CampaignActionLock(Base):
    """DB lock bersyarat untuk mencegah dua eksekusi campaign sejenis bersamaan."""
    __tablename__ = "campaign_action_locks"

    action = Column(String(40), primary_key=True)
    locked_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdminTaskClaim(Base):
    """Penanda "✋ Saya tangani": satu tugas admin (mis. withdraw) hanya boleh dipegang satu admin."""
    __tablename__ = "admin_task_claims"

    task_key = Column(String(80), primary_key=True)
    admin_id = Column(BigInteger, nullable=False)
    claimed_at = Column(DateTime, default=datetime.utcnow)


class WalletBalance(Base):
    __tablename__ = 'wallet_balances'
    __table_args__ = (
        UniqueConstraint('network', 'symbol', name='uq_wallet_network_symbol'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    network = Column(String(30), nullable=False)
    symbol = Column(String(20), nullable=False)
    balance = Column(Numeric(36, 18), default=0.0)
    reserved_balance = Column(Numeric(36, 18), default=0.0)
    address = Column(String(200), nullable=False)
    sync_status = Column(String(20), default="UNKNOWN", nullable=False, index=True)
    last_error = Column(String(500), nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class InventoryReservation(Base):
    __tablename__ = 'inventory_reservations'

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(50), unique=True, nullable=False, index=True)
    network = Column(String(30), nullable=False)
    symbol = Column(String(20), nullable=False)
    amount = Column(Numeric(36, 18), nullable=False)
    status = Column(String(20), default='RESERVED', nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    released_at = Column(DateTime, nullable=True)


class PriceConfig(Base):
    __tablename__ = 'price_config'

    symbol = Column(String(20), primary_key=True)
    spread_pct = Column(Numeric(5, 2), default=0.0)  # Pure market price (0% spread)
    is_active = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class AuditLog(Base):
    __tablename__ = 'audit_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=True)
    action = Column(String(100), nullable=False)
    order_id = Column(String(50), nullable=True)
    from_status = Column(String(30), nullable=True)
    to_status = Column(String(30), nullable=True)
    details = Column(String(1000), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MonthlyReport(Base):
    __tablename__ = 'monthly_reports'

    id = Column(Integer, primary_key=True, autoincrement=True)
    period = Column(String(7), unique=True, nullable=False, index=True)  # YYYY-MM
    order_count = Column(Integer, default=0)
    order_buy = Column(Integer, default=0)
    order_sell = Column(Integer, default=0)
    order_swap = Column(Integer, default=0)
    volume_idr = Column(BigInteger, default=0)
    fee_idr = Column(BigInteger, default=0)
    topup_count = Column(Integer, default=0)
    topup_idr = Column(BigInteger, default=0)
    total_idr = Column(BigInteger, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class GopaySession(Base):
    __tablename__ = 'gopay_sessions'

    key = Column(String(100), primary_key=True, default='active_session')
    session_data = Column(String, nullable=False)  # JSON payload string
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChainMaintenance(Base):
    """Penanda maintenance per jaringan (NETWORK) atau koin (COIN): order BARU ditutup, order berjalan tetap diproses."""
    __tablename__ = 'chain_maintenance'
    __table_args__ = (UniqueConstraint('scope', 'code', name='uq_chain_maintenance_scope_code'),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope = Column(String(10), nullable=False)   # 'NETWORK' | 'COIN'
    code = Column(String(30), nullable=False)    # mis. SOLANA, ETH, USDT
    note = Column(String(200), nullable=True)    # catatan untuk user (opsional)
    set_by = Column(BigInteger, nullable=True)   # telegram_id admin
    created_at = Column(DateTime, default=datetime.utcnow)


class NotificationTarget(Base):
    """Tujuan notifikasi per jenis fitur (topik grup forum atau chat)."""
    __tablename__ = 'notification_targets'

    kind = Column(String(20), primary_key=True)  # beli/jual/convert/error/alarm/topup/ops
    chat_id = Column(String(32), nullable=False)
    thread_id = Column(String(32), nullable=True)  # id topik forum (None bila bukan forum)
    title = Column(String(150), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Referral(Base):
    """Tracking referral: siapa yang invite siapa."""
    __tablename__ = 'referrals'
    __table_args__ = (
        UniqueConstraint('referee_id', name='uq_referee_one_referrer'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    referrer_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, index=True)
    referee_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, unique=True)
    status = Column(String(20), default='PENDING', nullable=False)
    # PENDING = referee belum transaksi, COMPLETED = reward sudah diberikan
    reward_idr = Column(BigInteger, default=0)
    completed_at = Column(DateTime, nullable=True)
    notified_at = Column(DateTime, nullable=True)  # (lama) pengundang sudah diberi tahu reward-nya
    # Program v2: jumlah transaksi teman yang sudah dihitung; order referral lama (sudah
    # dibayar dengan skema lama) selesai <= legacy_until tidak dihitung ulang.
    tx_count = Column(Integer, default=0, nullable=False)
    legacy_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReferralEarning(Base):
    """
    Buku besar reward referral. Setiap baris = satu hak saldo untuk satu penerima.
    HELD = masih masa tahan (reward pengundang), RELEASED = sudah masuk saldo bot.
    dedupe_key unik menjamin satu hak hanya tercatat sekali walau diproses bersamaan.
    """
    __tablename__ = 'referral_earnings'

    id = Column(Integer, primary_key=True, autoincrement=True)
    referral_id = Column(Integer, ForeignKey('referrals.id'), nullable=False, index=True)
    beneficiary_id = Column(BigInteger, nullable=False, index=True)  # penerima saldo
    referee_id = Column(BigInteger, nullable=False)
    order_id = Column(String(50), nullable=True, index=True)
    # FIRST_TX, SECOND_TX, FEE_SHARE (pengundang) | REFEREE_BONUS (teman) | COUNTED (penanda 0 rupiah)
    kind = Column(String(20), nullable=False)
    amount_idr = Column(BigInteger, default=0, nullable=False)
    status = Column(String(12), default='HELD', nullable=False, index=True)
    dedupe_key = Column(String(80), unique=True, nullable=False)
    release_at = Column(DateTime, nullable=True, index=True)
    released_at = Column(DateTime, nullable=True)
    notified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ReferralConfig(Base):
    """Konfigurasi referral program (reward amount, enabled flag, dll)."""
    __tablename__ = 'referral_config'

    key = Column(String(50), primary_key=True)
    value = Column(String(200), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Campaign(Base):
    """Campaign & Giveaway: wadah promosi dan bagi-bagi saldo bot."""
    __tablename__ = 'campaigns'

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_code = Column(String(50), unique=True, nullable=False, index=True)
    title = Column(String(150), nullable=False)
    template_type = Column(String(50), default='CUSTOM', nullable=False)
    mode = Column(String(30), nullable=False)  # 'EQUAL_SPLIT', 'RANDOM', 'MILESTONE'
    target_segment = Column(String(30), default='ALL', nullable=False)  # 'ALL', 'BUYERS', 'ACTIVE'
    total_pool = Column(BigInteger, nullable=False)  # Batas maksimal anggaran (Hard Budget Cap)
    max_winners = Column(Integer, nullable=True)  # Batas jumlah pemenang (kuota)
    reward_per_winner = Column(BigInteger, nullable=True)
    milestone_metric = Column(String(30), nullable=True)  # 'VOLUME_IDR', 'TX_COUNT'
    min_metric_value = Column(BigInteger, default=0)
    custom_message = Column(String(1000), nullable=True)  # Template pesan notifikasi custom
    status = Column(String(20), default='DRAFT', nullable=False, index=True)  # 'DRAFT', 'COMPLETED', 'CANCELLED'
    distributed_amount = Column(BigInteger, default=0)  # Total rupiah yang terdistribusi
    distributed_count = Column(Integer, default=0)  # Total user yang menerima
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    executed_at = Column(DateTime, nullable=True)

    # Relationships
    distributions = relationship("CampaignDistribution", back_populates="campaign", cascade="all, delete-orphan")


class MilestoneExclusion(Base):
    """User yang dikecualikan dari peringkat Top Milestone (tetap bebas bertransaksi).

    Tanpa FK ke users: admin boleh memasukkan ID numerik sebelum orangnya pernah /start.
    """
    __tablename__ = 'milestone_exclusions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, unique=True, index=True)
    note = Column(String(200), nullable=True)
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RewardBatch(Base):
    """Kirim Reward ke user pilihan admin: satu batch = satu daftar penerima + nominal masing-masing.

    Status: DRAFT -> RUNNING -> COMPLETED | CANCELLED. Perpindahan DRAFT -> RUNNING dilakukan
    atomik (UPDATE bersyarat) sehingga tombol "Kirim" yang ditekan dua kali tidak membayar dua kali.
    """
    __tablename__ = 'reward_batches'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_by = Column(BigInteger, nullable=False, index=True)
    status = Column(String(15), default='DRAFT', nullable=False, index=True)
    items_json = Column(Text, nullable=False)          # [{telegram_id,label,amount,message}]
    default_message = Column(String(1500), nullable=True)
    total_amount = Column(BigInteger, default=0, nullable=False)
    recipient_count = Column(Integer, default=0, nullable=False)
    result_json = Column(Text, nullable=True)          # hasil per penerima setelah eksekusi
    treasury_before = Column(BigInteger, nullable=True)
    treasury_after = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    executed_at = Column(DateTime, nullable=True)


class CampaignDistribution(Base):
    """Catatan riwayat distribusi saldo per user dalam campaign."""
    __tablename__ = 'campaign_distributions'
    __table_args__ = (
        UniqueConstraint('campaign_id', 'telegram_id', name='uq_campaign_user'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, ForeignKey('campaigns.id'), nullable=False, index=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, index=True)
    amount_idr = Column(BigInteger, nullable=False)
    rank = Column(Integer, nullable=True)
    metric_value = Column(BigInteger, nullable=True)
    status = Column(String(20), default='SUCCESS', nullable=False)
    notified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    campaign = relationship("Campaign", back_populates="distributions")


class UserSavedWallet(Base):
    """Menyimpan alamat wallet crypto milik user agar bisa dipilih instan saat Beli."""
    __tablename__ = 'user_saved_wallets'

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, index=True)
    label = Column(String(50), nullable=True)
    network = Column(String(30), nullable=True)      # BSC, ETH, SOLANA, TRON, SUI, TON, BTC, dll.
    chain_type = Column(String(20), nullable=True)   # 'EVM', 'SOLANA', 'TRON', 'SUI', 'TON', 'BITCOIN'
    wallet_address = Column(String(250), nullable=False)
    is_default = Column(Boolean, default=False)      # Wallet default untuk chain_type ini
    auto_detected = Column(Boolean, default=False)   # True jika jaringan dideteksi otomatis dari format alamat
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    user = relationship("User", backref="saved_wallets")


class UserSavedBank(Base):
    """Menyimpan rekening bank & e-wallet user untuk pencairan dana saat Jual."""
    __tablename__ = 'user_saved_banks'

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, index=True)
    bank_name = Column(String(50), nullable=False)  # BCA, Mandiri, BRI, BNI, Seabank, GoPay, OVO, DANA, ShopeePay, etc.
    account_number = Column(String(50), nullable=False)  # Nomor rekening atau nomor HP e-wallet
    account_name = Column(String(100), nullable=False)  # Nama pemilik rekening
    account_type = Column(String(20), default='BANK', nullable=False)  # 'BANK' atau 'EWALLET'
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    user = relationship("User", backref="saved_banks")


class WithdrawRequest(Base):
    """Permintaan penarikan saldo IDR ke rekening/e-wallet. Saldo dipotong saat dibuat; refund bila ditolak."""
    __tablename__ = 'withdraw_requests'

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, index=True)
    amount_idr = Column(BigInteger, nullable=False)
    # Snapshot tujuan: tetap benar walau rekening tersimpan kemudian dihapus user.
    bank_name = Column(String(50), nullable=False)
    account_number = Column(String(50), nullable=False)
    account_name = Column(String(100), nullable=False)
    status = Column(String(15), default='PENDING', nullable=False, index=True)  # PENDING, PAID, REJECTED
    handled_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    handled_at = Column(DateTime, nullable=True)


# ─────────────────────────────────────────────────────────
#  Phase 7: Advanced Rewards, Loyalty & Enhanced Wallet
# ─────────────────────────────────────────────────────────

class ReferralDiscount(Base):
    """
    Pelacak sisa quota diskon 7% untuk referrer yang berhasil mengundang user baru.
    Dibuat otomatis saat referee menyelesaikan transaksi pertama.
    """
    __tablename__ = 'referral_discounts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, unique=True, index=True)
    remaining_uses = Column(Integer, default=10, nullable=False)   # Sisa slot diskon (mulai dari 10)
    discount_pct = Column(Numeric(5, 2), default=7.0, nullable=False)  # Default 7%
    activated_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)    # Opsional: bisa dibatasi waktu oleh admin
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    user = relationship("User", backref="referral_discount")


class LoyaltyReward(Base):
    """
    Reward loyalitas berbasis time-window: jika user transaksi >= N kali
    dalam rentang M hari, maka mendapat reward saldo bot.
    Setiap user bisa punya banyak windows (satu per periode).
    """
    __tablename__ = 'loyalty_rewards'

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, index=True)
    window_start = Column(DateTime, nullable=False)    # Hari ke-1 transaksi pertama dalam window ini
    window_end = Column(DateTime, nullable=False)      # Batas akhir window (window_start + M hari)
    tx_count_in_window = Column(Integer, default=0)   # Jumlah transaksi yang sudah tercatat
    qualified = Column(Boolean, default=False)         # True = sudah memenuhi syarat (>= min_tx_count)
    reward_idr = Column(BigInteger, nullable=True)     # Besaran reward yang diberikan (setelah lolos)
    rewarded_at = Column(DateTime, nullable=True)      # Kapan reward dikreditkan
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationship
    user = relationship("User", backref="loyalty_rewards")


class LoyaltyConfig(Base):
    """Konfigurasi program loyalty yang dapat diubah oleh admin via panel."""
    __tablename__ = 'loyalty_config'

    # Default keys: loyalty_enabled, window_days, min_tx_count, reward_amount_idr, min_tx_amount_idr
    key = Column(String(50), primary_key=True)
    value = Column(String(200), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
