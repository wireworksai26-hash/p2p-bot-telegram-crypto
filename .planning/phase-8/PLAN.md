# PLAN — Phase 8: Operational Polish, Testimonial Channel & Weekly Reporting

> **Phase:** 8  
> **Priority:** 🔴 High  
> **Estimated Effort:** ~6-8 jam development + testing  
> **Dependencies:** Phase 7 (Advanced Rewards, Loyalty & Enhanced Wallet)

---

## 1. Konteks & Latar Belakang

Phase 8 berfokus pada penyempurnaan operasional bot, peningkatan kredibilitas publik melalui channel testimoni otomatis, perbaikan copy/UI checkout dan referral, penyesuaian segmentasi campaign saldo, serta sistem rekapitulasi laporan transaksi mingguan untuk admin.

### Cakupan Fitur:

| # | Fitur | Deskripsi Singkat |
|---|---|---|
| **8.A** | **Campaign Target & Template Update** | Ubah template `Bagi Rata` agar hanya menargetkan user yang minimal sudah 1x transaksi (`BUYERS`), dan sinkronkan copy template `Loyalty Buyer Giveaway`. |
| **8.B** | **Checkout Copy Cleanup** | Hapus limit `(01-200)` pada teks konfirmasi checkout beli agar tidak membocorkan rentang kode unik ke user. |
| **8.C** | **Testimonial Channel Integration** | Otomatis kirim log transaksi sukses (Beli, Jual, Swap) ke channel testimoni `@TokoKoinID` dengan format rapi dan username disensor. |
| **8.D** | **Post-Transaction Footer Update** | Perbarui pesan penutup terima kasih setelah order sukses dengan link Channel & Testimoni resmi. |
| **8.E** | **Weekly Transaction Report & Sheets Export** | Fitur rekap transaksi 1 minggu (siapa, jenis, nominal, tujuan wallet/rekening, tx hash) untuk admin + export CSV/Sheets. |
| **8.F** | **Referral Share Link Duplicate Bug Fix** | Perbaiki `share_msg` pada tautan bagikan referral Telegram agar URL tidak terkirim dua kali. |

---

## 2. Rincian Implementasi Teknis

### Task 8.1 — Campaign Template & Segment Refinements
**Files:** `services/campaign_service.py`, `bot/handlers/admin_campaign.py`, `tests/test_campaign.py`
- Pada `CAMPAIGN_TEMPLATES["tpl_split_all"]`:
  - Ubah `title` menjadi `"🎁 Bagi Rata Buyer Aktif"`
  - Ubah `description` menjadi `"Bagi rata total pool hadiah ke user yang minimal sudah pernah transaksi 1 kali."`
  - Ubah `target_segment` dari `"all"` menjadi `"buyers"`
- Pada `CAMPAIGN_TEMPLATES["tpl_loyalty_buyers"]`:
  - Perbarui copy `description` dan `default_notif` agar konsisten dengan program loyalitas aktif.

### Task 8.2 — Hapus Range Kode Unik di Konfirmasi Beli
**Files:** `bot/handlers/buy.py`, `tests/test_buy_flow.py`
- Pada `bot/handlers/buy.py` line ~577:
  - Ganti teks:
    ```python
    "ℹ️ <i>Kode unik (01-200) akan ditambahkan ke total bayar untuk verifikasi otomatis.</i>"
    ```
    menjadi:
    ```python
    "ℹ️ <i>Kode unik akan ditambahkan ke total bayar untuk verifikasi otomatis.</i>"
    ```

### Task 8.3 — Service Channel Testimoni Transaksi
**Files:** `services/testimony_service.py` (New), `services/payout_watchdog.py`, `bot/handlers/buy.py`, `bot/handlers/admin.py`, `config/settings.py`
- Buat modul `services/testimony_service.py` dengan fungsi `post_transaction_testimony(bot, order, db=None)`
- Sensor username pengguna secara elegan:
  - Contoh `@hendra_crypto` ➔ `@he***to`
  - Jika tidak ada username: `@User_87***77`
- Format pesan posting ke channel `@TokoKoinID`:
  ```
  Transaksi Selesai
  - Jenis Transaksi : Beli / Jual / Swap
  - Jenis Coin : USDT BSC / ETH ARBITRUM / SOLANA
  - Pengguna : @h****hy
  - Nominal : Rp520.000
  - Transaction Hash : <a href="{explorer_url}">Link</a> (atau '-' jika jual)
  - Bot order : @TokoKoinID_Bot
  ```
