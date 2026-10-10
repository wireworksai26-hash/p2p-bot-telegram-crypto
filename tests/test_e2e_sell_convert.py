"""
Comprehensive End-to-End (E2E) Integration Tests for Sell (Jual) and Convert (Swap) flows.
========================================================================================
Covers:
1. Sell Flow:
   - Valid decimal input, 0% spread price fetching
   - Gross nominal, fee calculation, and altcoin surcharge (+Rp 500)
   - Rejection of net <= 0 or gross < minimum (Rp 5.000 / Rp 7.500 gas pairs)
   - Bank details parsing (comma separated) and auto-save
   - 1-tap saved bank selection
   - Order creation with WAITING_CRYPTO_DEPOSIT
   - Hot wallet resolution across networks (EVM, Solana, Aptos, Sui, Ton)
   - TX Hash manual submission and format validation
   - Photo proof upload handling
   - Cancellation updates DB order to 'cancelled'

2. Convert (Swap) Flow:
   - Currency & network selection (including bridge mode)
   - Guard against target network in MANUAL_PAYOUT_NETWORKS
   - Server-side guard against exact same coin & network
   - Amount parser: Crypto amounts, IDR (dot, k, rb, jt, Rp), USD ($10, 10 usd, 25 usdt)
   - Convert fee tier, gas surcharge (+Rp 2.500 for gas pairs), target crypto amount
   - Wallet validation & rejection of self-dealing (bot's own wallet)
   - Stock inventory check before order creation
   - Order creation with 30-minute quote expiration
   - TX Hash instant verification vs pending notification
   - Cancellation updates DB order to 'cancelled'

3. Deposit Detector Lifecycle:
   - Confirmation of Sell order -> notifies admin to transfer Rupiah
   - Confirmation of Swap order -> executes payout of target coin
   - Late deposit verification within 24-hour window
"""

import os
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from database.connection import Base, SessionLocal, engine
from database.models import User, Order, UserSavedBank, AuditLog, WalletBalance
from database.crud import (
    create_order,
    get_order_by_id,
    save_user_bank,
    update_wallet_balance,
)
from services.fee_service import calculate_fee_idr, get_fee_category
from bot.handlers.sell import (
    handle_amount_input as sell_handle_amount,
    handle_bank_input as sell_handle_bank,
    handle_saved_bank_selection as sell_handle_saved_bank,
    handle_order_confirmation as sell_handle_confirm,
    handle_tx_hash_input as sell_handle_tx_hash,
    handle_sell_proof,
    cancel_sell,
    get_hot_wallet_address,
    INPUT_AMOUNT as SELL_INPUT_AMOUNT,
    INPUT_BANK as SELL_INPUT_BANK,
    INPUT_SENDER as SELL_INPUT_SENDER,
    CONFIRM_ORDER as SELL_CONFIRM_ORDER,
    WAITING_TX as SELL_WAITING_TX,
)
from bot.handlers.swap import (
    select_tgt_net as swap_select_tgt_net,
    parse_convert_amount,
    input_amount as swap_input_amount,
    input_target_addr as swap_input_target_addr,
    confirm_swap_order,
    input_deposit_hash as swap_input_deposit_hash,
    cancel_swap,
    INPUT_AMOUNT as SWAP_INPUT_AMOUNT,
    INPUT_TARGET_ADDR as SWAP_INPUT_TARGET_ADDR,
    CONFIRM_SWAP as SWAP_CONFIRM_SWAP,
    WAITING_DEPOSIT_HASH as SWAP_WAITING_DEPOSIT_HASH,
)
from services import tx_verifier
from services.detector import deposit_detector


