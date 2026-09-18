# Perbaikan stok dan emoji — 17 September 2026

Perubahan sudah diimplementasikan di workspace lokal. Belum commit, push,
deploy, menjalankan polling bot, atau mengubah service/database Railway.
Ini melanjutkan temuan awal di [audit-stock-emoji.md](audit-stock-emoji.md).

## Hasil pembacaan langsung

Pembacaan memakai kelas sender yang digunakan aplikasi, melalui
`python scripts/check_wallet_balances.py`. Script hanya membaca saldo publik;
tidak melakukan signing, broadcast, atau akses database.

| Aset | Jaringan | Saldo saat verifikasi |
| --- | --- | --- |
| SOL | Solana | 0.0257 |
| USDT | Solana | 2 |
| USDC | Solana | 2 |
| APT | Aptos | 3.27 |
| ETH | Robinhood mainnet, chain ID 4663 | 0.000735578216 |

Saldo adalah snapshot, bukan jaminan saldo/ketersediaan pada waktu berikutnya.
Robinhood yang sempat timeout pada audit awal sudah berhasil dibaca pada
verifikasi implementasi.

## Perubahan kode

- SOL dibaca langsung melalui JSON-RPC tanpa ketergantungan pada SDK signing.
  SPL memakai raw amount/decimals, termasuk jika `uiAmount` bernilai null.
- APT dibaca lewat view `0x1::coin::balance`, yang mencakup saldo Coin dan
  paired Fungible Asset; alamat placeholder dihapus.
- Robinhood menggunakan RPC mainnet, chain ID 4663, dan explorer yang benar.
  Pembacaan saldo serta jalur pengiriman memeriksa chain ID Robinhood.
- Pembacaan gagal/malformed tidak diubah menjadi nol. Riwayat saldo yang valid
  dipertahankan, diberi status gagal/lama, dan tidak dihitung sebagai stok
  terverifikasi. Stok lebih lama dari 15 menit juga tidak dapat direservasi.
- Job berkala, menu stok, dan `/refreshwallet` memakai layanan sinkronisasi
  yang sama; proses sinkronisasi yang bersamaan diserialkan.
- Menu stok menampilkan timestamp data, mempertahankan saldo kecil nonzero,
  dan memiliki pagination agar pesan tidak terpotong batas Telegram.
- Emoji aset yang tidak terverifikasi diganti simbol koin universal; USDT/USDC
  memakai simbol uang. Konfigurasi lama dengan ID bawaan keliru dimigrasikan
  saat dimuat. Pilihan custom admin yang bukan ID lama tetap dipertahankan.
- SUI/APT tidak lagi mengembalikan sukses dan hash transaksi acak. Pengiriman
  otomatis mengembalikan manual review; saldo ditandai "pengiriman admin".
- Environment Railway tidak lagi ditimpa `.env`. `.dockerignore` mengecualikan
  credential, sesi GoPay, database lokal, dan dependency pengujian dari image.
  API key gateway mengikuti `GOPAY_API_KEY` dan contoh port gateway adalah 3005.

## Pengujian

```sh
python tests/test_stock_fixes.py
python scripts/check_wallet_balances.py
```

- 25 tes regresi stok/emoji lulus: error versus nol, fallback RPC, Aptos view,
  chain ID Robinhood, manual payout SUI/APT, freshness/reservasi, migrasi schema
  lama dua kali, sinkronisasi ke SQLite, HTML/pagination, timestamp, dan emoji.
- Tes regresi menggunakan mock HTTP dan SQLite in-memory, tanpa `.env` produksi.
- Pemeriksaan sintaks Python dan `git diff --check` lulus.
- Tes lama `scratch/test_fee_inventory_validator.py` masih gagal di assertion
  fee pertama: ekspektasi Rp18.000 untuk ALTCOIN Rp1.010.000, sedangkan
  `services/fee_service.py` yang tidak diubah menetapkan Rp19.000. Assertion lain
  juga masih mengacu tarif/perhitungan persentase lama. Ini perlu rekonsiliasi
  dengan aturan tarif klien, bukan perubahan tarif otomatis dalam pekerjaan ini.
  Script lama sekarang memakai database in-memory sehingga aman dari database
  produksi dan tidak lagi menghapus file test pada path tetap.

