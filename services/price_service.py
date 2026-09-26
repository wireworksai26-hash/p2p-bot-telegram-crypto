"""CoinGecko direct-IDR quotes, shared batch cache, explicit spread and freshness.

Fallback saat CoinGecko kena limit: harga USD CoinPaprika / CoinMarketCap x kurs
USD/IDR (open.er-api.com / frankfurter.dev). Stablecoin dihitung 1 USD x kurs.
"""
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
PAPRIKA_TICKERS_URL = "https://api.coinpaprika.com/v1/tickers?quotes=USD"
CMC_QUOTES_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
FX_URLS = (
    "https://open.er-api.com/v6/latest/USD",
    "https://api.frankfurter.dev/latest?from=USD&to=IDR",
)
# 60 dtk: data demo CoinGecko delay "from 60 s" (lebih cepat = buang kredit).
# Burn ~43.200 call/bln terbagi rata ke COINGECKO_API_KEYS (kuota 10k/bln/akun).
CACHE_TTL_SECONDS = 60
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

# coin_id CoinGecko -> id CoinPaprika (fallback 1, tanpa API key).
PAPRIKA_IDS = {
    "ethereum": "eth-ethereum", "binancecoin": "bnb-binance-coin",
    "solana": "sol-solana", "avalanche-2": "avax-avalanche", "tron": "trx-tron",
    "polygon-ecosystem-token": "pol-polygon-ecosystem-token",
    "arbitrum": "arb-arbitrum", "sui": "sui-sui", "the-open-network": "ton-tontoken",
    "kaia": "kaia-kaia", "berachain-bera": "bera-berachain", "aptos": "apt-aptos",
    "optimism": "op-optimism", "hyperliquid": "hype-hyperliquid",
    "tether": "usdt-tether", "usd-coin": "usdc-usd-coin", "global-dollar": "usdg-global-dollar",
}

# coin_id CoinGecko -> simbol CoinMarketCap (fallback 2, hemat kuota).
CMC_SYMBOLS = {
    "ethereum": "ETH", "binancecoin": "BNB", "solana": "SOL", "avalanche-2": "AVAX",
    "tron": "TRX", "polygon-ecosystem-token": "POL", "arbitrum": "ARB", "sui": "SUI",
    "the-open-network": "TON", "kaia": "KAIA", "berachain-bera": "BERA", "aptos": "APT",
    "optimism": "OP", "hyperliquid": "HYPE", "tether": "USDT", "usd-coin": "USDC",
    "global-dollar": "USDG",
}

STABLE_COIN_IDS = {"tether", "usd-coin", "global-dollar"}

_key_seq = 0


def next_coingecko_index():
    global _key_seq
    i = _key_seq
    _key_seq += 1
    return i


