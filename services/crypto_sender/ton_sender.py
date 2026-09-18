"""
services/crypto_sender/ton_sender.py — Sender untuk TON Network.
================================================================
Pengecekan saldo & pengiriman TON native / Jetton (USDT) di TON Network.
Semua panggilan Toncenter memakai POST jsonRPC (endpoint GET rentan
rate-limit/404). Pengiriman memakai wallet v4r2 + tonsdk (broadcast via sendBoc).
"""

import logging
import asyncio
import base64
from decimal import Decimal
import httpx
from config.settings import settings
from services.crypto_sender import BaseCryptoSender, SendResult

logger = logging.getLogger(__name__)

# Alamat Jetton USDT di TON (master)
USDT_JETTON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"

# Fee forward yang dikirim owner saat transfer jetton (0.01 TON)
JETTON_FORWARD_TON = 0.01


class TonSender(BaseCryptoSender):
    def __init__(self):
        self.network = "TON"
        self.rpc_url = settings.TON_RPC or "https://toncenter.com/api/v2/jsonRPC"
        self.api_key = settings.TON_API_KEY
        self.wallet_address = settings.TON_WALLET_ADDRESS
        self.explorer_base = "https://tonviewer.com"

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

    # ---------------- Balance ----------------
    async def get_balance(self, symbol: str = "") -> float:
        """Ambil saldo TON native atau Jetton (USDT) dari Toncenter."""
        sym = symbol.upper() if symbol else ""
        if sym in ("USDT",):
            return await self._get_jetton_balance(USDT_JETTON_MASTER)
        if sym not in ("", "TON"):
            raise ValueError(f"Token '{sym}' tidak didukung pada TON.")

        if not self.wallet_address:
            raise RuntimeError("TON_WALLET_ADDRESS belum dikonfigurasi.")
        result = await self._rpc("getAddressInformation", {"address": self.wallet_address})
        if not isinstance(result, dict) or "balance" not in result:
            raise RuntimeError("RPC TON gagal membaca saldo TON.")
        return int(result["balance"]) / 1e9

    async def _get_jetton_balance(self, jetton_master: str) -> float:
        if not self.wallet_address:
            raise RuntimeError("TON_WALLET_ADDRESS belum dikonfigurasi.")
        # Coba via tonapi.io v2 API (standar industri untuk Jetton)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.get(f"https://tonapi.io/v2/accounts/{self.wallet_address}/jettons")
                if res.status_code == 200:
                    data = res.json()["balances"]
                    if not isinstance(data, list):
                        raise ValueError("Daftar saldo Jetton tidak valid.")
                    for item in data:
                        jetton_info = item.get("jetton", {})
                        master_addr = jetton_info.get("address", "")
                        from services.tx_verifier import ton_address
                        if ton_address(master_addr) == ton_address(jetton_master):
                            raw_bal = int(item.get("balance", 0))
                            decimals = int(jetton_info.get("decimals", 6))
                            return float(raw_bal / (10 ** decimals))
                    return 0.0
        except Exception as tonapi_err:
            logger.warning(f"tonapi get_jetton_balance error: {tonapi_err}")

        raise RuntimeError("API TON gagal membaca saldo Jetton.")

    # ---------------- Send ----------------
    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Mengirim TON native atau Jetton (USDT) di TON Network (broadcast real)."""
        try:
            from services.tx_verifier import ton_address
            from services.crypto_sender.evm_sender import _get_network_send_lock
            ton_address(to_address)
            quantity = Decimal(str(amount))
            if symbol.upper() not in ("TON", "USDT") or not quantity.is_finite() or quantity <= 0:
                raise ValueError("Nominal/aset TON tidak valid.")
            async with _get_network_send_lock("TON"):
                if symbol.upper() == "USDT":
                    return await self._send_jetton(to_address, amount)
                return await self._send_native(to_address, amount)
        except Exception as e:
            logger.warning("Preflight TON gagal (%s)", type(e).__name__)
            return SendResult(success=False, error_message=f"MANUAL_REVIEW: Konfigurasi/preflight TON gagal ({type(e).__name__}).")

    def _load_wallet(self):
        """Load wallet TON v4r2 dari mnemonic atau hex private key di settings.TON_PRIVATE_KEY."""
        try:
            from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        except ImportError:
            raise RuntimeError("Library tonsdk tidak terinstall (pip install tonsdk).")
        if not settings.TON_PRIVATE_KEY:
            raise RuntimeError("TON_PRIVATE_KEY belum di-set di .env (mnemonic / hex wallet TON).")
        
        raw_key = settings.TON_PRIVATE_KEY.strip()
        words = raw_key.split()
        if len(words) in (12, 24):
            from tonsdk.crypto import mnemonic_to_wallet_key
            pub_k, priv_k = mnemonic_to_wallet_key(words)
        else:
            import nacl.signing
            clean_hex = raw_key.replace("0x", "")
            key_bytes = bytes.fromhex(clean_hex)
            if len(key_bytes) == 32:
                signing_key = nacl.signing.SigningKey(key_bytes)
                priv_k = signing_key.encode() + signing_key.verify_key.encode()
                pub_k = signing_key.verify_key.encode()
            elif len(key_bytes) == 64:
                priv_k = key_bytes
                pub_k = key_bytes[32:]
            else:
                raise ValueError(f"Panjang private key TON tidak valid ({len(key_bytes)} bytes).")

        wallet = Wallets.ALL[WalletVersionEnum.v4r2](
            public_key=pub_k, private_key=priv_k, wc=0
        )
        from services.tx_verifier import ton_address
        if ton_address(wallet.address.to_string(False)) != ton_address(self.wallet_address):
            raise ValueError("Key/wallet version TON tidak cocok dengan alamat stok.")
        return wallet

    async def _get_seqno(self, address: str) -> int:
        state = await self._rpc("getAddressInformation", {"address": address})
        if isinstance(state, dict) and state.get("state") == "uninitialized":
            return 0
        result = await self._rpc("runGetMethod", {"address": address, "method": "seqno", "stack": []})
        if not isinstance(result, dict) or result.get("exit_code") != 0:
            raise RuntimeError("Seqno TON tidak dapat diverifikasi.")
        return int(result["stack"][0][1], 16)

    async def _send_boc(self, boc_bytes: bytes) -> bool:
        boc_b64 = base64.b64encode(boc_bytes).decode()
        # One broadcast attempt only. A timeout may already have sent the BOC.
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "sendBoc", "params": {"boc": boc_b64}},
                headers={"X-API-Key": self.api_key} if self.api_key else {})
            response.raise_for_status()
            data = response.json()
            if data.get("ok") is not True:
                raise RuntimeError("Broadcast TON belum terkonfirmasi.")
        return True

    async def _broadcast_transfer(self, transfer, to_address, amount, symbol):
        from services.tx_verifier import _ton_get, verify_deposit, normalize_tx_hash
        message_hash = transfer["message"].bytes_hash().hex()
        # This is a message identifier, NOT a confirmed transaction hash.
        reference = "msg:" + message_hash
        try:
            await self._send_boc(transfer["message"].to_boc(False))
            for _ in range(12):
                data = await _ton_get("transactionsByMessage", {"msg_hash": message_hash, "direction": "in", "limit": 10})
                for row in data.get("transactions", []):
                    tx_hash = normalize_tx_hash("TON", row["hash"])
                    reference = tx_hash
                    result = await verify_deposit("TON", symbol, tx_hash, to_address, amount)
                    if result.get("verified"):
                        return SendResult(True, tx_hash, explorer_url=f"{self.explorer_base}/transaction/{tx_hash}")
                await asyncio.sleep(2)
        except Exception as exc:
            logger.warning("Receipt TON belum pasti (%s)", type(exc).__name__)
        return SendResult(False, reference,
            "MANUAL_REVIEW: Periksa receipt/trace TON sebelum kirim ulang; broadcast sudah dicoba.",
            "" if reference.startswith("msg:") else f"{self.explorer_base}/transaction/{reference}")

    async def _send_native(self, to_address: str, amount: float) -> SendResult:
        wallet = self._load_wallet()
        seqno = await self._get_seqno(self.wallet_address)
        transfer = wallet.create_transfer_message(
            to_address, int(Decimal(str(amount)) * 10**9), seqno=seqno)
        return await self._broadcast_transfer(transfer, to_address, amount, "TON")

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
            to_addr=matching[0]["address"], amount=50_000_000, seqno=seqno, payload=body)
        return await self._broadcast_transfer(transfer, to_address, amount, "USDT")