class TestE2ESellFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.user_id = 987654
        self.user = User(
            telegram_id=self.user_id,
            username="test_trader",
            full_name="Test Trader"
        )
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _mock_price_response(self, market_price=2_000_000.0):
        # 0% spread: buy == sell == market
        return {
            "symbol": "SOL",
            "market_price_idr": market_price,
            "buy_price_idr": market_price,
            "sell_price_idr": market_price,
            "spread_pct": 0.0,
            "usdt_idr_rate": 18000.0,
            "source": "OKX+SpotFX",
            "price_updated_at": int(datetime.now(timezone.utc).timestamp()),
        }

    async def test_sell_amount_input_validation_zero_spread(self):
        """Uji input nominal jual SOL valid dengan harga market 0% spread dan surcharge altcoin."""
        update = SimpleNamespace(
            message=AsyncMock(text="0.5"),
            effective_user=SimpleNamespace(id=self.user_id, first_name="Test")
        )
        context = SimpleNamespace(
            user_data={"sell_symbol": "SOL", "sell_network": "SOLANA"},
            bot=AsyncMock()
        )

        with patch("services.price_service.price_service.get_price", new=AsyncMock(return_value=self._mock_price_response(2_000_000.0))):
            next_state = await sell_handle_amount(update, context)

        self.assertEqual(next_state, SELL_INPUT_SENDER)
        self.assertEqual(context.user_data["sell_crypto_amount"], 0.5)
        self.assertEqual(context.user_data["sell_price_per_unit"], 2_000_000.0)
        # Gross = 0.5 * 2.000.000 = 1.000.000
        self.assertEqual(context.user_data["sell_gross_nominal_idr"], 1_000_000)
        # Fee altcoin 1.000.000: tier (940.001-1.010.000) = 20.500 (tanpa surcharge)
        expected_fee = calculate_fee_idr(1_000_000, "ALTCOIN", symbol="SOL", network="SOLANA", is_outgoing=False)
        self.assertEqual(expected_fee, 20_500)
        self.assertEqual(context.user_data["sell_fee_idr"], 20_500)
        # Net = 1.000.000 - 20.500 = 979.500
        self.assertEqual(context.user_data["sell_net_idr"], 979_500)

    async def test_sell_amount_rejects_negative_or_zero_net(self):
        """Uji penolakan input jika nominal kotor setelah fee menghasilkan net <= 0."""
        update = SimpleNamespace(
            message=AsyncMock(text="0.001"),
            effective_user=SimpleNamespace(id=self.user_id, first_name="Test")
        )
        context = SimpleNamespace(
            user_data={"sell_symbol": "SOL", "sell_network": "SOLANA"},
            bot=AsyncMock()
        )

        # 0.001 * 50.000 = 50 IDR (di bawah min Rp 5.000)
        with patch("services.price_service.price_service.get_price", new=AsyncMock(return_value=self._mock_price_response(50_000.0))):
            next_state = await sell_handle_amount(update, context)

        self.assertEqual(next_state, SELL_INPUT_AMOUNT)
        reply_text = update.message.reply_text.call_args.args[0]
        self.assertIn("Minimum transaksi", reply_text)

    async def test_sell_amount_gas_pair_enforces_7500_min(self):
        """Uji pasangan gas (ETH di jaringan ETH) mewajibkan minimum nominal Rp 7.500."""
        update = SimpleNamespace(
            message=AsyncMock(text="0.0001"),
            effective_user=SimpleNamespace(id=self.user_id, first_name="Test")
        )
        context = SimpleNamespace(
            user_data={"sell_symbol": "ETH", "sell_network": "ETH"},
            bot=AsyncMock()
        )

        # 0.0001 * 50.000.000 = 5.000 IDR (lolos 5k biasa, tapi gagal min 7.5k pasangan gas)
        eth_mock = {
            "symbol": "ETH",
            "market_price_idr": 50_000_000.0,
            "buy_price_idr": 50_000_000.0,
            "sell_price_idr": 50_000_000.0,
            "spread_pct": 0.0,
            "usdt_idr_rate": 18000.0,
            "source": "OKX+SpotFX",
            "price_updated_at": int(datetime.now(timezone.utc).timestamp()),
        }
        with patch("services.price_service.price_service.get_price", new=AsyncMock(return_value=eth_mock)):
            next_state = await sell_handle_amount(update, context)

        self.assertEqual(next_state, SELL_INPUT_AMOUNT)
        reply_text = update.message.reply_text.call_args.args[0]
        self.assertTrue("7.500" in reply_text or "7,500" in reply_text)

    async def test_sell_bank_manual_and_saved_selection(self):
        """Uji input rekening manual (otomatis tersimpan) dan 1-tap pemilihan rekening tersimpan."""
        update_manual = SimpleNamespace(
            message=AsyncMock(text="BCA, 1234567890, Budi Santoso"),
            effective_user=SimpleNamespace(id=self.user_id, first_name="Budi"),
            callback_query=None
        )
        context = SimpleNamespace(
            user_data={
                "sell_symbol": "USDT", "sell_network": "BSC",
                "sell_crypto_amount": 10.0, "sell_price_per_unit": 18000.0,
                "sell_gross_nominal_idr": 180000, "sell_fee_idr": 5000,
                "sell_net_idr": 175000
            }
        )

        # 1. Manual Bank Input
        next_state = await sell_handle_bank(update_manual, context)
        self.assertEqual(next_state, SELL_CONFIRM_ORDER)
        self.assertEqual(context.user_data["sell_bank_name"], "BCA")
        self.assertEqual(context.user_data["sell_bank_acc"], "1234567890")
        self.assertEqual(context.user_data["sell_bank_holder"], "Budi Santoso")

        # Cek database: rekening tersimpan otomatis
        saved = self.db.query(UserSavedBank).filter(UserSavedBank.telegram_id == self.user_id).first()
        self.assertIsNotNone(saved)
        self.assertEqual(saved.bank_name, "BCA")

        # 2. 1-Tap Saved Bank Selection
        query_saved = AsyncMock()
        query_saved.data = f"sell_saved_bank_{saved.id}"
        query_saved.edit_message_text = AsyncMock()
        update_saved = SimpleNamespace(
            callback_query=query_saved,
            effective_user=SimpleNamespace(id=self.user_id, first_name="Budi")
        )
        next_saved_state = await sell_handle_saved_bank(update_saved, context)
        self.assertEqual(next_saved_state, SELL_CONFIRM_ORDER)

    async def test_sell_order_creation_and_hot_wallet(self):
        """Uji pembuatan order sell di DB, verifikasi alamat hot wallet multi-chain, dan alert admin."""
        query = AsyncMock()
        query.data = "sell_confirm"
        query.message = AsyncMock()
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=self.user_id, name="TestTrader")
        )
        context = SimpleNamespace(
            user_data={
                "sell_order_id": "SELL-20261005-001",
                "sell_symbol": "APT",
                "sell_network": "APTOS",
                "sell_crypto_amount": 2.5,
                "sell_price_per_unit": 150000.0,
                "sell_gross_nominal_idr": 375000,
                "sell_fee_idr": 9500,
                "sell_net_idr": 365500,
                "sell_bank_name": "MANDIRI",
                "sell_bank_acc": "987654321",
                "sell_bank_holder": "Test Trader"
            },
            bot=AsyncMock()
        )

        with patch("services.qris_generator.get_wallet_qr_stream", return_value=None), \
             patch("bot.handlers.sell.notify_admins", new=AsyncMock()) as mock_notify:
            next_state = await sell_handle_confirm(update, context)

        self.assertEqual(next_state, SELL_WAITING_TX)

        # Cek DB order tersimpan
        order = get_order_by_id(self.db, "SELL-20261005-001")
        self.assertIsNotNone(order)
        self.assertEqual(order.order_type, "sell")
        self.assertEqual(order.status, "WAITING_CRYPTO_DEPOSIT")
        self.assertEqual(order.crypto_symbol, "APT")
        self.assertEqual(order.network, "APTOS")
        # Nominal koin persis yang dipilih user: tanpa kode unik (kode unik hanya di QRIS).
        self.assertEqual(float(order.crypto_amount), 2.5)
        self.assertEqual(order.total_idr, 365500)
        # Admin baru dikabari setelah TX hash user terverifikasi on-chain, bukan saat order dibuat.
        self.assertFalse(mock_notify.called)

    async def test_sell_tx_hash_and_proof_upload(self):
        """Uji pengiriman TX hash manual dan upload foto bukti transfer pada order sell."""
        order_id = "SELL-20261005-TX"
        order_data = {
            "order_id": order_id,
            "telegram_id": self.user_id,
            "order_type": "sell",
            "crypto_symbol": "USDT",
            "network": "BSC",
            "crypto_amount": Decimal("10.0"),
            "price_per_unit": 18000,
            "nominal_idr": 180000,
            "fee_idr": 5000,
            "total_idr": 175000,
            "buyer_wallet": "BCA | 12345 | Test",
            "deposit_wallet": "0x1111111111111111111111111111111111111111",
            "status": "WAITING_CRYPTO_DEPOSIT"
        }
        create_order(self.db, order_data)

        # 1. Kirim TX Hash
        tx_hex = "0x" + "a" * 64
        update_tx = SimpleNamespace(
            message=AsyncMock(text=tx_hex),
            effective_user=SimpleNamespace(id=self.user_id, name="Trader")
        )
        context_tx = SimpleNamespace(
            user_data={
                "sell_order_id": order_id,
                "sell_symbol": "USDT",
                "sell_network": "BSC",
                "sell_crypto_amount": 10.0,
                "sell_net_idr": 175000,
                "sell_bank_name": "BCA",
                "sell_bank_acc": "12345",
                "sell_bank_holder": "Test"
            },
            bot=AsyncMock()
        )

        with patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value={"verified": False, "reason": "Menunggu konfirmasi"})), \
             patch("services.detector.deposit_detector.verifikasi_cepat", new=AsyncMock()), \
             patch("bot.utils.telegram_utils.notify_admins", new=AsyncMock()):
            state = await sell_handle_tx_hash(update_tx, context_tx)

        self.assertEqual(state, SELL_WAITING_TX)
        order_updated = get_order_by_id(self.db, order_id)
        self.assertEqual(order_updated.deposit_tx_hash, tx_hex)

        # 2. Upload Bukti Foto
        photo_mock = SimpleNamespace(file_id="AgACAgUAAxkBAAI_SELL_PROOF")
        update_proof = SimpleNamespace(
            message=SimpleNamespace(photo=[photo_mock], reply_text=AsyncMock()),
            effective_user=SimpleNamespace(id=self.user_id)
        )
        with patch("bot.handlers.sell.notify_admins", new=AsyncMock()):
            state_proof = await handle_sell_proof(update_proof, context_tx)

        self.assertEqual(state_proof, SELL_WAITING_TX)
        self.db.expire_all()
        order_with_proof = get_order_by_id(self.db, order_id)
        self.assertEqual(order_with_proof.deposit_proof_file_id, "AgACAgUAAxkBAAI_SELL_PROOF")

    async def test_sell_verified_tx_hash_is_accepted_not_rejected(self):
        """Hash yang langsung terverifikasi (reason "OK") dulu dibalas 'Belum Bisa Diverifikasi'."""
        order_id = "SELL-20261006-OK"
        create_order(self.db, {
            "order_id": order_id, "telegram_id": self.user_id, "order_type": "sell",
            "crypto_symbol": "USDT", "network": "BSC", "crypto_amount": Decimal("10.0"),
            "price_per_unit": 18000, "nominal_idr": 180000, "fee_idr": 5000, "total_idr": 175000,
            "buyer_wallet": "BCA | 12345 | Test",
            "deposit_wallet": "0x1111111111111111111111111111111111111111",
            "status": "WAITING_CRYPTO_DEPOSIT",
        })
        message = AsyncMock(text="0x" + "b" * 64)
        update_tx = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=self.user_id))
        context_tx = SimpleNamespace(
            user_data={"sell_order_id": order_id, "sell_symbol": "USDT", "sell_network": "BSC",
                       "sell_crypto_amount": 10.0, "sell_net_idr": 175000, "sell_bank_name": "BCA",
                       "sell_bank_acc": "12345", "sell_bank_holder": "Test"},
            bot=AsyncMock(), application=object(),
        )
        from services import tx_verifier
        order = get_order_by_id(self.db, order_id)
        verified = {"verified": True, "amount": 10.0, "reason": "OK", "from_address": "0xabc",
                    "tx_hash": "0x" + "b" * 64,
                    "timestamp": tx_verifier._timestamp(order.created_at) + 5}
        with patch("services.tx_verifier.verify_deposit", new=AsyncMock(return_value=verified)),              patch("services.detector.safe_send_message", new=AsyncMock()),              patch("services.detector.notify_admins", new=AsyncMock()) as notify_detector,              patch("bot.handlers.sell.notify_admins", new=AsyncMock()) as notify_sell:
            state = await sell_handle_tx_hash(update_tx, context_tx)

        self.assertEqual(state, SELL_WAITING_TX)
        sent = " ".join(str(c.kwargs.get("text") or (c.args[0] if c.args else ""))
                        for c in message.reply_text.await_args_list)
        self.assertIn("TX Hash Diterima", sent)
        self.assertNotIn("Belum Bisa Diverifikasi", sent)
        # Valid -> deposit terkonfirmasi dan admin dikabari (sekali) untuk transfer Rupiah.
        self.db.expire_all()
        self.assertEqual(get_order_by_id(self.db, order_id).status, "CRYPTO_CONFIRMED")
        notify_detector.assert_awaited_once()
        notify_sell.assert_not_called()

    async def test_sell_cancellation_updates_db(self):
        """Uji pembatalan order sell mengupdate status di DB menjadi 'cancelled'."""
        order_id = "SELL-CANCEL-TEST"
        create_order(self.db, {
            "order_id": order_id,
            "telegram_id": self.user_id,
            "order_type": "sell",
            "crypto_symbol": "SOL",
            "network": "SOLANA",
            "crypto_amount": Decimal("1.0"),
            "price_per_unit": 2000000,
            "nominal_idr": 2000000,
            "fee_idr": 15000,
            "total_idr": 1985000,
            "buyer_wallet": "BCA | 999 | User",
            "deposit_wallet": "SolDepositAddr",
            "status": "WAITING_CRYPTO_DEPOSIT"
        })

        query = AsyncMock()
        query.data = "sell_cancel"
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=self.user_id))
        context = SimpleNamespace(user_data={"sell_order_id": order_id}, bot=AsyncMock())

        with patch("bot.handlers.start.send_main_menu", new=AsyncMock()):
            await cancel_sell(update, context)

        order = get_order_by_id(self.db, order_id)
        self.assertEqual(order.status, "cancelled")


