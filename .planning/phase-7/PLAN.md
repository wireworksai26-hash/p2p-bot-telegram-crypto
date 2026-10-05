# PLAN — Phase 7: Advanced Rewards, Loyalty & Enhanced Wallet

> **Phase:** 7
> **Priority:** 🔴 High
> **Estimated Effort:** ~14-18 jam development + testing
> **Dependencies:** Phase 3 (Referral system — tabel `referrals`, `referral_config` sudah ada)

---

## 1. Konteks Bisnis

Phase ini mencakup 5 fitur baru:

| # | Fitur | Deskripsi Singkat |
|---|-------|-------------------|
| 7.A | **Referral Discount** | Referrer dapat 10% potongan per transaksi, selama 10x transaksi berikutnya |
| 7.B | **Top Spender Milestone** | Reward otomatis ke top 10 spender berdasarkan total volume beli/jual |
| 7.C | **Random Winner Draw** | Admin pilih pool, sistem undi N pemenang random, saldo langsung dikreditkan |
| 7.D | **Time-based Loyalty** | User yang transaksi >= 5x dalam rentang 5 hari mendapat reward giveaway |
| 7.E | **Enhanced Wallet Management** | Auto-detect jaringan wallet, pengelompokan per jaringan, multi-network support |

---

## 2. Database Schema

### Task 7.1 — Model: ReferralDiscount
**File:** `database/models.py`

Tambah model baru untuk melacak discount quota referral per user:

```python
class ReferralDiscount(Base):
    """Pelacak sisa quota diskon 10% untuk referrer aktif."""
    __tablename__ = 'referral_discounts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, unique=True, index=True)
    remaining_uses = Column(Integer, default=10, nullable=False)  # Sisa transaksi diskon (max 10)
    discount_pct = Column(Numeric(5, 2), default=10.0, nullable=False)  # Default 10%
    activated_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)  # Opsional: bisa dibatasi waktu
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="referral_discount")
```

### Task 7.2 — Model: LoyaltyReward
**File:** `database/models.py`

```python
class LoyaltyReward(Base):
    """Reward loyalitas berbasis waktu: 5 transaksi dalam rentang hari tertentu."""
    __tablename__ = 'loyalty_rewards'

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, index=True)
    window_start = Column(DateTime, nullable=False)   # Mulai hitung (hari ke-1 transaksi)
    window_end = Column(DateTime, nullable=False)     # Batas akhir window (+ N hari)
    tx_count_in_window = Column(Integer, default=0)  # Jumlah transaksi yang tercatat
    qualified = Column(Boolean, default=False)        # True jika sudah memenuhi syarat
    reward_idr = Column(BigInteger, nullable=True)    # Besaran reward yang diberikan
    rewarded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", backref="loyalty_rewards")
```

### Task 7.3 — Model: LoyaltyConfig
**File:** `database/models.py`

```python
class LoyaltyConfig(Base):
    """Konfigurasi program loyalty (bisa diubah admin)."""
    __tablename__ = 'loyalty_config'

    key = Column(String(50), primary_key=True)
    value = Column(String(200), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

Default rows:
- `loyalty_enabled` = `true`
- `window_days` = `5`
- `min_tx_count` = `5`
- `reward_amount_idr` = `25000`
- `min_tx_amount_idr` = `50000`

### Task 7.4 — Kolom Tambahan pada Order
**File:** `database/models.py`

```python
# Di dalam class Order, tambah kolom:
referral_discount_applied = Column(Boolean, default=False)
referral_discount_pct = Column(Numeric(5, 2), nullable=True)
discount_amount_idr = Column(BigInteger, default=0)
```

### Task 7.5 — Kolom Tambahan pada UserSavedWallet
**File:** `database/models.py`

```python
# Tambah kolom baru:
chain_type = Column(String(20), nullable=True)  # 'EVM', 'SOLANA', 'TRON', 'SUI', 'TON', 'BITCOIN'
is_default = Column(Boolean, default=False)
auto_detected = Column(Boolean, default=False)
```

---

## 3. CRUD Functions

### Task 7.6 — CRUD: Referral Discount
**File:** `database/crud.py`

```python
def get_referral_discount(db, telegram_id: int) -> ReferralDiscount | None
    """Ambil data diskon aktif milik user."""

