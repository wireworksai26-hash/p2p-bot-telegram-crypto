"""
services/crypto_sender/ton_sender.py — Sender untuk The Open Network (GRAM / TON).
=================================================================================
Pengecekan saldo & pengiriman koin GRAM native / Jetton (USDT) di The Open Network.

Provider utama tonapi.io (gratis, tanpa API key) dengan fallback toncenter.
Pengiriman mendukung wallet v5r1 (W5, default wallet baru) dan v4r2 (tonsdk),
termasuk deploy otomatis saat wallet masih uninitialized. Tidak ada broadcast
ulang setelah percobaan pertama agar tidak terjadi pengiriman ganda.
"""

import logging
import asyncio
import base64
import time
from decimal import Decimal
import httpx
from config.settings import settings
from services.crypto_sender import BaseCryptoSender, SendResult

logger = logging.getLogger(__name__)

# Alamat Jetton USDT di TON (master)
USDT_JETTON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"

# Fee forward yang dikirim owner saat transfer jetton (0.01 GRAM)
JETTON_FORWARD_TON = 0.01

# Nilai GRAM yang dibawa pesan jetton ke jetton wallet (sisa dikembalikan on-chain)
JETTON_MESSAGE_NANO = 50_000_000

NANO = 10**9
TONAPI_BASE = "https://tonapi.io/v2"
TONAPI_MAINNET_GLOBAL_ID = -239
W5_SIGNED_EXTERNAL_OPCODE = 0x7369676E
W5_UNLIMITED_VALID_UNTIL = 2**32 - 1


class TonBroadcastRejected(RuntimeError):
    """Provider menolak BOC secara pasti: belum ada transaksi yang masuk jaringan."""


class TonBroadcastUncertain(RuntimeError):
    """Status broadcast tidak diketahui (timeout): hash bisa saja sudah masuk jaringan."""


