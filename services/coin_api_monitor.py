"""
services/coin_api_monitor.py — Alarm Deteksi Dini URL & API Koin.
================================================================
Memantau secara proaktif ketersediaan dan status kesehatan dari:
1. API Harga Pasar Koin (CoinGecko)
2. Blockchain RPC Endpoints (EVM: BSC, ETH, Polygon, Base, Arbitrum, HyperEVM, dll.)
3. Non-EVM Nodes (Solana, Tron, TON, SUI, Aptos)

Mendeteksi:
- 404 Not Found (URL endpoint berubah / dipindahkan)
- 401 / 403 Forbidden (API key invalid / IP blocked)
- 429 Too Many Requests (Rate limit habis)
- 500 / 502 / 503 Server Error (Server node down)
- Timeout & Connection Refused (Domain mati / tidak merespons)
- Chain ID mismatch

Jika ada URL koin yang berubah atau mati:
- Mengirim pesan ALARM langsung ke admin Telegram via notify_admins.
- Memberikan instruksi variabel environment (.env / Railway) yang harus diganti.
- Mengirim notifikasi pemulihan (RECOVERED) saat URL kembali normal.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import httpx

from config.settings import settings
from bot.utils.telegram_utils import notify_admins

logger = logging.getLogger(__name__)

# Batas latensi untuk status DEGRADED (dalam milidetik)
LATENCY_DEGRADED_THRESHOLD_MS = 4000
TIMEOUT_SECONDS = 6.0

# Jeda pengingat ulang jika endpoint tetap DOWN (2 jam = 7200 detik)
REMINDER_INTERVAL_SECONDS = 7200


class CoinAPIMonitor:
    """Pemantau proaktif URL & API koin."""

    def __init__(self):
        self._endpoint_states: Dict[str, Dict[str, Any]] = {}
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def get_configured_endpoints(self) -> List[Dict[str, Any]]:
        """Daftar seluruh endpoint API & RPC koin yang dipantau beserta variabel env-nya."""
        return [
            # 1. Price Service API
            {
                "id": "PRICE_COINGECKO",
                "category": "PRICE_API",
                "name": "CoinGecko Price API",
                "symbol": "ALL",
                "network": "MARKET",
                "url": "https://api.coingecko.com/api/v3/simple/price",
                "env_var": "COINGECKO_API_KEY",
                "fallback_urls": ["https://api.coinpaprika.com/v1/tickers?quotes=USD"],
                "impact": "Seluruh perhitungan harga Beli, Jual, dan Swap IDR berhenti.",
            },
            # 2. EVM Blockchain RPCs
            {
                "id": "EVM_BSC",
                "category": "EVM_RPC",
                "name": "BNB Smart Chain (BSC)",
                "symbol": "BNB / USDT",
                "network": "BSC",
                "chain_id": 56,
                "url": settings.BSC_RPC,
                "env_var": "BSC_RPC",
                "fallback_urls": [
                    "https://bsc-dataseed.bnbchain.org",
                    "https://1rpc.io/bnb",
                ],
                "impact": "Transaksi koin BNB / USDT jaringan BSC tidak dapat diproses.",
            },
            {
                "id": "EVM_POLYGON",
                "category": "EVM_RPC",
                "name": "Polygon (POL / MATIC)",
                "symbol": "POL / USDT",
                "network": "POLYGON",
                "chain_id": 137,
                "url": settings.POLYGON_RPC,
                "env_var": "POLYGON_RPC",
                "fallback_urls": [
                    "https://polygon-bor-rpc.publicnode.com",
                    "https://polygon-rpc.com",
                    "https://1rpc.io/matic",
                ],
                "impact": "Transaksi koin POL / MATIC / USDT jaringan Polygon tidak dapat diproses.",
            },
            {
                "id": "EVM_BASE",
                "category": "EVM_RPC",
                "name": "Base Network",
                "symbol": "ETH / USDC",
                "network": "BASE",
                "chain_id": 8453,
                "url": settings.BASE_RPC,
                "env_var": "BASE_RPC",
                "fallback_urls": [
                    "https://mainnet.base.org",
                    "https://base-rpc.publicnode.com",
                    "https://1rpc.io/base",
                ],
                "impact": "Transaksi ETH / USDC jaringan Base tidak dapat diproses.",
            },
            {
                "id": "EVM_ARB",
                "category": "EVM_RPC",
                "name": "Arbitrum One",
                "symbol": "ETH / ARB",
                "network": "ARB",
                "chain_id": 42161,
                "url": settings.ARB_RPC,
                "env_var": "ARB_RPC",
                "fallback_urls": [
                    "https://arb1.arbitrum.io/rpc",
                    "https://arbitrum-one-rpc.publicnode.com",
                    "https://1rpc.io/arb",
                ],
                "impact": "Transaksi ETH / ARB / USDT jaringan Arbitrum tidak dapat diproses.",
            },
            {
                "id": "EVM_HYPEREVM",
                "category": "EVM_RPC",
                "name": "HyperEVM",
                "symbol": "HYPE",
                "network": "HYPEREVM",
                "chain_id": 999,
                "url": settings.HYPEREVM_RPC,
                "env_var": "HYPEREVM_RPC",
                "fallback_urls": [
                    "https://rpc.hyperliquid.xyz/evm",
                ],
                "impact": "Transaksi koin HYPE jaringan HyperEVM tidak dapat diproses.",
            },
            {
                "id": "EVM_ETH",
                "category": "EVM_RPC",
                "name": "Ethereum Mainnet",
                "symbol": "ETH / USDT",
                "network": "ETH",
                "chain_id": 1,
                "url": settings.ETH_RPC,
                "env_var": "ETH_RPC",
                "fallback_urls": [
                    "https://ethereum-rpc.publicnode.com",
                    "https://cloudflare-eth.com",
                    "https://1rpc.io/eth",
                ],
                "impact": "Transaksi ETH / USDT / USDC jaringan Ethereum tidak dapat diproses.",
            },
            {
                "id": "EVM_AVAX",
                "category": "EVM_RPC",
                "name": "Avalanche C-Chain",
                "symbol": "AVAX",
                "network": "AVAX",
                "chain_id": 43114,
                "url": settings.AVAX_RPC,
                "env_var": "AVAX_RPC",
                "fallback_urls": [
                    "https://api.avax.network/ext/bc/C/rpc",
                    "https://avalanche-c-chain-rpc.publicnode.com",
                ],
                "impact": "Transaksi koin AVAX jaringan Avalanche tidak dapat diproses.",
            },
            {
                "id": "EVM_OPTIMISM",
                "category": "EVM_RPC",
                "name": "Optimism (OP Mainnet)",
                "symbol": "ETH / OP",
                "network": "OPTIMISM",
                "chain_id": 10,
                "url": settings.OPTIMISM_RPC,
                "env_var": "OPTIMISM_RPC",
                "fallback_urls": [
                    "https://mainnet.optimism.io",
                    "https://optimism-rpc.publicnode.com",
                ],
                "impact": "Transaksi ETH jaringan Optimism tidak dapat diproses.",
            },
            {
                "id": "EVM_ROBINHOOD",
                "category": "EVM_RPC",
                "name": "Robinhood Chain",
                "symbol": "ETH",
                "network": "ROBINHOOD",
                "chain_id": 4663,
                "url": settings.ROBINHOOD_RPC,
                "env_var": "ROBINHOOD_RPC",
                "fallback_urls": [
                    "https://rpc.mainnet.chain.robinhood.com",
                    "https://robinhood-rpc.publicnode.com",
                ],
                "impact": "Transaksi ETH jaringan Robinhood tidak dapat diproses.",
            },
            {
                "id": "EVM_KAIA",
                "category": "EVM_RPC",
                "name": "Kaia Network",
                "symbol": "KAIA",
                "network": "KAIA",
                "chain_id": 8217,
                "url": settings.KAIA_RPC,
                "env_var": "KAIA_RPC",
                "fallback_urls": [
                    "https://public-en.node.kaia.io",
                    "https://klaytn.drpc.org",
                ],
                "impact": "Transaksi koin KAIA jaringan Kaia tidak dapat diproses.",
            },
            {
                "id": "EVM_BERA",
                "category": "EVM_RPC",
                "name": "Berachain",
                "symbol": "BERA",
                "network": "BERA",
                "chain_id": 80094,
                "url": settings.BERA_RPC,
                "env_var": "BERA_RPC",
                "fallback_urls": [
                    "https://rpc.berachain.com",
                    "https://berachain.drpc.org",
                ],
                "impact": "Transaksi koin BERA jaringan Berachain tidak dapat diproses.",
            },
            {
                "id": "NONEVM_TRON",
                "category": "NONEVM_RPC",
                "name": "TronGrid (TRON)",
                "symbol": "TRX / USDT",
                "network": "TRON",
                "url": settings.TRX_RPC,
                "env_var": "TRX_RPC",
                "fallback_urls": [
                    "https://api.trongrid.io",
                ],
                "impact": "Transaksi koin TRX dan USDT jaringan TRON tidak dapat diproses.",
            },
            {
                "id": "NONEVM_TON",
                "category": "NONEVM_RPC",
                "name": "The Open Network (TON / GRAM)",
                "symbol": "GRAM / USDT",
                "network": "TON",
                "url": settings.TON_RPC,
                "env_var": "TON_RPC",
                "fallback_urls": [
                    "https://toncenter.com/api/v2/jsonRPC",
                ],
                "impact": "Transaksi koin GRAM dan USDT di The Open Network tidak dapat diproses.",
            },
            {
                "id": "NONEVM_SUI",
                "category": "NONEVM_RPC",
                "name": "Sui Network",
                "symbol": "SUI",
                "network": "SUI",
                "url": settings.SUI_RPC,
                "env_var": "SUI_RPC",
                "fallback_urls": [
                    "https://sui-mainnet-endpoint.blockvision.org",
                ],
                "impact": "Transaksi koin SUI tidak dapat diproses.",
            },
            {
                "id": "NONEVM_APTOS",
                "category": "NONEVM_RPC",
                "name": "Aptos Mainnet",
                "symbol": "APT",
                "network": "APTOS",
                "url": settings.APTOS_RPC,
                "env_var": "APTOS_RPC",
                "fallback_urls": [
                    "https://fullnode.mainnet.aptoslabs.com/v1",
                ],
                "impact": "Transaksi koin APT di Aptos tidak dapat diproses.",
            },
        ]

    async def check_single_endpoint(self, ep: Dict[str, Any]) -> Dict[str, Any]:
        """Cek URL utama; bila DOWN, coba fallback sebelum melaporkan DOWN."""
        client = await self._get_client()
        url = ep["url"]
        result = {
            "id": ep["id"],
            "name": ep["name"],
            "category": ep["category"],
            "network": ep.get("network", ""),
            "symbol": ep.get("symbol", ""),
            "url": url,
            "env_var": ep.get("env_var", ""),
            "fallback_urls": ep.get("fallback_urls", []),
            "impact": ep.get("impact", ""),
            "status": "DOWN",
            "latency_ms": 0,
            "block_info": "",
            "error_code": None,
            "error_detail": "",
            "checked_at": datetime.now(timezone.utc),
        }

        if not url:
            result["error_code"] = "URL_EMPTY"
            result["error_detail"] = f"Variabel environment {ep['env_var']} kosong."
            return result

        result.update(await self._probe(client, url, ep))

        if result["status"] == "DOWN" and ep.get("fallback_urls"):
            for fallback in ep["fallback_urls"]:
                if not fallback or fallback == url:
                    continue
                fallback_result = await self._probe(client, fallback, ep)
                if fallback_result["status"] in ("OK", "DEGRADED"):
                    result.update(fallback_result)
                    result["status"] = "DEGRADED"
                    result["error_code"] = "PRIMARY_DOWN"
                    result["error_detail"] = f"URL utama tidak terjangkau; fallback aktif: {fallback}"
                    break

        return result

    async def _probe(self, client, url: str, ep: Dict[str, Any]) -> Dict[str, Any]:
        """Ping satu URL dan kembalikan status, latensi, serta detail kesalahan."""
        category = ep["category"]
        start_time = time.time()
        result = {
            "status": "DOWN",
            "latency_ms": 0,
            "block_info": "",
            "error_code": None,
            "error_detail": "",
        }

        try:
            # 1. Price API Check
            if category == "PRICE_API":
                headers = {}
                if settings.COINGECKO_API_KEY:
                    headers["x-cg-demo-api-key"] = settings.COINGECKO_API_KEY
                if "coinpaprika.com" in url:
                    resp = await client.get(url)
                else:
                    resp = await client.get(
                        url,
                        params={"ids": "tether", "vs_currencies": "idr"},
                        headers=headers,
                    )
                latency = int((time.time() - start_time) * 1000)
                result["latency_ms"] = latency

                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data and isinstance(data[0], dict) and (data[0].get("quotes") or {}).get("USD"):
                        result["status"] = "DEGRADED" if latency > LATENCY_DEGRADED_THRESHOLD_MS else "OK"
                        result["block_info"] = f"CoinPaprika: {len(data)} ticker"
                    elif isinstance(data, dict) and "tether" in data and "idr" in data["tether"]:
                        result["status"] = "DEGRADED" if latency > LATENCY_DEGRADED_THRESHOLD_MS else "OK"
                        result["block_info"] = f"USDT: Rp {data['tether']['idr']:,}"
                    else:
                        result["error_code"] = "RESPONSE_INVALID"
                        result["error_detail"] = "Format response JSON tidak memuat kurs idr."
                else:
                    self._parse_http_error(resp.status_code, result)

            # 2. EVM RPC Check
            elif category == "EVM_RPC":
                payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
                resp = await client.post(url, json=payload)
                latency = int((time.time() - start_time) * 1000)
                result["latency_ms"] = latency

                if resp.status_code == 200:
                    data = resp.json()
                    if "result" in data and data["result"]:
                        block_num = int(data["result"], 16)
                        # Sebagian endpoint menjawab eth_blockNumber tetapi gagal untuk state call
                        # (contoh nyata: KAIA drpc) -> probe eth_gasPrice sebelum bilang OK.
                        gas = await client.post(url, json={
                            "jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 2})
                        gas_data = gas.json() if gas.status_code == 200 else {}
                        if gas.status_code != 200 or not gas_data.get("result"):
                            result["error_code"] = "STATE_CALL_FAILED"
                            result["error_detail"] = (
                                "Endpoint menjawab eth_blockNumber tetapi gagal eth_gasPrice "
                                f"(HTTP {gas.status_code}); endpoint kemungkinan paruh-rusak."
                            )
                        else:
                            result["status"] = "DEGRADED" if latency > LATENCY_DEGRADED_THRESHOLD_MS else "OK"
                            result["block_info"] = f"Block #{block_num:,}"
                    elif "error" in data:
                        result["error_code"] = "RPC_ERROR"
                        result["error_detail"] = str(data["error"].get("message", data["error"]))
                    else:
                        result["error_code"] = "RESPONSE_INVALID"
                        result["error_detail"] = "Response JSON-RPC tidak memuat hasil 'result'."
                else:
                    self._parse_http_error(resp.status_code, result)

            # 3. Non-EVM RPCs
            elif category == "NONEVM_RPC":
                net = ep.get("network")
                if net == "SOLANA":
                    payload = {"jsonrpc": "2.0", "method": "getSlot", "params": [], "id": 1}
                    resp = await client.post(url, json=payload)
                    latency = int((time.time() - start_time) * 1000)
                    result["latency_ms"] = latency
                    if resp.status_code == 200:
                        data = resp.json()
                        if "result" in data:
                            result["status"] = "DEGRADED" if latency > LATENCY_DEGRADED_THRESHOLD_MS else "OK"
                            result["block_info"] = f"Slot #{data['result']:,}"
                        else:
                            result["error_code"] = "RPC_ERROR"
                            result["error_detail"] = str(data.get("error", "Tanpa field result"))
                    else:
                        self._parse_http_error(resp.status_code, result)

                elif net == "TRON":
                    tron_url = f"{url.rstrip('/')}/wallet/getnowblock"
                    headers = {"TRON-PRO-API-KEY": settings.TRONGRID_API_KEY} if settings.TRONGRID_API_KEY else {}
                    resp = await client.get(tron_url, headers=headers)
                    latency = int((time.time() - start_time) * 1000)
                    result["latency_ms"] = latency
                    if resp.status_code == 200:
                        data = resp.json()
                        block_num = data.get("block_header", {}).get("raw_data", {}).get("number")
                        if block_num is not None:
                            result["status"] = "DEGRADED" if latency > LATENCY_DEGRADED_THRESHOLD_MS else "OK"
                            result["block_info"] = f"Block #{block_num:,}"
                        else:
                            result["status"] = "OK"
                            result["block_info"] = "TronGrid Active"
                    else:
                        self._parse_http_error(resp.status_code, result)

                elif net == "TON":
                    payload = {"jsonrpc": "2.0", "method": "getMasterchainInfo", "params": [], "id": 1}
                    headers = {"X-API-Key": settings.TON_API_KEY} if settings.TON_API_KEY else {}
                    resp = await client.post(url, json=payload, headers=headers)
                    latency = int((time.time() - start_time) * 1000)
                    result["latency_ms"] = latency
                    if resp.status_code == 200:
                        data = resp.json()
                        if "result" in data:
                            seqno = data["result"].get("last", {}).get("seqno")
                            result["status"] = "DEGRADED" if latency > LATENCY_DEGRADED_THRESHOLD_MS else "OK"
                            result["block_info"] = f"Seqno #{seqno:,}" if seqno else "Masterchain OK"
                        else:
                            result["error_code"] = "RPC_ERROR"
                            result["error_detail"] = str(data.get("error", "Tanpa field result"))
                    else:
                        self._parse_http_error(resp.status_code, result)

                elif net == "SUI":
                    payload = {"jsonrpc": "2.0", "method": "sui_getLatestCheckpointSequenceNumber", "params": [], "id": 1}
                    resp = await client.post(url, json=payload)
                    latency = int((time.time() - start_time) * 1000)
                    result["latency_ms"] = latency
                    if resp.status_code == 200:
                        data = resp.json()
                        if "result" in data:
                            cp = int(data["result"])
                            result["status"] = "DEGRADED" if latency > LATENCY_DEGRADED_THRESHOLD_MS else "OK"
                            result["block_info"] = f"Checkpoint #{cp:,}"
                        else:
                            result["error_code"] = "RPC_ERROR"
                            result["error_detail"] = str(data.get("error", "Tanpa result"))
                    else:
                        self._parse_http_error(resp.status_code, result)

                elif net == "APTOS":
                    apt_url = f"{url.rstrip('/')}/"
                    resp = await client.get(apt_url)
                    latency = int((time.time() - start_time) * 1000)
                    result["latency_ms"] = latency
                    if resp.status_code == 200:
                        data = resp.json()
                        ledger = data.get("ledger_version")
                        result["status"] = "DEGRADED" if latency > LATENCY_DEGRADED_THRESHOLD_MS else "OK"
                        result["block_info"] = f"Ledger v{int(ledger):,}" if ledger else "Aptos Node OK"
                    else:
                        self._parse_http_error(resp.status_code, result)

        except httpx.TimeoutException:
            result["status"] = "DOWN"
            result["latency_ms"] = int(TIMEOUT_SECONDS * 1000)
            result["error_code"] = "TIMEOUT"
            result["error_detail"] = f"Timeout: Server tidak merespons dalam {TIMEOUT_SECONDS} detik."

        except httpx.ConnectError:
            result["status"] = "DOWN"
            result["error_code"] = "CONNECT_ERROR"
            result["error_detail"] = "Gagal koneksi (Connection Refused / DNS Error). Domain kemungkinan mati."

        except Exception as exc:
            result["status"] = "DOWN"
            result["error_code"] = type(exc).__name__
            result["error_detail"] = str(exc)

        return result

    def _parse_http_error(self, status_code: int, result: Dict[str, Any]):
        """Menganalisis kode HTTP error untuk memberikan detail manusiawi."""
        result["status"] = "DOWN"
        if status_code == 404:
            result["error_code"] = "HTTP_404"
            result["error_detail"] = "URL endpoint tidak ditemukan (404 Not Found). Kemungkinan rute atau domain sudah berganti."
        elif status_code in (401, 403):
            result["error_code"] = f"HTTP_{status_code}"
            result["error_detail"] = f"Akses ditolak ({status_code} Forbidden/Unauthorized). Memerlukan API key atau IP diblokir."
        elif status_code == 429:
            result["error_code"] = "HTTP_429"
            result["error_detail"] = "Terkena limit request (429 Too Many Requests). Kuota publik provider habis."
        elif status_code in (500, 502, 503, 504):
            result["error_code"] = f"HTTP_{status_code}"
            result["error_detail"] = f"Server node sedang mengalami gangguan internal ({status_code} Server Error)."
        else:
            result["error_code"] = f"HTTP_{status_code}"
            result["error_detail"] = f"Respons HTTP status tidak sukses: {status_code}."

    async def check_all(self) -> List[Dict[str, Any]]:
        """Memeriksa seluruh endpoint secara paralel dan mengembalikan laporannya."""
        endpoints = self.get_configured_endpoints()
        tasks = [self.check_single_endpoint(ep) for ep in endpoints]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return results

    async def check_all_and_alert(self, bot_sender) -> List[Dict[str, Any]]:
        """
        Dijalankan oleh background job APScheduler:
        1. Memeriksa seluruh endpoint.
        2. Jika ada yang berubah ke status DOWN -> kirim ALARM instan ke admin.
        3. Jika ada yang pulih (DOWN -> OK) -> kirim notifikasi pemulihan.
        4. Jika tetap DOWN selama > 2 jam -> kirim pengingat berkala.
        """
        results = await self.check_all()
        now = time.time()

        for res in results:
            ep_id = res["id"]
            current_status = res["status"]
            prev_state = self._endpoint_states.get(ep_id, {})
            prev_status = prev_state.get("status", "UNKNOWN")
            last_alert_at = prev_state.get("last_alert_at", 0)

            should_alert_down = False
            should_alert_recovery = False

            if current_status == "DOWN":
                if prev_status != "DOWN":
                    # Transisi baru ke DOWN -> ALARM INSTAN
                    should_alert_down = True
                elif (now - last_alert_at) >= REMINDER_INTERVAL_SECONDS:
                    # Sudah 2 jam masih DOWN -> Pengingat berkala
                    should_alert_down = True

            elif current_status in ("OK", "DEGRADED") and prev_status == "DOWN":
                # Pulih dari DOWN
                should_alert_recovery = True

            # Simpan state terkini
            self._endpoint_states[ep_id] = {
                "status": current_status,
                "last_alert_at": now if (should_alert_down or should_alert_recovery) else last_alert_at,
                "last_error": res.get("error_detail", ""),
            }

            # Kirim Telegram Alert jika diperlukan
            if should_alert_down:
                alert_text = self.format_alarm_message(res)
                logger.error("ALARM DETEKSI DINI: Endpoint %s DOWN (%s)", res["name"], res["error_detail"])
                try:
                    await notify_admins(bot_sender, alert_text, kind="alarm", butuh_tindakan=True)
                except Exception as exc:
                    logger.warning("Gagal mengirim alarm ke admin: %s", exc)

            elif should_alert_recovery:
                recovery_text = self.format_recovery_message(res)
                logger.info("RECOVERED: Endpoint %s kembali normal", res["name"])
                try:
                    await notify_admins(bot_sender, recovery_text, kind="alarm")
                except Exception as exc:
                    logger.warning("Gagal mengirim notif recovery ke admin: %s", exc)

        return results

    def format_alarm_message(self, res: Dict[str, Any]) -> str:
        """Membuat teks notifikasi alarm deteksi dini untuk dikirim ke admin."""
        env_var = res.get("env_var", "RPC_URL")
        fallbacks = res.get("fallback_urls", [])
        fallback_text = ""
        if fallbacks:
            fallback_text = "\n\n💡 <b>Rekomendasi URL Alternatif:</b>\n" + "\n".join([f"• <code>{fb}</code>" for fb in fallbacks[:2]])

        return (
            f"🚨 <b>ALARM DETEKSI DINI: URL/API KOIN BERMASALAH!</b>\n\n"
            f"⚠️ <b>Target:</b> {res['name']} ({res['symbol']})\n"
            f"🌐 <b>URL Saat Ini:</b> <code>{res['url']}</code>\n"
            f"🔴 <b>Status:</b> <b>DOWN</b> ({res.get('error_code', 'ERROR')})\n"
            f"⏱ <b>Latensi:</b> {res['latency_ms']} ms\n"
            f"📝 <b>Detail Masalah:</b>\n<i>{res['error_detail']}</i>\n\n"
            f"💥 <b>Dampak Operasional:</b>\n{res['impact']}\n\n"
            f"🛠 <b>Langkah Perbaikan untuk Admin:</b>\n"
            f"1. Periksa apakah URL endpoint koin di atas telah berganti rute atau domainnya mati.\n"
            f"2. Buka Railway Variables / file <code>.env</code>.\n"
            f"3. Perbarui variabel: <code>{env_var}=&lt;url_baru&gt;</code>\n"
            f"4. Bot akan otomatis menggunakan URL baru tanpa perlu mengubah kode sumber."
            f"{fallback_text}"
        )

    def format_recovery_message(self, res: Dict[str, Any]) -> str:
        """Membuat teks notifikasi saat URL kembali pulih."""
        return (
            f"✅ <b>PEMULIHAN: API / URL KOIN KEMBALI NORMAL</b>\n\n"
            f"Target: <b>{res['name']}</b> ({res['symbol']})\n"
            f"URL: <code>{res['url']}</code>\n"
            f"Status: 🟢 <b>ONLINE (OK)</b>\n"
            f"Latensi: <b>{res['latency_ms']} ms</b>\n"
            f"Info Terakhir: {res.get('block_info', 'OK')}\n\n"
            f"Layanan jual beli & sinkronisasi untuk koin ini telah berjalan normal kembali. 🙏"
        )

    def format_admin_dashboard(self, results: List[Dict[str, Any]]) -> str:
        """Membuat teks dashboard diagnostik lengkap untuk menu Admin (/checkapi)."""
        now_str = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M:%S UTC")
        total = len(results)
        healthy = sum(1 for r in results if r["status"] == "OK")
        degraded = sum(1 for r in results if r["status"] == "DEGRADED")
        down = sum(1 for r in results if r["status"] == "DOWN")

        overall_status = "🟢 <b>SEMUA URL NORMAL</b>"
        if down > 0:
            overall_status = f"🔴 <b>PERINGATAN: {down} URL MATI / BERMASALAH!</b>"
        elif degraded > 0:
            overall_status = f"🟡 <b>PERINGATAN: {degraded} URL LAMBAT (DEGRADED)</b>"

        price_rows = []
        evm_rows = []
        nonevm_rows = []

        for r in results:
            cat = r["category"]
            if r["status"] == "OK":
                icon = "🟢"
                extra = f"({r['latency_ms']}ms, {r.get('block_info', 'OK')})"
            elif r["status"] == "DEGRADED":
                icon = "🟡"
                extra = f"(LAMBAT: {r['latency_ms']}ms)"
            else:
                icon = "🔴"
                extra = f"({r.get('error_code', 'DOWN')}: {r.get('error_detail', 'Error')[:45]}...)"

            line = f"{icon} <b>{r['name']}</b>: <code>{extra}</code>"

            if cat == "PRICE_API":
                price_rows.append(line)
            elif cat == "EVM_RPC":
                evm_rows.append(line)
            else:
                nonevm_rows.append(line)

        text = (
            f"📡 <b>DIAGNOSTIK KESEHATAN URL & API KOIN</b>\n"
            f"🕒 <i>Waktu Scan: {now_str}</i>\n\n"
            f"🚦 <b>Kondisi:</b> {overall_status}\n"
            f"📊 <b>Ringkasan:</b> 🟢 {healthy} Normal · 🟡 {degraded} Lambat · 🔴 {down} Mati (Total: {total})\n\n"
            f"💰 <b>API Kurs Harga Pasar:</b>\n" + "\n".join(price_rows) + "\n\n"
            f"⛓ <b>Jaringan Blockchain EVM:</b>\n" + "\n".join(evm_rows) + "\n\n"
            f"⚡ <b>Jaringan Blockchain Non-EVM:</b>\n" + "\n".join(nonevm_rows)
        )

        if down > 0:
            text += (
                f"\n\n⚠️ <i><b>Catatan:</b> Jika ada URL berstatus 🔴, periksa variabel env terkait "
                f"di Railway / .env dan ganti dengan RPC alternatif agar transaksi tidak terhenti.</i>"
            )

        return text


# Singleton instance
coin_api_monitor = CoinAPIMonitor()