- Pasang hook pemanggilan pada:
  - `services/payout_watchdog.py` saat order buy/swap status berubah menjadi `COMPLETED`
  - `bot/handlers/buy.py` saat instant payout buy berhasil
  - `bot/handlers/admin.py` saat admin menyelesaikan order sell (`admin_confirm_sell_callback`)

### Task 8.4 — Update Footer Terima Kasih Transaksi
**Files:** `services/payout_watchdog.py`, `bot/handlers/buy.py`, `bot/handlers/admin.py`
- Ganti baris penutup setelah transaksi sukses menjadi:
  ```
  Terimakasih sudah bertransaksi di sini, Lancar selalu 🙏🙏
  Testimoni : t.me/TokoKoinID
  Channel : t.me/ROBHSN_STORE_SELLER
  ```

### Task 8.5 — Laporan Mingguan Admin & Export Spreadsheet
**Files:** `services/report_service.py` (New), `bot/handlers/admin.py`, `database/crud.py`
- Buat query rekap transaksi 7 hari terakhir:
  - Data: Order ID, Waktu, Tipe (Beli/Jual/Swap), User (@username & ID), Nominal IDR, Nominal Koin, Jaringan, Rekening/Wallet Tujuan, Status, TX Hash.
- Tambahkan command `/weeklyreport` (admin only) dan tombol di Admin Dashboard `📊 Rekap Transaksi Mingguan`.
- Hasil laporan:
  1. Ringkasan teks di chat admin: Total Volume Beli, Total Volume Jual, Total Volume Swap, Total Fee/Margin, Jumlah Transaksi.
  2. Attachment file CSV format `laporan_transaksi_7hari_YYYYMMDD.csv` dengan header lengkap yang dapat langsung di-import atau dibuka di Google Sheets/Excel.
  3. Dukungan webhook opsional untuk auto-sync ke Google Sheets (via Apps Script).

### Task 8.6 — Fix Bug Duplikasi Link Share Referral
**Files:** `bot/handlers/referral.py`
- Pada `bot/handlers/referral.py` baris ~94:
  - Ubah `share_msg` dari:
    ```python
    share_msg = f"Yuk beli dan jual crypto mudah, cepat & terpercaya di HSN Store! Daftar lewat link ini ya: {ref_link}"
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_msg)}"
    ```
    menjadi:
    ```python
    share_msg = "Yuk beli dan jual crypto mudah, cepat & terpercaya di HSN Store! Daftar lewat link ini ya:"
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_msg)}"
    ```

---

## 3. Rencana Pengujian (Test Plan)

Buat file pengujian `tests/test_phase8.py` untuk memvalidasi seluruh fungsionalitas baru:
1. `test_campaign_split_all_targets_only_buyers`: Memastikan `tpl_split_all` hanya memilih user dengan minimal 1 transaksi completed.
2. `test_checkout_message_hides_range_limit`: Memastikan string `(01-200)` tidak ada pada pesan konfirmasi order beli.
3. `test_anonymize_username`: Memvalidasi penyensoran username (`@user1234` ➔ `@us***34`, ID fallback).
4. `test_post_transaction_testimony_format`: Memverifikasi struktur pesan testimoni untuk Beli, Jual, dan Swap.
5. `test_weekly_report_generation`: Memvalidasi query transaksi 7 hari, penghitungan agregat, dan output CSV.
6. `test_referral_share_url_no_duplicate`: Memastikan URL referral tidak ganda di `share_url`.

---

## 4. Verification Checklist

- [ ] `CAMPAIGN_TEMPLATES["tpl_split_all"]` target_segment == "buyers"
- [ ] Teks konfirmasi Beli tidak lagi memuat `(01-200)`
- [ ] Hook testimoni terkirim ke channel `@TokoKoinID` saat Beli, Jual, dan Swap sukses
- [ ] Footer terima kasih memuat link `t.me/TokoKoinID` dan `t.me/ROBHSN_STORE_SELLER`
- [ ] Fitur `/weeklyreport` menghasilkan rekap ringkasan dan CSV laporan mingguan
- [ ] Tombol bagikan referral menghasilkan teks rapi tanpa link ganda
- [ ] Semua pengujian di `tests/test_phase8.py` dan regression test passing 100%
