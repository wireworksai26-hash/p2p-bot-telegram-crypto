"""Link TX hash di testimoni: setiap jaringan yang dilayani bot harus punya link explorer.

Bug: testimoni APT (Aptos) menampilkan potongan hash "0x238a...c1f4" tanpa link karena AptosSender
tidak punya config["explorer"] dan APTOS tidak ada di daftar cadangan.
"""
import os
import unittest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from services.chain_maintenance import known_networks  # noqa: E402
from services.testimony_service import format_testimony_message, get_explorer_url_for_tx  # noqa: E402

APTOS_HASH = "0x238a" + "0" * 56 + "c1f4"


class LinkExplorerTestimoni(unittest.TestCase):
    def test_semua_jaringan_punya_link(self):
        for network in known_networks():
            with self.subTest(network=network):
                self.assertTrue(get_explorer_url_for_tx(network, "0x" + "ab" * 32))

    def test_link_aptos(self):
        self.assertEqual(get_explorer_url_for_tx("APTOS", APTOS_HASH),
                         f"https://explorer.aptoslabs.com/txn/{APTOS_HASH}?network=mainnet")

    def test_testimoni_beli_dan_jual_aptos_memakai_link(self):
        for order_type in ("buy", "sell"):
            with self.subTest(order_type=order_type):
                msg = format_testimony_message(order_type, "APT", "APTOS", 5500, "Irfanoo", 1, tx_hash=APTOS_HASH)
                self.assertIn('<a href="https://explorer.aptoslabs.com/txn/', msg)
                self.assertNotIn("0x238a...c1f4", msg)


if __name__ == "__main__":
    unittest.main()
