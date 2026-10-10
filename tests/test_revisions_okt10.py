"""Revisi client 10 Okt: Cek Ulang tidak spam, Convert Rp5.000, TRON/TRC20 USDT dihapus, batas 10 menit tetap."""
import os
import re
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402
from telegram.error import BadRequest  # noqa: E402

import database.models  # noqa: F401,E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Order, TopupOrder, User  # noqa: E402
from bot.utils import telegram_utils as tu  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
USER = 96001


def fake_query(data, *, photo=False, text="Tagihan QRIS awal", markup=None, user_id=USER):
    msg = SimpleNamespace(
        caption=text if photo else None, caption_html=text if photo else None,
        text=None if photo else text, text_html=None if photo else text,
        photo=[object()] if photo else None, reply_markup=markup, reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        data=data, message=msg, from_user=SimpleNamespace(id=user_id), answer=AsyncMock(),
        edit_message_caption=AsyncMock(), edit_message_text=AsyncMock(),
    )


def recheck_markup(prefix):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Saya Sudah Transfer", callback_data=f"{prefix}ORD-1")],
        [InlineKeyboardButton("Menu Utama", callback_data="menu_back")],
    ])


# ───────────── 3. Cek Ulang: perbarui pesan yang sama ─────────────
class RefreshHelper(unittest.IsolatedAsyncioTestCase):
    async def test_teks_biasa_diedit_bukan_pesan_baru(self):
        q = fake_query("x", text="Isi pesan")
        ok = await tu.refresh_message_status(q, "⏳ belum terdeteksi")
        self.assertTrue(ok)
        q.message.reply_text.assert_not_awaited()
        q.edit_message_text.assert_awaited_once()
        sent = q.edit_message_text.await_args.kwargs["text"]
        self.assertTrue(sent.startswith("Isi pesan"))
        self.assertIn(tu.STATUS_DIVIDER, sent)
        self.assertTrue(sent.endswith("⏳ belum terdeteksi"))

    async def test_caption_foto_memakai_edit_caption(self):
        q = fake_query("x", photo=True, text="Caption QRIS")
        self.assertTrue(await tu.refresh_message_status(q, "status"))
        q.edit_message_caption.assert_awaited_once()
        q.edit_message_text.assert_not_awaited()
        q.message.reply_text.assert_not_awaited()

    async def test_klik_berulang_menimpa_status_tidak_menumpuk(self):
        q = fake_query("x", text="Isi pesan")
        await tu.refresh_message_status(q, "status pertama")
        first = q.edit_message_text.await_args.kwargs["text"]
        # Klik kedua: pesan sekarang sudah berisi blok status pertama.
        q2 = fake_query("x", text=first)
        await tu.refresh_message_status(q2, "status kedua")
        second = q2.edit_message_text.await_args.kwargs["text"]
        self.assertEqual(second.count(tu.STATUS_DIVIDER), 1)
        self.assertIn("status kedua", second)
        self.assertNotIn("status pertama", second)
        self.assertTrue(second.startswith("Isi pesan"))

    async def test_melewati_batas_caption_tidak_diedit_dan_return_false(self):
        q = fake_query("x", photo=True, text="x" * 1000)
        self.assertFalse(await tu.refresh_message_status(q, "status " * 20))
        q.edit_message_caption.assert_not_awaited()

    async def test_not_modified_dianggap_berhasil(self):
        q = fake_query("x")
        q.edit_message_text.side_effect = BadRequest("Message is not modified: ...")
        self.assertTrue(await tu.refresh_message_status(q, "status"))

    async def test_error_lain_return_false_tanpa_melempar(self):
        q = fake_query("x")
        q.edit_message_text.side_effect = BadRequest("Message to edit not found")
        self.assertFalse(await tu.refresh_message_status(q, "status"))
        q.edit_message_text.side_effect = RuntimeError("jaringan putus")
        self.assertFalse(await tu.refresh_message_status(q, "status"))

    def test_relabel_hanya_tombol_cek(self):
        markup = tu.relabel_recheck_button(recheck_markup("check_buy_payment_"), "check_buy_payment_")
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertEqual(labels, ["Cek Ulang", "Menu Utama"])
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "check_buy_payment_ORD-1")
        self.assertIsNone(tu.relabel_recheck_button(None, "x"))

    def test_jam_wib_format(self):
        self.assertRegex(tu.wib_clock(), r"^\d{2}:\d{2}:\d{2}$")


