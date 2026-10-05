# PLAN — Phase 1: Admin Credit Balance (Isi Saldo Buyer)

> **Phase:** 1 of 6
> **Priority:** 🔴 Highest — Blocking giveaway campaign launch
> **Estimated Effort:** ~4-6 jam development + testing
> **Dependencies:** None (existing `credit_user_balance()` in `database/crud.py` already works)

---

## 1. Konteks Bisnis

Client (Dan) ingin menjalankan campaign giveaway:
- Winner giveaway mendapat saldo bot (misal Rp 10.000/orang)
- Saldo ini **bukan rupiah tunai** — jadi credit internal bot untuk transaksi beli crypto
- Admin perlu cara cepat untuk mengisi saldo ke multiple winner
- Butuh audit trail untuk accountability

**Prasyarat database sudah terpenuhi:**
- Model `User` sudah punya kolom `balance_idr` (Numeric 15,2) → [models.py:15](file:///d:/Project_Test/p2p-crypto-telegram-bot/database/models.py#L15)
- CRUD `credit_user_balance()` sudah ada → [crud.py:915-932](file:///d:/Project_Test/p2p-crypto-telegram-bot/database/crud.py#L915-L932)
- CRUD `get_user_balance()` sudah ada → [crud.py:907-912](file:///d:/Project_Test/p2p-crypto-telegram-bot/database/crud.py#L907-L912)
- Model `AuditLog` sudah ada → [models.py:156-166](file:///d:/Project_Test/p2p-crypto-telegram-bot/database/models.py#L156-L166)

---

## 2. Task Breakdown

### Task 1.1 — Command `/credit` (Admin-Only)
**File:** [`bot/handlers/admin.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py)

Buat handler baru `credit_balance_handler`:

```python
async def credit_balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /credit <telegram_id> <jumlah_idr> [keterangan]
    Contoh: /credit 123456789 10000 Giveaway Winner Oktober
    """
```

**Logika:**
1. Validasi admin (`is_admin(user_id)`)
2. Parse args: `telegram_id` (int), `jumlah_idr` (int, min 1.000, max 10.000.000)
3. Cek user ada di database (`get_user()`)
4. Tampilkan konfirmasi: "Isi saldo Rp 10.000 ke @username (ID: 123456789)? Saldo saat ini: Rp 0"
5. Inline button: `[✅ Konfirmasi]` `[❌ Batal]`
6. Setelah konfirmasi:
   - Panggil `credit_user_balance(db, telegram_id, amount_idr)`
   - Tulis `AuditLog` (action: `ADMIN_CREDIT_BALANCE`, details: jumlah + keterangan)
   - Kirim notifikasi ke user penerima: "🎉 Saldo Anda bertambah Rp 10.000 dari Admin! Saldo sekarang: Rp 10.000"
   - Reply ke admin: "✅ Berhasil isi saldo Rp 10.000 ke @username"

**Callback data pattern:** `admin_credit_confirm_{telegram_id}_{amount}` dan `admin_credit_cancel`

### Task 1.2 — Command `/bulkcredit` (Admin-Only)
**File:** [`bot/handlers/admin.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py)

```python
async def bulkcredit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /bulkcredit <jumlah_idr> <id1> <id2> <id3> ...
    Contoh: /bulkcredit 10000 123456789 987654321 555666777
    """
```

**Logika:**
1. Validasi admin
2. Parse: nominal (pertama), lalu list telegram_id
3. Konfirmasi: "Isi saldo Rp 10.000 ke 3 user?"
4. Loop credit satu per satu, kumpulkan hasil (sukses/gagal per user)
5. Summary report: "✅ 3/3 berhasil. Total dikeluarkan: Rp 30.000"

### Task 1.3 — Tombol Admin Dashboard
**File:** [`bot/handlers/admin.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py)

Tambah tombol di `get_admin_dashboard_keyboard()` (line 38-68):

```python
InlineKeyboardButton("💳 Isi Saldo User", callback_data="admin_panel_credit"),
```

Posisi: di baris baru antara "👥 Kelola User" dan "📢 Broadcast Pesan", atau di baris bersama "👥 Kelola User".

Handler callback `admin_panel_credit` tampilkan panduan:
```
💳 ISI SALDO USER (ADMIN CREDIT)

Gunakan perintah berikut untuk mengisi saldo buyer:

▸ Satu user:
  /credit <telegram_id> <jumlah_idr> [keterangan]
  Contoh: /credit 123456789 10000 Winner Giveaway

▸ Banyak user sekaligus:
  /bulkcredit <jumlah_idr> <id1> <id2> ...
  Contoh: /bulkcredit 10000 123456789 987654321
```

### Task 1.4 — Register Handler di `main.py`
**File:** [`main.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/main.py)

```python
# Di import section (line ~45-62):
from bot.handlers.admin import credit_balance_handler, bulkcredit_handler

# Di register section (line ~362):
application.add_handler(CommandHandler("credit", credit_balance_handler))
application.add_handler(CommandHandler("bulkcredit", bulkcredit_handler))
```

### Task 1.5 — Unit Tests
**File:** `tests/test_admin_credit.py` (baru)

Test cases:
- `test_credit_non_admin_rejected` — non-admin tidak bisa /credit
- `test_credit_valid_user` — credit berhasil, saldo bertambah
- `test_credit_unknown_user` — user tidak ditemukan di DB
- `test_credit_invalid_amount` — jumlah negatif / 0 / terlalu besar
- `test_bulkcredit_multiple_users` — bulk credit ke 3 user
- `test_audit_log_created` — audit log tercatat setelah credit

---

## 3. File yang Diubah

| File | Aksi | Deskripsi |
|------|------|-----------|
| `bot/handlers/admin.py` | MODIFY | + `credit_balance_handler`, `bulkcredit_handler`, tombol dashboard, callback handler |
| `main.py` | MODIFY | + import & register `/credit`, `/bulkcredit` |
| `tests/test_admin_credit.py` | CREATE | Unit test untuk fitur credit |

---

## 4. Risiko & Mitigasi

| Risiko | Mitigasi |
|--------|----------|
| Admin salah ketik telegram_id → saldo masuk ke orang lain | Konfirmasi interaktif dengan nama user sebelum eksekusi |
| Saldo diisi berlebihan (abuse) | Max cap Rp 10.000.000 per operasi + audit log |
| Race condition pada `credit_user_balance` | Sudah ada db.commit() + db.refresh() di CRUD existing |
| User belum pernah /start (belum ada di DB) | `credit_user_balance` sudah handle: create user kalau belum ada |

---

## 5. Verification Checklist

- [ ] `/credit 123456789 10000` → konfirmasi muncul dengan nama user
- [ ] Setelah konfirmasi → saldo user bertambah Rp 10.000
- [ ] User menerima notifikasi saldo bertambah
- [ ] Audit log tercatat dengan action `ADMIN_CREDIT_BALANCE`
- [ ] `/bulkcredit 10000 id1 id2 id3` → 3 user ter-credit
- [ ] Non-admin mengetik `/credit` → ditolak
- [ ] Tombol "💳 Isi Saldo User" muncul di admin dashboard
