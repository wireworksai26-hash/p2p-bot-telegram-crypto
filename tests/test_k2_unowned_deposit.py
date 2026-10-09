"""K2 — deposit yang bukan milik order tidak boleh diklaim order lain.

Hot wallet dipakai bersama dan nominal koin TIDAK lagi berkode unik (kode unik hanya di
QRIS). Kepemilikan deposit dibuktikan lewat TX hash kiriman user yang diverifikasi
on-chain; auto-scan riwayat wallet dimatikan. Serangan yang tetap harus gagal:
A. Owner mengisi ulang stok dengan nominal bulat (mis. tepat 500 USDT). Penyerang
   membuat order bernominal sama lalu menempel hash isi ulang itu ke ordernya.
B. Order korban sudah `expired`, depositnya masuk telat (masih dalam jendela 24 jam).
   Order penyerang bernominal sama tidak boleh mengambil deposit itu.
C. Hash isi ulang ditempel ke order bernominal sedikit di bawahnya.
D. Dua order aktif bernominal sama: tidak ada yang boleh auto-klaim (eskalasi admin).
E. Tanpa hash, deposit yang kebetulan pas nominalnya tidak boleh dikonfirmasi.

Rantai disimulasikan: daftar deposit masuk on-chain, `verify_deposit` memeriksa nominal
>= order seperti aslinya dan mengembalikan alamat pengirim.
"""
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from tests._settings_guard import e2e_setup, e2e_teardown  # noqa: E402
from tests import test_e2e_bot_flows as _e2e  # noqa: E402
import database.models  # noqa: F401,E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import AuditLog, Order  # noqa: E402
from services import tx_verifier  # noqa: E402
from services.detector import DepositDetector  # noqa: E402
from config.settings import settings  # noqa: E402
from services.deposit_amount import assign_deposit_amount  # noqa: E402

HOT = os.environ["EVM_WALLET_ADDRESS"]


def H(tag):
    """Hash EVM valid (64 hex) yang deterministik per label."""
    import hashlib
    return "0x" + hashlib.sha256(tag.encode()).hexdigest()


class FakeChain:
    """Deposit masuk ke hot wallet; meniru penyaringan verifier asli."""

    def __init__(self):
        self.deposits = {}  # hash -> (amount, timestamp)

    def send(self, tx_hash, amount, when=None, sender="0x" + "5" * 40):
        self.deposits[tx_hash] = (Decimal(str(amount)), tx_verifier._timestamp(when or datetime.utcnow()), sender)

    async def verify_deposit(self, network, symbol, tx_hash, expected_wallet, expected_amount,
                             not_before=None, not_after=None, expected_sender=None):
        if tx_hash not in self.deposits:
            return tx_verifier._fail("Transaksi gagal atau belum confirmed.")
        amount, stamp, sender = self.deposits[tx_hash]
        if expected_sender and sender and not tx_verifier.addresses_match(network, sender, expected_sender):
            return tx_verifier._fail(
                f"Alamat pengirim tidak sesuai. Transaksi dikirim dari {sender}, "
                f"sedangkan wallet terdaftar adalah {expected_sender}.")
        if not tx_verifier._amount_matches(amount, expected_amount):
            return tx_verifier._fail(f"Nominal deposit kurang: diterima {amount} {symbol}, "
                                     f"dibutuhkan {Decimal(str(expected_amount))} {symbol}.")
        return tx_verifier._ok(amount, stamp, tx_hash, sender)

    async def get_recent_incoming(self, network, symbol, wallet, min_amount=0.0, limit=20,
                                  not_before=None, not_after=None):
        out = []
        for tx_hash, (amount, stamp, sender) in self.deposits.items():
            if tx_verifier.automatic_amount_matches(amount, min_amount):
                out.append(tx_verifier._ok(amount, stamp, tx_hash, sender))
        return out


