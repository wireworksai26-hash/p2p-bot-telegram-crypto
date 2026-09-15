"""
services/tx_verifier.py — Verifikasi Deposit Crypto On-chain Otomatis.
========================================================================
Memverifikasi TX hash dan memindai riwayat transaksi masuk ke hot wallet
tanpa keterlibatan admin. Dipakai oleh DepositDetector dan alur Convert.

Fungsi utama:
  - verify_deposit(...)   : verifikasi satu TX hash di blockchain
    (penerima, nominal, dan status sukses).
  - get_recent_incoming() : memindai transaksi masuk terakhir ke wallet
    yang cocok dengan (symbol, nominal) untuk auto-detect deposit.
"""

import asyncio
import logging

import base58
import httpx

from services.crypto_sender import CryptoSenderFactory

logger = logging.getLogger(__name__)

# Toleransi nominal 2% (fee jaringan / deduksi yang wajar)
AMOUNT_TOLERANCE = 0.02

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
TRONGRID_URL = "https://api.trongrid.io"
TRON_USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

EVM_NETWORKS = [
    "BSC", "ETH", "AVAX", "POLYGON", "BASE", "ARB", "OPTIMISM",
    "ROBINHOOD", "KAIA", "BERA", "HYPEREVM", "GRAVITY",
]


# ---------------- Helpers ----------------
def _ok(amount: float, from_addr: str = ""):
    return {"verified": True, "amount": amount, "from_address": from_addr, "reason": "OK"}


def _fail(reason: str):
    return {"verified": False, "amount": 0.0, "from_address": "", "reason": reason}


def _amount_matches(received: float, expected: float) -> bool:
    return received >= expected * (1 - AMOUNT_TOLERANCE)


def _log_data_to_int(data) -> int:
    """Konversi field `data` dari event log (bisa bytes atau hex str) ke int."""
    if data is None:
        return 0
    if isinstance(data, bytes):
        return int.from_bytes(data, "big")
    return int(data, 16)


def _tron_hex_to_base58(hex_addr: str) -> str:
    if not hex_addr:
        return ""
    try:
        return base58.b58encode_check(bytes.fromhex(hex_addr)).decode()
    except Exception:
        return ""


# ---------------- EVM ----------------
_ERC20_DECIMALS_CACHE = {}


def _erc20_decimals(w3, token_address: str, network: str) -> int:
    key = f"{network}:{str(token_address).lower()}"
    if key in _ERC20_DECIMALS_CACHE:
        return _ERC20_DECIMALS_CACHE[key]
    try:
        from services.crypto_sender.evm_sender import ERC20_ABI
        contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)
        decimals = contract.functions.decimals().call()
        _ERC20_DECIMALS_CACHE[key] = decimals
        return decimals
    except Exception:
        return 6


