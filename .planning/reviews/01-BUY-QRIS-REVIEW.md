---
phase: 01-buy-qris
reviewed: 2026-10-06T00:00:00Z
depth: deep
files_reviewed: 7
files_reviewed_list:
  - bot/handlers/buy.py
  - bot/handlers/balance.py
  - services/gopay_service.py
  - services/qris_generator.py
  - services/payout_service.py
  - services/payout_watchdog.py
  - services/fee_service.py
findings:
  critical: 4
  warning: 12
  info: 2
  total: 18
status: issues_found
---

# Phase 01 (Buy / QRIS / Topup / Payout): Code Review Report

**Reviewed:** 2026-10-06
**Depth:** deep (call chains traced into database/crud.py, database/models.py, main.py scheduler jobs, bot/handlers/admin.py approve/reject, gopay-gateway/server.js `/check-payment`, services/crypto_sender/evm_sender.py)
**Files Reviewed:** 7
**Status:** issues_found

## Summary

The per-order state machine (`claim_order_paid` -> `claim_order_payout_processing` -> completed/manual_review) is sound against the poller, the "Saya Sudah Transfer" button and admin approval racing each other. `send_crypto_with_retry` and the EVM sender also refuse to re-sign once a broadcast has been attempted. The money-loss risks are elsewhere:

1. **Payment-to-order binding.** A QRIS payment is matched only by amount, and three detection paths use three separate "already used" sets: the gateway's in-memory claim map, the bot's in-memory `_buy_matched_tx_ids`, and the topup set. `/check-payment` is also called with no start time. As a result, one real payment can pay for a later order or topup that happens to have the same total.
2. **Bot-balance (saldo) buys** are debited before a fire-and-forget task that nothing else retries. The expiry job can then mark them `expired` without a refund.
3. **Unbounded price lock.** The buy price is fixed when the amount is typed. With 0% spread, the user can wait for the market to move and only then confirm.
4. **Gross topup credit.** The topup photo-proof path credits the gross amount (including QRIS MDR) and ignores the treasury prefix.

Business rules from the brief (0% spread, 15-min QRIS expiry, unique code 1..400, gas pairs, tier jumps, +Rp500 sell surcharge, 0.3% MDR > 500k) were not flagged. `fee_service.py` tier tables are contiguous; no defects found there.

## Narrative Findings (AI reviewer)

## Critical Issues

### CR-01: One QRIS payment can pay for multiple orders/topups (no time floor + disjoint claim sets + unique code only unique among *pending*)

**File:** `services/gopay_service.py:33-37`, `main.py:1094-1101`, `database/crud.py:802-819`, callers `bot/handlers/buy.py:1170,1272`, `main.py:863`, `bot/handlers/balance.py:294,427`, `main.py:1047`
**Issue:**
- `check_payment()` sends only `amount` + `trx_id`, never `startTime`. The gateway (`gopay-gateway/server.js:634,642`) then scans the last 24 h with `filterStartTimeMs = 0`, so a payment made *before the order existed* matches.
- The gateway dedupes via an in-memory `claimedTransactions` map (lost on gateway restart). That map is only written by the `/check-payment` path.
- The 20 s buy poller (`_job_check_pending_buy_payments`) matches via `GET /transactions` and records the tx only in the bot-process set `_buy_matched_tx_ids`. It never claims in the gateway.
- `generate_unique_payment_code` only excludes codes of orders/topups that are currently `pending`/`PENDING`. As soon as an order completes, its exact total (nominal + MDR + code) can be issued again.

**Scenario (honest user, no attack):** Order A (nominal 100.000, code 207, total 100.207) is paid and detected by the poller, then completed; the gateway has no claim for that tx. 30 min later user B gets nominal 100.000 with code 207, so B's total is also 100.207. B never pays. At expiry, `_job_expire_orders` (main.py:863) calls `check_payment(100207, B)`. The gateway finds A's unclaimed tx, claims it for B and returns `paid`, and `finalize_gopay_buy_payment` sends crypto to B for free. The same happens if B presses "Saya Sudah Transfer".

