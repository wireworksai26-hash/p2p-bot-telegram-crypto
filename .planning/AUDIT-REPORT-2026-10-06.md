# Audit Report — P2P Crypto Telegram Bot
**Tanggal:** 6 Oktober 2026 · **Basis:** `abaaca0` + perubahan belum di-commit
**Metode:** GSD Core (`gsd-code-reviewer` deep ×4 area) + E2E full-stack + audit UI/UX
**Detail per area:** `.planning/reviews/01..04-*-REVIEW.md` (1.116 baris, file:line + skenario)

---

## 1. Ringkasan

| | Jumlah |
|---|---|
| BLOCKER ditemukan (setelah dedup) | 15 |
| BLOCKER sudah diperbaiki di sesi ini | 4 (F1–F5) |
| BLOCKER masih terbuka | 11 (B1–B11) |
| WARNING ditemukan | ±50 |
| WARNING diperbaiki | 8 |
| Tes: sebelum → sesudah | 453 → **467 lulus** (+14 tes baru), 10 expected-failure |

> ⚠️ **10 tes "expected failure" adalah bug yang SUDAH diketahui tapi belum diperbaiki**
> (overpay klaim order kecil, hash beda format, quote basi, user banned bisa jual, dll).
> Status "suite hijau" selama ini menyembunyikan bug-bug tersebut.

---

## 2. Sudah diperbaiki di sesi ini (terverifikasi + ada tes)

| # | Masalah | Dampak sebelum fix | File |
|---|---|---|---|
| F1 | **Hash TX valid ditolak** — verifier balas `reason:"OK"`, handler anggap gagal | Setiap user Jual yang input hash benar dapat pesan "Deposit Belum Bisa Diverifikasi — Alasan: OK", flow berakhir, admin tidak dinotif → user bisa kirim koin 2x | `sell.py` |
| F2 | **"Menu Utama" membatalkan order Jual/Convert** — tombol yang ditampilkan bot sendiri setelah "TX Hash Diterima" memanggil `cancel_sell` | Order jadi `cancelled` padahal koin user sudah terkirim → detector tak pernah memproses deposit | `sell.py`, `swap.py` |
| F3 | ID order lama basi di `user_data` → tombol "Batal" di flow berikutnya membatalkan order lama; cancel tidak cek pemilik | Sama dengan F2 | `sell.py`, `swap.py` |
| F4 | **Beli pakai Saldo Bot**: saldo dipotong, payout dijadwalkan *setelah* edit pesan; order `pending` lalu di-expire 15 menit | Edit Telegram gagal / bot restart → saldo hilang, koin tidak terkirim, tanpa refund | `buy.py`, `crud.py` |
| F5 | **Topup over-credit**: jalur foto-bukti & approve-admin mengkredit gross (termasuk pajak QRIS) dan topup TREASURY masuk saldo pribadi admin | Topup 5 jt kelebihan Rp 15.000; kas bot masuk dompet admin | `balance.py`, `admin.py`, `crud.py` |
| F6 | Notifikasi admin: `getattr(context.bot,"bot")` = **User** bot, bukan Bot | Foto bukti transfer (Beli/Topup/Convert) tidak pernah masuk topik grup admin | `telegram_utils.py` |
| F7 | `/topup` selalu crash (`callback_query` None) | Command di menu tak berfungsi | `balance.py` |
| F8 | Nama Telegram tidak di-escape di layar Saldo/Profil | Nama seperti `Ali <3` / `A & B` → Telegram tolak pesan, layar tidak muncul | `balance.py` |
| F9 | Tombol basi (setelah cancel/restart/deploy) diam saja | User kira bot macet | `start.py` |
| F10 | `/cancel` saat idle tidak merespons & tak pernah mencapai alur simpan wallet/rekening | Command di menu "mati"; teks berikutnya tersimpan sebagai wallet | `start.py`, `main.py` |
| F11 | Anti-fraud wallet: alamat Sui/Aptos case-sensitive | Bypass cukup dengan huruf kapital | `crud.py` |
| F12 | Format stok: `Rp7200000jt`, `100,000` (terbaca 100) | Tampilan stok salah baca | `stocks.py` |
| F13 | Label tombol rusak `? Upload Bukti Transfer` | Kosmetik | `sell.py` |

