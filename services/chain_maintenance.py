"""
services/chain_maintenance.py — Mode maintenance per jaringan / koin
=====================================================================
Admin menandai sebuah jaringan (mis. SOLANA saat RPC bermasalah, ETH saat gas melonjak)
atau koin sebagai [Maintenance]. Efeknya hanya menutup order BARU untuk jaringan/koin itu;
order yang sudah berjalan (deposit terdeteksi, payout, konfirmasi admin) tetap diproses
supaya dana user tidak tertahan.

Gagal baca DB = dianggap tidak maintenance (bot tetap melayani), karena penanda ini
hanya pelindung tambahan.
"""

import logging
from html import escape
from typing import Optional

from database.models import ChainMaintenance

logger = logging.getLogger(__name__)

SCOPE_NETWORK = "NETWORK"
SCOPE_COIN = "COIN"
SCOPES = (SCOPE_NETWORK, SCOPE_COIN)


def _norm(code: str) -> str:
    return (code or "").strip().upper()


def load_flags() -> dict:
    """{(scope, kode): catatan} untuk semua penanda aktif. Kosong bila DB bermasalah."""
    try:
        from database.connection import SessionLocal
        db = SessionLocal()
        try:
            return {(r.scope, r.code): (r.note or "") for r in db.query(ChainMaintenance).all()}
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Gagal membaca status maintenance (dianggap normal): %s", exc)
        return {}


def blocked_reason(symbol: Optional[str] = None, network: Optional[str] = None,
                   flags: Optional[dict] = None) -> Optional[tuple]:
    """(label, catatan) bila jaringan atau koin sedang maintenance, selain itu None."""
    flags = load_flags() if flags is None else flags
    net, sym = _norm(network), _norm(symbol)
    if net and (SCOPE_NETWORK, net) in flags:
        return f"Jaringan {net}", flags[(SCOPE_NETWORK, net)]
    if sym and (SCOPE_COIN, sym) in flags:
        return f"Koin {sym}", flags[(SCOPE_COIN, sym)]
    return None


def maintenance_text(reason: tuple) -> str:
    """Pesan untuk user saat memilih jaringan/koin yang sedang maintenance."""
    label, note = reason
    note_line = f"\n<i>Catatan: {escape(note)}</i>\n" if note else "\n"
    return (
        f"🛠 <b>[Maintenance] {escape(label)}</b>\n\n"
        f"Order baru untuk {escape(label)} sedang ditutup sementara karena jaringan/layanan "
        f"sedang terhambat.{note_line}"
        "Order yang sudah berjalan tetap diproses seperti biasa. "
        "Silakan pilih jaringan/koin lain, atau coba lagi beberapa saat lagi."
    )


def set_flag(scope: str, code: str, on: bool, note: str = "", admin_id: Optional[int] = None) -> bool:
    """Nyalakan/matikan maintenance. True bila status benar-benar berubah."""
    scope, code = _norm(scope), _norm(code)
    if scope not in SCOPES or not code or len(code) > 30:
        raise ValueError("scope harus NETWORK atau COIN dan kode tidak boleh kosong")
    from database.connection import SessionLocal
    db = SessionLocal()
    try:
        row = db.query(ChainMaintenance).filter(
            ChainMaintenance.scope == scope, ChainMaintenance.code == code).first()
        if on:
            note = (note or "").strip()[:200]
            if row:
                changed = (row.note or "") != note
                row.note, row.set_by = note or None, admin_id
            else:
                db.add(ChainMaintenance(scope=scope, code=code, note=note or None, set_by=admin_id))
                changed = True
        else:
            changed = row is not None
            if row:
                db.delete(row)
        db.commit()
        return changed
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def known_networks() -> list:
    """Semua jaringan yang dilayani bot (untuk panel admin)."""
    from bot.keyboards.crypto_select import BUY_NETWORKS_BY_SYMBOL
    seen = []
    for nets in BUY_NETWORKS_BY_SYMBOL.values():
        for n in nets:
            if n not in seen:
                seen.append(n)
    return seen


def known_coins() -> list:
    from bot.keyboards.crypto_select import BUY_NETWORKS_BY_SYMBOL
    return list(BUY_NETWORKS_BY_SYMBOL)
