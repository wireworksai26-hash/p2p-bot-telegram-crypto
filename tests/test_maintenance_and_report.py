"""Fitur baru: klausul AML/Judol di S&K, maintenance per jaringan/koin, dan tombol Laporkan Kendala Order."""
import asyncio
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "DATABASE_URL": "sqlite:///:memory:",
    "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "999",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from telegram.ext import ConversationHandler

from database.connection import Base, SessionLocal, engine
from database.models import Order, User
from services import chain_maintenance as cm


def _run(coro):
    return asyncio.run(coro)


def _query(data="x", user_id=111):
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=user_id), message=SimpleNamespace(chat_id=user_id),
        answer=AsyncMock(), edit_message_text=AsyncMock())


def _order(oid="ORD-1", uid=111, **kw):
    base = dict(order_id=oid, telegram_id=uid, order_type="buy", crypto_symbol="USDT", network="BSC",
                crypto_amount=Decimal("5"), price_per_unit=16000, nominal_idr=80000, fee_idr=3000,
                total_idr=80000, status="completed", payment_method="GOPAY_QRIS",
                payout_tx_hash="0x" + "ab" * 32, completed_at=datetime.utcnow())
    base.update(kw)
    return Order(**base)


class DbCase(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        db.add_all([User(telegram_id=111, username="u1"), User(telegram_id=222, username="u2")])
        db.commit()
        db.close()

    def tearDown(self):
        Base.metadata.drop_all(bind=engine)


class TestSnkClauses(unittest.TestCase):
    def test_klausul_aml_dan_judol_ada_dan_klausul_lama_utuh(self):
        from bot.utils.messages import SNK_TEXT
        self.assertIn("9. <b>Kebijakan Anti-Money Laundering (AML)", SNK_TEXT)
        self.assertIn("MENOLAK KERAS", SNK_TEXT)
        self.assertIn("pencucian uang, korupsi, penipuan", SNK_TEXT)
        self.assertIn("10. <b>Larangan Transaksi Judi Online (Judol)", SNK_TEXT)
        self.assertIn("TIDAK MELAYANI", SNK_TEXT)
        self.assertIn("situs/aplikasi judi online", SNK_TEXT)
        for old in ("1. Bot ini beroperasi", "7. Apabila ada saran", "8. Tidak menerima top up USD"):
            self.assertIn(old, SNK_TEXT)
        self.assertLess(len(SNK_TEXT), 4000)  # batas pesan Telegram


class TestChainMaintenanceService(DbCase):
    def test_nyalakan_cek_matikan(self):
        self.assertIsNone(cm.blocked_reason("USDT", "SOLANA"))
        self.assertTrue(cm.set_flag("network", "solana", True, note="RPC padat", admin_id=999))
        label, note = cm.blocked_reason("USDT", "SOLANA")
        self.assertEqual((label, note), ("Jaringan SOLANA", "RPC padat"))
        self.assertIsNone(cm.blocked_reason("USDT", "BSC"), "jaringan lain tidak terpengaruh")
        self.assertTrue(cm.set_flag("NETWORK", "SOLANA", False))
        self.assertIsNone(cm.blocked_reason("USDT", "SOLANA"))
        self.assertFalse(cm.set_flag("NETWORK", "SOLANA", False), "mematikan yang sudah mati tidak berubah")

    def test_maintenance_koin_memblokir_semua_jaringannya(self):
        cm.set_flag("COIN", "USDT", True)
        self.assertEqual(cm.blocked_reason("USDT", "BSC")[0], "Koin USDT")
        self.assertEqual(cm.blocked_reason("USDT", "TON")[0], "Koin USDT")
        self.assertIsNone(cm.blocked_reason("ETH", "BASE"))

    def test_input_tidak_valid_ditolak(self):
        with self.assertRaises(ValueError):
            cm.set_flag("CHAIN", "X", True)
        with self.assertRaises(ValueError):
            cm.set_flag("NETWORK", "", True)

    def test_gagal_baca_db_dianggap_normal(self):
        with patch("database.connection.SessionLocal", side_effect=RuntimeError("db down")):
            self.assertEqual(cm.load_flags(), {})
            self.assertIsNone(cm.blocked_reason("USDT", "SOLANA"))

    def test_teks_user_meng_escape_catatan(self):
        text = cm.maintenance_text(("Jaringan SOLANA", "<b>x</b> & y"))
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; y", text)
        self.assertIn("[Maintenance]", text)
        self.assertIn("tetap diproses", text)


class TestMaintenanceGuards(DbCase):
    def test_beli_pilih_jaringan_maintenance_diblokir(self):
        from bot.handlers import buy
        cm.set_flag("NETWORK", "BSC", True, note="gas tinggi")
        q = _query("buy_net_USDT_BSC")
        ctx = SimpleNamespace(user_data={})
        res = _run(buy.handle_network_selection(SimpleNamespace(callback_query=q), ctx))
        self.assertEqual(res, buy.SELECT_NETWORK)
        self.assertIn("[Maintenance]", q.edit_message_text.call_args.kwargs["text"])
        self.assertNotIn("buy_network", ctx.user_data, "tidak boleh lanjut ke input nominal")

    def test_beli_jaringan_normal_tidak_terpengaruh(self):
        from bot.handlers import buy
        cm.set_flag("NETWORK", "SOLANA", True)
        q = _query("buy_net_USDT_BSC")
        ctx = SimpleNamespace(user_data={})
        res = _run(buy.handle_network_selection(SimpleNamespace(callback_query=q), ctx))
        self.assertEqual(res, buy.INPUT_AMOUNT)
        self.assertEqual(ctx.user_data["buy_network"], "BSC")

    def test_beli_konfirmasi_diblokir_bila_maintenance_dinyalakan_setelah_pilih(self):
        from bot.handlers import buy
        cm.set_flag("COIN", "USDT", True)
        q = _query("buy_confirm")
        ctx = SimpleNamespace(user_data={"buy_symbol": "USDT", "buy_network": "BSC"})
        res = _run(buy.handle_order_confirmation(
            SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=111)), ctx))
        self.assertEqual(res, ConversationHandler.END)
        self.assertIn("[Maintenance]", q.edit_message_text.call_args.kwargs["text"])

    def test_jual_pilih_jaringan_dan_konfirmasi_diblokir(self):
        from bot.handlers import sell
        cm.set_flag("NETWORK", "BSC", True)
        q = _query("sell_net_USDT_BSC")
        ctx = SimpleNamespace(user_data={})
        with patch("bot.keyboards.crypto_select.sell_networks", return_value=["BSC"]):
            res = _run(sell.handle_network_selection(SimpleNamespace(callback_query=q), ctx))
        self.assertEqual(res, sell.SELECT_NETWORK)
        self.assertNotIn("sell_network", ctx.user_data)

        q2 = _query("sell_confirm")
        ctx2 = SimpleNamespace(user_data={
            "sell_order_id": "ORD-9", "sell_symbol": "USDT", "sell_network": "BSC", "sell_crypto_amount": 5,
            "sell_price_per_unit": 16000, "sell_gross_nominal_idr": 80000, "sell_fee_idr": 3000, "sell_net_idr": 77000})
        res2 = _run(sell.handle_order_confirmation(
            SimpleNamespace(callback_query=q2, effective_user=SimpleNamespace(id=111)), ctx2))
        self.assertEqual(res2, ConversationHandler.END)
        text = q2.edit_message_text.call_args.args[0]
        self.assertIn("Jangan menyetor crypto", text)
        self.assertNotIn("sell_order_id", ctx2.user_data)

    def test_convert_jaringan_asal_dan_tujuan_diblokir(self):
        from bot.handlers import swap
        cm.set_flag("NETWORK", "TON", True)
        q = _query("swap_src_net_TON")
        ctx = SimpleNamespace(user_data={"swap_src_symbol": "USDT"})
        with patch("services.tx_verifier.deposit_verifiable", return_value=True):
            res = _run(swap.select_src_net(SimpleNamespace(callback_query=q), ctx))
        self.assertEqual(res, swap.SELECT_SRC_NET)
        self.assertNotIn("swap_src_network", ctx.user_data)

        q2 = _query("swap_tgt_net_TON")
        ctx2 = SimpleNamespace(user_data={"swap_tgt_symbol": "USDT"})
        res2 = _run(swap.select_tgt_net(SimpleNamespace(callback_query=q2), ctx2))
        self.assertEqual(res2, swap.SELECT_TGT_NET)
        self.assertNotIn("swap_tgt_network", ctx2.user_data)

    def test_label_maintenance_di_keyboard_beli(self):
        from bot.keyboards.crypto_select import get_buy_network_keyboard, get_buy_symbol_keyboard
        cm.set_flag("NETWORK", "POLYGON", True)
        cm.set_flag("COIN", "SOL", True)
        nets = [b for r in get_buy_network_keyboard("USDT").inline_keyboard for b in r]
        poly = next(b for b in nets if b.callback_data == "buy_net_USDT_POLYGON")
        bsc = next(b for b in nets if b.callback_data == "buy_net_USDT_BSC")
        self.assertIn("[Maintenance]", poly.text)
        self.assertIsNone(poly.icon_custom_emoji_id)
        self.assertNotIn("[Maintenance]", bsc.text)
        syms = [b for r in get_buy_symbol_keyboard().inline_keyboard for b in r]
        self.assertIn("[Maintenance]", next(b for b in syms if b.callback_data == "buy_sym_SOL").text)


