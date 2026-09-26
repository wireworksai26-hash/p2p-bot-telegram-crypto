"""Upload 4 logo baru ke pak custom emoji hsncoinlogos_by_Hsnpro_bot, ambil ID baru."""
import json
import os

import httpx

tok = os.environ["PROD_BOT_TOKEN"]
base = f"https://api.telegram.org/bot{tok}"
SET = "hsncoinlogos_by_Hsnpro_bot"
MARKER = "scripts/_pack_added.json"

me = httpx.post(f"{base}/getMe", timeout=30).json()
assert me.get("ok"), me
bot_id = me["result"]["id"]
print(f"bot: @{me['result']['username']} id={bot_id}")


def get_set():
    r = httpx.get(f"{base}/getStickerSet", params={"name": SET}, timeout=30)
    j = r.json()
    assert j.get("ok"), j
    return j["result"]


s = get_set()
existing = {x.get("file_unique_id") for x in s["stickers"]}
print("count before:", len(s["stickers"]))

added = json.load(open(MARKER, encoding="utf-8")) if os.path.exists(MARKER) else {}

ITEMS = [
    ("BASE", "\U0001f535"),        # blue circle
    ("OPTIMISM", "\U0001f534"),     # red circle
    ("ROBINHOOD", "\U0001fab6"),    # feather
    ("MATIC", "\U0001f7e3"),        # purple circle
]

for name, emoji in ITEMS:
    if name in added:
        print(f"{name}: sudah pernah diupload, lewati")
        continue
    path = f"scripts/_new_icons/{name}.png"
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        payload = {
            "user_id": str(bot_id),
            "name": SET,
            "sticker": json.dumps(
                {"sticker": "attach://file0", "format": "static", "emoji_list": [emoji]}
            ),
        }
        r = httpx.post(
            f"{base}/addStickerToSet",
            data=payload,
            files={"file0": (f"{name}.png", fh, "image/png")},
            timeout=60,
        )
    j = r.json()
    print(f"{name}: ok={j.get('ok')} {j.get('description', '')} size={size}")
    if not j.get("ok"):
        raise SystemExit(f"gagal upload {name}")
    added[name] = True
    json.dump(added, open(MARKER, "w", encoding="utf-8"))

# Ambil ID custom emoji stiker baru
s = get_set()
new = [x for x in s["stickers"] if x.get("file_unique_id") not in existing]
print("count after:", len(s["stickers"]), "new:", len(new))
result = {}
for x in new:
    result[x["custom_emoji_id"]] = x.get("emoji")
    print(f"NEW ID {x['custom_emoji_id']} emoji={x.get('emoji','').encode('ascii','replace').decode()}")
json.dump(result, open("scripts/_new_ids.json", "w", encoding="utf-8"), indent=1)
print("saved scripts/_new_ids.json")