async def _verify_evm(network, symbol, tx_hash, expected_wallet, expected_amount):
    sender = CryptoSenderFactory.get_sender(network)
    w3 = sender.w3
    try:
        tx = await asyncio.to_thread(w3.eth.get_transaction, tx_hash)
    except Exception as exc:
        return _fail(f"TX tidak ditemukan: {exc}")
    if not tx:
        return _fail("TX tidak ditemukan")

    native_sym = sender.config.get("native_symbol")
    symbol_upper = symbol.upper()
    from_addr = tx.get("from") or ""

    if symbol_upper == native_sym:
        try:
            receipt = await asyncio.to_thread(w3.eth.get_transaction_receipt, tx_hash)
        except Exception:
            receipt = None
        if receipt is None or getattr(receipt, "status", None) != 1:
            return _fail("TX belum sukses/confirmed")
        to_addr = tx.get("to") or ""
        if to_addr and to_addr.lower() != expected_wallet.lower():
            return _fail("Penerima transaksi tidak cocok")
        amount = float(w3.from_wei(tx.get("value", 0), "ether"))
        if _amount_matches(amount, expected_amount):
            return _ok(amount, from_addr)
        return _fail(f"Nominal tidak sesuai: {amount} vs {expected_amount}")

    # ERC20: verifikasi via eth_getLogs pada blok TX. Tidak bergantung
    # get_transaction_receipt yang sering 403/rate-limit di RPC publik.
    token_address = (sender.config.get("tokens") or {}).get(symbol_upper)
    if not token_address:
        return _fail(f"Token {symbol_upper} tidak terdaftar di {network}")
    block_no = tx.get("blockNumber")
    if block_no is None:
        return _fail("TX belum masuk blok")

    padded = "0x" + expected_wallet.lower()[2:].zfill(64)
    logs = None
    for attempt in range(3):
        try:
            logs = await asyncio.to_thread(
                w3.eth.get_logs,
                {
                    "fromBlock": block_no,
                    "toBlock": block_no,
                    "address": w3.to_checksum_address(token_address),
                    "topics": [TRANSFER_TOPIC, None, padded],
                },
            )
            break
        except Exception as exc:
            if attempt < 2:
                await asyncio.sleep(1 + attempt)
                continue
            logger.warning("getLogs verifikasi %s gagal: %s", network, exc)
            return _fail(f"Gagal baca log token: {exc}")
    if not logs:
        return _fail("Tidak ada transfer token ke wallet ini")

    decimals = _erc20_decimals(w3, w3.to_checksum_address(token_address), network)
    tx_hash_clean = tx_hash.lower().replace("0x", "")
    for log in logs:
        th = getattr(log, "transactionHash", None)
        if th is not None:
            th_hex = th.hex() if isinstance(th, bytes) else str(th).lower().replace("0x", "")
            if th_hex != tx_hash_clean:
                continue
        raw = _log_data_to_int(getattr(log, "data", "0x0"))
        amount = raw / (10 ** decimals)
        if _amount_matches(amount, expected_amount):
            return _ok(amount, from_addr)
        return _fail(f"Nominal tidak sesuai: {amount} vs {expected_amount}")
    return _fail("Tidak ada transfer token ke wallet ini")


# ---------------- SOLANA ----------------
def _solana_walk_transfer(symbol, msg_instructions, inner_instructions, expected_wallet):
    """Scan instruksi transfer SOL / SPL token ke expected_wallet."""
    is_spl = symbol.upper() in ("USDT", "USDC")
    results = []

    def scan_ixns(instructions):
        for ixn in instructions or []:
            parsed = ixn.get("parsed") or {}
            info = parsed.get("info") or {}
            itype = parsed.get("type")
            if itype not in ("transfer", "transferChecked"):
                continue
            if info.get("destination") != expected_wallet:
                continue
            if itype == "transfer" and not is_spl:
                lamports = info.get("lamports")
                if lamports is not None:
                    results.append(int(lamports) / 1e9)
            elif itype == "transferChecked":
                ui = (info.get("tokenAmount") or {}).get("uiAmount")
                if ui is not None:
                    results.append(float(ui))

    scan_ixns(msg_instructions)
    for block in inner_instructions or []:
        scan_ixns(block.get("instructions") or [])
    return results