**Scenario (deliberate):** The attacker pays their own buy order once (detected by the poller). They then repeatedly open a topup with the same nominal and cancel it (`cancel_topup_` frees the code) until the shown amount equals the paid total (about 1/400 per try, no rate limit). The topup poller's `check_payment` claims the old tx and the balance is credited. The attacker then buys with saldo, so the same payment is spent twice.

Also: two orders/topups that are pending at the same time can have equal totals with different nominal+code (100.000+7 = 100.002+5). The poller then credits the payment to whichever order it iterates first, which may be another user's order.

**Fix:**
- Pass `startTime=<order/topup created_at ISO>` on every `check_payment` call.
- Persist consumed gateway transaction IDs in a DB table with a UNIQUE constraint. Insert into it (in the same transaction as `claim_order_paid` / `claim_topup_success`) from *all* paths: poller, button, expiry final-check, topup poller, proof photo. Treat a uniqueness violation as "not paid".
- Make the unique-code generator guarantee uniqueness of the **final amount** across pending orders/topups and across payments seen in the lookback window, not just of the code:
```python
used_totals = {o.total_idr for o in pending_orders} | {t.amount_idr for t in pending_topups} | recent_paid_totals_24h
code = next(c for c in shuffled(range(1, 401)) if base + mdr + c not in used_totals)
```

### CR-02: Saldo (BOT_BALANCE) buy can be debited and then silently expired with no payout and no refund

**File:** `bot/handlers/buy.py:755-797`, `database/crud.py:337-345`, `database/crud.py:359-366`
**Issue:**
- After `deduct_user_balance` commits, the order stays `status="pending"` (line 771). The only thing that moves it forward is `asyncio.create_task(_run_finalize_background(...))` at line 797. That task is scheduled *after* `await query.edit_message_text(...)` (line 792).
- If that edit raises (TimedOut/NetworkError/BadRequest), the outer `except` at 909 swallows it and the task is never created. A process restart before the task claims the order has the same effect.
- No backstop exists:
  - `get_pending_gopay_orders` and `get_gopay_resume_orders` filter `payment_method == "GOPAY_QRIS"`.
  - `expire_stale_orders` expires **every** `pending` order older than 15 min, regardless of payment method, so the debited order becomes `expired`. No refund, no crypto, no admin alert.
  - A BOT_BALANCE order that crashes mid-payout (`paid`/`payout_processing`, no hash) is never resumed either.

**Scenario:** The user buys Rp 500.000 with saldo and Telegram times out on the success edit. The balance is gone, and 15 min later the order is `expired`.
**Fix:** Make the debit and the state transition one DB transaction that leaves the order in `paid` (not `pending`). Schedule finalize *before* any Telegram I/O. Include `BOT_BALANCE` in `get_gopay_resume_orders` (or a generic resume query). Exclude it from `expire_stale_orders`:
```python
# crud.expire_stale_orders
.filter(Order.status == "pending", Order.created_at <= cutoff, Order.payment_method != "BOT_BALANCE")
```

### CR-03: Buy price is locked indefinitely between amount input and confirmation (free option at 0% spread)

**File:** `bot/handlers/buy.py:298-314` (quote captured), `bot/handlers/buy.py:655-664,732-751,806-826` (used unchanged), `bot/handlers/buy.py:1286-1334` (no `conversation_timeout`)
**Issue:**
- `buy_price_per_unit` / `buy_crypto_amount` are computed once in `handle_amount_input` and stored in `user_data`.
- `handle_payment_selection` and `handle_order_confirmation` never re-quote or check quote age, and the ConversationHandler has no timeout.
- `quoted_at` for BOT_BALANCE is written at confirmation time (line 746), so the stored order hides the real quote age.

