"""Read balances using the actual senders. No bot, database, signing or payout.

Run from repository root: python scripts/check_wallet_balances.py
Only public addresses are sent to the configured RPC providers.
"""
import asyncio
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))
logging.disable(logging.CRITICAL)

from services.crypto_sender import CryptoSenderFactory


async def main():
    async def read(symbol, network):
        try:
            sender = CryptoSenderFactory.get_sender(network)
            balance = await sender.get_balance(symbol)
            print(json.dumps({"symbol": symbol, "network": network, "status": "OK", "balance": balance}))
        except Exception as exc:
            print(json.dumps({"symbol": symbol, "network": network, "status": "ERROR", "error_type": type(exc).__name__}))
    await asyncio.gather(*(read(symbol, network) for symbol, network in [
        ("SOL", "SOLANA"), ("USDT", "SOLANA"), ("USDC", "SOLANA"),
        ("APT", "APTOS"), ("ETH", "ROBINHOOD"),
    ]))


if __name__ == "__main__":
    asyncio.run(main())
