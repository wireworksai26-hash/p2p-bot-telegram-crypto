"""CoinGecko direct-IDR quotes, shared batch cache, explicit spread and freshness."""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone

import httpx

from config.settings import settings
from database import crud

logger = logging.getLogger(__name__)
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price"
CACHE_TTL_SECONDS = 30
MAX_PRICE_AGE_SECONDS = 180
COINGECKO_IDS = {
    "USDT": "tether", "USDC": "usd-coin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana", "AVAX": "avalanche-2", "TRX": "tron",
    "MATIC": "polygon-ecosystem-token", "POL": "polygon-ecosystem-token",
    "ARB": "arbitrum", "SUI": "sui", "TON": "the-open-network", "KAIA": "kaia",
    "BERA": "berachain-bera", "APT": "aptos", "OP": "optimism", "HYPE": "hyperliquid",
    "BASE": "ethereum", "ETH_ROBINHOOD": "ethereum", "USDG": "global-dollar",
}


class PriceService:
    def __init__(self):
        self._cache = {}
        self._client = None
        self._lock = asyncio.Lock()

    async def _get_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10)
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()

    async def _refresh(self, force=False):
        async with self._lock:
            now = time.time()
            if not force and self._cache and now - self._cache.get("_fetched_at", 0) < CACHE_TTL_SECONDS:
                return
            try:
                client = await self._get_client()
                response = await client.get(COINGECKO_API_URL, params={
                    "ids": ",".join(sorted(set(COINGECKO_IDS.values()))),
                    "vs_currencies": "idr,usd", "include_last_updated_at": "true",
                }, headers={"x-cg-demo-api-key": settings.COINGECKO_API_KEY} if settings.COINGECKO_API_KEY else {})
                response.raise_for_status()
                data = response.json()
                # Replace only after a successful response; stale entries still
                # fail the timestamp gate below if the provider is unavailable.
                self._cache = {**data, "_fetched_at": now}
            except Exception as exc:
                logger.warning("CoinGecko tidak tersedia (%s)", type(exc).__name__)

    def _valid(self, coin_id):
        row = self._cache.get(coin_id) or {}
        try:
            age = time.time() - float(row["last_updated_at"])
            if not 0 <= age <= MAX_PRICE_AGE_SECONDS:
                return None
            if any(not math.isfinite(float(row[k])) or float(row[k]) <= 0 for k in ("idr", "usd")):
                return None
            return row
        except (KeyError, TypeError, ValueError):
            return None

    async def get_price(self, symbol, db=None):
        symbol = symbol.upper()
        coin_id = COINGECKO_IDS.get(symbol)
        if not coin_id:
            return None
        await self._refresh()
        row, tether = self._valid(coin_id), self._valid("tether")
        if row is None or tether is None:
            return None
        own_session = db is None
        if own_session:
            from database.connection import SessionLocal
            db = SessionLocal()
        try:
            config = crud.get_price_config(db, symbol)
            if config is None and symbol == "POL":
                config = crud.get_price_config(db, "MATIC")
            spread = float(config.spread_pct) if config else settings.DEFAULT_SPREAD_PCT
            if not math.isfinite(spread) or not 0 <= spread < 100:
                return None
            market = float(row["idr"])
            return {
                "symbol": symbol, "market_price_idr": round(market, 2),
                "buy_price_idr": round(market * (1 + spread / 100), 2),
                "sell_price_idr": round(market * (1 - spread / 100), 2),
                "spread_pct": spread, "usdt_idr_rate": float(tether["idr"]),
                "source": "CoinGecko IDR", "price_updated_at": int(row["last_updated_at"]),
            }
        finally:
            if own_session:
                db.close()

    async def get_all_prices(self, db):
        configs = crud.get_all_price_configs(db)
        symbols = [c.symbol for c in configs] or list(COINGECKO_IDS)
        await self._refresh()
        prices = {}
        for symbol in symbols:
            result = await self.get_price(symbol, db)
            if result:
                prices[symbol] = result
        return prices

    async def refresh_all_prices(self):
        await self._refresh(force=True)

    async def get_coingecko_price_idr(self, symbol):
        await self._refresh()
        row = self._valid(COINGECKO_IDS.get(symbol.upper()))
        return float(row["idr"]) if row else None

    async def get_realtime_usdt_idr_fallback(self):
        return await self.get_coingecko_price_idr("USDT")


def quote_source_text(quote):
    stamp = datetime.fromtimestamp(quote["price_updated_at"], timezone.utc).strftime("%H:%M:%S UTC")
    return f"Sumber: {quote['source']} · {stamp} · spread {quote['spread_pct']:g}%"


price_service = PriceService()