**Scenario:** The user enters Rp 10.000.000 of ETH and parks on the confirm screen. ETH rises 5% over a few hours, and the user taps "Konfirmasi". With saldo, the payout is immediate at the stale price, so the bot loses about Rp 500.000. With QRIS, they get another 15 min on top.
**Fix:** Store `buy_quoted_at` in `handle_amount_input`. In `handle_order_confirmation`, reject the order (or re-quote and re-show the summary) if it is older than N seconds. Also set `conversation_timeout` on `buy_conversation_handler`:
```python
if datetime.utcnow() - context.user_data["buy_quoted_at"] > timedelta(seconds=settings.BUY_QUOTE_TTL_SECONDS):
    return await _requote(update, context)
```

### CR-04: Topup photo-proof auto-check over-credits MDR and credits treasury topups to a personal balance

**File:** `bot/handlers/balance.py:428-430` (same pattern in `bot/handlers/admin.py:3314`)
**Issue:**
- `check_topup_payment_manual` (balance.py:308-328) and `_complete_topup` (main.py:942-971) credit `amount_idr - mdr_idr` and route `TREASURY-`/`TOPUP-TREASURY-` IDs to `topup_bot_treasury`.
- The proof path credits the gross `topup.amount_idr` to `credit_user_balance` unconditionally.

**Scenario:**
- Custom topup Rp 5.000.000: MDR is 15.000, so the total is 5.015.xxx. The user sends a photo after paying, and the auto-check credits 5.015.xxx instead of 5.000.xxx. The user gains Rp 15.000; any amount over Rp 500k leaks 0.3%.
- A treasury topup paid via photo is credited to the admin's spendable user balance, and the treasury is never funded.

**Fix:** Extract one `complete_topup(db, topup)` helper (the main.py version) and call it from all four paths. Never compute the credit inline.

## Warnings

### WR-01: Admin "Tolak" has no status guard: it can reject an order that is paying out or completed, and release its reservation mid-payout

**File:** `bot/handlers/admin.py:3270-3273` (buttons fanned out to every admin at `bot/handlers/buy.py:1250-1260`)
**Issue:** `admin_reject_buy_callback` sets `rejected` and calls `release_order_inventory` for any status.
**Scenario:** Admin A approves, so the order goes to `payout_processing` and the coins are being sent. Admin B (who has their own copy of the buttons) taps Tolak. The reservation is released while the coins are still in flight, so another order can reserve the same stock and its payout fails. The user also gets a "Order Ditolak" DM even though the crypto arrives. If the order is already completed, it flips to `rejected`. A rejected BOT_BALANCE order is never refunded.
**Fix:** Use an atomic `UPDATE ... WHERE status IN ('pending','manual_review','expired') AND payout_tx_hash IS NULL`. Only release inventory/notify when rowcount == 1, and refund saldo for BOT_BALANCE.

### WR-02: Inventory reservation leaks on failed payout

**File:** `bot/handlers/buy.py:1062-1074`
**Issue:** The failure branch sets `manual_review` but never calls `release_order_inventory`, even for a pre-broadcast failure where no coins left the wallet.
**Scenario:** An RPC outage makes three payouts fail before broadcast. Their reservations stay RESERVED, so `get_available_inventory` under-reports stock indefinitely and new buys are refused or pushed to manual_review.
**Fix:** If `result["tx_hash"]` is empty (nothing broadcast), release the reservation. Keep it only when a hash exists.

### WR-03: Late payments are lost: hard expiry with no grace, proof photo silently ignored, topup expired before final check

**File:** `database/crud.py:359-366`, `main.py:1043-1045`, `main.py:622-626`, `bot/handlers/buy.py:1215-1217`
**Issue:**
- Orders expire at exactly `ORDER_EXPIRE_MINUTES`, while the UI says mutations take 30-60 s to sync.
- The poller only scans `pending`, and the photo router only looks for `pending` orders/topups; otherwise it returns with no reply.
- The topup poller marks a topup EXPIRED *before* checking payment, unlike the buy expiry job, which re-checks first.

