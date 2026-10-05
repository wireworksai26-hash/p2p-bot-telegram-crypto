# PLAN — Phase 3: Referral Program

> **Phase:** 3 of 6
> **Priority:** 🟡 Medium
> **Estimated Effort:** ~6-8 jam development + testing
> **Dependencies:** Phase 1 (admin credit balance, digunakan untuk reward payout)

---

## 1. Konteks Bisnis

Sistem referral untuk akuisisi user baru:
- Setiap user punya link referral unik: `t.me/Hsnpro_bot?start=ref_<TELEGRAM_ID>`
- Referrer mendapat reward saldo bot ketika referee **menyelesaikan transaksi pertama** (bukan saat join saja — anti abuse)
- Reward default: Rp 5.000 per referral berhasil (configurable oleh admin)
- Admin bisa lihat leaderboard top referrers

**Tidak ada fitur referral sama sekali di codebase saat ini** — ini build from scratch.

---

## 2. Database Schema

### Task 3.1 — Model `Referral`
**File:** [`database/models.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/database/models.py)

```python
class Referral(Base):
    __tablename__ = 'referrals'
    __table_args__ = (
        UniqueConstraint('referee_id', name='uq_referee_one_referrer'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    referrer_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, index=True)
    referee_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False, unique=True)
    status = Column(String(20), default='PENDING', nullable=False)
    # PENDING = referee belum transaksi, COMPLETED = reward sudah diberikan, EXPIRED = 30 hari tanpa transaksi
    reward_idr = Column(BigInteger, default=0)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
```

### Task 3.2 — Model `ReferralConfig`
**File:** [`database/models.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/database/models.py)

```python
class ReferralConfig(Base):
    __tablename__ = 'referral_config'

    key = Column(String(50), primary_key=True)
    value = Column(String(200), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

Default rows:
- `reward_per_referral` = `5000` (Rp 5.000)
- `referral_enabled` = `true`
- `max_referrals_per_user` = `100`
- `reward_trigger` = `first_completed_order`

---

## 3. Task Breakdown

### Task 3.3 — CRUD Functions
**File:** [`database/crud.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/database/crud.py)

```python
def create_referral(db, referrer_id: int, referee_id: int) -> Referral | None
def get_referral_by_referee(db, referee_id: int) -> Referral | None
def complete_referral(db, referee_id: int) -> bool  # Mark COMPLETED + credit reward
def get_referral_stats(db, referrer_id: int) -> dict  # {total, completed, pending, total_reward}
def get_top_referrers(db, limit: int = 10) -> list  # Leaderboard
def get_referral_config(db, key: str) -> str | None
def set_referral_config(db, key: str, value: str) -> None
```

### Task 3.4 — Deep-Link Handler di `/start`
**File:** [`bot/handlers/start.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/start.py)

Modify `start_handler` untuk detect deep-link referral:

```python
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args  # ['ref_123456789'] jika dari deep-link
    
    if args and args[0].startswith("ref_"):
        referrer_id = int(args[0].replace("ref_", ""))
        referee_id = update.effective_user.id
        
        if referrer_id != referee_id:  # Tidak bisa refer diri sendiri
            db = SessionLocal()
            try:
                existing = get_referral_by_referee(db, referee_id)
                if not existing:
                    create_referral(db, referrer_id, referee_id)
                    # Kirim notifikasi ke referrer
                    await context.bot.send_message(
                        referrer_id,
                        f"🎉 User baru bergabung via link referral Anda!\n"
                        f"Reward akan diberikan setelah mereka transaksi pertama."
                    )
            finally:
                db.close()
    
    # Lanjut flow /start biasa...
```

### Task 3.5 — Auto-Reward Trigger
**File:** [`bot/handlers/buy.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/buy.py) (dan `sell.py`, `swap.py`)

Setelah order pertama COMPLETED, trigger reward:

```python
# Di akhir flow order COMPLETED:
async def _check_and_trigger_referral_reward(telegram_id: int, bot):
    db = SessionLocal()
    try:
        referral = get_referral_by_referee(db, telegram_id)
        if referral and referral.status == 'PENDING':
            success = complete_referral(db, telegram_id)
            if success:
                reward = referral.reward_idr
                await bot.send_message(
                    referral.referrer_id,
                    f"🎉 Referral reward! User yang Anda ajak sudah transaksi.\n"
                    f"Saldo Anda bertambah {format_idr(reward)}!"
                )
    finally:
        db.close()
```

