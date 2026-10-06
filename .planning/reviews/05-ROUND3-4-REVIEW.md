---
phase: round3-4
reviewed: 2026-10-06T00:00:00Z
depth: deep
files_reviewed: 14
files_reviewed_list:
  - services/reward_service.py
  - services/campaign_service.py
  - services/detector.py
  - services/gopay_service.py
  - services/quote_guard.py
  - database/crud.py
  - database/models.py
  - bot/handlers/admin.py
  - bot/handlers/admin_campaign.py
  - bot/handlers/sell.py
  - bot/handlers/buy.py
  - bot/handlers/swap.py
  - bot/handlers/saved_accounts.py
  - main.py
findings:
  critical: 3
  warning: 8
  info: 7
  total: 18
status: issues_found
---

# Round 3/4 Code Review (reward batch, milestone exclusion, treasury funding, bank/wallet locks)

**Depth:** deep. Several claims below were reproduced with a throwaway SQLite probe (not committed).

## Summary

The `reward_batches` claim (DRAFT -> RUNNING via conditional UPDATE + rowcount) is sound against double-tap.
Authorization is admin-only and ownership-scoped. HTML escaping of admin text and user names is consistent.
LIKE-wildcard injection through `is_bank_account_taken_by_other` is NOT possible, because `normalize_account_number`
strips everything except alphanumerics. The real problems are: stale wizard flags that capture later admin input
(including an immediate, unconfirmed write), two anti-fraud lock bypasses, a 100x amount misparse, and
treasury/refund accounting holes on crash and partial-failure paths.

## Critical Issues

### CR-01: Stale admin wizard flags capture later admin text; milestone exclusion is applied immediately
**File:** `bot/handlers/admin.py:4184-4256` (router), `:1202` (`_clear_reward_flags`), `:1364` (`admin_panel_main`)
**Issue:** `admin_awaiting_reward_list`, `admin_awaiting_reward_msg` and `admin_awaiting_milestone_excl` are checked FIRST
in `admin_interactive_text_router`, but they are only cleared by the reward callbacks themselves or by `/cancel`.
Pressing any other panel button (`admin_panel_main`, "Kirim Saldo User", "Kelola User", ...) does not clear them, and the
other flows only pop their own flags.
Scenario: admin taps "Tambah Pengecualian", walks away via the dashboard, later opens "Kirim Saldo User" and types `@budi`.
The text hits branch 0c, which calls `crud.add_milestone_exclusion` immediately with no confirmation. `@budi` is silently
removed from Top Milestone (a possible winner loses the reward) and the send-balance flow never advances. Branches 0a/0b
swallow the same way: the text becomes a reward list, or the draft's `default_message` (e.g. "@someuser"), which would be
sent to recipients if the admin does not re-read the preview.
**Fix:** Make admin wizard flags mutually exclusive. Call one `_clear_admin_wizard_flags(context)` at the top of
`admin_panel_callback` for every `data` except the callbacks that intentionally set a flag, and set exactly one flag per
flow. Require a confirm step (or at least an echo plus an Undo button) before 0c writes.

### CR-02: Wallet lock is bypassed through the 1-tap saved wallet in Buy
**File:** `bot/handlers/buy.py:517-543` (`handle_saved_wallet_selection`); also `handle_order_confirmation`
**Issue:** `is_wallet_address_taken_by_other` is only called in `handle_wallet_input` (typed address) and swap
`input_target_addr`. `handle_saved_wallet_selection` passes the saved address straight to `_proceed_to_payment_selection`.
Locks are created only after a COMPLETED buy/swap, while saving in the profile never locks. So an attacker saves a victim's
address first (it is not yet locked), the victim completes a buy (address becomes locked), and the attacker can still pick it
with one tap. No lock check runs at order creation either, so a wallet locked during a long conversation also slips through.
**Fix:** In `handle_saved_wallet_selection` call `is_wallet_address_taken_by_other(db, wallet_address, user_id)` and reject
with `WALLET_DUPLICATE_WARNING`. Re-check in `handle_order_confirmation` just before `create_order`, for both the BOT_BALANCE
and GOPAY_QRIS branches.

### CR-03: Bank/e-wallet lock is trivially bypassed and can be poisoned (pipe injection)
**File:** `database/crud.py:1573-1593`, `bot/handlers/sell.py:520`, `:412-470`
**Issue:** The order stores the user's raw text: `buyer_wallet = f"{bank_name} | {bank_acc} | {bank_holder}"`. The check
only strips spaces and `-` from the stored side (`flat`) but compares against a fully normalized `acc`. Verified with a probe:
1. Stored `0812.3456.7890` (dots), `+62 812 3456`: another user's `081234567890` and `628123456` are NOT detected, so the lock is bypassed.
2. Input without commas: `bank_acc` becomes the whole line. Locked account `1234567890`, attacker types `1234567890 BCA`:
   `normalize` gives `1234567890BCA`, no match, bypass.
