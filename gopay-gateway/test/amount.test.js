// K3 — satu pembayaran hanya cocok dengan SATU nominal tagihan.
// Jalankan: node --test gopay-gateway/test
const test = require('node:test');
const assert = require('node:assert');
const { amountMatches, txAmountIdr } = require('../amount');

// GoPay mengirim gross_amount dalam sen (heuristik /100 di gateway).
const paidRupiah = (rp) => ({ gross_amount: String(rp * 100) });

test('bayar Rp 1.000 tidak boleh melunasi tagihan Rp 100.000 (eksploit 1/100)', () => {
    assert.strictEqual(amountMatches(paidRupiah(1_000), 100_000), false);
});

test('bayar Rp 500 tidak boleh melunasi tagihan Rp 50.000', () => {
    assert.strictEqual(amountMatches(paidRupiah(500), 50_000), false);
});

test('pembayaran pas tetap cocok', () => {
    assert.strictEqual(amountMatches(paidRupiah(50_137), 50_137), true);
    assert.strictEqual(amountMatches(paidRupiah(100_000), 100_000), true);
});

test('satu transaksi hanya cocok dengan tepat satu nominal', () => {
    for (const rp of [1_000, 50_137, 100_000, 99_900]) {
        const tx = paidRupiah(rp);
        const hits = [rp, rp * 100, Math.round(rp / 100)].filter((t) => amountMatches(tx, t));
        assert.deepStrictEqual(hits, [rp], `tx Rp ${rp} cocok dengan ${hits}`);
    }
});

test('nominal rupiah bukan kelipatan 100 dibaca apa adanya', () => {
    assert.strictEqual(txAmountIdr({ gross_amount: '50137' }), 50137);
});
