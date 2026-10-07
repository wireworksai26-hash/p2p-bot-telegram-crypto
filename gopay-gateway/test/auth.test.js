// T1 — API key gateway tidak boleh lemah/default dan tidak boleh lewat query string.
const test = require('node:test');
const assert = require('node:assert');
const { makeApiKeyAuth, assertStrongApiKey } = require('../auth');

const KEY = 'k'.repeat(40);

function run(auth, { headers = {}, query = {} } = {}) {
    let status = 200;
    let passed = false;
    const res = { status(c) { status = c; return this; }, json() { return this; } };
    auth({ headers, query }, res, () => { passed = true; });
    return { status, passed };
}

test('key benar di header diterima', () => {
    assert.deepStrictEqual(run(makeApiKeyAuth(KEY), { headers: { 'x-api-key': KEY } }), { status: 200, passed: true });
});

test('key salah ditolak', () => {
    assert.strictEqual(run(makeApiKeyAuth(KEY), { headers: { 'x-api-key': 'salah' } }).passed, false);
});

test('key di query string ditolak (bocor ke log/proxy/riwayat)', () => {
    assert.strictEqual(run(makeApiKeyAuth(KEY), { query: { api_key: KEY } }).passed, false);
    assert.strictEqual(run(makeApiKeyAuth(KEY), { query: { apikey: KEY } }).passed, false);
});

test('gateway menolak start dengan key kosong / default / pendek', () => {
    for (const weak of [undefined, '', 'RAHASIA', 'YOUR_API_KEY_HERE', 'pendek123']) {
        assert.throws(() => assertStrongApiKey(weak), `key lemah lolos: ${weak}`);
    }
    assert.strictEqual(assertStrongApiKey(KEY), KEY);
});

test('key berbeda panjang tidak melempar error (perbandingan constant-time aman)', () => {
    assert.strictEqual(run(makeApiKeyAuth(KEY), { headers: { 'x-api-key': KEY + 'x' } }).passed, false);
});
