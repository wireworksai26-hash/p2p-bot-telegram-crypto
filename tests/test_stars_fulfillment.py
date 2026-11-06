"""Pemenuhan otomatis Stars/Premium via Fragment: klien web, wallet khusus, orkestrasi, dan pengaman uang.

Semua dependensi (Fragment, wallet TON, harga TON) diganti objek palsu: tidak ada jaringan atau uang sungguhan.
"""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1", "DATABASE_URL": "sqlite:///:memory:", "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "1,2", "EVM_WALLET_ADDRESS": "0x" + "1" * 40, "EVM_PRIVATE_KEY": "",
})

import httpx  # noqa: E402

import database.models  # noqa: E402,F401
from config.settings import settings  # noqa: E402
from database import stars_crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import StarsOrder, User  # noqa: E402
from services import fragment_client as FC  # noqa: E402
from services import stars_fulfillment as F  # noqa: E402
from services.crypto_sender import SendResult  # noqa: E402

USER = 9100
HASH_SECRET = "hash-RAHASIA-1234567890"
COOKIE_SECRET = "stel_ssid=RAHASIA-COOKIE-abcdef"


def prepared(nano=1_000_000_000, n=1):
    return FC.PreparedPurchase(recipient_name="Teman", req_id="REQ1", messages=[
        {"address": "EQ" + "A" * 46, "amount_nano": nano, "payload_b64": "te6cckEBAQEAAgAAAA=="} for _ in range(n)])


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result or prepared(), error, []

    async def prepare_stars(self, username, quantity, wallet_address, public_key=None):
        self.calls.append(("stars", username, quantity, wallet_address))
        if self.error:
            raise self.error
        return self.result

    async def prepare_premium(self, username, months, wallet_address, public_key=None):
        self.calls.append(("premium", username, months, wallet_address))
        if self.error:
            raise self.error
        return self.result


class FakeWallet:
    wallet_address = "UQ" + "B" * 46

    def __init__(self, results=None):
        self.results, self.sent = list(results or [SendResult(True, "TXHASH1")]), []

    def public_key_hex(self):
        return "ab" * 32

    async def send_payload(self, to_address, amount_nano, payload_b64):
        self.sent.append((to_address, amount_nano, payload_b64))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


async def ton_price(value=40_000.0):
    return value


class _Db(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, username="budi_ku", balance_idr=Decimal("1000000")))
        self.db.commit()
        self.patches = [
            patch.object(settings, "STARS_AUTOFULFILL", True), patch.object(settings, "FRAGMENT_HASH", HASH_SECRET),
            patch.object(settings, "FRAGMENT_COOKIE", COOKIE_SECRET), patch.object(settings, "STARS_TON_PRIVATE_KEY", "ab" * 32),
            patch.object(settings, "STARS_TON_WALLET_ADDRESS", "UQ" + "B" * 46),
            patch.object(settings, "STARS_MAX_ORDER_IDR", 1_500_000), patch.object(settings, "STARS_DAILY_LIMIT_IDR", 5_000_000),
            patch.object(settings, "STARS_MIN_MARGIN_PCT", 0.0),
        ]
        for p in self.patches:
            p.start()
        self.admin_notes, self.user_msgs = AsyncMock(), AsyncMock()
        self.patches += [patch.object(F, "_notify_admin", new=self.admin_notes), patch.object(F, "_tell_user", new=self.user_msgs)]
        self.patches[-2].start()
        self.patches[-1].start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def paid_order(self, product="stars", package=100, recipient="teman_ku"):
        return stars_crud.create_balance_order(self.db, USER, product, package, recipient)

    def fresh(self, order):
        self.db.expire_all()
        return stars_crud.get_order(self.db, order.order_id)

    def balance(self):
        self.db.expire_all()
        return int(self.db.query(User).filter_by(telegram_id=USER).one().balance_idr)

    async def run_one(self, order, client=None, wallet=None, price=40_000.0):
        client = client or FakeClient()
        wallet = wallet or FakeWallet()
        result = await F.try_auto_fulfill(AsyncMock(), order.order_id, client=client, wallet=wallet,
                                          price_fn=lambda: ton_price(price))
        return result, client, wallet


