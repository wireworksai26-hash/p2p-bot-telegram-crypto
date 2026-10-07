"""Harga koin yang tidak ada di OKX (TON) + fallback RPC non-EVM.

1. OKX sukses tapi tanpa TON: TON diisi dari CoinPaprika/CMC/CoinGecko (dulu tampil "- (~-)").
2. Pengisian tidak memanggil sumber cadangan tiap refresh (hemat kuota).
3. Verifikasi TRON/SUI/Aptos berpindah host bila host pertama mati.
4. Pemilih host TRON sender melewati host yang tidak sehat.
"""
import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
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
    "EVM_PRIVATE_KEY": "0x" + "1" * 64,
})

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base
from services import price_service as ps_mod
from services import tx_verifier
from services.price_service import PriceService

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
TestingSession = sessionmaker(bind=engine)

FX = 17000.0


def _okx_tanpa_ton():
    """Feed OKX lengkap kecuali TON dan USDG."""
    return {
        "ethereum": 2000.0, "solana": 150.0, "binancecoin": 600.0, "tether": 1.0, "usd-coin": 1.0,
        "tron": 0.2, "sui": 3.0, "aptos": 9.0, "avalanche-2": 30.0, "arbitrum": 0.8, "optimism": 2.0,
        "kaia": 0.2, "berachain-bera": 4.0, "hyperliquid": 25.0, "polygon-ecosystem-token": 0.5,
    }


class TestHargaTon(unittest.TestCase):
    def setUp(self):
        self.ps = PriceService()
        self.paprika_rows = None

    def _patch(self, paprika_usd=1.46, paprika_ok=True, cg_rows=None):
        ps = self.ps

        async def paprika(fx):
            if not paprika_ok:
                return None
            return ps._rows_from_usd({"the-open-network": paprika_usd}, fx)

        async def cg():
            return cg_rows

        self.mocks = [
            patch.object(ps, "_fetch_okx_usd", new=AsyncMock(return_value=_okx_tanpa_ton())),
            patch.object(ps, "_fetch_spot_fx", new=AsyncMock(return_value=FX)),
            patch.object(ps, "_fetch_paprika", side_effect=paprika),
            patch.object(ps, "_fetch_cmc", new=AsyncMock(return_value=None)),
            patch.object(ps, "_fetch_coingecko", side_effect=cg),
        ]
        started = [m.start() for m in self.mocks]
        self.addCleanup(lambda: [m.stop() for m in self.mocks])
        return started

    def _price(self, symbol):
        db = TestingSession()
        try:
            return asyncio.run(self.ps.get_price(symbol, db))
        finally:
            db.close()

    def test_ton_terisi_dari_coinpaprika_saat_okx_tidak_punya(self):
        self._patch()
        result = self._price("TON")
        self.assertIsNotNone(result, "TON tidak boleh kosong ('- (~-)')")
        self.assertAlmostEqual(result["market_price_idr"], 1.46 * FX, places=1)
        self.assertIn("CoinPaprika", result["source"])
        self.assertIn("OKX", result["source"])

    def test_koin_okx_tidak_terpengaruh(self):
        self._patch()
        eth = self._price("ETH")
        self.assertAlmostEqual(eth["market_price_idr"], 2000.0 * FX, places=1)
        self.assertIsNotNone(self._price("SOL"))

    def test_fallback_ke_coingecko_bila_paprika_dan_cmc_gagal(self):
        now = int(time.time())
        self._patch(paprika_ok=False, cg_rows={"the-open-network": {"idr": 26000.0, "usd": 1.5, "last_updated_at": now}})
        result = self._price("TON")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["market_price_idr"], 26000.0, places=1)
        self.assertIn("CoinGecko", result["source"])

    def test_semua_sumber_gagal_tetap_none_bukan_crash(self):
        self._patch(paprika_ok=False, cg_rows=None)
        self.assertIsNone(self._price("TON"))
        self.assertIsNotNone(self._price("ETH"), "koin lain tetap tampil")

    def test_pengisian_tidak_memanggil_cadangan_tiap_refresh(self):
        started = self._patch()
        asyncio.run(self.ps._refresh(force=True))
        asyncio.run(self.ps._refresh(force=True))
        asyncio.run(self.ps._refresh(force=True))
        paprika_mock = started[2]
        self.assertEqual(paprika_mock.call_count, 1, "baris TON yang masih segar dipakai ulang dari cache")
        self.assertIsNotNone(self._price("TON"))

    def test_pengisian_diperbarui_setelah_lewat_batas(self):
        started = self._patch()
        asyncio.run(self.ps._refresh(force=True))
        self.ps._cache["the-open-network"]["last_updated_at"] -= ps_mod.FILL_REFRESH_SECONDS + 5
        asyncio.run(self.ps._refresh(force=True))
        self.assertEqual(started[2].call_count, 2)


