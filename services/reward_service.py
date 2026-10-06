"""services/reward_service.py — Kirim Reward ke user pilihan admin.

Admin mengetik daftar penerima sekaligus (ID atau @username, nominal beda per orang,
pesan boleh custom). Dana diambil dari Kas Bot (treasury); bila kurang, admin diminta
mengisi Kas Bot dulu via QRIS.

Keamanan uang:
- Batch dibuat DRAFT, dieksekusi lewat klaim atomik DRAFT -> RUNNING: tombol "Kirim"
  yang ditekan dua kali (atau dari salinan pesan lain) tidak membayar dua kali.
- Kas Bot dipotong ketat (tidak boleh minus) SEBELUM saldo user dikredit; penerima yang
  gagal dikredit dikembalikan ke Kas Bot.
- Penerima divalidasi ulang saat eksekusi (masih ada, tidak diblokir).
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from html import escape as _esc
from typing import Optional

from sqlalchemy import update

from bot.utils.formatter import format_idr
from database import crud
from database.models import AuditLog, RewardBatch, User

logger = logging.getLogger(__name__)

MAX_RECIPIENTS = 30
MIN_REWARD_IDR = 1_000
MAX_REWARD_IDR = 10_000_000          # sama dengan batas "Kirim Saldo User"
MAX_CUSTOM_MESSAGE_CHARS = 700

DEFAULT_REWARD_MESSAGE = (
    "🎁 <b>Ada hadiah untukmu!</b>\n"
    "Admin HSN Store mengirimkan reward spesial. Semoga bermanfaat 🙏"
)

_LINE_RE = re.compile(r"^\s*(@?[A-Za-z0-9_]{3,32}|\d{1,15})[\s:=,;]+(.+?)\s*$")


# ─────────────────────────────────────────────────────────────────────────────
#  Parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_amount(text: str) -> Optional[int]:
    """'50000' '50.000' '50k' '50rb' 'Rp 1,5jt' -> int rupiah; None bila tidak valid."""
    t = (text or "").strip().lower().replace("rp", "").replace(" ", "")
    if not t:
        return None
    mult = 1
    for suffix, factor in (("juta", 1_000_000), ("jt", 1_000_000), ("ribu", 1_000), ("rb", 1_000), ("k", 1_000)):
        if t.endswith(suffix):
            t, mult = t[: -len(suffix)], factor
            break
    if mult > 1:
        # Dengan akhiran, titik/koma = desimal: 1,5jt / 2.5k
        t = t.replace(",", ".")
        if not re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,3})?", t):   # batas panjang: cegah OverflowError/inf
            return None
        return int(round(float(t) * mult))
    # Tanpa akhiran: "75.000,00" / "75,000.00" = ribuan + sen (buang sen, JANGAN jadi 7,5 juta).
    cents = re.fullmatch(r"(\d{1,3}(?:\.\d{3})*),\d{1,2}|(\d{1,3}(?:,\d{3})*)\.\d{1,2}", t)
    if cents:
        t = cents.group(1) or cents.group(2)
    # Selebihnya titik/koma = pemisah ribuan: 50.000 / 50,000
    digits = re.sub(r"[.,]", "", t)
    # [0-9] bukan isdigit(): "²" lolos isdigit() lalu int() melempar error; batasi panjang.
    return int(digits) if re.fullmatch(r"[0-9]{1,15}", digits) else None


def parse_reward_lines(text: str) -> tuple[list[dict], list[dict]]:
    """Baris 'ID|@username  nominal  [| pesan]' -> (rows, errors).

    rows:   [{line, target, amount, message|None}]
    errors: [{line, raw, reason}]
    """
    rows, errors = [], []
    for n, raw in enumerate((text or "").splitlines(), start=1):
        if not raw.strip():
            continue
        left, _, message = raw.partition("|")
        match = _LINE_RE.match(left)
        if not match:
            errors.append({"line": n, "raw": raw.strip(), "reason": "Format salah (contoh: @budi 50000)"})
            continue
        target, amount_text = match.group(1), match.group(2)
        amount = parse_amount(amount_text)
        if amount is None:
            errors.append({"line": n, "raw": raw.strip(), "reason": f"Nominal “{amount_text}” tidak terbaca"})
            continue
        if amount < MIN_REWARD_IDR or amount > MAX_REWARD_IDR:
            errors.append({"line": n, "raw": raw.strip(),
                           "reason": f"Nominal harus {format_idr(MIN_REWARD_IDR)} – {format_idr(MAX_REWARD_IDR)}"})
            continue
        message = message.strip()
        if len(message) > MAX_CUSTOM_MESSAGE_CHARS:
            errors.append({"line": n, "raw": raw.strip(), "reason": f"Pesan terlalu panjang (maks {MAX_CUSTOM_MESSAGE_CHARS} karakter)"})
            continue
        rows.append({"line": n, "target": target, "amount": amount, "message": message or None})
    return rows, errors


def resolve_recipients(db, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Cocokkan target ke user bot. Mengembalikan (items, skipped)."""
    items, skipped, seen = [], [], set()
    for row in rows:
        if len(items) >= MAX_RECIPIENTS:
            skipped.append({**row, "reason": f"Melebihi batas {MAX_RECIPIENTS} penerima per batch"})
            continue
        user = crud.get_user_by_identifier(db, row["target"])
        if not user:
            skipped.append({**row, "reason": "belum terdaftar di bot — minta orangnya /start dulu"})
            continue
        if user.is_banned:
            skipped.append({**row, "reason": "akun diblokir"})
            continue
        if user.telegram_id in seen:
            skipped.append({**row, "reason": "duplikat (user yang sama sudah ada di daftar)"})
            continue
        seen.add(user.telegram_id)
        items.append({
            "telegram_id": user.telegram_id,
            "label": f"@{user.username}" if user.username else (user.full_name or f"ID {user.telegram_id}"),
            "full_name": user.full_name or "",
            "amount": row["amount"],
            "message": row["message"],
        })
    return items, skipped


