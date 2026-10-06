---
phase: 04-core
reviewed: 2026-10-05T17:17:32Z
depth: deep
files_reviewed: 25
files_reviewed_list:
  - main.py
  - database/crud.py
  - database/models.py
  - database/connection.py
  - services/price_service.py
  - services/wallet_sync.py
  - services/coin_api_monitor.py
  - services/crypto_sender/__init__.py
  - services/crypto_sender/evm_sender.py
  - services/crypto_sender/solana_sender.py
  - services/crypto_sender/tron_sender.py
  - services/crypto_sender/ton_sender.py
  - services/crypto_sender/sui_sender.py
  - services/crypto_sender/aptos_sender.py
  - bot/handlers/start.py
  - bot/handlers/stocks.py
  - bot/handlers/price.py
  - bot/handlers/saved_accounts.py
  - bot/handlers/calculator.py
  - bot/utils/flow_guard.py
  - bot/utils/telegram_utils.py
  - bot/handlers/buy.py (cross-file trace only: finalize/claim/expiry paths)
  - bot/handlers/balance.py (cross-file trace only: topup amount/unique code)
  - services/payout_service.py (cross-file trace only: retry semantics)
  - gopay-gateway/server.js (cross-file trace only: /transactions vs /check-payment claims)
findings:
  critical: 4
  warning: 14
  info: 9
  total: 27
status: issues_found
---

# Phase 04-core: Code Review Report

**Reviewed:** 2026-10-05T17:17:32Z
**Depth:** deep
**Files Reviewed:** 21 in scope + 4 traced
**Status:** issues_found

## Summary

Adversarial deep review of startup/scheduler (`main.py`), the DB layer, price/stock services, all six
crypto senders and the user-facing handlers listed in scope. Call chains were traced into `buy.py`
(finalize/claim), `balance.py` (topup invoice), `payout_service.py` (retry policy) and the GoPay
gateway (`/transactions` vs `/check-payment`) because the money paths cross those boundaries.

The runtime is a single process / single asyncio loop with synchronous SQLAlchemy calls, so most
read-modify-write balance updates in `crud.py` are *currently* serialized by the event loop (no
`await` between read and write). The real defects are elsewhere:

1. **GoPay payment matching is amount-only and its "consumed" registry is split across three
   unshared, in-memory places** — one bank transfer can fund a topup *and* a crypto buy (CR-01).
2. **Crash/restart recovery re-sends payouts** that were already broadcast, because the tx hash is
   only persisted after the sender returns (CR-02).
3. **Topups expire without a final payment check** and the expiry write is unguarded (CR-03).
4. **Silent Postgres -> SQLite fallback** splits the money ledger (CR-04).

Handler registration order was checked: the global `/start` `CommandHandler` precedes every
ConversationHandler (so conv `/start` fallbacks are unreachable) but `start_handler` explicitly
resets `_conversations`, so no state is stranded; the catch-all `CallbackQueryHandler` is last and no
entry-point callback is swallowed by it. Remaining handler issues are minor (WR-11, IN-05).
Send-path retry policy (`payout_service.send_crypto_with_retry`) is sound: only pre-broadcast,
non-`MANUAL_REVIEW` failures are retried, and every sender returns a hash/`MANUAL_REVIEW` once a
broadcast was attempted.

## Critical Issues

### CR-01: One GoPay payment can be matched by both a topup and a buy order (double spend); consumed-tx registry is in-memory and not shared

**File:** `main.py:993-1017`, `main.py:1057-1067`, `main.py:1093-1102`, `database/crud.py:795-828`
(context: `bot/handlers/buy.py:802-804`, `bot/handlers/balance.py:196-198`, `gopay-gateway/server.js:583-616,653-657`)

**Issue:**
- Matching is by amount only (`_match_transaction`, main.py:999-1003). The final payable amount is
  `base + mdr + unique_code` (buy.py:804, balance.py:198), but `generate_unique_payment_code`
  only guarantees the *code* is unused, not the *final amount*. A topup of 50,000+120 and a buy with
  total 50,100+20 both expect 50,120.
- Three independent "already used" registries exist and never see each other:
  `_topup_matched_tx_ids` (main.py:84), `_buy_matched_tx_ids` (main.py:87), and the gateway's
  `claimedTransactions` map used only by `/check-payment` (server.js:653-657). `/transactions`
  (used by both Python pollers) ignores gateway claims. So a tx consumed by topup `check_payment`
  (main.py:1047) is still matchable by the buy poller (main.py:1097), and vice-versa.