class TestOrkestrasi(_Db):
    async def test_nonaktif_tidak_menyentuh_apa_pun(self):
        order = self.paid_order()
        with patch.object(settings, "STARS_AUTOFULFILL", False):
            result, client, wallet = await self.run_one(order)
        self.assertEqual(result, "disabled")
        self.assertEqual((client.calls, wallet.sent, self.fresh(order).status), ([], [], "paid"))

    async def test_konfigurasi_belum_lengkap_dilewati(self):
        order = self.paid_order()
        with patch.object(settings, "FRAGMENT_COOKIE", ""):
            self.assertEqual(F.missing_config(), ["FRAGMENT_COOKIE"])
            result, client, wallet = await self.run_one(order)
        self.assertEqual(result, "skipped")
        row = self.fresh(order)
        self.assertEqual((row.status, row.fulfill_attempts or 0), ("paid", 0))
        self.assertEqual(wallet.sent, [])

    async def test_sukses_stars(self):
        order = self.paid_order("stars", 100)  # harga 31.000
        result, client, wallet = await self.run_one(order, price=20_000.0)  # 1 TON = Rp 20.000: biaya Rp 20.000
        self.assertEqual(result, "completed")
        row = self.fresh(order)
        self.assertEqual((row.status, row.fulfill_mode, row.fulfill_ref, row.ton_spent_nano, row.handled_by),
                         ("completed", "auto", "TXHASH1", 1_000_000_000, 0))
        self.assertEqual(client.calls, [("stars", "teman_ku", 100, FakeWallet.wallet_address)])
        self.assertEqual(wallet.sent, [("EQ" + "A" * 46, 1_000_000_000, "te6cckEBAQEAAgAAAA==")])
        self.assertIn("sudah dikirim", self.user_msgs.await_args.args[2])
        self.assertIn("SELESAI otomatis", self.admin_notes.await_args.args[1])

    async def test_sukses_premium(self):
        order = self.paid_order("premium", 3)  # harga 228.000
        result, client, wallet = await self.run_one(order, price=100_000.0)
        self.assertEqual(result, "completed")
        self.assertEqual(client.calls[0][:3], ("premium", "teman_ku", 3))

    async def test_username_tidak_ditemukan_refund_otomatis(self):
        order = self.paid_order()
        before = self.balance()
        result, _, wallet = await self.run_one(order, client=FakeClient(error=FC.FragmentInvalidRecipient("not found")))
        self.assertEqual(result, "refunded")
        self.assertEqual(self.fresh(order).status, "rejected")
        self.assertEqual(self.balance(), before + 31_000)
        self.assertEqual(wallet.sent, [])
        self.assertIn("tidak ditemukan", self.user_msgs.await_args.args[2])

    async def test_sesi_kedaluwarsa_kembali_ke_manual_tanpa_bayar(self):
        order = self.paid_order()
        result, _, wallet = await self.run_one(order, client=FakeClient(error=FC.FragmentAuthError("auth")))
        self.assertEqual(result, "manual")
        row = self.fresh(order)
        self.assertEqual((row.status, row.fulfill_mode, row.fulfill_attempts), ("paid", "manual", 1))
        self.assertIn("kedaluwarsa", row.fulfill_error)
        self.assertEqual(wallet.sent, [])
        self.assertIn("MANUAL", self.admin_notes.await_args.args[1])

    async def test_margin_terlampaui_tidak_membayar(self):
        order = self.paid_order("stars", 100)  # jual Rp 31.000
        result, _, wallet = await self.run_one(order, price=40_000.0)  # biaya 1 TON = Rp 40.000 > 31.000
        self.assertEqual(result, "manual")
        self.assertEqual(wallet.sent, [])
        self.assertIn("margin", self.fresh(order).fulfill_error)

    async def test_margin_minimum_dihormati(self):
        order = self.paid_order("stars", 100)
        with patch.object(settings, "STARS_MIN_MARGIN_PCT", 20.0):  # biaya maks 24.800
            result, _, wallet = await self.run_one(order, price=30_000.0)
        self.assertEqual((result, wallet.sent), ("manual", []))
        order2 = self.paid_order("stars", 150)  # jual 45.000, maks 36.000
        with patch.object(settings, "STARS_MIN_MARGIN_PCT", 20.0):
            result, _, wallet = await self.run_one(order2, price=30_000.0)
        self.assertEqual(result, "completed")

    async def test_harga_ton_tidak_tersedia_manual(self):
        order = self.paid_order()
        result, _, wallet = await self.run_one(order, price=None)
        self.assertEqual((result, wallet.sent), ("manual", []))

    async def test_batas_per_pesanan_dan_harian(self):
        order = self.paid_order("stars", 100)
        with patch.object(settings, "STARS_MAX_ORDER_IDR", 20_000):
            result, client, wallet = await self.run_one(order)
        self.assertEqual((result, client.calls, wallet.sent), ("manual", [], []))
        self.assertEqual(self.fresh(order).status, "paid")

        order2 = self.paid_order("stars", 150)
        with patch.object(settings, "STARS_DAILY_LIMIT_IDR", 60_000):
            first, _, w1 = await self.run_one(order2, price=20_000.0)  # 45.000 <= 60.000
            self.assertEqual(first, "completed")
            order3 = self.paid_order("stars", 150)
            second, client, w2 = await self.run_one(order3, price=20_000.0)  # 45.000 + 45.000 > 60.000
        self.assertEqual((second, w2.sent), ("manual", []))

    async def test_wallet_gagal_pasti_sebelum_broadcast_kembali_ke_manual(self):
        order = self.paid_order()
        wallet = FakeWallet([SendResult(False, "", "MANUAL_REVIEW: Saldo wallet Fragment kurang (0 TON, butuh 1).")])
        result, _, wallet = await self.run_one(order, wallet=wallet, price=20_000.0)
        self.assertEqual(result, "manual")
        self.assertEqual((self.fresh(order).status, len(wallet.sent)), ("paid", 1))

    async def test_hasil_tidak_pasti_masuk_review_dan_tidak_diulang(self):
        order = self.paid_order()
        wallet = FakeWallet([SendResult(False, "msg:abc123", "MANUAL_REVIEW: broadcast belum pasti")])
        result, client, wallet = await self.run_one(order, wallet=wallet, price=20_000.0)
        self.assertEqual(result, "review")
        row = self.fresh(order)
        self.assertEqual((row.status, row.fulfill_ref), ("review", "msg:abc123"))
        self.assertIn("JANGAN kirim ulang", self.admin_notes.await_args.args[1])
        # sweeper tidak mencoba lagi pesanan yang sudah pernah dicoba / sedang review
        self.db.query(StarsOrder).update({"paid_at": datetime.utcnow() - timedelta(minutes=30)})
        self.db.commit()
        self.assertEqual(stars_crud.get_unattempted_paid(self.db), [])
        self.assertEqual(len(wallet.sent), 1)
        # admin bisa menyelesaikan dari review
        self.assertEqual(stars_crud.complete(self.db, order.order_id, 1).status, "completed")

    async def test_pesan_ganda_dibayar_semua_atau_review(self):
        order = self.paid_order("premium", 3)
        wallet = FakeWallet([SendResult(True, "T1"), SendResult(False, "msg:def", "belum pasti")])
        result, _, wallet = await self.run_one(order, client=FakeClient(prepared(100_000_000, n=2)), wallet=wallet, price=100_000.0)
        self.assertEqual(result, "review")
        self.assertEqual(len(wallet.sent), 2)
        self.assertEqual(self.fresh(order).ton_spent_nano, 100_000_000)  # yang sudah pasti terkirim

    async def test_kesalahan_tak_terduga_masuk_review(self):
        order = self.paid_order()
        result, _, wallet = await self.run_one(order, client=FakeClient(error=RuntimeError("meledak")))
        self.assertEqual(result, "review")
        self.assertEqual((self.fresh(order).status, wallet.sent), ("review", []))

    async def test_dua_pekerja_bersamaan_hanya_satu_membayar(self):
        order = self.paid_order()
        wallet = FakeWallet()
        client = FakeClient()
        results = await asyncio.gather(*[
            F.try_auto_fulfill(AsyncMock(), order.order_id, client=client, wallet=wallet, price_fn=lambda: ton_price(20_000.0))
            for _ in range(3)])
        self.assertEqual(sorted(results).count("completed"), 1)
        self.assertEqual(len(wallet.sent), 1)

    async def test_sweeper_mencoba_yang_belum_dicoba_dan_menandai_yang_macet(self):
        stale = self.paid_order("stars", 50)
        self.db.query(StarsOrder).filter_by(id=stale.id).update(
            {"status": "processing", "fulfill_mode": "auto", "fulfill_attempts": 1,
             "processing_at": datetime.utcnow() - timedelta(minutes=30)})
        fresh_unpaid = self.paid_order("stars", 75)
        self.db.query(StarsOrder).filter_by(id=fresh_unpaid.id).update({"paid_at": datetime.utcnow() - timedelta(minutes=5)})
        self.db.commit()
        tried = []

        async def fake_try(bot, order_id, **kw):
            tried.append(order_id)
            return "completed"

        with patch.object(F, "try_auto_fulfill", new=fake_try):
            stats = await F.sweep(AsyncMock())
        self.assertEqual(stats, {"tried": 1, "stale": 1})
        self.assertEqual(tried, [fresh_unpaid.order_id])
        self.assertEqual(self.fresh(stale).status, "review")

    async def test_sweeper_nonaktif_tidak_melakukan_apa_pun(self):
        order = self.paid_order()
        self.db.query(StarsOrder).update({"paid_at": datetime.utcnow() - timedelta(minutes=5)})
        self.db.commit()
        with patch.object(settings, "STARS_AUTOFULFILL", False):
            self.assertEqual(await F.sweep(AsyncMock()), {"tried": 0, "stale": 0})
        self.assertEqual(self.fresh(order).status, "paid")


