# PLAN — Phase 2: Broadcasting System Enhancement

> **Phase:** 2 of 6
> **Priority:** 🟠 High
> **Estimated Effort:** ~3-5 jam development + testing
> **Dependencies:** None (broadcast `/broadcast` sudah ada dan berfungsi)

---

## 1. Konteks Bisnis

Client ingin broadcast yang lebih canggih untuk campaign giveaway dan promosi rutin:
- Broadcast ke segment tertentu (bukan selalu semua user)
- Preview sebelum kirim (hindari typo pada blast ke ribuan user)
- Statistik hasil broadcast
- Template tersimpan untuk re-use

**Existing Implementation:**
- `/broadcast <pesan>` sudah ada → [admin.py:1305-1377](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py#L1305-L1377)
- Support foto + caption → sudah ada
- Pace control (20 msg/batch, 1s delay) → [admin.py:1265-1267](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py#L1265-L1267)
- Panel broadcast di admin dashboard → [admin.py:352-369](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py#L352-L369)

---

## 2. Task Breakdown

### Task 2.1 — Broadcast Targeting (Segment)
**File:** [`bot/handlers/admin.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py)

Upgrade `broadcast_handler` untuk menerima flag segment:

```
/broadcast --all Pesan untuk semua user
/broadcast --active Pesan untuk user aktif 30 hari
/broadcast --buyers Pesan untuk user yang pernah transaksi
/broadcast --balance Pesan untuk user yang punya saldo > 0
```

**Logika query per segment (di `database/crud.py`):**

```python
def get_users_by_segment(db, segment: str) -> list[User]:
    """Return list User berdasarkan segment."""
    query = db.query(User).filter(User.is_banned == False)
    
    if segment == "active":
        # User yang punya order dalam 30 hari terakhir
        cutoff = datetime.utcnow() - timedelta(days=30)
        active_ids = db.query(Order.telegram_id).filter(
            Order.created_at >= cutoff
        ).distinct().subquery()
        query = query.filter(User.telegram_id.in_(active_ids))
    elif segment == "buyers":
        # User yang punya minimal 1 order COMPLETED
        buyer_ids = db.query(Order.telegram_id).filter(
            Order.status == "COMPLETED"
        ).distinct().subquery()
        query = query.filter(User.telegram_id.in_(buyer_ids))
    elif segment == "balance":
        query = query.filter(User.balance_idr > 0)
    # else: "all" — semua non-banned
    
    return query.all()
```

### Task 2.2 — Broadcast Preview & Confirm
**File:** [`bot/handlers/admin.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py)

Sebelum kirim broadcast, tampilkan preview:

```
📢 PREVIEW BROADCAST

Segment: 👥 Semua User (342 penerima)
─────────────────────
[ISI PESAN PREVIEW DI SINI]
─────────────────────

[✅ Kirim Sekarang] [❌ Batal]
```

**Flow:**
1. Admin ketik `/broadcast --all Pesan...`
2. Bot tampilkan preview + jumlah penerima + tombol konfirmasi
3. Admin klik "✅ Kirim Sekarang"
4. Eksekusi broadcast (pakai logic existing `_send_broadcast_to_user`)
5. Tampilkan hasil statistik

### Task 2.3 — Broadcast Statistics Report
**File:** [`bot/handlers/admin.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py)

Setelah broadcast selesai, tampilkan:

```
📊 LAPORAN BROADCAST SELESAI

📤 Total target  : 342 user
✅ Terkirim       : 335
❌ Gagal (blocked): 7
⏱  Durasi        : 18 detik
📅 Waktu          : 03 Okt 2026, 10:15 WIB
```

### Task 2.4 — Upgrade Panel Broadcast di Dashboard
**File:** [`bot/handlers/admin.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/bot/handlers/admin.py)

Upgrade `build_admin_broadcast_view()` (line 352-369) untuk menampilkan:
- Panduan lengkap dengan flag segment
- Quick action buttons:
  ```
  [📢 Broadcast Semua]  [👥 Broadcast Aktif]
  [🛒 Broadcast Buyer]  [💰 Broadcast Bersaldo]
  ```
- Setiap button membuka flow interactive di chat

### Task 2.5 — CRUD Helper Functions
**File:** [`database/crud.py`](file:///d:/Project_Test/p2p-crypto-telegram-bot/database/crud.py)

Tambah:
- `get_users_by_segment(db, segment)` → return list User
- `get_segment_count(db, segment)` → return int (untuk preview)

---

## 3. File yang Diubah

| File | Aksi | Deskripsi |
|------|------|-----------|
| `bot/handlers/admin.py` | MODIFY | Upgrade broadcast_handler + preview + stats + dashboard |
| `database/crud.py` | MODIFY | + `get_users_by_segment()`, `get_segment_count()` |
| `tests/test_broadcast.py` | CREATE | Test segment query, preview flow |

---

## 4. Risiko & Mitigasi

| Risiko | Mitigasi |
|--------|----------|
| Broadcast ke 1000+ user kena Telegram flood limit | Pace control sudah ada (20/batch + 1s delay). Tambah RetryAfter handler |
| Admin broadcast tanpa sengaja ke semua user | Preview + konfirmasi sebelum kirim |
| Query segment lambat di DB besar | Index sudah ada di `orders.status`, `orders.created_at` |

---

## 5. Verification Checklist

- [ ] `/broadcast --all Pesan test` → preview muncul dengan jumlah penerima
- [ ] Klik "✅ Kirim Sekarang" → broadcast terkirim + statistik muncul
- [ ] `/broadcast --buyers Promo` → hanya ke user yang pernah transaksi
- [ ] `/broadcast --balance Saldo Anda...` → hanya ke user bersaldo
- [ ] Panel broadcast di dashboard menampilkan tombol segment
- [ ] Foto + caption broadcast tetap berfungsi