class TestFallbackRpcVerifier(unittest.IsolatedAsyncioTestCase):
    async def test_tron_pindah_host_bila_pertama_mati(self):
        panggilan = []

        async def fake_json(method, url, **kw):
            panggilan.append(url)
            if "dead-host" in url:
                raise RuntimeError("down")
            return {"id": "x"}

        with patch.object(tx_verifier, "TRON_HOSTS", ["https://dead-host.example", "https://ok-host.example"]), \
             patch.object(tx_verifier, "_json", side_effect=fake_json):
            hasil = await tx_verifier._tron("gettransactioninfobyid", "a" * 64)
        self.assertEqual(hasil, {"id": "x"})
        self.assertEqual(len(panggilan), 2)
        self.assertIn("dead-host", panggilan[0])
        self.assertIn("ok-host", panggilan[1])

    async def test_tron_semua_host_mati_melempar_error_jelas(self):
        with patch.object(tx_verifier, "TRON_HOSTS", ["https://a.example", "https://b.example"]), \
             patch.object(tx_verifier, "_json", new=AsyncMock(side_effect=RuntimeError("down"))):
            with self.assertRaises(RuntimeError) as ctx:
                await tx_verifier._tron("gettransactionbyid", "a" * 64)
        self.assertIn("Semua RPC TRON gagal", str(ctx.exception))

    async def test_tron_api_key_hanya_dikirim_ke_trongrid(self):
        headers_dipakai = {}

        async def fake_json(method, url, **kw):
            headers_dipakai[url] = kw.get("headers")
            if "api.trongrid.io" in url:
                raise RuntimeError("limit")
            return {}

        with patch.object(tx_verifier, "TRON_HOSTS", ["https://api.trongrid.io", "https://api.tronstack.io"]), \
             patch.object(tx_verifier.settings, "TRONGRID_API_KEY", "SECRET"), \
             patch.object(tx_verifier, "_json", side_effect=fake_json):
            await tx_verifier._tron("gettransactionbyid", "b" * 64)
        self.assertEqual(headers_dipakai["https://api.trongrid.io/walletsolidity/gettransactionbyid"],
                         {"TRON-PRO-API-KEY": "SECRET"})
        self.assertEqual(headers_dipakai["https://api.tronstack.io/walletsolidity/gettransactionbyid"], {})

    async def test_sui_pindah_rpc(self):
        async def fake_rpc(url, method, params):
            if "dead" in url:
                raise RuntimeError("down")
            return {"ok": url}

        with patch.object(tx_verifier, "SUI_RPCS", ["https://dead.example", "https://live.example"]), \
             patch.object(tx_verifier, "_rpc", side_effect=fake_rpc):
            self.assertEqual(await tx_verifier._sui_rpc("m", []), {"ok": "https://live.example"})

    async def test_aptos_pindah_rpc(self):
        async def fake_json(method, url, **kw):
            if "dead" in url:
                raise RuntimeError("down")
            return {"hash": url}

        with patch.object(tx_verifier, "APTOS_RPCS", ["https://dead.example/v1", "https://live.example/v1"]), \
             patch.object(tx_verifier, "_json", side_effect=fake_json):
            hasil = await tx_verifier._aptos_get("transactions/by_hash/0x1")
        self.assertEqual(hasil, {"hash": "https://live.example/v1/transactions/by_hash/0x1"})

    async def test_daftar_host_default_punya_cadangan(self):
        for name in ("TRON_HOSTS", "SUI_RPCS", "APTOS_RPCS", "SOLANA_RPCS"):
            with self.subTest(jaringan=name):
                self.assertGreaterEqual(len(getattr(tx_verifier, name)), 2, f"{name} butuh minimal 2 host")
                self.assertEqual(len(getattr(tx_verifier, name)), len(set(getattr(tx_verifier, name))))


class TestTronSenderHost(unittest.IsolatedAsyncioTestCase):
    async def test_host_sehat_melewati_host_mati(self):
        from services.crypto_sender import tron_sender

        class Resp:
            def __init__(self, status, body):
                self.status_code, self._body = status, body

            def json(self):
                return self._body

        class Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                if "dead" in url:
                    raise RuntimeError("down")
                return Resp(200, {"blockID": "00ab"})

        with patch.object(tron_sender, "TRON_HOSTS", ["https://dead.example", "https://live.example"]), \
             patch("httpx.AsyncClient", Client):
            self.assertEqual(await tron_sender.healthy_tron_host(), "https://live.example")

    async def test_semua_host_mati_kembali_ke_primer(self):
        from services.crypto_sender import tron_sender

        class Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                raise RuntimeError("down")

        with patch.object(tron_sender, "TRON_HOSTS", ["https://a.example", "https://b.example"]), \
             patch("httpx.AsyncClient", Client):
            self.assertEqual(await tron_sender.healthy_tron_host(), "https://a.example")


if __name__ == "__main__":
    unittest.main()