class TestKartuDanAdmin(_Db):
    async def test_announce_paid_memicu_otomatis_hanya_bila_aktif(self):
        from bot.handlers import stars as H
        order = self.paid_order()
        trigger = AsyncMock()
        with patch.object(H, "notify_admins", new=AsyncMock()), patch("services.stars_fulfillment.try_auto_fulfill", new=trigger):
            await H.announce_paid(AsyncMock(), order, self.db)
            await asyncio.sleep(0)
            self.assertEqual(trigger.await_count, 1)
            with patch.object(settings, "STARS_AUTOFULFILL", False):
                await H.announce_paid(AsyncMock(), order, self.db)
                await asyncio.sleep(0)
            self.assertEqual(trigger.await_count, 1)

    def test_kartu_menjelaskan_mode_otomatis_review_dan_alasan_manual(self):
        from bot.handlers import stars as H
        order = self.paid_order()
        text, _ = H._admin_card(order, "u")
        self.assertIn("Mode otomatis aktif", text)
        order.fulfill_attempts, order.fulfill_error = 1, "sesi Fragment kedaluwarsa"
        text, _ = H._admin_card(order, "u")
        self.assertIn("Otomatis tidak dipakai: sesi Fragment kedaluwarsa", text)
        order.status, order.fulfill_ref = "review", "msg:abc"
        text, _ = H._admin_card(order, "u")
        self.assertIn("BUTUH DICEK", text)
        self.assertIn("msg:abc", text)

    async def test_admin_tidak_bisa_mengubah_pesanan_yang_sedang_diproses(self):
        from bot.handlers import stars as H
        order = self.paid_order()
        claimed = stars_crud.claim_fulfillment(self.db, order.order_id)
        self.assertEqual(claimed.status, "processing")
        query = MagicMock()
        query.data = f"admin_stp_ok_{order.order_id}"
        query.from_user = SimpleNamespace(id=1)
        query.answer = AsyncMock()
        update = SimpleNamespace(callback_query=query)
        with patch("bot.handlers.admin.is_admin", return_value=True):
            await H.admin_stars_callback(update, SimpleNamespace(bot=AsyncMock()))
        self.assertIn("diproses otomatis", query.answer.await_args.args[0])
        self.assertEqual(self.fresh(order).status, "processing")

    def test_antrean_admin_menampilkan_review(self):
        from bot.handlers import stars as H
        order = self.paid_order()
        stars_crud.claim_fulfillment(self.db, order.order_id)
        stars_crud.mark_review(self.db, order.order_id, "msg:x", "tidak pasti")
        text, markup = H.build_admin_stars_view(self.db)
        self.assertIn("🚨 CEK", text)
        self.assertEqual(stars_crud.paid_summary(self.db)[0], 1)

    async def test_starscheck_hanya_admin_dan_tanpa_rahasia(self):
        from bot.handlers import stars as H
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=99))
        with patch("bot.handlers.admin.is_admin", return_value=False):
            await H.stars_check_handler(update, SimpleNamespace())
        self.assertIn("tidak memiliki akses", message.reply_text.await_args.args[0])

        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))
        wallet = SimpleNamespace(wallet_address="UQ" + "B" * 46, verify_key_matches_address=lambda: "v5r1",
                                 get_balance=AsyncMock(return_value=2.5))
        client = SimpleNamespace(check_session=AsyncMock(return_value="Owner"))
        with patch("bot.handlers.admin.is_admin", return_value=True), patch.object(F, "build_wallet", return_value=wallet), \
             patch.object(F, "build_client", return_value=client), patch.object(F, "ton_price_idr", new=AsyncMock(return_value=40_000.0)):
            await H.stars_check_handler(update, SimpleNamespace())
        text = message.reply_text.await_args.args[0]
        for part in ("AKTIF", "lengkap", "v5r1", "2.5000 TON", "sesi" if False else "Sesi Fragment: ✅", "Rp 40.000"):
            self.assertIn(part, text)
        for secret in (HASH_SECRET, COOKIE_SECRET, "ab" * 32):
            self.assertNotIn(secret, text)

    async def test_starscheck_melaporkan_yang_belum_diisi(self):
        from bot.handlers import stars as H
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))
        with patch.object(settings, "STARS_AUTOFULFILL", False), patch.object(settings, "FRAGMENT_COOKIE", ""), \
             patch.object(settings, "STARS_TON_PRIVATE_KEY", ""), patch("bot.handlers.admin.is_admin", return_value=True), \
             patch.object(F, "ton_price_idr", new=AsyncMock(return_value=None)):
            await H.stars_check_handler(update, SimpleNamespace())
        text = message.reply_text.await_args.args[0]
        self.assertIn("NONAKTIF", text)
        self.assertIn("FRAGMENT_COOKIE", text)
        self.assertIn("Wallet: ⚪ belum diisi", text)