def activate_referral_discount(db, telegram_id: int, uses: int = 10, pct: float = 10.0) -> ReferralDiscount
    """Aktifkan diskon referral untuk user."""

def consume_referral_discount(db, telegram_id: int) -> float | None
    """Gunakan 1 slot diskon. Return nilai diskon (%) atau None jika tidak ada."""

def get_referral_discount_info(db, telegram_id: int) -> dict
    """Return: {active: bool, remaining: int, discount_pct: float}"""
```

### Task 7.7 — CRUD: Loyalty Reward
**File:** `database/crud.py`

```python
def get_or_create_loyalty_window(db, telegram_id: int, window_days: int = 5) -> LoyaltyReward
    """Ambil window loyalty aktif user atau buat window baru."""

def increment_loyalty_tx(db, telegram_id: int, tx_amount_idr: int) -> dict
    """
    Tambah 1 hitungan transaksi ke window aktif.
    Return: {qualified: bool, current_count: int, needed: int, window_end: datetime}
    """

def get_loyalty_config(db, key: str) -> str | None
def set_loyalty_config(db, key: str, value: str) -> None

def get_loyalty_eligible_users(db, window_days: int, min_tx: int) -> list[dict]
    """Ambil semua user yang eligible untuk loyalty reward."""
```

### Task 7.8 — CRUD: Top Spender
**File:** `database/crud.py`

```python
def get_top_spenders(db, limit: int = 10, period_days: int = 30) -> list[dict]
    """
    Ambil top N spender berdasarkan total_spent_idr dalam periode tertentu.
    Return: [{rank, telegram_id, username, full_name, total_spent_idr}, ...]
    """
```

### Task 7.9 — CRUD: Random Winner
**File:** `database/crud.py`

```python
def get_random_winners(db, pool_segment: str, count: int,
                       min_tx_amount: int = 0) -> list[dict]
    """
    Pilih N pemenang random dari pool.
    pool_segment: 'ALL' | 'BUYERS' | 'ACTIVE_30D'
    Return: [{telegram_id, username, full_name}, ...]
    """
```

### Task 7.10 — CRUD: Enhanced Wallet
**File:** `database/crud.py`

```python
def get_saved_wallets_by_network(db, telegram_id: int, network: str) -> list[UserSavedWallet]
def get_saved_wallets_grouped(db, telegram_id: int) -> dict[str, list]
    """Return dict: {chain_type: [wallets...]}"""
def set_default_wallet(db, wallet_id: int, telegram_id: int) -> bool
```

---

## 4. Services

### Task 7.11 — Service: referral_discount_service.py (baru)
**File:** `services/referral_discount_service.py`

```python
REFERRAL_DISCOUNT_PCT = 10.0
REFERRAL_DISCOUNT_USES = 10

def calculate_discounted_fee(base_fee_idr: int, discount_pct: float) -> tuple[int, int]:
    """Return: (discounted_fee, discount_amount)"""

async def apply_referral_discount_if_eligible(telegram_id: int, base_fee_idr: int, db) -> dict:
    """
    Cek apakah user punya diskon aktif.
    Return: {applied: bool, discount_pct: float, discount_amount: int, final_fee: int}
    """

async def notify_discount_used(bot, telegram_id: int, remaining: int, discount_amount: int): ...
async def notify_discount_exhausted(bot, telegram_id: int): ...
```

### Task 7.12 — Service: loyalty_service.py (baru)
**File:** `services/loyalty_service.py`

```python
async def process_loyalty_after_order(telegram_id: int, order_amount_idr: int, bot, db):
    """
    Dipanggil setelah setiap order COMPLETED.
    1. Ambil/buat loyalty window aktif
    2. Increment tx count
    3. Jika qualified: kredit reward, kirim notifikasi
    """

async def run_loyalty_check_job(bot, db):
    """Cek window yang expired dan belum direward."""

async def notify_loyalty_reward(bot, telegram_id: int, reward_idr: int, tx_count: int): ...
async def notify_loyalty_progress(bot, telegram_id: int, current: int, needed: int, days_left: int): ...
```

### Task 7.13 — Update: campaign_service.py
**File:** `services/campaign_service.py`

```python
TOP_SPENDER_REWARDS = {
    1: 150_000,
    2: 100_000,
    3: 50_000,
    4: 30_000,
    5: 25_000,
    6: 20_000, 7: 20_000, 8: 20_000, 9: 20_000, 10: 20_000,
}

