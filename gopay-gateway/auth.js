// Autentikasi API key gateway.
// Key hanya lewat header X-Api-Key (query string tercatat di log/proxy) dan
// dibandingkan constant-time. Gateway menolak start dengan key lemah/default.
const crypto = require('crypto');

const WEAK_KEYS = new Set(['RAHASIA', 'YOUR_API_KEY_HERE', 'CHANGE_ME', 'changeme', 'secret']);
const MIN_KEY_LENGTH = 24;

function assertStrongApiKey(key) {
    if (!key || WEAK_KEYS.has(key) || String(key).length < MIN_KEY_LENGTH) {
        throw new Error(`API_KEY gateway kosong/default/terlalu pendek (min ${MIN_KEY_LENGTH} karakter). `
            + 'Set GOPAY_API_KEY yang acak dan sama untuk bot & gateway.');
    }
    return key;
}

function safeEqual(a, b) {
    const ha = crypto.createHash('sha256').update(String(a)).digest();
    const hb = crypto.createHash('sha256').update(String(b)).digest();
    return crypto.timingSafeEqual(ha, hb);
}

function makeApiKeyAuth(expectedKey) {
    return (req, res, next) => {
        const apiKey = req.headers['x-api-key'];
        if (!apiKey || !safeEqual(apiKey, expectedKey)) {
            return res.status(401).json({ success: false, message: 'Autentikasi Gagal: API Key tidak valid' });
        }
        next();
    };
}

module.exports = { makeApiKeyAuth, assertStrongApiKey };
