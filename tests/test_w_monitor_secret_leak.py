"""Alarm monitor API tidak boleh mengirim API key RPC ke grup Telegram.

Dulu pesan alarm/pemulihan memuat URL RPC lengkap, mis.
https://bnb-mainnet.g.alchemy.com/v2/<API_KEY> atau ?api_key=<API_KEY>, ke grup
notifikasi yang anggotanya tidak semua berhak melihat secret.
"""
import os
import unittest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from services.coin_api_monitor import CoinAPIMonitor, redact_url  # noqa: E402

KEY = "AbCdEf0123456789XyZaLcHeMyKeY01"
URLS = [
    f"https://bnb-mainnet.g.alchemy.com/v2/{KEY}",
    f"https://toncenter.com/api/v2/jsonRPC?api_key={KEY}",
    f"https://mainnet.infura.io/v3/{KEY}",
    f"https://user:{KEY}@rpc.example.com/",
]


def _res(url):
    return {"name": "BSC", "symbol": "BNB", "url": url, "latency_ms": 12, "env_var": "BSC_RPC",
            "error_code": "HTTP_401", "error_detail": f"Client error '401' for url '{url}'",
            "impact": "-", "fallback_urls": [], "status": "DOWN", "category": "EVM_RPC"}


class MonitorRedaction(unittest.TestCase):
    def setUp(self):
        self.mon = CoinAPIMonitor()

    def test_alarm_dan_pemulihan_tanpa_api_key(self):
        for url in URLS:
            with self.subTest(url=url):
                self.assertNotIn(KEY, self.mon.format_alarm_message(_res(url)))
                self.assertNotIn(KEY, self.mon.format_recovery_message(_res(url)))

    def test_dashboard_tanpa_api_key(self):
        self.assertNotIn(KEY, self.mon.format_admin_dashboard([_res(u) for u in URLS]))

    def test_host_tetap_terlihat_untuk_diagnosa(self):
        self.assertIn("bnb-mainnet.g.alchemy.com", redact_url(URLS[0]))
        self.assertEqual(redact_url("https://bsc-dataseed.bnbchain.org"), "https://bsc-dataseed.bnbchain.org")


if __name__ == "__main__":
    unittest.main()