class RecheckHandlers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=USER, balance_idr=Decimal("0")))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def test_beli_belum_dibayar_edit_pesan_qris_yang_sama(self):
        from bot.handlers.buy import check_buy_payment
        self.db.add(Order(order_id="ORD-1", telegram_id=USER, order_type="buy", crypto_symbol="USDT",
                          network="BSC", crypto_amount=Decimal("1"), price_per_unit=17000, nominal_idr=17000,
                          fee_idr=3000, total_idr=20299, buyer_wallet="0x" + "c" * 40,
                          payment_method="GOPAY_QRIS", status="pending", created_at=datetime.utcnow()))
        self.db.commit()
        q = fake_query("check_buy_payment_ORD-1", photo=True, text="QRIS caption",
                       markup=recheck_markup("check_buy_payment_"))
        update = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=USER))
        with patch("bot.handlers.buy.gopay_service.confirm_payment", new=AsyncMock(return_value=False)):
            for _ in range(3):  # klik berkali-kali
                await check_buy_payment(update, SimpleNamespace(bot=AsyncMock()))
        q.message.reply_text.assert_not_awaited()          # tidak ada pesan baru sama sekali
        self.assertEqual(q.edit_message_caption.await_count, 3)  # pesan yang sama diperbarui
        caption = q.edit_message_caption.await_args.kwargs["caption"]
        self.assertIn("belum terdeteksi", caption)
        self.assertIn("WIB", caption)
        labels = [b.text for row in q.edit_message_caption.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(labels[0], "Cek Ulang")
        q.answer.assert_awaited()  # tombol selalu dijawab (tidak menggantung)

    async def test_beli_gagal_edit_memakai_popup_bukan_pesan_baru(self):
        from bot.handlers.buy import check_buy_payment
        self.db.add(Order(order_id="ORD-1", telegram_id=USER, order_type="buy", crypto_symbol="USDT",
                          network="BSC", crypto_amount=Decimal("1"), price_per_unit=17000, nominal_idr=17000,
                          fee_idr=3000, total_idr=20299, buyer_wallet="0x" + "c" * 40,
                          payment_method="GOPAY_QRIS", status="pending", created_at=datetime.utcnow()))
        self.db.commit()
        q = fake_query("check_buy_payment_ORD-1", photo=True, text="QRIS caption",
                       markup=recheck_markup("check_buy_payment_"))
        q.edit_message_caption.side_effect = BadRequest("Message can't be edited")
        update = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=USER))
        with patch("bot.handlers.buy.gopay_service.confirm_payment", new=AsyncMock(return_value=False)):
            await check_buy_payment(update, SimpleNamespace(bot=AsyncMock()))
        q.message.reply_text.assert_not_awaited()
        self.assertTrue(q.answer.await_args.kwargs.get("show_alert"))

    async def test_topup_belum_dibayar_edit_pesan_yang_sama(self):
        from bot.handlers.balance import check_topup_payment_manual
        self.db.add(TopupOrder(topup_id="TOPUP-1", telegram_id=USER, amount_idr=50_137, mdr_idr=0,
                               status="PENDING", created_at=datetime.utcnow(),
                               expires_at=datetime.utcnow() + timedelta(minutes=10)))
        self.db.commit()
        q = fake_query("check_topup_TOPUP-1", photo=True, text="Tagihan topup",
                       markup=recheck_markup("check_topup_"))
        update = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=USER))
        with patch("bot.handlers.balance.gopay_service.confirm_payment", new=AsyncMock(return_value=False)):
            await check_topup_payment_manual(update, SimpleNamespace(bot=AsyncMock()))
            await check_topup_payment_manual(update, SimpleNamespace(bot=AsyncMock()))
        q.message.reply_text.assert_not_awaited()
        self.assertEqual(q.edit_message_caption.await_count, 2)
        self.assertIn("belum terdeteksi", q.edit_message_caption.await_args.kwargs["caption"])

    async def test_topup_error_tetap_menjawab_tombol(self):
        from bot.handlers.balance import check_topup_payment_manual
        q = fake_query("check_topup_NOPE")
        update = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=USER))
        with patch("bot.handlers.balance.get_topup_order_by_id", side_effect=RuntimeError("db mati")):
            await check_topup_payment_manual(update, SimpleNamespace(bot=AsyncMock()))
        q.answer.assert_awaited()

    async def test_admin_cek_ulang_deposit_tidak_membuat_pesan_baru(self):
        from bot.handlers import admin
        self.db.add(Order(order_id="SWAP-1", telegram_id=USER, order_type="swap", crypto_symbol="USDT",
                          network="BSC", crypto_amount=Decimal("10"), price_per_unit=0, nominal_idr=160000,
                          fee_idr=6000, total_idr=160000, buyer_wallet="0x" + "c" * 40,
                          status="WAITING_CRYPTO_DEPOSIT", created_at=datetime.utcnow()))
        self.db.commit()
        q = fake_query("admin_recheck_swap_SWAP-1", text="Notifikasi deposit", user_id=1)
        update = SimpleNamespace(callback_query=q, effective_user=SimpleNamespace(id=1))
        with patch("bot.handlers.admin.is_admin", return_value=True), \
             patch("services.detector.deposit_detector._process_order", new=AsyncMock()):
            await admin.admin_recheck_swap_callback(update, SimpleNamespace(application=AsyncMock()))
            await admin.admin_recheck_swap_callback(update, SimpleNamespace(application=AsyncMock()))
        q.message.reply_text.assert_not_awaited()
        self.assertEqual(q.edit_message_text.await_count, 2)
        self.assertIn("WAITING_CRYPTO_DEPOSIT", q.edit_message_text.await_args.kwargs["text"])


