"""
services/sui_graphql.py — Penerjemah JSON-RPC Sui -> GraphQL resmi Sui.
=======================================================================
JSON-RPC publik Sui (fullnode Mysten, publicnode, blastapi) sudah dimatikan; node publik kini
hanya melayani gRPC / GraphQL. Sender, verifier, dan monitor bot masih memakai bentuk hasil
JSON-RPC, jadi modul ini menerima (method, params) yang sama dan mengembalikan hasil berbentuk
sama, tetapi mengambilnya lewat GraphQL. URL GraphQL dikenali dari akhiran "/graphql".
"""
import logging
import re
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

PAGE_MAX = 50  # batas halaman server GraphQL Sui

_BALANCE = """query($a: SuiAddress!, $t: String!) {
  address(address: $a) { balance(coinType: $t) { totalBalance } }
}"""
_COINS = """query($a: SuiAddress!, $t: String!, $n: Int!) {
  address(address: $a) {
    objects(first: $n, filter: {type: $t}) {
      nodes { address version digest contents { json } }
      pageInfo { hasNextPage }
    }
  }
}"""
_CHECKPOINT = "{ checkpoint { sequenceNumber } }"
_GAS_PRICE = "{ epoch { referenceGasPrice } }"
_SIMULATE = """query($t: JSON!) {
  simulateTransaction(transaction: $t) {
    effects { status executionError { message }
      gasEffects { gasSummary { computationCost storageCost storageRebate } } }
  }
}"""
_EXECUTE = """mutation($d: Base64!, $s: [Base64!]!) {
  executeTransaction(transactionDataBcs: $d, signatures: $s) {
    effects { digest status executionError { message } }
  }
}"""
_TRANSACTION = """query($d: String!) {
  transaction(digest: $d) {
    digest
    sender { address }
    effects {
      status
      timestamp
      checkpoint { sequenceNumber }
      balanceChanges(first: 50) { nodes { owner { address } coinType { repr } amount } }
    }
  }
}"""
_INCOMING = """query($a: SuiAddress!, $n: Int!) {
  transactions(last: $n, filter: {affectedAddress: $a}) {
    nodes { digest effects { balanceChanges(first: 50) { nodes { owner { address } amount } } } }
  }
}"""


def is_graphql_url(url) -> bool:
    return str(url or "").rstrip("/").lower().endswith("/graphql")


