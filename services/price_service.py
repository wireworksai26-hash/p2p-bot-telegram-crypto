"""
services/price_service.py — Service untuk ambil harga crypto dari Binance API.

Fitur:
- Fetch harga dari Binance API (GET /api/v3/ticker/price)
- Cache harga dengan TTL 30 detik biar nggak spam API
- Support konversi semua crypto ke IDR via USDT rate
- CoinGecko fallback untuk token yang nggak ada di Binance (Gravity/G)
- Spread markup (buy) dan markdown (sell) dari PriceConfig database
"""

import logging
import time
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from config.settings import settings
from database import crud

logger = logging.getLogger(__name__)

# Mapping symbol ke Binance pair name
# USDT langsung punya pair IDR (USDTBIDR), yang lain lewat USDT dulu
BINANCE_PAIRS = {
    "USDT":  "USDTBIDR",
    "USDC":  "USDCUSDT",
    "ETH":   "ETHUSDT",
    "BNB":   "BNBUSDT",
    "SOL":   "SOLUSDT",
    "AVAX":  "AVAXUSDT",
    "TRX":   "TRXUSDT",
    "MATIC": "MATICUSDT",
    "ARB":   "ARBUSDT",
    "SUI":   "SUIUSDT",
    "TON":   "TONUSDT",
    "KAIA":  "KAIAUSDT",
    "BERA":  "BERAUSDT",
    "APT":   "APTUSDT",
    "OP":    "OPUSDT",
    "G":     "GUSDT",
}

COINGECKO_IDS = {
    "USDT": "tether",
    "USDC": "usd-coin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
    "AVAX": "avalanche-2",
    "TRX": "tron",
    "MATIC": "matic-network",
    "ARB": "arbitrum",
    "SUI": "sui",
    "TON": "the-open-network",
    "KAIA": "kaia",
    "BERA": "berachain-bera",
    "APT": "aptos",
    "OP": "optimism",
    "HYPE": "hyperliquid",
    "G": "gravity-bridge",
    "BASE": "ethereum",
    "ETH_ROBINHOOD": "ethereum",
}

# Binance API endpoints (data-api.binance.vision tidak terkena geoblock cloud US/EU)
BINANCE_API_ENDPOINTS = [
    "https://data-api.binance.vision/api/v3/ticker/price",
    "https://api1.binance.com/api/v3/ticker/price",
    "https://api2.binance.com/api/v3/ticker/price",
    "https://api.binance.com/api/v3/ticker/price",
]

# CoinGecko API base URL (free, no key needed)
COINGECKO_API_URL = "https://api.coingecko.com/api/v3/simple/price"

# Cache TTL dalam detik
CACHE_TTL_SECONDS = 30


