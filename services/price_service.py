"""CoinGecko direct-IDR quotes, shared batch cache, explicit spread and freshness.

Fallback saat CoinGecko kena limit: harga USDT Binance x kurs USD/IDR
(open.er-api.com / frankfurter.app), tanpa API key.
"""
import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone

import httpx

from config.settings import settings
from database import crud

logger = logging.getLogger(__name__)
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price"
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
FX_URLS = (
    "https://open.er-api.com/v6/latest/USD",
    "https://api.frankfurter.app/latest?from=USD&to=IDR",
)
CACHE_TTL_SECONDS = 30
MAX_PRICE_AGE_SECONDS = 600
MAX_CLOCK_SKEW_SECONDS = 60
COINGECKO_IDS = {
    "USDT": "tether", "USDC": "usd-coin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana", "AVAX": "avalanche-2", "TRX": "tron",
    "MATIC": "polygon-ecosystem-token", "POL": "polygon-ecosystem-token",
    "ARB": "arbitrum", "SUI": "sui", "TON": "the-open-network", "KAIA": "kaia",
    "BERA": "berachain-bera", "APT": "aptos", "OP": "optimism", "HYPE": "hyperliquid",
    "BASE": "ethereum", "ETH_ROBINHOOD": "ethereum", "USDG": "global-dollar",
}

# coin_id CoinGecko -> pasangan Binance (fallback saat CoinGecko limit).
BINANCE_SYMBOLS = {
    "ethereum": "ETHUSDT", "binancecoin": "BNBUSDT", "solana": "SOLUSDT",
    "avalanche-2": "AVAXUSDT", "tron": "TRXUSDT", "polygon-ecosystem-token": "POLUSDT",
    "arbitrum": "ARBUSDT", "sui": "SUIUSDT", "the-open-network": "TONUSDT",
    "kaia": "KAIAUSDT", "berachain-bera": "BERAUSDT", "aptos": "APTUSDT",
    "optimism": "OPUSDT", "hyperliquid": "HYPEUSDT",
}
STABLE_COIN_IDS = {"tether", "usd-coin", "global-dollar"}


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

    async def _fetch_coingecko(self):
        try:
            client = await self._get_client()
            response = await client.get(COINGECKO_API_URL, params={
                "ids": ",".join(sorted(set(COINGECKO_IDS.values()))),
                "vs_currencies": "idr,usd", "include_last_updated_at": "true",
            }, headers={"x-cg-demo-api-key": settings.COINGECKO_API_KEY} if settings.COINGECKO_API_KEY else {})
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and any(k in data for k in set(COINGECKO_IDS.values())):
                return data
        except Exception as exc:
            logger.warning("CoinGecko tidak tersedia (%s)", type(exc).__name__)
        return None

    async def _fetch_binance(self):
        """Fallback tanpa API key: harga USDT Binance x kurs USD->IDR."""
        try:
            client = await self._get_client()
            fx = None
            for url in FX_URLS:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    fx = float(resp.json()["rates"]["IDR"])
                    if fx > 0:
                        break
                except Exception:
                    continue
            if not fx:
                logger.warning("Kurs USD/IDR fallback tidak tersedia")
                return None
            resp = await client.get(BINANCE_PRICE_URL, params={
                "symbols": json.dumps(sorted(set(BINANCE_SYMBOLS.values())), separators=(",", ":")),
            })
            resp.raise_for_status()
            prices = {row["symbol"]: float(row["price"]) for row in resp.json()}
        except Exception as exc:
            logger.warning("Fallback Binance tidak tersedia (%s)", type(exc).__name__)
            return None
        stamp = int(time.time())
        data = {}
        for coin_id in set(COINGECKO_IDS.values()):
            if coin_id in STABLE_COIN_IDS:
                data[coin_id] = {"idr": fx, "usd": 1.0, "last_updated_at": stamp}
                continue
            price_usd = prices.get(BINANCE_SYMBOLS.get(coin_id, ""))
            if price_usd and price_usd > 0:
                data[coin_id] = {"idr": price_usd * fx, "usd": price_usd, "last_updated_at": stamp}
        return data or None

    async def _refresh(self, force=False):
        async with self._lock:
            now = time.time()
            if not force and self._cache and now - self._cache.get("_fetched_at", 0) < CACHE_TTL_SECONDS:
                return
            data = await self._fetch_coingecko()
            source = "CoinGecko IDR"
            if data is None:
                data = await self._fetch_binance()
                source = "Binance+FX"
            if data is not None:
                self._cache = {**data, "_fetched_at": now, "_source": source}
            else:
                # Tetap cap waktu saat gagal: cegah badai retry (429) dari tiap get_price.
                self._cache = {**(self._cache or {}), "_fetched_at": now}

    def _invalid_reason(self, coin_id):
        """Alasan baris harga CoinGecko tidak layak dipakai; '' bila layak."""
        row = self._cache.get(coin_id)
        if not row:
            return f"tidak ada data {coin_id} di cache"
        now = time.time()
        age_fetch = now - float(self._cache.get("_fetched_at") or 0)
        if age_fetch > MAX_PRICE_AGE_SECONDS:
            return f"harga gagal diperbarui {age_fetch:.0f} detik (CoinGecko tidak tersedia?)"
        try:
            age = now - float(row["last_updated_at"])
        except (KeyError, TypeError, ValueError):
            return f"data {coin_id} tidak lengkap"
        if age > MAX_PRICE_AGE_SECONDS:
            return f"data harga {coin_id} berumur {age:.0f} detik"
        if age < -MAX_CLOCK_SKEW_SECONDS:
            return f"timestamp {coin_id} {-age:.0f} detik di masa depan"
        try:
            if any(not math.isfinite(float(row[k])) or float(row[k]) <= 0 for k in ("idr", "usd")):
                return f"nilai harga {coin_id} tidak valid"
        except (KeyError, TypeError, ValueError):
            return f"data {coin_id} tidak lengkap"
        return ""

    def _valid(self, coin_id):
        return None if self._invalid_reason(coin_id) else self._cache.get(coin_id)

    async def get_price(self, symbol, db=None):
        symbol = symbol.upper()
        coin_id = COINGECKO_IDS.get(symbol)
        if not coin_id:
            logger.warning("Harga %s tidak tersedia: simbol belum punya id CoinGecko", symbol)
            return None
        await self._refresh()
        row, tether = self._valid(coin_id), self._valid("tether")
        if row is None or tether is None:
            alasan = self._invalid_reason(coin_id) or self._invalid_reason("tether")
            logger.warning("Harga %s tidak tersedia: %s", symbol, alasan or "-")
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
                logger.warning("Harga %s ditolak: spread_pct %r tidak valid", symbol, spread)
                return None
            market = float(row["idr"])
            return {
                "symbol": symbol, "market_price_idr": round(market, 2),
                "buy_price_idr": round(market * (1 + spread / 100), 2),
                "sell_price_idr": round(market * (1 - spread / 100), 2),
                "spread_pct": spread, "usdt_idr_rate": float(tether["idr"]),
                "source": self._cache.get("_source", "CoinGecko IDR"),
                "price_updated_at": int(row["last_updated_at"]),
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