async def execute_top_spender_campaign(campaign_id: int, bot, db) -> dict:
    """Kredit reward ke top 10 spender, kirim notifikasi."""

async def execute_random_winner_campaign(
    campaign_id: int, pool_segment: str, winner_count: int,
    reward_per_winner: int, bot, db
) -> dict:
    """Pilih N pemenang random, kredit saldo, kirim notifikasi."""
```

### Task 7.14 — Service: wallet_detector.py (baru)
**File:** `services/wallet_detector.py`

```python
NETWORK_PATTERNS = {
    'EVM':     {'chains': ['BSC','ETH','POLYGON','ARBITRUM','BASE'], 'pattern': r'^0x[a-fA-F0-9]{40}$'},
    'SOLANA':  {'chains': ['SOLANA'],  'pattern': r'^[1-9A-HJ-NP-Za-km-z]{32,44}$'},
    'TRON':    {'chains': ['TRC20'],   'pattern': r'^T[a-zA-Z0-9]{33}$'},
    'SUI':     {'chains': ['SUI'],     'pattern': r'^0x[a-fA-F0-9]{64}$'},
    'TON':     {'chains': ['TON'],     'pattern': r'^(EQ|UQ)[a-zA-Z0-9_-]{46}$'},
    'BITCOIN': {'chains': ['BTC'],     'pattern': r'^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}$'},
}

def detect_wallet_network(address: str) -> dict | None:
    """Return: {chain_type: str, possible_chains: list[str]} atau None."""

def validate_wallet_address(address: str, network: str) -> bool:
    """Validasi format alamat sesuai jaringan."""
```

---

## 5. Bot Handlers

### Task 7.15 — Referral Menu: Tampilkan Status Diskon
**File:** `bot/handlers/referral.py`

Tambah section ke tampilan menu referral:

```
📢 PROGRAM REFERRAL — REWARD & DISKON

🔗 Link Referral: https://t.me/Hsnpro_bot?start=ref_123456789

📊 Statistik:
├── 👥 Total Ajakan    : 5 orang
├── ✅ Sudah Transaksi : 3 orang
└── 💰 Total Reward    : Rp 15.000

🎁 Status Diskon Transaksi:
├── Status : ✅ AKTIF
├── Diskon : 10% dari biaya transaksi
└── Sisa   : 7 dari 10 transaksi

💡 Diskon otomatis diterapkan saat Anda transaksi.
```

### Task 7.16 — Admin: Submenu Top Spender
**File:** `bot/handlers/admin.py`

Tambah tombol "🏆 Top Spender" di Admin Dashboard. Tampilan:

```
🏆 TOP SPENDER — LEADERBOARD
Periode: 30 hari terakhir | Pool Hadiah: Rp 545.000

Rank | User         | Volume         | Hadiah
  1  | @user1       | Rp 25.500.000  | Rp 150.000
  2  | @user2       | Rp 18.200.000  | Rp 100.000
  ...

[💰 Bagikan Hadiah] [📊 Ganti Periode] [🔙 Admin]
```

Tombol "💰 Bagikan Hadiah" → konfirmasi → `execute_top_spender_campaign()`.

### Task 7.17 — Admin: Submenu Random Winner Draw
**File:** `bot/handlers/admin.py`

Tambah tombol "🎲 Undi Pemenang" di Admin Dashboard. Flow:
1. Pilih pool: `[Semua User]` `[Pernah Beli]` `[Aktif 30 Hari]`
2. Input jumlah pemenang
3. Input reward per pemenang (IDR)
4. Konfirmasi → eksekusi → tampilkan hasil

```
🎲 HASIL UNDIAN ACAK
Pool: Aktif 30 Hari (127 peserta)

1. @alice    → Rp 50.000 ✅
2. @bob      → Rp 50.000 ✅
3. @charlie  → Rp 50.000 ✅

