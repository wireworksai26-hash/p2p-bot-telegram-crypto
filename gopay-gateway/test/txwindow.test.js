// Cache transaksi gateway selalu berjendela lebar (bukan jendela sempit milik pemanggil pertama).
// Jalankan: node --test gopay-gateway/test/*.test.js
const test = require('node:test');
const assert = require('node:assert');
const { cacheable, wideWindow, CACHE_WINDOW_MS, CACHE_PAGE_SIZE } = require('../txwindow');

const NOW = Date.parse('2026-10-26T13:33:00Z');

test('cek order baru (jendela sempit) dilayani dari cache lebar', () => {
    assert.strictEqual(cacheable(NOW - 60_000, 100, NOW), true);
});

test('jendela default poller (3 hari) tetap boleh memakai cache', () => {
    assert.strictEqual(cacheable(NOW - CACHE_WINDOW_MS, 100, NOW), true);
});

test('laporan lebih lama dari jendela cache atau halaman lebih besar tidak memakai cache', () => {
    assert.strictEqual(cacheable(NOW - CACHE_WINDOW_MS - 1, 100, NOW), false);
    assert.strictEqual(cacheable(NOW - 1000, 101, NOW), false);
    assert.strictEqual(cacheable(NaN, 100, NOW), false);
});

test('fetch untuk cache selalu jendela lebar penuh', () => {
    const w = wideWindow(NOW);
    assert.strictEqual(Date.parse(w.endISO), NOW);
    assert.strictEqual(NOW - Date.parse(w.startISO), CACHE_WINDOW_MS);
    assert.strictEqual(w.pageSize, CACHE_PAGE_SIZE);
});