# ─────────────────────────────────────────────────────────────────────────────
#  Batch
# ─────────────────────────────────────────────────────────────────────────────

def create_batch(db, admin_id: int, items: list[dict]) -> RewardBatch:
    batch = RewardBatch(
        created_by=admin_id, status="DRAFT",
        items_json=json.dumps(items, ensure_ascii=False),
        total_amount=sum(i["amount"] for i in items), recipient_count=len(items),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def get_batch_items(batch: RewardBatch) -> list[dict]:
    return json.loads(batch.items_json or "[]")


def set_default_message(db, batch_id: int, admin_id: int, message: Optional[str]) -> bool:
    batch = db.query(RewardBatch).filter(RewardBatch.id == batch_id, RewardBatch.created_by == admin_id,
                                         RewardBatch.status == "DRAFT").first()
    if not batch:
        return False
    batch.default_message = (message or "").strip()[:1500] or None
    db.commit()
    return True


def cancel_batch(db, batch_id: int, admin_id: int) -> bool:
    res = db.execute(update(RewardBatch).where(
        RewardBatch.id == batch_id, RewardBatch.created_by == admin_id, RewardBatch.status == "DRAFT",
    ).values(status="CANCELLED"))
    db.commit()
    return res.rowcount == 1


def render_message(body: Optional[str], *, name: str, amount: int, new_balance: float) -> str:
    """Pesan ke penerima: isi custom (di-escape; boleh santai) atau default + info saldo."""
    if body:
        text = (_esc(body)
                .replace("{nama}", _esc(name)).replace("{nominal}", format_idr(amount))
                .replace("{saldo}", format_idr(int(new_balance))))
    else:
        text = DEFAULT_REWARD_MESSAGE
    return (f"{text}\n\n💰 <b>+{format_idr(amount)}</b> sudah masuk ke Saldo Bot kamu.\n"
            f"💳 Saldo sekarang: <b>{format_idr(int(new_balance))}</b>")


async def execute_batch(db, bot, batch_id: int, admin_id: int) -> dict:
    """Bayar semua penerima batch. Idempoten: hanya satu pemanggil yang lolos klaim."""
    claim = db.execute(update(RewardBatch).where(
        RewardBatch.id == batch_id, RewardBatch.created_by == admin_id, RewardBatch.status == "DRAFT",
    ).values(status="RUNNING"))
    db.commit()
    if claim.rowcount != 1:
        return {"ok": False, "error_code": "claimed",
                "error": "Batch ini sudah diproses, dibatalkan, atau bukan milik Anda."}

    batch = db.query(RewardBatch).filter(RewardBatch.id == batch_id).one()

    def _revert(code, message, **extra):
        batch.status = "DRAFT"
        db.commit()
        return {"ok": False, "error_code": code, "error": message, **extra}

    # Validasi ulang penerima saat eksekusi.
    valid, dropped = [], []
    for item in get_batch_items(batch):
        user = db.query(User).filter(User.telegram_id == item["telegram_id"]).first()
        if not user or user.is_banned:
            dropped.append({**item, "reason": "akun tidak ditemukan / diblokir"})
        else:
            valid.append(item)
    total = sum(i["amount"] for i in valid)
    if not valid:
        return _revert("empty", "Tidak ada penerima yang valid.")

    treasury_before = crud.get_bot_treasury_balance(db)
    treasury_after = crud.try_deduct_bot_treasury(
        db, total, admin_id=admin_id, note=f"Reward batch #{batch.id} ({len(valid)} penerima)")
    if treasury_after is None:
        return _revert("treasury", "Kas Bot tidak cukup.", needed=total,
                       balance=treasury_before, shortfall=total - treasury_before)

    results, refund = [], 0
    for item in valid:
        try:
            new_balance = crud.credit_user_balance(db, item["telegram_id"], float(item["amount"]))
        except Exception as exc:
            db.rollback()
            logger.error("Reward batch %s: gagal kredit %s: %s", batch.id, item["telegram_id"], exc)
            refund += item["amount"]
            results.append({**item, "status": "FAILED", "error": str(exc)[:120]})
            continue
        # Saldo SUDAH masuk (commit di credit_user_balance): audit gagal tidak boleh membuat
        # penerima ditandai gagal lalu direfund ke Kas Bot (uang ganda).
        results.append({**item, "status": "OK", "new_balance": new_balance})
        try:
            db.add(AuditLog(
                telegram_id=item["telegram_id"], action="ADMIN_REWARD",
                details=f"Reward {format_idr(item['amount'])} dari admin {admin_id} (batch #{batch.id}, Kas Bot)"))
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error("Reward batch %s: audit %s gagal (saldo tetap masuk): %s", batch.id, item["telegram_id"], exc)
    if refund:
        treasury_after = crud.topup_bot_treasury(
            db, refund, admin_id=admin_id, note=f"Refund reward batch #{batch.id} (gagal kredit)")

    paid_total = total - refund
    batch.status = "COMPLETED"
    batch.executed_at = datetime.utcnow()
    batch.treasury_before, batch.treasury_after = treasury_before, treasury_after
    batch.total_amount = paid_total
    batch.result_json = json.dumps(results + [{**d, "status": "DROPPED"} for d in dropped], ensure_ascii=False)
    db.commit()

    # Notifikasi setelah uang aman (gagal kirim notif TIDAK membatalkan reward).
    notif_ok, notif_fail = [], []
    for r in results:
        if r["status"] != "OK":
            continue
        body = r.get("message") or batch.default_message
        text = render_message(body, name=r.get("full_name") or r["label"], amount=r["amount"],
                              new_balance=r["new_balance"])
        sent = False
        try:
            await bot.send_message(chat_id=r["telegram_id"], text=text, parse_mode="HTML")
            sent = True
        except Exception as exc:
            logger.info("Notif reward ke %s gagal (bot diblokir / belum chat): %s", r["telegram_id"], exc)
        (notif_ok if sent else notif_fail).append(r["label"])
        await asyncio.sleep(0.05)

    return {
        "ok": True, "batch_id": batch.id, "paid_count": len(notif_ok) + len(notif_fail),
        "paid_total": paid_total, "failed": [r for r in results if r["status"] == "FAILED"],
        "dropped": dropped, "notif_ok": notif_ok, "notif_fail": notif_fail,
        "treasury_before": treasury_before, "treasury_after": treasury_after,
    }
