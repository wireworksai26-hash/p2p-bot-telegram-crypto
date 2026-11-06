"""Wallet TON KHUSUS pembelian Fragment (Stars/Premium).

Memakai kunci dan alamat sendiri (STARS_TON_PRIVATE_KEY / STARS_TON_WALLET_ADDRESS), terpisah dari wallet stok
GRAM bot, dan sebaiknya hanya berisi saldo kecil. Mengirim satu pesan internal dengan payload dari Fragment
(wallet v5r1 atau v4r2), tanpa pernah broadcast ulang: hasil yang tidak pasti dikembalikan sebagai
SendResult dengan reference (pesan sudah mungkin keluar) agar pemanggil menyerahkannya ke admin.

Konvensi hasil: SendResult.tx_hash kosong = PASTI belum ada yang keluar; terisi (msg:...) = mungkin sudah keluar.
"""
import base64
import logging
import time
from decimal import Decimal

from config.settings import settings
from services.crypto_sender import SendResult
from services.crypto_sender.ton_sender import (
    NANO, TonBroadcastRejected, TonBroadcastUncertain, TonSender,
    W5_SIGNED_EXTERNAL_OPCODE, W5_UNLIMITED_VALID_UNTIL,
)

logger = logging.getLogger(__name__)

GAS_RESERVE_TON = Decimal("0.02")  # sisa saldo minimum di atas nominal pembayaran (biaya jaringan)


def decode_payload(payload_b64: str) -> bytes:
    """Payload Fragment berupa base64 (kadang urlsafe, tanpa padding) dari sel BOC."""
    text = (payload_b64 or "").strip()
    if not text:
        return b""
    text += "=" * (-len(text) % 4)
    return base64.b64decode(text, altchars=b"-_", validate=False)


class FragmentWalletSender(TonSender):
    def __init__(self):
        super().__init__()
        self.wallet_address = settings.STARS_TON_WALLET_ADDRESS

    def _raw_private_key(self) -> str:
        return (settings.STARS_TON_PRIVATE_KEY or "").strip()

    def configured(self) -> bool:
        return bool(self._raw_private_key() and self.wallet_address)

    def public_key_hex(self) -> str:
        return self._load_key()[2].hex()

    def verify_key_matches_address(self) -> str:
        """Versi wallet ('v5r1'/'v4r2') bila kunci cocok dengan alamat; raise bila tidak."""
        return self._resolve_wallet()["version"]

    async def send_payload(self, to_address: str, amount_nano: int, payload_b64: str) -> SendResult:
        """Kirim `amount_nano` ke `to_address` dengan payload Fragment. Sekali jalan, tanpa broadcast ulang."""
        from services.crypto_sender.evm_sender import _get_network_send_lock
        from services.tx_verifier import ton_address

        try:
            ton_address(to_address)
            amount_nano = int(amount_nano)
            if amount_nano <= 0:
                raise ValueError("nominal tidak valid")
            body_bytes = decode_payload(payload_b64)
            wallet = self._resolve_wallet()
        except Exception as exc:
            logger.warning("Preflight wallet Fragment gagal (%s): %s", type(exc).__name__, str(exc)[:160])
            return SendResult(False, "", f"MANUAL_REVIEW: Preflight wallet Fragment gagal ({str(exc)[:160]}).")

        try:
            balance = Decimal(str(await self.get_balance("TON")))
        except Exception as exc:
            return SendResult(False, "", f"MANUAL_REVIEW: Saldo wallet Fragment tidak terbaca ({type(exc).__name__}).")
        need = Decimal(amount_nano) / Decimal(NANO) + GAS_RESERVE_TON
        if balance < need:
            return SendResult(False, "", f"MANUAL_REVIEW: Saldo wallet Fragment kurang ({balance} TON, butuh {need}).")

        async with _get_network_send_lock("TON"):
            if wallet["version"] == "v5r1":
                return await self._send_payload_w5(wallet, to_address, amount_nano, body_bytes)
            return await self._send_payload_v4(to_address, amount_nano, body_bytes)

    def _delivery_amount(self, amount_nano: int) -> Decimal:
        return Decimal(amount_nano) / Decimal(NANO)

    async def _send_payload_w5(self, wallet: dict, to_address: str, amount_nano: int, body_bytes: bytes) -> SendResult:
        from pytoniq.contract.contract import Contract
        from pytoniq.contract.wallets.wallet_v5 import WalletV5R1
        from pytoniq_core.boc import Builder, Cell
        from pytoniq_core.boc.address import Address
        from pytoniq_core.crypto.signature import sign_message

        reference = ""
        broadcast_started = False
        try:
            body = Cell.one_from_boc(body_bytes) if body_bytes else Builder().end_cell()
            message = WalletV5R1.create_wallet_internal_message(
                destination=Address(to_address), value=amount_nano, body=body)
            reference = "msg:" + message.serialize().hash.hex()
            since_ts = int(time.time())
            seqno = await self._get_seqno(self.wallet_address)
            state_init = wallet["state_init"] if await self._account_status() != "active" else None
            valid_until = W5_UNLIMITED_VALID_UNTIL if seqno == 0 else int(time.time()) + 60
            signing_cell = (
                Builder().store_uint(W5_SIGNED_EXTERNAL_OPCODE, 32).store_uint(wallet["wallet_id"], 32)
                .store_uint(valid_until, 32).store_uint(seqno, 32)
                .store_cell(WalletV5R1.pack_actions([message])).end_cell()
            )
            signature = sign_message(signing_cell.hash, wallet["priv"])
            external = Contract.create_external_msg(
                dest=Address(wallet["address"]), state_init=state_init,
                body=Builder().store_cell(signing_cell).store_bytes(signature).end_cell())
            broadcast_started = True  # mulai sini uang bisa keluar: kegagalan berikutnya tidak pasti
            provider = await self._broadcast(external.serialize().to_boc())
            logger.info("Broadcast pembayaran Fragment via %s (ref %s)", provider, reference)
            return await self._await_delivery(to_address, self._delivery_amount(amount_nano), "TON", since_ts, reference)
        except TonBroadcastRejected as exc:
            return SendResult(False, "", f"MANUAL_REVIEW: {str(exc)[:250]}")
        except TonBroadcastUncertain as exc:
            return SendResult(False, reference, f"MANUAL_REVIEW: {str(exc)[:200]}")
        except Exception as exc:
            # Sebelum broadcast (reference belum ada) = pasti belum keluar; sesudahnya tidak pasti.
            logger.warning("Pembayaran Fragment v5r1 gagal (%s): %s", type(exc).__name__, str(exc)[:200])
            return SendResult(False, reference if broadcast_started else "",
                              f"MANUAL_REVIEW: Pembayaran Fragment gagal ({type(exc).__name__}).")

    async def _send_payload_v4(self, to_address: str, amount_nano: int, body_bytes: bytes) -> SendResult:
        try:
            from tonsdk.boc import Cell as TonCell

            wallet = self._load_wallet()
            seqno = await self._get_seqno(self.wallet_address)
            payload = TonCell.one_from_boc(body_bytes) if body_bytes else None
            transfer = wallet.create_transfer_message(to_address, amount_nano, seqno=seqno, payload=payload)
        except Exception as exc:
            return SendResult(False, "", f"MANUAL_REVIEW: Menyusun pembayaran Fragment gagal ({type(exc).__name__}).")
        return await self._broadcast_transfer(transfer, to_address, self._delivery_amount(amount_nano), "TON")
