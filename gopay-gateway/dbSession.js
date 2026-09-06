/**
 * gopay-gateway/dbSession.js
 * =========================================================================
 * Manajemen persistensi sesi GoPay ke PostgreSQL / Database.
 * Menghindari hilangnya sesi login GoBiz saat container restart / redeploy di Railway.
 */

const fs = require('fs');
const path = require('path');
const { Pool } = require('pg');

const SESSION_FILE = path.join(__dirname, '.GOPAY_SESI_JANGAN_DIHAPUS.json');

// Ambil DATABASE_URL dari environment
let dbUrl = process.env.DATABASE_URL || '';

let pool = null;
let isPostgres = false;

if (dbUrl && (dbUrl.startsWith('postgres://') || dbUrl.startsWith('postgresql://'))) {
    isPostgres = true;
    try {
        const poolConfig = {
            connectionString: dbUrl,
            connectionTimeoutMillis: 5000,
            idleTimeoutMillis: 10000,
        };
        // Aktifkan SSL untuk cloud PostgreSQL (Railway, Supabase, Neon, dll)
        if (!dbUrl.includes('localhost') && !dbUrl.includes('127.0.0.1')) {
            poolConfig.ssl = { rejectUnauthorized: false };
        }
        pool = new Pool(poolConfig);
    } catch (err) {
        console.warn('[DB_SESSION] Gagal inisialisasi koneksi PostgreSQL:', err.message);
    }
}

/**
 * Inisialisasi tabel gopay_sessions di PostgreSQL jika belum ada.
 */
async function init() {
    if (!pool || !isPostgres) return false;
    try {
        await pool.query(`
            CREATE TABLE IF NOT EXISTS gopay_sessions (
                key VARCHAR(100) PRIMARY KEY,
                session_data TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        `);
        return true;
    } catch (err) {
        console.warn('[DB_SESSION] Gagal cek/buat tabel gopay_sessions:', err.message);
        return false;
    }
}

/**
 * Mengambil data sesi dari PostgreSQL.
 */
async function loadSessionFromDb(key = 'active_session') {
    if (!pool || !isPostgres) return null;
    try {
        const res = await pool.query('SELECT session_data FROM gopay_sessions WHERE key = $1 LIMIT 1', [key]);
        if (res.rows && res.rows.length > 0) {
            const raw = res.rows[0].session_data;
            return typeof raw === 'string' ? JSON.parse(raw) : raw;
        }
        return null;
    } catch (err) {
        console.warn('[DB_SESSION] Error query loadSessionFromDb:', err.message);
        return null;
    }
}

/**
 * Menyimpan data sesi ke PostgreSQL (Upsert).
 */
async function saveSessionToDb(sessionData, key = 'active_session') {
    if (!pool || !isPostgres || !sessionData) return false;
    try {
        const payloadStr = typeof sessionData === 'string' ? sessionData : JSON.stringify(sessionData);
        await pool.query(`
            INSERT INTO gopay_sessions (key, session_data, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (key) DO UPDATE
            SET session_data = EXCLUDED.session_data, updated_at = NOW();
        `, [key, payloadStr]);
        console.log('💾 [DB_SESSION] Sesi GoPay berhasil disimpan ke database PostgreSQL.');
        return true;
    } catch (err) {
        console.warn('[DB_SESSION] Error saveSessionToDb:', err.message);
        return false;
    }
}

/**
 * Sinkronisasi dua arah saat startup:
 * 1. Jika file lokal tidak ada / kosong, ambil dari PostgreSQL dan tulis file.
 * 2. Jika file lokal ada, simpan juga ke PostgreSQL.
 */
async function syncOnStartup() {
    if (!pool || !isPostgres) return false;
    try {
        let fileSession = null;
        if (fs.existsSync(SESSION_FILE)) {
            try {
                const content = fs.readFileSync(SESSION_FILE, 'utf-8').trim();
                if (content) {
                    fileSession = JSON.parse(content);
                }
            } catch (fe) {
                console.warn('[DB_SESSION] File sesi lokal korup/tidak valid:', fe.message);
            }
        }

        const dbSession = await loadSessionFromDb();

        if (!fileSession && dbSession) {
            // Pulihkan sesi dari DB ke file
            fs.writeFileSync(SESSION_FILE, JSON.stringify(dbSession, null, 2), 'utf-8');
            console.log('🔑 [DB_SESSION] Sesi GoPay berhasil dipulihkan dari PostgreSQL ke file lokal.');
            return true;
        } else if (fileSession) {
            // Backup sesi lokal ke DB
            if (!dbSession || fileSession.updated_at !== dbSession.updated_at) {
                await saveSessionToDb(fileSession);
            }
            return true;
        }
        return false;
    } catch (err) {
        console.warn('[DB_SESSION] Error syncOnStartup:', err.message);
        return false;
    }
}

/**
 * Monitor berkala (setiap 30 detik) file sesi lokal.
 * Jika file diperbarui (misal oleh auto-refresh token GoPay), langsung sync ke DB.
 */
let lastFileMtime = 0;
function startAutoSync() {
    if (!pool || !isPostgres) return;

    setInterval(async () => {
        try {
            if (!fs.existsSync(SESSION_FILE)) return;
            const stats = fs.statSync(SESSION_FILE);
            if (stats.mtimeMs > lastFileMtime) {
                lastFileMtime = stats.mtimeMs;
                const content = fs.readFileSync(SESSION_FILE, 'utf-8').trim();
                if (content) {
                    const session = JSON.parse(content);
                    if (session && session.access_token) {
                        await saveSessionToDb(session);
                    }
                }
            }
        } catch (err) {
            // Abaikan error background loop
        }
    }, 30000);
}

module.exports = {
    SESSION_FILE,
    init,
    loadSessionFromDb,
    saveSessionToDb,
    syncOnStartup,
    startAutoSync
};
