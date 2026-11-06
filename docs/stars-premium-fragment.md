# Telegram Stars & Premium: pemenuhan otomatis via Fragment

Status kode: **siap secara arsitektur, nonaktif bawaan**. Tanpa pengaturan di bawah, bot tetap menerima pesanan dan
admin memenuhinya manual (beli di Fragment, kirim ke username tujuan, tekan **Sudah Dikirim**).

> **Peringatan.** Fragment tidak punya API resmi. Klien di `services/fragment_client.py` meniru endpoint web yang dipakai
> situs Fragment, jadi bisa berhenti bekerja kapan pun situsnya berubah, dan belum pernah diuji terhadap Fragment
> sungguhan. Uji dulu dengan pesanan kecil sebelum dipakai untuk semua pembeli.

## Alur

1. User bayar (Saldo Bot / QRIS) → pesanan `paid` → kartu masuk ke admin.
2. Bila otomatis aktif: sistem mengklaim pesanan (`processing`), meminta Fragment menyiapkan transaksi, memeriksa
   biaya TON terhadap harga jual, membayar dari **wallet TON khusus**, menunggu bukti on-chain, lalu `completed`.
3. Apa pun yang tidak pasti diserahkan ke admin, tidak pernah dicoba ulang otomatis.

| Keadaan | Hasil |
|---|---|
| Gagal **pasti sebelum** uang keluar (sesi habis, saldo wallet kurang, margin terlampaui, harga TON tidak ada) | kembali `paid`, proses manual + alarm admin |
| Username tidak ditemukan Fragment | otomatis `rejected`, dana kembali ke Saldo Bot user |
| Pembayaran TON **mungkin** sudah keluar tetapi tidak terkonfirmasi, atau proses mati di tengah jalan | `review` + alarm: admin cek wallet/tonviewer dan akun tujuan, lalu **Sudah Dikirim** atau **Tolak** |
| Nilai pesanan di atas batas per pesanan / batas harian | proses manual |

## Variabel (Railway → Variables)

| Variabel | Isi |
|---|---|
| `STARS_AUTOFULFILL` | `true` untuk mengaktifkan (bawaan `false`) |
| `FRAGMENT_HASH` | nilai `hash` pada URL permintaan `https://fragment.com/api?hash=...` |
| `FRAGMENT_COOKIE` | seluruh header `Cookie` dari permintaan yang sama (akun Fragment yang sudah KYC) |
| `STARS_TON_PRIVATE_KEY` | mnemonic 24 kata / hex wallet **khusus** pembelian Fragment |
| `STARS_TON_WALLET_ADDRESS` | alamat wallet khusus itu |
| `STARS_MAX_ORDER_IDR` | batas otomatis per pesanan (bawaan 1.500.000) |
| `STARS_DAILY_LIMIT_IDR` | batas otomatis per hari WIB (bawaan 5.000.000) |
| `STARS_MIN_MARGIN_PCT` | biaya TON harus ≤ harga jual × (1 − nilai ini%) (bawaan 0) |

Cookie dan hash adalah rahasia akun: simpan hanya di Variables, jangan kirim lewat chat. Bot tidak pernah mencatatnya di log.

### Mengambil `FRAGMENT_HASH` dan `FRAGMENT_COOKIE`
1. Login di fragment.com dengan akun yang sudah KYC, buka halaman beli Stars.
2. Buka DevTools browser → tab Network → lakukan pencarian username di kolom penerima.
3. Pilih permintaan `api?hash=XXXX`: `XXXX` adalah `FRAGMENT_HASH`; header `Cookie` adalah `FRAGMENT_COOKIE`.
4. Sesi bisa kedaluwarsa. Bila alarm "sesi Fragment kedaluwarsa" muncul, ulangi langkah ini.

### Wallet khusus
- Buat wallet TON baru (v5r1 atau v4r2). **Jangan** memakai wallet stok GRAM (`TON_PRIVATE_KEY`).
- Isi seperlunya saja (mis. setara beberapa pesanan). Saldo kurang → pesanan otomatis jatuh ke manual, tidak ada uang hilang.

## Urutan mengaktifkan
1. Isi variabel, **biarkan `STARS_AUTOFULFILL=false`**, restart.
2. Admin jalankan `/starscheck`: harus menampilkan pengaturan lengkap, wallet cocok (v5r1/v4r2) dengan saldo, sesi Fragment aktif, harga TON.
3. Ubah `STARS_AUTOFULFILL=true`. Buat satu pesanan kecil (50 Stars ke akun sendiri) dan pantau sampai selesai.
4. Setelah yakin, naikkan batas sesuai kebutuhan.

## Yang sudah diuji dan yang belum
Diuji otomatis (tanpa jaringan/uang): klien Fragment (tiga langkah, retry harga berubah, pemetaan error, rahasia tidak bocor),
orkestrasi (sukses, refund, manual, review, margin, batas, klaim ganda, sweeper), pengirim wallet (preflight, saldo, kunci khusus).
**Belum diuji:** Fragment sungguhan (protokol tidak resmi), penandatanganan wallet v5r1 (library `pytoniq` tidak ada di lingkungan uji),
dan bahwa Stars benar-benar masuk ke akun tujuan setelah pembayaran. Karena itu langkah 3 di atas wajib.
