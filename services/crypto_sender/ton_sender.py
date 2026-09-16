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

        if not self.wallet_address:
            return 0.0
        result = await self._rpc("getAddressInformation", {"address": self.wallet_address})
        if result:
            try:
                return float(int(result.get("balance") or 0) / 1e9)
            except (ValueError, TypeError):
                return 0.0
        return 0.0

    async def _get_jetton_balance(self, jetton_master: str) -> float:
        if not self.wallet_address:
            return 0.0
        # Coba via tonapi.io v2 API (standar industri untuk Jetton)
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.get(f"https://tonapi.io/v2/accounts/{self.wallet_address}/jettons")
                if res.status_code == 200:
                    data = res.json().get("balances", [])
                    for item in data:
                        jetton_info = item.get("jetton", {})
                        master_addr = jetton_info.get("address", "")
                        sym = jetton_info.get("symbol", "").upper()
                        if master_addr == jetton_master or sym in ("USDT", "USD₮"):
                            raw_bal = int(item.get("balance", 0))
                            decimals = int(jetton_info.get("decimals", 6))
                            return float(raw_bal / (10 ** decimals))
                    return 0.0
        except Exception as tonapi_err:
            logger.warning(f"tonapi get_jetton_balance error: {tonapi_err}")

        return 0.0

    # ---------------- Send ----------------
    async def send(self, to_address: str, amount: float, symbol: str) -> SendResult:
        """Mengirim TON native atau Jetton (USDT) di TON Network (broadcast real)."""
        try:
            if symbol.upper() == "USDT":
                return await self._send_jetton(to_address, amount)
            return await self._send_native(to_address, amount)
        except Exception as e:
            logger.error(f"Error pengiriman TON: {e}", exc_info=True)
            return SendResult(success=False, error_message=f"Exception pengiriman TON: {str(e)}")

    def _load_wallet(self):
        """Load wallet TON v4r2 dari mnemonic di settings.TON_PRIVATE_KEY."""
        try:
            from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        except ImportError:
            raise RuntimeError("Library tonsdk tidak terinstall (pip install tonsdk).")
        if not settings.TON_PRIVATE_KEY:
            raise RuntimeError("TON_PRIVATE_KEY belum di-set di .env (mnemonic wallet TON).")
        from tonsdk.crypto import mnemonic_to_wallet_key
        mnemonics = settings.TON_PRIVATE_KEY.strip().split()
        pub_k, priv_k = mnemonic_to_wallet_key(mnemonics)
        return Wallets.ALL[WalletVersionEnum.v4r2](
            public_key=pub_k, private_key=priv_k, wc=0
        )

    async def _get_seqno(self, address: str) -> int:
        result = await self._rpc("getSeqno", {"address": address})
        try:
            return int(result or 0)
        except (ValueError, TypeError):
            return 0

    async def _send_boc(self, boc_bytes: bytes) -> bool:
        boc_b64 = base64.b64encode(boc_bytes).decode()
        result = await self._rpc("sendBoc", {"boc": boc_b64})
        return result is not None

    async def _get_latest_outgoing_hash(self) -> str:
        """Ambil TX hash transaksi keluar terbaru dari wallet (validasi broadcast)."""
        txs = await self._rpc("getTransactions", {"address": self.wallet_address, "limit": 5})
        if not isinstance(txs, list):
            return ""
        for txn in txs:
            in_msg = txn.get("in_msg") or {}
            if (in_msg.get("source") or "") == self.wallet_address:
                return (txn.get("transaction_id") or {}).get("hash") or ""
        return ""

    async def _send_native(self, to_address: str, amount: float) -> SendResult:
        if not self.validate_address(to_address):
            return SendResult(success=False, error_message="Alamat TON tidak valid.")
        wallet = self._load_wallet()
        seqno = await self._get_seqno(self.wallet_address)
        amount_nano = int(amount * 1e9)  # 1 TON = 1e9 nanoTON
        transfer = wallet.create_transfer_message(to_address, amount_nano, seqno=seqno)
        boc = transfer["message"].to_boc(False)
        ok = await self._send_boc(boc)
        if not ok:
            return SendResult(success=False, error_message="Gagal broadcast transaksi ke Toncenter.")

        await asyncio.sleep(2)
        tx_hash = await self._get_latest_outgoing_hash() or transfer["message"].bytes_hash().hex()
        logger.info(f"TON native terkirim: {amount} TON ke {to_address} (hash {tx_hash})")
        return SendResult(
            success=True,
            tx_hash=tx_hash,
            explorer_url=f"{self.explorer_base}/transaction/{tx_hash}",
        )

    async def _send_jetton(self, to_address: str, amount: float) -> SendResult:
        try:
            from tonsdk.contract.token.ft import JettonWallet
        except ImportError:
            return SendResult(success=False, error_message="Library tonsdk tidak terinstall.")
        if not settings.TON_PRIVATE_KEY:
            return SendResult(success=False, error_message="TON_PRIVATE_KEY belum di-set di .env.")

        # Resolve alamat jetton wallet milik owner (USDT TON)
        jetton_wallet_addr = await self._rpc(
            "getAccountAddress",
            {"jetton_master": USDT_JETTON_MASTER, "owner": self.wallet_address},
        )
        if not jetton_wallet_addr:
            return SendResult(success=False, error_message="Gagal resolve jetton wallet TON.")

        wallet = self._load_wallet()
        seqno = await self._get_seqno(self.wallet_address)

        # Body transfer jetton (USDT TON = 6 decimals), forward fee 0.01 TON
        jetton = JettonWallet()
        body = jetton.create_transfer_body(
            to_address=to_address,
            jetton_amount=int(amount * 1e6),
            forward_amount=int(JETTON_FORWARD_TON * 1e9),
            response_address=self.wallet_address,
        )
        # Owner wallet kirim pesan ke jetton wallet dengan body transfer
        transfer = wallet.create_transfer_message(
            to_addr=jetton_wallet_addr,
            amount=int(JETTON_FORWARD_TON * 1e9),
            seqno=seqno,
            payload=body,
        )
        boc = transfer["message"].to_boc(False)
        ok = await self._send_boc(boc)
        if not ok:
            return SendResult(success=False, error_message="Gagal broadcast jetton ke Toncenter.")

        await asyncio.sleep(2)
        tx_hash = await self._get_latest_outgoing_hash() or transfer["message"].bytes_hash().hex()
        logger.info(f"TON jetton (USDT) terkirim: {amount} ke {to_address} (hash {tx_hash})")
        return SendResult(
            success=True,
            tx_hash=tx_hash,
            explorer_url=f"{self.explorer_base}/transaction/{tx_hash}",
        )
