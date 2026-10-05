import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "DATABASE_URL": "sqlite:///:memory:",
    "ADMIN_CHAT_IDS": "999",
})

from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.connection import Base
from database.models import User, UserSavedWallet, UserSavedBank
from database.crud import (
    create_user,
    get_user_saved_wallets,
    get_saved_wallet_by_id,
    save_user_wallet,
    delete_user_saved_wallet,
    get_user_saved_banks,
    get_saved_bank_by_id,
    save_user_bank,
    delete_user_saved_bank,
    detect_account_type,
)
from bot.handlers.saved_accounts import (
    build_saved_wallets_view,
    build_saved_banks_view,
    handle_saved_account_text_input,
)


class BaseDBSessionTest:
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db_session = self.TestingSessionLocal()

    def tearDown(self):
        self.db_session.close()
        Base.metadata.drop_all(bind=self.engine)


class TestSavedWalletCRUD(BaseDBSessionTest, unittest.TestCase):
    def test_save_and_get_user_wallet(self):
        db_session = self.db_session
        user = create_user(db_session, telegram_id=11111, username="alice", full_name="Alice")
        
        # Save EVM wallet
        w1 = save_user_wallet(
            db_session,
            telegram_id=user.telegram_id,
            wallet_address="0x71C839556CB3250b716773B3aBE329a4a796c9c6",
            network="BSC",
            label="Metamask Utama"
        )
        self.assertIsNotNone(w1.id)
        self.assertEqual(w1.network, "BSC")
        self.assertEqual(w1.label, "Metamask Utama")

        # Get all
        all_wallets = get_user_saved_wallets(db_session, user.telegram_id)
        self.assertEqual(len(all_wallets), 1)
        self.assertEqual(all_wallets[0].wallet_address, "0x71C839556CB3250b716773B3aBE329a4a796c9c6")

        # Filter per network
        bsc_wallets = get_user_saved_wallets(db_session, user.telegram_id, network="BSC")
        self.assertEqual(len(bsc_wallets), 1)

        tron_wallets = get_user_saved_wallets(db_session, user.telegram_id, network="TRON")
        self.assertEqual(len(tron_wallets), 0)

    def test_save_wallet_duplicate_updates(self):
        db_session = self.db_session
        user = create_user(db_session, telegram_id=22222)
        addr = "0x71C839556CB3250b716773B3aBE329a4a796c9c6"
        
        w1 = save_user_wallet(db_session, user.telegram_id, addr, network="BSC", label="Old Label")
        w2 = save_user_wallet(db_session, user.telegram_id, addr, network="ETH", label="New Label")
        
        self.assertEqual(w1.id, w2.id)
        self.assertEqual(w2.label, "New Label")
        self.assertEqual(w2.network, "ETH")
        self.assertEqual(len(get_user_saved_wallets(db_session, user.telegram_id)), 1)

    def test_delete_user_saved_wallet(self):
        db_session = self.db_session
        user = create_user(db_session, telegram_id=33333)
        addr = "0x71C839556CB3250b716773B3aBE329a4a796c9c6"
        w = save_user_wallet(db_session, user.telegram_id, addr, network="BSC")
        
        # Delete with wrong user returns False
        self.assertFalse(delete_user_saved_wallet(db_session, w.id, telegram_id=99999))
        self.assertIsNotNone(get_saved_wallet_by_id(db_session, w.id))

        # Delete with correct user returns True
        self.assertTrue(delete_user_saved_wallet(db_session, w.id, telegram_id=user.telegram_id))
        self.assertIsNone(get_saved_wallet_by_id(db_session, w.id))


class TestSavedBankCRUD(BaseDBSessionTest, unittest.TestCase):
    def test_detect_account_type(self):
        self.assertEqual(detect_account_type("BCA"), "BANK")
        self.assertEqual(detect_account_type("Bank Mandiri"), "BANK")
        self.assertEqual(detect_account_type("BRI"), "BANK")
        self.assertEqual(detect_account_type("GOPAY"), "EWALLET")
        self.assertEqual(detect_account_type("Go-Pay"), "EWALLET")
        self.assertEqual(detect_account_type("DANA"), "EWALLET")
        self.assertEqual(detect_account_type("OVO"), "EWALLET")
        self.assertEqual(detect_account_type("SHOPEEPAY"), "EWALLET")

    def test_save_and_get_user_bank_and_ewallet(self):
        db_session = self.db_session
        user = create_user(db_session, telegram_id=44444)

        b1 = save_user_bank(
            db_session,
            telegram_id=user.telegram_id,
            bank_name="BCA",
            account_number="882049281",
            account_name="Budi Santoso"
        )
        self.assertEqual(b1.account_type, "BANK")
        self.assertEqual(b1.bank_name, "BCA")

        b2 = save_user_bank(
            db_session,
            telegram_id=user.telegram_id,
            bank_name="GOPAY",
            account_number="081234567890",
            account_name="Budi Santoso"
        )
        self.assertEqual(b2.account_type, "EWALLET")
        self.assertEqual(b2.bank_name, "GOPAY")

        banks = get_user_saved_banks(db_session, user.telegram_id)
        self.assertEqual(len(banks), 2)

    def test_delete_user_saved_bank(self):
        db_session = self.db_session
        user = create_user(db_session, telegram_id=55555)
        b = save_user_bank(db_session, user.telegram_id, "BCA", "12345678", "Budi")

        self.assertFalse(delete_user_saved_bank(db_session, b.id, telegram_id=99999))
        self.assertTrue(delete_user_saved_bank(db_session, b.id, telegram_id=user.telegram_id))
        self.assertIsNone(get_saved_bank_by_id(db_session, b.id))