**Scenario:** The user pays at minute 14:40 and the sync lands at 15:20, after the order is already `expired`. Nothing ever detects the payment, and the user's proof photo gets no response.
**Fix:**
- Add a grace window of 2-3 min past the displayed deadline.
- Run a final `check_payment` (with `startTime`) before expiring topups.
- Make the photo router fall back to the user's most recent `expired` order/topup (e.g. last 24 h) and forward it to admins flagged "late payment".

### WR-04: QRIS totals above Rp 10.000.000 are unpayable, and the fallback caption is wrong

**File:** `bot/handlers/buy.py:804,845-849`, `bot/handlers/balance.py:198,223`, `services/qris_generator.py:77-78,128-132`
**Issue:** The amount validator allows a nominal up to 10.000.000. The final total adds MDR (up to 30.000) plus the code (up to 400), so it can reach 10.030.400. `generate_dynamic_qris_string` raises for anything over 10.000.000, and the code silently falls back to the static merchant image. The caption still says "Nominal ... akan muncul otomatis (QRIS Dinamis)".
**Scenario:** A nominal of Rp 9.990.000 gives a total of 10.020.xxx. The user gets a static QR, must type the amount by hand, and the amount is above the BI QRIS limit anyway, so the order cannot be paid.
**Fix:** Cap the nominal so that `nominal + mdr + 400 <= 10_000_000`, and validate it in `handle_amount_input` and `handle_custom_nominal_input`. Have `get_qris_image_stream` report whether the QR is dynamic, and change the caption when it fell back to static.

### WR-05: Second `query.answer()` on an already-answered callback: alerts never shown, one handler raises

**File:** `bot/handlers/buy.py:1144` then `1152/1161/1165/1173`; `bot/handlers/buy.py:520` then `530/538`; `bot/handlers/balance.py:275` then `282/286/289/301`
**Issue:** Each handler answers the callback first and then answers it again with `show_alert=True`. Telegram rejects a second answerCallbackQuery for the same query.
- In `check_buy_payment` and `check_topup_payment_manual`, the user never sees the "Akses ditolak" or "Order sudah berstatus X" alerts.
- In `handle_saved_wallet_selection`, the second answer is not caught, so the exception propagates to the global error handler and the user gets a generic error instead of "Alamat tidak cocok".

**Fix:** Defer the first `answer()` until the branch is known, or send the alert text as a message after the first answer.

### WR-06: `/topup` command always crashes

**File:** `bot/handlers/balance.py:491` -> `bot/handlers/balance.py:106-107`
**Issue:** `CommandHandler("topup", start_topup_callback)` runs `update.callback_query.answer()`, but `callback_query` is `None` for a command. The result is an AttributeError on every `/topup`.
**Fix:** Add a separate `start_topup_command` that uses `update.message.reply_text`, as `start_buy_command` does.

### WR-07: ID collisions abort order/topup creation

**File:** `bot/handlers/balance.py:191` with `database/models.py:32`; `bot/utils/formatter.py:92-100` with `database/models.py:49`
**Issue:**
- `topup_id = f"TOPUP-{int(timestamp)}"` has 1 s resolution. Two topups in the same second hit the unique constraint, `generate_and_send_qris` has no handler, and the user is left on "Menyiapkan invoice...".
- `generate_order_id` has only 36³ = 46.656 IDs per day. At about 300 orders/day, the chance of at least one collision is about 60%, and `create_order` raises into the generic "kesalahan internal" (buy.py:909-911).

**Fix:** Use `uuid4().hex[:12]` (or a DB sequence) in both IDs, and retry on IntegrityError.

### WR-08: Referral discount slot consumed for unpaid QRIS orders and applied without quota

