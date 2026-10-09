"""Langkah "Alamat Wallet Pengirim" di Jual & Convert + deposit hanya sah dari alamat itu."""
import os
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from tests._settings_guard import e2e_setup, e2e_teardown  # noqa: E402
from tests import test_e2e_bot_flows as _e2e  # noqa: E402
from config.settings import settings  # noqa: E402
from database.connection import SessionLocal  # noqa: E402
from database.models import Order  # noqa: E402
from services import tx_verifier  # noqa: E402
from services.sender_wallet import check_sender_address  # noqa: E402

USER = 94001
GOOD = "0x" + "a" * 40
HOT = "0x" + "1" * 40


class CheckSenderAddress(unittest.TestCase):
    def test_alamat_evm_valid(self):
        self.assertEqual(check_sender_address("BSC", GOOD), "")

    def test_format_salah_ditolak(self):
        for bad in ("", "abc", "0x123", "bukan alamat", "0x" + "z" * 40):
            with self.subTest(addr=bad):
                self.assertIn("tidak valid", check_sender_address("BSC", bad))

    def test_alamat_bot_sendiri_ditolak(self):
        self.assertIn("milik bot sendiri", check_sender_address("BSC", HOT))
        self.assertIn("milik bot sendiri", check_sender_address("BSC", HOT.upper().replace("0X", "0x")))

    def test_wallet_owner_ditolak(self):
        owner = "0x" + "b" * 40
        with patch.object(settings, "OWNER_WALLET_ADDRESSES", (owner,)):
            self.assertIn("tidak bisa dipakai", check_sender_address("BSC", owner))

    def test_terlalu_panjang_ditolak(self):
        self.assertIn("tidak valid", check_sender_address("BSC", "0x" + "a" * 300))

    def test_format_tidak_bergantung_pada_koneksi_rpc(self):
        # Validasi format memakai validator statis; sender yang rusak tidak boleh menolak alamat valid.
        with patch("services.crypto_sender.CryptoSenderFactory.get_sender", side_effect=RuntimeError("rpc mati")):
            self.assertEqual(check_sender_address("BSC", GOOD), "")


class SenderStepE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await e2e_setup(self)

    async def asyncTearDown(self):
        await e2e_teardown(self)

    _user = _e2e.BotFlowE2E._user
    _dispatch = _e2e.BotFlowE2E._dispatch
    say = _e2e.BotFlowE2E.say
    tap = _e2e.BotFlowE2E.tap

    async def _sell_to_sender_step(self):
        await self.say(USER, "/start")
        await self.tap(USER, "menu_sell")
        await self.tap(USER, "sell_sym_USDT")
        await self.tap(USER, "sell_net_USDT_BSC")
        return await self.say(USER, "10")

    async def test_jual_meminta_wallet_pengirim_sebelum_rekening(self):
        shown = await self._sell_to_sender_step()
        self.assertIn("Alamat Wallet Pengirim", shown)
        self.assertIn("Cwallet", shown)  # catatan jangan kirim dari exchange
        self.assertNotIn("Rekening Bank / E-Wallet Penerima", shown)

    async def test_jual_alamat_salah_diminta_ulang_dan_tidak_lanjut(self):
        await self._sell_to_sender_step()
        shown = await self.say(USER, "bukan-alamat")
        self.assertIn("tidak valid", shown)
        self.assertIn("Alamat Wallet Pengirim", shown)
        shown = await self.say(USER, HOT)  # alamat hot wallet bot sendiri
        self.assertIn("milik bot sendiri", shown)

    async def test_jual_alamat_valid_lanjut_ke_rekening_lalu_tersimpan_di_order(self):
        await self._sell_to_sender_step()
        shown = await self.say(USER, GOOD)
        self.assertIn("Rekening Bank / E-Wallet Penerima", shown)
        summary = await self.say(USER, "BCA, 1234567890, Budi Santoso")
        self.assertIn("Wallet Pengirim Koin Anda", summary)
        self.assertIn(GOOD, summary)
        await self.tap(USER, "sell_confirm", from_screen=False)
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.telegram_id == USER, Order.order_type == "sell").one()
            self.assertEqual(order.sender_wallet, GOOD)
        finally:
            db.close()

    async def test_convert_meminta_wallet_pengirim_setelah_wallet_tujuan(self):
        await self.say(USER, "/start")
        await self.tap(USER, "start_swap", from_screen=False)
        await self.tap(USER, "swap_src_sym_USDT", from_screen=False)
        await self.tap(USER, "swap_src_net_BSC", from_screen=False)
        await self.tap(USER, "swap_tgt_sym_ETH", from_screen=False)
        await self.tap(USER, "swap_tgt_net_BASE", from_screen=False)
        await self.say(USER, "20")
        shown = await self.say(USER, "0x" + "c" * 40)
        self.assertIn("Alamat Wallet Pengirim", shown)
        self.assertIn("Cwallet", shown)
        self.assertIn("tidak valid", await self.say(USER, "salah"))
        summary = await self.say(USER, GOOD)
        self.assertIn("RINGKASAN QUOTE CONVERT", summary)
        self.assertIn(GOOD, summary)
        await self.tap(USER, "confirm_swap_order", from_screen=False)
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.telegram_id == USER, Order.order_type == "swap").one()
            self.assertEqual(order.sender_wallet, GOOD)
        finally:
            db.close()


