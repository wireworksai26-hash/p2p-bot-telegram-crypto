"""Unduh logo baru (BASE, OPTIMISM, ROBINHOOD, MATIC) dari sumber publik, resize 100x100 PNG."""
import io
import os
import sys

import httpx
from PIL import Image

OUT = "scripts/_new_icons"
os.makedirs(OUT, exist_ok=True)

CANDIDATES = {
    "BASE": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/base/info/logo.png",
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/base/info/logo.png",
        "https://cryptologos.cc/logos/base-base-logo.png",
    ],
    "OPTIMISM": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/optimism/info/logo.png",
        "https://cryptologos.cc/logos/optimism-optimism-op-logo.png",
    ],
    "ROBINHOOD": [
        "https://brandlogos.sgp1.digitaloceanspaces.com/png/cbi/robinhood-400.png",
        "https://cryptologos.cc/assets/robinhood.png",
        "https://cryptologos.zenobank.io/logo/robinhood",
    ],
    "MATIC": [
        "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/polygon/info/logo.png",
        "https://cryptologos.cc/logos/polygon-matic-logo.png",
    ],
}

TIMEOUT = httpx.Timeout(20.0)

for sym, urls in CANDIDATES.items():
    ok = False
    for url in urls:
        try:
            r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                print(f"  {sym}: HTTP {r.status_code} {url[:70]}")
                continue
            img = Image.open(io.BytesIO(r.content))
            img = img.convert("RGBA")
            # Center-crop ke persegi lalu resize 100x100
            w, h = img.size
            side = min(w, h)
            img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
            img = img.resize((100, 100), Image.LANCZOS)
            path = os.path.join(OUT, f"{sym}.png")
            img.save(path, "PNG")
            print(f"  {sym}: OK {img.size} <- {url[:70]}")
            ok = True
            break
        except Exception as e:
            print(f"  {sym}: ERR {type(e).__name__} {e} {url[:70]}")
    if not ok:
        print(f"  {sym}: GAGAL semua sumber")

print("done:", sorted(os.listdir(OUT)) if os.path.isdir(OUT) else "none")
