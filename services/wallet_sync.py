"""Balance-only synchronization shared by the scheduler, stock menu and admin."""

import asyncio
import logging
import math

from config.assets import STOCK_ASSETS, get_wallet_address
from database.connection import SessionLocal
from database import crud
from services.crypto_sender import CryptoSenderFactory

logger = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()


async def sync_wallet_balances(assets=None) -> dict:
    pairs = STOCK_ASSETS if assets is None else assets
    # Prevent overlapping menu/admin/scheduler runs from overwriting newer results.
    async with _sync_lock:
        semaphore = asyncio.Semaphore(6)

        async def fetch(symbol, network):
            async with semaphore:
                address = get_wallet_address(network)
                try:
                    sender = CryptoSenderFactory.get_sender(network)
                    address = sender.wallet_address
                    balance = await sender.get_balance(symbol=symbol)
                    if balance is None or not math.isfinite(balance) or balance < 0:
                        raise ValueError("Respons saldo tidak valid.")
                    return balance, address, None
                except Exception as exc:
                    # Exception URLs may contain provider API keys; persist only a safe reason.
                    error = f"Pembacaan saldo {symbol}/{network} gagal ({type(exc).__name__})."
                    logger.warning(error)
                    return None, address, error

        results = await asyncio.gather(*(fetch(sym, net) for sym, net in pairs))
        report = {"success": [], "failed": []}
        db = SessionLocal()
        try:
            for (symbol, network), (balance, address, error) in zip(pairs, results):
                if error:
                    crud.mark_wallet_balance_error(db, network, symbol, error, address)
                    report["failed"].append(f"{symbol} ({network})")
                else:
                    crud.update_wallet_balance(db, network, balance, symbol, address)
                    report["success"].append(f"{symbol} ({network})")
            if assets is None:
                crud.prune_wallet_balances(db, STOCK_ASSETS)
        finally:
            db.close()
        return report