- All registries are process memory: after any restart (PM2 `max_memory_restart`, deploy) every
  recent tx is matchable again (guard is only "tx not older than order.created_at - 5 min").
- `_match_transaction` ignores `txn["status"]`; the gateway returns `refund`/`partial_refund` rows
  (server.js:549,600), so a refunded payment still funds an order.
- `generate_unique_payment_code` fallback (crud.py:821-825) searches `range(1, 201)`, a subset of
  the already-exhausted 1..400 range, then returns a random (colliding) code.

**Failure scenario:** attacker creates a buy order (total F), then creates topups with custom
nominals until one invoice equals F (each retry gets a fresh random code; ~400 cheap attempts), pays
F once. Topup poller (gateway `check_payment`) credits the topup; buy poller matches the same tx via
`/transactions` and auto-sends crypto. Natural collisions between unrelated users trigger the same
double fulfilment without any attacker.

**Fix:**
```python
# models.py — persistent, shared claim (mirror DepositClaim)
class GopayTxClaim(Base):
    __tablename__ = "gopay_tx_claims"
    tx_id = Column(String(100), primary_key=True)
    claimed_by = Column(String(60), nullable=False)   # order_id / topup_id
    created_at = Column(DateTime, default=datetime.utcnow)

# every path (topup poll, topup fallback, buy poll, check_buy_payment, expiry final-check)
# inserts the claim inside the same transaction as claim_order_paid / claim_topup_success,
# and treats IntegrityError as "already used".
```
Also: reject `txn.get("status") not in ("settlement", "capture")`; generate codes so that
`final_amount` is unique across all PENDING topups *and* pending orders (query existing final
amounts, not codes); have `/check-payment` return the tx id so it can be recorded in the same table.

### CR-02: Stale `payout_processing` reclaim re-sends crypto after a crash/restart (double payout)

**File:** `database/crud.py:289-309`, `main.py:1091`, `main.py:1110-1115`
(context: `bot/handlers/buy.py:978-981`, `bot/handlers/buy.py:1027-1037`)

**Issue:** `finalize_gopay_buy_payment` claims `paid -> payout_processing`, calls
`send_order_payout`, and only persists `payout_tx_hash` *after* the sender returns
(buy.py:1029-1037). Senders block until receipt/finality: EVM up to 60 s (evm_sender.py:525),
Tron up to 150 s (tron_sender.py:250), Solana ~40+ s, TON ~40+ s. The GoPay poller resumes every
`payout_processing` order with `payout_tx_hash IS NULL` (crud.py:332-345 via main.py:1091) using
`allow_recovery=True`, and `claim_stale_payout_processing` succeeds once `updated_at` is 120 s old.
The in-process `_finalize_locks` (buy.py:962) protects only the *same* process.

**Failure scenario:** order X broadcasts a TRC-20 payout; while waiting on `result.wait(timeout=150)`
the process restarts (PM2 memory restart at 500 MB, Railway redeploy, crash). On boot the poller
finds X in `payout_processing` with no hash; 120 s after the original claim it reclaims and sends
the coins a second time.

**Fix:** never auto-resend a stale `payout_processing` row. Either move it to `manual_review`
on reclaim, or persist the deterministic tx id before broadcast and reconcile on-chain:
```python
# sender API: on_signed(tx_hash) callback invoked before broadcast
async def _persist_hash(h):  # in finalize
    update_order_status(db, order.order_id, "payout_processing", payout_tx_hash=h)
# claim_stale_payout_processing -> only transitions to "manual_review"
.values(status="manual_review", failure_reason="Payout status unknown after restart", ...)
```
(EVM `candidate_hash`, Tron `txn.txid`, Solana signature, Sui `reference_digest`, TON message hash
are all available pre-broadcast.)

### CR-03: QRIS topups expire without a final payment check; expiry overwrites any status

**File:** `main.py:1042-1045`, `database/crud.py:1044-1060`
(context: `bot/handlers/admin.py:3306-3312`)

**Issue:** `_check` marks a topup `EXPIRED` as soon as `now > expires_at`, *before* calling
`check_payment`. Buy orders get a "final check before expire" (main.py:849-871); topups do not.
`update_topup_status` does an unconditional `topup.status = status` (no `WHERE status='PENDING'`).
Once EXPIRED, the topup leaves `get_pending_topup_orders`, the `/transactions` fallback ignores it,
and the admin "Approve" button refuses it because `claim_topup_success` requires `PENDING`
(admin.py:3310).