Tes baru: `tests/test_e2e_bot_flows.py` (11 skenario E2E full-stack: Update Telegram asli → Application asli → transport palsu), + regresi di `test_e2e_sell_convert.py`, `test_saved_accounts.py`, `test_stock_fixes.py`.

---

## 2b. Ronde 2 — diperbaiki setelah persetujuan client (6 Okt 2026)

| # | Perbaikan | Cara kerja sekarang | Tes |
|---|---|---|---|
| B1 | **Satu pembayaran QRIS = satu order/topup** | Tabel baru `qris_payment_claims` (tx_id UNIQUE, persisten). Semua 8 jalur deteksi (poller beli, poller topup ×2, tombol "Sudah Transfer" beli/topup, bukti foto beli/topup, final-check expiry) wajib klaim di DB. `/check-payment` kini kirim `startTime` = waktu order dibuat (−60 dtk); zona waktu dikonversi ke UTC (dulu offset dibuang); refund ditolak (bot + gateway). Topup kini dicek bayar DULU baru di-expire | `test_qris_payment_claims.py`, `test_abuse_beli.py` |
| B2 | **Tidak ada payout dobel setelah restart** | Payout yang terputus (`payout_processing` tanpa hash) tidak lagi dikirim ulang otomatis → `manual_review` + notif admin "cek explorer dulu" + tombol Approve | `test_payout_recovery.py` |
| B3 | **Hash deposit orang lain tidak bisa diklaim** | Hash kiriman user hanya auto-konfirmasi bila nominal pas (lebih ≤0,5% untuk pembulatan) DAN tidak juga cocok dengan order lain yang menunggu. Selain itu → admin dicek manual (notif sekali, tombol konfirmasi). Berlaku di Jual & Convert | `test_deposit_hash_guard.py` |
| B4 | **Quote tidak bisa ditahan lalu dipakai saat harga menguntungkan** | Konfirmasi >60 dtk setelah simulasi → harga diambil ulang; bergeser >0,5% → ditolak, user ulangi. Berlaku Beli, Jual, Convert. Deposit Convert yang masuk setelah quote berakhir → admin cek kurs (tidak auto-payout kurs lama) | `test_abuse_convert.py`, `test_e2e_bot_flows.py`, `test_deposit_hash_guard.py` |
| — | Jual 30 menit | `SELL_QUOTE_MINUTES = 30` (QRIS tetap 15) | E2E |
| — | Kunci wallet setelah transaksi sukses | Alamat terkunci hanya dari order Beli/Convert `completed`; simpan di profil tidak mengunci. Cek juga dipasang di input alamat Convert. Catatan 🔐 tampil tepat sebelum tombol konfirmasi Beli & Convert | `test_saved_accounts.py`, E2E |

Hasil: **497 tes lulus**, 7 expected-failure tersisa (bug yang belum dikerjakan: B5, B6, banned user bisa jual, dll).
Deploy: tabel `qris_payment_claims` dibuat otomatis oleh `create_all` saat start; `gopay-gateway/server.js` ikut berubah (filter refund) → restart gateway juga.

## 2c. Ronde 3 — Top Milestone & Kirim Reward ke User Pilihan (6 Okt 2026)

### Top Milestone (Phase 7.B) — catatan client
| Catatan client | Implementasi |
|---|---|
| Admin channel airdrop dikecualikan (diblok dari milestone, tetap bisa transaksi) | Tabel `milestone_exclusions`. Panel: Top Spender → **🚫 Pengecualian** → tambah (ketik ID/@username + catatan, banyak sekaligus) / cabut. Hanya memengaruhi peringkat — **bukan** ban; transaksi normal |
| Tetap Top 10 | Pengecualian dibuang **sebelum** `limit 10`, jadi daftar tetap terisi 10 orang; urutan berikutnya naik (rank & tier reward ikut bergeser) |
| Hanya member/real user yang menyelesaikan ≥ 1 transaksi | Sudah terjamin: ranking hanya menghitung order `completed` dari user terdaftar & tidak diblokir; pending/belum transaksi tidak masuk (ada tes) |
| Ide milestone sama seperti plan kemarin | Tier tidak diubah (150k/100k/50k/30k/25k/20k×5). Berlaku di leaderboard, eksekusi Top Spender, dan template campaign mode MILESTONE |
| (tambahan) Tombol "Eksekusi" Top Spender kini kebal tap ganda | Cooldown 5 menit — sebelumnya tap dobel membayar Top 10 dua kali (temuan B8) |