class DepositHarusDariAlamatTerdaftar(unittest.TestCase):
    """verify_deposit menolak setoran yang pengirimnya bukan alamat terdaftar."""

    def test_alamat_cocok_tanpa_peduli_huruf_besar_kecil(self):
        self.assertTrue(tx_verifier.addresses_match("BSC", "0xAbC" + "0" * 37, "0xabc" + "0" * 37))

    def test_alamat_berbeda_tidak_cocok(self):
        self.assertFalse(tx_verifier.addresses_match("BSC", "0x" + "a" * 40, "0x" + "b" * 40))

    def test_alamat_kosong_tidak_cocok(self):
        self.assertFalse(tx_verifier.addresses_match("BSC", "", GOOD))


class DepositDariAlamatLain(unittest.IsolatedAsyncioTestCase):
    async def test_pengirim_berbeda_ditolak_dan_hash_dilepas(self):
        from database.connection import Base, engine
        from services.detector import DepositDetector
        from unittest.mock import AsyncMock
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            order = Order(
                order_id="SELL-SND", telegram_id=USER, order_type="sell", crypto_symbol="USDT", network="BSC",
                crypto_amount=Decimal("10"), price_per_unit=17000, nominal_idr=170000, fee_idr=3000,
                total_idr=167000, buyer_wallet="BCA | 1 | X", deposit_wallet=HOT,
                sender_wallet=GOOD, status="WAITING_CRYPTO_DEPOSIT", created_at=datetime.utcnow(),
                deposit_tx_hash="0x" + "ef" * 32)
            db.add(order)
            db.commit()
            seen = {}

            async def fake_verify(**kw):
                seen.update(kw)
                if kw.get("expected_sender") and not tx_verifier.addresses_match(
                        kw["network"], "0x" + "c" * 40, kw["expected_sender"]):
                    return tx_verifier._fail("Alamat pengirim tidak sesuai. Transaksi dikirim dari x, "
                                             "sedangkan wallet terdaftar adalah y.")
                return tx_verifier._ok(10, datetime.utcnow().timestamp(), kw["tx_hash"], "0x" + "c" * 40)

            with patch("services.tx_verifier.verify_deposit", side_effect=fake_verify), \
                 patch("services.detector.notify_admins", new=AsyncMock()) as notify, \
                 patch.object(DepositDetector, "_confirm_order", new=AsyncMock()) as confirm:
                await DepositDetector()._process_order(db, order, AsyncMock())
            self.assertEqual(seen.get("expected_sender"), GOOD)
            confirm.assert_not_awaited()
            db.refresh(order)
            self.assertIsNone(order.deposit_tx_hash)  # hash orang lain dilepas dari order
            notify.assert_awaited()
        finally:
            db.close()
            Base.metadata.drop_all(bind=engine)


if __name__ == "__main__":
    unittest.main()