**Failure scenario:** user pays in the last minute of the invoice; GoPay mutation sync takes the
30-60 s the bot itself warns about (buy.py:1185). The next 20 s tick sees `now > expires_at` and
expires the topup. Money is in the merchant account, the user is never credited, and the approve
button says "sudah diproses sistem". Separately, a topup credited by the user's manual check in the
same tick can be overwritten from SUCCESS to EXPIRED (audit/report corruption).

**Fix:**
```python
if topup.expires_at and datetime.utcnow() > topup.expires_at:
    pay_res = await gopay_service.check_payment(topup.amount_idr, topup.topup_id)
    if pay_res.get("paid"):
        await _complete_topup(db, topup); return None
    # guarded transition
    db.execute(update(TopupOrder).where(TopupOrder.topup_id == topup.topup_id,
               TopupOrder.status == "PENDING").values(status="EXPIRED")); db.commit()
    return None
```
Keep a grace window (e.g. +10 min) during which `/transactions` matching still considers the
topup, and let admin approve `EXPIRED` topups explicitly.

### CR-04: Silent fallback from Postgres to local SQLite splits the money ledger

**File:** `database/connection.py:56-72`, `main.py:111-120`

**Issue:** if Postgres is unreachable at import time (transient DNS like
`postgres.railway.internal`, a 1-second outage during deploy) or `create_all` fails, the bot
switches to a fresh local `./p2p_bot.db` and keeps running. Balances, pending orders, referrals and
saved wallets "disappear"; new topups/orders are written only to the container-local SQLite file,
which is discarded on the next deploy.

**Failure scenario:** container boots during a Postgres blip -> SQLite. Users topping up via QRIS
are credited in SQLite and buy crypto; the next restart reconnects to Postgres where none of those
credits/orders exist (users lose paid balance, payouts/orders vanish from audit). Pending GoPay
orders in Postgres are invisible meanwhile, so their payments are never matched.

**Fix:** fail closed for a money ledger: retry Postgres with backoff and exit non-zero (let PM2 /
Railway restart) instead of falling back. If a SQLite mode is needed for local dev, gate it behind an
explicit `ALLOW_SQLITE_FALLBACK=true` that is never set in production, and alert admins loudly.

## Warnings

### WR-01: Non-atomic claim/credit sequences (lost credit or double referral reward on DB error)

