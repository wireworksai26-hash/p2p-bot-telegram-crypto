"""Dashboard user: bot beroperasi 24 jam setiap hari, kendala diproses admin pada jam operasional 08.00–23.59 WIB."""
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")
os.environ.setdefault("EVM_WALLET_ADDRESS", "0x" + "1" * 40)
os.environ.setdefault("EVM_PRIVATE_KEY", "")

from bot.handlers.start import build_welcome_message  # noqa: E402


class JamOperasionalDiDashboard(unittest.TestCase):
    def test_teks_24_7_tebal_dan_jam_operasional_admin(self):
        text = build_welcome_message(SimpleNamespace(first_name="Budi"),
                                     SimpleNamespace(balance_idr=0, total_orders=0), 10, 5)
        self.assertIn("<b>BOT BEROPERASI 24 JAM SETIAP HARI</b>", text)
        self.assertIn("08.00 – 23.59 WIB", text)
        self.assertIn("Apabila ada kendala atau pertanyaan bisa Chat Admin di Jam Operasional 🙏🏻", text)
        self.assertLess(text.index("24 JAM SETIAP HARI"), text.index("Silakan pilih menu"))


if __name__ == "__main__":
    unittest.main()