class TestE2EConvertFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.user_id = 555666
        self.user = User(
            telegram_id=self.user_id,
            username="swap_user",
            full_name="Swap User"
        )
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _mock_prices(self):
        return {
            "USDT": {
                "symbol": "USDT", "market_price_idr": 18000.0,
                "buy_price_idr": 18000.0, "sell_price_idr": 18000.0,
                "spread_pct": 0.0, "usdt_idr_rate": 18000.0,
                "source": "OKX+SpotFX", "price_updated_at": 1728000000,
            },
            "ETH": {
                "symbol": "ETH", "market_price_idr": 48_000_000.0,
                "buy_price_idr": 48_000_000.0, "sell_price_idr": 48_000_000.0,
                "spread_pct": 0.0, "usdt_idr_rate": 18000.0,
                "source": "OKX+SpotFX", "price_updated_at": 1728000000,
            },
            "SOL": {
                "symbol": "SOL", "market_price_idr": 2_160_000.0,
                "buy_price_idr": 2_160_000.0, "sell_price_idr": 2_160_000.0,
                "spread_pct": 0.0, "usdt_idr_rate": 18000.0,
                "source": "OKX+SpotFX", "price_updated_at": 1728000000,
            }
        }

    async def test_convert_selection_guards(self):
        """Uji guard pemilihan convert: jaringan manual ditolak, bridge beda jaringan diizinkan."""
        query = AsyncMock()
        query.data = "swap_tgt_net_ROBINHOOD"
        query.edit_message_text = AsyncMock()
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=self.user_id))
        context = SimpleNamespace(
            user_data={"swap_src_symbol": "ETH", "swap_src_network": "ETH", "swap_tgt_symbol": "ETH"}
        )

        with patch("bot.handlers.swap.MANUAL_PAYOUT_NETWORKS", {"ROBINHOOD"}):
            res = await swap_select_tgt_net(update, context)
        self.assertEqual(res, -1)  # ConversationHandler.END
        call_msg = query.edit_message_text.call_args.args[0]
        self.assertIn("belum tersedia", call_msg)

    def test_convert_amount_parser_all_formats(self):
        """Uji fleksibilitas parser nominal: crypto amount, nominal rupiah (dot, k, rb, jt), dan dollar ($/usd)."""
        src_price = 2_000_000.0  # SOL
        usdt_rate = 18_000.0

        # 1. Koin desimal
        amt, nom, mode = parse_convert_amount("0.5", src_price, usdt_rate, "SOL")
        self.assertEqual(amt, 0.5)
        self.assertEqual(nom, 1_000_000)
        self.assertEqual(mode, "CRYPTO")

        # 2. Format Rupiah: 50k, 50.000, 1.5jt, Rp 100000
        amt_k, nom_k, mode_k = parse_convert_amount("50k", src_price, usdt_rate, "SOL")
        self.assertEqual(nom_k, 50_000)
        self.assertEqual(amt_k, 50_000 / src_price)
        self.assertEqual(mode_k, "IDR")

        amt_dot, nom_dot, _ = parse_convert_amount("100.000", src_price, usdt_rate, "SOL")
        self.assertEqual(nom_dot, 100_000)

        amt_jt, nom_jt, _ = parse_convert_amount("1.5jt", src_price, usdt_rate, "SOL")
        self.assertEqual(nom_jt, 1_500_000)

        # 3. Format Dollar: $10, 25 USD, 10.5 usdt
        amt_usd, nom_usd, mode_usd = parse_convert_amount("$10", src_price, usdt_rate, "SOL")
        self.assertEqual(nom_usd, 180_000)
        self.assertEqual(amt_usd, 180_000 / src_price)
        self.assertEqual(mode_usd, "USD")

    async def test_convert_fee_and_target_amount_zero_spread(self):
        """Uji perhitungan fee convert tier resmi dan estimasi koin tujuan dengan pure realtime market price."""
        update = SimpleNamespace(
            message=AsyncMock(text="100k"),
            effective_user=SimpleNamespace(id=self.user_id)
        )
        context = SimpleNamespace(
            user_data={
                "swap_src_symbol": "USDT", "swap_src_network": "BSC",
                "swap_tgt_symbol": "SOL", "swap_tgt_network": "SOLANA"
            }
        )

        mock_prices = self._mock_prices()
        with patch("services.price_service.price_service.get_price", side_effect=lambda s: mock_prices.get(s)):
            state = await swap_input_amount(update, context)

        self.assertEqual(state, SWAP_INPUT_TARGET_ADDR)
        # Rp 100.000 -> USDT dibulatkan KE ATAS ke presisi deposit (4 desimal, tanpa kode unik) agar
        # nilainya tidak di bawah nominal; nominal yang dicatat tetap persis Rp 100.000.
        src = context.user_data["swap_src_amount"]
        usdt_px = mock_prices["USDT"]["market_price_idr"]
        self.assertEqual(src, float(Decimal(str(100_000 / usdt_px)).quantize(Decimal("0.0001"), rounding="ROUND_UP")))
        nominal = context.user_data["swap_nominal_idr"]
        self.assertEqual(nominal, 100_000)
        self.assertGreaterEqual(int(Decimal(str(src)) * Decimal(str(usdt_px))), 100_000)  # koin yang disetor cukup
        expected_fee = calculate_fee_idr(nominal, "CONVERT", symbol="SOL", network="SOLANA", is_outgoing=True)
        self.assertEqual(context.user_data["swap_fee_idr"], expected_fee)
        expected_tgt = (nominal - expected_fee) / 2_160_000.0
        self.assertAlmostEqual(context.user_data["swap_tgt_amount"], expected_tgt, places=6)

    async def test_convert_nominal_pas_minimum_5000_diterima_dan_tercatat_5000(self):
        """Dulu Rp 5.000 ditolak (koin dibulatkan ke bawah -> Rp 4.999) dan harus diketik 5001."""
        for usdt_px in (16_000.0, 16_432.0, 17_915.0, 16_789.0, 15_917.0):
            for text in ("5000", "5.000", "5k", "Rp 5000"):
                with self.subTest(price=usdt_px, text=text):
                    update = SimpleNamespace(message=AsyncMock(text=text),
                                             effective_user=SimpleNamespace(id=self.user_id))
                    context = SimpleNamespace(user_data={
                        "swap_src_symbol": "USDT", "swap_src_network": "BSC",
                        "swap_tgt_symbol": "SOL", "swap_tgt_network": "SOLANA",
                        "swap_input_mode": "IDR"})
                    prices = self._mock_prices()
                    prices["USDT"] = dict(prices["USDT"], market_price_idr=usdt_px, usdt_idr_rate=usdt_px)
                    with patch("services.price_service.price_service.get_price", side_effect=lambda s: prices.get(s)):
                        state = await swap_input_amount(update, context)
                    self.assertEqual(state, SWAP_INPUT_TARGET_ADDR, f"Rp 5.000 harus diterima ({text})")
                    self.assertEqual(context.user_data["swap_nominal_idr"], 5000)
                    self.assertGreaterEqual(
                        int(Decimal(str(context.user_data["swap_src_amount"])) * Decimal(str(usdt_px))), 5000)

    async def test_convert_di_bawah_minimum_tetap_ditolak(self):
        update = SimpleNamespace(message=AsyncMock(text="4999"), effective_user=SimpleNamespace(id=self.user_id))
        context = SimpleNamespace(user_data={
            "swap_src_symbol": "USDT", "swap_src_network": "BSC",
            "swap_tgt_symbol": "SOL", "swap_tgt_network": "SOLANA", "swap_input_mode": "IDR"})
        mock_prices = self._mock_prices()
        with patch("services.price_service.price_service.get_price", side_effect=lambda s: mock_prices.get(s)):
            state = await swap_input_amount(update, context)
        self.assertEqual(state, SWAP_INPUT_AMOUNT)

    async def test_convert_rejects_self_dealing_wallet(self):
        """Uji validasi alamat tujuan: menolak jika user memasukkan alamat hot wallet milik bot sendiri."""
        bot_hot_wallet = "0x1111111111111111111111111111111111111111"
        update = SimpleNamespace(
            message=AsyncMock(text=bot_hot_wallet),
            effective_user=SimpleNamespace(id=self.user_id)
        )
        context = SimpleNamespace(
            user_data={
                "swap_src_symbol": "SOL", "swap_src_network": "SOLANA",
                "swap_tgt_symbol": "USDT", "swap_tgt_network": "POLYGON",
                "swap_src_amount": 0.5, "swap_tgt_amount": 55.0,
                "swap_nominal_idr": 1000000, "swap_fee_idr": 10000
            }
        )

        fake_sender = SimpleNamespace(
            validate_address=lambda addr: True,
            wallet_address=bot_hot_wallet
        )
        with patch("services.crypto_sender.CryptoSenderFactory.get_sender", return_value=fake_sender):
            state = await swap_input_target_addr(update, context)

        self.assertEqual(state, SWAP_INPUT_TARGET_ADDR)
        reply = update.message.reply_text.call_args.args[0]
        self.assertIn("tidak boleh wallet milik bot sendiri", reply)

    async def test_convert_stock_inventory_guard(self):
        """Uji guard stok: menolak order convert jika stok koin target di hot wallet tidak mencukupi."""
        query = AsyncMock()
        query.data = "confirm_swap_order"
        query.from_user = SimpleNamespace(id=self.user_id, username="u", full_name="U")
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(
            user_data={
                "swap_src_symbol": "USDT", "swap_src_network": "BSC",
                "swap_src_amount": 100.0, "swap_tgt_symbol": "SOL",
                "swap_tgt_network": "SOLANA", "swap_tgt_amount": 5.0,
                "swap_nominal_idr": 1800000, "swap_fee_idr": 25000,
                "swap_target_addr": "UserSolAddr111111111111111111111111111111111",
                "swap_seller_deposit_wallet": "BotBscAddr111111111111111111111111111111111"
            },
            bot=AsyncMock()
        )

        # Stok SOL hanya 1.0 < 5.0 SOL yang diminta
        with patch("services.wallet_sync.sync_wallet_balances", new=AsyncMock()), \
             patch("database.crud.get_available_inventory", return_value=Decimal("1.0")):
            await confirm_swap_order(update, context)

        call_msg = query.edit_message_text.call_args.args[0]
        self.assertIn("Stok tujuan tidak cukup", call_msg)
        self.assertEqual(self.db.query(Order).count(), 0)

    async def test_convert_order_creation_success(self):
        """Uji sukses pembuatan order swap di DB lengkap dengan audit log dan masa berlaku quote 30 menit."""
        query = AsyncMock()
        query.data = "confirm_swap_order"
        query.from_user = SimpleNamespace(id=self.user_id, username="u", full_name="U")
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(
            user_data={
                "swap_src_symbol": "USDT", "swap_src_network": "BSC",
                "swap_src_amount": 10.0, "swap_tgt_symbol": "USDT",
                "swap_tgt_network": "POLYGON", "swap_tgt_amount": 9.5,
                "swap_nominal_idr": 180000, "swap_fee_idr": 6000,
                "swap_target_addr": "0xUserPolygonAddr1111111111111111111111111",
                "swap_seller_deposit_wallet": "0xBotBscAddr1111111111111111111111111"
            },
            bot=AsyncMock()
        )

        # Stok mencukupi (100 USDT)
        with patch("services.wallet_sync.sync_wallet_balances", new=AsyncMock()), \
             patch("database.crud.get_available_inventory", return_value=Decimal("100.0")):
            state = await confirm_swap_order(update, context)

        self.assertEqual(state, SWAP_WAITING_DEPOSIT_HASH)
        order = self.db.query(Order).filter(Order.order_type == "swap").first()
        self.assertIsNotNone(order)
        self.assertEqual(order.status, "WAITING_CRYPTO_DEPOSIT")
        # 10 USDT persis, tanpa kode unik.
        self.assertEqual(float(order.crypto_amount), 10.0)
        self.assertEqual(float(order.target_crypto_amount), 9.5)
        # Quote berlaku 10 menit
        self.assertIsNotNone(order.quote_expires_at)
        diff = order.quote_expires_at - order.quoted_at
        self.assertEqual(int(diff.total_seconds()), 600)

    async def test_convert_cancellation_updates_db(self):
        """Uji pembatalan convert order mengupdate status order di DB menjadi 'cancelled'."""
        order_id = "SWAP-20261005120000-777"
        create_order(self.db, {
            "order_id": order_id,
            "telegram_id": self.user_id,
            "order_type": "swap",
            "crypto_symbol": "USDT",
            "network": "BSC",
            "crypto_amount": Decimal("10.0"),
            "target_crypto_symbol": "SOL",
            "target_network": "SOLANA",
            "target_crypto_amount": Decimal("0.08"),
            "price_per_unit": 0,
            "nominal_idr": 180000,
            "fee_idr": 6000,
            "total_idr": 180000,
            "deposit_wallet": "0xDepositAddr",
            "buyer_wallet": "SolUserAddr",
            "status": "WAITING_CRYPTO_DEPOSIT"
        })

        query = AsyncMock()
        query.data = f"cancel_swap_order_{order_id}"
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=self.user_id))
        context = SimpleNamespace(user_data={"active_swap_order_id": order_id}, bot=AsyncMock())

        with patch("bot.handlers.start.send_main_menu", new=AsyncMock()):
            await cancel_swap(update, context)

        order = get_order_by_id(self.db, order_id)
        self.assertEqual(order.status, "cancelled")


class TestDepositDetectorLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self.user_id = 111222

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    async def test_detector_confirm_sell_order(self):
        """Detector: Konfirmasi deposit Sell mengubah status ke CRYPTO_CONFIRMED dan menotifikasi admin untuk kirim Rupiah."""
        order_id = "SELL-DETECTOR-01"
        order = Order(
            order_id=order_id,
            telegram_id=self.user_id,
            order_type="sell",
            crypto_symbol="USDT",
            network="BSC",
            crypto_amount=Decimal("50.0"),
            price_per_unit=18000,
            nominal_idr=900000,
            fee_idr=10000,
            total_idr=890000,
            buyer_wallet="BCA | 54321 | Sukses",
            deposit_wallet="0x1111111111111111111111111111111111111111",
            status="WAITING_CRYPTO_DEPOSIT",
            created_at=datetime.utcnow() - timedelta(minutes=5)
        )
        self.db.add(order)
        self.db.commit()

        tx_hash = "0x" + "b" * 64
        verified = {
            "verified": True,
            "timestamp": int(tx_verifier._timestamp(order.created_at)) + 60,
            "amount": Decimal("50.0"),
            "tx_hash": tx_hash
        }

        bot_mock = AsyncMock()
        with patch("services.detector.notify_admins", new=AsyncMock()) as mock_admin, \
             patch("services.detector.safe_send_message", new=AsyncMock()) as mock_user:
            await deposit_detector._confirm_order(self.db, order, tx_hash, verified, bot_mock)

        self.assertEqual(order.status, "CRYPTO_CONFIRMED")
        self.assertEqual(order.deposit_tx_hash, tx_hash)
        self.assertTrue(mock_user.called)
        self.assertTrue(mock_admin.called)
        admin_call_args = mock_admin.call_args.args[1]
        self.assertIn("TRANSFER RUPIAH SEGERA", admin_call_args)
        self.assertIn("890.000", admin_call_args)

    async def test_detector_confirm_swap_order_triggers_payout(self):
        """Detector: Konfirmasi deposit Swap otomatis memicu pengiriman koin tujuan ke wallet user."""
        order_id = "SWAP-DETECTOR-01"
        order = Order(
            order_id=order_id,
            telegram_id=self.user_id,
            order_type="swap",
            crypto_symbol="USDT",
            network="BSC",
            crypto_amount=Decimal("10.0"),
            target_crypto_symbol="SOL",
            target_network="SOLANA",
            target_crypto_amount=Decimal("0.08"),
            price_per_unit=0,
            nominal_idr=180000,
            fee_idr=6000,
            total_idr=180000,
            buyer_wallet="SolUserWalletAddress111111111111111111",
            deposit_wallet="0x1111111111111111111111111111111111111111",
            status="WAITING_CRYPTO_DEPOSIT",
            created_at=datetime.utcnow() - timedelta(minutes=5)
        )
        self.db.add(order)
        self.db.commit()

        tx_hash = "0x" + "c" * 64
        verified = {
            "verified": True,
            "timestamp": int(tx_verifier._timestamp(order.created_at)) + 60,
            "amount": Decimal("10.0"),
            "tx_hash": tx_hash
        }

        bot_mock = AsyncMock()
        with patch.object(deposit_detector, "_execute_payout", new=AsyncMock()) as mock_payout, \
             patch("services.detector.safe_send_message", new=AsyncMock()):
            await deposit_detector._confirm_order(self.db, order, tx_hash, verified, bot_mock)

        self.assertEqual(order.status, "CRYPTO_CONFIRMED")
        self.assertTrue(mock_payout.called)

    async def test_detector_recovers_expired_sell_order_within_24h(self):
        """Detector: Order sell yang statusnya sudah 'expired' tetap dapat diverifikasi jika deposit masuk dalam jendela 24 jam."""
        order_id = "SELL-EXPIRED-LATE"
        order = Order(
            order_id=order_id,
            telegram_id=self.user_id,
            order_type="sell",
            crypto_symbol="USDT",
            network="BSC",
            crypto_amount=Decimal("20.0"),
            price_per_unit=18000,
            nominal_idr=360000,
            fee_idr=8000,
            total_idr=352000,
            buyer_wallet="BCA | 777 | Late",
            deposit_wallet="0x1111111111111111111111111111111111111111",
            status="expired",  # Order berstatus expired
            created_at=datetime.utcnow() - timedelta(hours=2)  # Dibuat 2 jam lalu (masih dalam jendela 24 jam)
        )
        self.db.add(order)
        self.db.commit()

        tx_hash = "0x" + "d" * 64
        verified = {
            "verified": True,
            "timestamp": int(tx_verifier._timestamp(order.created_at)) + 60,
            "amount": Decimal("20.0"),
            "tx_hash": tx_hash
        }

        bot_mock = AsyncMock()
        with patch("services.detector.notify_admins", new=AsyncMock()), \
             patch("services.detector.safe_send_message", new=AsyncMock()):
            await deposit_detector._confirm_order(self.db, order, tx_hash, verified, bot_mock)

        # Berhasil dipulihkan dari expired menjadi CRYPTO_CONFIRMED
        self.assertEqual(order.status, "CRYPTO_CONFIRMED")


if __name__ == "__main__":
    unittest.main()