**File:** `main.py:939-971`, `database/crud.py:1320-1353`, `database/crud.py:967-984`
**Issue:** `credit_user_balance` commits internally. `_complete_topup` commits the
`PENDING->SUCCESS` claim first, then credits; if the credit raises, the topup is SUCCESS with no
credit and no automated recovery (admin approve says "LUNAS"). `complete_referral` credits the
referrer (commit #1), then the referee (commit #2), then marks the referral COMPLETED (commit #3);
a failure after commit #1 rolls back only the status, so the referral stays PENDING and the next
completed order pays the referrer again.
**Fix:** add `commit: bool = True` parameters (or a `_credit_no_commit` helper) and perform
claim + credit + audit + status change in one transaction with a single `db.commit()`; use an atomic
`UPDATE referrals SET status='COMPLETED' WHERE id=:id AND status='PENDING'` as the claim and only
credit when `rowcount == 1`.

### WR-02: `expire_stale_orders` expires BOT_BALANCE orders whose balance was already deducted (no refund)

**File:** `database/crud.py:360-366`, `database/crud.py:396-399` (context: `bot/handlers/buy.py:745-773`, `database/crud.py:332-345`)
**Issue:** BOT_BALANCE buys are created `pending`, the balance is deducted, `paid_at` set, and the
status deliberately left `pending` (buy.py:771, comment says "paid"). Payout is a fire-and-forget
task. If the process restarts before `claim_order_paid`, nothing resumes it
(`get_gopay_resume_orders` filters `GOPAY_QRIS`), and after `ORDER_EXPIRE_MINUTES` the order is set
`expired` with the user's balance gone. Also, the expiry `UPDATE` has no status predicate.
**Fix:** set BOT_BALANCE orders to `paid` in the same commit as the deduction (and include them in
the resume query), and exclude `paid_at IS NOT NULL` from the pending-expiry query; use a guarded
`UPDATE ... WHERE status='pending'`.

### WR-03: Inventory accounting oversells after each payout; `release_order_inventory` can wipe other reservations

**File:** `database/crud.py:697-729` (context: `bot/handlers/buy.py:1038`, `database/crud.py:654-656`)
**Issue:** on payout success the reservation is released but `wallet_balances.balance` keeps the
pre-payout on-chain value until the next sync (every 5 min; freshness allows 15 min), so
`balance - reserved` overstates stock by the amount just sent -> next buyer pays, reservation passes,
sender then fails "Saldo hot wallet tidak cukup" -> manual review after payment. Also release is an
ORM read-modify-write of `reserved_balance`; on the recovery path the `WalletBalance` row was already
loaded at reserve time (crud.py:654) and, with `expire_on_commit=False`, the stale in-memory value
is written back, erasing reservations made by other orders during the send.
**Fix:** on successful payout do an atomic SQL update:
`UPDATE wallet_balances SET reserved_balance = MAX(reserved_balance - :amt, 0), balance = balance - :amt WHERE network=:n AND symbol=:s`
(and the same atomic form for release-on-cancel).

### WR-04: TON payout "success" can be reported for someone else's transfer

**File:** `services/crypto_sender/ton_sender.py:327-345`, `services/crypto_sender/ton_sender.py:358-393`
**Issue:** USDT success = "recipient jetton balance grew by >= amount" (line 379), with the baseline
read *after* broadcast and re-read in later iterations if the first read failed (372-376). Native
success = "any successful outgoing tx of ours to that address with that value in the last
60 s+" (336-344). The returned hash for USDT is any recent out-tx to our jetton wallet with no value
filter (382). Bounced transfers still have the out-message, so they count as success.
**Failure scenario:** user buys USDT to an exchange deposit address that receives >= amount from
someone else within ~40 s while our message fails/bounces -> order COMPLETED, user gets nothing.
Two consecutive same-amount native orders to the same address -> order B verified with A's tx.
**Fix:** snapshot the baseline before broadcast, verify by the external-message hash
(`/v2/blockchain/messages/{hash}/transaction` on tonapi) and check the resulting trace for
`success` and no bounce; never reuse a hash already stored on another order.

### WR-05: Deterministic pre-broadcast rejections recorded as "uncertain broadcast" with a phantom hash

**File:** `services/crypto_sender/solana_sender.py:248-268`, `services/crypto_sender/aptos_sender.py:176-186`
**Issue:** Solana sets `broadcast_attempted = True` before `sendTransaction`; a preflight rejection
(e.g. SOL to a new account below rent-exempt minimum) raises in `_rpc` after trying every RPC and
returns `MANUAL_REVIEW` *with* the signature. finalize stores it as `payout_tx_hash`
(buy.py:1066-1074), so the order can never be auto-retried (`if order.payout_tx_hash: return`) and
admins see a hash that never existed on chain. Conversely Aptos computes no hash before
`submit_bcs_transaction`; a submit timeout returns `tx_hash=""`, so an actually-landed transfer has
no trace and invites a manual duplicate send.
**Fix:** distinguish JSON-RPC error responses (definitive reject -> no hash, safe retry) from
transport errors (uncertain -> keep hash). For Aptos compute `signed.hash()` before submit and
return it on any submit exception.

### WR-06: Wallet anti-fraud check is bypassable and can be used to block legitimate owners

**File:** `database/crud.py:1458-1497` (callers `bot/handlers/saved_accounts.py:622-632`, `bot/handlers/buy.py:483-500`)
**Issue:** only 42-char `0x` addresses are canonicalized. SUI (`0x`+64 hex) and Aptos (`0x`+1..64
hex, leading zeros optional) are compared case/zero-sensitively, and TON has EQ/UQ/raw forms of the
same account (the repo already has `services.tx_verifier.ton_address` for this) — a second user
re-enters the same wallet with different casing/format and passes. In the other direction,
saved-wallet rows are free and unauthenticated yet count as ownership (line 1482-1487), the exact
"poisoning" the comment at 1456-1457 says it prevents for unpaid orders.
**Failure scenario:** attacker saves a victim's (or a shared exchange TON deposit) address; the
victim's buy flow then refuses their own address with `WALLET_DUPLICATE_WARNING`.
**Fix:** canonicalize per chain (lowercase + zero-pad hex for SUI/APTOS, `ton_address()` for TON)
and store a `normalized_address` column with an index; count only addresses bound by a paid/completed
order (not mere saved entries), or require the first-binder to have a completed order.

### WR-07: GoPay tx freshness guard mishandles timezones and skips the check when time is missing

**File:** `main.py:1005-1016`
**Issue:** `tx_dt.replace(tzinfo=None)` drops a non-UTC offset instead of converting; a
`+07:00` timestamp is treated as UTC, i.e. 7 h in the future, so payments up to ~7 h older than the
order pass the "after created_at - 5 min" guard. If the time field is absent or unparseable
(`ValueError -> pass`), there is no time check at all.
**Fix:** `tx_dt = tx_dt.astimezone(timezone.utc).replace(tzinfo=None)`; return `False` when the time
is missing/unparseable.

### WR-08: Price feed: alias overwrite, coins missing from OKX never fall back, 10-minute staleness at 0% spread

**File:** `services/price_service.py:257-265`, `services/price_service.py:310-319`, `services/price_service.py:29`
**Issue:** `_fetch_okx_usd` iterates `COINGECKO_IDS` and writes `usd[coin_id]` per *symbol*; the
alias `"BASE": "ethereum"` makes it look up `BASE-USDT` and, if OKX lists such a pair, overwrite the
ETH price with an unrelated token. When OKX succeeds, coins it lacks are only filled from the old
cache row (never from CoinGecko/Paprika), so on a cold start they have no price at all and later
they age out after 600 s. With a deliberate 0% spread, accepting quotes up to 600 s old (and the
OKX/fallback rows are stamped with fetch time, not exchange time) exposes the bot to stale-price
arbitrage during volatility.
**Fix:** iterate a canonical `{coin_id: okx_symbol}` map (no aliases); when OKX lacks coins, fetch
those ids from CoinGecko/Paprika in the same refresh; lower `MAX_PRICE_AGE_SECONDS` for quoting
(e.g. 60-120 s) and use OKX `ts` as `last_updated_at`.

### WR-09: Monthly report misses the last hours of every month and double-counts fees

**File:** `main.py:791-799`, `main.py:1132-1133`, `database/crud.py:892-906`
**Issue:** `CronTrigger(day="last", hour=9)` uses the scheduler's local zone (container is UTC ->
16:00 WIB), but the report window ends at 24:00 WIB of that day and the per-period guard prevents
regeneration, so orders/topups from 16:00-24:00 WIB on the last day are never reported. `total_idr =
volume + fee + topup` adds fees that are already inside buy `total_idr`.
**Fix:** run at 00:05 WIB on day 1 for the *previous* month (`timezone="Asia/Jakarta"`), compute
`now` in WIB, and define TOTAL MASUK as `sum(buy total) + topups` (fee is a breakdown, not an addend).

