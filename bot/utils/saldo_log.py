"""Riwayat topup & withdraw saldo bot ke topik "Topup & Withdraw Saldo".

Dipasang dengan /settarget topup (alias: /settarget withdraw) di topik tujuan. Bila belum dipasang,
fungsi ini tidak mengirim apa-apa. Tidak pernah melempar error: riwayat tidak boleh menggagalkan saldo.
"""
import logging
from html import escape as _esc

from bot.utils.admin_alert import idr_copy, user_label
from bot.utils.formatter import format_idr
from bot.utils.telegram_utils import kirim_ke_topik

logger = logging.getLogger(__name__)

_VIA = {"auto": "otomatis (mutasi GoPay)", "manual": "cek manual user", "admin": "approve admin"}


async def log_topup_success(bot, topup, net_amount, new_balance, is_treasury=False, via="auto", db=None) -> None:
    try:
        mdr = int(getattr(topup, "mdr_idr", 0) or 0)
        target = "Kas Bot" if is_treasury else "Saldo Bot user"
        text = (
            f"✅ <b>TOPUP {target.upper()} BERHASIL</b>\n\n"
            f"🎫 ID: <code>{_esc(str(topup.topup_id))}</code>\n"
            f"User: {user_label(topup.telegram_id, db)}\n"
            f"Dibayar (QRIS): {format_idr(int(topup.amount_idr or 0))}"
            f"{f' · pajak QRIS {format_idr(mdr)}' if mdr else ''}\n"
            f"Masuk ke {target}: <b>{format_idr(int(net_amount or 0))}</b>\n"
            f"{target} sekarang: <b>{format_idr(int(new_balance or 0))}</b>\n"
            f"Via: {_VIA.get(via, via)}"
        )
        await kirim_ke_topik(bot, kind="topup", text=text)
    except Exception as exc:
        logger.warning("Riwayat topup gagal dikirim: %s", exc)


async def log_withdraw_event(bot, req_id, telegram_id, amount, bank_name, acc_no, acc_name, status, admin_id=None, db=None) -> None:
    """status: PENDING (diajukan), PAID (sudah ditransfer), REJECTED (ditolak & direfund)."""
    try:
        head = {"PENDING": "📝 WITHDRAW SALDO DIAJUKAN", "PAID": "✅ WITHDRAW SALDO SUDAH DITRANSFER",
                "REJECTED": "❌ WITHDRAW SALDO DITOLAK (SALDO DIREFUND)"}.get(status, "WITHDRAW SALDO")
        handled = f"\nDiproses admin: <code>{admin_id}</code>" if admin_id else ""
        text = (
            f"<b>{head}</b>\n\n"
            f"🎫 ID: <code>WD-{req_id}</code>\n"
            f"User: {user_label(telegram_id, db)}\n"
            f"Nominal: {idr_copy(amount)}\n"
            f"Tujuan: <b>{_esc(str(bank_name))}</b> <code>{_esc(str(acc_no))}</code> a.n {_esc(str(acc_name))}"
            f"{handled}"
        )
        await kirim_ke_topik(bot, kind="topup", text=text)
    except Exception as exc:
        logger.warning("Riwayat withdraw gagal dikirim: %s", exc)