class TestAdminMaintenance(DbCase):
    def setUp(self):
        super().setUp()
        from config.settings import settings
        patcher = patch.object(settings, "ADMIN_CHAT_IDS", [999])  # tidak bergantung urutan impor modul tes
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cb(self, data, admin=True):
        from bot.handlers.admin import admin_panel_callback
        q = _query(data, user_id=999 if admin else 111)
        ctx = SimpleNamespace(user_data={}, bot=AsyncMock())

        async def run():
            with patch("bot.handlers.admin.SessionLocal", side_effect=lambda: SessionLocal()):
                await admin_panel_callback(SimpleNamespace(callback_query=q), ctx)
        _run(run())
        return q

    def test_dashboard_punya_tombol_maintenance(self):
        from bot.handlers.admin import get_admin_dashboard_keyboard
        data = {b.callback_data for r in get_admin_dashboard_keyboard(0).inline_keyboard for b in r}
        self.assertIn("admin_panel_maint", data)

    def test_toggle_lewat_tombol_panel(self):
        q = self._cb("admin_panel_maint")
        self.assertIn("Semua jaringan &amp; koin normal", q.edit_message_text.call_args.kwargs["text"])
        q = self._cb("admin_panel_mt_n_SOLANA")
        self.assertIsNotNone(cm.blocked_reason(network="SOLANA"))
        self.assertIn("SOLANA", q.edit_message_text.call_args.kwargs["text"])
        self._cb("admin_panel_mt_n_SOLANA")
        self.assertIsNone(cm.blocked_reason(network="SOLANA"))
        self._cb("admin_panel_mt_c_USDT")
        self.assertIsNotNone(cm.blocked_reason(symbol="USDT"))

    def test_non_admin_dan_kode_ngawur_ditolak(self):
        q = self._cb("admin_panel_mt_n_SOLANA", admin=False)
        self.assertIsNone(cm.blocked_reason(network="SOLANA"))
        q = self._cb("admin_panel_mt_n_NGAWUR")
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))

    def test_perintah_maintenance(self):
        from bot.handlers.admin_maintenance import maintenance_command_handler

        def cmd(*args, uid=999):
            msg = SimpleNamespace(reply_text=AsyncMock())
            upd = SimpleNamespace(effective_user=SimpleNamespace(id=uid), message=msg)
            _run(maintenance_command_handler(upd, SimpleNamespace(args=list(args))))
            return msg.reply_text

        cmd("on", "network", "solana", "RPC", "padat")
        self.assertEqual(cm.blocked_reason(network="SOLANA"), ("Jaringan SOLANA", "RPC padat"))
        cmd("off", "NETWORK", "SOLANA")
        self.assertIsNone(cm.blocked_reason(network="SOLANA"))
        self.assertIn("tidak dikenal", cmd("on", "NETWORK", "NGAWUR").call_args.args[0])
        self.assertIn("Format", cmd("on", "NETWORK").call_args.args[0])
        cmd("on", "COIN", "USDT", uid=111)  # bukan admin: diabaikan
        self.assertIsNone(cm.blocked_reason(symbol="USDT"))