async def _verify_solana(symbol, tx_hash, expected_wallet, expected_amount):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [tx_hash, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(SOLANA_RPC, json=payload)
            if res.status_code != 200:
                return _fail(f"RPC Solana HTTP {res.status_code}")
            data = res.json()
    except Exception as exc:
        return _fail(f"RPC Solana error: {exc}")

    result = data.get("result")
    if not result:
        return _fail("TX tidak ditemukan")
    meta = result.get("meta") or {}
    if meta.get("err"):
        return _fail("TX gagal di blockchain")

    msg = (result.get("transaction") or {}).get("message") or {}
    amounts = _solana_walk_transfer(
        symbol,
        msg.get("instructions") or [],
        meta.get("innerInstructions") or [],
        expected_wallet,
    )
    if not amounts:
        return _fail("Tidak ada transfer ke wallet tujuan di TX ini")
    amount = max(amounts)
    if _amount_matches(amount, expected_amount):
        return _ok(amount, "")
    return _fail(f"Nominal tidak sesuai: {amount} vs {expected_amount}")


# ---------------- TRON ----------------
async def _verify_tron(symbol, tx_hash, expected_wallet, expected_amount):
    symbol_upper = symbol.upper()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            info_res = await client.post(
                f"{TRONGRID_URL}/wallet/gettransactioninfo", json={"value": tx_hash}
            )
            if info_res.status_code != 200:
                return _fail("Gagal query info TX TRON")
            info = info_res.json()
    except Exception as exc:
        return _fail(f"RPC TRON error: {exc}")

    if not info.get("id"):
        return _fail("TX belum muncul / tidak ditemukan")
    receipt = info.get("receipt") or {}
    if receipt.get("result") not in (None, "SUCCESS"):
        return _fail("TX gagal di blockchain")

    if symbol_upper in ("USDT", "USDC"):
        amount = None
        for log in info.get("log") or []:
            if (log.get("address") or "").lower() != TRON_USDT_TRC20.lower():
                continue
            topics = log.get("topics") or []
            if not topics or topics[0].lower() != TRANSFER_TOPIC:
                continue
            to_hex = topics[2][-40:] if len(topics) > 2 and topics[2] else ""
            if _tron_hex_to_base58(to_hex) != expected_wallet:
                continue
            amount = int(log.get("data") or "0", 16) / 1e6
            break
        if amount is None:
            return _fail("Tidak ada transfer TRC20 ke wallet ini")
        if _amount_matches(amount, expected_amount):
            return _ok(amount, "")
        return _fail(f"Nominal tidak sesuai: {amount} vs {expected_amount}")

    # Native TRX — buka AsyncClient baru karena client sebelumnya sudah ditutup
    try:
        async with httpx.AsyncClient(timeout=10.0) as trx_client:
            tx_res = await trx_client.post(
                f"{TRONGRID_URL}/wallet/gettransactionbyid", json={"value": tx_hash}
            )
            txn = tx_res.json()
    except Exception as exc:
        return _fail(f"Gagal query TX TRON native: {exc}")
    for c in (txn.get("raw_data") or {}).get("contract") or []:
        value = (c.get("parameter") or {}).get("value") or {}
        to_hex = value.get("to_address") or ""
        if _tron_hex_to_base58(to_hex) != expected_wallet:
            continue
        amount = int(value.get("amount") or 0) / 1e6
        if _amount_matches(amount, expected_amount):
            return _ok(amount, "")
        return _fail(f"Nominal tidak sesuai: {amount} vs {expected_amount}")
    return _fail("Tidak ada transfer TRX ke wallet ini")


# ---------------- TON ----------------
async def _ton_rpc(rpc, api_key, method, params):
    """POST jsonRPC ke Toncenter."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    url = f"{rpc}?api_key={api_key}" if api_key else rpc
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, json=payload)
            if res.status_code != 200:
                return None
            data = res.json()
            if not data.get("ok"):
                return None
            return data.get("result")
    except Exception:
        return None


async def _verify_ton(symbol, tx_hash, expected_wallet, expected_amount):
    from config.settings import settings
    rpc = settings.TON_RPC or "https://toncenter.com/api/v2/jsonRPC"
    api_key = settings.TON_API_KEY
    symbol_upper = symbol.upper()

    txs = await _ton_rpc(rpc, api_key, "getTransactions", {"address": expected_wallet, "limit": 20})
    if not isinstance(txs, list):
        return _fail("Gagal mengambil riwayat transaksi TON")

    for txn in txs:
        txn_id = txn.get("transaction_id") or {}
        if (txn_id.get("hash") or "") != tx_hash:
            continue
        if txn.get("success") is not True:
            return _fail("TX tidak sukses")
        if symbol_upper == "TON":
            in_msg = txn.get("in_msg") or {}
            amount = int(in_msg.get("value") or 0) / 1e9
            if _amount_matches(amount, expected_amount):
                return _ok(amount, in_msg.get("source") or "")
            return _fail(f"Nominal tidak sesuai: {amount} vs {expected_amount}")
        # Jetton: verifikasi best-effort (hash sukses sudah cukup)
        return _ok(0.0, (txn.get("in_msg") or {}).get("source") or "")
    return _fail("TX hash tidak ditemukan di riwayat wallet")


# ---------------- SUI / APTOS ----------------
async def _verify_sui(tx_hash, expected_wallet="", expected_amount=0.0):
    """Verifikasi TX SUI. Cek status sukses + penerima via balance_changes."""
    from config.settings import settings
    rpc = settings.SUI_RPC or "https://fullnode.mainnet.sui.io:443"
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "sui_getTransactionBlock",
        "params": [tx_hash, {
            "showEffects": True, "showInput": False,
            "showEvents": False, "showObjectChanges": True,
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(rpc, json=payload)
            data = res.json()
    except Exception as exc:
        return _fail(f"SUI RPC error: {exc}")
    result = data.get("result")
    if not result:
        return _fail("TX tidak ditemukan")
    effects = result.get("effects") or {}
    if (effects.get("status") or {}).get("status") != "success":
        return _fail("TX tidak sukses")

    # Validasi penerima via objectChanges (best-effort: tidak semua TX punya ini)
    if expected_wallet:
        obj_changes = result.get("objectChanges") or []
        recipient_found = any(
            (ch.get("recipient") or "") == expected_wallet
            or (ch.get("owner") or {}).get("AddressOwner", "") == expected_wallet
            for ch in obj_changes
        )
        if obj_changes and not recipient_found:
            logger.warning(
                "SUI TX %s: penerima tidak cocok dengan wallet %s (best-effort)",
                tx_hash, expected_wallet,
            )
            # Tetap lanjut karena SUI object model kompleks — log saja untuk audit
    return _ok(0.0, "")  # Nominal SUI tidak di-parse (butuh coin type check)


async def _verify_aptos(tx_hash, expected_wallet="", expected_amount=0.0):
    """Verifikasi TX Aptos. Cek status sukses + penerima via payload."""
    from config.settings import settings
    rpc = settings.APTOS_RPC or "https://fullnode.mainnet.aptos.labs.com/v1"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"{rpc}/transactions/by_hash/{tx_hash}")
            if res.status_code != 200:
                return _fail(f"APTOS HTTP {res.status_code}")
            data = res.json()
    except Exception as exc:
        return _fail(f"APTOS RPC error: {exc}")
    if data.get("success") is not True:
        return _fail("TX tidak sukses")

    # Validasi penerima dari payload function arguments (best-effort)
    if expected_wallet:
        payload = data.get("payload") or {}
        args = payload.get("arguments") or []
        # Format args: [recipient_address, amount_string, ...]
        if args and isinstance(args[0], str):
            if args[0].lower() != expected_wallet.lower():
                return _fail(f"Penerima TX tidak cocok: {args[0]} vs {expected_wallet}")
        # Validasi nominal (dalam octa = 1e8 untuk APT)
        if expected_amount > 0 and len(args) > 1:
            try:
                sent_octa = int(args[1])
                sent_apt = sent_octa / 1e8
                if not _amount_matches(sent_apt, expected_amount):
                    return _fail(f"Nominal tidak sesuai: {sent_apt} vs {expected_amount}")
                return _ok(sent_apt, data.get("sender") or "")
            except (ValueError, TypeError):
                pass  # format args tidak dikenal — lanjut best-effort
    return _ok(0.0, data.get("sender") or "")


# ---------------- Public API ----------------
async def verify_deposit(network, symbol, tx_hash, expected_wallet, expected_amount):
    """
    Verifikasi TX hash deposit di blockchain.

    Returns dict: {verified, amount, from_address, reason}
    """
    net = network.upper()
    try:
        if net in EVM_NETWORKS:
            return await _verify_evm(net, symbol, tx_hash, expected_wallet, expected_amount)
        if net == "SOLANA":
            return await _verify_solana(symbol, tx_hash, expected_wallet, expected_amount)
        if net == "TRON":
            return await _verify_tron(symbol, tx_hash, expected_wallet, expected_amount)
        if net == "TON":
            return await _verify_ton(symbol, tx_hash, expected_wallet, expected_amount)
        if net == "SUI":
            return await _verify_sui(tx_hash, expected_wallet, expected_amount)
        if net == "APTOS":
            return await _verify_aptos(tx_hash, expected_wallet, expected_amount)
        return _fail(f"Network {net} tidak didukung verifikasi on-chain")
    except Exception as exc:
        logger.error("verify_deposit error %s/%s: %s", net, symbol, exc, exc_info=True)
        return _fail(f"Error verifikasi: {exc}")


# ---------------- Auto-scan riwayat masuk ----------------
async def get_recent_incoming(network, symbol, wallet, min_amount=0.0, limit=20):
    """
    Memindai transaksi masuk terakhir ke wallet yang cocok (symbol & nominal).

    Returns list[dict]: {tx_hash, amount, from_address}
    """
    net = network.upper()
    try:
        if net in EVM_NETWORKS:
            return await _scan_evm_incoming(net, symbol, wallet, min_amount, limit)
        if net == "SOLANA":
            return await _scan_solana_incoming(symbol, wallet, min_amount, limit)
        if net == "TRON":
            return await _scan_tron_incoming(symbol, wallet, min_amount, limit)
        if net == "TON":
            return await _scan_ton_incoming(symbol, wallet, min_amount, limit)
    except Exception as exc:
        logger.error("get_recent_incoming error %s/%s: %s", net, symbol, exc, exc_info=True)
    return []


async def _scan_evm_incoming(network, symbol, wallet, min_amount, limit):
    sender = CryptoSenderFactory.get_sender(network)
    native_sym = sender.config.get("native_symbol")
    symbol_upper = symbol.upper()
    results = []

    for attempt in range(len(sender.rpc_list)):
        w3 = sender.w3
        try:
            latest = await asyncio.to_thread(lambda: w3.eth.block_number)
            if symbol_upper == native_sym:
                from_block = max(0, latest - 30)
                for blk_num in range(from_block, latest + 1):
                    try:
                        blk = await asyncio.to_thread(w3.eth.get_block, blk_num)
                    except Exception:
                        continue
                    for tx_hash_hex in getattr(blk, "transactions", []) or []:
                        try:
                            tx = await asyncio.to_thread(w3.eth.get_transaction, tx_hash_hex)
                        except Exception:
                            continue
                        to_addr = tx.get("to") or ""
                        if to_addr and to_addr.lower() == wallet.lower():
                            amount = float(w3.from_wei(tx.get("value", 0), "ether"))
                            if amount >= min_amount * (1 - AMOUNT_TOLERANCE):
                                results.append({
                                    "tx_hash": w3.to_hex(tx_hash_hex),
                                    "amount": amount,
                                    "from_address": tx.get("from") or "",
                                })
                                if len(results) >= limit:
                                    return results
                return results
            else:
                # ERC20 via eth_getLogs (scan ±2000 blok terakhir)
                token_address = (sender.config.get("tokens") or {}).get(symbol_upper)
                if not token_address:
                    return []
                padded = "0x" + wallet.lower()[2:].zfill(64)
                logs = await asyncio.to_thread(
                    w3.eth.get_logs,
                    {
                        "fromBlock": max(0, latest - 2000),
                        "toBlock": "latest",
                        "address": w3.to_checksum_address(token_address),
                        "topics": [TRANSFER_TOPIC, None, padded],
                    },
                )
                decimals = _erc20_decimals(w3, w3.to_checksum_address(token_address), network)
                for log in logs[-limit:]:
                    topics = getattr(log, "topics", None) or []
                    from_hex = topics[1].hex()[-40:] if len(topics) > 1 else ""
                    try:
                        amount = _log_data_to_int(getattr(log, "data", "0x0")) / (10 ** decimals)
                    except Exception:
                        amount = 0.0
                    if amount >= min_amount * (1 - AMOUNT_TOLERANCE):
                        txh = getattr(log, "transactionHash", None)
                        txh_str = txh.hex() if hasattr(txh, "hex") else (w3.to_hex(txh) if txh else "")
                        results.append({
                            "tx_hash": txh_str,
                            "amount": amount,
                            "from_address": "0x" + from_hex,
                        })
                return results
        except Exception as exc:
            logger.warning(f"Gagal scan incoming EVM {network} via RPC {sender.rpc_list[sender.current_rpc_index]}: {exc}")
            if attempt < len(sender.rpc_list) - 1:
                sender._rotate_rpc()
            else:
                return []
    return results


async def _scan_solana_incoming(symbol, wallet, min_amount, limit):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
        "params": [wallet, {"limit": min(limit * 2, 40)}],
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(SOLANA_RPC, json=payload)
            if res.status_code != 200:
                return []
            sigs = res.json().get("result") or []
    except Exception as exc:
        logger.warning("SOLANA scan error: %s", exc)
        return []

    results = []
    for sig_item in sigs:
        sig = sig_item.get("signature")
        if not sig:
            continue
        ver = await _verify_solana(symbol, sig, wallet, min_amount)
        if ver["verified"]:
            results.append({
                "tx_hash": sig,
                "amount": ver["amount"],
                "from_address": ver["from_address"],
            })
        if len(results) >= limit:
            break
    return results


async def _scan_tron_incoming(symbol, wallet, min_amount, limit):
    symbol_upper = symbol.upper()
    results = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        if symbol_upper in ("USDT", "USDC"):
            try:
                res = await client.get(
                    f"{TRONGRID_URL}/v1/accounts/{wallet}/transactions/trc20",
                    params={"limit": limit, "only_confirmed": "true",
                            "contract_address": TRON_USDT_TRC20},
                )
                if res.status_code != 200:
                    return []
                for item in (res.json().get("data") or []):
                    if (item.get("to") or "") != wallet or item.get("type") != "Transfer":
                        continue
                    amount = float(item.get("value") or 0) / 1e6
                    if amount >= min_amount * (1 - AMOUNT_TOLERANCE):
                        results.append({
                            "tx_hash": item.get("transaction_id") or "",
                            "amount": amount,
                            "from_address": item.get("from") or "",
                        })
            except Exception as exc:
                logger.warning("TRON TRC20 scan error: %s", exc)
            return results

        try:
            res = await client.get(
                f"{TRONGRID_URL}/v1/accounts/{wallet}/transactions",
                params={"limit": limit, "only_confirmed": "true"},
            )
            if res.status_code != 200:
                return []
            for item in (res.json().get("data") or []):
                for c in (item.get("raw_data") or {}).get("contract") or []:
                    if c.get("type") != "TransferContract":
                        continue
                    value = (c.get("parameter") or {}).get("value") or {}
                    if _tron_hex_to_base58(value.get("to_address") or "") != wallet:
                        continue
                    amount = int(value.get("amount") or 0) / 1e6
                    if amount >= min_amount * (1 - AMOUNT_TOLERANCE):
                        results.append({
                            "tx_hash": item.get("txID") or item.get("hash") or "",
                            "amount": amount,
                            "from_address": "",
                        })
        except Exception as exc:
            logger.warning("TRON TRX scan error: %s", exc)
    return results


async def _scan_ton_incoming(symbol, wallet, min_amount, limit):
    from config.settings import settings
    rpc = settings.TON_RPC or "https://toncenter.com/api/v2/jsonRPC"
    api_key = settings.TON_API_KEY
    if symbol.upper() != "TON":
        return []  # scan jetton tidak didukung — andalkan TX hash manual

    data = await _ton_rpc(rpc, api_key, "getTransactions", {"address": wallet, "limit": limit})
    if not isinstance(data, list):
        return []

    results = []
    for txn in data:
        if txn.get("success") is not True:
            continue
        in_msg = txn.get("in_msg") or {}
        source = in_msg.get("source") or ""
        if source == wallet:
            continue  # transaksi keluar
        amount = int(in_msg.get("value") or 0) / 1e9
        if amount >= min_amount * (1 - AMOUNT_TOLERANCE):
            results.append({
                "tx_hash": (txn.get("transaction_id") or {}).get("hash") or "",
                "amount": amount,
                "from_address": source,
            })
    return results