**File:** `bot/handlers/buy.py:274-282,710-726,830`; `database/crud.py:1702-1704`
**Issue:**
- The slot is consumed when the QRIS order is created. If the order expires unpaid, the slot is gone.
- Eligibility is decided at amount input. If the last slot is used elsewhere before confirmation, `consume_referral_discount` returns `None` silently and the order keeps the discounted fee anyway.

**Fix:** Consume the slot in `finalize_gopay_buy_payment` once the order reaches `paid`. At confirmation, re-check `get_referral_discount_info` and recompute the fee if the discount is no longer active.

### WR-09: Watchdog referral-discount activation is dead code; loyalty never counted for normal buys

**File:** `services/payout_watchdog.py:167-173,201-220`, `database/crud.py:228-233`, `bot/handlers/buy.py:1029-1061`
**Issue:**
- `update_order_status(..., "completed")` already calls `complete_referral`, so the second call at payout_watchdog.py:203 always returns False. `activate_discount_for_referrer` and the reward DM never run.
- `process_loyalty_after_order` is only called from the watchdog. Orders completed by the normal finalize path never count toward loyalty windows.

**Fix:** Move the post-completion side effects (referral discount activation and notification, loyalty) into one `on_order_completed(db, order, bot)` and call it from finalize success and from watchdog success. Have `update_order_status` return whether it completed the referral, or drop the inline call.

### WR-10: Reverted payouts cannot be re-approved, and the admin is told the order is COMPLETED

**File:** `bot/handlers/admin.py:3230-3232`, `bot/handlers/buy.py:966-967`, `services/payout_watchdog.py:243-261`
**Issue:** After an on-chain revert, the order stays `manual_review` with `payout_tx_hash` set. Approve answers "Order ini sudah COMPLETED", and finalize returns early.
**Scenario:** The admin follows the watchdog's "Kirim ulang secara manual" alert, taps Approve, sees "COMPLETED", and assumes the user was paid. The user never receives the coins.
**Fix:** Have the watchdog move reverted orders to a distinct status, e.g. `payout_reverted`, and clear or archive the hash. Let admin approval for that status claim and re-send, and say "reverted" rather than "completed".

### WR-11: Balance credit/debit is Python read-modify-write on float

**File:** `database/crud.py:967-984`, `database/crud.py:987-1008`
**Issue:** The new balance is computed in Python as `float(balance) ± amount` and written back. Today this is safe only because there is one process (`ecosystem.config.js` instances: 1) and no `await` between read and commit. A second worker, or a future async DB call, would lose updates: two concurrent debits each see the old balance, so the user spends the same saldo twice.
**Fix:** Do it atomically in SQL:
```python
db.execute(update(User).where(User.telegram_id == tid, User.balance_idr >= amt)
           .values(balance_idr=User.balance_idr - amt))  # rowcount==1 => success
```

### WR-12: Paid-but-unreservable stock only surfaces after payment

**File:** `bot/handlers/buy.py:683-707` vs `bot/handlers/buy.py:996-1017`
**Issue:** Stock is checked at confirmation but only reserved at finalize. N concurrent QRIS orders can each pass the check against the same stock, and after payment all but one go to manual_review. This is noted for completeness and is an acceptable trade-off only if it is intentional.
**Fix:** Reserve inventory at order creation with a TTL equal to the order expiry, and release it on expire/reject.

## Info

### IN-01: Unique-code fallback range is dead logic

**File:** `database/crud.py:821-825`
**Issue:** When codes 1..400 are exhausted, the fallback searches 1..200, which is a subset and therefore also exhausted, then returns a random colliding code.
**Fix:** Return an error ("antrian penuh") instead of a colliding code. This becomes moot once CR-01's total-uniqueness fix lands.

### IN-02: `create_order` inflates `total_orders` / `total_spent_idr` for unpaid, expired and deleted orders

**File:** `database/crud.py:154-158`; `bot/handlers/buy.py:758-760` (BOT_BALANCE rollback deletes the order but not the stat increment)
**Fix:** Increment stats on completion, not on creation.

---

_Reviewed: 2026-10-06_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