Total: Rp 150.000 | Notif: 3/3
```

### Task 7.18 — Admin: Submenu Loyalty Config
**File:** `bot/handlers/admin.py`

Tambah tombol "⏳ Loyalty Reward" di Admin Dashboard:

```
⏳ PENGATURAN LOYALTY REWARD
Status : ✅ AKTIF
Syarat : 5 transaksi dalam 5 hari
Reward : Rp 25.000 per user
Min. Nominal: Rp 50.000/transaksi

[✏️ Edit Syarat] [💰 Edit Reward] [📊 User Eligible] [🔙 Admin]
```

### Task 7.19 — Enhanced Wallet UI
**File:** `bot/handlers/saved_accounts.py`

Daftar wallet dikelompokkan per chain:

```
💼 WALLET TERSIMPAN

🔷 EVM (BSC / ETH / POLYGON)
  1. 0xABCD...1234 [BSC] ⭐ Default
  2. 0xEFGH...5678 [ETH]

🟣 SOLANA
  3. 7Xki...mnop [SOLANA]

🔴 TRON
  4. TYui...qrst [TRC20]

[➕ Tambah EVM]    [➕ Tambah Solana]
[➕ Tambah TRON]   [➕ Tambah TON]
[➕ Tambah SUI]    [🔙 Kembali]
```

Saat input alamat EVM → konfirmasi pilih chain spesifik: `[BSC] [ETH] [Polygon] [Arbitrum]`

Saat Buy Flow, hanya wallet sesuai jaringan transaksi yang ditampilkan sebagai pilihan.

---

## 6. Integration Points

### Task 7.20 — Hook di Order Completion
**File:** `services/payout_watchdog.py`

```python
# Setelah mark order COMPLETED:
if order.status == 'COMPLETED':
    await process_loyalty_after_order(
        telegram_id=order.telegram_id,
        order_amount_idr=int(order.total_idr),
        bot=bot,
        db=db
    )
    await _check_and_trigger_referral_reward(order.telegram_id, bot)
```

### Task 7.21 — Hook di Fee Calculation (Referral Discount)
**File:** `bot/handlers/buy.py`

```python
# Saat quote/confirm order:
discount_info = await apply_referral_discount_if_eligible(
    telegram_id=user_id, base_fee_idr=fee_idr, db=db
)
if discount_info['applied']:
    fee_idr = discount_info['final_fee']
    # Tampilkan: "Fee: Rp 5.000 → Rp 4.500 (diskon 10% referral, sisa 6 kali)"
```

### Task 7.22 — Database Migration
**File:** `main.py`

Fungsi `_migrate_phase7_schema(engine)`:
- CREATE TABLE IF NOT EXISTS: `referral_discounts`, `loyalty_rewards`, `loyalty_config`
- ALTER TABLE `orders`: tambah kolom discount
- ALTER TABLE `user_saved_wallets`: tambah kolom `chain_type`, `is_default`, `auto_detected`
- INSERT OR IGNORE default `loyalty_config` rows

---

## 7. File yang Diubah / Dibuat

| File | Aksi | Deskripsi |
|------|------|-----------|
| `database/models.py` | MODIFY | + `ReferralDiscount`, `LoyaltyReward`, `LoyaltyConfig`; kolom baru di `Order` & `UserSavedWallet` |
| `database/crud.py` | MODIFY | + CRUD untuk referral discount, loyalty, top spender, random winner, enhanced wallet |
| `services/referral_discount_service.py` | CREATE | Logic diskon 10% per transaksi untuk referrer |
| `services/loyalty_service.py` | CREATE | Logic reward loyalitas berbasis time-window |
| `services/campaign_service.py` | MODIFY | + `execute_top_spender_campaign()`, `execute_random_winner_campaign()` |
| `services/wallet_detector.py` | CREATE | Auto-detect chain dari format alamat wallet |
| `bot/handlers/referral.py` | MODIFY | + tampilan status diskon aktif |
| `bot/handlers/admin.py` | MODIFY | + submenu Top Spender, Undi Pemenang, Loyalty Config |
| `bot/handlers/saved_accounts.py` | MODIFY | + grouping per chain, per-network buttons, auto-detect |
| `bot/handlers/buy.py` | MODIFY | + apply referral discount saat fee calculation |
| `services/payout_watchdog.py` | MODIFY | + trigger loyalty & referral check setelah COMPLETED |
| `main.py` | MODIFY | + `_migrate_phase7_schema()`, import & register handlers baru |
| `tests/test_phase7.py` | CREATE | Unit & integration tests seluruh fitur Phase 7 |

---

## 8. Urutan Eksekusi (Wave)

```
Wave 1 (Paralel — no dependencies):
  ├── Task 7.1-7.5  → database/models.py (schema baru)
  ├── Task 7.14     → services/wallet_detector.py (pure utility)
  └── Task 7.13 (constants) → campaign_service.py TOP_SPENDER_REWARDS dict