class TestOrderReport(DbCase):
    def _add(self, *orders):
        db = SessionLocal()
        db.add_all(orders)
        db.commit()
        db.close()

    def _call(self, fn, data, uid=111):
        q = _query(data, user_id=uid)
        _run(fn(SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=uid)), SimpleNamespace(user_data={})))
        return q

    def test_tombol_laporan_di_riwayat_hanya_bila_ada_order(self):
        from bot.handlers.history import show_history

        def hist(uid):
            q = _query("menu_history", user_id=uid)
            q.message.reply_chat_action = AsyncMock()
            upd = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=uid))
            _run(show_history(upd, SimpleNamespace(user_data={})))
            return {b.callback_data for r in q.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard for b in r}

        self.assertNotIn("report_issue", hist(222))
        self._add(_order("ORD-A"))
        self.assertIn("report_issue", hist(111))

    def test_daftar_order_hanya_milik_sendiri(self):
        from bot.handlers.order_report import report_issue_menu
        self._add(_order("ORD-MINE", uid=111), _order("ORD-OTHER", uid=222))
        q = self._call(report_issue_menu, "report_issue")
        data = {b.callback_data for r in q.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard for b in r}
        self.assertIn("report_order_ORD-MINE", data)
        self.assertNotIn("report_order_ORD-OTHER", data)

    def test_template_memuat_semua_field_yang_diminta(self):
        from bot.handlers.order_report import report_order_detail
        self._add(_order("ORD-20261008-001"))
        q = self._call(report_order_detail, "report_order_ORD-20261008-001")
        kw = q.edit_message_text.call_args.kwargs
        text = kw["text"]
        for needle in ("Order ID: ORD-20261008-001", "Jenis Transaksi: Beli", "Pembayaran: QRIS (GoPay)",
                       "Waktu Order:", "WIB", "TX Hash: 0x" + "ab" * 32, "Kendala:"):
            self.assertIn(needle, text)
        buttons = [b for r in kw["reply_markup"].inline_keyboard for b in r]
        url = next(b.url for b in buttons if b.url)
        self.assertTrue(url.startswith("https://t.me/"))
        self.assertIn("?text=", url)
        self.assertIn("ORD-20261008-001", url)

    def test_template_order_jual_belum_ada_hash_dan_convert(self):
        from bot.handlers.order_report import build_report_template
        sell = _order("ORD-S", order_type="sell", payment_method=None, payout_tx_hash=None, completed_at=None,
                      status="pending", deposit_tx_hash=None)
        t = build_report_template(sell)
        self.assertIn("Jenis Transaksi: Jual", t)
        self.assertIn("Pembayaran: Kirim koin ke wallet bot", t)
        self.assertIn("TX Hash: -", t)
        self.assertNotIn("Waktu Selesai", t)
        swap = _order("SWAP-1", order_type="swap", target_crypto_symbol="SOL", target_network="SOLANA",
                      target_crypto_amount=Decimal("0.5"))
        label = build_report_template(swap)
        self.assertIn("-> 0.500000 SOL (SOLANA)", label)
        self.assertNotIn("USDT USDT", label)

    def test_order_milik_orang_lain_ditolak(self):
        from bot.handlers.order_report import report_order_detail
        self._add(_order("ORD-OTHER", uid=222))
        q = self._call(report_order_detail, "report_order_ORD-OTHER", uid=111)
        q.edit_message_text.assert_not_awaited()
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))

    def test_html_di_template_di_escape(self):
        from bot.handlers.order_report import report_order_detail
        self._add(_order("ORD-X", payout_tx_hash="<b>bad</b>"))
        q = self._call(report_order_detail, "report_order_ORD-X")
        text = q.edit_message_text.call_args.kwargs["text"]
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;", text)
        self.assertNotIn("<b>bad</b>", text)

    def test_callback_terdaftar_sebelum_catch_all(self):
        import re
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        report_pos = src.index("report_issue_menu, pattern")
        catch_all = src.index("CallbackQueryHandler(menu_callback_handler)")
        self.assertLess(report_pos, catch_all)
        pattern = re.search(r'report_order_detail, pattern=r"([^"]+)"', src).group(1)
        self.assertTrue(re.match(pattern, "report_order_SWAP-20261008123456-007"))
        self.assertFalse(re.match(pattern, "report_order_"))


if __name__ == "__main__":
    unittest.main()