### 🎁 Kirim Reward ke User Pilihan (fitur baru)
Menu: **Dashboard → 🎁 Kirim Reward ke User Pilihan** (juga ada di Pusat Campaign).
1. Admin ketik daftar sekaligus, satu orang per baris — `@budi 50000`, `123456789 25k`, `@adminchannel 100.000 | pesan khusus orang ini`. Nominal bebas beda per orang (`50000`, `50.000`, `50k`, `1,5jt`).
2. Admin tulis pesan (santai/custom; placeholder `{nama}` `{nominal}` `{saldo}`) atau pakai pesan standar. Pesan khusus per baris menimpa pesan umum. Info "+Rp … masuk, saldo sekarang …" ditambahkan otomatis. Teks admin di-escape (aman dari `<`/`&`).
3. Ringkasan: penerima, total, saldo Kas Bot, sisa. **Jika Kas Bot kurang** → peringatan "kurang Rp X" + tombol **Isi Kas Bot via QRIS**; draft tersimpan dan bisa dilanjutkan (▶️ Lanjutkan Draft).
4. **Kirim** → dana dipotong dari Kas Bot, saldo penerima dikredit, notifikasi dikirim, hasil dilaporkan (notif gagal tetap berarti saldo masuk). Riwayat batch tersedia.

Keamanan uang (belajar dari temuan audit B8):
- Eksekusi lewat klaim atomik `DRAFT→RUNNING`: tap ganda / salinan tombol lama **tidak bisa bayar dua kali**; hanya pembuat batch yang bisa mengeksekusi; non-admin ditolak.
- Kas Bot dipotong **ketat** (tidak boleh minus) sebelum kredit; kredit gagal → dikembalikan ke Kas Bot. Penerima divalidasi ulang saat eksekusi (diblokir setelah preview → dilewati, tidak dipotong).
- Batas: Rp 1.000 – Rp 10.000.000 per orang (sama dengan "Kirim Saldo User"), maks 30 penerima per batch. Penerima harus sudah pernah `/start` (username belum terdaftar dilaporkan & dilewati).
- Semua kredit tercatat di Audit Trail (`ADMIN_REWARD`).

### 🐞 Bug produksi lama yang ditemukan lewat E2E admin (diperbaiki)
`admin_panel_callback` punya dua `import format_idr` lokal yang membuat `format_idr` "lokal" di seluruh fungsi → **UnboundLocalError** di 6 jalur panel. Paling berbahaya: **Kirim Saldo User → konfirmasi** dan **top-up Kas Bot preset** *mengkredit dulu lalu crash saat menyusun pesan* → admin melihat error dan menekan lagi → **kredit dobel**. Perbaikan: impor lokal dihapus. Dua tes E2E baru merah tanpa perbaikan, hijau dengan perbaikan. Pemindaian statis seluruh kode: tidak ada kasus serupa lain.

### Ronde 4 — jawaban client atas 5 pertanyaan (6 Okt 2026)
| # | Keputusan client | Implementasi |
|---|---|---|
| 1 | Customer yang sukses 1× transaksi → wallet **dan rekening pencairan** otomatis tersimpan & terkunci ke user itu | `auto_save_order_accounts` dipanggil saat order `completed` (Beli → wallet; Convert → wallet tujuan di jaringan tujuan; Jual → rekening). Rekening dikunci seperti wallet: `is_bank_account_taken_by_other` (dari penjualan `completed` milik user lain, dicocokkan per nomor rekening — nama bank bebas "BCA"/"Bank BCA"). Pesan "Duplikat Rekening…" + catatan 🔐 tepat sebelum tombol Konfirmasi Jual; rekening duplikat tidak ikut tersimpan di profil. Gagal auto-save tidak pernah menggagalkan penyelesaian order |
| 2 | Cakupan pengecualian "tetap sesuai plan awal" | Tidak diubah: hanya Top Spender + campaign milestone |
| 3 | Seluruh hadiah Milestone dari Kas Bot; kurang → admin wajib mengisi | Top Spender (7.B) dan campaign mode MILESTONE: cek saldo → potong ketat → kredit; gagal di tengah/penerima terlewat → dikembalikan. Layar Top Spender menampilkan Kas Bot; bila kurang, tombol bagi diganti **📲 Isi Kas Bot via QRIS**. Wizard campaign: preview menampilkan Kas Bot, eksekusi saat kurang → draft tetap utuh + tombol QRIS. Bagi rata / undian / loyalty otomatis 7.D **tidak diubah** |
| 4 | Volume Top Spender = akumulasi semua transaksi beli + jual + convert | `get_top_spenders` & `get_top_users_by_milestone` kini menghitung 3 jenis order selesai. Dasar nilai tetap `total_idr` (sama dengan plan Phase 7); `order_volume_idr()` satu tempat bila client mau nilai bruto |
| 5 | Undian hanya untuk yang sudah `/start` | Sudah terpenuhi secara struktural: baris `users` hanya dibuat di `/start`/menu; semua jalur kredit memverifikasi user dulu; fitur reward menolak ID yang belum terdaftar (ada tes) |

