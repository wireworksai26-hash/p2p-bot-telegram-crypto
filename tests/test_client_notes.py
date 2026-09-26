"""Pagar regresi 3 catatan klien 26 Sep 2026:

1. Bridge - convert koin sama beda jaringan (koin multi-jaringan muncul di
   daftar target dengan label (Bridge), jaringan asal dikecualikan).
2. Logo asli di semua menu - setiap koin & jaringan wajib punya custom
   emoji ID (bukan bentuk Unicode polos yang dikeluhkan klien).
3. Tombol Salin alamat - pesan deposit jual & convert wajib punya tombol
   copy_text + alamat dibungkus <code> (biru, tap-to-copy).
"""
import os
import sys
import unittest
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
    "ADMIN_CHAT_IDS": "123456",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from database.connection import Base, engine, SessionLocal
from database.models import User
from bot.handlers.swap import (
    NETWORKS_BY_SYMBOL,
    SUPPORTED_ASSETS,
    WAITING_DEPOSIT_HASH,
    confirm_swap_order,
    select_src_net,
    select_tgt_symbol,
)
from bot.handlers.sell import handle_order_confirmation, get_hot_wallet_address
from bot.keyboards.crypto_select import BUY_NETWORKS_BY_SYMBOL
from bot.utils.emojis import (
    get_coin_emoji_id,
    get_network_emoji_id,
    network_button_text,
)

HOT_WALLET = "0x" + "1" * 40
SEMUA_JARINGAN = sorted({
    net
    for peta in (NETWORKS_BY_SYMBOL, BUY_NETWORKS_BY_SYMBOL)
    for daftar in peta.values()
    for net in daftar
})


def _tombol(markup):
    return [btn for baris in markup.inline_keyboard for btn in baris]


class TestBridge(unittest.IsolatedAsyncioTestCase):
    """Fitur bridge: koin sama boleh jadi target, asal ≠ tujuan."""

    async def _pilih_src(self, src_sym, src_net):
        query = AsyncMock()
        query.data = f"swap_src_net_{src_net}"
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(
            user_data={"swap_src_symbol": src_sym}, bot=AsyncMock()
        )
        await select_src_net(update, context)
        return _tombol(query.edit_message_text.call_args.kwargs["reply_markup"])

    async def _pilih_tgt(self, src_sym, src_net, tgt_sym):
        query = AsyncMock()
        query.data = f"swap_tgt_sym_{tgt_sym}"
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(
            user_data={
                "swap_src_symbol": src_sym,
                "swap_src_network": src_net,
            },
            bot=AsyncMock(),
        )
        await select_tgt_symbol(update, context)
        return _tombol(query.edit_message_text.call_args.kwargs["reply_markup"])

    async def test_koin_multi_jaringan_ditawarkan_dengan_label_bridge(self):
        tombol = await self._pilih_src("USDT", "BSC")
        bridge = [b for b in tombol if b.callback_data == "swap_tgt_sym_USDT"]
        self.assertEqual(len(bridge), 1, "koin sumber wajib ikut di daftar target")
        self.assertIn("(Bridge)", bridge[0].text)
        lain = [b for b in tombol if b.callback_data == "swap_tgt_sym_SOL"]
        self.assertEqual(len(lain), 1)
        self.assertNotIn("(Bridge)", lain[0].text, "koin beda tidak boleh berlabel bridge")

    async def test_koin_satu_jaringan_tidak_ditawarkan_sebagai_bridge(self):
        for src_sym, src_net in (("SOL", "SOLANA"), ("MATIC", "POLYGON"), ("TRX", "TRON")):
            tombol = await self._pilih_src(src_sym, src_net)
            data = [b.callback_data for b in tombol]
            self.assertNotIn(
                f"swap_tgt_sym_{src_sym}", data,
                f"{src_sym} cuma punya 1 jaringan, bridge tidak mungkin",
            )

    async def test_bridge_koin_sama_jaringan_asal_dikecualikan(self):
        tombol = await self._pilih_tgt("USDT", "BSC", "USDT")
        data = [b.callback_data for b in tombol]
        self.assertNotIn("swap_tgt_net_BSC", data, "jaringan asal tidak boleh jadi tujuan")
        for net in NETWORKS_BY_SYMBOL["USDT"]:
            if net != "BSC":
                self.assertIn(f"swap_tgt_net_{net}", data, net)

    async def test_koin_bedas_jaringan_asal_tetap_ditawarkan(self):
        # USDC punya BSC juga, tapi karena beda simbol BSC tetap boleh dipilih
        tombol = await self._pilih_tgt("USDT", "BSC", "USDC")
        data = [b.callback_data for b in tombol]
        for net in NETWORKS_BY_SYMBOL["USDC"]:
            self.assertIn(f"swap_tgt_net_{net}", data, net)

    async def test_bridge_usdc_eth_juga_bisa(self):
        for sym in ("USDC", "ETH"):
            tombol = await self._pilih_src(sym, "BSC" if sym == "USDC" else "BASE")
            bridge = [b for b in tombol if b.callback_data == f"swap_tgt_sym_{sym}"]
            self.assertEqual(len(bridge), 1, sym)
            self.assertIn("(Bridge)", bridge[0].text, sym)


