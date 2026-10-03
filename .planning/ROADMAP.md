# ROADMAP — P2P Crypto Telegram Bot (Milestone v2.0)

> **Objective:** Fitur-fitur baru yang diminta client (Dan) pada 3 Oktober 2026.
> **Branch:** `main` — semua pekerjaan langsung di main, deploy Railway.

---

## Phase 1 — Admin Credit Balance (Isi Saldo Buyer)
**Status:** `planned`
**Priority:** 🔴 Highest (blocking giveaway campaign)
**Description:** Admin dapat mengisi saldo `balance_idr` ke user tertentu via panel admin bot. Digunakan untuk campaign giveaway (winner dapat saldo bot, misal Rp 10.000/orang). Saldo ini tidak jadi rupiah tunai, tapi jadi credit internal bot untuk transaksi beli crypto.

**Scope:**
- Command `/credit <telegram_id> <jumlah_idr>` (admin-only)
- Inline button di Admin Dashboard → "💳 Isi Saldo User"
- Konfirmasi sebelum kredit (tampilkan nama user + jumlah)
- Audit log setiap kredit (action: `ADMIN_CREDIT_BALANCE`)
- Notifikasi ke user penerima saldo
- Bulk credit (opsional): `/bulkcredit <id1,id2,id3> <jumlah>`

---

## Phase 2 — Broadcasting System Enhancement
**Status:** `planned`
**Priority:** 🟠 High
**Description:** Upgrade fitur broadcast yang sudah ada (`/broadcast`) dengan kemampuan targeting, scheduling, dan template management dari panel admin inline.

**Scope:**
- Broadcast ke ALL users (sudah ada, perlu polish)
- Broadcast ke segment: users yang pernah transaksi, users dengan saldo > 0, users aktif 30 hari terakhir
- Preview pesan sebelum kirim
- Statistik broadcast (terkirim/gagal/blocked)
- Template broadcast tersimpan (promo, giveaway winner, maintenance)

---

## Phase 3 — Referral Program
**Status:** `planned`  
**Priority:** 🟡 Medium
**Description:** Sistem referral untuk akuisisi user baru. Setiap user punya kode/link referral unik. Referrer mendapat reward saldo bot ketika referee melakukan transaksi pertama.

**Scope:**
- Tabel `referrals` di database (referrer_id, referee_id, status, reward_idr)
- Deep-link referral: `t.me/Hsnpro_bot?start=ref_<TELEGRAM_ID>`
- Tracking: siapa yang invite siapa
- Reward otomatis ke referrer saat referee selesai transaksi pertama
- Menu "📢 Referral" di main menu untuk lihat statistik & share link
- Admin panel: lihat top referrers, set reward amount

---

## Phase 4 — UI/UX Bot Improvements
**Status:** `planned`
**Priority:** 🟡 Medium
**Description:** Perbaikan tampilan dan pengalaman pengguna bot secara keseluruhan.

**Scope:**
- Review dan perbaiki flow navigasi (tombol Kembali, Menu Utama konsisten)
- Pesan error yang lebih user-friendly
- Loading indicator saat proses panjang
- Konsistensi format angka dan emoji di semua handler

---

## Phase 5 — QRIS Dinamis Deep Research
**Status:** `planned`
**Priority:** 🟠 High  
**Mode:** research
**Description:** Riset mendalam tentang payment gateway QRIS dinamis. Evaluasi alternatif selain GoPay/Gopiz gateway yang sedang dipakai. Ulik limitasi, MDR, settlement, dan reliability.

**Scope:**
- Audit gateway GoPay/Gopiz saat ini (uptime, limitasi, session expiry)
- Riset alternatif: Midtrans QRIS, Xendit QRIS, DOKU, Fazz/Xfers
- Perbandingan MDR, settlement time, API reliability
- Rekomendasi arsitektur: single vs multi-gateway fallback
- Dokumen keputusan teknis

---

## Phase 6 — E2E Testing System
**Status:** `planned`
**Priority:** 🟢 Normal
**Description:** Sistemasi testing untuk semua flow bot. Expand dari AI agent tester yang sudah ada di `local_e2e_agent/`.

**Scope:**
- Test suite untuk buy, sell, swap, topup, balance
- Regression test setelah setiap deployment
- Automated smoke test via CI/CD
