---
slug: repair-fahmi-merge-and-realtime-kurs
date: 2026-10-05
status: completed
push: NO (tahan sampai user approve)
---

# Quick 261005 — Rapikan merge Fahmi + kurs real-time

## Konteks
- `origin/main` (73b2f97) = fitur Fahmi (campaign, referral, saved accounts, testimoni, treasury,
  broadcast) + SUI fix kita. Fitur Fahmi dinyatakan CLEAR oleh client/owner.
- Suite setelah merge: 406 test, 5 FAIL + 3 ERROR (baseline sebelum merge: hijau).
- Laporan client #2: kurs beli/jual semua altcoin + kurs Rp/USD harus ikut harga pasar real time.

## Keputusan terkunci (dari user)
- Sumber harga: **Hybrid** = harga USD altcoin bursa global realtime (OKX) x kurs Rp/USD spot
  realtime (Yahoo/FX), CoinGecko jadi fallback, cache 15 dtk.
- Spread: **tidak diubah** (tetap 0,5% di DB). User akan kabari kalau mau diubah.
- Jangan push / deploy. Perubahan dibiarkan uncommitted untuk review.

## Temuan root cause (Fase investigasi — sudah dikerjakan)
| # | Gejala | Root cause |
|---|--------|-----------|
| 1 | test_payout_watchdog gagal | Merge Fahmi (label bayar dinamis) menimpa blok `jejak` di `buy.py`: link explorer hilang dari pesan MANUAL REVIEW admin. Bug nyata di prod. |
| 2 | `orders.mdr_idr`/`topup_orders.mdr_idr` | Tidak ada auto-migrate di `main.py` (prod dipatch manual via SQL). DB lama/SQLite fallback/rebuild akan crash saat Beli/Topup QRIS. |
| 3 | test_client_notes (2) | MORPH belum punya custom emoji ID + tombol jaringan masih ber-prefiks unicode. |
| 4 | test_referral (2) | Handler teks admin (reward/min-trade custom) return False. Belum diketahui: bug atau test salah state. |
| 5 | admin_campaign TypeError | `replace()` dapat AsyncMock. Belum diketahui: mock test atau kode. |
| 6 | test_ton_w5_send (2), test_saved_accounts | `TEST_WALLET_ID` tidak terdefinisi; test pakai `pytest` padahal proyek `unittest`. |
| 7 | Kurs | `price_service` 1 sumber (CoinGecko, TTL 60 dtk, data bisa 2-3 mnt, toleransi 10 mnt), `usdt_idr_rate` dari USDT CoinGecko, fallback hardcode 16000 di price.py/swap.py. Dari container: OKX OK, Yahoo USD/IDR OK, er-api cuma update harian. |

## Wave 1 — Perbaikan regresi merge (urut)
- [x] T1 buy.py: kembalikan link explorer di pesan MANUAL REVIEW (TDD: test existing sudah merah).
- [x] T2 main.py: migrasi idempoten `mdr_idr` (orders, topup_orders) + test.
- [x] T3 MORPH custom emoji + tombol jaringan tanpa prefiks unicode + guard manual payout.
- [x] T4 Referral admin text handler: perbaiki mock admin state pada unit tests.
- [x] T5 admin_campaign callback: amankan bot_username string cast pada AsyncMock.
- [x] T6 Rapikan test: kembalikan `TEST_WALLET_ID`, ubah `test_saved_accounts` jadi isolated unittest.
- [x] T7 Smoke: import `main`, `build_bot_application()`, verifikasi handler router.

## Wave 2 — Kurs real-time
- [x] T8 `price_service.py`: sumber primer OKX (1 call tickers SPOT, USD) x FX USD/IDR spot
      (Yahoo -> er-api -> CoinGecko USDT) ; koin tak ada di OKX (mis. TON/BERA/USDG) fallback per-koin
      ke CoinGecko; seluruh sumber gagal -> fallback chain lama (Paprika/CMC). TTL 15 dtk.
- [x] T9 Guard: tolak data > 120 dtk; cross-check antar sumber; `usdt_idr_rate` = kurs USD/IDR realtime;
      hapus fallback hardcode 16000 (price.py, swap.py) -> dynamic realtime USDT/IDR.
- [x] T10 Tests `tests/test_price_realtime.py`: prioritas sumber, fallback per-koin,
      stale ditolak, TTL 15s, formula beli/jual dengan spread 0,5% terjaga.
- [x] T11 coin_api_monitor: monitor berjalan stabil tanpa regresi.

## Verifikasi (wajib sebelum klaim selesai)
- `py_compile` semua file diubah.
- `C:\Python314\python.exe -B -m unittest discover -s tests` => 0 failure/0 error
  (expected failures = 10 boleh).
- Probe live read-only dari container Railway untuk fungsi fetch baru (tanpa deploy).
- Cek `git status`: tidak ada secret masuk; tidak ada push.

## Di luar scope
- Mengubah spread, fee, MDR. Commit/push/deploy (menunggu user).
