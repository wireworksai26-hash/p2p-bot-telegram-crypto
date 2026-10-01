# Project State

## Project Reference

P2P Bot Telegram Crypto (HSN) — bot produksi, deploy Railway. Repo ini tidak memakai
alur roadmap GSD (`.planning/` dibuat mulai 2026-09-30 untuk quick tasks saja).
Panduan proyek: commit atomik per task, suite `C:\Python314\python.exe -B -m unittest discover -s tests`.

## Current Status

Produksi live (commit 06d4055). Audit keamanan 26 Sep 2026 menemukan 22 temuan
abuse/crossvalidation yang terdokumentasi sebagai `@unittest.expectedFailure`
di `tests/test_abuse_{beli,jual,convert,input_state,qris_topup}.py`
(suite: 205 test, 22 expected failure).

### Blockers/Concerns

- Temuan non-Python: `gopay-gateway/server.js` (REFUND sebagai pembayaran, heuristik
  /100, claim-scope default) — butuh harness node, di luar scope quick task ini.
- Race lintas proses (`deduct_user_balance` non-atomik) — arsitektural.

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 260930-tp4 | Fix high-severity abuse bugs from 26 Sep audit (12 fix: replay hash jual, HTML escape, hash pre-validasi, guard cancel topup/sell, invariant convert, hardening validator) — expected failures 22 → 10 | 2026-09-30 | 41a733b | [260930-tp4-fix-high-severity-abuse-bugs-from-26-sep](./quick/260930-tp4-fix-high-severity-abuse-bugs-from-26-sep/) |
| 261001-fix | Fix Telegram bot /start failure & PM2 crash loop from Railway PostgreSQL hostname resolution error with graceful SQLite fallback | 2026-10-01 | In Progress | [261001-fix-railway-db-crash-start-loop](./quick/261001-fix-railway-db-crash-start-loop/) |

Last activity: 2026-10-01 - Created quick plan 261001: Fix Railway PostgreSQL connection failure & auto SQLite fallback.
