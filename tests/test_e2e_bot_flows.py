"""Full-stack E2E: real telegram.Update -> real Application dispatch.

Unlike handler-level tests, these drive the bot exactly as Telegram does:
updates go through main.build_bot_application() (ConversationHandlers, flow
guard, catch-all router) and every Bot API call is captured by a fake HTTP
transport. Buttons are tapped from the keyboard of the previous screen.
"""
import itertools
import json
import os
import re
import time
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
# Tanpa ini alur Convert/Jual gagal bila modul dijalankan sendirian (wallet bot kosong).
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from telegram import Update
from telegram.request import BaseRequest

import main
import database.models  # noqa: F401  (registers tables)
from database import crud
from database.connection import Base, SessionLocal, engine
from database.models import Order, UserSavedBank
from config.assets import STOCK_ASSETS
from services.price_service import price_service

BOT_USER = {"id": 123456, "is_bot": True, "first_name": "HSN", "username": "Hsnpro_bot"}
_ids = itertools.count(1000)


class FakeTelegram(BaseRequest):
    """Records Bot API calls and returns minimal valid responses."""

    calls = []

    def __init__(self, *args, **kwargs):
        pass

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        FakeTelegram.calls.append((endpoint, params))
        if endpoint == "getMe":
            result = {**BOT_USER, "can_join_groups": True, "can_read_all_group_messages": False,
                      "supports_inline_queries": False}
        elif endpoint in ("answerCallbackQuery", "sendChatAction", "deleteMessage",
                          "editMessageReplyMarkup", "setMyCommands"):
            result = True
        else:
            result = {"message_id": next(_ids), "date": int(time.time()), "from": BOT_USER,
                      "chat": {"id": int(params.get("chat_id") or 0), "type": "private"},
                      "text": params.get("text") or params.get("caption") or ""}
        return 200, json.dumps({"ok": True, "result": result}).encode()


async def fake_price(symbol, db=None):
    px = {"USDT": 17915, "USDC": 17915, "ETH": 72_000_000}.get(symbol.upper(), 10_000)
    return {"symbol": symbol.upper(), "market_price_idr": px, "buy_price_idr": px,
            "sell_price_idr": px, "spread_pct": 0.0, "usdt_idr_rate": 17915.0,
            "source": "TEST", "price_updated_at": int(time.time())}


def _plain(html):
    return re.sub(r"<[^>]+>", "", html or "").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