### WR-10: Alarm messages broadcast full RPC URLs (often containing API keys) to notification targets

**File:** `services/coin_api_monitor.py:641`, `services/coin_api_monitor.py:523-526`, `services/coin_api_monitor.py:644`
**Issue:** `format_alarm_message` prints `res['url']` (settings `*_RPC`, which for
Alchemy/Infura/NodeReal/QuickNode embed the key in the path) and `str(exc)` (httpx errors include the
URL) into messages sent to forum/group targets. `wallet_sync.py:31-33` explicitly avoids persisting
exception URLs for this reason; the monitor does the opposite.
**Fix:** redact before sending (`urlsplit` -> scheme+host only), and send `type(exc).__name__` plus a
sanitized reason; HTML-escape `error_detail`.

### WR-11: Saved-wallet text flow: `/cancel` is unreachable, junk text is saved as a wallet, duplicate save on closed session

**File:** `main.py:553`, `bot/handlers/saved_accounts.py:569`, `bot/handlers/saved_accounts.py:597-609`, `bot/handlers/saved_accounts.py:679-689`
**Issue:** the prompt tells users to type `/cancel`, but `_route_admin_text` is registered with
`~filters.COMMAND`, so `/cancel` never reaches line 569 and `awaiting_save_wallet` stays set; the
next unrelated message is consumed as an address. Any undetected text of 15-250 chars is saved with
`chain_type="OTHER"`. For non-EVM addresses the wallet is saved twice, the second time on a session
already closed at line 645.
**Fix:** register a global `CommandHandler("cancel", ...)` (after conversations) that clears the
flags; reject undetected addresses outright; delete the duplicate block at 679-689.

### WR-12: Referral deep link with unknown referrer breaks `/start` on Postgres