def _transport(handler):
    return httpx.MockTransport(handler)


class TestFragmentClient(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FC._LEARNED_HASH.clear()

    def tearDown(self):
        FC._LEARNED_HASH.clear()

    def client(self, handler):
        return FC.FragmentClient(HASH_SECRET, COOKIE_SECRET, transport=_transport(handler))

    @staticmethod
    def form(request):
        return dict(x.split("=", 1) for x in request.content.decode().split("&"))

    async def test_alur_stars_tiga_langkah_dan_parsing(self):
        seen = []

        def handler(request):
            body = self.form(request)
            seen.append(body["method"])
            self.assertIn(HASH_SECRET, str(request.url))
            self.assertEqual(request.headers["cookie"], COOKIE_SECRET)
            if body["method"] == "searchStarsRecipient":
                return httpx.Response(200, json={"found": {"recipient": "RCP1", "name": "Teman"}})
            if body["method"] == "initBuyStarsRequest":
                self.assertEqual((body["recipient"], body["quantity"], body["payment_method"]), ("RCP1", "100", "ton"))
                return httpx.Response(200, json={"req_id": "REQ9", "amount": "1.5"})
            self.assertEqual(body["method"], "getBuyStarsLink")
            account = json.loads(httpx.QueryParams(f"a={body['account']}")["a"]) if False else None
            return httpx.Response(200, json={"transaction": {"messages": [
                {"address": "EQdest", "amount": "1500000000", "payload": "te6cc"}]}})

        result = await self.client(handler).prepare_stars("teman_ku", 100, "UQwallet", "ab" * 32)
        self.assertEqual(seen, ["searchStarsRecipient", "initBuyStarsRequest", "getBuyStarsLink"])
        self.assertEqual((result.recipient_name, result.req_id, result.total_nano), ("Teman", "REQ9", 1_500_000_000))
        self.assertEqual(result.messages[0], {"address": "EQdest", "amount_nano": 1_500_000_000, "payload_b64": "te6cc"})

    async def test_pengirim_anonim_untuk_stars_dan_premium(self):
        """Permintaan klien: "Show my name to the recipient" tidak dicentang, penerima hanya melihat "Someone"."""
        link_bodies = []

        def handler(request):
            body = self.form(request)
            if body["method"].startswith("search"):
                return httpx.Response(200, json={"found": {"recipient": "R"}})
            if body["method"].startswith("init"):
                return httpx.Response(200, json={"req_id": "Q"})
            link_bodies.append(body)
            return httpx.Response(200, json={"transaction": {"messages": [{"address": "EQx", "amount": "9", "payload": ""}]}})

        client = self.client(handler)
        await client.prepare_stars("teman_ku", 50, "UQw")
        await client.prepare_premium("teman_ku", 3, "UQw")
        self.assertEqual([b["method"] for b in link_bodies], ["getBuyStarsLink", "getGiftPremiumLink"])
        self.assertEqual([b["show_sender"] for b in link_bodies], ["0", "0"])

    async def test_alur_premium(self):
        seen = []

        def handler(request):
            body = self.form(request)
            seen.append(body["method"])
            if body["method"] == "searchPremiumGiftRecipient":
                return httpx.Response(200, json={"found": {"recipient": "R2"}})
            if body["method"] == "initGiftPremiumRequest":
                self.assertEqual(body["months"], "6")
                return httpx.Response(200, json={"req_id": "Q"})
            return httpx.Response(200, json={"transaction": {"messages": [{"address": "EQx", "amount": "9", "payload": ""}]}})

        result = await self.client(handler).prepare_premium("teman_ku", 6, "UQw")
        self.assertEqual(seen, ["searchPremiumGiftRecipient", "initGiftPremiumRequest", "getGiftPremiumLink"])
        self.assertEqual(result.total_nano, 9)

    async def test_harga_berubah_dicoba_ulang_lalu_berhasil_atau_menyerah(self):
        attempts = {"n": 0}

        def handler(request):
            body = self.form(request)
            if body["method"] == "searchStarsRecipient":
                return httpx.Response(200, json={"found": {"recipient": "R"}})
            if body["method"] == "initBuyStarsRequest":
                attempts["n"] += 1
                if attempts["n"] < 3:
                    return httpx.Response(200, json={"error": "Price was changed, try again"})
                return httpx.Response(200, json={"req_id": "OK"})
            return httpx.Response(200, json={"transaction": {"messages": [{"address": "EQ", "amount": "5", "payload": ""}]}})

        self.assertEqual((await self.client(handler).prepare_stars("teman_ku", 50, "UQ")).req_id, "OK")
        attempts["n"] = -10  # selalu berubah
        with self.assertRaises(FC.FragmentPriceChanged):
            await self.client(handler).prepare_stars("teman_ku", 50, "UQ")

    async def test_pemetaan_error(self):
        cases = [("auth required", FC.FragmentAuthError), ("No Telegram users found", FC.FragmentInvalidRecipient),
                 ("something odd", FC.FragmentError)]
        for message, exc in cases:
            with self.subTest(message=message):
                with self.assertRaises(exc):
                    await self.client(lambda r, m=message: httpx.Response(200, json={"error": m})).prepare_stars("teman_ku", 50, "UQ")
        with self.assertRaises(FC.FragmentAuthError):
            await self.client(lambda r: httpx.Response(403, text="no")).prepare_stars("teman_ku", 50, "UQ")
        with self.assertRaises(FC.FragmentError):
            await self.client(lambda r: httpx.Response(200, text="<html>")).prepare_stars("teman_ku", 50, "UQ")

    async def test_rahasia_tidak_bocor_di_pesan_error(self):
        leak = {"error": f"gagal dengan {HASH_SECRET} dan {COOKIE_SECRET}"}
        with self.assertRaises(FC.FragmentError) as ctx:
            await self.client(lambda r: httpx.Response(200, json=leak)).prepare_stars("teman_ku", 50, "UQ")
        self.assertNotIn(HASH_SECRET, str(ctx.exception))
        self.assertNotIn(COOKIE_SECRET, str(ctx.exception))

    async def test_jaringan_putus_dan_transaksi_tidak_valid(self):
        def boom(request):
            raise httpx.ConnectError("x")

        with self.assertRaises(FC.FragmentError):
            await self.client(boom).prepare_stars("teman_ku", 50, "UQ")

        def bad(request):
            body = self.form(request)
            if body["method"] == "searchStarsRecipient":
                return httpx.Response(200, json={"found": {"recipient": "R"}})
            if body["method"] == "initBuyStarsRequest":
                return httpx.Response(200, json={"req_id": "Q"})
            return httpx.Response(200, json={"transaction": {"messages": [{"address": "EQ", "amount": "0", "payload": ""}]}})

        with self.assertRaises(FC.FragmentError):
            await self.client(bad).prepare_stars("teman_ku", 50, "UQ")

    async def test_validasi_paket_dan_cek_sesi(self):
        c = self.client(lambda r: httpx.Response(200, json={"found": {"recipient": "R", "name": "Owner"}}))
        with self.assertRaises(FC.FragmentError):
            await c.prepare_stars("teman_ku", 49, "UQ")
        with self.assertRaises(FC.FragmentError):
            await c.prepare_premium("teman_ku", 4, "UQ")
        self.assertEqual(await c.check_session("owner"), "Owner")
        self.assertTrue(c.configured())
        self.assertTrue(FC.FragmentClient("", "x").configured())   # hash opsional, cukup cookie
        self.assertFalse(FC.FragmentClient("h", "").configured())

    # ───── hash dibaca otomatis dari halaman Fragment ─────

    PAGE = ('<script>ajInit({"version":632,"apiUrl":"\\/api?hash=FRESH123abc","state":{"quickSearch":false}});</script>')

    def test_parse_api_hash(self):
        self.assertEqual(FC.parse_api_hash(self.PAGE), "FRESH123abc")
        self.assertEqual(FC.parse_api_hash('{"apiUrl": "/api?hash=ab12&x=1"}'), "ab12")
        for bad in ("", None, "<html></html>", '{"apiUrl":"/api"}'):
            self.assertIsNone(FC.parse_api_hash(bad), bad)

    async def test_tanpa_hash_dibaca_dari_halaman_memakai_cookie(self):
        calls = []

        def handler(request):
            calls.append((request.method, str(request.url).split("?")[0], request.url.params.get("hash")))
            if request.method == "GET":
                self.assertEqual(request.headers["cookie"], COOKIE_SECRET)
                return httpx.Response(200, text=self.PAGE)
            return httpx.Response(200, json={"found": {"recipient": "R", "name": "Owner"}})

        client = FC.FragmentClient("", COOKIE_SECRET, transport=_transport(handler))
        self.assertEqual(await client.check_session("owner"), "Owner")
        self.assertEqual(calls, [("GET", "https://fragment.com/stars", None),
                                 ("POST", "https://fragment.com/api", "FRESH123abc")])

    async def test_hash_basi_diambil_ulang_lalu_dicoba_sekali_lagi_dan_diingat(self):
        posts = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, text=self.PAGE)
            posts.append(request.url.params.get("hash"))
            if request.url.params.get("hash") == "STALE":
                return httpx.Response(200, json={"error": "Bad request"})
            return httpx.Response(200, json={"found": {"recipient": "R", "name": "Owner"}})

        first = FC.FragmentClient("STALE", COOKIE_SECRET, transport=_transport(handler))
        self.assertEqual(await first.check_session("owner"), "Owner")
        self.assertEqual(posts, ["STALE", "FRESH123abc"])
        # klien baru langsung memakai hash terbaru yang sudah dipelajari, tanpa gagal dulu
        posts.clear()
        second = FC.FragmentClient("STALE", COOKIE_SECRET, transport=_transport(handler))
        self.assertEqual(await second.check_session("owner"), "Owner")
        self.assertEqual(posts, ["FRESH123abc"])

    async def test_bad_request_tetap_gagal_bila_hash_sama_tidak_mengulang_tanpa_batas(self):
        posts = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, text='"apiUrl":"\\/api?hash=SAMA"')
            posts.append(request.url.params.get("hash"))
            return httpx.Response(200, json={"error": "Bad request"})

        client = FC.FragmentClient("SAMA", COOKIE_SECRET, transport=_transport(handler))
        with self.assertRaises(FC.FragmentBadRequest):
            await client.check_session("owner")
        self.assertEqual(posts, ["SAMA"])  # hash tidak berubah → tidak dicoba ulang

    async def test_halaman_tak_bisa_dibaca_dan_tanpa_hash_gagal_jelas_tanpa_bocor(self):
        def handler(request):
            return httpx.Response(302 if request.method == "GET" else 200, headers={"location": "/"}, text="login")

        client = FC.FragmentClient("", COOKIE_SECRET, transport=_transport(handler))
        with self.assertRaises(FC.FragmentAuthError) as ctx:
            await client.check_session("owner")
        self.assertNotIn(COOKIE_SECRET, str(ctx.exception))
        self.assertIn("FRAGMENT_COOKIE", str(ctx.exception))

    async def test_hash_isian_tidak_wajib_untuk_kelengkapan_konfigurasi(self):
        with patch.object(settings, "FRAGMENT_HASH", ""), patch.object(settings, "FRAGMENT_COOKIE", COOKIE_SECRET), \
             patch.object(settings, "STARS_TON_PRIVATE_KEY", "ab" * 32), \
             patch.object(settings, "STARS_TON_WALLET_ADDRESS", "UQwallet"):
            self.assertEqual(F.missing_config(), [])


