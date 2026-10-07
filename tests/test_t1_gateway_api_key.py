"""T1 — API key gateway GoPay tidak boleh default dan tidak boleh lewat query string.

Dulu bot & gateway memakai key "RAHASIA" bila env kosong, gateway listen di
0.0.0.0 dengan CORS terbuka, dan key dikirim di URL (?api_key=...) sehingga
tercatat di log/proxy. Siapa pun yang menjangkau port 3005 bisa membaca mutasi
merchant dan "meracuni" klaim transaksi (/check-payment dengan trx_id palsu).
"""
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from services import gopay_service as gs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KEY = "k" * 40


class BotSendsKeyInHeader(unittest.IsolatedAsyncioTestCase):
    async def _capture(self, call):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"success": True, "paid": False, "data": {"transactions": []}})

        real = httpx.AsyncClient
        with patch.object(gs.httpx, "AsyncClient",
                          side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            svc = gs.GopayGatewayService()
            svc.api_key = KEY
            await call(svc)
        return seen

    async def test_check_payment_key_di_header_bukan_url(self):
        seen = await self._capture(lambda s: s.check_payment(50_137, "ORD-1"))
        self.assertEqual(seen[0].headers.get("x-api-key"), KEY)
        self.assertNotIn(KEY, str(seen[0].url))

    async def test_transactions_key_di_header_bukan_url(self):
        seen = await self._capture(lambda s: s.get_recent_transactions(page_size=5))
        self.assertEqual(seen[0].headers.get("x-api-key"), KEY)
        self.assertNotIn(KEY, str(seen[0].url))


class NoDefaultKey(unittest.TestCase):
    def test_settings_tanpa_env_tidak_memakai_rahasia(self):
        env = {k: v for k, v in os.environ.items() if k != "GOPAY_API_KEY"}
        env.update(PYTHON_DOTENV_DISABLED="1", DATABASE_URL="sqlite:///:memory:")
        out = subprocess.run(
            ["python", "-B", "-c",
             "from services.gopay_service import GopayGatewayService as G;"
             "from config.settings import settings as s;"
             "print(repr(s.GOPAY_API_KEY), repr(G().api_key))"],
            capture_output=True, text=True, cwd=ROOT, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("RAHASIA", out.stdout)

    def test_start_sh_tidak_memakai_key_default(self):
        script = (ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertNotIn("RAHASIA", script)

    def test_env_example_tidak_berisi_key_default(self):
        for f in (ROOT / ".env.example", ROOT / "gopay-gateway" / ".env.example"):
            self.assertNotIn("=RAHASIA", f.read_text(encoding="utf-8"), str(f))


if __name__ == "__main__":
    unittest.main()
