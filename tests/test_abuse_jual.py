"""Uji abuse/crossvalidation alur JUAL — hasil audit keamanan 26 Sep 2026.

Skenario penyerangan yang diuji:
1. Replay tx hash lintas order lewat jalur admin (_reverify_sell_deposit tidak
   memakai DepositClaim/_is_hash_used) — hash milik order A bisa mengklaim order B.
2. _is_hash_used membandingkan string mentah → varian format (0x/case) lolos.
3. Over-payment diterima jalur manual (>=) tapi exact di auto-scan → transfer
   besar milik order lain bisa diklaim order kecil.
4. HTML injection lewat TX hash ke pesan keputusan admin (tidak di-escape).
5. Hash sampah (format invalid) tetap diklasifikasi "menunggu" + alert admin.
6. User bisa cancel order yang depositnya SUDAH terkonfirmasi.
7. Pembayaran sesuai angka yang ditampilkan formatter = underpay permanen.

Konvensi: XFAIL = bukti RED dari bug yang belum diperbaiki (jangan dihapus
markernya sebelum implementasi diperbaiki menjadi XPASS).
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
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
from database.models import DepositClaim, Order, User
from services.tx_verifier import _amount_matches, automatic_amount_matches, normalize_tx_hash
from services.detector import DepositDetector, deposit_detector
from bot.utils.formatter import format_crypto
from bot.handlers.sell import cancel_sell, handle_tx_hash_input

HASH_A = "0x" + "ab" * 32


def _order(order_id="ORD-JUAL-1", status="WAITING_CRYPTO_DEPOSIT", telegram_id=555, deposit_hash=None):
    return Order(
        order_id=order_id,
        telegram_id=telegram_id,
        order_type="sell",
        crypto_symbol="USDT",
        network="BSC",
        crypto_amount=Decimal("10"),
        price_per_unit=16000,
        nominal_idr=160000,
        fee_idr=6000,
        total_idr=154000,
        buyer_wallet="Bank Uji | 123456 | Pemilik",
        deposit_wallet="0x" + "1" * 40,
        deposit_tx_hash=deposit_hash,
        status=status,
    )


class TestAmountMatches(unittest.TestCase):
    """Predikat nominal deposit: >= (manual) vs exact (auto-scan)."""

    def test_underpay_ditolak_overpay_diterima(self):
        self.assertTrue(_amount_matches(1.5, 1.0))
        self.assertFalse(_amount_matches(0.99, 1.0))
        self.assertFalse(_amount_matches(1.0, 0))
        self.assertFalse(_amount_matches(float("inf"), 1.0))

    def test_auto_scan_harus_exact(self):
        self.assertTrue(automatic_amount_matches(1.0, 1.0))
        self.assertFalse(automatic_amount_matches(1.01, 1.0))
        self.assertFalse(automatic_amount_matches(0.99, 1.0))

    @unittest.expectedFailure
    def test_overpay_besar_tidak_klaim_order_kecil(self):
        """Transfer jauh lebih besar (milik order/orang lain) menutup order kecil
        di jalur manual (admin/hash). Crossvalidation harus tolak overpay ekstrem."""
        self.assertFalse(
            _amount_matches(Decimal("99"), Decimal("1")),
            "overpay 99x tidak boleh otomatis dianggap deposit order kecil ini",
        )


class TestNormalizeHash(unittest.TestCase):
    def test_evm_normalisasi(self):
        self.assertEqual(normalize_tx_hash("BSC", "0x" + "AB" * 32), "0x" + "ab" * 32)
        self.assertEqual(normalize_tx_hash("bsc", "ab" * 32), "0x" + "ab" * 32)

    def test_url_diambil_hash_nya_tanpa_fetch(self):
        url = f"https://bscscan.com/tx/0x{'cd' * 32}"
        self.assertEqual(normalize_tx_hash("BSC", url), "0x" + "cd" * 32)

    def test_hash_sampah_ditolak(self):
        for net, bad in (("BSC", "0x123"), ("BSC", ""), ("BSC", "zz" * 32), ("SOLANA", "abc")):
            with self.subTest(net=net, bad=bad):
                with self.assertRaises(ValueError):
                    normalize_tx_hash(net, bad)


class TestIsHashUsed(unittest.TestCase):
    """Guard anti-reuse hash di jalur detektor."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_hash_tanpa_nilai_dianggap_terpakai(self):
        self.assertTrue(DepositDetector._is_hash_used(self.db, "", exclude_order="ORD-B"))

    def test_hash_diklaim_order_lain_terdeteksi(self):
        self.db.add(DepositClaim(network="BSC", tx_hash=HASH_A, order_id="ORD-A"))
        self.db.commit()
        self.assertTrue(DepositDetector._is_hash_used(self.db, HASH_A, exclude_order="ORD-B"))
        self.assertFalse(DepositDetector._is_hash_used(self.db, HASH_A, exclude_order="ORD-A"))

    def test_order_selesai_dengan_hash_sama_terdeteksi(self):
        self.db.add(_order(order_id="ORD-A", status="CRYPTO_CONFIRMED", deposit_hash=HASH_A))
        self.db.commit()
        self.assertTrue(DepositDetector._is_hash_used(self.db, HASH_A, exclude_order="ORD-B"))

    @unittest.expectedFailure
    def test_hash_sama_beda_format_terdeteksi(self):
        """Order A menyimpan '0xAB..' (case/prefix apa pun) — hash yang sama harus
        terdeteksi sudah dipakai, bukan hanya cocok string mentah."""
        self.db.add(_order(order_id="ORD-A", status="CRYPTO_CONFIRMED",
                           deposit_hash="0x" + "AB" * 32))
        self.db.commit()
        self.assertTrue(
            DepositDetector._is_hash_used(self.db, "ab" * 32, exclude_order="ORD-B"),
            "hash sama tanpa prefix 0x/lowercase harus terdeteksi sudah dipakai",
        )