async def _gql(url: str, query: str, variables: dict = None, timeout: float = 20.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.post(url, json={"query": query, "variables": variables or {}})
        res.raise_for_status()
        data = res.json()
    if not isinstance(data, dict):
        raise RuntimeError("Balasan GraphQL Sui tidak valid.")
    if data.get("errors"):
        raise RuntimeError(str((data["errors"][0] or {}).get("message"))[:180])
    if not isinstance(data.get("data"), dict):
        # Fail-closed: balasan tanpa data (mis. {"error": ...}) bukan "saldo nol".
        raise RuntimeError("Balasan GraphQL Sui tanpa data.")
    return data["data"]


def _status(effects: dict) -> dict:
    ok = str((effects or {}).get("status") or "").upper() == "SUCCESS"
    error = ((effects or {}).get("executionError") or {}).get("message")
    return {"status": "success" if ok else "failure", **({} if ok else {"error": error or "gagal"})}


def _timestamp_ms(value) -> str:
    """'2026-10-26T12:16:30.650Z' -> '1792...' (ms sejak epoch, string seperti JSON-RPC)."""
    text = str(value or "")
    try:
        match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z?$", text)
        moment = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        millis = int((match.group(2) or "0").ljust(3, "0")[:3])
        return str(int(moment.timestamp()) * 1000 + millis)
    except Exception:
        raise RuntimeError("Waktu transaksi Sui tidak terbaca.")


async def call(url: str, method: str, params: list):
    """Satu panggilan bergaya JSON-RPC lewat GraphQL; raise bila node menolak."""
    if method == "sui_getLatestCheckpointSequenceNumber":
        data = await _gql(url, _CHECKPOINT)
        return str(data["checkpoint"]["sequenceNumber"])

    if method == "suix_getBalance":
        data = await _gql(url, _BALANCE, {"a": params[0], "t": params[1]})
        if not isinstance(data.get("address"), dict):
            raise RuntimeError("Alamat Sui tidak terbaca.")
        balance = data["address"].get("balance") or {}  # alamat tanpa koin ini = saldo nol sungguhan
        return {"coinType": params[1], "totalBalance": str(balance.get("totalBalance") or "0")}

    if method == "suix_getCoins":
        coin_type, limit = params[1], min(int(params[3] or PAGE_MAX), PAGE_MAX)
        data = await _gql(url, _COINS, {"a": params[0], "t": f"0x2::coin::Coin<{coin_type}>", "n": limit})
        if not isinstance(data.get("address"), dict):
            raise RuntimeError("Alamat Sui tidak terbaca.")
        objects = data["address"].get("objects") or {}
        rows = []
        for node in objects.get("nodes") or []:
            rows.append({
                "coinObjectId": node["address"], "version": str(node["version"]), "digest": node["digest"],
                "balance": str(((node.get("contents") or {}).get("json") or {}).get("balance") or "0"),
            })
        return {"data": rows, "hasNextPage": bool((objects.get("pageInfo") or {}).get("hasNextPage"))}

    if method == "suix_getReferenceGasPrice":
        data = await _gql(url, _GAS_PRICE)
        return str(data["epoch"]["referenceGasPrice"])

    if method == "sui_dryRunTransactionBlock":
        data = await _gql(url, _SIMULATE, {"t": {"bcs": {"value": params[0]}}})
        effects = (data.get("simulateTransaction") or {}).get("effects")
        if not effects:
            raise RuntimeError("Simulasi Sui tanpa effects.")
        gas = ((effects.get("gasEffects") or {}).get("gasSummary") or {})
        return {"effects": {
            "status": _status(effects),
            "gasUsed": {key: str(gas.get(key) or 0) for key in ("computationCost", "storageCost", "storageRebate")},
        }}

    if method == "sui_executeTransactionBlock":
        data = await _gql(url, _EXECUTE, {"d": params[0], "s": list(params[1])}, timeout=30.0)
        effects = (data.get("executeTransaction") or {}).get("effects")
        if not effects:
            raise RuntimeError("Eksekusi Sui tanpa effects.")  # hasil tidak pasti -> ditangani sebagai review manual
        return {"digest": effects.get("digest"), "effects": {"status": _status(effects)}}

    if method == "sui_getTransactionBlock":
        data = await _gql(url, _TRANSACTION, {"d": params[0]})
        tx = data.get("transaction")
        if not tx or not tx.get("effects"):
            raise RuntimeError("Transaksi Sui belum ditemukan.")
        effects = tx["effects"]
        checkpoint = (effects.get("checkpoint") or {}).get("sequenceNumber")
        return {
            "digest": tx["digest"],
            "effects": {"status": _status(effects)},
            "checkpoint": str(checkpoint) if checkpoint is not None else None,
            "timestampMs": _timestamp_ms(effects.get("timestamp")),
            "transaction": {"data": {"sender": (tx.get("sender") or {}).get("address") or ""}},
            "balanceChanges": [
                {"owner": {"AddressOwner": (change.get("owner") or {}).get("address") or ""},
                 "coinType": (change.get("coinType") or {}).get("repr") or "", "amount": str(change["amount"])}
                for change in (effects.get("balanceChanges") or {}).get("nodes") or []
            ],
        }

    if method == "suix_queryTransactionBlocks":
        wallet = "0x" + str((params[0].get("filter") or {}).get("ToAddress")).lower().removeprefix("0x").zfill(64)
        data = await _gql(url, _INCOMING, {"a": wallet, "n": min(int(params[2] or PAGE_MAX), PAGE_MAX)})
        digests = []
        for node in ((data.get("transactions") or {}).get("nodes") or [])[::-1]:  # terbaru dulu
            changes = ((node.get("effects") or {}).get("balanceChanges") or {}).get("nodes") or []
            if any(int(c["amount"]) > 0 and ((c.get("owner") or {}).get("address") or "").lower() == wallet
                   for c in changes):
                digests.append({"digest": node["digest"]})
        return {"data": digests}

    raise RuntimeError(f"Metode Sui {method} tidak didukung lewat GraphQL.")