class TonSender(BaseCryptoSender):
    def __init__(self):
        self.network = "TON"
        self.rpc_url = settings.TON_RPC or "https://toncenter.com/api/v2/jsonRPC"
        self.api_key = settings.TON_API_KEY
        self.wallet_address = settings.TON_WALLET_ADDRESS
        self.explorer_base = "https://tonviewer.com"
        self.api_base = TONAPI_BASE

    def validate_address(self, address: str) -> bool:
        """Validasi alamat TON (format user-friendly / raw)."""
        from bot.utils.validator import validate_wallet_address
        return validate_wallet_address(address, "TON")

    # ---------------- RPC helper (POST jsonRPC) ----------------
    async def _rpc(self, method: str, params: dict):
        """
        Panggil Toncenter via POST jsonRPC dengan retry (anti 429 rate-limit).
        Return result dict/list/str, atau None.
        """
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        url = self.rpc_url
        if self.api_key:
            url = f"{self.rpc_url}?api_key={self.api_key}"

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    res = await client.post(url, json=payload)
                    if res.status_code == 200:
                        data = res.json()
                        if not data.get("ok"):
                            logger.warning(f"TON RPC {method} error: {data.get('error')}")
                            return None
                        return data.get("result")
                    if res.status_code in (429, 500, 502, 503, 504):
                        logger.warning(f"TON RPC {method} HTTP {res.status_code} (retry {attempt + 1})")
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    logger.warning(f"TON RPC {method} HTTP {res.status_code}")
                    return None
            except Exception as e:
                logger.warning(f"TON RPC {method} error: {e}")
                if attempt < 2:
                    await asyncio.sleep(1.0)
                    continue
        return None

    # ---------------- Helper tonapi (tanpa API key) ----------------
    async def _tonapi_get(self, path: str, params: dict | None = None):
        async with httpx.AsyncClient(timeout=12.0) as client:
            res = await client.get(f"{self.api_base}/{path.lstrip('/')}", params=params)
            if res.status_code == 404:
                raise FileNotFoundError(f"tonapi 404: {path}")
            res.raise_for_status()
            return res.json()

    async def _tonapi_post(self, path: str, payload: dict) -> httpx.Response:
        async with httpx.AsyncClient(timeout=15.0) as client:
            return await client.post(f"{self.api_base}/{path.lstrip('/')}", json=payload)

    async def _account_status(self) -> str:
        """Status akun wallet (active/uninit/nonexist); 'unknown' bila tidak terbaca."""
        try:
            data = await self._tonapi_get(f"accounts/{self.wallet_address}")
            return str(data.get("status", "unknown"))
        except FileNotFoundError:
            return "nonexist"
        except Exception as exc:
            logger.warning("Gagal membaca status akun TON (%s).", type(exc).__name__)
            return "unknown"

    # ---------------- Balance ----------------
    async def get_balance(self, symbol: str = "") -> float:
        """Ambil saldo GRAM native (tonapi, fallback toncenter) atau Jetton (USDT)."""
        sym = symbol.upper() if symbol else ""
        if sym in ("USDT",):
            return await self._get_jetton_balance(USDT_JETTON_MASTER)
        if sym not in ("", "TON"):
            raise ValueError(f"Token '{sym}' tidak didukung pada TON.")

        if not self.wallet_address:
            raise RuntimeError("TON_WALLET_ADDRESS belum dikonfigurasi.")

        try:
            data = await self._tonapi_get(f"accounts/{self.wallet_address}")
            return int(data.get("balance", 0)) / NANO
        except Exception as exc:
            logger.warning("Saldo TON via tonapi gagal (%s); beralih ke toncenter.", type(exc).__name__)

        result = await self._rpc("getAddressInformation", {"address": self.wallet_address})
        if not isinstance(result, dict) or "balance" not in result:
            raise RuntimeError("RPC TON gagal membaca saldo TON.")
        return int(result["balance"]) / NANO

    async def _jetton_balance_of(self, address: str, jetton_master: str = USDT_JETTON_MASTER) -> float:
        """Saldo jetton milik satu alamat (tonapi, tanpa API key)."""
        from services.tx_verifier import ton_address

        data = await self._tonapi_get(f"accounts/{address}/jettons")
        balances = data.get("balances")
        if not isinstance(balances, list):
            raise ValueError("Daftar saldo Jetton tidak valid.")
        for item in balances:
            jetton_info = item.get("jetton", {})
            if ton_address(jetton_info.get("address", "")) == ton_address(jetton_master):
                decimals = int(jetton_info.get("decimals", 6))
                return float(int(item.get("balance", 0)) / (10 ** decimals))
        return 0.0

    async def _get_jetton_balance(self, jetton_master: str) -> float:
        if not self.wallet_address:
            raise RuntimeError("TON_WALLET_ADDRESS belum dikonfigurasi.")
        from services.tx_verifier import ton_address

        # 1. tonapi.io v2 (tanpa API key)
        try:
            return await self._jetton_balance_of(self.wallet_address, jetton_master)
        except Exception as tonapi_err:
            logger.warning(f"tonapi get_jetton_balance error: {tonapi_err}")

        # 2. toncenter v3 (tanpa API key) sebagai fallback
        try:
            from services.tx_verifier import _ton_get
            data = await _ton_get("jetton/wallets", {
                "owner_address": self.wallet_address,
                "jetton_address": jetton_master,
                "limit": 10,
            })
            for row in (data.get("jetton_wallets") or []):
                if (ton_address(row.get("jetton", "")) == ton_address(jetton_master)
                        and ton_address(row.get("owner", "")) == ton_address(self.wallet_address)):
                    return int(row.get("balance", 0)) / 1e6
        except Exception as v3_err:
            logger.warning("toncenter v3 get_jetton_balance error: %s", type(v3_err).__name__)

        raise RuntimeError("API TON gagal membaca saldo Jetton.")

    # ---------------- Wallet & kunci ----------------
    def _load_key(self) -> tuple[bytes, bytes, bytes]:
        """Kembalikan (seed32, private64, public32) dari TON_PRIVATE_KEY (mnemonic atau hex)."""
        raw_key = (settings.TON_PRIVATE_KEY or "").strip()
        if not raw_key:
            raise RuntimeError("TON_PRIVATE_KEY belum di-set di .env (mnemonic / hex wallet TON).")

        words = raw_key.split()
        if len(words) in (12, 24):
            from tonsdk.crypto import mnemonic_to_wallet_key
            pub_k, priv_k = mnemonic_to_wallet_key(words)
            return priv_k[:32], priv_k, pub_k

        import nacl.signing
        clean_hex = raw_key.replace("0x", "").replace("0X", "")
        key_bytes = bytes.fromhex(clean_hex)
        if len(key_bytes) == 32:
            signing_key = nacl.signing.SigningKey(key_bytes)
            pub_k = signing_key.verify_key.encode()
            return key_bytes, signing_key.encode() + pub_k, pub_k
        if len(key_bytes) == 64:
            return key_bytes[:32], key_bytes, key_bytes[32:]
        raise ValueError(f"Panjang private key TON tidak valid ({len(key_bytes)} bytes).")

    def _resolve_wallet(self) -> dict:
        """Tentukan versi wallet yang cocok dengan TON_WALLET_ADDRESS (v5r1 atau v4r2)."""
        from services.tx_verifier import ton_address

        _seed, priv_k, pub_k = self._load_key()
        target = ton_address(self.wallet_address)

        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        wallet_v4 = Wallets.ALL[WalletVersionEnum.v4r2](public_key=pub_k, private_key=priv_k, wc=0)
        if ton_address(wallet_v4.address.to_string(False)) == target:
            return {
                "version": "v4r2", "priv": priv_k, "pub": pub_k,
                "address": wallet_v4.address.to_string(False),
                "wallet_id": None, "state_init": None,
            }

        try:
            from pytoniq.contract.wallets.wallet_v5 import (
                WALLET_V5_R1_CODE,
                WalletV5R1,
                WalletV5WalletID,
            )
            from pytoniq_core.boc.address import Address
            from pytoniq_core.tlb.account import StateInit
        except ImportError as exc:
            raise RuntimeError("Library pytoniq tidak tersedia untuk wallet v5r1.") from exc

        wallet_id = WalletV5WalletID(
            network_global_id=TONAPI_MAINNET_GLOBAL_ID, workchain=0
        ).pack()
        data = WalletV5R1.create_data_cell(
            pub_k, wc=0, network_global_id=TONAPI_MAINNET_GLOBAL_ID
        )
        state_init = StateInit(code=WALLET_V5_R1_CODE, data=data)
        address = Address((0, state_init.serialize().hash))
        if ton_address(address.to_str(is_user_friendly=True, is_bounceable=True)) == target:
            return {
                "version": "v5r1", "priv": priv_k, "pub": pub_k,
                "address": address.to_str(is_user_friendly=True, is_bounceable=True),
                "wallet_id": wallet_id, "state_init": state_init,
            }

        raise ValueError("Key/wallet version TON tidak cocok dengan alamat stok.")

    def _load_wallet(self):
        """Load wallet TON v4r2 dari mnemonic atau hex private key."""
        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        from services.tx_verifier import ton_address

        _seed, priv_k, pub_k = self._load_key()
        wallet = Wallets.ALL[WalletVersionEnum.v4r2](public_key=pub_k, private_key=priv_k, wc=0)
        if ton_address(wallet.address.to_string(False)) != ton_address(self.wallet_address):
            raise ValueError("Key/wallet version TON tidak cocok dengan alamat stok.")
        return wallet

    async def _get_seqno(self, address: str = "") -> int:
        """Seqno wallet: tonapi (404 = belum deploy = 0), fallback toncenter."""
        address = address or self.wallet_address
        try:
            data = await self._tonapi_get(f"blockchain/accounts/{address}/methods/seqno")
            if data.get("success") is False:
                return 0
            stack = data.get("stack") or []
            if not stack:
                return 0
            top = stack[0] if isinstance(stack[0], dict) else {}
            value = top.get("num")
            return int(value, 16) if isinstance(value, str) else int(value or 0)
        except FileNotFoundError:
            return 0
        except Exception as exc:
            logger.warning("Seqno TON via tonapi gagal (%s); beralih ke toncenter.", type(exc).__name__)

        state = await self._rpc("getAddressInformation", {"address": address})
        if isinstance(state, dict) and state.get("state") == "uninitialized":
            return 0
        result = await self._rpc("runGetMethod", {"address": address, "method": "seqno", "stack": []})
        if not isinstance(result, dict) or result.get("exit_code") != 0:
            raise RuntimeError("Seqno TON tidak dapat diverifikasi.")
        return int(result["stack"][0][1], 16)

    # ---------------- Broadcast ----------------
    async def _broadcast(self, boc_bytes: bytes) -> str:
        """Kirim BOC: tonapi dulu (gratis tanpa key), lalu toncenter sebagai fallback."""
        boc_b64 = base64.b64encode(boc_bytes).decode()

        tonapi_error = ""
        try:
            response = await self._tonapi_post("blockchain/message", {"boc": boc_b64})
            if response.status_code == 200:
                return "tonapi"
            tonapi_error = f"tonapi HTTP {response.status_code}: {response.text[:160]}"
        except Exception as exc:
            # Timeout/transport: status belum pasti, jangan kirim ulang lewat provider lain.
            raise TonBroadcastUncertain(
                f"Broadcast TON belum pasti via tonapi ({type(exc).__name__}); cek hash sebelum kirim ulang."
            ) from exc

        toncenter_error = ""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    self.rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "sendBoc", "params": {"boc": boc_b64}},
                    headers={"X-API-Key": self.api_key} if self.api_key else {},
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok") is True:
                        return "toncenter"
                    toncenter_error = f"toncenter menolak BOC: {str(data.get('error') or data.get('result'))[:160]}"
                else:
                    toncenter_error = f"toncenter HTTP {response.status_code}: {response.text[:160]}"
        except Exception as exc:
            toncenter_error = f"toncenter {type(exc).__name__}: {str(exc)[:120]}"

        raise TonBroadcastRejected(f"Broadcast TON ditolak. {tonapi_error} | {toncenter_error}")

    async def _recent_out_tx_hash(self, dest_address: str, since_ts: int, value_nano: int = 0) -> str:
        """Hash transaksi kita yang mengirim pesan keluar ke alamat tujuan (opsional nominal tertentu)."""
        from services.tx_verifier import ton_address

        data = await self._tonapi_get(
            f"blockchain/accounts/{self.wallet_address}/transactions", {"limit": 12}
        )
        target = ton_address(dest_address)
        for tx in data.get("transactions") or []:
            if not tx.get("success") or int(tx.get("utime") or 0) < since_ts - 60:
                continue
            for msg in tx.get("out_msgs") or []:
                dest = (msg.get("destination") or {}).get("address", "")
                if ton_address(dest) != target:
                    continue
                if value_nano and int(msg.get("value") or 0) != value_nano:
                    continue
                return str(tx.get("hash") or "")
        return ""

    async def _await_delivery(
        self, recipient, amount, symbol, since_ts, reference, jetton_wallet=""
    ) -> SendResult:
        """
        Tunggu bukti on-chain dari pesan yang sudah dibroadcast.

        Native: ada pesan keluar ke penerima dengan nominal tepat.
        USDT: saldo jetton penerima bertambah minimal sebesar nominal.
        """
        from services.tx_verifier import ton_address

        amount_float = float(Decimal(str(amount)))
        native_nano = int(Decimal(str(amount)) * NANO)
        jetton_before = None
        if symbol == "USDT":
            try:
                jetton_before = await self._jetton_balance_of(recipient)
            except Exception as exc:
                logger.warning("Gagal membaca saldo jetton penerima (%s)", type(exc).__name__)

        for _ in range(15):
            try:
                if symbol == "USDT":
                    if jetton_before is not None:
                        current = await self._jetton_balance_of(recipient)
                        if current >= jetton_before + amount_float - 1e-9:
                            tx_hash = ""
                            if jetton_wallet:
                                tx_hash = await self._recent_out_tx_hash(jetton_wallet, since_ts)
                            return SendResult(
                                True,
                                tx_hash or reference,
                                explorer_url=f"{self.explorer_base}/transaction/{tx_hash}" if tx_hash else "",
                            )
                else:
                    tx_hash = await self._recent_out_tx_hash(recipient, since_ts, native_nano)
                    if tx_hash:
                        return SendResult(
                            True, tx_hash, explorer_url=f"{self.explorer_base}/transaction/{tx_hash}"
                        )
            except Exception as exc:
                logger.warning("Verifikasi payout TON belum pasti (%s)", type(exc).__name__)
            await asyncio.sleep(2)

        return SendResult(
            False,
            reference,
            "MANUAL_REVIEW: Periksa receipt/trace TON sebelum kirim ulang; broadcast sudah dicoba.",
            "",
        )

    # ---------------- Send ----------------
    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Mengirim GRAM native atau Jetton (USDT) di The Open Network."""
        try:
            from services.tx_verifier import ton_address
            from services.crypto_sender.evm_sender import _get_network_send_lock
            ton_address(to_address)
            quantity = Decimal(str(amount))
            if symbol.upper() not in ("TON", "USDT") or not quantity.is_finite() or quantity <= 0:
                raise ValueError("Nominal/aset TON tidak valid.")
        except Exception as e:
            logger.warning("Preflight TON gagal (%s)", type(e).__name__)
            return SendResult(success=False, error_message=f"MANUAL_REVIEW: Konfigurasi/preflight TON gagal ({type(e).__name__}).")

        try:
            wallet = self._resolve_wallet()
        except Exception as exc:
            logger.warning("Preflight wallet TON gagal (%s): %s", type(exc).__name__, str(exc)[:180])
            return SendResult(success=False, error_message=f"MANUAL_REVIEW: {str(exc)[:220]}")

        async with _get_network_send_lock("TON"):
            if wallet["version"] == "v5r1":
                return await self._send_via_w5(wallet, to_address, amount, symbol)
            if symbol.upper() == "USDT":
                return await self._send_jetton(to_address, amount)
            return await self._send_native(to_address, amount)

    async def _send_via_w5(self, wallet: dict, to_address: str, amount: float, symbol: str) -> SendResult:
        """Kirim lewat wallet v5r1: pesan internal -> body bertanda tangan -> external message."""
        from pytoniq.contract.contract import Contract
        from pytoniq.contract.wallets.wallet_v5 import WalletV5R1
        from pytoniq_core.boc import Builder
        from pytoniq_core.boc.address import Address
        from pytoniq_core.crypto.signature import sign_message

        reference = ""
        try:
            quantity = Decimal(str(amount))
            symbol_upper = symbol.upper()

            if symbol_upper == "USDT":
                jetton_balance = await self.get_balance("USDT")
                if jetton_balance < float(quantity):
                    return SendResult(
                        success=False,
                        error_message=f"Saldo USDT tidak cukup. Saldo: {jetton_balance}, Kebutuhan: {amount}",
                    )
                native_balance = await self.get_balance("TON")
                if native_balance < JETTON_FORWARD_TON * 2:
                    return SendResult(
                        success=False,
                        error_message=f"Saldo GRAM untuk gas jetton tidak cukup. Saldo: {native_balance} GRAM",
                    )
                jetton_wallet, value, body = await self._jetton_transfer_target(to_address, quantity)
                message = WalletV5R1.create_wallet_internal_message(
                    destination=Address(jetton_wallet), value=value, body=body
                )
            else:
                jetton_wallet = ""
                native_balance = await self.get_balance("TON")
                if native_balance < float(quantity) + 0.005:
                    return SendResult(
                        success=False,
                        error_message=(
                            f"Saldo GRAM tidak cukup. Saldo: {native_balance}, "
                            f"Kebutuhan: {amount} GRAM + gas."
                        ),
                    )
                message = WalletV5R1.create_wallet_internal_message(
                    destination=Address(to_address), value=int(quantity * NANO)
                )

            message_hash = message.serialize().hash.hex()
            # This is a message identifier, NOT a confirmed transaction hash.
            reference = "msg:" + message_hash
            since_ts = int(time.time())

            seqno = await self._get_seqno(self.wallet_address)
            state_init = wallet["state_init"] if await self._account_status() != "active" else None
            valid_until = W5_UNLIMITED_VALID_UNTIL if seqno == 0 else int(time.time()) + 60

            signing_cell = (
                Builder()
                .store_uint(W5_SIGNED_EXTERNAL_OPCODE, 32)
                .store_uint(wallet["wallet_id"], 32)
                .store_uint(valid_until, 32)
                .store_uint(seqno, 32)
                .store_cell(WalletV5R1.pack_actions([message]))
                .end_cell()
            )
            signature = sign_message(signing_cell.hash, wallet["priv"])
            body = Builder().store_cell(signing_cell).store_bytes(signature).end_cell()
            external = Contract.create_external_msg(
                dest=Address(wallet["address"]), state_init=state_init, body=body
            )

            provider = await self._broadcast(external.serialize().to_boc())
            logger.info("Broadcast TON v5r1 via %s (ref %s)", provider, reference)
            return await self._await_delivery(
                to_address, amount, symbol_upper, since_ts, reference, jetton_wallet
            )
        except TonBroadcastRejected as exc:
            logger.warning("Broadcast TON ditolak: %s", str(exc)[:200])
            return SendResult(False, "", f"MANUAL_REVIEW: {str(exc)[:250]}")
        except TonBroadcastUncertain as exc:
            logger.warning("Broadcast TON belum pasti: %s", str(exc)[:200])
            return SendResult(
                False,
                reference,
                f"MANUAL_REVIEW: {str(exc)[:200]}",
                f"{self.explorer_base}/transaction/{reference.removeprefix('msg:')}",
            )
        except Exception as exc:
            logger.warning("Pengiriman TON v5r1 gagal (%s): %s", type(exc).__name__, str(exc)[:200])
            return SendResult(False, "", f"MANUAL_REVIEW: Pengiriman TON gagal ({type(exc).__name__}).")

    async def _jetton_transfer_target(self, to_address: str, quantity: Decimal):
        """Kembalikan (alamat jetton wallet, nilai GRAM pesan, body transfer jetton)."""
        from pytoniq_core.boc import Cell as PyCell
        from tonsdk.contract.token.ft import JettonWallet
        from services.tx_verifier import _ton_get, ton_address

        data = await _ton_get("jetton/wallets", {
            "owner_address": self.wallet_address,
            "jetton_address": USDT_JETTON_MASTER,
            "limit": 10,
        })
        matching = [row for row in (data.get("jetton_wallets") or [])
                    if ton_address(row.get("owner", "")) == ton_address(self.wallet_address)
                    and ton_address(row.get("jetton", "")) == ton_address(USDT_JETTON_MASTER)]
        if len(matching) != 1:
            raise RuntimeError("Wallet Jetton USDT belum terverifikasi.")

        body = JettonWallet().create_transfer_body(
            to_address=to_address,
            jetton_amount=int(quantity * 10**6),
            forward_amount=10_000_000,
            response_address=self.wallet_address,
        )
        return matching[0]["address"], JETTON_MESSAGE_NANO, PyCell.one_from_boc(body.to_boc())

    async def _broadcast_transfer(self, transfer, to_address, amount, symbol, jetton_wallet=""):
        """Broadcast transfer tonsdk (v4r2) lalu tunggu bukti on-chain."""
        message_hash = transfer["message"].bytes_hash().hex()
        # This is a message identifier, NOT a confirmed transaction hash.
        reference = "msg:" + message_hash
        since_ts = int(time.time())
        try:
            await self._broadcast(transfer["message"].to_boc(False))
        except TonBroadcastRejected as exc:
            logger.warning("Broadcast TON ditolak: %s", str(exc)[:200])
            return SendResult(False, "", f"MANUAL_REVIEW: {str(exc)[:250]}")
        except TonBroadcastUncertain as exc:
            logger.warning("Broadcast TON belum pasti: %s", str(exc)[:200])
            return SendResult(
                False,
                reference,
                f"MANUAL_REVIEW: {str(exc)[:200]}",
                f"{self.explorer_base}/transaction/{message_hash}",
            )
        return await self._await_delivery(
            to_address, amount, symbol, since_ts, reference, jetton_wallet
        )

    async def _send_native(self, to_address: str, amount: float) -> SendResult:
        wallet = self._load_wallet()
        seqno = await self._get_seqno(self.wallet_address)
        transfer = wallet.create_transfer_message(
            to_address, int(Decimal(str(amount)) * NANO), seqno=seqno)
        return await self._broadcast_transfer(transfer, to_address, float(amount), "TON")

    async def _send_jetton(self, to_address: str, amount: float) -> SendResult:
        from tonsdk.contract.token.ft import JettonWallet
        from services.tx_verifier import _ton_get, ton_address
        wallet = self._load_wallet()
        data = await _ton_get("jetton/wallets", {
            "owner_address": self.wallet_address, "jetton_address": USDT_JETTON_MASTER, "limit": 10})
        matching = [row for row in data.get("jetton_wallets", [])
                    if ton_address(row["owner"]) == ton_address(self.wallet_address)
                    and ton_address(row["jetton"]) == ton_address(USDT_JETTON_MASTER)]
        if len(matching) != 1:
            raise RuntimeError("Wallet Jetton USDT belum terverifikasi.")
        seqno = await self._get_seqno(self.wallet_address)
        body = JettonWallet().create_transfer_body(
            to_address=to_address, jetton_amount=int(Decimal(str(amount)) * 10**6),
            forward_amount=10_000_000, response_address=self.wallet_address)
        transfer = wallet.create_transfer_message(
            to_addr=matching[0]["address"], amount=JETTON_MESSAGE_NANO, seqno=seqno, payload=body)
        return await self._broadcast_transfer(
            transfer, to_address, float(amount), "USDT", jetton_wallet=matching[0]["address"]
        )