class TestWalletFragment(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services.crypto_sender.fragment_wallet import FragmentWalletSender
        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        from tonsdk.crypto import mnemonic_new, mnemonic_to_wallet_key

        words = mnemonic_new()
        pub, priv = mnemonic_to_wallet_key(words)
        wallet = Wallets.ALL[WalletVersionEnum.v4r2](public_key=pub, private_key=priv, wc=0)
        self.address = wallet.address.to_string(False)
        self.patches = [patch.object(settings, "STARS_TON_PRIVATE_KEY", " ".join(words)),
                        patch.object(settings, "STARS_TON_WALLET_ADDRESS", self.address),
                        patch.object(settings, "TON_PRIVATE_KEY", "kunci-wallet-stok-JANGAN-DIPAKAI")]
        for p in self.patches:
            p.start()
        self.sender = FragmentWalletSender()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_memakai_kunci_khusus_bukan_kunci_stok(self):
        self.assertTrue(self.sender.configured())
        self.assertEqual(self.sender.wallet_address, self.address)
        self.assertEqual(self.sender.verify_key_matches_address(), "v4r2")
        self.assertEqual(len(self.sender.public_key_hex()), 64)

    def test_decode_payload(self):
        from services.crypto_sender.fragment_wallet import decode_payload
        self.assertEqual(decode_payload(""), b"")
        self.assertEqual(decode_payload("aGVsbG8"), b"hello")  # tanpa padding
        self.assertEqual(decode_payload("aGVsbG8="), b"hello")

    def test_alamat_tidak_cocok_ditolak(self):
        with patch.object(settings, "STARS_TON_WALLET_ADDRESS", "EQDtFpEwcFAEcRe5mLVh2N6C0x-_hJEM7W61_JLnSF74p4q2"):
            sender = type(self.sender)()
            with self.assertRaises(Exception):
                sender.verify_key_matches_address()

    async def test_preflight_gagal_pasti_belum_ada_uang_keluar(self):
        result = await self.sender.send_payload("bukan-alamat", 1_000_000_000, "te6cc")
        self.assertFalse(result.success)
        self.assertEqual(result.tx_hash, "")  # konvensi: kosong = pasti belum keluar
        zero = await self.sender.send_payload("EQDtFpEwcFAEcRe5mLVh2N6C0x-_hJEM7W61_JLnSF74p4q2", 0, "")
        self.assertEqual((zero.success, zero.tx_hash), (False, ""))

    async def test_saldo_kurang_ditolak_sebelum_menandatangani(self):
        with patch.object(type(self.sender), "get_balance", new=AsyncMock(return_value=0.5)):
            result = await self.sender.send_payload(
                "EQDtFpEwcFAEcRe5mLVh2N6C0x-_hJEM7W61_JLnSF74p4q2", 1_000_000_000, "")
        self.assertFalse(result.success)
        self.assertEqual(result.tx_hash, "")
        self.assertIn("Saldo wallet Fragment kurang", result.error_message)

    async def test_saldo_tak_terbaca_ditolak(self):
        with patch.object(type(self.sender), "get_balance", new=AsyncMock(side_effect=RuntimeError("x"))):
            result = await self.sender.send_payload(
                "EQDtFpEwcFAEcRe5mLVh2N6C0x-_hJEM7W61_JLnSF74p4q2", 1_000_000_000, "")
        self.assertEqual((result.success, result.tx_hash), (False, ""))

    def test_wallet_stok_tidak_terpengaruh(self):
        from services.crypto_sender.ton_sender import TonSender
        with patch.object(settings, "TON_PRIVATE_KEY", "ab" * 32):
            self.assertEqual(TonSender()._raw_private_key(), "ab" * 32)
        self.assertNotEqual(self.sender._raw_private_key(), settings.TON_PRIVATE_KEY)


class TestKonfigurasi(unittest.TestCase):
    def test_bawaan_nonaktif_dan_aman(self):
        """Dibaca di proses terpisah (env bersih) supaya tidak mengganggu objek settings milik tes lain."""
        import subprocess
        env = {k: v for k, v in os.environ.items() if not k.startswith(("STARS_", "FRAGMENT_"))}
        code = (
            "import sys; sys.path.insert(0, r'%s'); sys.path.insert(0, r'%s')\n"
            "from config.settings import settings as s\n"
            "print(s.STARS_AUTOFULFILL, repr(s.FRAGMENT_HASH), repr(s.FRAGMENT_COOKIE), repr(s.STARS_TON_PRIVATE_KEY), "
            "repr(s.STARS_TON_WALLET_ADDRESS), s.STARS_MAX_ORDER_IDR, s.STARS_DAILY_LIMIT_IDR, s.STARS_MIN_MARGIN_PCT)"
        ) % (str(ROOT), str(ROOT / ".testdeps"))
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        last = out.stdout.strip().splitlines()[-1]
        self.assertEqual(last, "False '' '' '' '' 1500000 5000000 0.0", out.stderr[-300:])


if __name__ == "__main__":
    unittest.main()
