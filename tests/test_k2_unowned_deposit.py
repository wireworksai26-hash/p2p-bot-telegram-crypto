"""K2 — deposit yang bukan milik order tidak boleh diklaim order lain.

Hot wallet dipakai bersama dan deposit dicocokkan lewat nominal. Serangan:
A. Owner mengisi ulang stok dengan nominal bulat (mis. tepat 10 USDT). Order
   Jual/Convert penyerang bernominal sama terkonfirmasi otomatis (Convert
   langsung membayar koin ke penyerang).
B. Order korban sudah `expired`, depositnya masuk telat (masih dalam jendela
   24 jam). Order penyerang bernominal sama mengambil deposit itu.
C. Penyerang menempel hash isi ulang stok ke ordernya (jalur hash user,
   dulu toleransi lebih bayar 0,5%).

Rantai disimulasikan: daftar deposit masuk on-chain, `get_recent_incoming`
menyaring nominal persis seperti aslinya, `verify_deposit` memeriksa nominal
>= order seperti aslinya.
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

    def send(self, tx_hash, amount, when=None):
        self.deposits[tx_hash] = (Decimal(str(amount)), tx_verifier._timestamp(when or datetime.utcnow()))

    async def verify_deposit(self, network, symbol, tx_hash, expected_wallet, expected_amount,
                             not_before=None, not_after=None):
        if tx_hash not in self.deposits:
            return tx_verifier._fail("Transaksi gagal atau belum confirmed.")
        amount, stamp = self.deposits[tx_hash]
        if not tx_verifier._amount_matches(amount, expected_amount):
            return tx_verifier._fail(f"Nominal deposit kurang: diterima {amount} {symbol}, "
                                     f"dibutuhkan {Decimal(str(expected_amount))} {symbol}.")
        return tx_verifier._ok(amount, stamp, tx_hash)

    async def get_recent_incoming(self, network, symbol, wallet, min_amount=0.0, limit=20,
                                  not_before=None, not_after=None):
        out = []
        for tx_hash, (amount, stamp) in self.deposits.items():
            if tx_verifier.automatic_amount_matches(amount, min_amount):
                out.append(tx_verifier._ok(amount, stamp, tx_hash))
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

    def _new_order(self, order_id, requested, **kw):
        amount = assign_deposit_amount(self.db, "BSC", "USDT", HOT, Decimal(str(requested)))
        self.assertIsNotNone(amount)
        self.db.add(_order(order_id, amount, **kw))
        self.db.commit()
        return amount


class SkenarioA_IsiUlangStok(DetectorCase):
    async def test_isi_ulang_bulat_tidak_mengkonfirmasi_order_penyerang(self):
        self._new_order("ORD-ATK", 500, order_type="swap", telegram_id=666)
        self.chain.send(H("refill"), 500)  # owner isi ulang tepat 500 USDT
        await self._process("ORD-ATK")
        self.assertEqual(self._status("ORD-ATK"), "WAITING_CRYPTO_DEPOSIT")

    async def test_kontrol_deposit_pas_milik_order_tetap_auto(self):
        amount = self._new_order("ORD-OK", 500)
        self.chain.send(H("mine"), amount)
        await self._process("ORD-OK")
        self.assertEqual(self._status("ORD-OK"), "CRYPTO_CONFIRMED")


class SkenarioB_DepositTelat(DetectorCase):
    async def test_deposit_telat_korban_tidak_diambil_order_penyerang(self):
        old = datetime.utcnow() - timedelta(hours=2)
        victim_amount = self._new_order("ORD-VICTIM", 25, status="expired", created=old)
        # Penyerang meminta nominal yang sama persis dengan order korban.
        attacker_amount = assign_deposit_amount(self.db, "BSC", "USDT", HOT, victim_amount)
        self.db.add(_order("ORD-ATK", attacker_amount, telegram_id=666))
        self.db.commit()
        self.chain.send(H("victim"), victim_amount)
        await self._process("ORD-ATK")
        self.assertEqual(self._status("ORD-ATK"), "WAITING_CRYPTO_DEPOSIT")
        await self._process("ORD-VICTIM")
        self.assertEqual(self._status("ORD-VICTIM"), "CRYPTO_CONFIRMED")

    async def test_order_lama_kembar_nominal_tidak_auto(self):
        """Pertahanan berlapis: order lama (tanpa kode unik) bernominal sama dengan
        order expired yang masih dalam jendela tidak boleh auto-klaim."""
        old = datetime.utcnow() - timedelta(hours=2)
        self.db.add(_order("ORD-VICTIM", 25, status="expired", created=old))
        self.db.add(_order("ORD-ATK", 25, telegram_id=666))
        self.db.commit()
        self.chain.send(H("victim"), 25)
        await self._process("ORD-ATK")
        self.assertEqual(self._status("ORD-ATK"), "WAITING_CRYPTO_DEPOSIT")


class SkenarioC_HashUser(DetectorCase):
    async def test_hash_isi_ulang_ditempel_ke_order_penyerang(self):
        amount = self._new_order("ORD-ATK", "499.5", telegram_id=666)
        self.chain.send(H("refill"), 500)
        self.db.query(Order).filter(Order.order_id == "ORD-ATK").update({Order.deposit_tx_hash: H("refill")})
        self.db.commit()
        self.assertLess(amount, Decimal("500"))
        await self._process("ORD-ATK")
        self.assertEqual(self._status("ORD-ATK"), "WAITING_CRYPTO_DEPOSIT")

    async def test_lupa_kode_unik_dieskalasi_ke_admin(self):
        amount = self._new_order("ORD-LUPA", 10)
        self.assertNotEqual(amount, Decimal("10"))
        self.chain.send(H("lupa"), 10)  # user kirim tanpa kode unik
        self.db.query(Order).filter(Order.order_id == "ORD-LUPA").update({Order.deposit_tx_hash: H("lupa")})
        self.db.commit()
        await self._process("ORD-LUPA")
        self.assertEqual(self._status("ORD-LUPA"), "WAITING_CRYPTO_DEPOSIT")
        db = SessionLocal()
        try:
            self.assertTrue(db.query(AuditLog).filter(
                AuditLog.order_id == "ORD-LUPA", AuditLog.action == "DEPOSIT_HASH_NEEDS_REVIEW").first())
        finally:
            db.close()

    async def test_hash_pas_milik_sendiri_auto(self):
        amount = self._new_order("ORD-OK", 10)
        self.chain.send(H("ok"), amount)
        self.db.query(Order).filter(Order.order_id == "ORD-OK").update({Order.deposit_tx_hash: H("ok")})
        self.db.commit()
        await self._process("ORD-OK")
        self.assertEqual(self._status("ORD-OK"), "CRYPTO_CONFIRMED")


class AssignAmount(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_nominal_unik_tidak_bulat_dan_presisi_tampilan(self):
        seen = set()
        for i in range(60):
            amt = assign_deposit_amount(self.db, "BSC", "USDT", HOT, Decimal("100"))
            self.assertEqual(amt, amt.quantize(Decimal("0.0001")), "presisi = presisi tampilan USDT")
            self.assertNotEqual(amt, amt.quantize(Decimal("0.01")), "dua digit terakhir tidak boleh 00")
            self.assertTrue(Decimal("100") < amt < Decimal("100.01"))
            self.assertNotIn(amt, seen)
            seen.add(amt)
            self.db.add(_order(f"ORD-{i}", amt))
            self.db.commit()

    def test_eth_presisi_8_dan_kode_kecil(self):
        amt = assign_deposit_amount(self.db, "BASE", "ETH", HOT, Decimal("0.0123456789"))
        self.assertEqual(amt, amt.quantize(Decimal("0.00000001")))
        self.assertTrue(Decimal("0.012345") < amt < Decimal("0.012346"))


class SellE2E(unittest.IsolatedAsyncioTestCase):
    """Alur Jual asli: nominal yang diminta bot = nominal order, berkode unik."""
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

    async def test_order_jual_berkode_unik_dan_tampil_persis(self):
        order_id = await self._sell_order_waiting_deposit(self.A)
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.order_id == order_id).one()
            amount = Decimal(str(order.crypto_amount))
        finally:
            db.close()
        self.assertNotEqual(amount, Decimal("10"), "isi ulang tepat 10 USDT tidak boleh cocok")
        shown = "\n".join(str(p.get("text") or p.get("caption") or "") for e, p in _e2e.FakeTelegram.calls
                          if e in ("sendMessage", "editMessageText", "sendPhoto"))
        self.assertIn(f"{amount:.4f}", shown, "nominal yang diminta harus sama persis dengan order")

    async def test_order_convert_berkode_unik(self):
        await self.say(self.A, "/start")
        await self.tap(self.A, "start_swap", from_screen=False)
        await self.tap(self.A, "swap_src_sym_USDT", from_screen=False)
        await self.tap(self.A, "swap_src_net_BSC", from_screen=False)
        await self.tap(self.A, "swap_tgt_sym_ETH", from_screen=False)
        await self.tap(self.A, "swap_tgt_net_BASE", from_screen=False)
        await self.say(self.A, "20")
        await self.say(self.A, "0x" + "c" * 40)
        shown = await self.tap(self.A, "confirm_swap_order", from_screen=False)
        db = SessionLocal()
        try:
            order = db.query(Order).filter(Order.telegram_id == self.A, Order.order_type == "swap").one()
            amount = Decimal(str(order.crypto_amount))
        finally:
            db.close()
        self.assertNotEqual(amount, amount.quantize(Decimal("0.01")))
        self.assertIn(f"{amount:.4f}", shown)


if __name__ == "__main__":
    unittest.main()
