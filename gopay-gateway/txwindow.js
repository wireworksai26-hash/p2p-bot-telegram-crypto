// Jendela fetch transaksi untuk cache gateway.
//
// Cache transaksi dipakai bersama oleh semua pemanggil (poller bot 20 dtk, tombol Cek Ulang user,
// /check-payment). Dulu isi cache ditentukan pemanggil PERTAMA setelah TTL habis: pengecekan satu
// order memakai jendela sempit (sejak order itu dibuat), sehingga cache yang dipakai poller untuk
// order LAIN yang lebih lama ikut sempit dan pembayarannya baru terlihat di refresh berikutnya
// (terasa sebagai delay deteksi saat banyak user menekan Cek Ulang bersamaan). Sekarang cache
// SELALU berisi jendela lebar; tiap pemanggil menyaring waktu sendiri (verifyPayment sudah begitu).
const CACHE_WINDOW_MS = 3 * 24 * 3600 * 1000;
const CACHE_PAGE_SIZE = 100;

// Permintaan boleh dilayani dari cache lebar bila jendelanya tidak lebih lama dari jendela cache
// dan ukuran halamannya tidak melebihi isi cache.
function cacheable(startMs, pageSize, nowMs) {
    return Number.isFinite(startMs) && startMs >= nowMs - CACHE_WINDOW_MS && pageSize <= CACHE_PAGE_SIZE;
}

function wideWindow(nowMs) {
    return {
        startISO: new Date(nowMs - CACHE_WINDOW_MS).toISOString(),
        endISO: new Date(nowMs).toISOString(),
        pageSize: CACHE_PAGE_SIZE,
    };
}

module.exports = { CACHE_WINDOW_MS, CACHE_PAGE_SIZE, cacheable, wideWindow };
