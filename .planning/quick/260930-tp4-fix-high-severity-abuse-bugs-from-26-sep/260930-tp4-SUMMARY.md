---
phase: quick-260930-tp4
plan: 260930-tp4
status: complete
date: 2026-09-30
commits:
  - 45d510c
  - e9ba368
  - 41a733b
---

# Quick Task 260930-tp4 — Fix High-Severity Abuse Bugs (Audit 26 Sep 2026)

Perbaikan 12 temuan abuse/crossvalidation dari audit 26 Sep 2026: 3 task atomik,
12 marker `@unittest.expectedFailure` dilepas menjadi regression test hijau.
Suite akhir: `Ran 205 tests ... OK (expected failures=10)` dengan zero failure/error.

## Task Results

### Task 1 — Jual: replay hash jalur admin + HTML injection + hash sampah (`45d510c`)

- `bot/handlers/admin.py` — `_reverify_sell_deposit` sekarang cek
  `DepositDetector._is_hash_used(db, tx_hash, exclude_order=order.order_id)` setelah
  guard kosong/`PHOTO:` dan sebelum `verify_deposit`. Hash milik order lain →
  `{"verified": False, "reason": "TX hash sudah diklaim order lain (indikasi replay)."}`.
  Satu guard menutup `admin_confirm_sell_callback` dan `/verifysell` (keduanya gate
  flip status ke `verified.get("verified")`).
- `bot/handlers/sell.py` — `handle_tx_hash_input` mengganti cek `len(tx_hash) < 10`
  dengan pre-validasi `tx_verifier.normalize_tx_hash(network, tx_hash)` dalam
  `try/except ValueError`; hash sampah dapat copy "Format TX Hash Salah!" lama
  (tidak disimpan, tidak ada alert admin, `return INPUT_TX_HASH`).
- HTML escape (`html.escape as _esc`): `sell.py` alert admin (`tx_hash`, `bank_info`)
  dan reply user (`tx_hash`); `services/detector.py` alert admin sell (`tx_hash`,
  `order.buyer_wallet`); `bot/handlers/balance.py` caption bukti topup
  (`update.effective_user.name`).
- `tests/test_abuse_jual.py` — helper `_kirim_hash(payload, alasan_verifikasi, bank_name="Bank Uji")`;
  test injection memakai hash valid `0x` + `"12"*32` dengan `bank_name="<b>HACK</b>"`.
  3 marker dilepas: `test_reverify_tolak_hash_milik_order_lain`,
  `test_injection_tx_hash_tidak_lolos_ke_pesan_admin`,
  `test_hash_sampah_tidak_dianggap_menunggu`.
- Verifikasi: `test_abuse_jual.py` → `Ran 15 tests ... OK (expected failures=4)`;
  full suite → `Ran 205 tests ... OK (expected failures=19)`.

### Task 2 — Guard cancel topup & sell (`e9ba368`)

- `bot/handlers/balance.py` — `cancel_topup_manual` guard berurutan: data tidak
  ditemukan → bukan pemilik (`topup.telegram_id != query.from_user.id`) → status
  bukan `PENDING`; hanya path `PENDING` yang menulis `CANCELLED` dan mengedit caption.
  `query.answer()` kini dipanggil tepat sekali per invocation (guard pakai `show_alert=True`,
  sukses pakai answer biasa).
- `bot/handlers/sell.py` — `cancel_sell` fetch `get_order_by_id`; hanya memanggil
  `update_order_status(..., "cancelled", ...)` bila order `None` (legacy no-op) atau
  status di `{WAITING_CRYPTO_DEPOSIT, PENDING, DRAFT, QUOTED}`. Selain itu status tidak
  disentuh dan user dapat alert "⚠️ Deposit sudah terkonfirmasi dan order diteruskan ke admin."
  Kedua branch tetap `send_main_menu` + `ConversationHandler.END`.