Belum dilakukan build image Linux, migrasi PostgreSQL produksi, uji visual
Telegram langsung, atau uji payout blockchain. Hasil saldo tidak membuktikan
bahwa semua jalur pengiriman sudah siap produksi.

## Checklist sebelum mengaktifkan Railway

1. Samakan Variables pada service/environment yang dituju dengan konfigurasi
   wallet terbaru. Nilai `.env` lokal tidak otomatis diterapkan ke Railway.
   Untuk saldo publik, private key tidak dibutuhkan.

   ```dotenv
   SOL_WALLET_ADDRESS=CpmQtQ73gVNzb3vSzk8CRLktzeU5ttgDcdxi3wbLi1WL
   SOL_RPC=https://solana-rpc.publicnode.com
   APTOS_WALLET_ADDRESS=0x593e9d152c392003e26b11105dc9b8d50ee8a1ed8a6600e09e58fbfb0b0c7dee
   APTOS_RPC=https://fullnode.mainnet.aptoslabs.com/v1
   ROBINHOOD_RPC=https://rpc.mainnet.chain.robinhood.com
   GOPAY_GATEWAY_URL=http://127.0.0.1:3005
   ```

   Verifikasi `EVM_WALLET_ADDRESS`, `TRX_WALLET_ADDRESS`, `TON_WALLET_ADDRESS`,
   `SUI_WALLET_ADDRESS`, dan RPC lain di Variables. Masukkan token/API key/private
   key melalui secret Variables, jangan commit `.env` atau sesi ke Git.
2. Tetapkan `QRIS_STATIC` dan `GOPAY_MERCHANT_ID` sesuai credential terbaru;
   jangan mengandalkan QRIS fallback lama yang masih ada dalam startup.
   Sesi GoPay harus tersedia melalui `GOPAY_SESSION_JSON` atau mekanisme
   pemulihan database; file sesi lokal sengaja tidak disertakan di image.
3. Startup menambah kolom `sync_status`, `last_error`, `last_checked_at`, dan
   `last_success_at` pada `wallet_balances`. Backup database sebelum deploy;
   verifikasi migrasi PostgreSQL dan `/refreshwallet` setelah startup.
4. Pertahankan pengiriman SUI/APT sebagai proses admin hingga signing dan
   broadcast nyata diimplementasikan. Pastikan customer diberi informasi yang
   sesuai sebelum membuka perdagangan jaringan tersebut.
5. Format signing TRON/TON perlu pemeriksaan terpisah sebelum payout: parser
   TRON saat ini mengharapkan hex tanpa awalan `0x`; implementasi TON mendukung
   mnemonic/hex, tetapi kesesuaiannya dengan wallet belum diuji. Keberhasilan
   baca saldo tidak memvalidasi credential signing.
6. Dua aset Git LFS (`Logo bot.jpg`, `Qris statis.jpeg`) tidak tersedia dari
   origin ketika clone (404). Status deleted lokal sudah ada sejak checkout;
   jangan ikut commit penghapusannya. Pulihkan aset dari sumber pemilik sebelum
   memverifikasi tampilan/QRIS produksi.
7. Private key yang pernah dibagikan dalam percakapan sebaiknya diganti sebelum
   penggunaan produksi. Tidak ada pemindahan dana atau rotasi otomatis di sini.

Referensi teknis yang dipakai saat audit:
[Robinhood mainnet](https://docs.robinhood.com/chain/connecting/) dan
[Aptos coin framework](https://github.com/aptos-labs/aptos-core/blob/main/aptos-move/framework/aptos-framework/sources/coin.move).
