"""Read-only, fail-closed deposit verification. Never trusts screenshots or wallet totals."""
import asyncio
import base64
import binascii
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse, unquote

import base58
import httpx

from config.settings import settings
from config.assets import NON_EVM_TOKENS
from services.crypto_sender import CryptoSenderFactory

logger = logging.getLogger(__name__)
EVM_NETWORKS = {"BSC", "ETH", "AVAX", "POLYGON", "BASE", "ARB", "OPTIMISM",
                "ROBINHOOD", "KAIA", "BERA", "HYPEREVM"}
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SOLANA_RPCS = list(dict.fromkeys([settings.SOL_RPC, "https://solana-rpc.publicnode.com",
                                 "https://api.mainnet-beta.solana.com"]))
TRONGRID_URL = settings.TRX_RPC.rstrip("/")
# Serialise unauthenticated TON requests to respect the public 1 RPS limit.
_ton_lock = asyncio.Lock()
_ton_last_request = 0.0


def _scan_web3(rpc_url: str):
    """Web3 khusus scan deposit: endpoint bisa dipilih + middleware PoA (BSC/AVAX dll).

    Tanpa middleware PoA, web3 menolak blok BSC dengan ExtraDataLengthError.
    """
    from web3 import Web3
    try:
        from web3.middleware import ExtraDataToPOAMiddleware as poa_middleware
    except ImportError:
        from web3.middleware import geth_poa_middleware as poa_middleware
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 12}))
    try:
        if poa_middleware not in w3.middleware_onion:
            w3.middleware_onion.inject(poa_middleware, layer=0)
    except Exception:
        pass
    return w3


def normalize_tx_hash(network, value):
    value = (value or "").strip()
    if "://" in value:
        parsed = urlparse(value)
        # Extract only a hash. Never fetch a user-supplied URL.
        value = unquote(parsed.path.rstrip("/").split("/")[-1])
    net = network.upper()
    if net in EVM_NETWORKS or net in {"TRON", "APTOS"}:
        value = value.removeprefix("0x")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("TX hash harus 64 digit hex.")
        return value.lower() if net == "TRON" else "0x" + value.lower()
    if net == "TON":
        value = value.removeprefix("0x")
        if re.fullmatch(r"[0-9a-fA-F]{64}", value):
            return value.lower()
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if len(decoded) != 32:
            raise ValueError("Hash TON tidak valid.")
        return decoded.hex()
    if net in {"SOLANA", "SUI"}:
        if len(base58.b58decode(value)) != (64 if net == "SOLANA" else 32):
            raise ValueError("Hash base58 tidak valid.")
        return value
    raise ValueError("Jaringan tidak didukung.")


def ton_address(value):
    if re.fullmatch(r"-?\d+:[0-9a-fA-F]{64}", value or ""):
        wc, account = value.split(":")
        return f"{int(wc)}:{account.lower()}"
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) != 36 or binascii.crc_hqx(raw[:34], 0).to_bytes(2, "big") != raw[34:]:
        raise ValueError("Alamat TON tidak valid.")
    # Reject test-only addresses and unknown tags.
    if raw[0] not in (0x11, 0x51):
        raise ValueError("Alamat bukan TON mainnet.")
    return f"{int.from_bytes(raw[1:2], 'big', signed=True)}:{raw[2:34].hex()}"


def _move_address(value):
    return "0x" + value.lower().removeprefix("0x").zfill(64)


def _hex(value):
    return (value.hex() if isinstance(value, bytes) else str(value)).lower().removeprefix("0x")


def _timestamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
    return float(value)


def _fail(reason):
    return {"verified": False, "amount": 0.0, "from_address": "", "reason": reason}


def _ok(amount, stamp, tx_hash, from_address=""):
    return {"verified": True, "amount": float(amount), "timestamp": float(stamp),
            "tx_hash": tx_hash, "from_address": from_address, "reason": "OK"}


def _amount_matches(received, expected):
    received, expected = Decimal(str(received)), Decimal(str(expected))
    return received.is_finite() and expected.is_finite() and expected > 0 and received >= expected


def automatic_amount_matches(received, expected):
    # Shared-wallet discovery must not claim an unrelated larger transfer.
    return Decimal(str(received)).quantize(Decimal("0.00000001")) == Decimal(str(expected)).quantize(Decimal("0.00000001"))


async def _json(method, url, **kwargs):
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.request(method, url, **kwargs)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError("Provider returned an error.")
        return data