### 🔎 Audit bagian 2c (gsd-code-reviewer deep, 14 file) — laporan: `.planning/reviews/05-ROUND3-4-REVIEW.md`
Ditemukan **3 BLOCKER, 8 WARNING, 7 INFO**. Setiap temuan diverifikasi sendiri (3 direproduksi), diperbaiki, dan diberi tes yang **merah tanpa perbaikan / hijau dengan perbaikan**.

| ID | Temuan | Status |
|---|---|---|
| CR-01 | Prompt teks admin yang ditinggalkan (mis. "Tambah Pengecualian") menelan teks berikutnya → user bisa terkecualikan dari milestone tanpa sengaja | ✅ Setiap tombol lain / `/admin` membatalkan semua prompt menunggu; wizard baru kedaluwarsa 10 menit; `editmsg` cek kepemilikan draft |
| CR-02 | Alamat terkunci masih bisa dipakai lewat tombol 1-tap (alamat disimpan sebelum pemilik bertransaksi) | ✅ Dicek di 1-tap + dicek ulang saat konfirmasi Beli & Convert |
| CR-03 | Kunci rekening dikelabui format (`0812.3456.7890`, `+62`, tanpa koma) & **poisoning** lewat `|` di nama pemilik | ✅ Pembandingan angka toleran format; hanya kolom nomor yang dipakai; `|` dinetralkan di input Jual; cek ulang saat konfirmasi Jual |
| WR-01 | `75.000,00` terbaca 7,5 juta (100×) | ✅ Sen dibuang; angka raksasa/`²` ditolak tanpa error |
| WR-02 | Batch/campaign macet RUNNING, kas sudah terpotong | ✅ Campaign: RUNNING di-commit sebelum potong kas (re-run tak memotong dua kali); riwayat reward menampilkan RUNNING |
| WR-03 | Audit-log gagal setelah kredit → penerima ditandai gagal & kas direfund (uang ganda) | ✅ Kredit dicatat sukses dulu; audit best-effort |
| WR-04 | Refund sisa Top Spender di dalam `try` → bila gagal, refund penuh (kas membengkak) | ✅ Dipindah ke luar `try` |
| WR-05 | Potong Kas Bot baca-lalu-tulis (balapan antar proses) | ✅ Satu `UPDATE … WHERE saldo >= n` atomik; top-up juga inkremental |
| WR-06 | Eskalasi hash dedup per order → hash kedua tak sampai admin | ✅ Dedup per (order, hash) |
| WR-07 | Pesan preview/hasil/pengecualian bisa >4096 karakter | ✅ Dipotong, total & tombol tetap utuh |
| WR-08 | Peringkat seri tanpa tie-break (preview ≠ eksekusi) | ✅ Urut `telegram_id` |
| INFO | `isdigit()` vs `²`; `get_user_by_identifier` dobel di crud; `menu_callback_handler` meng-answer di muka; `quote_guard` membandingkan harga jual dgn harga pasar (valid hanya di spread 0%) | regex ID ✅ · sisanya **dicatat, tidak diubah** (lama / di luar scope) |

Dinyatakan aman oleh reviewer: klaim atomik `DRAFT→RUNNING`, otorisasi admin & kepemilikan batch, escape HTML teks admin/nama user, tidak ada LIKE-injection pada nomor rekening.

Hasil: **564 tes lulus**, 7 expected-failure tersisa (bug lama yang belum dikerjakan).

## 3. BLOCKER yang BELUM diperbaiki (butuh keputusan / perubahan desain)

