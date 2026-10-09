"""
bot/handlers/admin_maintenance.py — Panel & perintah admin: maintenance per jaringan/koin
==========================================================================================
Panel : Dashboard Admin ➔ 🛠 Maintenance Chain/Koin (tombol toggle per jaringan dan koin)
Perintah: /maintenance                      -> lihat status
          /maintenance on NETWORK SOLANA [catatan]
          /maintenance off COIN USDT
Efek: order BARU untuk jaringan/koin itu ditutup; order yang sudah berjalan tetap diproses.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from services import chain_maintenance as cm

logger = logging.getLogger(__name__)

_SCOPE_KEY = {"n": cm.SCOPE_NETWORK, "c": cm.SCOPE_COIN}


def build_maintenance_view() -> tuple:
    """(teks, keyboard) panel maintenance."""
    flags = cm.load_flags()
    active = sorted(flags)
    lines = ["🛠 <b>MAINTENANCE JARINGAN &amp; KOIN</b>\n"]
    if active:
        lines.append("<b>Sedang maintenance:</b>")
        for scope, code in active:
            label = "Jaringan" if scope == cm.SCOPE_NETWORK else "Koin"
            note = f" — <i>{html.escape(flags[(scope, code)])}</i>" if flags[(scope, code)] else ""
            lines.append(f"• 🛠 {label} <b>{html.escape(code)}</b>{note}")
    else:
        lines.append("🟢 <b>Semua jaringan &amp; koin normal.</b>")
    lines.append(
        "\nKetuk tombol untuk menyalakan/mematikan maintenance. Order <b>baru</b> untuk yang ditandai "
        "ditutup; order yang sudah berjalan tetap diproses.\n"
        "<i>Dengan catatan untuk user: <code>/maintenance on NETWORK SOLANA RPC sedang padat</code></i>"
    )

    def _rows(scope_letter, codes, scope):
        buttons = [
            InlineKeyboardButton(
                f"{'🛠' if (scope, c) in flags else '🟢'} {c}",
                callback_data=f"admin_panel_mt_{scope_letter}_{c}",
            )
            for c in codes
        ]
        return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]

    keyboard = [[InlineKeyboardButton("— Jaringan —", callback_data="admin_panel_maint")]]
    keyboard += _rows("n", cm.known_networks(), cm.SCOPE_NETWORK)
    keyboard.append([InlineKeyboardButton("— Koin —", callback_data="admin_panel_maint")])
    keyboard += _rows("c", cm.known_coins(), cm.SCOPE_COIN)
    keyboard.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="admin_panel_maint"),
        InlineKeyboardButton("🔙 Dashboard Utama", callback_data="admin_panel_main"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def handle_maintenance_callback(query, data: str, admin_id: int) -> None:
    """Routing tombol panel: admin_panel_maint (tampil) / admin_panel_mt_<n|c>_<KODE> (toggle)."""
    toast = None
    if data.startswith("admin_panel_mt_"):
        try:
            _, _, _, letter, code = data.split("_", 4)
            scope = _SCOPE_KEY[letter]
        except (ValueError, KeyError):
            await query.answer("Tombol tidak dikenal.", show_alert=True)
            return
        known = cm.known_networks() if scope == cm.SCOPE_NETWORK else cm.known_coins()
        if code not in known:
            await query.answer("Kode tidak dikenal.", show_alert=True)
            return
        currently_on = (scope, code) in cm.load_flags()
        cm.set_flag(scope, code, not currently_on, admin_id=admin_id)
        toast = f"{code}: " + ("maintenance DIMATIKAN ✅" if currently_on else "maintenance DINYALAKAN 🛠")
        logger.info("Maintenance %s %s -> %s oleh admin %s", scope, code, not currently_on, admin_id)

    text, markup = build_maintenance_view()
    try:
        await query.edit_message_text(text=text, reply_markup=markup, parse_mode="HTML")
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    await query.answer(toast or "Status maintenance dimuat.")


async def maintenance_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/maintenance [on|off] [NETWORK|COIN] [KODE] [catatan...] — khusus admin."""
    from bot.handlers.admin import is_admin
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    args = context.args or []
    if not args:
        text, markup = build_maintenance_view()
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
        return

    action = args[0].lower()
    if action not in ("on", "off") or len(args) < 3 or args[1].upper() not in cm.SCOPES:
        await update.message.reply_text(
            "⚠️ <b>Format:</b>\n"
            "• <code>/maintenance on NETWORK SOLANA [catatan]</code>\n"
            "• <code>/maintenance off COIN USDT</code>\n"
            "• <code>/maintenance</code> — lihat status &amp; tombol",
            parse_mode="HTML",
        )
        return

    scope, code = args[1].upper(), args[2].upper()
    known = cm.known_networks() if scope == cm.SCOPE_NETWORK else cm.known_coins()
    if code not in known:
        await update.message.reply_text(
            f"❌ <code>{html.escape(code)}</code> tidak dikenal. Pilihan: "
            + ", ".join(f"<code>{c}</code>" for c in known),
            parse_mode="HTML",
        )
        return
    note = " ".join(args[3:])
    changed = cm.set_flag(scope, code, action == "on", note=note, admin_id=user.id)
    label = "Jaringan" if scope == cm.SCOPE_NETWORK else "Koin"
    if action == "on":
        msg = f"🛠 {label} <b>{code}</b> kini <b>MAINTENANCE</b>."
        msg += f"\nCatatan: <i>{html.escape(note)}</i>" if note else ""
    else:
        msg = f"✅ {label} <b>{code}</b> kembali <b>NORMAL</b>." if changed else f"ℹ️ {label} {code} memang tidak sedang maintenance."
    await update.message.reply_text(msg, parse_mode="HTML")