class BotFlowE2E(unittest.IsolatedAsyncioTestCase):
    A, B = 70001, 70002
    WALLET = "0x71C839556CB3250b716773B3aBE329a4a796c9c6"

    async def asyncSetUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            for sym, net in STOCK_ASSETS:
                crud.update_wallet_balance(db, net, 100000.0, sym, "addr")
        finally:
            db.close()
        self.patches = [
            patch.object(main, "HTTPXRequest", FakeTelegram),
            patch.object(price_service, "get_price", side_effect=fake_price),
            patch.object(price_service, "_refresh", new=AsyncMock()),
            patch("services.gopay_service.gopay_service.check_payment",
                  new=AsyncMock(return_value={"paid": False})),
        ]
        for p in self.patches:
            p.start()
        FakeTelegram.calls = []
        self.app = main.build_bot_application()
        self.errors = []

        async def on_error(update, context):
            self.errors.append(context.error)

        self.app.error_handlers.clear()
        self.app.add_error_handler(on_error)
        await self.app.initialize()
        self.last_buttons = {}

    async def asyncTearDown(self):
        await self.app.shutdown()
        for p in reversed(self.patches):
            p.stop()
        Base.metadata.drop_all(bind=engine)
        self.assertEqual(self.errors, [], "handler raised during E2E flow")

    # --- driver -------------------------------------------------------
    def _user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": f"User{uid}", "username": f"u{uid}"}

    async def _dispatch(self, uid, payload):
        start = len(FakeTelegram.calls)
        await self.app.process_update(Update.de_json(payload, self.app.bot))
        shown = [p for e, p in FakeTelegram.calls[start:]
                 if e in ("sendMessage", "editMessageText", "sendPhoto") and int(p.get("chat_id") or uid) == uid]
        for p in shown:
            markup = p.get("reply_markup")
            markup = json.loads(markup) if isinstance(markup, str) else markup
            if markup and markup.get("inline_keyboard"):
                self.last_buttons[uid] = [b for row in markup["inline_keyboard"] for b in row]
        return "\n".join(_plain(p.get("text") or p.get("caption")) for p in shown)

    async def say(self, uid, text):
        msg = {"message_id": next(_ids), "date": int(time.time()), "text": text,
               "chat": {"id": uid, "type": "private"}, "from": self._user(uid)}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return await self._dispatch(uid, {"update_id": next(_ids), "message": msg})

    async def tap(self, uid, callback_data, from_screen=True):
        if from_screen:
            available = [b.get("callback_data") for b in self.last_buttons.get(uid, [])]
            self.assertIn(callback_data, available, f"button not on screen: {available}")
        query = {"id": str(next(_ids)), "from": self._user(uid), "chat_instance": "ci",
                 "data": callback_data,
                 "message": {"message_id": 1, "date": int(time.time()), "text": "x", "from": BOT_USER,
                             "chat": {"id": uid, "type": "private"}}}
        return await self._dispatch(uid, {"update_id": next(_ids), "callback_query": query})

    async def open_buy_amount_step(self, uid):
        await self.say(uid, "/start")
        await self.tap(uid, "menu_buy")
        await self.tap(uid, "buy_sym_USDT")
        await self.tap(uid, "buy_net_USDT_BSC")
        return await self.say(uid, "50000")

    # --- scenarios ----------------------------------------------------
    async def test_buy_qris_end_to_end(self):
        shown = await self.open_buy_amount_step(self.A)
        self.assertIn("Fee Layanan", shown)
        self.assertIn("Batas Waktu", await self._buy_to_qris(self.A) or "Batas Waktu")
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.telegram_id == self.A).one()
            self.assertEqual(order.status, "pending")
            self.assertTrue(1 <= order.unique_code <= 400)
            self.assertEqual(order.total_idr, 50000 + order.unique_code)
        finally:
            db.close()

    async def _buy_to_qris(self, uid):
        await self.say(uid, self.WALLET)
        await self.tap(uid, "paymethod_GOPAY_QRIS")
        shown = await self.tap(uid, "buy_confirm")
        self.assertIn("Batas Waktu: 15 Menit", shown)
        self.assertIn("Saya Sudah Transfer", [b["text"] for b in self.last_buttons[uid]])
        return shown

    async def test_wallet_of_other_user_is_rejected_in_buy(self):
        await self.open_buy_amount_step(self.A)
        await self._buy_to_qris(self.A)
        # Order A belum lunas → alamat belum terkunci, B masih boleh.
        await self.open_buy_amount_step(self.B)
        self.assertIn("PILIH METODE PEMBAYARAN", await self.say(self.B, self.WALLET))
        await self.say(self.B, "/cancel")
        # Setelah transaksi A sukses, alamat terkunci untuk A.
        db = SessionLocal()
        try:
            db.query(Order).filter(Order.telegram_id == self.A).update({Order.status: "completed"})
            db.commit()
        finally:
            db.close()
        await self.open_buy_amount_step(self.B)
        shown = await self.say(self.B, self.WALLET.lower())
        self.assertIn("Duplikat Addres", shown)
        # Masih di langkah input wallet: alamat lain diterima.
        shown = await self.say(self.B, "0x" + "b" * 40)
        self.assertIn("PILIH METODE PEMBAYARAN", shown)

    async def test_buy_summary_warns_wallet_will_be_locked(self):
        await self.open_buy_amount_step(self.A)
        await self.say(self.A, self.WALLET)
        shown = await self.tap(self.A, "paymethod_GOPAY_QRIS")
        self.assertIn("terkunci ke akun Telegram Anda", shown)
        self.assertLess(shown.index("terkunci"), len(shown))  # tampil di layar konfirmasi
        self.assertIn("buy_confirm", [b.get("callback_data") for b in self.last_buttons[self.A]])

    async def _convert_to_target_wallet_step(self, uid):
        await self.say(uid, "/start")
        await self.tap(uid, "start_swap")
        await self.tap(uid, "swap_src_sym_USDT")
        await self.tap(uid, "swap_src_net_BSC")
        await self.tap(uid, "swap_tgt_sym_ETH")
        await self.tap(uid, "swap_tgt_net_BASE")
        return await self.say(uid, "20")

    async def test_convert_summary_warns_and_locked_target_rejected(self):
        shown = await self._convert_to_target_wallet_step(self.A)
        self.assertIn("INPUT WALLET TUJUAN", shown)
        shown = await self.say(self.A, "0x" + "c" * 40)
        self.assertIn("RINGKASAN QUOTE CONVERT", shown)
        self.assertIn("terkunci ke akun Telegram Anda", shown)
        self.assertIn("confirm_swap_order", [b.get("callback_data") for b in self.last_buttons[self.A]])
        await self.say(self.A, "/cancel")
        # Alamat yang terkunci ke user lain ditolak di Convert juga.
        db = SessionLocal()
        try:
            db.add(Order(order_id="ORD-LOCK", telegram_id=self.B, order_type="buy", crypto_symbol="USDT",
                         network="BSC", crypto_amount=1, price_per_unit=1, nominal_idr=1, fee_idr=0,
                         total_idr=1, buyer_wallet=self.WALLET, status="completed"))
            db.commit()
        finally:
            db.close()
        await self._convert_to_target_wallet_step(self.A)
        self.assertIn("Duplikat Addres", await self.say(self.A, self.WALLET.lower()))

    async def test_stale_buy_quote_rejected_when_price_moved(self):
        from services import quote_guard
        await self.open_buy_amount_step(self.A)
        await self.say(self.A, self.WALLET)
        await self.tap(self.A, "paymethod_GOPAY_QRIS")

        async def moved(symbol, db=None):
            info = await fake_price(symbol, db)
            info["market_price_idr"] = info["buy_price_idr"] = info["market_price_idr"] * 1.02
            return info

        with patch.object(quote_guard, "QUOTE_FRESH_SECONDS", -1), \
             patch.object(price_service, "get_price", side_effect=moved):
            shown = await self.tap(self.A, "buy_confirm")
        self.assertIn("Harga pasar sudah berubah", shown)
        db = SessionLocal()
        try:
            self.assertEqual(db.query(Order).count(), 0)
        finally:
            db.close()

    async def test_stale_sell_quote_with_stable_price_still_works(self):
        from services import quote_guard
        with patch.object(quote_guard, "QUOTE_FRESH_SECONDS", -1):
            await self._sell_order_waiting_deposit(self.A)  # asserts order dibuat

    async def test_switching_flow_mid_buy_is_blocked(self):
        await self.open_buy_amount_step(self.A)
        shown = await self.tap(self.A, "menu_sell", from_screen=False)
        self.assertIn("Selesaikan dulu proses Beli Crypto", shown)

    async def test_stale_button_gets_a_reply(self):
        await self.say(self.A, "/start")
        shown = await self.tap(self.A, "buy_confirm", from_screen=False)
        self.assertIn("Sesi tombol ini sudah berakhir", shown)
        self.assertEqual([b.get("callback_data") for b in self.last_buttons[self.A]], ["menu_back"])

    async def test_cancel_when_idle_replies(self):
        shown = await self.say(self.A, "/cancel")
        self.assertIn("Tidak ada proses yang sedang berjalan", shown)

    async def test_cancel_inside_buy_ends_flow(self):
        await self.open_buy_amount_step(self.A)
        shown = await self.say(self.A, "/cancel")
        self.assertIn("dibatalkan", shown)
        # Flow benar-benar berakhir: teks berikutnya bukan input wallet.
        self.assertNotIn("Alamat Wallet", await self.say(self.A, self.WALLET))

    async def test_cancel_clears_pending_save_wallet_input(self):
        await self.say(self.A, "/start")
        await self.tap(self.A, "menu_balance")
        await self.tap(self.A, "menu_saved_wallets")
        await self.tap(self.A, "act_add_saved_wallet", from_screen=False)
        self.assertIn("dibatalkan", await self.say(self.A, "/cancel"))
        # Alamat yang diketik setelah batal tidak ikut tersimpan.
        await self.say(self.A, self.WALLET)
        db = SessionLocal()
        try:
            self.assertEqual(crud.get_user_saved_wallets(db, self.A), [])
        finally:
            db.close()

    async def _sell_order_waiting_deposit(self, uid):
        await self.say(uid, "/start")
        await self.tap(uid, "menu_sell")
        await self.tap(uid, "sell_sym_USDT")
        await self.tap(uid, "sell_net_USDT_BSC")
        await self.say(uid, "10")
        await self.say(uid, "BCA, 1234567890, Budi Santoso")
        shown = await self.tap(uid, "sell_confirm")
        self.assertIn("ORDER PENJUALAN DIBUAT", shown)
        self.assertIn("Batas Waktu Quote: 30 Menit", shown)  # QRIS 15, Jual 30
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.telegram_id == uid, Order.order_type == "sell").one()
            return order.order_id
        finally:
            db.close()

    def _status(self, order_id):
        db = SessionLocal()
        try:
            return db.query(Order).filter(Order.order_id == order_id).one().status
        finally:
            db.close()

    async def test_menu_button_after_sell_order_keeps_order_alive(self):
        # Dulu "Menu Utama" (ditampilkan bot sendiri setelah TX hash) membatalkan
        # order — deposit yang sudah dikirim user tak pernah diproses.
        order_id = await self._sell_order_waiting_deposit(self.A)
        await self.tap(self.A, "menu_back", from_screen=False)
        self.assertEqual(self._status(order_id), "WAITING_CRYPTO_DEPOSIT")
        # Flow berikutnya yang dibatalkan juga tidak menyentuh order lama.
        await self.tap(self.A, "menu_sell", from_screen=False)
        await self.tap(self.A, "sell_cancel", from_screen=False)
        self.assertEqual(self._status(order_id), "WAITING_CRYPTO_DEPOSIT")

    async def test_explicit_sell_cancel_cancels_order(self):
        order_id = await self._sell_order_waiting_deposit(self.A)
        await self.tap(self.A, "sell_cancel")
        self.assertEqual(self._status(order_id), "cancelled")

    # --- Kirim Reward ke User Pilihan (admin) ------------------------------
    ADMIN_UID, GIFT_A, GIFT_B = 9001, 9101, 9102

    def _seed_admin_world(self, treasury):
        db = SessionLocal()
        try:
            crud.create_user(db, telegram_id=self.ADMIN_UID, username="boss", full_name="Boss")
            crud.create_user(db, telegram_id=self.GIFT_A, username="budi", full_name="Budi")
            crud.create_user(db, telegram_id=self.GIFT_B, username=None, full_name="Citra")
            if treasury:
                crud.topup_bot_treasury(db, treasury, admin_id=self.ADMIN_UID)
        finally:
            db.close()
        patcher = patch("bot.handlers.admin.is_admin", side_effect=lambda uid: uid == self.ADMIN_UID)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _balance(self, uid):
        db = SessionLocal()
        try:
            return float(crud.get_user(db, uid).balance_idr or 0)
        finally:
            db.close()

    def _alerts(self):
        return [p.get("text") for e, p in FakeTelegram.calls if e == "answerCallbackQuery" and p.get("text")]

    async def _compose_reward(self, text_list, text_msg="Makasih ya kak, ini dari kami 🙏"):
        a = self.ADMIN_UID
        await self.tap(a, "admin_panel_reward", from_screen=False)
        await self.tap(a, "admin_reward_start")
        shown = await self.say(a, text_list)
        self.assertIn("penerima terbaca", shown)
        if text_msg is None:
            return await self.tap(a, next(b["callback_data"] for b in self.last_buttons[a]
                                          if b.get("callback_data", "").startswith("admin_reward_preview_")))
        return await self.say(a, text_msg)

    async def test_admin_sends_custom_rewards_end_to_end(self):
        self._seed_admin_world(treasury=200_000)
        preview = await self._compose_reward(
            f"@budi 50000\n{self.GIFT_B} 25k | halo {{nama}}, ini dari admin!")
        self.assertIn("KONFIRMASI REWARD", preview)
        self.assertIn("Total: Rp 75.000", preview)
        self.assertIn("Sisa Kas Bot setelah kirim: Rp 125.000", preview)
        self.assertNotIn("Kas Bot kurang", preview)

        done = await self.tap(self.ADMIN_UID, next(b["callback_data"] for b in self.last_buttons[self.ADMIN_UID]
                                                  if b.get("callback_data", "").startswith("admin_reward_exec_")))
        self.assertIn("REWARD TERKIRIM", done)
        self.assertEqual((self._balance(self.GIFT_A), self._balance(self.GIFT_B)), (50000, 25000))
        sent = {int(p["chat_id"]): p["text"] for e, p in FakeTelegram.calls
                if e == "sendMessage" and int(p.get("chat_id") or 0) in (self.GIFT_A, self.GIFT_B)}
        self.assertIn("Makasih ya kak", sent[self.GIFT_A])            # pesan custom untuk semua
        self.assertIn("halo Citra, ini dari admin!", sent[self.GIFT_B])  # pesan khusus + {nama}
        self.assertIn("+Rp 50.000", sent[self.GIFT_A])

    async def test_shortfall_asks_for_qris_topup_and_pays_nothing(self):
        self._seed_admin_world(treasury=10_000)
        preview = await self._compose_reward(f"@budi 50000\n{self.GIFT_B} 25000", text_msg=None)
        self.assertIn("Kas Bot kurang Rp 65.000", preview)
        buttons = [b.get("callback_data") for b in self.last_buttons[self.ADMIN_UID]]
        self.assertIn("admin_treasury_qris_menu", buttons)
        self.assertFalse([b for b in buttons if (b or "").startswith("admin_reward_exec_")])
        # Tombol lama/dipalsukan tetap tidak bisa membayar saat kas kurang.
        await self.tap(self.ADMIN_UID, "admin_reward_exec_1", from_screen=False)
        self.assertEqual((self._balance(self.GIFT_A), self._balance(self.GIFT_B)), (0, 0))
        # Setelah Kas Bot diisi, draft yang sama bisa dilanjutkan dari menu.
        db = SessionLocal()
        try:
            crud.topup_bot_treasury(db, 100_000, admin_id=self.ADMIN_UID)
        finally:
            db.close()
        await self.tap(self.ADMIN_UID, "admin_panel_reward", from_screen=False)
        resume = [b["callback_data"] for b in self.last_buttons[self.ADMIN_UID]
                  if b.get("callback_data", "").startswith("admin_reward_preview_")]
        self.assertTrue(resume, "draft harus bisa dilanjutkan setelah top-up")
        again = await self.tap(self.ADMIN_UID, resume[0])
        self.assertNotIn("Kas Bot kurang", again)

    async def test_double_tap_and_non_admin_cannot_double_pay(self):
        self._seed_admin_world(treasury=500_000)
        await self._compose_reward(f"@budi 50000", text_msg=None)
        exec_cb = next(b["callback_data"] for b in self.last_buttons[self.ADMIN_UID]
                       if b.get("callback_data", "").startswith("admin_reward_exec_"))
        await self.tap(self.ADMIN_UID, exec_cb)
        await self.tap(self.ADMIN_UID, exec_cb, from_screen=False)      # tap ganda / salinan lama
        self.assertEqual(self._balance(self.GIFT_A), 50000)             # bukan 100000
        await self.tap(self.A, exec_cb, from_screen=False)              # user biasa memalsukan callback
        self.assertEqual(self._balance(self.GIFT_A), 50000)
        self.assertTrue(any("Akses ditolak" in (t or "") for t in self._alerts()))

    async def test_unregistered_and_bad_lines_are_reported_not_paid(self):
        self._seed_admin_world(treasury=100_000)
        a = self.ADMIN_UID
        await self.tap(a, "admin_panel_reward", from_screen=False)
        await self.tap(a, "admin_reward_start")
        shown = await self.say(a, "@budi 50000\n@belumpernahstart 10000\nngawur\n@budi 20000")
        self.assertIn("1 penerima terbaca", shown)
        self.assertIn("belum terdaftar", shown)
        self.assertIn("Format salah", shown)
        self.assertIn("duplikat", shown)

    async def test_admin_manages_milestone_exclusions_via_panel(self):
        self._seed_admin_world(treasury=0)
        a = self.ADMIN_UID
        await self.tap(a, "admin_panel_top_spenders", from_screen=False)
        await self.tap(a, "admin_milestone_excl")
        await self.tap(a, "admin_milestone_excl_add")
        shown = await self.say(a, f"@budi admin channel airdrop\n{self.GIFT_B}\n@hantu")
        self.assertIn("Dikecualikan", shown)
        self.assertIn("tidak ditemukan", shown)          # @hantu
        db = SessionLocal()
        try:
            self.assertEqual(crud.get_milestone_excluded_ids(db), {self.GIFT_A, self.GIFT_B})
        finally:
            db.close()
        await self.tap(a, f"admin_milestone_excl_rm_{self.GIFT_B}", from_screen=False)
        db = SessionLocal()
        try:
            self.assertEqual(crud.get_milestone_excluded_ids(db), {self.GIFT_A})
        finally:
            db.close()

    async def test_admin_send_balance_confirm_shows_result_and_credits_once(self):
        # Regresi: impor lokal `format_idr` di admin_panel_callback membuat jalur ini
        # mengkredit lalu CRASH saat menyusun pesan → admin menekan lagi → kredit dobel.
        self._seed_admin_world(treasury=0)
        shown = await self.tap(self.ADMIN_UID, f"admin_send_bal_confirm_{self.GIFT_A}_50000", from_screen=False)
        self.assertIn("Nominal Terkirim", shown)
        self.assertEqual(self._balance(self.GIFT_A), 50000)

    async def test_admin_treasury_preset_topup_and_manual_set_views_render(self):
        self._seed_admin_world(treasury=0)
        await self.tap(self.ADMIN_UID, "admin_treasury_topup_100000", from_screen=False)
        self.assertTrue(any("Kas bot berhasil di-topup +Rp 100.000" in (t or "") for t in self._alerts()))
        shown = await self.tap(self.ADMIN_UID, "admin_treasury_set_manual", from_screen=False)
        self.assertIn("Saldo saat ini", shown)

    async def _sell_until_bank_step(self, uid):
        await self.say(uid, "/start")
        await self.tap(uid, "menu_sell")
        await self.tap(uid, "sell_sym_USDT")
        await self.tap(uid, "sell_net_USDT_BSC")
        return await self.say(uid, "10")

    async def test_sell_bank_account_locked_to_user_after_successful_sale(self):
        db = SessionLocal()
        try:
            crud.create_user(db, telegram_id=self.B, username="owner", full_name="Owner")
            db.add(Order(order_id="ORD-OWNER", telegram_id=self.B, order_type="sell", crypto_symbol="USDT",
                         network="BSC", crypto_amount=1, price_per_unit=1, nominal_idr=1, fee_idr=0, total_idr=1,
                         buyer_wallet="BCA | 1234567890 | Owner", status="completed"))
            db.commit()
        finally:
            db.close()
        await self._sell_until_bank_step(self.A)
        shown = await self.say(self.A, "BCA, 1234 5678 90, Pencuri")
        self.assertIn("Duplikat Rekening", shown)
        db = SessionLocal()
        try:  # rekening milik orang lain tidak boleh ikut tersimpan di profil A
            self.assertEqual(db.query(UserSavedBank).filter(UserSavedBank.telegram_id == self.A).count(), 0)
        finally:
            db.close()
        # Rekening lain yang bebas diterima dan menampilkan catatan kunci sebelum konfirmasi.
        shown = await self.say(self.A, "BCA, 9876543210, Budi Santoso")
        self.assertIn("RINGKASAN ORDER PENJUALAN", shown)
        self.assertIn("rekening/e-wallet di atas", shown)
        self.assertIn("terkunci ke akun Telegram Anda", shown)
        self.assertIn("sell_confirm", [b.get("callback_data") for b in self.last_buttons[self.A]])

    async def test_abandoned_exclusion_prompt_does_not_swallow_later_admin_text(self):
        # Regresi CR-01: prompt "Tambah Pengecualian" ditinggalkan, lalu admin memakai menu lain
        # dan mengetik @username → dulu username itu diam-diam dikecualikan dari Top Milestone.
        self._seed_admin_world(treasury=0)
        a = self.ADMIN_UID
        await self.tap(a, "admin_panel_top_spenders", from_screen=False)
        await self.tap(a, "admin_milestone_excl")
        await self.tap(a, "admin_milestone_excl_add")                 # prompt menunggu teks
        await self.tap(a, "admin_panel_main", from_screen=False)      # admin pindah menu
        await self.say(a, "@budi")                                    # teks biasa
        db = SessionLocal()
        try:
            self.assertEqual(crud.get_milestone_excluded_ids(db), set())
        finally:
            db.close()

    async def test_abandoned_reward_wizard_is_cancelled_by_any_other_button(self):
        self._seed_admin_world(treasury=0)
        a = self.ADMIN_UID
        await self.tap(a, "admin_panel_reward", from_screen=False)
        await self.tap(a, "admin_reward_start")
        await self.tap(a, "admin_panel_stats", from_screen=False)
        self.assertNotIn("penerima terbaca", await self.say(a, "@budi 50000"))

    async def test_saved_wallet_one_tap_cannot_use_address_locked_to_another_user(self):
        # Regresi CR-02: A menyimpan alamat B di profil (tidak mengunci); setelah B sukses beli
        # alamat itu terkunci — tombol 1-tap milik A tidak boleh meloloskannya.
        locked_addr = "0x" + "9" * 40
        db = SessionLocal()
        try:
            crud.create_user(db, telegram_id=self.B, username="owner", full_name="Owner")
            crud.create_user(db, telegram_id=self.A, username="thief", full_name="Thief")
            crud.save_user_wallet(db, self.A, locked_addr, network="BSC")
            db.add(Order(order_id="ORD-B-DONE", telegram_id=self.B, order_type="buy", crypto_symbol="USDT",
                         network="BSC", crypto_amount=1, price_per_unit=1, nominal_idr=1, fee_idr=0, total_idr=1,
                         buyer_wallet=locked_addr, status="completed"))
            db.commit()
        finally:
            db.close()
        await self.say(self.A, "/start")
        await self.tap(self.A, "menu_buy")
        await self.tap(self.A, "buy_sym_USDT")
        await self.tap(self.A, "buy_net_USDT_BSC")
        await self.say(self.A, "50000")
        one_tap = [b["callback_data"] for b in self.last_buttons[self.A]
                   if (b.get("callback_data") or "").startswith("buy_saved_wallet_")]
        self.assertTrue(one_tap, "tombol 1-tap harus ada")
        shown = await self.tap(self.A, one_tap[0])
        self.assertIn("Duplikat Addres", shown)
        self.assertNotIn("PILIH METODE PEMBAYARAN", shown)

    async def test_sell_pipe_characters_in_bank_input_are_neutralised(self):
        await self._sell_until_bank_step(self.A)
        await self.say(self.A, "BCA, 1234567890, Budi|5555555555|x")
        await self.tap(self.A, "sell_confirm")
        db = SessionLocal()
        try:
            stored = db.query(Order).filter(Order.telegram_id == self.A, Order.order_type == "sell").one().buyer_wallet
        finally:
            db.close()
        self.assertEqual(stored.count("|"), 2)                      # hanya pemisah kolom yang sah
        self.assertEqual(crud.account_number_keys(crud._bank_field_of(stored)), {"1234567890"})

    async def test_top_spender_panel_requires_funded_kas_bot(self):
        self._seed_admin_world(treasury=0)
        db = SessionLocal()
        try:
            for i, (uid, amt) in enumerate(((self.GIFT_A, 900_000), (self.GIFT_B, 400_000))):
                db.add(Order(order_id=f"ORD-TS{i}", telegram_id=uid, order_type=("sell" if i else "buy"),
                             crypto_symbol="USDT", network="BSC", crypto_amount=1, price_per_unit=1,
                             nominal_idr=amt, fee_idr=0, total_idr=amt, status="completed"))
            db.commit()
        finally:
            db.close()
        a = self.ADMIN_UID
        shown = await self.tap(a, "admin_panel_top_spenders", from_screen=False)
        self.assertIn("beli + jual + convert", shown)
        self.assertIn("Kas Bot kurang Rp 250.000", shown)                 # 150k + 100k
        buttons = [b.get("callback_data") for b in self.last_buttons[a]]
        self.assertIn("admin_treasury_qris_menu", buttons)
        self.assertFalse([b for b in buttons if (b or "").startswith("admin_top_spender_exec_")])
        # Tombol lama dipaksa: tidak ada yang terbayar.
        await self.tap(a, "admin_top_spender_exec_30", from_screen=False)
        self.assertEqual((self._balance(self.GIFT_A), self._balance(self.GIFT_B)), (0, 0))

        db = SessionLocal()
        try:
            crud.topup_bot_treasury(db, 300_000, admin_id=a)
        finally:
            db.close()
        shown = await self.tap(a, "admin_panel_top_spenders", from_screen=False)
        self.assertNotIn("Kas Bot kurang", shown)
        await self.tap(a, "admin_top_spender_exec_30", from_screen=False)
        self.assertEqual((self._balance(self.GIFT_A), self._balance(self.GIFT_B)), (150_000, 100_000))
        db = SessionLocal()
        try:
            self.assertEqual(crud.get_bot_treasury_balance(db), 50_000)
        finally:
            db.close()
        await self.tap(a, "admin_top_spender_exec_30", from_screen=False)  # tap ganda
        self.assertEqual(self._balance(self.GIFT_A), 150_000)

    async def test_cancel_clears_admin_wizard(self):
        self._seed_admin_world(treasury=0)
        a = self.ADMIN_UID
        await self.tap(a, "admin_panel_reward", from_screen=False)
        await self.tap(a, "admin_reward_start")
        self.assertIn("dibatalkan", await self.say(a, "/cancel"))
        # Teks berikutnya tidak lagi dianggap daftar reward.
        self.assertNotIn("penerima terbaca", await self.say(a, "@budi 50000"))

    async def test_topic_photo_uses_real_bot_not_bot_user(self):
        # ExtBot.bot adalah telegram.User milik bot; dulu kirim_ke_topik memakai itu
        # sehingga foto bukti tidak pernah masuk topik admin (TypeError chat_id).
        from types import SimpleNamespace
        from bot.utils import telegram_utils
        self.assertIs(telegram_utils.resolve_bot(self.app.bot), self.app.bot)
        self.assertIs(telegram_utils.resolve_bot(self.app), self.app.bot)
        row = SimpleNamespace(chat_id=-100123, thread_id=7)
        with patch.object(telegram_utils, "_target_row", return_value=row):
            start = len(FakeTelegram.calls)
            ok = await telegram_utils.kirim_ke_topik(self.app.bot, kind="beli", photo="FILEID", text="bukti")
        self.assertTrue(ok)
        sent = [p for e, p in FakeTelegram.calls[start:] if e == "sendPhoto"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(str(sent[0]["chat_id"]), "-100123")

    async def test_profile_survives_html_in_display_name(self):
        payload_user = {"id": self.A, "is_bot": False, "first_name": "Ali <3 & Co"}
        await self.say(self.A, "/start")
        query = {"id": "q1", "from": payload_user, "chat_instance": "ci", "data": "menu_balance",
                 "message": {"message_id": 1, "date": int(time.time()), "text": "x", "from": BOT_USER,
                             "chat": {"id": self.A, "type": "private"}}}
        start = len(FakeTelegram.calls)
        await self.app.process_update(Update.de_json({"update_id": 9, "callback_query": query}, self.app.bot))
        sent = [p for e, p in FakeTelegram.calls[start:] if e == "editMessageText"]
        self.assertTrue(sent)
        self.assertIn("Ali &lt;3 &amp; Co", sent[-1]["text"])


if __name__ == "__main__":
    unittest.main()