class TestSavedAccountsUIViews(BaseDBSessionTest, unittest.TestCase):
    def test_build_saved_wallets_view_empty_and_populated(self):
        db_session = self.db_session
        user = create_user(db_session, telegram_id=66666)

        # Empty view matches Screenshot 1 text
        text_empty, markup_empty = build_saved_wallets_view(user.telegram_id, db_session)
        self.assertIn("Belum ada alamat tersimpan.", text_empty)
        self.assertIn("Pilih tombol di bawah untuk menambahkan / mengubah Addres.", text_empty)
        self.assertTrue(any(btn.text == "📌 Tambah / Simpan Addres" for row in markup_empty.inline_keyboard for btn in row))

        # Add wallet
        save_user_wallet(db_session, user.telegram_id, "0x71C839556CB3250b716773B3aBE329a4a796c9c6", "BSC")
        text_pop, markup_pop = build_saved_wallets_view(user.telegram_id, db_session)
        self.assertIn("Daftar Alamat Tersimpan:", text_pop)
        self.assertIn("0x71C839556CB3250b716773B3aBE329a4a796c9c6", text_pop)
        self.assertTrue(any(btn.text == "🗑 Hapus Alamat" for row in markup_pop.inline_keyboard for btn in row))

    def test_build_saved_banks_view_empty_and_populated(self):
        db_session = self.db_session
        user = create_user(db_session, telegram_id=77777)

        # Empty view matches Screenshot 3 text
        text_empty, markup_empty = build_saved_banks_view(user.telegram_id, db_session)
        self.assertIn("Belum ada rekening tersimpan", text_empty)
        self.assertIn("Pilih tombol dibawah untuk menambah atau ganti rekening.", text_empty)
        self.assertTrue(any(btn.text == "✍️ Tambah / Ganti Rekening" for row in markup_empty.inline_keyboard for btn in row))

        # Add bank & ewallet
        save_user_bank(db_session, user.telegram_id, "BCA", "882049281", "Budi Santoso")
        save_user_bank(db_session, user.telegram_id, "GOPAY", "081234567890", "Budi Santoso")
        text_pop, markup_pop = build_saved_banks_view(user.telegram_id, db_session)
        self.assertIn("Daftar Rekening / E-Wallet Tersimpan:", text_pop)
        self.assertIn("BCA", text_pop)
        self.assertIn("GOPAY", text_pop)
        self.assertTrue(any(btn.text == "🗑 Hapus Rekening" for row in markup_pop.inline_keyboard for btn in row))


class TestInteractiveSaveHandlers(BaseDBSessionTest, unittest.IsolatedAsyncioTestCase):
    async def test_handle_saved_account_text_input_wallet(self):
        db_session = self.db_session
        update = MagicMock()
        update.effective_user.id = 88888
        update.message.text = "0x71C839556CB3250b716773B3aBE329a4a796c9c6"
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.user_data = {"awaiting_save_wallet": True}

        with patch("bot.handlers.saved_accounts.SessionLocal", return_value=db_session):
            handled = await handle_saved_account_text_input(update, context)

        self.assertTrue(handled)
        self.assertNotIn("awaiting_save_wallet", context.user_data)
        wallets = get_user_saved_wallets(db_session, 88888)
        self.assertEqual(len(wallets), 1)
        self.assertEqual(wallets[0].wallet_address, "0x71C839556CB3250b716773B3aBE329a4a796c9c6")
        update.message.reply_text.assert_called_once()
        self.assertIn("Berhasil Disimpan", update.message.reply_text.call_args[1]["text"])

    async def test_handle_saved_account_text_input_bank(self):
        db_session = self.db_session
        update = MagicMock()
        update.effective_user.id = 99999
        update.message.text = "BCA, 882049281, Budi Santoso"
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.user_data = {"awaiting_save_bank": True}

        with patch("bot.handlers.saved_accounts.SessionLocal", return_value=db_session):
            handled = await handle_saved_account_text_input(update, context)

        self.assertTrue(handled)
        self.assertNotIn("awaiting_save_bank", context.user_data)
        banks = get_user_saved_banks(db_session, 99999)
        self.assertEqual(len(banks), 1)
        self.assertEqual(banks[0].bank_name, "BCA")
        self.assertEqual(banks[0].account_number, "882049281")
        self.assertEqual(banks[0].account_name, "BUDI SANTOSO")
        update.message.reply_text.assert_called_once()
        self.assertIn("Berhasil Disimpan", update.message.reply_text.call_args[1]["text"])


