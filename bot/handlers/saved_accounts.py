"""
bot/handlers/saved_accounts.py — Handler Alamat Wallet & Rekening Pencairan Tersimpan (Phase 7 Enhanced).
=====================================================================================================
Memungkinkan pengguna untuk:
1. Menyimpan alamat wallet crypto per jaringan dengan auto-detection (EVM, Solana, Tron, SUI, TON, BTC).
2. Mengelompokkan tampilan alamat wallet berdasarkan chain_type dan set wallet Default.
3. Menyimpan rekening bank lokal & e-wallet untuk pencairan dana Jual (1-Tap).
4. Mengelola (lihat, tambah, set default, dan hapus) alamat wallet dan rekening tersimpan.
"""

import logging
from html import escape as _esc
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database.connection import SessionLocal
from database.crud import (
    get_user_saved_wallets,
    get_saved_wallets_grouped,
    get_saved_wallet_by_id,
    save_user_wallet,
    save_user_wallet_v2,
    is_wallet_address_taken_by_other,
    is_bank_account_taken_by_other,
    set_default_wallet,
    delete_user_saved_wallet,
    get_user_saved_banks,
    get_saved_bank_by_id,
    save_user_bank,
    delete_user_saved_bank,
    detect_account_type,
)
from services.wallet_detector import (
    detect_wallet_network,
    validate_wallet_address as _validate_wallet_by_detector,
    NETWORK_PATTERNS,
    EVM_CHAINS,
)
from bot.keyboards.main_menu import get_owner_button
from bot.utils.validator import validate_wallet_address
from bot.utils.messages import WALLET_DUPLICATE_WARNING, BANK_DUPLICATE_WARNING
from bot.utils.emojis import CUSTOM_EMOJI_IDS

logger = logging.getLogger(__name__)

CHAIN_EMOJIS = {
    "EVM": "🔷",
    "SOLANA": "🟣",
    "TRON": "🔴",
    "SUI": "🔵",
    "APTOS": "⚫",
    "TON": "💎",
    "BITCOIN": "🟠",
    "OTHER": "🌐",
}

CHAIN_TITLES = {
    "EVM": "EVM (BSC / ETH / Polygon / Arbitrum / Base)",
    "SOLANA": "Solana (SOL)",
    "TRON": "Tron (TRC20 / TRX)",
    "SUI": "SUI Network",
    "APTOS": "Aptos (APT)",
    "TON": "The Open Network (TON)",
    "BITCOIN": "Bitcoin (BTC)",
    "OTHER": "Lainnya",
}


# ============================================================
# 1. ALAMAT WALLET VIEW (Phase 7 Enhanced Grouped View)
# ============================================================