def _order(order_id, amount, status="WAITING_CRYPTO_DEPOSIT", order_type="sell",
           created=None, telegram_id=1):
    created = created or datetime.utcnow() - timedelta(minutes=1)
    return Order(
        order_id=order_id, telegram_id=telegram_id, order_type=order_type,
        crypto_symbol="USDT", network="BSC", crypto_amount=Decimal(str(amount)),
        target_crypto_symbol="ETH" if order_type == "swap" else None,
        target_network="BASE" if order_type == "swap" else None,
        target_crypto_amount=Decimal("0.001") if order_type == "swap" else None,
        price_per_unit=17915, nominal_idr=1, fee_idr=0, total_idr=1,
        buyer_wallet="BCA | 1 | X", deposit_wallet=HOT, status=status, created_at=created,
        quote_expires_at=created + timedelta(minutes=30),
    )


class DetectorCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.chain = FakeChain()
        self.det = DepositDetector()
        self.patches = [
            patch.object(tx_verifier, "verify_deposit", side_effect=self.chain.verify_deposit),
            patch.object(tx_verifier, "get_recent_incoming", side_effect=self.chain.get_recent_incoming),
            patch("services.detector.notify_admins", new=AsyncMock()),
            patch("services.detector.safe_send_message", new=AsyncMock()),
            patch.object(DepositDetector, "_execute_payout", new=AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _status(self, order_id):
        db = SessionLocal()
        try:
            return db.query(Order).filter(Order.order_id == order_id).one().status
        finally:
            db.close()

    async def _process(self, order_id):
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.order_id == order_id).one()
            await self.det._process_order(db, order, bot_app=object())
        finally:
            db.close()

    def _attach_hash(self, order_id, tx_hash):
        self.db.query(Order).filter(Order.order_id == order_id).update({Order.deposit_tx_hash: tx_hash})
        self.db.commit()

    def _reviewed(self, order_id):
        db = SessionLocal()
        try:
            return db.query(AuditLog).filter(
                AuditLog.order_id == order_id, AuditLog.action == "DEPOSIT_HASH_NEEDS_REVIEW").first() is not None
        finally:
            db.close()

    def _new_order(self, order_id, requested, **kw):
        amount = assign_deposit_amount(self.db, "BSC", "USDT", HOT, Decimal(str(requested)))
        self.assertIsNotNone(amount)
        self.db.add(_order(order_id, amount, **kw))
        self.db.commit()
        return amount


class SkenarioA_IsiUlangStok(DetectorCase):
    async def test_isi_ulang_bulat_tanpa_hash_tidak_mengkonfirmasi_order_penyerang(self):
        self._new_order("ORD-ATK", 500, order_type="swap", telegram_id=666)
        self.chain.send(H("refill"), 500)  # owner isi ulang tepat 500 USDT
        await self._process("ORD-ATK")
        self.assertEqual(self._status("ORD-ATK"), "WAITING_CRYPTO_DEPOSIT")

    async def test_hash_isi_ulang_dari_wallet_owner_tidak_auto(self):
        """Penyerang menempel hash isi ulang owner (nominal persis sama) ke ordernya:
        pengirimnya wallet owner, jadi tidak boleh auto-konfirmasi/auto-payout."""
        owner = "0x" + "a" * 40
        self._new_order("ORD-ATK", 500, order_type="swap", telegram_id=666)
        self.chain.send(H("refill"), 500, sender=owner)
        self._attach_hash("ORD-ATK", H("refill"))
        with patch.object(settings, "OWNER_WALLET_ADDRESSES", (owner,)):
            await self._process("ORD-ATK")
        self.assertEqual(self._status("ORD-ATK"), "WAITING_CRYPTO_DEPOSIT")
        self.assertTrue(self._reviewed("ORD-ATK"))

    async def test_kontrol_hash_pas_milik_order_tetap_auto(self):
        amount = self._new_order("ORD-OK", 500)
        self.chain.send(H("mine"), amount)
        self._attach_hash("ORD-OK", H("mine"))
        await self._process("ORD-OK")
        self.assertEqual(self._status("ORD-OK"), "CRYPTO_CONFIRMED")


class SkenarioB_DepositTelat(DetectorCase):
    async def test_deposit_telat_korban_tidak_diambil_order_penyerang(self):
        old = datetime.utcnow() - timedelta(hours=2)
        victim_amount = self._new_order("ORD-VICTIM", 25, status="expired", created=old)
        # Penyerang meminta nominal yang sama persis dengan order korban, lalu menempel
        # hash deposit korban ke ordernya.
        attacker_amount = assign_deposit_amount(self.db, "BSC", "USDT", HOT, victim_amount)
        self.assertEqual(attacker_amount, victim_amount)
        self.db.add(_order("ORD-ATK", attacker_amount, telegram_id=666))
        self.db.commit()
        self.chain.send(H("victim"), victim_amount)
        self._attach_hash("ORD-ATK", H("victim"))
        await self._process("ORD-ATK")
        self.assertEqual(self._status("ORD-ATK"), "WAITING_CRYPTO_DEPOSIT")
        self.assertTrue(self._reviewed("ORD-ATK"), "ambigu: harus dicek admin, bukan diklaim diam-diam")

    async def test_order_lama_kembar_nominal_tidak_auto(self):
        """Order bernominal sama dengan order expired yang masih dalam jendela tidak boleh auto-klaim."""
        old = datetime.utcnow() - timedelta(hours=2)
        self.db.add(_order("ORD-VICTIM", 25, status="expired", created=old))
        self.db.add(_order("ORD-ATK", 25, telegram_id=666))
        self.db.commit()
        self.chain.send(H("victim"), 25)
        self._attach_hash("ORD-ATK", H("victim"))
        await self._process("ORD-ATK")
        self.assertEqual(self._status("ORD-ATK"), "WAITING_CRYPTO_DEPOSIT")


class SkenarioC_HashUser(DetectorCase):
    async def test_hash_isi_ulang_ditempel_ke_order_penyerang(self):
        amount = self._new_order("ORD-ATK", "499.5", telegram_id=666)
        self.chain.send(H("refill"), 500)
        self._attach_hash("ORD-ATK", H("refill"))
        self.assertLess(amount, Decimal("500"))
        await self._process("ORD-ATK")
        self.assertEqual(self._status("ORD-ATK"), "WAITING_CRYPTO_DEPOSIT")

    async def test_hash_nominal_kurang_ditolak(self):
        """User mengirim kurang dari nominal order: tidak boleh terkonfirmasi."""
        self._new_order("ORD-KURANG", 10)
        self.chain.send(H("kurang"), "9.99")
        self._attach_hash("ORD-KURANG", H("kurang"))
        await self._process("ORD-KURANG")
        self.assertEqual(self._status("ORD-KURANG"), "WAITING_CRYPTO_DEPOSIT")

    async def test_hash_pas_milik_sendiri_auto(self):
        amount = self._new_order("ORD-OK", 10)
        self.assertEqual(amount, Decimal("10"))
        self.chain.send(H("ok"), amount)
        self._attach_hash("ORD-OK", H("ok"))
        await self._process("ORD-OK")
        self.assertEqual(self._status("ORD-OK"), "CRYPTO_CONFIRMED")


class SkenarioD_NominalKembar(DetectorCase):
    async def test_dua_order_aktif_nominal_sama_dieskalasi_bukan_auto(self):
        """Tanpa kode unik dua user bisa menjual nominal yang sama persis. Hash masing-masing
        valid on-chain tetapi tidak bisa dipastikan pemiliknya -> admin yang memutuskan."""
        self._new_order("ORD-U1", 10, telegram_id=1)
        self._new_order("ORD-U2", 10, telegram_id=2)
        self.chain.send(H("u1"), 10)
        self._attach_hash("ORD-U1", H("u1"))
        await self._process("ORD-U1")
        self.assertEqual(self._status("ORD-U1"), "WAITING_CRYPTO_DEPOSIT")
        self.assertTrue(self._reviewed("ORD-U1"))
        self.assertEqual(self._status("ORD-U2"), "WAITING_CRYPTO_DEPOSIT")


class SkenarioE_TanpaHash(DetectorCase):
    async def test_deposit_pas_tanpa_hash_tidak_dikonfirmasi(self):
        """Auto-scan mati: deposit yang kebetulan sama persis tapi tanpa hash dari user tidak diklaim."""
        amount = self._new_order("ORD-NOHASH", 500)
        self.chain.send(H("mine"), amount)
        await self._process("ORD-NOHASH")
        self.assertEqual(self._status("ORD-NOHASH"), "WAITING_CRYPTO_DEPOSIT")

    async def test_autoscan_bisa_dinyalakan_lewat_setting(self):
        amount = self._new_order("ORD-SCAN", 500)
        self.chain.send(H("mine"), amount)
        with patch.object(settings, "DEPOSIT_AUTOSCAN_ENABLED", True):
            await self._process("ORD-SCAN")
        self.assertEqual(self._status("ORD-SCAN"), "CRYPTO_CONFIRMED")


class AssignAmount(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_nominal_tanpa_kode_unik_persis_yang_dipilih(self):
        for requested in ("100", "0.5", "10.5"):
            amt = assign_deposit_amount(self.db, "BSC", "USDT", HOT, Decimal(requested))
            self.assertEqual(amt, Decimal(requested), "tidak ada kode unik di nominal koin")
            self.assertEqual(amt, amt.quantize(Decimal("0.0001")), "presisi = presisi tampilan USDT")

    def test_nominal_dibulatkan_ke_bawah_ke_presisi_koin(self):
        self.assertEqual(assign_deposit_amount(self.db, "BSC", "USDT", HOT, Decimal("10.123456")), Decimal("10.1234"))
        self.assertIsNone(assign_deposit_amount(self.db, "BSC", "USDT", HOT, Decimal("0.00001")))

    def test_nominal_sama_boleh_dipakai_banyak_order(self):
        for i in range(5):
            amt = assign_deposit_amount(self.db, "BSC", "USDT", HOT, Decimal("100"))
            self.assertEqual(amt, Decimal("100"))
            self.db.add(_order(f"ORD-{i}", amt))
            self.db.commit()

    def test_eth_presisi_8(self):
        amt = assign_deposit_amount(self.db, "BASE", "ETH", HOT, Decimal("0.0123456789"))
        self.assertEqual(amt, Decimal("0.01234567"))


class SellE2E(unittest.IsolatedAsyncioTestCase):
    """Alur Jual/Convert asli: nominal yang diminta bot = nominal order, bulat, tanpa kode unik."""
    async def asyncSetUp(self):
        await e2e_setup(self)
    async def asyncTearDown(self):
        await e2e_teardown(self)
    _user = _e2e.BotFlowE2E._user
    _dispatch = _e2e.BotFlowE2E._dispatch
    say = _e2e.BotFlowE2E.say
    tap = _e2e.BotFlowE2E.tap
    _sell_order_waiting_deposit = _e2e.BotFlowE2E._sell_order_waiting_deposit
    A = 70001

    async def test_order_jual_nominal_bulat_tanpa_kode_unik(self):
        order_id = await self._sell_order_waiting_deposit(self.A)
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.order_id == order_id).one()
            amount = Decimal(str(order.crypto_amount))
        finally:
            db.close()
        self.assertEqual(amount, Decimal("10"), "nominal koin = persis yang dipilih user")
        shown = "\n".join(str(p.get("text") or p.get("caption") or "") for e, p in _e2e.FakeTelegram.calls
                          if e in ("sendMessage", "editMessageText", "sendPhoto"))
        self.assertIn(f"{amount:.4f}", shown, "nominal yang diminta harus sama persis dengan order")
        self.assertNotIn("kode unik", shown.lower())
        self.assertIn("TX Hash", shown, "user harus diminta mengirim TX Hash")

    async def test_order_convert_nominal_bulat_tanpa_kode_unik(self):
        await self.say(self.A, "/start")
        await self.tap(self.A, "start_swap", from_screen=False)
        await self.tap(self.A, "swap_src_sym_USDT", from_screen=False)
        await self.tap(self.A, "swap_src_net_BSC", from_screen=False)
        await self.tap(self.A, "swap_tgt_sym_ETH", from_screen=False)
        await self.tap(self.A, "swap_tgt_net_BASE", from_screen=False)
        await self.say(self.A, "20")
        await self.say(self.A, "0x" + "c" * 40)
        await self.say(self.A, "0x" + "a" * 40)  # wallet pengirim koin
        shown = await self.tap(self.A, "confirm_swap_order", from_screen=False)
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.telegram_id == self.A, Order.order_type == "swap").one()
            amount = Decimal(str(order.crypto_amount))
        finally:
            db.close()
        self.assertEqual(amount, Decimal("20"))
        self.assertIn(f"{amount:.4f}", shown)
        self.assertNotIn("kode unik", shown.lower())
        self.assertIn("TX Hash", shown)


if __name__ == "__main__":
    unittest.main()