class TestBuyAndSellIntegration(BaseDBSessionTest, unittest.IsolatedAsyncioTestCase):
    async def test_buy_flow_saved_wallet_selection(self):
        db_session = self.db_session
        from bot.handlers.buy import handle_saved_wallet_selection, SELECT_PAYMENT

        user = create_user(db_session, telegram_id=123123)
        sw = save_user_wallet(
            db_session,
            telegram_id=user.telegram_id,
            wallet_address="0x71C839556CB3250b716773B3aBE329a4a796c9c6",
            network="BSC"
        )

        update = MagicMock()
        update.effective_user.id = user.telegram_id
        update.callback_query.data = f"buy_saved_wallet_{sw.id}"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        context = MagicMock()
        context.user_data = {
            "buy_network": "BSC",
            "buy_symbol": "USDT",
            "buy_total_idr": 100000,
            "buy_crypto_amount": 6.0,
        }

        with patch("bot.handlers.buy.SessionLocal", return_value=db_session), \
             patch("bot.handlers.buy.get_available_inventory", return_value=100.0), \
             patch("bot.handlers.buy.get_user_balance", return_value=50000.0):
            res = await handle_saved_wallet_selection(update, context)

        self.assertEqual(res, SELECT_PAYMENT)
        self.assertEqual(context.user_data["buy_wallet"], sw.wallet_address)
        update.callback_query.edit_message_text.assert_called_once()
        self.assertIn("PILIH METODE PEMBAYARAN", update.callback_query.edit_message_text.call_args[1]["text"])

    async def test_sell_flow_saved_bank_selection(self):
        db_session = self.db_session
        from bot.handlers.sell import handle_saved_bank_selection, CONFIRM_ORDER

        user = create_user(db_session, telegram_id=321321)
        sb = save_user_bank(
            db_session,
            telegram_id=user.telegram_id,
            bank_name="GOPAY",
            account_number="081234567890",
            account_name="Budi Santoso"
        )

        update = MagicMock()
        update.effective_user.id = user.telegram_id
        update.callback_query.data = f"sell_saved_bank_{sb.id}"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        context = MagicMock()
        context.user_data = {
            "sell_network": "BSC",
            "sell_symbol": "USDT",
            "sell_crypto_amount": 10.0,
            "sell_price_per_unit": 16000,
            "sell_fee_idr": 2000,
            "sell_net_idr": 158000,
        }

        with patch("bot.handlers.sell.SessionLocal", return_value=db_session):
            res = await handle_saved_bank_selection(update, context)

        self.assertEqual(res, CONFIRM_ORDER)
        self.assertEqual(context.user_data["sell_bank_name"], "GOPAY")
        self.assertEqual(context.user_data["sell_bank_acc"], "081234567890")
        self.assertEqual(context.user_data["sell_bank_holder"], "BUDI SANTOSO")
        update.callback_query.edit_message_text.assert_called_once()
        self.assertIn("Konfirmasi Jual", str(update.callback_query.edit_message_text.call_args))

    async def test_buy_flow_manual_wallet_retained_and_auto_saved(self):
        db_session = self.db_session
        from bot.handlers.buy import handle_wallet_input, SELECT_PAYMENT

        user = create_user(db_session, telegram_id=555111)
        user_id = user.telegram_id
        manual_addr = "0x71C839556CB3250b716773B3aBE329a4a796c9c6"

        update = MagicMock()
        update.effective_user.id = user_id
        update.callback_query = None
        update.message.text = manual_addr
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.user_data = {
            "buy_network": "BSC",
            "buy_symbol": "USDT",
            "buy_total_idr": 100000,
            "buy_crypto_amount": 6.0,
        }

        # Prevent closing db_session during test
        orig_close = db_session.close
        db_session.close = MagicMock()
        try:
            with patch("bot.handlers.buy.SessionLocal", return_value=db_session), \
                 patch("bot.handlers.buy.get_available_inventory", return_value=100.0), \
                 patch("bot.handlers.buy.get_user_balance", return_value=50000.0):
                res = await handle_wallet_input(update, context)

            self.assertEqual(res, SELECT_PAYMENT)
            self.assertEqual(context.user_data["buy_wallet"], manual_addr)
            update.message.reply_text.assert_called_once()
            self.assertIn("PILIH METODE PEMBAYARAN", update.message.reply_text.call_args[1]["text"])

            # Ensure auto-saved to DB for next time!
            saved = get_user_saved_wallets(db_session, user_id)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].wallet_address, manual_addr)
        finally:
            db_session.close = orig_close

    async def test_sell_flow_manual_bank_retained_and_auto_saved(self):
        db_session = self.db_session
        from bot.handlers.sell import handle_bank_input, CONFIRM_ORDER

        user = create_user(db_session, telegram_id=666222)
        user_id = user.telegram_id

        update = MagicMock()
        update.effective_user.id = user_id
        update.callback_query = None
        update.message.text = "Bank Mandiri, 137001234567, Siti Fatimah"
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.user_data = {
            "sell_network": "BSC",
            "sell_symbol": "USDT",
            "sell_crypto_amount": 10.0,
            "sell_price_per_unit": 16000,
            "sell_fee_idr": 2000,
            "sell_net_idr": 158000,
        }

        orig_close = db_session.close
        db_session.close = MagicMock()
        try:
            with patch("bot.handlers.sell.SessionLocal", return_value=db_session):
                res = await handle_bank_input(update, context)

            self.assertEqual(res, CONFIRM_ORDER)
            self.assertEqual(context.user_data["sell_bank_name"], "Bank Mandiri")
            self.assertEqual(context.user_data["sell_bank_acc"], "137001234567")
            self.assertEqual(context.user_data["sell_bank_holder"], "Siti Fatimah")
            update.message.reply_text.assert_called_once()
            self.assertIn("Konfirmasi Jual", str(update.message.reply_text.call_args))

            # Ensure auto-saved to DB
            banks = get_user_saved_banks(db_session, user_id)
            self.assertEqual(len(banks), 1)
            self.assertEqual(banks[0].bank_name, "BANK MANDIRI")
            self.assertEqual(banks[0].account_number, "137001234567")
        finally:
            db_session.close = orig_close

    async def test_buy_symbol_keyboard_has_wallet_button(self):
        from bot.keyboards.crypto_select import get_buy_symbol_keyboard
        kb = get_buy_symbol_keyboard()
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        wallet_btn = [btn for btn in buttons if btn.callback_data == "buy_saved_wallets"]
        self.assertEqual(len(wallet_btn), 1)
        self.assertIn("Alamat Wallet", wallet_btn[0].text)

    async def test_sell_symbol_keyboard_has_bank_button(self):
        from bot.keyboards.crypto_select import get_sell_symbol_keyboard
        kb = get_sell_symbol_keyboard()
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        bank_btn = [btn for btn in buttons if btn.callback_data == "sell_saved_banks"]
        self.assertEqual(len(bank_btn), 1)
        self.assertIn("Rekening Pencairan", bank_btn[0].text)

    async def test_buy_open_saved_wallets_flow(self):
        db_session = self.db_session
        from bot.handlers.buy import buy_open_saved_wallets, SELECT_SYMBOL

        update = MagicMock()
        update.effective_user.id = 777111
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        context = MagicMock()
        context.user_data = {}

        with patch("bot.handlers.saved_accounts.SessionLocal", return_value=db_session):
            res = await buy_open_saved_wallets(update, context)

        self.assertEqual(res, SELECT_SYMBOL)
        self.assertEqual(context.user_data.get("saved_wallets_back"), "buy_back_to_menu")
        update.callback_query.edit_message_text.assert_called_once()
        self.assertIn("Alamat Wallet", update.callback_query.edit_message_text.call_args[0][0])

    async def test_sell_open_saved_banks_flow(self):
        db_session = self.db_session
        from bot.handlers.sell import sell_open_saved_banks, SELECT_SYMBOL

        update = MagicMock()
        update.effective_user.id = 888222
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        context = MagicMock()
        context.user_data = {}

        with patch("bot.handlers.saved_accounts.SessionLocal", return_value=db_session):
            res = await sell_open_saved_banks(update, context)

        self.assertEqual(res, SELECT_SYMBOL)
        self.assertEqual(context.user_data.get("saved_banks_back"), "sell_back_to_menu")
        update.callback_query.edit_message_text.assert_called_once()
        self.assertIn("Rekening Pencairan", update.callback_query.edit_message_text.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