- Tests: 2 marker `test_abuse_qris_topup.py` + 1 marker `test_abuse_jual.py` dilepas.
- Verifikasi: qris topup → `Ran 4 tests ... OK` (xfail 0); jual → `OK (expected failures=3)`;
  full suite → `OK (expected failures=16)`.

### Task 3 — Convert invariants + hardening validator/input (`41a733b`)

- `bot/handlers/swap.py` — `confirm_swap_order`: invariant server-side
  `src_sym == tgt_sym and src_net == tgt_net` → `edit_message_text` penolakan +
  `ConversationHandler.END`, sebelum cek `MANUAL_PAYOUT_NETWORKS`/inventory; tidak ada
  `Order` dibuat. `input_target_addr`: alamat tujuan == `sender.wallet_address`
  (non-kosong, case-insensitive) ditolak dengan keyboard Batal/owner → `INPUT_TARGET_ADDR`.
- `bot/utils/validator.py` — `import math`; `validate_amount_idr` menolak input
  non-ASCII komplit (`if not text.isascii()`) sehingga digit Arab/fullwidth ditolak di
  semua path termasuk shorthand `k/rb`; `validate_crypto_amount` memakai
  `if not math.isfinite(amount) or amount <= 0` (input 400-digit → inf ditolak).
- `bot/handlers/sell.py` — `handle_bank_input` menolak `len(bank_info) > 250` sebelum
  menulis `sell_bank_*` ke `context.user_data` (kolom `Order.buyer_wallet = String(250)`).
- Tests: 2 marker `test_abuse_convert.py` + 4 marker `test_abuse_input_state.py` dilepas.
- Verifikasi: convert → `Ran 8 tests ... OK (expected failures=3)`; input_state →
  `Ran 10 tests ... OK (expected failures=2)`; full suite → **`Ran 205 tests ... OK (expected failures=10)`** (target tercapai).

## Test Ledger (expected failures)

| Stage | jual | qris_topup | convert | input_state | beli (out-of-scope) | Full suite |
|-------|------|------------|---------|-------------|---------------------|------------|
| Baseline | 7 | 2 | 5 | 6 | 2 | 22 |
| After Task 1 (`45d510c`) | 4 | 2 | 5 | 6 | 2 | 19 |
| After Task 2 (`e9ba368`) | 3 | 0 | 5 | 6 | 2 | 16 |
| After Task 3 (`41a733b`) | 3 | 0 | 3 | 2 | 2 | 10 |

Zero failures/errors di setiap tahap. Command:
`C:\Python314\python.exe -B -m unittest discover -s tests`.

Marker out-of-scope diverifikasi tetap utuh: `test_abuse_beli.py` (2),
`test_abuse_jual.py` (3: overpay, hash beda format, bayar sesuai tampilan),
`test_abuse_convert.py` (3: notasi ilmiah, titik desimal koin USD, harga segar),
`test_abuse_input_state.py` (2: completed→cancelled, banned user).

## Deviations from Plan

None — plan dieksekusi persis seperti tertulis. Catatan proses: satu edit test file
sempat menambahkan decorator `@unittest.expectedFailure` ganda pada
`test_reverify_tolak_hash_milik_order_lain`; langsung dikoreksi sebelum commit dan
file yang ter-commit sudah bersih (tidak ada dampak pada hasil).

## Files Changed

- `bot/handlers/admin.py` (Task 1)
- `bot/handlers/sell.py` (Task 1, 2, 3)
- `bot/handlers/balance.py` (Task 1, 2)
- `bot/handlers/swap.py` (Task 3)
- `services/detector.py` (Task 1)
- `bot/utils/validator.py` (Task 3)
- `tests/test_abuse_jual.py`, `tests/test_abuse_qris_topup.py`,
  `tests/test_abuse_convert.py`, `tests/test_abuse_input_state.py`

## Self-Check

- Commits exist on branch: `45d510c`, `e9ba368`, `41a733b` (main, 06d4055 base).
- Final suite line: `Ran 205 tests in 11.219s` / `OK (expected failures=10)`.
- `.planning/` artifacts tidak di-commit (orchestrator yang commit terpisah).