class TestAdminReverifyReplay(unittest.IsolatedAsyncioTestCase):
    """Jalur admin (Sudah Ditransfer / verifysell) tidak boleh melewatkan cek klaim hash."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    @unittest.expectedFailure
    async def test_reverify_tolak_hash_milik_order_lain(self):
        """Serangan: order B (penyerang) setor hash milik deposit order A yang sah.
        verify_deposit lolos on-chain (wallet/amount/waktu sama), lalu admin klik
        Sudah Ditransfer → Rupiah lari ke penyerang. Harus ada cek DepositClaim."""
        from bot.handlers.admin import _reverify_sell_deposit

        self.db.add(DepositClaim(network="BSC", tx_hash=HASH_A, order_id="ORD-A"))
        order_b = _order(order_id="ORD-B", deposit_hash=HASH_A)
        self.db.add(order_b)
        self.db.commit()

        with patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value={
            "verified": True, "amount": 10.0, "timestamp": 0, "tx_hash": HASH_A, "reason": "OK",
        })):
            hasil = await _reverify_sell_deposit(self.db, order_b)

        self.assertFalse(
            (hasil or {}).get("verified"),
            "hash yang sudah diklaim order lain tidak boleh lolos reverify jalur admin",
        )


class TestRoundingTampilanVsExpected(unittest.TestCase):
    """User membayar persis angka yang ditampilkan bot — apakah diterima?"""

    @unittest.expectedFailure
    def test_bayar_sesuai_tampilan_tidak_macet(self):
        expected = 10.111111
        tampil = float(format_crypto(expected, "USDT").split()[0])  # "10.1111"
        self.assertTrue(
            _amount_matches(tampil, expected),
            f"bayar persis angka tampilan ({tampil}) tidak boleh underpay dari expected ({expected})",
        )


class TestCancelSellStateGuard(unittest.IsolatedAsyncioTestCase):
    @unittest.expectedFailure
    async def test_cancel_tidak_boleh_hapus_order_terkonfirmasi(self):
        """Setelah deposit terkonfirmasi, user menekan Batal Jual → order jadi
        cancelled. Tidak boleh: pencairan/admin bisa kehilangan jejak."""
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            db.add(User(telegram_id=555, username="u", full_name="U"))
            db.add(_order(order_id="ORD-CANCEL-1", status="CRYPTO_CONFIRMED"))
            db.commit()
        finally:
            db.close()

        query = AsyncMock()
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=555))
        context = SimpleNamespace(user_data={"sell_order_id": "ORD-CANCEL-1"})
        with patch("bot.handlers.sell.SessionLocal", side_effect=lambda: SessionLocal()), \
             patch("bot.handlers.start.send_main_menu", new=AsyncMock()):
            await cancel_sell(update, context)

        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.order_id == "ORD-CANCEL-1").first()
            self.assertNotEqual(
                order.status, "cancelled",
                "order yang depositnya sudah terkonfirmasi tidak boleh dibatalkan oleh user",
            )
        finally:
            db.close()
            Base.metadata.drop_all(bind=engine)


class TestHashInputAbuse(unittest.IsolatedAsyncioTestCase):
    """Handler input TX hash: injection ke pesan admin + klasifikasi hash sampah."""

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.db.add(User(telegram_id=555, username="u", full_name="U"))
        self.db.add(_order(order_id="ORD-HASH-1"))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def _kirim_hash(self, payload, alasan_verifikasi):
        message = AsyncMock()
        message.text = payload
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=555))
        context = SimpleNamespace(
            bot=AsyncMock(),
            application=None,
            user_data={
                "sell_order_id": "ORD-HASH-1",
                "sell_symbol": "USDT",
                "sell_network": "BSC",
                "sell_crypto_amount": 10.0,
                "sell_net_idr": 154000,
                "sell_bank_name": "Bank Uji",
                "sell_bank_acc": "123456",
                "sell_bank_holder": "Pemilik",
                "sell_user_id": 555,
            },
        )
        notify = AsyncMock()
        with patch("bot.handlers.sell.SessionLocal", side_effect=lambda: SessionLocal()), \
             patch("bot.handlers.sell.notify_admins", notify), \
             patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value={
                 "verified": False, "amount": 0.0, "from_address": "", "reason": alasan_verifikasi,
             })), \
             patch.object(deposit_detector, "verifikasi_cepat", new=AsyncMock()):
            await handle_tx_hash_input(update, context)
        return update, notify

    @unittest.expectedFailure
    async def test_injection_tx_hash_tidak_lolos_ke_pesan_admin(self):
        """Bank/hash diketik user masuk ke pesan keputusan admin tanpa escape →
        bisa menyisipkan teks 'SEGERA TRANSFER' palsu."""
        payload = "<b>HACK</b>abcdef123456"
        _, notify = await self._kirim_hash(payload, "Menunggu konfirmasi jaringan")
        teks = notify.call_args.args[1] if notify.call_args.args else ""
        self.assertIn("&lt;b&gt;HACK", teks)
        self.assertNotIn("<b>HACK", teks)

    @unittest.expectedFailure
    async def test_hash_sampah_tidak_dianggap_menunggu(self):
        """Hash format invalid (ValueError) tidak boleh dibalas 'TX Hash Diterima'
        selama scanner belum tentu bisa mengonfirmasinya."""
        update, _ = await self._kirim_hash(
            "gtgtgtgtgtgtgt", "Data blockchain belum dapat diverifikasi (ValueError)"
        )
        teks = update.message.reply_text.call_args.kwargs.get("text", "")
        self.assertNotIn("TX Hash Diterima", teks)


if __name__ == "__main__":
    unittest.main()