# ───────────── 4. Convert Rp 5.000 ─────────────
class ConvertMinimumAmount(unittest.TestCase):
    def test_pembulatan_ke_atas_menjaga_nominal(self):
        from decimal import ROUND_UP
        from services.deposit_amount import quantum
        for price in (15_917, 16_000, 16_432, 16_789, 17_915, 18_203):
            exact = Decimal(5000) / Decimal(price)
            coin = exact.quantize(quantum("USDT"), rounding=ROUND_UP)
            self.assertGreaterEqual(int(coin * Decimal(price)), 5000, price)
            self.assertLess(coin - exact, quantum("USDT"))  # tidak lebih dari satu langkah presisi


# ───────────── 2 & 6. TRON (USDT) / TRC20 dihapus ─────────────
class TronUsdtRemoved(unittest.TestCase):
    def test_usdt_tron_tidak_ada_di_daftar_stok_tapi_trx_tetap(self):
        from config.assets import STOCK_ASSETS
        self.assertNotIn(("USDT", "TRON"), STOCK_ASSETS)
        self.assertIn(("TRX", "TRON"), STOCK_ASSETS)  # TRX masih dijual

    def test_usdt_tidak_ditawarkan_di_jaringan_tron(self):
        from bot.keyboards.crypto_select import BUY_NETWORKS_BY_SYMBOL
        from bot.handlers.swap import NETWORKS_BY_SYMBOL
        self.assertNotIn("TRON", BUY_NETWORKS_BY_SYMBOL["USDT"])
        self.assertNotIn("TRON", NETWORKS_BY_SYMBOL["USDT"])

    def test_label_stok_tanpa_trc20(self):
        from bot.handlers import stocks
        for label in list(stocks.NETWORK_LABELS.values()) + list(stocks.CHAIN_SHORT_LABELS.values()):
            self.assertNotIn("TRC20", label.upper())
        self.assertEqual(stocks.NETWORK_LABELS["TRON"], "TRON")

    def test_label_wallet_tersimpan_tanpa_trc20(self):
        from bot.handlers import saved_accounts as sa
        from services import wallet_detector as wd
        labels = list(sa.CHAIN_TITLES.values()) + [v["label"] for v in wd.NETWORK_PATTERNS.values()] \
            + list(wd.NETWORK_DISPLAY.values())
        self.assertTrue(labels)
        for label in labels:
            self.assertNotIn("TRC20", str(label).upper())

    def test_wallet_tron_lama_ditampilkan_sebagai_tron(self):
        from bot.handlers.saved_accounts import _net_label
        self.assertEqual(_net_label("TRC20"), "TRON")
        self.assertEqual(_net_label("trc20"), "TRON")
        self.assertEqual(_net_label("BSC"), "BSC")
        self.assertEqual(_net_label(None), "")

    def test_pemantau_api_tron_hanya_trx(self):
        src = (ROOT / "services" / "coin_api_monitor.py").read_text(encoding="utf-8")
        i = src.index('"id": "NONEVM_TRON"')
        j = src.index('"id":', i + 10)  # sampai entri berikutnya
        block = src[i:j]
        self.assertIn('"symbol": "TRX"', block)
        self.assertNotIn("USDT", block)

    def test_tombol_dan_teks_bot_tidak_menyebut_trc20(self):
        # Teks yang tampil ke user/admin: tidak boleh ada tulisan TRC20 (kunci internal data lama dikecualikan).
        shown = re.compile(r'(?:"|\')[^"\']*(TRC20|TRC-20)[^"\']*(?:"|\')')
        allowed_keys = re.compile(
            r'^\s*"TRC20"\s*:|"TRC20":\s*"TRON"|\("TRON",\s*\["TRX"\]\)|chains.*TRC20|net_map|"TRON":\s*"TRC20"'
            r'|ditampilkan sebagai|upper\(\) == "TRC20"')
        offenders = []
        for path in (ROOT / "bot").rglob("*.py"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if shown.search(line) and not allowed_keys.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:90]}")
        self.assertEqual(offenders, [])


# ───────────── 7. Batas waktu 10 menit (tidak bisa diubah lewat env) ─────────────
class TenMinuteLimit(unittest.TestCase):
    def test_env_lama_30_menit_diabaikan(self):
        code = "from config.settings import settings; print(settings.ORDER_EXPIRE_MINUTES)"
        env = dict(os.environ, ORDER_EXPIRE_MINUTES="30", PYTHON_DOTENV_DISABLED="1")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "10", out.stderr[-300:])

    def test_tidak_ada_teks_30_atau_15_menit_di_bot(self):
        pattern = re.compile(r"\b(30|15)\s*menit", re.IGNORECASE)
        offenders = []
        for folder in ("bot", "services"):
            for path in (ROOT / folder).rglob("*.py"):
                for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern.search(line) and not line.lstrip().startswith("#"):
                        offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:90]}")
        self.assertEqual(offenders, [])

    def test_topup_kas_bot_admin_memakai_batas_yang_sama(self):
        src = (ROOT / "bot" / "handlers" / "admin.py").read_text(encoding="utf-8")
        self.assertNotIn("Batas Waktu:</b> 30 Menit", src)
        self.assertNotIn("timedelta(minutes=30)", src)


if __name__ == "__main__":
    unittest.main()