Wave 2 (Butuh Wave 1):
  ├── Task 7.6-7.10 → database/crud.py (butuh model)
  └── Task 7.22     → main.py migration (butuh model)

Wave 3 (Butuh Wave 2):
  ├── Task 7.11     → services/referral_discount_service.py
  ├── Task 7.12     → services/loyalty_service.py
  └── Task 7.13 (complete) → campaign_service.py execute functions

Wave 4 (Butuh Wave 3):
  ├── Task 7.15     → referral.py (butuh discount service)
  ├── Task 7.16-18  → admin.py submenus (butuh campaign/loyalty service)
  ├── Task 7.19     → saved_accounts.py (butuh wallet_detector)
  └── Task 7.20-21  → payout_watchdog.py + buy.py integration hooks

Wave 5 (Terakhir):
  └── tests/test_phase7.py
```

---

## 9. Verification Checklist

### 7.A — Referral Discount
- [ ] Setelah referral berhasil, `ReferralDiscount` dibuat untuk referrer (`remaining_uses=10`)
- [ ] Saat referrer melakukan order berikutnya, fee dipotong 10%
- [ ] Quote order menampilkan: `Fee: Rp X → Rp Y (diskon 10% referral, sisa Z kali)`
- [ ] Setelah 10x transaksi, diskon otomatis tidak berlaku
- [ ] Notifikasi: "Diskon referral Anda telah habis (10/10 transaksi)"
- [ ] Menu referral user menampilkan status dan sisa kuota diskon

### 7.B — Top Spender Milestone
- [ ] Admin bisa melihat leaderboard top 10 spender dari panel admin
- [ ] Admin bisa klik "Bagikan Hadiah" → konfirmasi → eksekusi
- [ ] Reward dikreditkan sesuai tier (Top 1: 150k, Top 2: 100k, dst.)
- [ ] Setiap pemenang mendapat notifikasi beserta ranknya
- [ ] `CampaignDistribution` records dibuat untuk audit

### 7.C — Random Winner Draw
- [ ] Admin bisa pilih pool (All / Buyers / Active 30D)
- [ ] Admin input jumlah pemenang dan reward per pemenang
- [ ] Sistem memilih N pemenang secara random tanpa duplikat
- [ ] Saldo dikreditkan ke pemenang
- [ ] Pemenang mendapat notifikasi bahwa mereka menang
- [ ] Admin melihat daftar pemenang + status notifikasi

### 7.D — Time-based Loyalty
- [ ] Setiap order COMPLETED memanggil `process_loyalty_after_order()`
- [ ] Sistem menghitung transaksi dalam window 5 hari
- [ ] User yang capai 5 transaksi dalam 5 hari → reward Rp 25.000
- [ ] User mendapat notifikasi reward loyalty
- [ ] Admin bisa ubah: `window_days`, `min_tx_count`, `reward_amount_idr`
- [ ] User tidak bisa klaim reward loyalty dua kali untuk window yang sama

### 7.E — Enhanced Wallet Management
- [ ] Saat input alamat wallet, sistem auto-detect chain (EVM/Solana/TRON/SUI/TON)
- [ ] Jika EVM terdeteksi, user pilih chain spesifik (BSC/ETH/POLYGON/dll)
- [ ] Tombol "Tambah Wallet" dipisah per chain
- [ ] Daftar wallet tersimpan dikelompokkan per `chain_type`
- [ ] Saat Buy, hanya wallet sesuai jaringan transaksi yang ditampilkan
- [ ] User bisa set wallet sebagai Default untuk chain tertentu
- [ ] Validasi format alamat sebelum disimpan
