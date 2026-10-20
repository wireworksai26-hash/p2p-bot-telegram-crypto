"""Pengaman satu-salinan: dua bot dengan token sama tidak boleh aktif bersamaan."""
import asyncio
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

from services.instance_lock import LOCK_KEY, SingleInstanceLock  # noqa: E402


class FakePostgres:
    """Meniru advisory lock Postgres: satu pemegang; kunci lepas saat koneksinya ditutup."""

    def __init__(self):
        self.holder = None
        self.down = False
        self.queries = []

    def engine(self):
        return FakeEngine(self)


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class FakeConn:
    _next_id = 0

    def __init__(self, pg):
        FakeConn._next_id += 1
        self.id = FakeConn._next_id
        self.pg = pg
        self.closed = False

    def execution_options(self, **kw):
        return self

    def execute(self, stmt, params=None):
        if self.pg.down or self.closed:
            raise RuntimeError("koneksi putus")
        sql = str(stmt)
        self.pg.queries.append(sql)
        if "pg_try_advisory_lock" in sql:
            assert params["k"] == LOCK_KEY
            if self.pg.holder in (None, self.id):
                self.pg.holder = self.id
                return FakeResult(True)
            return FakeResult(False)
        return FakeResult(1)

    def close(self):
        self.closed = True
        if self.pg.holder == self.id:
            self.pg.holder = None


class FakeEngine:
    def __init__(self, pg, name="postgresql"):
        self.pg = pg
        self.dialect = SimpleNamespace(name=name)

    def connect(self):
        if self.pg.down:
            raise RuntimeError("database mati")
        return FakeConn(self.pg)


class LockBehaviour(unittest.IsolatedAsyncioTestCase):
    def test_salinan_pertama_mendapat_kunci_kedua_ditolak(self):
        pg = FakePostgres()
        a, b = SingleInstanceLock(pg.engine()), SingleInstanceLock(pg.engine())
        self.assertTrue(a.try_acquire())
        self.assertFalse(b.try_acquire())
        self.assertIsNone(b._conn)  # koneksi salinan yang ditolak ditutup, tidak menggantung

    def test_kunci_lepas_saat_salinan_pertama_berhenti(self):
        pg = FakePostgres()
        a, b = SingleInstanceLock(pg.engine()), SingleInstanceLock(pg.engine())
        a.try_acquire()
        self.assertFalse(b.try_acquire())
        a.release()
        self.assertTrue(b.try_acquire())

    def test_sqlite_tanpa_kunci_dan_tanpa_koneksi(self):
        lock = SingleInstanceLock(SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
        self.assertFalse(lock.enabled)
        self.assertTrue(lock.try_acquire())
        self.assertTrue(lock.still_held())

    def test_database_mati_tidak_memblokir_bot(self):
        pg = FakePostgres()
        pg.down = True
        lock = SingleInstanceLock(pg.engine())
        self.assertIsNone(lock.try_acquire())  # tak pasti -> pemanggil tetap menjalankan bot

    async def test_acquire_blocking_menunggu_lalu_mengambil_alih(self):
        pg = FakePostgres()
        first, second = SingleInstanceLock(pg.engine()), SingleInstanceLock(pg.engine())
        first.try_acquire()
        waiter = asyncio.create_task(second.acquire_blocking(poll_seconds=0.02))
        await asyncio.sleep(0.15)
        self.assertFalse(waiter.done(), "salinan kedua harus menunggu selama yang pertama hidup")
        first.release()  # salinan pertama berhenti (deploy baru menggantikan)
        await asyncio.wait_for(waiter, timeout=2)
        self.assertEqual(pg.holder, second._conn.id)

    async def test_acquire_blocking_langsung_selesai_bila_tidak_ada_pesaing(self):
        pg = FakePostgres()
        lock = SingleInstanceLock(pg.engine())
        await asyncio.wait_for(lock.acquire_blocking(poll_seconds=0.02), timeout=2)
        self.assertEqual(pg.holder, lock._conn.id)

    async def test_acquire_blocking_tidak_menunggu_selamanya_bila_db_mati(self):
        pg = FakePostgres()
        pg.down = True
        await asyncio.wait_for(SingleInstanceLock(pg.engine()).acquire_blocking(poll_seconds=0.02), timeout=2)

    def test_pemantauan_koneksi_putus_lalu_diambil_ulang(self):
        pg = FakePostgres()
        lock = SingleInstanceLock(pg.engine())
        lock.try_acquire()
        pg.holder = None          # server memutus sesi lama (kunci lepas)
        lock._conn.closed = True  # koneksi lama mati
        self.assertTrue(lock.still_held())   # tidak ada pesaing: diambil ulang
        self.assertEqual(pg.holder, lock._conn.id)

    def test_pemantauan_kunci_diambil_salinan_lain(self):
        pg = FakePostgres()
        mine, other = SingleInstanceLock(pg.engine()), SingleInstanceLock(pg.engine())
        mine.try_acquire()
        pg.holder = None
        mine._conn.closed = True       # koneksi saya putus, kunci lepas...
        self.assertTrue(other.try_acquire())  # ...dan salinan lain mengambilnya
        self.assertFalse(mine.still_held())   # saya sadar kunci hilang -> harus berhenti

    async def test_watch_memanggil_on_lost_bila_kunci_hilang(self):
        pg = FakePostgres()
        mine, other = SingleInstanceLock(pg.engine()), SingleInstanceLock(pg.engine())
        mine.try_acquire()
        lost = asyncio.Event()
        task = asyncio.create_task(mine.watch(lost.set, interval=0.02))
        await asyncio.sleep(0.05)
        self.assertFalse(lost.is_set())
        pg.holder = None
        mine._conn.closed = True
        other.try_acquire()
        await asyncio.wait_for(lost.wait(), timeout=2)
        await task


class Wiring(unittest.TestCase):
    def test_main_memakai_pengaman_sebelum_polling(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        i_lock = src.index("acquire_blocking()")
        i_build = src.index("application = build_bot_application()", i_lock)
        i_poll = src.index("start_polling(")
        self.assertLess(i_lock, i_build)   # kunci diambil sebelum aplikasi/penjadwal dibuat
        self.assertLess(i_build, i_poll)

    def test_bisa_dimatikan_lewat_env(self):
        from config.settings import settings
        self.assertTrue(hasattr(settings, "SINGLE_INSTANCE_LOCK"))


if __name__ == "__main__":
    unittest.main()