def build_saved_wallets_view(telegram_id: int, db, back_callback: str = "menu_balance") -> tuple[str, InlineKeyboardMarkup]:
    """Menyusun teks dan keyboard untuk menu Alamat Wallet yang dikelompokkan per chain."""
    grouped = get_saved_wallets_grouped(db, telegram_id)

    if not grouped:
        text = (
            "👛 <b>Alamat Wallet</b>\n"
            "-------------------------------------\n\n"
            "<code>Belum ada alamat tersimpan.</code>\n\n"
            "💡 Simpan alamat wallet Anda agar proses Beli koin crypto lebih cepat (1-Tap checkout) tanpa perlu ketik ulang alamat.\n\n"
            "✍️ <b>Pilih tombol di bawah untuk menambahkan / mengubah Address.</b>"
        )
        keyboard = [
            [InlineKeyboardButton("📌 Tambah / Simpan Address", callback_data="act_add_saved_wallet")],
            [
                InlineKeyboardButton("➕ EVM (BSC/ETH)", callback_data="act_add_wallet_EVM"),
                InlineKeyboardButton("➕ Solana", callback_data="act_add_wallet_SOLANA"),
            ],
            [
                InlineKeyboardButton("➕ TRON (TRC20)", callback_data="act_add_wallet_TRON"),
                InlineKeyboardButton("➕ TON", callback_data="act_add_wallet_TON"),
            ],
            [
                InlineKeyboardButton("➕ SUI", callback_data="act_add_wallet_SUI"),
                InlineKeyboardButton("➕ Aptos", callback_data="act_add_wallet_APTOS"),
            ],
            [InlineKeyboardButton("🔙 Kembali", callback_data=back_callback, icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
    else:
        sections = []
        wallet_counter = 1

        for chain_type, wallets in grouped.items():
            emoji = CHAIN_EMOJIS.get(chain_type, "🌐")
            title = CHAIN_TITLES.get(chain_type, chain_type)
            lines = [f"{emoji} <b>{title}</b>"]

            for w in wallets:
                short_addr = f"{w.wallet_address[:6]}...{w.wallet_address[-4:]}" if len(w.wallet_address) > 12 else w.wallet_address
                net_badge = f" [{w.network}]" if w.network else ""
                default_badge = " ⭐ <b>Default</b>" if w.is_default else ""
                label_badge = f" — <i>{_esc(w.label)}</i>" if w.label else ""
                lines.append(f"  {wallet_counter}. <code>{_esc(w.wallet_address)}</code>{net_badge}{default_badge}{label_badge}")
                wallet_counter += 1

            sections.append("\n".join(lines))

        full_list_text = "\n\n".join(sections)
        text = (
            "👛 <b>Alamat Wallet Tersimpan</b>\n"
            "-------------------------------------\n"
            "Daftar Alamat Tersimpan:\n\n"
            f"{full_list_text}\n\n"
            "✍️ <b>Kelola alamat wallet Anda di bawah:</b>"
        )
        keyboard = [
            [InlineKeyboardButton("📌 Tambah / Simpan Address", callback_data="act_add_saved_wallet")],
            [
                InlineKeyboardButton("➕ EVM", callback_data="act_add_wallet_EVM"),
                InlineKeyboardButton("➕ Solana", callback_data="act_add_wallet_SOLANA"),
                InlineKeyboardButton("➕ TRON", callback_data="act_add_wallet_TRON"),
            ],
            [
                InlineKeyboardButton("➕ TON", callback_data="act_add_wallet_TON"),
                InlineKeyboardButton("➕ SUI", callback_data="act_add_wallet_SUI"),
                InlineKeyboardButton("➕ Aptos", callback_data="act_add_wallet_APTOS"),
            ],
            [InlineKeyboardButton("⭐ Set Default", callback_data="act_set_default_wallet_menu")],
            [InlineKeyboardButton("🗑 Hapus Alamat", callback_data="act_del_saved_wallet_menu")],
            [InlineKeyboardButton("🔙 Kembali", callback_data=back_callback, icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]

    return text, InlineKeyboardMarkup(keyboard)


async def show_saved_wallets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, back_callback: str = None):
    """Menampilkan tampilan Alamat Wallet tersimpan."""
    user = update.effective_user
    if back_callback:
        context.user_data["saved_wallets_back"] = back_callback
    else:
        back_callback = context.user_data.get("saved_wallets_back", "menu_balance")

    db = SessionLocal()
    try:
        text, reply_markup = build_saved_wallets_view(user.id, db, back_callback=back_callback)
    finally:
        db.close()

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


# ============================================================
# 2. REKENING PENCAIRAN VIEW
# ============================================================

def build_saved_banks_view(telegram_id: int, db, back_callback: str = "menu_balance") -> tuple[str, InlineKeyboardMarkup]:
    """Menyusun teks dan keyboard untuk menu Rekening Pencairan."""
    banks = get_user_saved_banks(db, telegram_id)

    if not banks:
        text = (
            "🏦 <b>Rekening Pencairan</b>\n"
            "-------------------------------------\n\n"
            "<code>Belum ada rekening tersimpan</code>\n\n"
            "Pilih tombol dibawah untuk menambah atau ganti rekening."
        )
        keyboard = [
            [InlineKeyboardButton("✍️ Tambah / Ganti Rekening", callback_data="act_add_saved_bank")],
            [InlineKeyboardButton("🔙 Kembali", callback_data=back_callback, icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
    else:
        bank_lines = []
        for i, b in enumerate(banks, 1):
            icon = "📱" if b.account_type == "EWALLET" else "🏦"
            bank_lines.append(
                f"<b>{i}.</b> {icon} <b>{_esc(b.bank_name)}</b> - <code>{_esc(b.account_number)}</code>\n"
                f"   └ a.n <i>{_esc(b.account_name)}</i>"
            )

        list_text = "\n\n".join(bank_lines)
        text = (
            "🏦 <b>Rekening Pencairan</b>\n"
            "-------------------------------------\n\n"
            f"<b>Daftar Rekening / E-Wallet Tersimpan:</b>\n\n{list_text}\n\n"
            "Pilih tombol dibawah untuk menambah atau ganti rekening."
        )
        keyboard = [
            [InlineKeyboardButton("✍️ Tambah / Ganti Rekening", callback_data="act_add_saved_bank")],
            [InlineKeyboardButton("🗑 Hapus Rekening", callback_data="act_del_saved_bank_menu")],
            [InlineKeyboardButton("🔙 Kembali", callback_data=back_callback, icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]

    return text, InlineKeyboardMarkup(keyboard)


async def show_saved_banks_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, back_callback: str = None):
    """Menampilkan tampilan Rekening Pencairan tersimpan."""
    user = update.effective_user
    if back_callback:
        context.user_data["saved_banks_back"] = back_callback
    else:
        back_callback = context.user_data.get("saved_banks_back", "menu_balance")

    db = SessionLocal()
    try:
        text, reply_markup = build_saved_banks_view(user.id, db, back_callback=back_callback)
    finally:
        db.close()

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


# ============================================================
# 3. INTERACTIVE ADD / EDIT HANDLERS (Phase 7 Enhanced)
# ============================================================

async def prompt_add_saved_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Meminta user mengetik alamat wallet baru (Auto-detect)."""
    query = update.callback_query
    await query.answer()

    context.user_data["awaiting_save_wallet"] = True
    context.user_data["target_chain_type"] = None  # None = auto-detect
    context.user_data.pop("awaiting_save_bank", None)

    keyboard = [
        [InlineKeyboardButton("🔙 Batal", callback_data="menu_saved_wallets", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]
    ]

    await query.edit_message_text(
        text=(
            "📌 <b>Tambah Alamat Wallet (Auto-detect)</b>\n"
            "-------------------------------------\n\n"
            "Kirimkan <b>Alamat Wallet</b> Anda di room chat ini. Sistem akan secara otomatis mendeteksi jaringan yang sesuai.\n\n"
            "• Contoh <b>EVM (BSC / ETH / Polygon)</b>:\n"
            "  <code>0x71C839556CB3250b716773B3aBE329a4a796c9c6</code>\n"
            "• Contoh <b>TRON (TRC20)</b>:\n"
            "  <code>TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t</code>\n"
            "• Contoh <b>SOLANA</b>:\n"
            "  <code>9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM</code>\n"
            "• Contoh <b>TON</b>:\n"
            "  <code>EQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N</code>\n"
            "• Contoh <b>SUI</b>:\n"
            "  <code>0x524c7dbda9eb0fca02d68a2e1d7cf9d164dfb3a99bbbebd04353f81e354a8677</code>\n\n"
            "<i>Ketik /cancel untuk membatalkan.</i>"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def prompt_add_wallet_specific(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Meminta user input wallet untuk chain tertentu dari tombol per-chain."""
    query = update.callback_query
    await query.answer()

    # callback format: act_add_wallet_{CHAIN_TYPE}
    chain_type = query.data.replace("act_add_wallet_", "").upper()
    context.user_data["awaiting_save_wallet"] = True
    context.user_data["target_chain_type"] = chain_type
    context.user_data.pop("awaiting_save_bank", None)

    title = CHAIN_TITLES.get(chain_type, chain_type)
    emoji = CHAIN_EMOJIS.get(chain_type, "👛")

    keyboard = [
        [InlineKeyboardButton("🔙 Batal", callback_data="menu_saved_wallets", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]
    ]

    await query.edit_message_text(
        text=(
            f"{emoji} <b>Tambah Alamat Wallet {title}</b>\n"
            "-------------------------------------\n\n"
            f"Silakan kirimkan alamat wallet <b>{chain_type}</b> Anda di chat ini.\n\n"
            "<i>Ketik /cancel untuk membatalkan.</i>"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def prompt_add_saved_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Meminta user mengetik rekening bank atau e-wallet untuk disimpan."""
    query = update.callback_query
    await query.answer()

    context.user_data["awaiting_save_bank"] = True
    context.user_data.pop("awaiting_save_wallet", None)

    keyboard = [
        [InlineKeyboardButton("🔙 Batal", callback_data="menu_saved_banks", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]
    ]

    await query.edit_message_text(
        text=(
            "✍️ <b>Tambah / Ganti Rekening Pencairan</b>\n"
            "-------------------------------------\n\n"
            "Mendukung semua <b>Rekening Bank</b> (BCA, Mandiri, BRI, BNI, Seabank, dll) dan <b>E-Wallet</b> (GoPay, OVO, DANA, ShopeePay, dll).\n\n"
            "Silakan ketik detail rekening Anda dengan format koma:\n"
            "<code>Nama Bank, No Rekening, Atas Nama</code>\n\n"
            "<b>Contoh Bank:</b>\n"
            "<code>BCA, 882049281, Budi Santoso</code>\n\n"
            "<b>Contoh E-Wallet:</b>\n"
            "<code>GOPAY, 081234567890, Budi Santoso</code>\n"
            "<code>DANA, 085712345678, Siti Aminah</code>\n\n"
            "<i>Ketik /cancel untuk membatalkan.</i>"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


# ============================================================
# 4. SET DEFAULT WALLET & CONFIRM EVM CHAIN
# ============================================================

async def show_set_default_wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan daftar wallet untuk dipilih sebagai Default."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    db = SessionLocal()
    try:
        wallets = get_user_saved_wallets(db, user.id)
    finally:
        db.close()

    if not wallets:
        await show_saved_wallets_menu(update, context)
        return

    keyboard = []
    for w in wallets:
        short = f"{w.wallet_address[:6]}...{w.wallet_address[-4:]}" if len(w.wallet_address) > 12 else w.wallet_address
        net = f" [{w.network}]" if w.network else ""
        def_tag = " ⭐ (Default)" if w.is_default else ""
        keyboard.append([
            InlineKeyboardButton(f"⭐ Jadikan Default: {short}{net}{def_tag}", callback_data=f"act_set_default_{w.id}")
        ])

    keyboard.append([InlineKeyboardButton("🔙 Batal", callback_data="menu_saved_wallets", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))])

    await query.edit_message_text(
        text=(
            "⭐ <b>Pilih Alamat Wallet Default</b>\n\n"
            "Wallet default akan otomatis diutamakan saat Anda melakukan pembelian crypto pada jaringan tersebut:"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def handle_set_default_wallet_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Eksekusi set wallet sebagai default."""
    query = update.callback_query
    user = update.effective_user
    wallet_id = int(query.data.replace("act_set_default_", ""))

    db = SessionLocal()
    try:
        success = set_default_wallet(db, wallet_id, user.id)
    finally:
        db.close()

    if success:
        await query.answer("⭐ Wallet berhasil dijadikan Default!", show_alert=True)
    else:
        await query.answer("⚠️ Gagal mengubah wallet default.", show_alert=True)

    await show_saved_wallets_menu(update, context)


async def handle_confirm_evm_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menyimpan wallet EVM setelah user memilih sub-chain spesifik (BSC, ETH, Polygon, dll)."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    # format: act_evm_chain_{CHAIN}
    chosen_chain = query.data.replace("act_evm_chain_", "").upper()
    pending_addr = context.user_data.pop("pending_wallet_address", None)

    if not pending_addr:
        await query.edit_message_text("⚠️ Sesi penyimpanan telah kadaluarsa. Silakan ulangi input alamat wallet.")
        return

    network_val = None if chosen_chain == "ALL" else chosen_chain
    db = SessionLocal()
    try:
        saved = save_user_wallet_v2(
            db=db,
            telegram_id=user.id,
            wallet_address=pending_addr,
            network=network_val,
            chain_type="EVM",
            auto_detected=True,
        )
    finally:
        db.close()

    chain_display = f"EVM ({chosen_chain})" if chosen_chain != "ALL" else "EVM (Semua Network)"
    keyboard = [
        [InlineKeyboardButton("👛 Lihat Alamat Wallet", callback_data="menu_saved_wallets")],
        [InlineKeyboardButton("🔙 Ke Menu Utama", callback_data="menu_back")]
    ]
    await query.edit_message_text(
        text=(
            "✅ <b>Alamat Wallet EVM Berhasil Disimpan!</b>\n\n"
            f"• Jaringan: <b>{chain_display}</b>\n"
            f"• Alamat: <code>{_esc(saved.wallet_address)}</code>\n\n"
            "<i>Alamat ini akan otomatis muncul sebagai opsi 1-Tap saat Anda melakukan pembelian di jaringan terkait!</i>"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


# ============================================================
# 5. DELETE MENUS & ACTIONS
# ============================================================

async def show_delete_wallet_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan pilihan wallet mana yang ingin dihapus."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    db = SessionLocal()
    try:
        wallets = get_user_saved_wallets(db, user.id)
    finally:
        db.close()

    if not wallets:
        await show_saved_wallets_menu(update, context)
        return

    keyboard = []
    for w in wallets:
        short = f"{w.wallet_address[:6]}...{w.wallet_address[-4:]}" if len(w.wallet_address) > 12 else w.wallet_address
        net = f" ({w.network})" if w.network else ""
        keyboard.append([
            InlineKeyboardButton(f"❌ Hapus: {short}{net}", callback_data=f"act_del_wallet_{w.id}")
        ])

    keyboard.append([InlineKeyboardButton("🔙 Batal", callback_data="menu_saved_wallets", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))])

    await query.edit_message_text(
        text=(
            "🗑 <b>Hapus Alamat Wallet Tersimpan</b>\n\n"
            "Pilih alamat wallet yang ingin Anda hapus:"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def handle_delete_wallet_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Eksekusi hapus wallet tersimpan."""
    query = update.callback_query
    user = update.effective_user
    wallet_id = int(query.data.replace("act_del_wallet_", ""))

    db = SessionLocal()
    try:
        success = delete_user_saved_wallet(db, wallet_id, user.id)
    finally:
        db.close()

    if success:
        await query.answer("✅ Alamat wallet berhasil dihapus!", show_alert=True)
    else:
        await query.answer("⚠️ Alamat wallet tidak ditemukan atau sudah dihapus.", show_alert=True)

    await show_saved_wallets_menu(update, context)


async def show_delete_bank_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan pilihan rekening mana yang ingin dihapus."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    db = SessionLocal()
    try:
        banks = get_user_saved_banks(db, user.id)
    finally:
        db.close()

    if not banks:
        await show_saved_banks_menu(update, context)
        return

    keyboard = []
    for b in banks:
        icon = "📱" if b.account_type == "EWALLET" else "🏦"
        btn_text = f"❌ {icon} {b.bank_name} - {b.account_number}"
        if len(btn_text) > 40:
            btn_text = btn_text[:37] + "..."
        keyboard.append([
            InlineKeyboardButton(btn_text, callback_data=f"act_del_bank_{b.id}")
        ])

    keyboard.append([InlineKeyboardButton("🔙 Batal", callback_data="menu_saved_banks", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))])

    await query.edit_message_text(
        text=(
            "🗑 <b>Hapus Rekening Pencairan</b>\n\n"
            "Pilih rekening bank atau e-wallet yang ingin Anda hapus:"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def handle_delete_bank_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Eksekusi hapus rekening tersimpan."""
    query = update.callback_query
    user = update.effective_user
    bank_id = int(query.data.replace("act_del_bank_", ""))

    db = SessionLocal()
    try:
        success = delete_user_saved_bank(db, bank_id, user.id)
    finally:
        db.close()

    if success:
        await query.answer("✅ Rekening berhasil dihapus!", show_alert=True)
    else:
        await query.answer("⚠️ Rekening tidak ditemukan atau sudah dihapus.", show_alert=True)

    await show_saved_banks_menu(update, context)


# ============================================================
# 6. TEXT INPUT HANDLER (Interactive Router)
# ============================================================

async def handle_saved_account_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Menangani input teks dari user saat sedang dalam flow simpan wallet atau simpan rekening.
    Return True jika pesan diproses oleh handler ini, False jika tidak.
    """
    if not update.message or not update.message.text:
        return False

    raw_text = update.message.text.strip()
    user = update.effective_user

    if raw_text.lower() in ["/cancel", "batal"]:
        context.user_data.pop("awaiting_save_wallet", None)
        context.user_data.pop("awaiting_save_bank", None)
        context.user_data.pop("target_chain_type", None)
        await update.message.reply_text("❌ Proses penyimpanan dibatalkan.")
        return True

    # 1. Flow Simpan Wallet
    if context.user_data.get("awaiting_save_wallet"):
        context.user_data.pop("awaiting_save_wallet", None)
        target_chain = context.user_data.pop("target_chain_type", None)

        # Deteksi format jaringan
        detected = detect_wallet_network(raw_text)

        # Aptos & SUI berformat sama (0x + 64 hex): pilihan tombol user yang menentukan
        if target_chain == "APTOS" and not detected:
            if validate_wallet_address(raw_text, "APTOS"):
                detected = {**NETWORK_PATTERNS["APTOS"]}
        elif target_chain == "APTOS" and detected and detected["chain_type"] == "SUI":
            detected = {**NETWORK_PATTERNS["APTOS"]}

        # Jika user memilih tombol spesifik (misal Solana), validasi formatnya
        if target_chain and detected and detected["chain_type"] != target_chain:
            keyboard = [[InlineKeyboardButton("Coba Lagi", callback_data=f"act_add_wallet_{target_chain}")]]
            await update.message.reply_text(
                f"❌ <b>Format Alamat Tidak Sesuai!</b>\n\n"
                f"Alamat yang Anda masukkan bukan format valid untuk <b>{target_chain}</b>.\n"
                f"Format terdeteksi: <b>{detected['label']}</b>.\n\n"
                f"Silakan coba lagi:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return True

        if not detected and (len(raw_text) < 15 or len(raw_text) > 250):
            keyboard = [[InlineKeyboardButton("Coba Lagi", callback_data="act_add_saved_wallet")]]
            await update.message.reply_text(
                "❌ <b>Alamat Wallet Tidak Dikenali!</b>\n\n"
                "Format alamat tidak cocok dengan jaringan blockchain yang didukung "
                "(EVM/BSC/ETH, Solana, Tron, SUI, TON, BTC).\n"
                "Pastikan alamat yang Anda kirimkan benar.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return True

        chain_type = detected["chain_type"] if detected else (target_chain or "OTHER")

        # Mapping network default
        net_map = {
            "EVM": "BSC",
            "SOLANA": "SOLANA",
            "TRON": "TRC20",
            "SUI": "SUI",
            "APTOS": "APTOS",
            "TON": "TON",
            "BITCOIN": "BTC",
        }
        network_name = net_map.get(chain_type, chain_type)

        db = SessionLocal()
        try:
            taken = is_wallet_address_taken_by_other(db, raw_text, user.id)
        finally:
            db.close()
        if taken:
            context.user_data["awaiting_save_wallet"] = True
            if target_chain:
                context.user_data["target_chain_type"] = target_chain
            await update.message.reply_text(WALLET_DUPLICATE_WARNING, parse_mode="HTML")
            return True

        db = SessionLocal()
        try:
            saved = save_user_wallet_v2(
                db=db,
                telegram_id=user.id,
                wallet_address=raw_text,
                network=network_name,
                chain_type=chain_type,
                auto_detected=True,
            )
        finally:
            db.close()

        emoji = CHAIN_EMOJIS.get(chain_type, "👛")
        title = CHAIN_TITLES.get(chain_type, chain_type)

        if chain_type == "EVM":
            context.user_data["pending_wallet_address"] = raw_text
            keyboard = [
                [
                    InlineKeyboardButton("BSC (BNB)", callback_data="act_evm_chain_BSC"),
                    InlineKeyboardButton("Ethereum", callback_data="act_evm_chain_ETH"),
                    InlineKeyboardButton("Polygon", callback_data="act_evm_chain_POLYGON"),
                ],
                [
                    InlineKeyboardButton("Arbitrum", callback_data="act_evm_chain_ARBITRUM"),
                    InlineKeyboardButton("Base", callback_data="act_evm_chain_BASE"),
                    InlineKeyboardButton("🌐 Semua EVM", callback_data="act_evm_chain_ALL"),
                ],
                [
                    InlineKeyboardButton("👛 Lihat Alamat Wallet", callback_data="menu_saved_wallets"),
                    InlineKeyboardButton("🔙 Ke Menu Utama", callback_data="menu_back"),
                ],
            ]
            await update.message.reply_text(
                text=(
                    "✅ <b>Alamat Wallet Berhasil Disimpan!</b>\n\n"
                    f"• Network Terdeteksi: <b>EVM ({network_name})</b>\n"
                    f"• Alamat: <code>{_esc(saved.wallet_address)}</code>\n\n"
                    "<i>Alamat ini otomatis aktif untuk transaksi Beli. Jika Anda ingin mengkhususkan ke sub-chain tertentu, pilih tombol di bawah:</i>"
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return True
        try:
            saved = save_user_wallet_v2(
                db=db,
                telegram_id=user.id,
                wallet_address=raw_text,
                network=network_name,
                chain_type=chain_type,
                auto_detected=True,
            )
        finally:
            db.close()

        emoji = CHAIN_EMOJIS.get(chain_type, "👛")
        title = CHAIN_TITLES.get(chain_type, chain_type)

        keyboard = [
            [InlineKeyboardButton("👛 Lihat Alamat Wallet", callback_data="menu_saved_wallets")],
            [InlineKeyboardButton("🔙 Ke Menu Utama", callback_data="menu_back")]
        ]
        await update.message.reply_text(
            text=(
                f"✅ <b>Alamat Wallet Berhasil Disimpan!</b>\n\n"
                f"• Jaringan: {emoji} <b>{title}</b>\n"
                f"• Alamat: <code>{_esc(saved.wallet_address)}</code>\n\n"
                "<i>Alamat ini akan otomatis muncul sebagai opsi 1-Tap saat Anda melakukan pembelian koin crypto!</i>"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return True

    # 2. Flow Simpan Rekening / E-Wallet
    if context.user_data.get("awaiting_save_bank"):
        context.user_data.pop("awaiting_save_bank", None)

        parts = [p.strip() for p in raw_text.split(",") if p.strip()]
        if len(parts) < 3:
            keyboard = [[InlineKeyboardButton("Coba Lagi", callback_data="act_add_saved_bank")]]
            await update.message.reply_text(
                "❌ <b>Format Rekening Tidak Lengkap!</b>\n\n"
                "Harap gunakan tanda koma untuk memisahkan:\n"
                "<code>Nama Bank, No Rekening, Atas Nama</code>\n\n"
                "<i>Contoh Bank:</i> <code>BCA, 882049281, Budi Santoso</code>\n"
                "<i>Contoh E-Wallet:</i> <code>GOPAY, 081234567890, Budi Santoso</code>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return True

        bank_name = parts[0]
        account_number = parts[1]
        account_name = " ".join(parts[2:])

        clean_num = "".join(c for c in account_number if c.isdigit() or c.isalnum())
        if len(clean_num) < 6:
            keyboard = [[InlineKeyboardButton("Coba Lagi", callback_data="act_add_saved_bank")]]
            await update.message.reply_text(
                "❌ <b>Nomor Rekening Terlalu Pendek!</b>\n\n"
                "Pastikan nomor rekening atau nomor HP e-wallet yang Anda masukkan valid.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return True

        db = SessionLocal()
        try:
            if is_bank_account_taken_by_other(db, clean_num, user.id):
                context.user_data["awaiting_save_bank"] = True   # minta input ulang
                await update.message.reply_text(BANK_DUPLICATE_WARNING, parse_mode="HTML")
                return True
            saved = save_user_bank(
                db=db,
                telegram_id=user.id,
                bank_name=bank_name,
                account_number=clean_num,
                account_name=account_name
            )
        finally:
            db.close()

        icon = "📱" if saved.account_type == "EWALLET" else "🏦"
        type_str = "E-Wallet" if saved.account_type == "EWALLET" else "Rekening Bank"

        keyboard = [
            [InlineKeyboardButton("🏦 Lihat Rekening Pencairan", callback_data="menu_saved_banks")],
            [InlineKeyboardButton("🔙 Ke Menu Utama", callback_data="menu_back")]
        ]
        await update.message.reply_text(
            text=(
                f"✅ <b>{type_str} Berhasil Disimpan!</b>\n\n"
                f"{icon} <b>{_esc(saved.bank_name)}</b>\n"
                f"• No. Rekening / HP: <code>{_esc(saved.account_number)}</code>\n"
                f"• Atas Nama: <b>{_esc(saved.account_name)}</b>\n"
                f"• Kategori: <b>{type_str}</b>\n\n"
                "<i>Rekening ini akan otomatis muncul sebagai tombol pilihan cepat saat Anda melakukan penjualan koin crypto!</i>"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return True

    return False
