---
phase: 02-sell-swap
reviewed: 2026-10-06T00:00:00Z
depth: deep
files_reviewed: 6
files_reviewed_list:
  - bot/handlers/sell.py
  - bot/handlers/swap.py
  - services/tx_verifier.py
  - services/detector.py
  - services/wallet_detector.py
  - bot/utils/validator.py
findings:
  critical: 6
  warning: 11
  info: 2
  total: 19
status: issues_found
---

# Phase 02 (Sell / Convert): Code Review Report

**Reviewed:** 2026-10-06
**Depth:** deep (call chains traced into database/crud.py, database/models.py, services/fee_service.py, services/price_service.py, services/crypto_sender/*, bot/utils/flow_guard.py, bot/utils/formatter.py, bot/utils/messages.py, bot/keyboards/crypto_select.py, main.py)
**Files Reviewed:** 6
**Status:** issues_found

## Summary

The basic on-chain checks are sound: verification fails closed, checks the chain ID, token contract, recipient, receipt status and EVM confirmations, and Solana `finalized` / TRON `walletsolidity`. Concurrency is also handled: `DepositClaim` has a unique `(network, tx_hash)` constraint, and status transitions are conditional UPDATEs, so the scanner, `verifikasi_cepat` and the user's own hash cannot double-confirm or double-pay. The real risks are in how the order and the deposit are linked:

1. A user can claim someone else's deposit to the shared hot wallet. On convert, the bot then pays out to the claimer automatically.
2. Pending sell/convert orders get cancelled by ordinary navigation (the "Menu Utama" button, the `/cancel` the bot itself suggests, or "Batal" in a new flow). Deposits for cancelled orders are never picked up again.
3. Quote expiry is not enforced. Prices are frozen at amount input with no timeout, and the detector still confirms and auto-pays `expired` orders for 24 h at the old quote.
4. A valid, confirmed sell TX hash is reported to the user as "Deposit Belum Bisa Diverifikasi — Alasan: OK". This was reproduced at runtime.

Three of the BLOCKERs (CR-03, CR-05a, CR-06) already have RED `@unittest.expectedFailure` tests in `tests/test_abuse_*.py`. The suite is green only because those tests are marked XFAIL. The bugs are not fixed.

Client-confirmed rules were treated as correct and not flagged: 0% spread, the gas pairs and +Rp2.500 outgoing surcharge, the +Rp500 altcoin sell surcharge, and the fee tier jumps.

## Critical Issues

### CR-01: A valid, confirmed sell TX hash is reported as "not verifiable" (Alasan: OK)

**File:** `bot/handlers/sell.py:652-670`
**Issue:** On success, `tx_verifier.verify_deposit` returns `reason: "OK"` (`services/tx_verifier.py:108-110`). The handler computes `alasan = "OK"`, so `tertunda = (not "OK") or startswith("Menunggu…") or "belum dapat diverifikasi" in "OK"` is `False`. It therefore takes the failure branch: "❌ Deposit Belum Bisa Diverifikasi … Alasan: OK … kirim hash yang benar". It then returns `ConversationHandler.END`, skips `verifikasi_cepat`, and skips the admin "TX HASH DITERIMA" alert. There is no `verified` branch at all (swap.py:711 has one).
**Scenario:** The user sends 50 USDT, waits for confirmations (the normal case), and pastes the hash. The bot says the deposit could not be verified and to check the transaction, so the user may send the coins again. The 20 s scanner later confirms only one deposit, and the second transfer is stranded. Reproduced with a scratch script based on `tests/test_abuse_jual.py::_kirim_hash`, mocking `verified=True`: return `-1`, reply "❌ Deposit Belum Bisa Diverifikasi", `verifikasi_cepat` not called, admin not notified. No existing test covers the verified=True path.
**Fix:**
```python
if hasil and hasil.get("verified"):
    await deposit_detector._confirm_order(db, order, hasil["tx_hash"], hasil, context.application)
    return ConversationHandler.END   # _confirm_order notifies user + admin
alasan = (hasil or {}).get("reason") or ""
```

### CR-02: Pending sell/convert orders are cancelled by normal navigation, and their deposits are then abandoned

**Files:** `bot/handlers/sell.py:704-705, 785-788, 802-821, 880-886, 903`; `bot/handlers/swap.py:812-833`; `bot/utils/flow_guard.py:62-65`; `services/detector.py:67-74, 148`
**Issue:** After the sell order is created, the conversation stays in `WAITING_TX` indefinitely (no `conversation_timeout`). In that state `menu_back`, `sell_cancel` and `/cancel` all route to `cancel_sell`. `cancel_sell` sets the order to `cancelled` whenever its status is `WAITING_CRYPTO_DEPOSIT`. It does not check whether a deposit hash or proof was already submitted, and it does not check ownership. Four paths reach it:
- The "TX Hash Diterima!" reply (line 705) and the "Bukti Transfer Tersimpan!" reply (line 787) show a **"Menu Utama" (`menu_back`) button**. Pressing it cancels the order the user just paid into. Because the sell ConversationHandler is registered before the global `menu_callback_handler` (main.py:543 vs 557), any `menu_back` press anywhere in the bot does the same while the conversation is alive.
- When the user tries to Buy/Convert while the sell conversation is alive, `block_if_busy` replies "tekan /cancel untuk kembali ke menu". `/cancel` then cancels the sell order.
- `sell_order_id` and `active_swap_order_id` stay in `user_data` after `END`. A new `/sell` or `/swap` followed by "Kembali ke Menu"/"Batal" (`swap.py:818-819`, `sell.py:806`) cancels the **previous** order, even though that order is still inside its 24 h deposit window.

The detector only scans `WAITING_CRYPTO_DEPOSIT | PAYOUT_QUEUED | expired`, never `cancelled`. Once the coins land, nothing confirms them.
**Scenario:** The user sells 0.5 ETH, sends it, submits the hash, sees "Deposit sedang diverifikasi", and taps "Menu Utama". The alert says "❌ Penjualan dibatalkan". The ETH arrives 30 s later and is never attributed. Admin must reconcile by hand.
**Fix:**
- Never cancel once `deposit_tx_hash` or `deposit_proof_file_id` is set. Use a conditional UPDATE: `WHERE order_id=:id AND telegram_id=:uid AND status='WAITING_CRYPTO_DEPOSIT' AND deposit_tx_hash IS NULL AND deposit_proof_file_id IS NULL`.
- Map `menu_back` in `WAITING_TX`/`INPUT_TX_HASH`/`INPUT_PROOF` to a handler that ends the conversation **without** cancelling the order.
- Pop `sell_*` and `active_swap_order_id` keys at every entry point and on `END`.
- Add a `conversation_timeout`.
- Have `block_if_busy` end the stale conversation instead of telling the user to `/cancel`.

### CR-03: Any user can claim another user's deposit to the shared hot wallet (convert pays the attacker automatically)

**Files:** `services/tx_verifier.py:113-115, 494-526`; `services/detector.py:162-177, 218-228, 284`; `bot/handlers/swap.py:699-716`; `bot/handlers/sell.py:633-651`
**Issue:** All sell/convert orders on a network share one deposit address. The user-supplied-hash path accepts **any** inbound transfer that (a) is to the hot wallet, (b) is timestamped after the claimer's `created_at`, and (c) is `>=` the claimer's amount (`_amount_matches`). The deposit's `from_address` is never bound to the user. The "equal quotes on a shared address" guard (`detector.py:218-228`) runs only on the auto-scan branch, not on the hash branch or `swap.input_deposit_hash → _confirm_order`. The first claim wins `DepositClaim`, so the real sender's order can never confirm afterwards.
**Scenario:** The attacker opens a convert order "100 USDT (BSC) → ETH (BASE), wallet = attacker". Order creation is free and the order stays claimable for 24 h, including after it expires. The attacker watches the BSC hot wallet. A victim's 100 USDT (or any amount ≥100) deposit appears, and the attacker pastes its hash into their own order before the victim does. If the amounts are equal, the victim's auto-scan is blocked by the competing guard, so only a manual hash can confirm it. `_confirm_order` then runs `_execute_payout`, which sends ETH to the attacker's wallet. The victim's order stays unconfirmed. On sell, the admin is told "TRANSFER RUPIAH SEGERA" to the attacker's bank. Known RED test: `tests/test_abuse_jual.py::test_overpay_besar_tidak_klaim_order_kecil` (XFAIL).
**Fix:** Make the deposit attributable to one order.
- Add a per-order unique dust suffix to `crypto_amount`, like the buy flow's unique code, and require **exact** match (`automatic_amount_matches`) on every path, not `>=`.
- Run the competing-order check on the hash path too. If more than one open order on the same network/symbol/wallet could match, set `manual_review` instead of auto-paying.
- Persist `verified["from_address"]` (requires populating it in `_verify_evm`/`_verify_solana`/`_verify_tron`). Route mismatched or first-seen senders to manual review for swap auto-payout above a threshold.

### CR-04: An unverified hash attached to any non-waiting order permanently blocks the real depositor

**Files:** `bot/handlers/sell.py:601-636`; `services/detector.py:521-534, 178-204`
**Issue:**
- `handle_tx_hash_input` writes the raw user text into `order.deposit_tx_hash` and commits **before** verification. It does not check order status or ownership, and it does not normalize the hash (`normalize_tx_hash` result is discarded at line 612).
- `_is_hash_used` treats any hash stored on any order whose status is not `WAITING_CRYPTO_DEPOSIT` (expired/cancelled/completed) as already used, even if that hash was never verified for that order.
- Rejection reasons such as "Nominal deposit kurang…" and "Transaksi mendahului pembuatan order." are not in `ALASAN_HASH_BATAL`, so the bogus hash is never released.
**Scenario:** The attacker keeps an old sell conversation open (order expired after 15 min, conversation still in `WAITING_TX`). They tap "Masukkan TX Hash Manual" and paste a victim's fresh deposit hash in normalized form (`0x`+lowercase). The hash is stored on the expired order. For the victim's order, `_is_hash_used(hash)` now finds the attacker's expired order and returns `True`, so the victim's deposit never confirms. If the amount check passes, the attacker's expired order instead claims it (CR-03). The raw, non-normalized storage also makes the hash comparison format-sensitive (`tests/test_abuse_jual.py::test_hash_sama_beda_format_terdeteksi`, XFAIL).
**Fix:**
- Only accept a hash when `order.telegram_id == user` and `order.status == 'WAITING_CRYPTO_DEPOSIT'`.
- Store the hash only in normalized form.
- Base `_is_hash_used` solely on `DepositClaim`, which records verified claims only. Drop the `Order.deposit_tx_hash` scan, or restrict it to `CRYPTO_CONFIRMED`/`COMPLETED`/`PAYOUT_*`.

### CR-05: Quote expiry is not enforced (stale price at confirm; expired quotes auto-paid for 24 h)

**Files:** `bot/handlers/sell.py:191-227, 442-479`; `bot/handlers/swap.py:373-435, 525-559`; `services/detector.py:65-74, 121-124, 142-145, 273, 340-341`; `database/crud.py:368-397`
**Issue:**
- (a) The price is captured when the amount is typed (`sell.py:195`, `swap.py:380-381`). The user can sit on the INPUT_BANK/CONFIRM (sell) or INPUT_TARGET_ADDR/CONFIRM_SWAP (convert) screens for hours, since there is no `conversation_timeout`. Confirmation creates the order with the frozen price. For convert, `quoted_at = utcnow()` is stamped at **confirm** time (`swap.py:558`), so the 30-min window starts from a price that may be hours old. Known RED test: `tests/test_abuse_convert.py::test_konfirmasi_wajib_segarkan_harga` (XFAIL).
- (b) After `expire_stale_orders` marks a sell/swap `expired`, `is_recoverable_expired` + `claimable=("WAITING_CRYPTO_DEPOSIT","expired")` still let the detector confirm it for `SELL_DEPOSIT_WINDOW_MINUTES` (default 1440). For swap, the bot then **auto-pays the stored `target_crypto_amount`**. `swap.py:707` checks `quote_expires_at` on the direct path, but the same stored hash is re-verified by the scanner with the 24 h `deposit_deadline`.
**Scenario:** The user quotes 1,000 USDT → ETH and does not deposit. ETH rises 8% over the next 20 h. The user then deposits and receives the old, larger ETH amount automatically. This is a free 24 h option against the bot. Sell has the mirror image: a stale high IDR quote, with the admin alert (`detector.py:346-356`) showing no "quote expired" warning.
**Fix:**
- Store `quoted_at` (and the price) at amount input. In `handle_order_confirmation`/`confirm_swap_order`, reject and re-quote if `now - quoted_at > 60 s` (or `price_updated_at` is too old).
- In `_confirm_order`, if `verified["timestamp"] > quote_expires_at` (sell: `expired_at`), do not auto-pay. Re-quote at the current price (pay `min(quoted, current)`) or set `manual_review` with both amounts in the admin alert.

### CR-06: Sell asks for a rounded amount but expects the exact one, so honest deposits never confirm

**Files:** `bot/handlers/sell.py:169, 223, 469, 492`; `bot/utils/formatter.py:36-55`; `services/tx_verifier.py:113-120`
**Issue:** `validate_crypto_amount` accepts unlimited decimals. The order stores the exact value (`Decimal(str(crypto_amount))`), but the instruction "Harap kirimkan tepat …" uses `format_crypto`, which rounds to 4 decimals (USDT/USDG/TON), 6 (BNB/SOL/AVAX/MATIC) or 8 (others). If the display rounds down, the user underpays: the manual hash path fails with "Nominal deposit kurang", and auto-scan requires an exact 8-decimal match. If it rounds up, auto-scan never matches and only a manual hash works. Swap already fixed this at `swap.py:426-428`; sell did not.
**Scenario:** The user types `10.12344` USDT. The bot shows "kirimkan tepat 10.1234 USDT", the user sends exactly that, and the order is never confirmed. Known RED test: `tests/test_abuse_jual.py::test_bayar_sesuai_tampilan_tidak_macet` (XFAIL).
**Fix:** Quantize at input with `ROUND_DOWN` to the display precision (or the token's decimals), and use that value for the gross IDR, the order, and the instruction. Alternatively, reject input with more decimals than shown.

## Warnings

### WR-01: Sell offers the MORPH network, which can never be verified, and every TX hash the user submits is rejected

**File:** `bot/keyboards/crypto_select.py:17-18`, `bot/handlers/sell.py:56-67, 609-627`, `services/tx_verifier.py:20-21, 74, 513-514`
**Issue:** The sell keyboard reuses `BUY_NETWORKS_BY_SYMBOL`, which includes MORPH for USDC and ETH. `CryptoSenderFactory.get_sender("MORPH")` raises, so the hot wallet silently falls back to `EVM_WALLET_ADDRESS`. `normalize_tx_hash("MORPH")` raises "Jaringan tidak didukung", which the handler shows as "Format TX Hash Salah" in an endless loop. `_scan_hashes` has no MORPH branch, yet the user is told the deposit is checked automatically.
**Fix:** Exclude `MANUAL_PAYOUT_NETWORKS` and any network not in `tx_verifier` support from the sell keyboard. Alternatively, mark these orders `manual_review` with explicit messaging.

### WR-02: Unescaped bank input in HTML breaks the sell summary and permanently breaks the saved bank

**File:** `bot/utils/messages.py:70-72`, `bot/handlers/sell.py:306-316, 386-400, 432, 772`
**Issue:** `bank_name`, `bank_acc` and `bank_holder` (and the `first_name` fallback) are inserted raw into an HTML message. Input like `BCA, 123, Budi <3` makes Telegram return BadRequest, and the handler raises with no reply, leaving the user stuck in INPUT_BANK. The record was already auto-saved (`save_user_bank` at line 391), so every later one-tap selection of that saved bank fails the same way. The proof caption at line 772 also uses raw `order.buyer_wallet`.
**Fix:** Apply `html.escape` to all user fields in `ORDER_SUMMARY_SELL.format(...)` and in the caption.

### WR-03: "Masukkan TX Hash" and "Batal Order" buttons stop working after the conversation ends

**File:** `bot/handlers/swap.py:622-623, 726-738, 760`; `bot/handlers/sell.py:656-670`; `bot/handlers/start.py:268`
**Issue:** On a rejected hash, the user is told to send the correct proof or press "Batal Order", but the handler returns `END`. `input_swap_tx_*`, `cancel_swap_order_*` and `sell_input_tx` are not entry points, and the global callback router does not handle them, so the buttons do nothing.
**Fix:** Return `WAITING_DEPOSIT_HASH`/`WAITING_TX` after a rejection. Alternatively, register order-scoped entry points that check ownership.

### WR-04: Convert can go silent when a hash verifies but cannot be claimed

**File:** `bot/handlers/swap.py:711-716`; `services/detector.py:274-299, 279`
**Issue:** `_confirm_order` returns early without telling the user when the hash is already claimed, the status changed, or `verified.timestamp < created_at`. That last comparison is strict, while `verify_deposit` allows -120 s. A tx up to 120 s before `created_at` (clock skew) therefore "verifies" on every scan but is never claimable. The swap handler sends nothing and ends the conversation.
**Fix:** After `_confirm_order`, re-read the status and reply accordingly. Apply the same 120 s tolerance in `_confirm_order`, or drop it in `verify_deposit`.

### WR-05: Sell quote validity is 15 min; the client rule is 30 min

**File:** `bot/handlers/sell.py:477, 496`
**Issue:** `expired_at = now + 15 min` and the text says "Batas Waktu Quote: 15 Menit", which conflicts with the confirmed 30-min sell/convert quote rule. Sell also never sets `quoted_at`/`quote_expires_at`.
**Fix:** Use one shared `QUOTE_TTL_MINUTES = 30` and set `quoted_at`/`quote_expires_at` like swap does.

### WR-06: Convert ignores the Rp7.500 minimum when the **source** is a gas pair

**File:** `bot/handlers/swap.py:404-410`; `services/fee_service.py:198-210`
**Issue:** `calculate_fee_idr` receives only the target symbol and network. A USDT-ETH (or ETH-ETH) **source** convert at Rp6.500 is accepted with the 6.000 convert minimum. Per `fee_service` the gas-pair minimum of Rp7.500 applies to all transaction types; the outgoing-only +2.500 surcharge is correct and is not affected.
**Fix:** `if is_gas_pair(src_sym, src_net) and nominal_idr < GAS_PAIR_MIN_IDR: raise ValueError(...)`.

### WR-07: Order-ID collisions combined with no ownership check let a user cancel someone else's order

**File:** `bot/utils/formatter.py:92-100`; `bot/handlers/sell.py:296-297, 561-567, 808-816`; `bot/handlers/swap.py:530, 816-829`
**Issue:** `ORD-YYYYMMDD-XXX` has 46,656 values per day (birthday collision about 50% at roughly 250 orders/day). On collision, `create_order` raises, the handler still returns `WAITING_TX`, and `sell_order_id` now names another user's order. "Batal" then cancels it, because `cancel_sell`/`cancel_swap` never compare `order.telegram_id`. The swap ID `SWAP-<sec>-<tid%1000>` can also collide.
**Fix:** Use `secrets.token_hex(6)`/uuid. Add a `telegram_id` filter to every cancel/hash/proof lookup. Return `END` when order creation fails.

### WR-08: The token contract hint is never shown (NameError swallowed)

**File:** `bot/handlers/sell.py:481-487`
**Issue:** `CryptoSenderFactory` is imported only inside `get_hot_wallet_address`, so line 483 raises NameError, which is caught, and `token_hint` is always `""`.
**Fix:** `from services.crypto_sender import CryptoSenderFactory` at module level.

### WR-09: Banned users can still sell and convert

**File:** `bot/handlers/sell.py:70-105`, `bot/handlers/swap.py:89-121`
**Issue:** No entry point or order-creation path checks `User.is_banned` (only `admin.py` sets it). Known RED test: `tests/test_abuse_input_state.py::test_user_banned_tidak_bisa_mulai_jual` (XFAIL).
**Fix:** Check `is_banned` at entry and again in `handle_order_confirmation`/`confirm_swap_order`.

### WR-10: The PAYOUT_QUEUED watchdog (120 s) is shorter than the TRON payout wait (150 s)

**File:** `services/detector.py:154-160, 386-389, 424-436`; `services/crypto_sender/tron_sender.py:250`
**Issue:** While `send_order_payout` waits up to 150 s for TRON solidity, the 20 s scanner sees `PAYOUT_QUEUED` older than 120 s and flips it to `manual_review`. The in-flight coroutine then writes `COMPLETED` from its stale ORM object. During that window, admins see "Payout terputus" and may resend, causing a double payout.
**Fix:** Set the watchdog above the longest sender wait (for example 600 s). Alternatively, use a dedicated `payout_started_at` lease and make the final write conditional (`WHERE status='PAYOUT_QUEUED'`).

### WR-11: The TRON address validator does not verify the checksum

**File:** `bot/utils/validator.py:51-59` (used by `bot/handlers/buy.py:467,537`, `database/crud.py:1439`)
**Issue:** It calls `base58.b58decode` instead of `b58decode_check`, so a one-character typo in a T-address passes and the payout is unrecoverable. `wallet_detector.py:36` (`^T[a-zA-Z0-9]{33}$`) even allows the non-base58 characters 0/O/I/l.
**Fix:** `raw = base58.b58decode_check(address); return len(raw) == 21 and raw[0] == 0x41`.

## Info

### IN-01: Sell orders never record `fee_category`

**File:** `bot/handlers/sell.py:463-478`
**Issue:** USDT/USDC sells are stored with the model default `'ALTCOIN'`. The field is not read today, but it will mislead reports.
**Fix:** Add `"fee_category": fee_category` (store it in `user_data` at amount input).

### IN-02: wallet_detector network names disagree with the rest of the system

**File:** `services/wallet_detector.py:21, 35, 67, 137-145`
**Issue:** It uses `TRC20`/`ARBITRUM` while every other module uses `TRON`/`ARB`, and it does not know OPTIMISM/AVAX/ROBINHOOD/APTOS. Saved wallets tagged by the detector fall through to revalidation in `crud.get_user_saved_wallets`.
**Fix:** Align the names with `CryptoSenderFactory` networks.

---

_Reviewed: 2026-10-06_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
