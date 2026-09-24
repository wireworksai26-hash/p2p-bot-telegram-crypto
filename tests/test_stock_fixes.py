"""Offline regression tests. Never load production .env, start a bot or send funds.

Run: python -m unittest discover -s tests -v
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))
os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1", "DATABASE_URL": "sqlite:///:memory:",
    "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY", "ADMIN_CHAT_IDS": "123456",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40, "EVM_PRIVATE_KEY": "",
})

import httpx
from sqlalchemy import create_engine, inspect
from database.connection import Base, engine, SessionLocal
from database import crud
from database.models import WalletBalance
from services.crypto_sender.solana_sender import SolanaSender
from services.crypto_sender.aptos_sender import AptosSender
from services.crypto_sender.sui_sender import SuiSender
from services.crypto_sender.evm_sender import EVMSender
from services.crypto_sender.ton_sender import TonSender
from services.crypto_sender.tron_sender import TronSender
from services.payout_service import send_crypto_with_retry
from services import wallet_sync
from bot.handlers import stocks
from bot.handlers.stocks import build_stock_pages, format_crypto_qty, show_stocks
from bot.utils import emojis
from bot.utils.formatter import format_datetime
from config.assets import STOCK_ASSETS

REAL_ASYNC_CLIENT = httpx.AsyncClient

# Pubkey valid (wrapped SOL mint) hanya untuk derivasi ATA di tes, bukan wallet siapa pun.
SYSTEM_PUBKEY = "So11111111111111111111111111111111111111112"


def mock_http(handler):
    transport = httpx.MockTransport(handler)
    return patch("httpx.AsyncClient", side_effect=lambda **kw: REAL_ASYNC_CLIENT(transport=transport))


class Readers(unittest.IsolatedAsyncioTestCase):
    async def test_sol_balance_without_signing_sdk(self):
        sender = SolanaSender()
        sender.wallet_address = "PublicAddress"
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["method"], "getBalance")
            self.assertEqual(body["params"][0], "PublicAddress")
            return httpx.Response(200, json={"result": {"value": 25700000}})
        with patch("services.crypto_sender.solana_sender.SOLANA_LIB_AVAILABLE", False), mock_http(handler):
            self.assertEqual(await sender.get_balance("SOL"), 0.0257)

    async def test_sol_http_200_rpc_error_tries_next_endpoint(self):
        sender = SolanaSender()
        sender.wallet_address = SYSTEM_PUBKEY
        sender.rpc_list = ["https://bad.invalid", "https://good.invalid"]
        seen = []
        def handler(request):
            seen.append(request.url.host)
            if request.url.host == "bad.invalid":
                return httpx.Response(200, json={"error": {"code": -32005}})
            if "api.mainnet-beta" in request.url.host:
                return httpx.Response(200, json={"result": {"value": []}})
            return httpx.Response(200, json={"result": {"value": None}})
        with mock_http(handler):
            self.assertEqual(await sender.get_balance("USDT"), 0)
        self.assertEqual(seen[:2], ["bad.invalid", "good.invalid"])

    async def test_spl_reads_ata_amount_without_indexed_query(self):
        from services.crypto_sender.solana_sender import SPL_TOKENS

        sender = SolanaSender()
        sender.wallet_address = SYSTEM_PUBKEY
        methods = []
        def handler(request):
            body = json.loads(request.content)
            methods.append(body["method"])
            return httpx.Response(200, json={"result": {"value": {"data": {"parsed": {"info": {
                "mint": SPL_TOKENS["USDC"],
                "tokenAmount": {"amount": "1250000", "decimals": 6, "uiAmount": None},
            }}}}}})
        with mock_http(handler):
            self.assertEqual(await sender.get_balance("USDC"), 1.25)
        self.assertEqual(methods, ["getAccountInfo"])

    async def test_spl_missing_ata_falls_back_to_mint_crosscheck(self):
        sender = SolanaSender()
        sender.wallet_address = SYSTEM_PUBKEY
        def handler(request):
            body = json.loads(request.content)
            if body["method"] == "getAccountInfo":
                return httpx.Response(200, json={"result": {"value": None}})
            info = {"tokenAmount": {"amount": "2000000", "decimals": 6}}
            account = {"account": {"data": {"parsed": {"info": info}}}}
            return httpx.Response(200, json={"result": {"value": [account]}})
        with mock_http(handler):
            self.assertEqual(await sender.get_balance("USDT"), 2)

    async def test_spl_wrong_mint_is_error_not_zero(self):
        sender = SolanaSender()
        sender.wallet_address = SYSTEM_PUBKEY
        def handler(request):
            return httpx.Response(200, json={"result": {"value": {"data": {"parsed": {"info": {
                "mint": SYSTEM_PUBKEY,
                "tokenAmount": {"amount": "1000", "decimals": 6},
            }}}}}})
        with mock_http(handler):
            with self.assertRaises(RuntimeError):
                await sender.get_balance("USDC")

    async def test_spl_malformed_response_is_not_zero(self):
        sender = SolanaSender()
        sender.wallet_address = "PublicAddress"
        with mock_http(lambda req: httpx.Response(200, json={"result": {}})):
            with self.assertRaises(RuntimeError):
                await sender.get_balance("USDT")

    async def test_sol_http_failure_is_not_zero(self):
        sender = SolanaSender()
        sender.wallet_address = "PublicAddress"
        with mock_http(lambda req: httpx.Response(503)):
            with self.assertRaises(RuntimeError):
                await sender.get_balance("SOL")

    async def test_aptos_reads_coin_and_fungible_asset_view(self):
        sender = AptosSender()
        sender.wallet_address = "0x1"
        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertTrue(request.url.path.endswith("/view"))
            self.assertEqual(json.loads(request.content), {
                "function": "0x1::coin::balance",
                "type_arguments": ["0x1::aptos_coin::AptosCoin"], "arguments": ["0x1"],
            })
            return httpx.Response(200, json=["327000000"])
        with mock_http(handler):
            self.assertEqual(await sender.get_balance("APT"), 3.27)

    async def test_aptos_zero_is_valid_but_failed_view_is_not(self):
        sender = AptosSender()
        sender.wallet_address = "0x1"
        with mock_http(lambda req: httpx.Response(200, json=["0"])):
            self.assertEqual(await sender.get_balance("APT"), 0)
        with mock_http(lambda req: httpx.Response(404, json={"error_code": "resource_not_found"})):
            with self.assertRaises(RuntimeError):
                await sender.get_balance("APT")

    async def test_missing_wallets_never_report_confirmed_zero(self):
        for sender in (SolanaSender(), AptosSender(), SuiSender(), TonSender(), TronSender()):
            sender.wallet_address = ""
            with self.subTest(sender=type(sender).__name__), self.assertRaises(RuntimeError):
                await sender.get_balance()

    async def test_sui_bad_response_is_not_zero(self):
        sender = SuiSender()
        sender.wallet_address = "0x1"
        with mock_http(lambda req: httpx.Response(200, json={"error": {"code": -1}})):
            with self.assertRaises(RuntimeError):
                await sender.get_balance("SUI")

    async def test_ton_rpc_failure_is_not_zero(self):
        sender = TonSender()
        sender.wallet_address = "public"
        sender._tonapi_get = AsyncMock(side_effect=RuntimeError("tonapi down"))
        sender._rpc = AsyncMock(return_value=None)
        with self.assertRaises(RuntimeError):
            await sender.get_balance("TON")
        sender._rpc.return_value = {"balance": "0"}
        self.assertEqual(await sender.get_balance("TON"), 0)

    async def test_tron_http_failure_is_not_zero(self):
        sender = TronSender()
        sender.wallet_address = "PublicAddress"
        with patch("services.crypto_sender.tron_sender.TRONPY_AVAILABLE", False), mock_http(lambda req: httpx.Response(429)):
            with self.assertRaises(RuntimeError):
                await sender.get_balance("TRX")

    async def test_evm_error_is_not_zero_and_robinhood_checks_chain(self):
        sender = EVMSender("ROBINHOOD")
        sender.rpc_list = ["https://example.invalid"]
        eth = SimpleNamespace(chain_id=1337, get_balance=Mock(return_value=1))
        sender.w3 = SimpleNamespace(eth=eth)
        with self.assertRaises(RuntimeError):
            await sender.get_balance("ETH")
        eth.get_balance.assert_not_called()
        eth.chain_id = 4663
        eth.get_balance.side_effect = RuntimeError("RPC down")
        with self.assertRaises(RuntimeError):
            await sender.get_balance("ETH")

    async def test_manual_sui_aptos_never_success_or_retry(self):
        for sender in (SuiSender(), AptosSender()):
            sender.send = AsyncMock(wraps=sender.send)
            result = await send_crypto_with_retry(sender, "0x" + "1" * 64, 1, "APT")
            self.assertFalse(result["success"])
            self.assertEqual(result["tx_hash"], "")
            sender.send.assert_awaited_once()


class Inventory(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(engine)

    def test_error_preserves_history_but_blocks_inventory_then_recovers(self):
        wallet = crud.update_wallet_balance(self.db, "SOLANA", 6, "USDC", "address")
        stamp = wallet.last_success_at
        self.assertTrue(crud.reserve_order_inventory(self.db, "ORDER1", "SOLANA", "USDC", Decimal(5)))
        self.assertEqual(crud.get_available_inventory(self.db, "SOLANA", "USDC"), 1)
        crud.mark_wallet_balance_error(self.db, "SOLANA", "USDC", "down", "address")
        self.assertEqual(wallet.balance, 6)
        self.assertEqual(wallet.last_success_at, stamp)
        self.assertIsNone(crud.get_available_inventory(self.db, "SOLANA", "USDC"))
        self.assertFalse(crud.reserve_order_inventory(self.db, "ORDER1", "SOLANA", "USDC", Decimal(5)))
        self.assertFalse(crud.reserve_order_inventory(self.db, "ORDER2", "SOLANA", "USDC", Decimal(1)))
        crud.update_wallet_balance(self.db, "SOLANA", 6, "USDC", "address")
        self.assertTrue(crud.reserve_order_inventory(self.db, "ORDER1", "SOLANA", "USDC", Decimal(5)))
        self.assertTrue(crud.release_order_inventory(self.db, "ORDER1"))
        self.assertEqual(crud.get_available_inventory(self.db, "SOLANA", "USDC"), 6)

    def test_stale_success_blocks_new_and_existing_reservations(self):
        wallet = crud.update_wallet_balance(self.db, "SOLANA", 6, "SOL", "address")
        self.assertTrue(crud.reserve_order_inventory(self.db, "ORDER1", "SOLANA", "SOL", Decimal(1)))
        wallet.last_success_at = datetime.utcnow() - timedelta(minutes=16)
        self.db.commit()
        self.assertIsNone(crud.get_available_inventory(self.db, "SOLANA", "SOL"))
        self.assertFalse(crud.reserve_order_inventory(self.db, "ORDER1", "SOLANA", "SOL", Decimal(1)))

    def test_failed_first_read_and_changed_address_do_not_relabel_history(self):
        wallet = crud.mark_wallet_balance_error(self.db, "APTOS", "APT", "down", "old")
        self.assertIsNone(wallet.last_success_at)
        self.assertEqual(wallet.sync_status, "ERROR")
        crud.update_wallet_balance(self.db, "APTOS", 3.27, "APT", "old")
        crud.mark_wallet_balance_error(self.db, "APTOS", "APT", "down", "new")
        self.assertIsNone(wallet.last_success_at)
        self.assertEqual(wallet.balance, 0)

    def test_successful_zero_is_fresh(self):
        wallet = crud.update_wallet_balance(self.db, "APTOS", 0, "APT", "address")
        self.assertTrue(crud.wallet_balance_is_fresh(wallet))
        self.assertEqual(wallet.balance, 0)


class SyncIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_stock_handler_navigates_without_resyncing_or_truncating(self):
        now = datetime.utcnow()
        # Error/history messages are deliberately long enough to need pagination.
        rows = [SimpleNamespace(network=net, symbol=sym, balance=1, sync_status="ERROR",
                                last_success_at=now - timedelta(hours=1)) for sym, net in STOCK_ASSETS]
        query = SimpleNamespace(data="menu_stocks_page_1", edit_message_text=AsyncMock(),
                                message=SimpleNamespace(reply_text=AsyncMock()))
        with patch("bot.handlers.stocks.SessionLocal") as session, \
             patch("bot.handlers.stocks.get_all_wallet_balances", return_value=rows), \
             patch("bot.handlers.stocks.fetch_usd_prices", new=AsyncMock(return_value={})), \
             patch.object(wallet_sync, "sync_wallet_balances", new=AsyncMock()) as sync:
            await show_stocks(SimpleNamespace(callback_query=query), None)
        sync.assert_not_awaited()
        session.return_value.close.assert_called_once()
        query.message.reply_text.assert_not_awaited()
        sent = query.edit_message_text.await_args.kwargs
        self.assertIn("Halaman 2/", sent["text"])
        self.assertLess(len(sent["text"].encode("utf-16-le")) // 2, 4096)
        buttons = [b for row in sent["reply_markup"].inline_keyboard for b in row]
        self.assertIn("menu_stocks_page_0", [b.callback_data for b in buttons])

    async def test_sync_persists_error_separately_and_menu_renders_it(self):
        Base.metadata.create_all(engine)
        senders = {
            "SOLANA": SimpleNamespace(wallet_address="sol", get_balance=AsyncMock(return_value=0.0257)),
            "APTOS": SimpleNamespace(wallet_address="apt", get_balance=AsyncMock(return_value=3.27)),
            "ROBINHOOD": SimpleNamespace(wallet_address="evm", get_balance=AsyncMock(side_effect=RuntimeError("https://rpc/key-SECRET"))),
        }
        pairs = [("SOL", "SOLANA"), ("APT", "APTOS"), ("ETH", "ROBINHOOD")]
        try:
            with patch.object(wallet_sync, "STOCK_ASSETS", pairs), patch.object(wallet_sync.CryptoSenderFactory, "get_sender", side_effect=senders.get):
                report = await wallet_sync.sync_wallet_balances()
            self.assertEqual(len(report["success"]), 2)
            self.assertEqual(report["failed"], ["ETH (ROBINHOOD)"])
            db = SessionLocal()
            try:
                rows = crud.get_all_wallet_balances(db)
                failed = next(w for w in rows if w.network == "ROBINHOOD")
                self.assertNotIn("SECRET", failed.last_error)
                text = "\n".join(build_stock_pages(rows, {}))
                self.assertIn("0.0257 SOL", text)
                self.assertIn("3.27 APT", text)
                self.assertIn("Gagal membaca saldo", text)
                self.assertNotIn("(Kosong)", text)
                # Label "pengiriman admin" hanya untuk jaringan di emergency brake.
                self.assertNotIn("pengiriman admin", text)
                with patch.object(stocks, "MANUAL_PAYOUT_NETWORKS", {"APTOS"}):
                    manual_text = "\n".join(build_stock_pages(rows, {}))
                self.assertIn("pengiriman admin", manual_text)
            finally:
                db.close()
        finally:
            Base.metadata.drop_all(engine)


class Presentation(unittest.TestCase):
    def test_timestamp_reflects_balance_read_not_menu_open_time(self):
        now = datetime(2026, 9, 17, 12, 0)
        stamp = now - timedelta(minutes=5)
        row = SimpleNamespace(network="SOLANA", symbol="SOL", balance=0.0257,
                              sync_status="OK", last_success_at=stamp)
        page = build_stock_pages([row], {}, now)[0]
        self.assertIn(format_datetime(stamp), page)
        self.assertNotIn(format_datetime(now), page)

    def test_all_stock_rows_paginate_with_complete_html(self):
        now = datetime.utcnow()
        rows = [SimpleNamespace(network=net, symbol=sym, balance=1, sync_status="ERROR",
                                last_success_at=now - timedelta(hours=1)) for sym, net in STOCK_ASSETS]
        pages = build_stock_pages(rows, {}, now)
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLess(len(page.encode("utf-16-le")) // 2, 4096)
            self.assertEqual(page.count("<b>"), page.count("</b>"))
            self.assertEqual(page.count("<i>"), page.count("</i>"))
        text = "\n".join(pages)
        self.assertEqual(text.count("Data lama:"), len(STOCK_ASSETS))
        self.assertNotIn("(Kosong)", text)

    def test_small_nonzero_balance_does_not_round_to_zero(self):
        self.assertNotEqual(format_crypto_qty(0.000000001, "SOL"), "0 SOL")
        self.assertEqual(format_crypto_qty(0, "SOL"), "0 SOL")

    def test_aset_punya_logo_asli_bukan_koin_generik(self):
        simbol = ("USDT", "USDC", "USDG", "ETH", "SOL", "TRX", "BNB", "SUI", "TON",
                  "POL", "ARB", "AVAX", "KAIA", "BERA", "APT", "HYPE")
        for symbol in simbol:
            self.assertTrue(emojis.get_coin_emoji_id(symbol), f"{symbol} belum punya logo asli")
            self.assertIn("emoji-id=", emojis.get_coin_emoji(symbol), symbol)
            self.assertNotIn("🪙", emojis.coin_button_text(symbol), symbol)

    def test_saved_legacy_emoji_migration_preserves_new_custom_choices(self):
        initial_ids, initial_alts = dict(emojis.CUSTOM_EMOJI_IDS), dict(emojis.CUSTOM_EMOJI_ALTS)
        payload = {"ids": {"COIN_ETH": "5309958691854754293", "COIN_APT": "1234567890123456789"}, "alts": {}}
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "emojis.json"
                path.write_text(json.dumps(payload), encoding="utf8")
                with patch.object(emojis, "CUSTOM_EMOJIS_FILE", str(path)):
                    emojis.load_custom_emojis()
                self.assertNotEqual(emojis.get_coin_emoji_id("ETH"), "5309958691854754293")
                self.assertEqual(emojis.get_coin_emoji_id("APT"), "1234567890123456789")
        finally:
            emojis.CUSTOM_EMOJI_IDS.clear()
            emojis.CUSTOM_EMOJI_IDS.update(initial_ids)
            emojis.CUSTOM_EMOJI_ALTS.clear()
            emojis.CUSTOM_EMOJI_ALTS.update(initial_alts)


class Migration(unittest.TestCase):
    def test_legacy_wallet_schema_migration_is_repeatable_and_preserves_balance(self):
        import main
        legacy = create_engine("sqlite:///:memory:")
        try:
            with legacy.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE wallet_balances (id INTEGER PRIMARY KEY, network TEXT, symbol TEXT, balance NUMERIC, address TEXT, updated_at TIMESTAMP)")
                conn.exec_driver_sql("INSERT INTO wallet_balances VALUES (1, 'SOLANA', 'SOL', 0.0257, 'public', CURRENT_TIMESTAMP)")
            with patch("database.connection.engine", legacy):
                main._migrate_inventory_schema()
                main._migrate_inventory_schema()
            columns = {c["name"] for c in inspect(legacy).get_columns("wallet_balances")}
            self.assertTrue({"sync_status", "last_error", "last_checked_at", "last_success_at", "reserved_balance"} <= columns)
            with legacy.connect() as conn:
                row = conn.exec_driver_sql("SELECT balance, sync_status, last_success_at FROM wallet_balances").one()
                self.assertEqual(float(row[0]), 0.0257)
                self.assertEqual(row[1], "UNKNOWN")
                self.assertIsNone(row[2])
        finally:
            legacy.dispose()


if __name__ == "__main__":
    unittest.main(verbosity=2)