3. Pipes are not sanitized in bank name or holder. A user with one real completed sell and holder `x|5555555555|y` makes
   `...|5555555555|...` match, which locks a victim's account number to the attacker (griefing; the victim's sells get
   `BANK_DUPLICATE_WARNING`).
**Fix:** Normalize at write time and compare normalized values.
- At order creation store `normalize_account_number(bank_acc)` in the middle field.
- Strip or replace `|` in `bank_name`/`bank_holder` before composing `buyer_wallet`.
- Reject free-text input that has no comma (or extract digits explicitly).
- Optionally add an indexed `bank_account_norm` column on `orders` and compare equality instead of `LIKE '%|x|%'`.

## Warnings

### WR-01: `parse_amount` silently turns Indonesian decimals into 100x amounts
**File:** `services/reward_service.py:64-66`
**Issue:** With no suffix, `,` and `.` are both treated as thousands separators. `75.000,00` becomes 7.500.000 and
`50.000,50` becomes 5.000.050, both under `MAX_REWARD_IDR` and accepted (verified). Only the admin's eyeballing of the
preview stands between this and real money.
**Fix:** If the string matches `^\d{1,3}(\.\d{3})*,\d{1,2}$` (or `^\d+,\d{1,2}$`), treat the trailing `,dd` as decimals and
round or reject. If ambiguous (`1.5`, `5,5`) reject rather than guess.

### WR-02: Batch/campaign stuck RUNNING with treasury already deducted; no recovery or visibility
**File:** `services/reward_service.py:181-239`, `:208-213`; `services/campaign_service.py:349-360`; `bot/handlers/admin.py:1312`
**Issue:**
- After the DRAFT->RUNNING claim, any unexpected exception (e.g. from `try_deduct_bot_treasury`, `get_batch_items`,
  `topup_bot_treasury` on the refund, or a process kill) leaves the batch RUNNING forever. It can neither be re-run nor
  cancelled, because both require DRAFT. If the deduct had happened, the Kas Bot money is gone and some users may be
  credited (each user is committed individually).
- `build_reward_history_view` filters `status IN (COMPLETED, CANCELLED, DRAFT)`, so RUNNING batches are invisible even
  though `labels` has a RUNNING entry.
- `execute_campaign` deducts treasury BEFORE committing `status="RUNNING"`. A crash between the two leaves the campaign
  DRAFT, the treasury already charged, and a re-run charges it again. A crash mid-loop leaves it RUNNING with no refund.
**Fix:** Wrap the whole post-claim body in `try/except` that either reverts to DRAFT (nothing deducted yet) or marks
FAILED and refunds exactly the un-credited remainder. Include RUNNING in the history view and add an admin "reconcile
RUNNING" action that reads `result_json`/AuditLog. For campaigns, deduct inside the same transaction as the RUNNING claim
and the credits.