> Status ronde 2: **B1, B2, B3, B4 sudah diperbaiki** (lihat 2b). Sisa: B5–B11.

Urut prioritas (uang hilang tanpa perlu serangan dulu):

| # | Masalah | Skenario | Lokasi | Usulan fix |
|---|---|---|---|---|
| B1 | **Satu pembayaran QRIS bisa melunasi >1 order/topup** | Pencocokan hanya per nominal; 3 daftar "tx terpakai" terpisah & di memori (hilang saat restart); `check_payment` tanpa `startTime` → cari 24 jam ke belakang. User bayar 1x, ulang buat order/topup sampai total sama (1/400), klik "Sudah Transfer" → lunas gratis | `gopay_service.py:33`, `main.py:993-1102`, `gateway/server.js:629` | Tabel `consumed_payments(tx_id UNIQUE)` dipakai SEMUA jalur; kirim `startTime=created_at`; abaikan status REFUND |
| B2 | **Payout dobel setelah restart** | Hash payout disimpan setelah sender selesai (Tron s/d 150 dtk); restart di tengah → poller klaim ulang `payout_processing` >120 dtk → kirim lagi | `crud.py:289`, `buy.py:978-1037` | Simpan nonce/raw-tx/hash SEBELUM broadcast; recovery cek on-chain dulu |
| B3 | **Klaim deposit orang lain** (Jual/Convert) | Hot wallet dipakai bersama; verifikasi hash tidak cek pengirim & menerima nominal ≥ order. Penyerang tempel hash korban ke order miliknya → Convert auto-bayar ke wallet penyerang | `tx_verifier.py:113`, `detector.py:162-228`, `swap.py:699` | Wajib nominal pas + sumber alamat terdaftar, atau alamat deposit unik per order |
| B4 | **Harga quote tidak pernah kedaluwarsa** (Beli/Jual/Convert) | Harga dikunci saat ketik nominal; tanpa `conversation_timeout`. Spread 0% → user tunggu harga naik lalu konfirmasi (Saldo Bot = payout instan). Order `expired` tetap dikonfirmasi 24 jam | `buy.py:298`, `sell.py:191-479`, `swap.py:373-558`, `detector.py:121-273` | Re-quote saat konfirmasi bila >N detik; tolak deposit utk order expired kecuali harga di-refresh |
| B5 | **Nominal Jual tampil dibulatkan** | Input 10.12344 → layar "kirim 10.1234"; user kirim sesuai layar → order tak pernah terkonfirmasi | `sell.py:169-492`, `formatter.py:36` | Bulatkan jumlah order ke presisi tampilan sebelum disimpan |
| B6 | **Hash dipakai ulang beda format** | Hash A untuk order 1, URL explorer/uppercase hash A untuk order 2 → admin konfirmasi & bayar Rupiah 2x | `sell.py:635`, `admin.py:2438-2608` | Normalisasi hash sebelum simpan & cek |
| B7 | **Hash korban diblokir** | Tempel hash korban ke order lama milik penyerang → deposit korban ditolak "hash sudah dipakai" | `sell.py:601-636`, `detector.py:521` | Cek kepemilikan + status order sebelum simpan hash |
| B8 | **Admin: tombol bayar tanpa idempotensi** | Double-tap Top Spender / Random Draw / "Kirim Saldo" → bayar 2x; nominal di callback tak divalidasi | `admin.py:973, 1352-1371, 1631-1714`, `campaign_service.py:499-742` | Token sekali pakai di DB / status job |
| B9 | **Admin reject tanpa guard status** | Reject order Saldo Bot → tanpa refund; reject swap setelah deposit masuk → koin user tertahan; topup SUCCESS bisa jadi CANCELLED | `admin.py:3259-3441` | Guard status + refund otomatis |
| B10 | **Topup bayar di menit terakhir hangus** | Poller meng-expire SEBELUM cek pembayaran; tombol approve admin menolak EXPIRED | `main.py:1042`, `crud.py:1044` | Cek pembayaran dulu, baru expire; izinkan approve EXPIRED |
| B11 | **Postgres down saat boot → diam-diam pakai SQLite kosong** | Semua order/saldo di jendela itu hilang saat restart berikutnya | `connection.py:56-72` | Di production: gagal keras (crash → Railway restart), jangan fallback |

---

