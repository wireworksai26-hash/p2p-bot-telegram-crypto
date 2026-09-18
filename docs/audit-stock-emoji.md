# Audit stok dan emoji — 17 September 2026

Catatan ini merekam kondisi sebelum perbaikan. Hasil implementasi dan verifikasi
terbaru tersedia di [stock-fixes.md](stock-fixes.md), termasuk saldo Robinhood
yang sudah berhasil dibaca setelah perbaikan.

Scope: kode lokal pada commit `c968784`, perubahan terkait wallet/emoji,
konfigurasi lokal, pembacaan saldo publik, dan metadata custom emoji Telegram.
Variabel, log, versi deployment, dan status aktual service Railway belum diverifikasi.
Tidak ada bot yang dijalankan, transaksi ditandatangani, database produksi diakses,
atau deployment yang dilakukan selama audit.

Catatan klien dibaca dari Google Docs melalui skill Google Drive:
https://docs.google.com/document/d/1Tz2CZDtnpKM4yYDp_L5HTCqTjDSKphTj5r6ypOS4nNw/edit

## Hasil pembacaan langsung

| Pemeriksaan | Hasil | Implikasi |
| --- | --- | --- |
| SOL alamat konfigurasi lokal | 0 SOL pada dua RPC | Alamat lokal berbeda dengan alamat terbaru yang diberikan klien |
| SOL alamat terbaru klien | 0.0257 SOL pada dua RPC | Saldo tersedia dan bisa dibaca menggunakan alamat publik |
| USDT/USDC Solana alamat terbaru | 2 USDT dan 2 USDC | Token SPL tersedia |
| APT alamat terbaru, endpoint CoinStore | HTTP 404, resource_not_found | Metode yang sekarang dipakai bot tidak membaca saldo wallet ini |
| APT alamat terbaru, view coin::balance | 327000000 unit = 3.27 APT | View membaca saldo Coin dan paired Fungible Asset |
| Robinhood RPC bawaan kode | TLS handshake failure | Pembacaan gagal; hasil nol dari bot tidak membuktikan wallet kosong |
| Robinhood RPC mainnet dokumentasi resmi | Timeout saat audit, termasuk percobaan di luar sandbox | Saldo Robinhood belum bisa dikonfirmasi |
| Telegram getCustomEmojiStickers | Berhasil | ID default ETH/ARB menunjuk diamond umum; APT menunjuk lightning umum |

Saldo merupakan snapshot waktu audit, bukan jaminan ketersediaan berikutnya.

## Penyebab dan audit perubahan

1. **Solana: konfigurasi alamat tidak sesuai.** `.env` lokal masih mengarah ke
   `52ze…6rMU`, sedangkan alamat klien adalah `CpmQ…i1WL`. Kedua public RPC utama
   berhasil membaca alamat klien. Fallback `1rpc.io/solana` mengembalikan HTTP 400.
   Kemungkinan tambahan: import SDK pengiriman dan pembacaan native digabung
   dalam satu blok `try`; satu import gagal menyebabkan saldo SOL menjadi nol.
   Ini belum direproduksi pada lingkungan Railway.

2. **Aptos: konfigurasi hilang dan metode saldo lama.** `.env` lokal tidak memiliki
   `APTOS_WALLET_ADDRESS`, sehingga sender memakai alamat placeholder. Bahkan
   dengan alamat yang benar, endpoint CoinStore mengembalikan 404 untuk wallet
   klien. Gunakan `/v1/view`, function `0x1::coin::balance`, type argument
   `0x1::aptos_coin::AptosCoin`, dan alamat pemilik sebagai argument. Implementasi
   resmi menjumlahkan saldo Coin dan paired Fungible Asset.
   Sumber: https://github.com/aptos-labs/aptos-core/blob/main/aptos-move/framework/aptos-framework/sources/coin.move

3. **Robinhood: konfigurasi chain keliru.** Kode menggunakan RPC
   `https://rpc.robinhood.com`, chain ID `1337`, dan explorer lama. Dokumentasi
   resmi menyebut mainnet chain ID `4663`, RPC
   `https://rpc.mainnet.chain.robinhood.com`, explorer
   `https://robinhoodchain.blockscout.com`. Chain ID salah berpengaruh pada
   pengiriman; masalah pembacaan saat ini adalah endpoint gagal dihubungi.
   Sebelum mengaktifkan perdagangan, pastikan aset klien berada di chain yang
   dimaksud dan verifikasi chain ID melalui RPC yang berfungsi.
   Sumber: https://docs.robinhood.com/chain/connecting/

4. **Error RPC disamakan dengan saldo nol.** Beberapa sender mengembalikan `0.0`
   saat gagal. Job sinkronisasi lalu menyimpan nol, dan menu menampilkan
   `(Kosong)`. Kegagalan harus dilaporkan sebagai status pembacaan gagal/stale;
   saldo terakhir hanya boleh dianggap tersedia jika masih cukup segar menurut
   kebijakan inventori. Menu juga menampilkan waktu membuka pesan, bukan waktu
   pembacaan saldo, padahal sinkronisasi normal berinterval lima menit.

5. **Perbaikan emoji sebagian besar hanya mengubah label.** Diff commit
   `44fc6d0` mengganti ID USDT/USDC ke simbol uang, tetapi mayoritas ID koin
   lain tetap sama. ETH dan ARB berbagi diamond dari pack `Topics`; APT memakai
   lightning dari `Topics`; HYPE memakai rocket dari `Emoji666D`.
   Mengubah komentar atau fallback tidak mengganti gambar premium.
   Untuk logo asli perlu ID yang diverifikasi secara visual; jika tidak tersedia,
   gunakan fallback universal sesuai catatan klien. SOL/BASE berasal dari pack
   `CryptocurrencyCoins`, tetapi nama pack saja tidak membuktikan gambar benar.
   File runtime `data/custom_emojis.json` juga dapat menimpa default; versi
   Railway belum diperiksa.

6. **Konfigurasi Railway berpotensi tertimpa.** Commit `c968784` mengubah
   `load_dotenv` menjadi `override=True`. Jika `.env` ikut masuk ke image,
   nilainya akan menimpa environment Railway. Dockerfile melakukan `COPY . .`
   dan repo belum mempunyai `.dockerignore`. Risiko ini berlaku bila file lokal
   tersebut ikut dikirim pada build; tidak berarti `.env` sudah terunggah.

## Temuan kritis tambahan

`services/crypto_sender/aptos_sender.py` dan `sui_sender.py` masih membuat
`secrets.token_hex(...)` lalu mengembalikan `success=True`. Tidak ada signing atau
broadcast blockchain pada fungsi tersebut. Setelah stok terbaca, order dapat
terlihat selesai meskipun koin tidak terkirim. Payout dua jaringan tersebut perlu
dinonaktifkan dengan hasil gagal/manual review sampai pengiriman nyata selesai
diimplementasikan dan diuji. Ini bukan masalah kekurangan private key semata.

## Batas perubahan audit

Audit menambahkan laporan ini dan `scripts/audit_stock_readonly.mjs` untuk
mengulang pemeriksaan baca-saja. Belum mengubah kode bisnis, credential lokal,
variabel Railway, maupun database. Script memuat `.env` hanya di memori dan
tidak mencetak private key, token bot, atau URL yang mengandung token.

Urutan perbaikan yang disarankan: hentikan sukses palsu SUI/APT; samakan alamat
konfigurasi; perbarui metode saldo Aptos; validasi koneksi/chain Robinhood;
pisahkan saldo nol dan error; koreksi mapping emoji; lalu uji menu dan stok pada
lingkungan terisolasi sebelum menghidupkan deployment.