class PriceService:
    """
    Service untuk mengambil dan menghitung harga crypto dalam IDR.

    Cara kerja:
    1. Ambil rate USDT/IDR dari Binance (base rate)
    2. Ambil rate SYMBOL/USDT dari Binance
    3. market_price_idr = symbol_usdt_rate * usdt_idr_rate
    4. buy_price = market_price * (1 + spread_pct / 100)  ← user beli lebih mahal
    5. sell_price = market_price * (1 - spread_pct / 100)  ← user jual lebih murah
    """

    def __init__(self):
        # Cache: {pair_name: {"price": float, "timestamp": float}}
        self._cache: dict[str, dict] = {}
        # HTTP client (akan di-init saat pertama kali dipakai)
        self._client: Optional[httpx.AsyncClient] = None
        # Circuit breaker properties
        self.binance_online = True
        self.binance_last_checked = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy init async HTTP client. Reuse client untuk efisiensi."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def close(self):
        """Tutup HTTP client saat service shutdown."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("PriceService HTTP client closed.")

    # ============================================================
    # BINANCE API — Ambil harga dari Binance
    # ============================================================

    async def get_binance_price(self, pair: str) -> Optional[float]:
        """
        Ambil harga satu pair dari Binance API.
        Return None jika gagal (pair tidak ada, network error, dll).
        """
        cached = self._cache.get(pair)
        if cached and (time.time() - cached["timestamp"] < CACHE_TTL_SECONDS):
            return cached["price"]

        client = await self._get_client()
        for endpoint in BINANCE_API_ENDPOINTS:
            try:
                response = await client.get(
                    endpoint,
                    params={"symbol": pair},
                    timeout=3.0
                )
                response.raise_for_status()
                data = response.json()
                price = float(data["price"])

                self.binance_online = True
                self._cache[pair] = {
                    "price": price,
                    "timestamp": time.time(),
                }
                return price
            except Exception as e:
                continue

        logger.warning(f"Semua Binance endpoint gagal untuk pair {pair}.")
        self.binance_online = False
        self.binance_last_checked = time.time()
        if cached:
            return cached["price"]
        return None

    # ============================================================
    # COINGECKO FALLBACK — Untuk token yang nggak ada di Binance
    # ============================================================

    async def get_coingecko_price(self, coin_id: str) -> Optional[float]:
        """
        Ambil harga dari CoinGecko API (fallback).
        Return harga dalam USD. Perlu dikali USDT/IDR rate untuk konversi ke IDR.
        """
        cache_key = f"coingecko_{coin_id}"
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached["timestamp"]) < CACHE_TTL_SECONDS:
            return cached["price"]

        try:
            client = await self._get_client()
            response = await client.get(
                COINGECKO_API_URL,
                params={
                    "ids": coin_id,
                    "vs_currencies": "usd",
                }
            )
            response.raise_for_status()

            data = response.json()
            price_usd = float(data[coin_id]["usd"])

            self._cache[cache_key] = {
                "price": price_usd,
                "timestamp": time.time(),
            }

            return price_usd

        except Exception as e:
            logger.error(f"Gagal fetch harga dari CoinGecko ({coin_id}): {e}")
            if cached:
                return cached["price"]
            return None

    # ============================================================
    # MAIN PRICE METHODS — Hitung harga beli/jual dalam IDR
    # ============================================================

    async def get_coingecko_price_idr(self, symbol: str) -> Optional[float]:
        """
        Ambil harga token langsung dalam IDR dari CoinGecko API.
        """
        symbol = symbol.upper()
        coin_id = COINGECKO_IDS.get(symbol)
        if not coin_id:
            logger.error(f"Symbol {symbol} tidak memiliki CoinGecko ID.")
            return None

        cache_key = f"coingecko_idr_{symbol}"
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached["timestamp"]) < CACHE_TTL_SECONDS:
            return cached["price"]

        try:
            client = await self._get_client()
            response = await client.get(
                COINGECKO_API_URL,
                params={
                    "ids": coin_id,
                    "vs_currencies": "idr",
                }
            )
            response.raise_for_status()

            data = response.json()
            price_idr = float(data[coin_id]["idr"])

            # Cache the result
            self._cache[cache_key] = {
                "price": price_idr,
                "timestamp": time.time(),
            }

            return price_idr

        except Exception as e:
            logger.error(f"Gagal fetch harga IDR dari CoinGecko ({symbol}): {e}")
            if cached:
                logger.info(f"Menggunakan cached price untuk CoinGecko IDR {symbol}")
                return cached["price"]
            return None

    async def _get_price_binance(self, symbol: str, db: Session) -> Optional[dict]:
        """
        Ambil harga buy/sell dalam IDR untuk satu symbol via Binance API.
        """
        symbol = symbol.upper()
        try:
            # Step 1: Ambil USDT/IDR rate realtime (Indodax -> CoinGecko -> ER-API)
            usdt_idr = await self.get_realtime_usdt_idr_fallback()
            if usdt_idr is None or usdt_idr < 10000:
                usdt_idr = 16000.0

            # Step 2: Tentukan market price IDR berdasarkan symbol
            if symbol in ("USDT", "USDC"):
                market_price_idr = usdt_idr
            elif symbol == "BASE":
                eth_usdt = await self.get_binance_price("ETHUSDT")
                if eth_usdt is None:
                    return None
                market_price_idr = eth_usdt * usdt_idr
            elif symbol == "G":
                g_usdt = await self.get_binance_price("GUSDT")
                if g_usdt is not None:
                    market_price_idr = g_usdt * usdt_idr
                else:
                    g_usd = await self.get_coingecko_price(COINGECKO_IDS["G"])
                    if g_usd is None:
                        return None
                    market_price_idr = g_usd * usdt_idr
            else:
                pair = BINANCE_PAIRS.get(symbol)
                if not pair:
                    return None
                symbol_usdt = await self.get_binance_price(pair)
                if symbol_usdt is None:
                    return None
                market_price_idr = symbol_usdt * usdt_idr

            # Step 3: Ambil spread dari database PriceConfig
            price_config = crud.get_price_config(db, symbol)
            spread_pct = float(price_config.spread_pct) if price_config else settings.DEFAULT_SPREAD_PCT
            buy_price_idr = market_price_idr * (1 + spread_pct / 100)
            sell_price_idr = market_price_idr * (1 - spread_pct / 100)

            return {
                "symbol": symbol,
                "market_price_idr": round(market_price_idr, 2),
                "buy_price_idr": round(buy_price_idr, 2),
                "sell_price_idr": round(sell_price_idr, 2),
                "spread_pct": spread_pct,
                "usdt_idr_rate": round(usdt_idr, 2),
            }
        except Exception as e:
            logger.error(f"Error di _get_price_binance untuk {symbol}: {e}")
            return None

    async def get_realtime_usdt_idr_fallback(self) -> float:
        """
        Mendapatkan rate USD/IDR (atau USDT/IDR) realtime secara dinamik tanpa hardcode:
        1. Indodax Ticker API (Pasar lokal Indonesia)
        2. Open ExchangeRates API (Rate pasar uang dunia)
        3. CoinGecko Tether IDR API
        """
        cached = self._cache.get("realtime_usdt_idr_rate")
        if cached and (time.time() - cached["timestamp"]) < CACHE_TTL_SECONDS:
            return cached["price"]

        client = await self._get_client()

        # 1. Try Indodax API (Indonesia's #1 Exchange)
        try:
            res = await client.get("https://indodax.com/api/ticker/usdtidr", timeout=3.0)
            if res.status_code == 200:
                rate = float(res.json()["ticker"]["last"])
                if rate > 0:
                    self._cache["realtime_usdt_idr_rate"] = {"price": rate, "timestamp": time.time()}
                    logger.info(f"Realtime USDT/IDR rate dari Indodax: Rp {rate:,.2f}")
                    return rate
        except Exception as e:
            logger.warning(f"Indodax rate fetch failed: {e}")

        # 2. Try Open Exchange Rates API
        try:
            res = await client.get("https://open.er-api.com/v6/latest/USD", timeout=3.0)
            if res.status_code == 200:
                rate = float(res.json()["rates"]["IDR"])
                if rate > 0:
                    self._cache["realtime_usdt_idr_rate"] = {"price": rate, "timestamp": time.time()}
                    logger.info(f"Realtime USD/IDR rate dari ER-API: Rp {rate:,.2f}")
                    return rate
        except Exception as e:
            logger.warning(f"ER-API rate fetch failed: {e}")

        # 3. Try CoinGecko API
        try:
            res = await client.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "tether", "vs_currencies": "idr"}, timeout=3.0)
            if res.status_code == 200:
                rate = float(res.json()["tether"]["idr"])
                if rate > 0:
                    self._cache["realtime_usdt_idr_rate"] = {"price": rate, "timestamp": time.time()}
                    logger.info(f"Realtime USDT/IDR rate dari CoinGecko: Rp {rate:,.2f}")
                    return rate
        except Exception as e:
            logger.warning(f"CoinGecko rate fetch failed: {e}")

        return 16000.0

    async def get_price(self, symbol: str, db: Session = None) -> Optional[dict]:
        """
        Ambil harga buy/sell dalam IDR untuk satu symbol.
        Mencoba Binance API terlebih dahulu, jika gagal otomatis beralih ke Indodax/CoinGecko.
        """
        symbol = symbol.upper()
        
        # If db is not provided, manage local session
        local_db = False
        if db is None:
            from database.connection import SessionLocal
            db = SessionLocal()
            local_db = True

        try:
            # 1. Coba lewat Binance dulu
            price_data = await self._get_price_binance(symbol, db)
            if price_data:
                return price_data
                
            # 2. Fallback ke Realtime USD/IDR & CoinGecko
            logger.warning(f"Gagal mengambil harga {symbol} dari Binance. Menggunakan fallback realtime Indodax/CoinGecko...")
            
            realtime_usdt_idr = await self.get_realtime_usdt_idr_fallback()

            if symbol in ["USDT", "USDC"]:
                market_price_idr = realtime_usdt_idr
            else:
                price_usd = await self.get_coingecko_price(COINGECKO_IDS.get(symbol, symbol.lower()))
                if price_usd is not None:
                    market_price_idr = price_usd * realtime_usdt_idr
                else:
                    market_price_idr = await self.get_coingecko_price_idr(symbol)

            if market_price_idr is None:
                logger.error(f"Semua sumber harga gagal untuk {symbol}!")
                return None
                    
            # Ambil spread dari database PriceConfig
            price_config = crud.get_price_config(db, symbol)
            spread_pct = float(price_config.spread_pct) if price_config else settings.DEFAULT_SPREAD_PCT
            buy_price_idr = market_price_idr * (1 + spread_pct / 100)
            sell_price_idr = market_price_idr * (1 - spread_pct / 100)

            return {
                "symbol": symbol,
                "market_price_idr": round(market_price_idr, 2),
                "buy_price_idr": round(buy_price_idr, 2),
                "sell_price_idr": round(sell_price_idr, 2),
                "spread_pct": spread_pct,
                "usdt_idr_rate": round(realtime_usdt_idr, 2),
            }
        except Exception as e:
            logger.error(f"Gagal total mendapatkan harga {symbol}: {e}")
            return None
        finally:
            if local_db and db:
                db.close()


    async def get_coingecko_prices_idr_batch(self, symbols: list[str]) -> dict[str, float]:
        """
        Ambil harga beberapa token sekaligus dalam IDR dari CoinGecko API.
        Membantu menghemat request & mempercepat loading (/price) saat Binance diblokir.
        """
        coin_ids = []
        symbol_to_id = {}
        for sym in symbols:
            sym_upper = sym.upper()
            coin_id = COINGECKO_IDS.get(sym_upper)
            if coin_id:
                coin_ids.append(coin_id)
                symbol_to_id[sym_upper] = coin_id

        if not coin_ids:
            return {}

        try:
            client = await self._get_client()
            response = await client.get(
                COINGECKO_API_URL,
                params={
                    "ids": ",".join(coin_ids),
                    "vs_currencies": "idr",
                }
            )
            response.raise_for_status()
            data = response.json()

            prices = {}
            for sym, coin_id in symbol_to_id.items():
                if coin_id in data and "idr" in data[coin_id]:
                    prices[sym] = float(data[coin_id]["idr"])
            return prices
        except Exception as e:
            logger.error(f"Gagal fetch batch harga IDR dari CoinGecko: {e}")
            return {}

    async def get_all_prices(self, db: Session) -> dict:
        """
        Ambil semua harga untuk semua symbol yang aktif.
        Jika Binance terdeteksi offline (circuit breaker),
        akan menggunakan batch fetch CoinGecko secara langsung.

        Returns:
            {
                "USDT": {...price_data...},
                "ETH": {...price_data...},
                ...
            }
        """
        prices = {}
        # Ambil semua symbol yang aktif dari database
        active_configs = crud.get_all_price_configs(db)
        symbols = [config.symbol for config in active_configs]

        # Kalau belum ada config di DB, pakai default dari BINANCE_PAIRS + BASE
        if not symbols:
            symbols = list(BINANCE_PAIRS.keys()) + ["BASE"]

        # Cek status Binance terlebih dahulu secara cepat
        if self.binance_online:
            try:
                # Cek status base rate USDT/IDR
                usdt_price = await self.get_binance_price("USDTBIDR")
                if usdt_price is None:
                    # Tandai offline agar sisanya langsung menggunakan batch CoinGecko
                    self.binance_online = False
                    self.binance_last_checked = time.time()
                    logger.warning("Koneksi Binance offline. Menggunakan fallback CoinGecko batch.")
            except Exception:
                self.binance_online = False
                self.binance_last_checked = time.time()

        # Mode fallback cepat: Circuit Breaker aktif (Binance offline)
        if not self.binance_online and (time.time() - self.binance_last_checked) < 300:
            logger.info("Binance offline (circuit breaker). Mengambil seluruh harga dari CoinGecko secara batch...")
            cg_prices = await self.get_coingecko_prices_idr_batch(symbols)
            for symbol in symbols:
                market_price_idr = cg_prices.get(symbol)
                # Coba ambil dari cache jika CoinGecko gagal mendadak
                if market_price_idr is None:
                    cache_key = f"coingecko_idr_{symbol}"
                    cached = self._cache.get(cache_key)
                    if cached:
                        market_price_idr = cached["price"]

                if market_price_idr is not None:
                    # Ambil spread_pct
                    price_config = crud.get_price_config(db, symbol)
                    spread_pct = float(price_config.spread_pct) if price_config else settings.DEFAULT_SPREAD_PCT
                    buy_price_idr = market_price_idr * (1 + spread_pct / 100)
                    sell_price_idr = market_price_idr * (1 - spread_pct / 100)
                    prices[symbol] = {
                        "symbol": symbol,
                        "market_price_idr": round(market_price_idr, 2),
                        "buy_price_idr": round(buy_price_idr, 2),
                        "sell_price_idr": round(sell_price_idr, 2),
                        "spread_pct": spread_pct,
                        "usdt_idr_rate": 16000.0,
                    }
            return prices

        # Mode normal: Binance online (iterasi biasa)
        for symbol in symbols:
            try:
                price_data = await self.get_price(symbol, db)
                if price_data:
                    prices[symbol] = price_data
                else:
                    logger.warning(f"Harga untuk {symbol} tidak tersedia.")
            except Exception as e:
                logger.error(f"Error fetching price for {symbol}: {e}")

        return prices

    async def refresh_all_prices(self):
        """
        Refresh cache untuk seluruh pair Binance agar cache terisi.
        Dipanggil secara berkala oleh scheduler di main.py.
        """
        try:
            # Refresh base rate USDT/IDR
            await self.get_binance_price("USDTBIDR")
            # Refresh rate pair symbol/USDT lainnya
            for pair in BINANCE_PAIRS.values():
                if pair != "USDTBIDR":
                    await self.get_binance_price(pair)
            logger.debug("Cache harga berhasil di-refresh dari Binance.")
        except Exception as e:
            logger.error(f"Gagal refresh semua harga di PriceService: {e}")



# Singleton instance — pakai ini di seluruh aplikasi
price_service = PriceService()
