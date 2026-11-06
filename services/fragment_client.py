"""Klien web Fragment.com untuk membeli Telegram Stars / Premium (tidak resmi).

Fragment tidak punya API publik resmi; protokol di sini mengikuti endpoint web yang dipakai halaman Fragment
sendiri (POST https://fragment.com/api?hash=...), sama dengan yang dipakai pustaka pihak ketiga `afragment`.
Karena itu: (1) bisa berhenti bekerja kapan pun Fragment mengubah situsnya, (2) butuh sesi akun yang sudah
login (FRAGMENT_COOKIE), (3) klien ini HANYA menyiapkan transaksi (alamat tujuan, nominal, payload);
penandatanganan dan pembayaran TON dilakukan terpisah oleh wallet khusus (fragment_wallet.py).

Hash API ditanam di halaman Fragment (ajInit({"apiUrl":"\\/api?hash=..."})) dan bisa berubah. Karena itu klien
membaca hash terbaru sendiri dari https://fragment.com/stars memakai cookie; FRAGMENT_HASH hanya nilai awal/cadangan.
Hash yang ditolak ("Bad request"/ditolak sesi) diambil ulang dan dicoba sekali lagi. Itu aman diulang karena
semua panggilan di sini hanya menyiapkan transaksi, tidak ada uang yang bergerak.

Cookie/hash adalah rahasia akun: tidak pernah dicatat di log dan tidak pernah muncul di pesan error.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://fragment.com/api"
PAGE_URL = "https://fragment.com/stars"
USER_AGENT = "Mozilla/5.0 (compatible; TokoKoinBot/1.0)"
_API_URL_RE = re.compile(r'"apiUrl"\s*:\s*"([^"]+)"')
_HASH_RE = re.compile(r"[?&]hash=([0-9A-Za-z_-]+)")
# Hash terbaru yang dibaca dari halaman (kunci: cookie), hanya di memori; dipakai bersama oleh semua klien.
_LEARNED_HASH: dict = {}
TON_MAINNET_CHAIN = "-239"
DEFAULT_DEVICE = {
    "platform": "android", "appName": "Tonkeeper", "appVersion": "5.0.18", "maxProtocolVersion": 2,
    "features": ["SendTransaction", {"name": "SendTransaction", "maxMessages": 4}],
}
PRICE_CHANGED_RETRIES = 2


class FragmentError(RuntimeError):
    """Kegagalan umum Fragment (pesan sudah dibersihkan dari data rahasia)."""


class FragmentAuthError(FragmentError):
    """Sesi akun Fragment tidak berlaku (hash/cookie kedaluwarsa)."""


class FragmentBadRequest(FragmentError):
    """Fragment menolak permintaan ("Bad request") atau membalas bukan JSON: biasanya hash basi."""


class FragmentInvalidRecipient(FragmentError):
    """Username tujuan tidak ditemukan / tidak bisa menerima."""


class FragmentPriceChanged(FragmentError):
    """Harga berubah di tengah permintaan; coba ulang."""


@dataclass
class PreparedPurchase:
    """Transaksi TON yang harus dibayar untuk satu pembelian."""
    recipient_name: str = ""
    req_id: str = ""
    messages: List[dict] = field(default_factory=list)  # [{"address", "amount_nano", "payload_b64"}]

    @property
    def total_nano(self) -> int:
        return sum(int(m["amount_nano"]) for m in self.messages)


def parse_api_hash(page_text: str) -> Optional[str]:
    """Hash API dari source halaman Fragment: ajInit({... "apiUrl":"\\/api?hash=XXXX" ...}); None bila tidak ada."""
    match = _API_URL_RE.search(page_text or "")
    if not match:
        return None
    found = _HASH_RE.search(match.group(1).replace("\\/", "/"))
    return found.group(1) if found else None


def _clean(text, *secrets) -> str:
    """Potong dan buang data rahasia dari teks yang boleh tampil di log/pesan."""
    out = str(text)[:200]
    for secret in secrets:
        if secret and len(secret) > 6:
            out = out.replace(secret, "***")
    return out


class FragmentClient:
    def __init__(self, fragment_hash: str, fragment_cookie: str, timeout: float = 20.0, transport=None):
        self._cookie = fragment_cookie
        self._hash = _LEARNED_HASH.get(fragment_cookie) or fragment_hash
        self._timeout = timeout
        self._transport = transport  # hanya untuk tes

    def configured(self) -> bool:
        """Cukup cookie: hash bisa dibaca sendiri dari halaman Fragment."""
        return bool(self._cookie)

    async def _refresh_hash(self) -> Optional[str]:
        """Baca hash terbaru dari halaman Fragment (memakai cookie). None bila gagal; tidak pernah melempar."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                         follow_redirects=True) as client:
                response = await client.get(PAGE_URL, headers={
                    "Cookie": self._cookie, "User-Agent": USER_AGENT, "Accept": "text/html"})
            if response.status_code != 200:
                return None
            fresh = parse_api_hash(response.text)
        except Exception:
            logger.warning("Gagal membaca hash dari halaman Fragment", exc_info=False)
            return None
        if fresh:
            self._hash = fresh
            _LEARNED_HASH[self._cookie] = fresh
        return fresh

    async def _call(self, data: dict) -> dict:
        if not self._hash:
            await self._refresh_hash()
        if not self._hash:
            raise FragmentAuthError("Hash Fragment tidak ditemukan: pastikan FRAGMENT_COOKIE masih berlaku "
                                    "(login ulang) atau isi FRAGMENT_HASH.")
        try:
            return await self._post(data)
        except (FragmentAuthError, FragmentBadRequest):
            # Hash basi? Ambil yang terbaru dari halaman; coba lagi hanya bila hash-nya memang berubah.
            old = self._hash
            fresh = await self._refresh_hash()
            if not fresh or fresh == old:
                raise
            return await self._post(data)

    async def _post(self, data: dict) -> dict:
        headers = {
            "Cookie": self._cookie,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://fragment.com",
            "Referer": "https://fragment.com/",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(f"{BASE_URL}?hash={self._hash}", data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise FragmentError(f"Fragment tidak terjangkau ({type(exc).__name__})") from None
        if response.status_code in (401, 403):
            raise FragmentAuthError("Sesi Fragment ditolak (login ulang dan perbarui FRAGMENT_COOKIE).")
        try:
            result = response.json()
        except ValueError:
            raise FragmentBadRequest(f"Balasan Fragment bukan JSON (HTTP {response.status_code})") from None
        if isinstance(result, dict) and result.get("error"):
            message = _clean(result["error"], self._hash, self._cookie)
            low = message.lower()
            if "price was changed" in low:
                raise FragmentPriceChanged(message)
            if "auth" in low or "unauthorized" in low or "log in" in low:
                raise FragmentAuthError(message)
            if "bad request" in low:
                raise FragmentBadRequest(message)
            if any(x in low for x in ("not found", "invalid", "no telegram users", "user not found")):
                raise FragmentInvalidRecipient(message)
            raise FragmentError(message)
        if not isinstance(result, dict):
            raise FragmentError("Balasan Fragment tidak dikenali.")
        return result

    @staticmethod
    def _messages(link: dict) -> List[dict]:
        raw = ((link.get("transaction") or {}).get("messages")) or []
        out = []
        for m in raw:
            try:
                out.append({"address": str(m["address"]), "amount_nano": int(m["amount"]),
                            "payload_b64": str(m.get("payload") or "")})
            except (KeyError, TypeError, ValueError):
                raise FragmentError("Format transaksi Fragment tidak dikenali.") from None
        if not out or any(m["amount_nano"] <= 0 for m in out):
            raise FragmentError("Fragment tidak mengembalikan transaksi pembayaran yang valid.")
        return out

    async def _prepare(self, search, init, link, username, extra_init, wallet_address, public_key) -> PreparedPurchase:
        found = (await self._call(search)).get("found") or {}
        recipient = found.get("recipient")
        if not recipient:
            raise FragmentInvalidRecipient("Username tujuan tidak ditemukan di Fragment.")
        account = {"address": wallet_address, "chain": TON_MAINNET_CHAIN}
        if public_key:
            account["publicKey"] = public_key
        last = None
        for _ in range(PRICE_CHANGED_RETRIES + 1):
            try:
                started = await self._call({**init, "recipient": recipient, **extra_init, "payment_method": "ton"})
                req_id = started.get("req_id")
                if not req_id:
                    raise FragmentError("Fragment tidak memberi req_id.")
                result = await self._call({
                    **link, "id": req_id, "account": json.dumps(account), "device": json.dumps(DEFAULT_DEVICE),
                    "transaction": "1", "show_sender": "0"})
                return PreparedPurchase(recipient_name=str(found.get("name") or ""), req_id=str(req_id),
                                        messages=self._messages(result))
            except FragmentPriceChanged as exc:
                last = exc
        raise last or FragmentError("Harga Fragment terus berubah.")

    async def prepare_stars(self, username: str, quantity: int, wallet_address: str,
                            public_key: Optional[str] = None) -> PreparedPurchase:
        if quantity < 50:
            raise FragmentError("Pembelian Stars minimal 50.")
        return await self._prepare(
            {"method": "searchStarsRecipient", "query": username, "quantity": quantity},
            {"method": "initBuyStarsRequest"}, {"method": "getBuyStarsLink"},
            username, {"quantity": quantity}, wallet_address, public_key)

    async def prepare_premium(self, username: str, months: int, wallet_address: str,
                              public_key: Optional[str] = None) -> PreparedPurchase:
        if months not in (3, 6, 12):
            raise FragmentError("Premium hanya 3, 6, atau 12 bulan.")
        return await self._prepare(
            {"method": "searchPremiumGiftRecipient", "query": username},
            {"method": "initGiftPremiumRequest"}, {"method": "getGiftPremiumLink"},
            username, {"months": months}, wallet_address, public_key)

    async def check_session(self, username: str) -> str:
        """Uji sesi tanpa membeli apa pun: cari penerima Stars. Mengembalikan nama penerima."""
        found = (await self._call({"method": "searchStarsRecipient", "query": username, "quantity": 50})).get("found") or {}
        if not found.get("recipient"):
            raise FragmentInvalidRecipient("Username uji tidak ditemukan.")
        return str(found.get("name") or username)