class TestLogoEmoji(unittest.TestCase):
    """Klien komplain tombol tampil bentuk polos, bukan logo asli."""

    def test_semua_koin_punya_custom_emoji_id(self):
        for simbol in sorted(set(SUPPORTED_ASSETS) | set(BUY_NETWORKS_BY_SYMBOL)):
            self.assertTrue(
                get_coin_emoji_id(simbol),
                f"koin {simbol} tidak punya custom emoji ID (tampil shape polos)",
            )

    def test_semua_jaringan_punya_custom_emoji_id(self):
        for net in SEMUA_JARINGAN:
            self.assertTrue(
                get_network_emoji_id(net),
                f"jaringan {net} tidak punya custom emoji ID (tampil shape polos)",
            )

    def test_tombol_jaringan_tanpa_prefiks_unicode(self):
        # network_button_text hanya menambah ? shape kalau ID tidak ada
        for net in SEMUA_JARINGAN:
            self.assertEqual(network_button_text(net), net, net)

    def test_id_logo_baru_terpasang(self):
        # Stiker diunggah 2026-09-26 ke pak hsncoinlogos_by_Hsnpro_bot
        self.assertEqual(get_network_emoji_id("BASE"), "6176933879622934017")
        self.assertEqual(get_network_emoji_id("OPTIMISM"), "6177169656147619617")
        self.assertEqual(get_network_emoji_id("ROBINHOOD"), "6177109603914883733")
        self.assertEqual(get_coin_emoji_id("MATIC"), "6179091310415192762")


class TestTombolSalin(unittest.IsolatedAsyncioTestCase):
    """Alamat deposit wajib bisa disalin sekali klik."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=999, username="tester", full_name="Tester"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def test_order_convert_punya_tombol_salin_alamat(self):
        query = AsyncMock()
        query.data = "swap_confirm"
        query.from_user = SimpleNamespace(id=999, username="tester", full_name="Tester")
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        alamat = "0x" + "2" * 40
        context = SimpleNamespace(
            user_data={
                "swap_src_symbol": "USDT",
                "swap_src_network": "BSC",
                "swap_src_amount": 10.0,
                "swap_tgt_symbol": "USDT",
                "swap_tgt_network": "POLYGON",
                "swap_tgt_amount": 9.9,
                "swap_nominal_idr": 160000,
                "swap_fee_idr": 6000,
                "swap_target_addr": "0x" + "3" * 40,
                "swap_seller_deposit_wallet": alamat,
            },
            bot=AsyncMock(),
        )

        with patch("services.wallet_sync.sync_wallet_balances", new=AsyncMock()), \
             patch("database.crud.get_available_inventory", return_value=Decimal("999")):
            hasil = await confirm_swap_order(update, context)

        self.assertEqual(hasil, WAITING_DEPOSIT_HASH)
        panggilan = query.edit_message_text.call_args
        kwargs = panggilan.kwargs
        salin = [b for b in _tombol(kwargs["reply_markup"]) if b.copy_text]
        self.assertEqual(len(salin), 1, "pesan deposit convert wajib punya tombol salin")
        self.assertEqual(salin[0].copy_text.text, alamat)
        teks = panggilan.args[0] if panggilan.args else kwargs.get("text", "")
        self.assertIn(f"<code>{alamat}</code>", teks, "alamat harus biru <code>")

    async def test_order_jual_punya_tombol_salin_alamat(self):
        query = AsyncMock()
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=999, first_name="Tester", name="Tester"),
        )
        context = SimpleNamespace(
            user_data={
                "sell_order_id": "TEST-SALIN-001",
                "sell_symbol": "USDT",
                "sell_network": "BSC",
                "sell_crypto_amount": 10.0,
                "sell_price_per_unit": 16000,
                "sell_gross_nominal_idr": 160000,
                "sell_fee_idr": 6000,
                "sell_net_idr": 154000,
                "sell_bank_name": "Bank Uji",
                "sell_bank_acc": "9990001",
                "sell_bank_holder": "Tester",
            },
            bot=AsyncMock(),
        )
        hot = get_hot_wallet_address("BSC")
        self.assertEqual(hot, HOT_WALLET)

        with patch("bot.handlers.sell.notify_admins", new=AsyncMock()):
            await handle_order_confirmation(update, context)

        # QR berhasil dibuat -> pesan terkirim sebagai caption foto
        kirim = context.bot.send_photo.call_args
        self.assertIsNotNone(kirim, "pesan deposit jual harus terkirim")
        kwargs = kirim.kwargs
        salin = [b for b in _tombol(kwargs["reply_markup"]) if b.copy_text]
        self.assertEqual(len(salin), 1, "pesan deposit jual wajib punya tombol salin")
        self.assertEqual(salin[0].copy_text.text, hot)
        self.assertIn(f"<code>{hot}</code>", kwargs["caption"], "alamat harus biru <code>")


if __name__ == "__main__":
    unittest.main()
