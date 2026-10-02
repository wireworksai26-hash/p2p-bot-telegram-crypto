"""bot/utils/flow_guard.py — Guard pindah transaksi: cancel dulu sebelum ganti alur.

Mencegah dua conversation menumpuk (entry command + allow_reentry=True bisa
memulai flow baru di atas flow yang masih berjalan). State dibaca langsung
dari ConversationHandler._conversations milik PTB — tidak ada flag sendiri
yang bisa basi (stale) saat flow selesai/cancel.
"""

FLOW_LABELS = {
    "buy": "Beli Crypto",
    "sell": "Jual Crypto",
    "swap": "Convert/Swap",
    "topup": "Topup Saldo",
    "calc": "Kalkulator",
}


def _handlers():
    from bot.handlers.buy import buy_conversation_handler
    from bot.handlers.sell import sell_conversation_handler
    from bot.handlers.swap import swap_conv_handler
    from bot.handlers.balance import topup_conversation_handler
    from bot.handlers.calculator import calculator_conversation_handler
    return {
        "buy": buy_conversation_handler,
        "sell": sell_conversation_handler,
        "swap": swap_conv_handler,
        "topup": topup_conversation_handler,
        "calc": calculator_conversation_handler,
    }


def active_flow_names(update, exclude=None) -> list:
    """Daftar flow yang sedang aktif untuk user ini (selain `exclude`)."""
    user = update.effective_user if update else None
    chat = update.effective_chat if update else None
    if not user:
        return []
    u_id = user.id
    c_id = chat.id if chat else u_id
    keys_to_check = [(c_id, u_id), (u_id,), (u_id, u_id), (c_id,)]
    names = []
    for name, handler in _handlers().items():
        if name == exclude:
            continue
        convs = getattr(handler, "_conversations", None)
        if isinstance(convs, dict) and any(k in convs for k in keys_to_check):
            names.append(name)
    return names


async def block_if_busy(flow: str, update, context) -> bool:
    """True bila user sedang di flow lain: kirim peringatan, entry harus batal.

    Entry yang diblokir harus `return None` agar conversation lama tetap
    berjalan dan conversation baru tidak dimulai.
    """
    busy = active_flow_names(update, exclude=flow)
    if not busy:
        return False
    label = FLOW_LABELS.get(busy[0], busy[0])
    text = (
        f"⚠️ <b>Selesaikan dulu proses {label} yang sedang berjalan</b>, "
        "atau tekan /cancel untuk kembali ke menu."
    )
    try:
        query = getattr(update, "callback_query", None)
        if query:
            try:
                await query.answer()
            except Exception:
                pass
        message = update.effective_message if update else None
        if message:
            await message.reply_text(text, parse_mode="HTML")
    except Exception:
        pass
    return True