def next_coingecko_key():
    """Key CoinGecko berikutnya, round-robin antar akun (kuota terpisah/bln)."""
    keys = settings.COINGECKO_API_KEYS
    return keys[next_coingecko_index() % len(keys)] if keys else None


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
            # Round-robin + failover: 401/403/429 = key ini habis/invalid,
            # geser ke key berikut (kuota 10k/bln dihitung per akun).
            keys = settings.COINGECKO_API_KEYS or (None,)
            mulai = next_coingecko_index() % len(keys)
            for j in range(len(keys)):
                key = keys[(mulai + j) % len(keys)]
                response = await client.get(COINGECKO_API_URL, params={
                    "ids": ",".join(sorted(set(COINGECKO_IDS.values()))),
                    "vs_currencies": "idr,usd", "include_last_updated_at": "true",
                }, headers={"x-cg-demo-api-key": key} if key else {})
                if response.status_code in (401, 403, 429):
                    logger.warning(
                        "CoinGecko HTTP %s (key %d/%d), geser ke key berikut",
                        response.status_code, j + 1, len(keys),
                    )
                    continue
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and any(k in data for k in set(COINGECKO_IDS.values())):
                    return data
                return None
            logger.warning("Semua %d key CoinGecko gagal (limit/invalid), pakai fallback", len(keys))
        except Exception as exc:
            logger.warning("CoinGecko tidak tersedia (%s)", type(exc).__name__)
        return None

    async def _fetch_fx(self):
        """Kurs USD->IDR untuk fallback harga (tanpa API key)."""
        try:
            client = await self._get_client()
            for url in FX_URLS:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    fx = float(resp.json()["rates"]["IDR"])
                    if fx > 0:
                        return fx
                except Exception:
                    continue
            logger.warning("Kurs USD/IDR fallback tidak tersedia")
        except Exception as exc:
            logger.warning("Kurs USD/IDR fallback tidak tersedia (%s)", type(exc).__name__)
        return None

    def _rows_from_usd(self, usd_by_coin_id, fx):
        """Susun baris cache dari harga USD: stablecoin 1 USD x kurs."""
        stamp = int(time.time())
        data = {}
        for coin_id in set(COINGECKO_IDS.values()):
            if coin_id in STABLE_COIN_IDS:
                data[coin_id] = {"idr": fx, "usd": 1.0, "last_updated_at": stamp}
                continue
            price_usd = usd_by_coin_id.get(coin_id)
            if price_usd and price_usd > 0:
                data[coin_id] = {"idr": price_usd * fx, "usd": price_usd, "last_updated_at": stamp}
        return data or None

    async def _fetch_paprika(self, fx):
        """Fallback 1 (tanpa key): satu call ticker CoinPaprika, harga USD x kurs."""
        try:
            client = await self._get_client()
            resp = await client.get(PAPRIKA_TICKERS_URL)
            resp.raise_for_status()
            by_id = {row.get("id"): row for row in resp.json() if isinstance(row, dict)}
            usd = {}
            for coin_id, paprika_id in PAPRIKA_IDS.items():
                row = by_id.get(paprika_id)
                if row:
                    price = ((row.get("quotes") or {}).get("USD") or {}).get("price")
                    if price:
                        usd[coin_id] = float(price)
            return self._rows_from_usd(usd, fx)
        except Exception as exc:
            logger.warning("Fallback CoinPaprika tidak tersedia (%s)", type(exc).__name__)
        return None

    async def _fetch_cmc(self, fx):
        """Fallback 2 (key CoinMarketCap): quotes USD x kurs, hemat kuota harian."""
        if not settings.COINMARKETCAP_API_KEY:
            return None
        try:
            client = await self._get_client()
            resp = await client.get(CMC_QUOTES_URL, params={
                "symbol": ",".join(sorted(set(CMC_SYMBOLS.values()))),
                "convert": "USD",
            }, headers={"X-CMC_PRO_API_KEY": settings.COINMARKETCAP_API_KEY})
            resp.raise_for_status()
            raw = (resp.json() or {}).get("data") or {}
            by_symbol = {str(k).upper(): v for k, v in raw.items()}
            usd = {}
            for coin_id, symbol in CMC_SYMBOLS.items():
                row = by_symbol.get(symbol.upper())
                if row:
                    price = ((row.get("quote") or {}).get("USD") or {}).get("price")
                    if price:
                        usd[coin_id] = float(price)
            return self._rows_from_usd(usd, fx)
        except Exception as exc:
            logger.warning("Fallback CoinMarketCap tidak tersedia (%s)", type(exc).__name__)
        return None

    async def _refresh(self, force=False):
        async with self._lock:
            now = time.time()
            if not force and self._cache and now - self._cache.get("_fetched_at", 0) < CACHE_TTL_SECONDS:
                return
            data = await self._fetch_coingecko()
            source = "CoinGecko IDR"
            if data is None:
                fx = await self._fetch_fx()
                if fx:
                    data = await self._fetch_paprika(fx)
                    source = "CoinPaprika+FX"
                    if data is None:
                        data = await self._fetch_cmc(fx)
                        source = "CoinMarketCap+FX"
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
        # Non-force: hormati CACHE_TTL. force=True = 1 call API per tick
        # scheduler tiap 30 dtk dan kuota 10k/bln habis dalam ~3 hari.
        await self._refresh()

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

