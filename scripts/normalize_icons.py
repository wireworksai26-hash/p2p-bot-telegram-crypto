"""Normalisasi 4 ikon baru: trim ke bbox konten, pad jadi persegi, resize 100x100."""
import os

from PIL import Image

OUT = "scripts/_new_icons"
NAMES = ["BASE", "OPTIMISM", "ROBINHOOD", "MATIC"]
PAD_RATIO = 0.06

for name in NAMES:
    path = os.path.join(OUT, f"{name}.png")
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    px = img.load()

    # putih -> transparan dulu (kalau ada latar putih opaque)
    opaque = sum(
        1 for y in range(0, h, 2) for x in range(0, w, 2)
        if px[x, y][3] > 10 and px[x, y][0] > 235 and px[x, y][1] > 235 and px[x, y][2] > 235
    )
    total = len(range(0, h, 2)) * len(range(0, w, 2))
    if opaque > total * 0.2:
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                if a > 0 and r > 235 and g > 235 and b > 235:
                    px[x, y] = (r, g, b, 0)

    # bbox konten
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if px[x, y][3] > 10:
                minx = min(minx, x); miny = min(miny, y)
                maxx = max(maxx, x); maxy = max(maxy, y)
    if maxx < 0:
        print(f"{name}: KOSONG!")
        continue
    cw, ch = maxx - minx + 1, maxy - miny + 1
    side = int(max(cw, ch) * (1 + PAD_RATIO * 2))
    cx, cy = (minx + maxx) // 2, (miny + maxy) // 2
    left, top = cx - side // 2, cy - side // 2

    # kanvas besar transparan -> tempel utuh dengan offset side -> crop (selalu dalam batas)
    big = Image.new("RGBA", (w + 2 * side, h + 2 * side), (0, 0, 0, 0))
    big.paste(img, (side, side))
    box = (left + side, top + side, left + side + side, top + side + side)
    final = big.crop(box).resize((100, 100), Image.LANCZOS)
    final.save(path)
    print(f"{name}: bbox=({minx},{miny})-({maxx},{maxy}) content={cw}x{ch} canvas={side} -> 100x100")
