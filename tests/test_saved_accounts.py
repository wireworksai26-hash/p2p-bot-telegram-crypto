import os
import sys
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

import pytest
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


@pytest.fixture
def db_session():
    """Isolated SQLite in-memory database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


class TestSavedWalletCRUD:
    def test_save_and_get_user_wallet(self, db_session):
        user = create_user(db_session, telegram_id=11111, username="alice", full_name="Alice")
        
        # Save EVM wallet
        w1 = save_user_wallet(
            db_session,
            telegram_id=user.telegram_id,
            wallet_address="0x71C839556CB3250b716773B3aBE329a4a796c9c6",
            network="BSC",
            label="Metamask Utama"
        )
        assert w1.id is not None
        assert w1.network == "BSC"
        assert w1.label == "Metamask Utama"

        # Get all
        all_wallets = get_user_saved_wallets(db_session, user.telegram_id)
        assert len(all_wallets) == 1
        assert all_wallets[0].wallet_address == "0x71C839556CB3250b716773B3aBE329a4a796c9c6"

        # Filter per network
        bsc_wallets = get_user_saved_wallets(db_session, user.telegram_id, network="BSC")
        assert len(bsc_wallets) == 1

        tron_wallets = get_user_saved_wallets(db_session, user.telegram_id, network="TRON")
        assert len(tron_wallets) == 0

    def test_save_wallet_duplicate_updates(self, db_session):
        user = create_user(db_session, telegram_id=22222)
        addr = "0x71C839556CB3250b716773B3aBE329a4a796c9c6"
        
        w1 = save_user_wallet(db_session, user.telegram_id, addr, network="BSC", label="Old Label")
        w2 = save_user_wallet(db_session, user.telegram_id, addr, network="ETH", label="New Label")
        
        assert w1.id == w2.id
        assert w2.label == "New Label"
        assert w2.network == "ETH"
        assert len(get_user_saved_wallets(db_session, user.telegram_id)) == 1

    def test_delete_user_saved_wallet(self, db_session):
        user = create_user(db_session, telegram_id=33333)
        addr = "0x71C839556CB3250b716773B3aBE329a4a796c9c6"
        w = save_user_wallet(db_session, user.telegram_id, addr, network="BSC")
        
        # Delete with wrong user returns False
        assert delete_user_saved_wallet(db_session, w.id, telegram_id=99999) is False
        assert get_saved_wallet_by_id(db_session, w.id) is not None

        # Delete with correct user returns True
        assert delete_user_saved_wallet(db_session, w.id, telegram_id=user.telegram_id) is True
        assert get_saved_wallet_by_id(db_session, w.id) is None


class TestSavedBankCRUD:
    def test_detect_account_type(self):
        assert detect_account_type("BCA") == "BANK"
        assert detect_account_type("Bank Mandiri") == "BANK"
        assert detect_account_type("BRI") == "BANK"
        assert detect_account_type("GOPAY") == "EWALLET"
        assert detect_account_type("Go-Pay") == "EWALLET"
        assert detect_account_type("DANA") == "EWALLET"
        assert detect_account_type("OVO") == "EWALLET"
        assert detect_account_type("SHOPEEPAY") == "EWALLET"

    def test_save_and_get_user_bank_and_ewallet(self, db_session):
        user = create_user(db_session, telegram_id=44444)

        b1 = save_user_bank(
            db_session,
            telegram_id=user.telegram_id,
            bank_name="BCA",
            account_number="882049281",
            account_name="Budi Santoso"
        )
        assert b1.account_type == "BANK"
        assert b1.bank_name == "BCA"

        b2 = save_user_bank(
            db_session,
            telegram_id=user.telegram_id,
            bank_name="GOPAY",
            account_number="081234567890",
            account_name="Budi Santoso"
        )
        assert b2.account_type == "EWALLET"
        assert b2.bank_name == "GOPAY"

        banks = get_user_saved_banks(db_session, user.telegram_id)
        assert len(banks) == 2

    def test_delete_user_saved_bank(self, db_session):
        user = create_user(db_session, telegram_id=55555)
        b = save_user_bank(db_session, user.telegram_id, "BCA", "12345678", "Budi")

        assert delete_user_saved_bank(db_session, b.id, telegram_id=99999) is False
        assert delete_user_saved_bank(db_session, b.id, telegram_id=user.telegram_id) is True
        assert get_saved_bank_by_id(db_session, b.id) is None


class TestSavedAccountsUIViews:
    def test_build_saved_wallets_view_empty_and_populated(self, db_session):
        user = create_user(db_session, telegram_id=66666)

        # Empty view matches Screenshot 1 text
        text_empty, markup_empty = build_saved_wallets_view(user.telegram_id, db_session)
        assert "Belum ada alamat tersimpan." in text_empty
        assert "Pilih tombol di bawah untuk menambahkan / mengubah Addres." in text_empty
        assert any(btn.text == "📌 Tambah / Simpan Addres" for row in markup_empty.inline_keyboard for btn in row)

        # Add wallet
        save_user_wallet(db_session, user.telegram_id, "0x71C839556CB3250b716773B3aBE329a4a796c9c6", "BSC")
        text_pop, markup_pop = build_saved_wallets_view(user.telegram_id, db_session)
        assert "Daftar Alamat Tersimpan:" in text_pop
        assert "0x71C839556CB3250b716773B3aBE329a4a796c9c6" in text_pop
        assert any(btn.text == "🗑 Hapus Alamat" for row in markup_pop.inline_keyboard for btn in row)

    def test_build_saved_banks_view_empty_and_populated(self, db_session):
        user = create_user(db_session, telegram_id=77777)

        # Empty view matches Screenshot 3 text
        text_empty, markup_empty = build_saved_banks_view(user.telegram_id, db_session)
        assert "Belum ada rekening tersimpan" in text_empty
        assert "Pilih tombol dibawah untuk menambah atau ganti rekening." in text_empty
        assert any(btn.text == "✍️ Tambah / Ganti Rekening" for row in markup_empty.inline_keyboard for btn in row)

        # Add bank & ewallet
        save_user_bank(db_session, user.telegram_id, "BCA", "882049281", "Budi Santoso")
        save_user_bank(db_session, user.telegram_id, "GOPAY", "081234567890", "Budi Santoso")
        text_pop, markup_pop = build_saved_banks_view(user.telegram_id, db_session)
        assert "Daftar Rekening / E-Wallet Tersimpan:" in text_pop
        assert "BCA" in text_pop
        assert "GOPAY" in text_pop
        assert any(btn.text == "🗑 Hapus Rekening" for row in markup_pop.inline_keyboard for btn in row)


@pytest.mark.asyncio
class TestInteractiveSaveHandlers:
    async def test_handle_saved_account_text_input_wallet(self, db_session):
        update = MagicMock()
        update.effective_user.id = 88888
        update.message.text = "0x71C839556CB3250b716773B3aBE329a4a796c9c6"
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.user_data = {"awaiting_save_wallet": True}

        with patch("bot.handlers.saved_accounts.SessionLocal", return_value=db_session):
            handled = await handle_saved_account_text_input(update, context)

        assert handled is True
        assert "awaiting_save_wallet" not in context.user_data
        wallets = get_user_saved_wallets(db_session, 88888)
        assert len(wallets) == 1
        assert wallets[0].wallet_address == "0x71C839556CB3250b716773B3aBE329a4a796c9c6"
        update.message.reply_text.assert_called_once()
        assert "Berhasil Disimpan" in update.message.reply_text.call_args[1]["text"]

    async def test_handle_saved_account_text_input_bank(self, db_session):
        update = MagicMock()
        update.effective_user.id = 99999
        update.message.text = "BCA, 882049281, Budi Santoso"
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.user_data = {"awaiting_save_bank": True}

        with patch("bot.handlers.saved_accounts.SessionLocal", return_value=db_session):
            handled = await handle_saved_account_text_input(update, context)

        assert handled is True
        assert "awaiting_save_bank" not in context.user_data
        banks = get_user_saved_banks(db_session, 99999)
        assert len(banks) == 1
        assert banks[0].bank_name == "BCA"
        assert banks[0].account_number == "882049281"
        assert banks[0].account_name == "BUDI SANTOSO"
        update.message.reply_text.assert_called_once()
        assert "Berhasil Disimpan" in update.message.reply_text.call_args[1]["text"]


@pytest.mark.asyncio
class TestBuyAndSellIntegration:
    async def test_buy_flow_saved_wallet_selection(self, db_session):
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

        assert res == SELECT_PAYMENT
        assert context.user_data["buy_wallet"] == sw.wallet_address
        update.callback_query.edit_message_text.assert_called_once()
        assert "PILIH METODE PEMBAYARAN" in update.callback_query.edit_message_text.call_args[1]["text"]

    async def test_sell_flow_saved_bank_selection(self, db_session):
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

        assert res == CONFIRM_ORDER
        assert context.user_data["sell_bank_name"] == "GOPAY"
        assert context.user_data["sell_bank_acc"] == "081234567890"
        assert context.user_data["sell_bank_holder"] == "BUDI SANTOSO"
        update.callback_query.edit_message_text.assert_called_once()
        assert "Konfirmasi Jual" in str(update.callback_query.edit_message_text.call_args)

    async def test_buy_flow_manual_wallet_retained_and_auto_saved(self, db_session):
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

            assert res == SELECT_PAYMENT
            assert context.user_data["buy_wallet"] == manual_addr
            update.message.reply_text.assert_called_once()
            assert "PILIH METODE PEMBAYARAN" in update.message.reply_text.call_args[1]["text"]

            # Ensure auto-saved to DB for next time!
            saved = get_user_saved_wallets(db_session, user_id)
            assert len(saved) == 1
            assert saved[0].wallet_address == manual_addr
        finally:
            db_session.close = orig_close

    async def test_sell_flow_manual_bank_retained_and_auto_saved(self, db_session):
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

            assert res == CONFIRM_ORDER
            assert context.user_data["sell_bank_name"] == "Bank Mandiri"
            assert context.user_data["sell_bank_acc"] == "137001234567"
            assert context.user_data["sell_bank_holder"] == "Siti Fatimah"
            update.message.reply_text.assert_called_once()
            assert "Konfirmasi Jual" in str(update.message.reply_text.call_args)

            # Ensure auto-saved to DB
            banks = get_user_saved_banks(db_session, user_id)
            assert len(banks) == 1
            assert banks[0].bank_name == "BANK MANDIRI"
            assert banks[0].account_number == "137001234567"
        finally:
            db_session.close = orig_close