### WR-03: A recipient credited but marked FAILED is refunded to the Kas Bot (money created)
**File:** `services/reward_service.py:216-228`
**Issue:** `crud.credit_user_balance` commits the credit internally. The `AuditLog` add and `db.commit()` come after it, in
the same `try`. If the AuditLog commit raises (constraint, lock timeout, connection drop), the `except` rolls back, sets
`status=FAILED` and adds `item["amount"]` to `refund`. The user keeps the money AND the treasury is refunded, so IDR is
created.
**Fix:** Do the credit and the audit in one transaction (add the audit row before the single commit, without using
`credit_user_balance`'s internal commit), or split try blocks so only a failed credit counts toward `refund`.

### WR-04: Top Spender post-commit failure refunds the full amount after rewards are committed
**File:** `services/campaign_service.py:673-687`
**Issue:** The leftover refund `crud.topup_bot_treasury(...)` runs inside the same `try` as the main commit. If it
raises after `db.commit()` (rewards already credited), the `except` runs `topup_bot_treasury(db, needed, ...)`, refunding
the FULL `needed` although `total_given` was paid, so the treasury is over-credited by `total_given`.
**Fix:** Keep only the DB writes in the `try`. Run the leftover top-up after the `try`, with its own error handling and
log. Track a `committed` flag so the `except` refunds only when `committed` is false.

### WR-05: `try_deduct_bot_treasury` is a non-atomic read-then-write
**File:** `database/crud.py:2409-2421` (plus `deduct_bot_treasury`/`topup_bot_treasury`, `:2327`)
**Issue:** The balance is a text row in `loyalty_config`. The "strict" check is `get balance` -> compare -> `set value`.
Under the single event loop with synchronous DB calls there is no interleaving, but two processes (rolling deploy overlap,
or a second worker) can both pass the check and over-spend. The name `try_deduct` implies atomicity.
**Fix:** One conditional statement, e.g. `UPDATE loyalty_config SET value = CAST(CAST(value AS BIGINT) - :n AS TEXT)
WHERE key=:k AND CAST(value AS BIGINT) >= :n`, using rowcount as the claim. At least `SELECT ... FOR UPDATE` on PostgreSQL.

### WR-06: `escalate_user_hash` dedups per order, not per hash
**File:** `services/detector.py` (`escalate_user_hash`, the `already` query)
**Issue:** Only the first `DEPOSIT_HASH_NEEDS_REVIEW` per order notifies admins. If the admin judges hash #1 to be someone
else's deposit and the real user then submits hash #2, `user_hash_review_reason` escalates again, but `already` is true and
the call silently returns. The order sits in `WAITING_CRYPTO_DEPOSIT` with no admin notice until it expires.
**Fix:** Dedup on `(order_id, tx_hash)`, e.g. include the hash in the audit details and filter by it.

### WR-07: Admin messages are unbounded against Telegram's 4096-char limit
**File:** `bot/handlers/admin.py:1230-1289` (`_fmt_skipped`, `build_reward_preview_view`), `:1292-1308`, `:1327-1348`, `:4190-4205`
**Issue:**
- 30 recipients with long names (full_name up to ~128 chars) plus a skipped section can exceed 4096; `edit_message_text`
  then raises BadRequest and the admin cannot see or execute the draft.
- `parse_reward_lines` puts the unbounded `amount_text` into the error reason, and `_fmt_skipped` only caps the number
  of lines, not their length.
- The exclusion view lists every row with one button each (also capped at 100 buttons per keyboard).
- `build_reward_result_text` joins all failed/dropped/notif_fail labels.
**Fix:** Truncate labels (`label[:40]`) and reasons (`[:80]`), cap the total length with an ellipsis, and paginate the
exclusion list.

### WR-08: Rankings have no deterministic tie-break, so preview and execution can pay different users
**File:** `database/crud.py` (`get_top_spenders` `order_by`), `services/campaign_service.py:146`
**Issue:** `ORDER BY SUM(...) DESC` with no secondary key. At the 10th-place boundary (and in tier boundaries, which pay
different amounts) equal volumes can come back in a different order between the preview query and `execute_*`.
**Fix:** `.order_by(metric.desc(), Order.telegram_id.asc())` in both functions.

## Info

### IN-01: Wizard text helpers can raise on exotic Unicode / huge numbers
**File:** `services/reward_service.py:66`, `:63`; `bot/handlers/admin.py:4240`
`"²".isdigit()` is True but `int("²")` raises ValueError (probe confirmed). `"9"*400 + "k"` gives
`OverflowError: cannot convert float infinity to integer`. The milestone-exclusion branch also uses `isdigit()` then
`int()`. Admin-only, but it surfaces as an unhandled error and (for exclusion) leaves the flag set. Use `re.fullmatch(r"[0-9]+")`
and catch OverflowError.

### IN-02: Duplicate `get_user_by_identifier` definitions in crud
**File:** `database/crud.py:92` and `:2300`
The second definition silently shadows the first (the one with the try/except). Delete one.

### IN-03: Reward history ignores `admin_id`
**File:** `bot/handlers/admin.py:1311-1313`
`build_reward_history_view(db, admin_id)` never filters by `created_by`, so it lists other admins' batches and totals.
Probably fine for a shared finance view, but the unused parameter suggests it was intended.

### IN-04: `admin_reward_editmsg_` does not verify ownership/status when setting the flag
**File:** `bot/handlers/admin.py:1838-1842`
`admin_reward_batch_id` is set from callback data without validation (the write is later guarded by `set_default_message`).
Also `admin_reward_skipped` may belong to a different batch, so the preview can show the wrong "Dilewati" text.

### IN-05: Double `query.answer()` on admin alerts
**File:** `bot/handlers/start.py:244-248`, `bot/handlers/admin.py:1858-1873`
`menu_callback_handler` pre-answers every callback, so the later `query.answer(..., show_alert=True)` in reward exec
(`claimed`, `empty`, `treasury`) is either ignored or raises BadRequest. If it raises, the `except` at `admin.py:2154`
also answers and raises, and the intro view is never re-rendered on the error path. Move alert-producing answers out of the
pre-answer path, or edit the message text instead.

### IN-06: Milestone volume definitions differ between functions; payment-time parsing assumption
**File:** `services/campaign_service.py:119` vs `database/crud.py` (`MILESTONE_ORDER_TYPES`); `main.py` `_match_transaction`
`get_top_users_by_milestone` filters only on status=completed while `get_top_spenders` filters `order_type IN (buy, sell, swap)`.
Fine today (only those types exist) but diverges if another type is added. Separately, `_match_transaction` treats a naive
gateway timestamp as UTC; if the gateway returns local (WIB) time without a zone, a stale payment up to 7h old would pass
the `>= start` check. Verify the gateway format.

### IN-07: `quote_guard` compares the customer sell price against the market price
**File:** `services/quote_guard.py:55-58`, `bot/handlers/sell.py:494`
For Sell, `price_per_unit` is `sell_price_idr` while the re-quote uses `market_price_idr`. With 0% spread they are equal; if a
spread is ever re-enabled, every quote older than 60s will be rejected. Also, if `price_service.get_price` is cached for
>= 60s the re-quote returns the same price and the guard is a no-op.

---

_Reviewed: 2026-10-06_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