## 4. WARNING penting (pilihan)

- **WR — Jual 15 menit vs aturan 30 menit**: `sell.py:477` + teks hardcoded "15 Menit". (Lihat Pertanyaan Q1.)
- **Anti-fraud wallet bisa disalahgunakan**: siapa pun bisa "menyimpan" alamat orang lain duluan → pemilik asli diblokir; alamat bersama (exchange/Telegram Wallet TON) memblokir user lain; format TON EQ/UQ/raw belum dinormalisasi. (Lihat Q2.)
- Sell menawarkan network **MORPH** yang tidak bisa diverifikasi → loop "Format TX Hash Salah".
- Detail rekening Jual tidak di-escape HTML (`messages.py:70`) → input seperti `Budi <3` membuat ringkasan gagal terkirim; parser hanya mengerti format koma (input `BCA 123 Budi` → "Bank Lokal", atas nama = nama Telegram).
- QRIS total > Rp 10 jt jatuh ke QR statis padahal teks bilang "nominal muncul otomatis".
- ID order: hanya 3 karakter acak/hari → tabrakan → error generik.
- Bulk credit tanpa dedup ID & tanpa batas total; treasury cap tidak pernah dipakai di payout.
- Cron laporan bulanan jalan 16:00 WIB (container UTC) → 8 jam terakhir bulan tidak terlapor; "TOTAL MASUK" hitung fee 2x.
- Alarm monitor API mengirim URL RPC lengkap (berisi API key) ke grup notifikasi.
- CSV export rentan formula injection (nama/rekening user).
- Kalkulator: "N/A" untuk nominal > 1,01 jt padahal price list punya tier persen.

---

## 5. E2E & UI/UX

**E2E full-stack** (9 alur, Update asli → dispatcher asli): Beli QRIS, Anti-fraud, Jual, Convert, Topup, Kalkulator, Guard pindah-flow, Tombol basi, Saldo/Riwayat/Stok/Harga. 0 crash handler. Routing: 209 `callback_data` dicek, 0 tombol mati.

**UI/UX — yang sudah enak:** menu utama 10 tombol rapi; Beli 3 langkah jelas; pesan error nominal/wallet spesifik; guard pindah flow jelas; Price List & Stok kini kartu ringkas expandable.

**UI/UX — masih crowded / tidak konsisten (usulan, belum diubah):**
1. **Cek Harga: 54 baris** — 2 baris per koin ("Beli / Jual" redundant karena spread 0%). Usul 1 baris per koin → ±20 baris.
2. **Convert pakai format angka Inggris** (`Rp 358,300`, `$4,018.9785`) — fitur lain pakai `Rp 358.300`.
3. **Nama koin tidak konsisten**: Convert menampilkan `MATIC`, Beli/Jual `POL`; judul Convert `[TUKAR ANTAR JARINGAN / OTC CONVERT]` beda gaya.
4. Network ditampilkan mentah (`BSC`, `POLYGON`, `ROBINHOOD`) di tombol, sedangkan Stok pakai `BEP20/Poly`.
5. Beranda: "Total Transaksi: **0x** Berhasil" terbaca seperti alamat hex.
6. Pesan saat stok kosong tetap menampilkan footer ⚠️ data lama.
7. Teks diketik saat bot menunggu tombol → diam tanpa petunjuk.

---

## 6. Pertanyaan untuk client

- **Q1.** Order **Jual** ternyata sejak awal 15 menit (bukan 30 seperti yang saya laporkan sebelumnya — koreksi). Mau dijadikan 30 menit, atau tetap 15?
- **Q2.** Anti-fraud wallet: apakah "simpan alamat di profil" boleh langsung mengikat alamat (bisa disalahgunakan), atau hanya alamat yang **pernah dipakai di transaksi sukses** yang dianggap milik user?
- **Q3.** Untuk B1–B11: boleh saya kerjakan bertahap (mulai B1, B2, B3 — risiko uang terbesar)? Beberapa butuh migrasi DB (tabel baru).

## 7. Keamanan repo
- `.planning/CLAUDE_CODE_HANDOFF.md` (untracked, **tidak** di-.gitignore) berisi **token bot production plaintext**. Jangan `git add .`; hapus token dari file atau tambahkan ke `.gitignore`. Token juga sempat ditempel di chat → pertimbangkan rotate via @BotFather sebelum dipakai production.