async def _rpc(url, method, params):
    data = await _json("POST", url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if data.get("result") is None:
        raise RuntimeError("RPC result belum tersedia.")
    return data["result"]


async def _sol_rpc(method, params):
    for rpc in SOLANA_RPCS:
        try:
            return await _rpc(rpc, method, params)
        except Exception:
            continue
    raise RuntimeError("Semua RPC Solana gagal.")


async def _tron(method, tx_hash):
    return await _json("POST", f"{TRONGRID_URL}/walletsolidity/{method}",
                       json={"value": tx_hash},
                       headers={"TRON-PRO-API-KEY": settings.TRONGRID_API_KEY} if settings.TRONGRID_API_KEY else {})


def _tron_address(value):
    if value.startswith("T"):
        return value
    raw = bytes.fromhex(value.removeprefix("0x"))
    if len(raw) == 20:
        raw = b"\x41" + raw
    return base58.b58encode_check(raw).decode()


async def _ton_get(path, params):
    global _ton_last_request
    async with _ton_lock:
        loop = asyncio.get_running_loop()
        if not settings.TON_API_KEY:
            await asyncio.sleep(max(0, 1.05 - (loop.time() - _ton_last_request)))
        try:
            return await _json("GET", f"{settings.TON_INDEXER_URL}/{path}", params=params,
                               headers={"X-API-Key": settings.TON_API_KEY} if settings.TON_API_KEY else {})
        finally:
            _ton_last_request = loop.time()


EXPLORER_V2_API = "https://api.etherscan.io/v2/api"
EXPLORER_CHAIN_IDS = {
    "ETH": 1, "BSC": 56, "POLYGON": 137, "BASE": 8453,
    "ARB": 42161, "OPTIMISM": 10, "AVAX": 43114,
}
# Decimals cadangan bila eth_call tidak tersedia saat fallback explorer dipakai.
EXPLORER_TOKEN_DECIMALS = {
    ("BSC", "USDT"): 18, ("BSC", "USDC"): 18,
    ("ETH", "USDT"): 6, ("ETH", "USDC"): 6,
    ("POLYGON", "USDT"): 6, ("POLYGON", "USDC"): 6,
    ("BASE", "USDC"): 6, ("ARB", "USDT"): 6, ("ARB", "USDC"): 6,
    ("OPTIMISM", "USDC"): 6, ("AVAX", "USDT"): 6, ("ROBINHOOD", "USDG"): 6,
}


async def _explorer_proxy(chain_id, action, **params):
    """Panggil Etherscan V2 (multichain) sebagai fallback verifikasi."""
    if not settings.ETHERSCAN_API_KEY:
        return None
    data = await _json("GET", EXPLORER_V2_API, params={
        "chainid": chain_id, "module": "proxy", "action": action,
        "apikey": settings.ETHERSCAN_API_KEY, **params})
    if str(data.get("status")) != "1":
        raise RuntimeError(str(data.get("result"))[:140])
    return data.get("result")


async def _verify_evm_via_explorer(network, symbol, tx_hash, wallet):
    """Fallback bila semua RPC publik gagal (kuota/arsip). None bila tidak tersedia."""
    chain_id = EXPLORER_CHAIN_IDS.get(network)
    if not chain_id or not settings.ETHERSCAN_API_KEY:
        return None
    sender = CryptoSenderFactory.get_sender(network)
    try:
        receipt = await _explorer_proxy(chain_id, "eth_getTransactionReceipt", txhash=tx_hash)
        if not isinstance(receipt, dict) or not receipt.get("blockNumber"):
            return None
        if int(receipt.get("status", "0x0"), 16) != 1:
            return _fail("Transaksi gagal atau belum confirmed.")
        block_no = int(receipt["blockNumber"], 16)
        latest_raw = await _explorer_proxy(chain_id, "eth_blockNumber")
        if latest_raw and int(latest_raw, 16) - block_no + 1 < settings.EVM_DEPOSIT_CONFIRMATIONS:
            return _fail("Menunggu konfirmasi blockchain.")
        block = await _explorer_proxy(
            chain_id, "eth_getBlockByNumber", tag=receipt["blockNumber"], boolean="false")
        timestamp = int(block["timestamp"], 16)

        amount = Decimal(0)
        if symbol == sender.config["native_symbol"]:
            tx = await _explorer_proxy(chain_id, "eth_getTransactionByHash", txhash=tx_hash)
            if (tx.get("to") or "").lower() != wallet.lower():
                return _fail("Penerima tidak cocok.")
            if (tx.get("from") or "").lower() == wallet.lower():
                return _fail("Transfer ke diri sendiri bukan deposit.")
            amount = Decimal(int(tx["value"], 16)) / 10**18
        else:
            token = sender.config["tokens"].get(symbol)
            if not token:
                return _fail("Token tidak terdaftar.")
            decimals = EXPLORER_TOKEN_DECIMALS.get((network, symbol))
            if decimals is None:
                from services.crypto_sender.evm_sender import ERC20_ABI
                w3 = sender.w3
                contract = w3.eth.contract(address=w3.to_checksum_address(token), abi=ERC20_ABI)
                decimals = await asyncio.to_thread(contract.functions.decimals().call)
            for log in receipt.get("logs", []):
                topics = log.get("topics", [])
                if (log.get("address") or "").lower() != token.lower() or len(topics) != 3:
                    continue
                if _hex(topics[0]) != TRANSFER_TOPIC or "0x" + _hex(topics[2])[-40:] != wallet.lower():
                    continue
                if "0x" + _hex(topics[1])[-40:] == wallet.lower():
                    continue
                amount += Decimal(int(_hex(log["data"]), 16)) / (10**decimals)
        return _ok(amount, timestamp, tx_hash)
    except Exception as exc:
        logger.warning("Verifikasi explorer %s gagal (%s)", network, type(exc).__name__)
        return None


async def _verify_evm(network, symbol, tx_hash, wallet):
    sender = CryptoSenderFactory.get_sender(network)
    symbol = "MATIC" if network == "POLYGON" and symbol == "POL" else symbol
    for rpc_url in _ordered_rpcs(network, sender.rpc_list):
        w3 = _scan_web3(rpc_url)
        try:
            if await asyncio.to_thread(lambda: w3.eth.chain_id) != sender.config["chain_id"]:
                raise ValueError("Chain ID tidak sesuai.")
            receipt = await asyncio.to_thread(w3.eth.get_transaction_receipt, tx_hash)
            # RPC sudah menjawab benar (chain id cocok + receipt ada) meski hash
            # nantinya ditolak secara semantik; simpan agar tidak dicoba ulang.
            _scan_rpc_cache[network] = rpc_url
            if receipt.get("status") != 1:
                return _fail("Transaksi gagal atau belum confirmed.")
            block_no = receipt["blockNumber"]
            latest = await asyncio.to_thread(lambda: w3.eth.block_number)
            if latest - block_no + 1 < settings.EVM_DEPOSIT_CONFIRMATIONS:
                return _fail("Menunggu konfirmasi blockchain.")
            block = await asyncio.to_thread(w3.eth.get_block, block_no)
            amount = Decimal(0)
            if symbol == sender.config["native_symbol"]:
                tx = await asyncio.to_thread(w3.eth.get_transaction, tx_hash)
                if (tx.get("to") or "").lower() != wallet.lower():
                    return _fail("Penerima tidak cocok.")
                if (tx.get("from") or "").lower() == wallet.lower():
                    return _fail("Transfer ke diri sendiri bukan deposit.")
                amount = Decimal(tx["value"]) / 10**18
            else:
                token = sender.config["tokens"].get(symbol)
                if not token:
                    return _fail("Token tidak terdaftar.")
                from services.crypto_sender.evm_sender import ERC20_ABI
                contract = w3.eth.contract(address=w3.to_checksum_address(token), abi=ERC20_ABI)
                decimals = await asyncio.to_thread(contract.functions.decimals().call)
                self_send = False
                for log in receipt.get("logs", []):
                    topics = log.get("topics", [])
                    if (log.get("address") or "").lower() != token.lower() or len(topics) != 3:
                        continue
                    if _hex(topics[0]) != TRANSFER_TOPIC or "0x" + _hex(topics[2])[-40:] != wallet.lower():
                        continue
                    if "0x" + _hex(topics[1])[-40:] == wallet.lower():
                        self_send = True
                        continue
                    amount += Decimal(int(_hex(log["data"]), 16)) / (10**decimals)
                if amount == 0:
                    if self_send:
                        return _fail("Transfer ke diri sendiri bukan deposit.")
                    return _fail("Tidak ada transfer token masuk ke wallet deposit pada transaksi ini.")
            return _ok(amount, block["timestamp"], tx_hash)
        except Exception as exc:
            logger.warning("Verifikasi RPC %s via %s gagal: %s", network, rpc_url, exc)
    fallback = await _verify_evm_via_explorer(network, symbol, tx_hash, wallet)
    if fallback is not None:
        return fallback
    return _fail("RPC EVM gagal memverifikasi receipt/chain.")


async def _verify_solana(symbol, tx_hash, wallet):
    result = await _sol_rpc("getTransaction", [tx_hash, {"encoding": "jsonParsed",
                           "commitment": "finalized", "maxSupportedTransactionVersion": 0}])
    meta = result.get("meta")
    if not isinstance(meta, dict) or "err" not in meta or meta["err"] is not None:
        return _fail("Transaksi Solana gagal/belum lengkap.")
    msg = result["transaction"]["message"]
    if symbol == "SOL":
        keys = [key["pubkey"] if isinstance(key, dict) else key for key in msg["accountKeys"]]
        if wallet not in keys:
            return _fail("Penerima SOL tidak ada.")
        index = keys.index(wallet)
        net_gain = int(meta["postBalances"][index]) - int(meta["preBalances"][index])
        instructions = list(msg.get("instructions", []))
        for inner in meta.get("innerInstructions") or []:
            instructions += inner.get("instructions", [])
        transferred = 0
        for instruction in instructions:
            parsed = instruction.get("parsed") or {}
            info = parsed.get("info") or {}
            if instruction.get("program") == "system" and parsed.get("type") in ("transfer", "transferWithSeed"):
                if info.get("destination") == wallet and info.get("source") != wallet:
                    transferred += int(info.get("lamports", 0))
        amount = Decimal(min(net_gain, transferred)) / 10**9
    else:
        mint = NON_EVM_TOKENS["SOLANA"].get(symbol)
        if not mint:
            return _fail("Mint SPL tidak terdaftar.")
        balances = {}
        for side, sign in (("postTokenBalances", 1), ("preTokenBalances", -1)):
            for item in meta.get(side) or []:
                if item.get("mint") == mint and item.get("owner") == wallet:
                    token = item["uiTokenAmount"]
                    balances[item["accountIndex"]] = balances.get(item["accountIndex"], Decimal(0)) + sign * Decimal(token["amount"]) / (10**int(token["decimals"]))
        amount = sum(balances.values(), Decimal(0))
    return _ok(amount, result["blockTime"], tx_hash)


async def _verify_tron(symbol, tx_hash, wallet):
    info = await _tron("gettransactioninfobyid", tx_hash)
    tx = await _tron("gettransactionbyid", tx_hash)
    if info.get("id") != tx_hash or not info.get("blockTimeStamp") or not info.get("blockNumber"):
        return _fail("Receipt TRON belum confirmed.")
    if not tx.get("ret") or any(r.get("contractRet") != "SUCCESS" for r in tx["ret"]):
        return _fail("Transaksi TRON gagal.")
    if info.get("result") == "FAILED" or info.get("receipt", {}).get("result") not in (None, "SUCCESS"):
        return _fail("Receipt TRON gagal.")
    amount = Decimal(0)
    if symbol == "TRX":
        for contract in tx.get("raw_data", {}).get("contract", []):
            value = contract.get("parameter", {}).get("value", {})
            if contract.get("type") == "TransferContract" and _tron_address(value.get("to_address", "")) == wallet and _tron_address(value.get("owner_address", "")) != wallet:
                amount += Decimal(value["amount"]) / 10**6
    elif symbol == "USDT":
        for log in info.get("log", []):
            topics = log.get("topics", [])
            if _tron_address(log.get("address", "")) != NON_EVM_TOKENS["TRON"]["USDT"] or len(topics) != 3:
                continue
            if _hex(topics[0]) == TRANSFER_TOPIC and _tron_address(_hex(topics[2])[-40:]) == wallet and _tron_address(_hex(topics[1])[-40:]) != wallet:
                amount += Decimal(int(log["data"], 16)) / 10**6
    return _ok(amount, info["blockTimeStamp"] / 1000, tx_hash)


def _ton_native_evidence(tx, wallet):
    msg = tx.get("in_msg") or {}
    description = tx.get("description") or {}
    if tx.get("emulated") is True or msg.get("bounced") is True:
        return _fail("Transaksi TON belum berhasil atau bounced.")

    # Periksa apakah ada pesan bounce keluar yang mengembalikan dana
    for out in tx.get("out_msgs") or []:
        if out.get("bounced") is True or out.get("decoded_opcode") == "bounce" or str(out.get("opcode")) in ("0xffffffff", "-1"):
            return _fail("Transaksi TON di-bounce balik ke pengirim.")

    compute = description.get("compute_ph") or {}
    if compute.get("skipped") is True:
        # Transfer biasa ke dompet uninit men-skip compute phase (reason: no_state).
        # Protokol TON menetapkan aborted: True meski saldo masuk secara sah via credit phase.
        if compute.get("reason") not in ("no_state", None):
            return _fail("Transaksi TON compute phase skipped bukan no_state.")
    else:
        if not compute.get("success") or description.get("aborted") is True:
            return _fail("Transaksi TON gagal atau aborted.")

    if not msg.get("source") or ton_address(msg["source"]) == ton_address(wallet) or ton_address(tx["account"]) != ton_address(wallet) or ton_address(msg["destination"]) != ton_address(wallet):
        return _fail("Penerima TON tidak cocok.")
    # Contract messages can carry attached TON for gas; accept simple transfers only.
    if msg.get("opcode") not in (None, 0, "0", "0x00000000"):
        return _fail("Pesan kontrak bukan transfer TON biasa.")
    return _ok(Decimal(msg["value"]) / 10**9, tx["now"], normalize_tx_hash("TON", tx["hash"]), msg["source"])


async def _verify_ton(symbol, tx_hash, wallet):
    if symbol == "TON":
        data = await _ton_get("transactions", {"hash": tx_hash, "limit": 1})
        for tx in data.get("transactions", []):
            direct = _ton_native_evidence(tx, wallet)
            if direct["verified"]:
                return direct
            # Wallet explorers may return the sending account's transaction hash.
            adjacent = await _ton_get("adjacentTransactions", {"hash": tx_hash, "direction": "out", "limit": 100})
            outgoing = {msg["hash"] for msg in tx.get("out_msgs", []) if msg.get("hash")}
            for child in adjacent.get("transactions", []):
                if (child.get("in_msg") or {}).get("hash") in outgoing:
                    evidence = _ton_native_evidence(child, wallet)
                    if evidence["verified"]:
                        return evidence
        return _fail("Transfer TON masuk belum ditemukan.")
    if symbol != "USDT":
        return _fail("Jetton tidak didukung.")
    # Verify the receiving Jetton contract, not only the owner's successful
    # transfer request. A later hop can bounce even when that request succeeded.
    wallets = await _ton_get("jetton/wallets", {"owner_address": wallet,
        "jetton_address": NON_EVM_TOKENS["TON"]["USDT"], "limit": 10})
    addresses = {ton_address(row["address"]) for row in wallets.get("jetton_wallets", [])
                 if ton_address(row["owner"]) == ton_address(wallet)
                 and ton_address(row["jetton"]) == ton_address(NON_EVM_TOKENS["TON"]["USDT"])}
    if not addresses:
        return _fail("Wallet Jetton penerima belum terverifikasi.")
    data = await _ton_get("transactions", {"hash": tx_hash, "limit": 1})
    queue = [(tx, 0) for tx in data.get("transactions", [])]
    seen = set()
    while queue and len(seen) < 20:
        tx, depth = queue.pop(0)
        canonical = normalize_tx_hash("TON", tx["hash"])
        if canonical in seen:
            continue
        seen.add(canonical)
        msg = tx.get("in_msg") or {}
        if (ton_address(tx["account"]) in addresses
                and ton_address(msg.get("destination", tx["account"])) == ton_address(tx["account"])
                and (tx.get("description") or {}).get("aborted") is False
                and msg.get("bounced") is False and tx.get("emulated") is not True
                and str(msg.get("opcode")).lower() in ("0x178d4519", str(0x178d4519))):
            from tonsdk.boc import Cell
            body = Cell.one_from_boc(base64.b64decode(msg["message_content"]["body"])).begin_parse()
            if body.read_uint(32) == 0x178d4519:
                body.read_uint(64)  # query id
                units = body.read_coins()
                source = body.read_msg_addr().to_string(False)
                if ton_address(source) != ton_address(wallet):
                    return _ok(Decimal(units) / 10**6, tx["now"], canonical, source)
        if depth < 2:
            outgoing = {msg["hash"] for msg in tx.get("out_msgs", []) if msg.get("hash")}
            if outgoing:
                adjacent = await _ton_get("adjacentTransactions", {"hash": canonical, "direction": "out", "limit": 20})
                queue.extend((child, depth + 1) for child in adjacent.get("transactions", [])
                             if (child.get("in_msg") or {}).get("hash") in outgoing)
    return _fail("Penerimaan USDT TON belum confirmed; jangan klaim dari request pengiriman saja.")

async def _verify_sui(tx_hash, wallet):
    result = await _rpc(settings.SUI_RPC, "sui_getTransactionBlock", [tx_hash,
                         {"showEffects": True, "showBalanceChanges": True}])
    if result.get("effects", {}).get("status", {}).get("status") != "success" or not result.get("checkpoint"):
        return _fail("Transaksi SUI belum sukses/final.")
    amount = Decimal(0)
    for change in result.get("balanceChanges") or []:
        coin_type = change.get("coinType", "").split("::")
        if len(coin_type) == 3 and _move_address(coin_type[0]) == _move_address("0x2") and coin_type[1:] == ["sui", "SUI"]:
            if _move_address(change.get("owner", {}).get("AddressOwner", "")) == _move_address(wallet):
                amount += Decimal(change["amount"]) / 10**9
    return _ok(amount, int(result["timestampMs"]) / 1000, tx_hash)


async def _verify_aptos(tx_hash, wallet):
    data = await _json("GET", f"{settings.APTOS_RPC.rstrip('/')}/transactions/by_hash/{tx_hash}")
    if data.get("success") is not True or data.get("type") != "user_transaction":
        return _fail("Transaksi APT belum sukses.")
    payload = data.get("payload") or {}
    function, args = payload.get("function"), payload.get("arguments", [])
    allowed = function in ("0x1::aptos_account::transfer", "0x1::aptos_account::transfer_coins",
                           "0x1::coin::transfer")
    if function != "0x1::aptos_account::transfer":
        allowed = allowed and payload.get("type_arguments") == ["0x1::aptos_coin::AptosCoin"]
    if not allowed or len(args) != 2 or _move_address(args[0]) != _move_address(wallet):
        return _fail("Payload bukan transfer APT ke wallet tujuan.")
    return _ok(Decimal(args[1]) / 10**8, int(data["timestamp"]) / 1e6, data["hash"], data.get("sender", ""))


async def verify_deposit(network, symbol, tx_hash, expected_wallet, expected_amount,
                         not_before=None, not_after=None):
    net, symbol = network.upper(), symbol.upper()
    try:
        if not expected_wallet or Decimal(str(expected_amount)) <= 0:
            return _fail("Wallet/nominal order tidak valid.")
        tx_hash = normalize_tx_hash(net, tx_hash)
        if net in EVM_NETWORKS:
            result = await _verify_evm(net, symbol, tx_hash, expected_wallet)
        elif net == "SOLANA":
            result = await _verify_solana(symbol, tx_hash, expected_wallet)
        elif net == "TRON":
            result = await _verify_tron(symbol, tx_hash, expected_wallet)
        elif net == "TON":
            result = await _verify_ton(symbol, tx_hash, expected_wallet)
        elif net == "SUI" and symbol == "SUI":
            result = await _verify_sui(tx_hash, expected_wallet)
        elif net == "APTOS" and symbol == "APT":
            result = await _verify_aptos(tx_hash, expected_wallet)
        else:
            return _fail("Pasangan aset/jaringan tidak didukung.")
        if result["verified"]:
            stamp = result.get("timestamp")
            # Toleransi clock drift 120 detik untuk variasi waktu block/server
            if stamp is None or (not_before is not None and stamp < (int(_timestamp(not_before)) - 120)):
                return _fail("Transaksi mendahului pembuatan order.")
            if not_after is not None and stamp > (_timestamp(not_after) + 120):
                return _fail("Transaksi melewati batas waktu order.")
            if not _amount_matches(result["amount"], expected_amount):
                return _fail(
                    f"Nominal deposit kurang: diterima {result['amount']} {symbol}, "
                    f"dibutuhkan {Decimal(str(expected_amount))} {symbol}.")
        return result
    except Exception as exc:
        logger.warning("Verifikasi %s/%s gagal (%s)", net, symbol, type(exc).__name__)
        return _fail(f"Data blockchain belum dapat diverifikasi ({type(exc).__name__}).")


async def _scan_hashes(network, symbol, wallet, limit, not_before):
    since = max(0, int(_timestamp(not_before)) - 120) if not_before is not None else 0
    if network == "SOLANA":
        addresses = [wallet]
        mint = NON_EVM_TOKENS["SOLANA"].get(symbol)
        if mint:
            accounts = await _sol_rpc("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
            addresses += [a["pubkey"] for a in accounts["value"]]
        for address in addresses:
            rows = await _sol_rpc("getSignaturesForAddress", [address, {"limit": 100, "commitment": "finalized"}])
            for row in rows:
                if row.get("err") is None and row.get("blockTime", 0) >= since:
                    yield row["signature"]
    elif network == "TRON":
        path = "transactions/trc20" if symbol == "USDT" else "transactions"
        params = {"limit": min(200, limit * 3), "only_confirmed": "true", "only_to": "true", "min_timestamp": since * 1000}
        if symbol == "USDT":
            params["contract_address"] = NON_EVM_TOKENS["TRON"]["USDT"]
        data = await _json("GET", f"{TRONGRID_URL}/v1/accounts/{wallet}/{path}", params=params,
                          headers={"TRON-PRO-API-KEY": settings.TRONGRID_API_KEY} if settings.TRONGRID_API_KEY else {})
        for row in data.get("data", []):
            yield row.get("txID") or row.get("transaction_id")
    elif network == "TON":
        if symbol == "TON":
            data = await _ton_get("transactions", {"account": wallet, "start_utime": since, "limit": 100})
            for row in data.get("transactions", []):
                if _ton_native_evidence(row, wallet)["verified"]:
                    yield normalize_tx_hash("TON", row["hash"])
        elif symbol == "USDT":
            data = await _ton_get("jetton/transfers", {"owner_address": wallet, "direction": "in",
                                  "jetton_master": NON_EVM_TOKENS["TON"]["USDT"], "start_utime": since, "limit": 100})
            for row in data.get("jetton_transfers", []):
                yield normalize_tx_hash("TON", row["transaction_hash"])
    elif network == "SUI":
        data = await _rpc(settings.SUI_RPC, "suix_queryTransactionBlocks", [
            {"filter": {"ToAddress": wallet}}, None, 100, True])
        for row in data.get("data", []):
            yield row["digest"]
    elif network == "APTOS":
        query = """query Incoming($owner: String!) {
          fungible_asset_activities(where: {owner_address: {_eq: $owner},
            is_transaction_success: {_eq: true}}, order_by: {transaction_version: desc}, limit: 100) {
            transaction_version
          }
        }"""
        data = await _json("POST", settings.APTOS_INDEXER_URL,
                           json={"query": query, "variables": {"owner": _move_address(wallet)}})
        for version in dict.fromkeys(row["transaction_version"] for row in data.get("data", {}).get("fungible_asset_activities", [])):
            tx = await _json("GET", f"{settings.APTOS_RPC.rstrip('/')}/transactions/by_version/{version}")
            if int(tx["timestamp"]) / 1e6 >= since:
                yield tx["hash"]
    elif network in EVM_NETWORKS:
        sender = CryptoSenderFactory.get_sender(network)
        w3 = _scan_web3(sender.rpc_list[0])
        if await asyncio.to_thread(lambda: w3.eth.chain_id) != sender.config["chain_id"]:
            raise ValueError("Chain ID tidak sesuai.")
        latest = await asyncio.to_thread(lambda: w3.eth.block_number)
        native = symbol == sender.config["native_symbol"] or (network == "POLYGON" and symbol == "POL")
        if native:
            # Full blocks avoid one RPC per transaction. Bound work per cycle.
            for height in range(latest, max(-1, latest - 500), -1):
                block = await asyncio.to_thread(w3.eth.get_block, height, full_transactions=True)
                if block["timestamp"] < since:
                    break
                for tx in block["transactions"]:
                    if (tx.get("to") or "").lower() == wallet.lower():
                        yield w3.to_hex(tx["hash"])
        else:
            token = sender.config["tokens"].get(symbol)
            if not token:
                return
            # Jendela per chunk 10 blok (batas Alchemy free tier getLogs). toBlock harus
            # absolut: Alchemy free tier menolak toBlock "latest" (range dihitung melebar).
            # Jalan mundur beberapa chunk supaya scan gap / restart container tidak
            # melewatkan deposit; deposit lebih tua ditangkap riwayat explorer bila ada.
            last_error = None
            chunk = 0
            for to_block in range(latest, max(-1, latest - 70), -10):
                query = {"fromBlock": max(0, to_block - 9), "toBlock": hex(to_block),
                         "address": None, "topics": ["0x" + TRANSFER_TOPIC, None,
                                                     "0x" + wallet.lower()[2:].zfill(64)]}
                logs = None
                for rpc in _ordered_rpcs(network, sender.rpc_list):
                    try:
                        scan_w3 = _scan_web3(rpc)
                        query["address"] = scan_w3.to_checksum_address(token)
                        logs = await asyncio.to_thread(scan_w3.eth.get_logs, query)
                        _scan_rpc_cache[network] = rpc
                        break
                    except Exception as exc:
                        last_error = exc
                        logger.warning("Scan %s/%s getLogs gagal via %s: %s", network, symbol, rpc, exc)
                if logs is None:
                    raise RuntimeError(f"getLogs gagal di semua RPC {network}: {last_error}")
                for log in reversed(logs):
                    yield w3.to_hex(log["transactionHash"])
                chunk += 1
                if chunk >= 7:
                    break


_incoming_cache: dict = {}
_INCOMING_CACHE_TTL = 300.0
_scan_rpc_cache: dict = {}


def _ordered_rpcs(network, rpc_list):
    """RPC yang terakhir berhasil dicoba lebih dulu (hindari spam RPC yang menolak)."""
    rpcs = list(rpc_list)
    cached = _scan_rpc_cache.get(network)
    if cached in rpcs:
        rpcs.remove(cached)
        rpcs.insert(0, cached)
    return rpcs


async def _explorer_incoming_hashes(network, symbol, wallet, since):
    """Riwayat transfer masuk via Etherscan V2 (tokentx/txlist).

    Deposit yang lebih tua dari jendela blok RPC publik tidak bisa dipindai
    dengan eth_getLogs; riwayat alamat explorer tetap menyimpannya.
    """
    chain_id = EXPLORER_CHAIN_IDS.get(network)
    if not chain_id or not settings.ETHERSCAN_API_KEY:
        return []
    sender = CryptoSenderFactory.get_sender(network)
    token = sender.config["tokens"].get(symbol)
    if symbol == sender.config["native_symbol"] or (network == "POLYGON" and symbol == "POL"):
        action, extra = "txlist", {}
    elif token:
        action, extra = "tokentx", {"contractaddress": token}
    else:
        return []

    key = (network, symbol, wallet.lower())
    now = time.time()
    cached = _incoming_cache.get(key)
    if cached and now - cached[0] < _INCOMING_CACHE_TTL:
        rows = cached[1]
    else:
        data = await _json("GET", EXPLORER_V2_API, params={
            "chainid": chain_id, "module": "account", "action": action,
            "address": wallet, "sort": "desc", "page": "1", "offset": "100",
            "apikey": settings.ETHERSCAN_API_KEY, **extra})
        rows = data.get("result") if str(data.get("status")) == "1" else []
        if not isinstance(rows, list):
            rows = []
        _incoming_cache[key] = (now, rows)

    hashes = []
    for row in rows:
        try:
            if str(row.get("to", "")).lower() != wallet.lower():
                continue
            if int(row.get("timeStamp", 0)) < since:
                continue
        except Exception:
            continue
        if row.get("hash"):
            hashes.append(row["hash"])
    return hashes


async def get_recent_incoming(network, symbol, wallet, min_amount=0.0, limit=20,
                              not_before=None, not_after=None):
    net, sym = network.upper(), symbol.upper()
    hashes = []
    try:
        async for tx_hash in _scan_hashes(net, sym, wallet, limit, not_before):
            hashes.append(tx_hash)
    except Exception as exc:
        logger.warning("Scan %s/%s gagal (%s): %s", network, symbol, type(exc).__name__, exc)

    if net in EVM_NETWORKS:
        try:
            since = _timestamp(not_before) if not_before is not None else 0
            hashes += await _explorer_incoming_hashes(net, sym, wallet, max(0, since - 120))
        except Exception as exc:
            logger.warning("Scan explorer %s/%s gagal (%s): %s",
                           network, symbol, type(exc).__name__, exc)

    results, seen = [], set()
    for tx_hash in hashes:
        if not tx_hash or tx_hash in seen:
            continue
        seen.add(tx_hash)
        result = await verify_deposit(network, symbol, tx_hash, wallet, min_amount,
                                      not_before, not_after)
        if result["verified"] and automatic_amount_matches(result["amount"], min_amount):
            results.append(result)
        if len(results) >= limit:
            break
    return results