### Task 3.6 — Menu "Referral" untuk User
**File:** `bot/handlers/referral.py` (baru)

Tampilan saat user klik tombol "📢 Referral":

```
📢 PROGRAM REFERRAL HSN STORE

🔗 Link Referral Anda:
https://t.me/Hsnpro_bot?start=ref_123456789

📊 Statistik Referral:
├── 👥 Total Ajakan : 5 orang
├── ✅ Sudah Transaksi: 3 orang  
├── ⏳ Belum Transaksi: 2 orang
└── 💰 Total Reward  : Rp 15.000

💡 Bagikan link di atas ke teman Anda.
Anda mendapat Rp 5.000 untuk setiap teman yang bertransaksi!

[📋 Salin Link] [📊 Leaderboard] [🏠 Menu Utama]
```

### Task 3.7 — Tombol Referral di Main Menu
**File:** [`bot/keyboards/main_menu.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/keyboards/main_menu.py)

Tambah tombol di `get_main_menu_keyboard()`:

```python
# Baris baru sebelum "Hubungi Owner":
InlineKeyboardButton("Program Referral", callback_data="menu_referral", 
                     icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("LINK", "...")),
```

### Task 3.8 — Admin: Referral Management
**File:** [`bot/handlers/admin.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py)

Di admin dashboard, tambah tombol "📢 Referral Stats":
- Tampilkan top 10 referrers (leaderboard)
- Setting reward amount: `/setreferral reward 10000`
- Enable/disable referral: `/setreferral enabled true|false`

### Task 3.9 — Schema Migration
**File:** [`main.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/main.py)

Tambah `_migrate_referral_schema()` yang create tabel `referrals` dan `referral_config` + seed default config.

### Task 3.10 — Register Handlers
**File:** [`main.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/main.py)

```python
from bot.handlers.referral import referral_menu_handler
from bot.handlers.admin import setreferral_handler

application.add_handler(CommandHandler("referral", referral_menu_handler))
application.add_handler(CommandHandler("setreferral", setreferral_handler))
```

---

## 4. File yang Diubah

| File | Aksi | Deskripsi |
|------|------|-----------|
| `database/models.py` | MODIFY | + `Referral`, `ReferralConfig` models |
| `database/crud.py` | MODIFY | + CRUD referral functions |
| `bot/handlers/referral.py` | CREATE | Menu referral untuk user |
| `bot/handlers/start.py` | MODIFY | Deep-link detection `ref_<ID>` |
| `bot/handlers/buy.py` | MODIFY | Trigger referral reward setelah order pertama |
| `bot/handlers/sell.py` | MODIFY | Trigger referral reward |
| `bot/handlers/swap.py` | MODIFY | Trigger referral reward |
| `bot/handlers/admin.py` | MODIFY | + referral stats, admin config |
| `bot/keyboards/main_menu.py` | MODIFY | + tombol "Program Referral" |
| `main.py` | MODIFY | + migration, import, register handlers |
| `tests/test_referral.py` | CREATE | Unit tests referral |

---

## 5. Anti-Abuse Measures

| Threat | Mitigation |
|--------|------------|
| Self-referral (user refer dirinya sendiri) | Check `referrer_id != referee_id` |
| Fake account farming (buat banyak akun untuk klaim reward) | Reward hanya setelah transaksi COMPLETED (beli/jual/swap real) |
| Satu referee di-refer ulang oleh referrer lain | UniqueConstraint `referee_id` — satu referee hanya bisa punya 1 referrer |
| Referrer spam mengumpulkan 1000+ referral | `max_referrals_per_user` cap (default 100) |
| Referral link shared di public channel tanpa kontrol | Monitoring via leaderboard, admin bisa ban |

---

## 6. Verification Checklist

- [ ] User A share link `t.me/Hsnpro_bot?start=ref_A_ID`
- [ ] User B klik link → bot /start biasa + referral record tercatat
- [ ] User A dapat notifikasi "user baru bergabung via referral"
- [ ] User B selesai transaksi pertama → User A dapat reward Rp 5.000
- [ ] User A cek menu Referral → statistik benar
- [ ] User B tidak bisa di-refer ulang oleh user C
- [ ] Admin lihat leaderboard top referrers
- [ ] Admin ubah reward amount via `/setreferral reward 10000`
- [ ] Self-referral ditolak
