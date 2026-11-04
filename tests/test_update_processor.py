"""Pemroses update: user lambat tidak menahan user lain, user yang sama tetap berurutan."""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1", "DATABASE_URL": "sqlite:///:memory:", "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "1", "EVM_WALLET_ADDRESS": "0x" + "1" * 40, "EVM_PRIVATE_KEY": "",
})

from bot.utils.update_processor import PerUserUpdateProcessor  # noqa: E402


def upd(uid):
    return SimpleNamespace(effective_user=None if uid is None else SimpleNamespace(id=uid))


class TestPerUserProcessor(unittest.IsolatedAsyncioTestCase):
    async def test_user_lambat_tidak_menahan_user_lain(self):
        proc = PerUserUpdateProcessor(8)
        order = []
        release = asyncio.Event()

        async def slow():
            await release.wait()
            order.append("lambat")

        async def fast():
            order.append("cepat")

        t1 = asyncio.create_task(proc.process_update(upd(1), slow()))
        await asyncio.sleep(0)
        await asyncio.wait_for(proc.process_update(upd(2), fast()), timeout=1)
        self.assertEqual(order, ["cepat"])  # user 2 selesai selagi user 1 masih menunggu
        release.set()
        await t1
        self.assertEqual(order, ["cepat", "lambat"])

    async def test_user_sama_berurutan(self):
        proc = PerUserUpdateProcessor(8)
        order = []

        async def job(name, delay):
            order.append(f"mulai-{name}")
            await asyncio.sleep(delay)
            order.append(f"selesai-{name}")

        await asyncio.gather(proc.process_update(upd(7), job("a", 0.05)), proc.process_update(upd(7), job("b", 0)))
        self.assertEqual(order, ["mulai-a", "selesai-a", "mulai-b", "selesai-b"])

    async def test_kunci_dibuang_setelah_selesai_dan_update_tanpa_user_jalan(self):
        proc = PerUserUpdateProcessor(8)
        ran = []

        async def job():
            ran.append(1)

        await proc.process_update(upd(5), job())
        await proc.process_update(upd(None), job())
        self.assertEqual(ran, [1, 1])
        self.assertEqual(proc._locks, {})

    async def test_error_handler_satu_user_tidak_mengunci_user_itu(self):
        proc = PerUserUpdateProcessor(8)

        async def boom():
            raise RuntimeError("x")

        with self.assertRaises(RuntimeError):
            await proc.process_update(upd(9), boom())
        ran = []

        async def ok():
            ran.append(1)

        await asyncio.wait_for(proc.process_update(upd(9), ok()), timeout=1)
        self.assertEqual(ran, [1])

    def test_aplikasi_bot_memakai_pemroses_ini(self):
        import main
        app = main.build_bot_application()
        self.assertIsInstance(app.update_processor, PerUserUpdateProcessor)


if __name__ == "__main__":
    unittest.main()