**File:** `database/crud.py:1275-1283`, `bot/handlers/start.py:185-229`
**Issue:** `create_referral` never checks that `referrer_id` exists; `Referral.referrer_id` is a FK
to `users`. On Postgres the commit raises `IntegrityError`, which `start.py` does not catch
(only `ValueError, TypeError` at line 228) -> outer handler replies "Terjadi kesalahan" and the main
menu is never shown; the session is left without rollback inside `create_referral`.
**Fix:** `if not get_user(db, referrer_id): return None`, and wrap the commit in
`try/except IntegrityError: db.rollback(); return None`.

### WR-13: DB sessions held open across network awaits (idle-in-transaction / pool exhaustion)

**File:** `bot/handlers/price.py:146-209`, `main.py:847-877`, `main.py:1031-1073`
**Issue:** `show_prices` opens a session, runs a query (starting a transaction) and then awaits 16
`get_price` calls, each of which can trigger a multi-provider refresh (10 s timeouts, serialized
behind `PriceService._lock`). `_job_expire_orders` keeps its transaction open across sequential
8 s `check_payment` calls. On Postgres (pool 5 + 10 overflow) a burst of `/price` users holds every
connection "idle in transaction", starving the payout/topup jobs (QueuePool timeout).
**Fix:** read configs once, `db.close()`/`commit()` before awaiting network I/O, and pass plain data
into `get_price`; in jobs, collect ids first and open short sessions per item.

### WR-14: Fee calculator shows "N/A (di atas batas)" for amounts that have published percent tiers

**File:** `bot/handlers/calculator.py:117-130`
**Issue:** hard caps 1,015,000 / 1,010,000 bypass `calculate_fee_idr`, but `USD_PERCENT_TIERS` /
`ALTCOIN_PERCENT_TIERS` (fee_service.py:99-109) price those amounts and the Price List shows them.
Users get no/incorrect fee simulation for >1 M IDR trades (also ignores gas surcharge and QRIS MDR).
**Fix:** call `calculate_fee_idr` for all amounts and show the gas-surcharge/MDR notes.

## Info

### IN-01: `get_user_by_identifier` defined twice
**File:** `database/crud.py:92`, `database/crud.py:2133` — the second silently shadows the first. Remove one.

### IN-02: Dead ETH gas-estimation guard
**File:** `services/crypto_sender/evm_sender.py:420`, `:471-475` — `gas_estimation_failed` is never set True. Remove or wire it.

### IN-03: Fetch-age staleness check is defeated by failure stamping
**File:** `services/price_service.py:320-322`, `:330-332` — failures set `_fetched_at=now`, so `age_fetch > MAX_PRICE_AGE_SECONDS` can never trigger; only per-row age protects. Track `_last_success_at` separately.

### IN-04: Misleading handler-priority comment; unreachable conv `/start` fallbacks
**File:** `main.py:538-540`, `main.py:482` — standalone `CommandHandler("start")` is registered first in group 0, so every ConversationHandler `CommandHandler("start", ...)` fallback is dead code (behaviour is still correct because `start_handler` calls `reset_user_conversations`). Fix the comment / drop the dead fallbacks.

### IN-05: `_finalize_locks` grows without bound
**File:** `bot/handlers/buy.py:962` — one `asyncio.Lock` per order id forever. Pop after completion.

### IN-06: W5 first-deploy message never expires
**File:** `services/crypto_sender/ton_sender.py:484` — `valid_until = 2**32-1` at seqno 0 keeps a stuck first payout replayable indefinitely; use `now+60` as for later seqnos.

### IN-07: Float conversion of 18-decimal amounts before send
**File:** `services/payout_service.py:104`, `:111` — `float(order.crypto_amount)` can round up the last wei digits vs the recorded/reserved amount; pass `Decimal` through to senders.

### IN-08: Gateway auto-spawn races PM2
**File:** `main.py:1167-1182` — under PM2 the bot may start a second `node server.js` before PM2's gateway binds :3005 (orphan / EADDRINUSE crash loop). Skip auto-spawn when running under PM2 (`PM2_HOME`/`pm_id` env).

### IN-09: Unescaped exception text in HTML notifications
**File:** `main.py:662`, `services/coin_api_monitor.py:644`, `:694` — `<code>{context.error}</code>` / `<i>{error_detail}</i>` break HTML parsing on `<...>` (e.g. SQLAlchemy `<Order at 0x..>`); `notify_admins` falls back to plain text so the message still arrives, but with raw tags. `html.escape()` the interpolated values.

---

_Reviewed: 2026-10-05T17:17:32Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
