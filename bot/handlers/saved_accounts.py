"""
bot/handlers/saved_accounts.py — Handler Alamat Wallet & Rekening Pencairan Tersimpan.
=====================================================================================
Memungkinkan pengguna untuk:
1. Menyimpan alamat wallet crypto untuk kemudahan checkout Beli (1-Tap).
2. Menyimpan rekening bank lokal & e-wallet untuk pencairan dana Jual (1-Tap).
3. Mengelola (lihat, tambah, ganti, dan hapus) alamat wallet dan rekening tersimpan.
"""

import logging
from html import escape as _esc
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from database.connection import SessionLocal
from database.crud import (
    get_user_saved_wallets,
    get_saved_wallet_by_id,
    save_user_wallet,
    delete_user_saved_wallet,
    get_user_saved_banks,
    get_saved_bank_by_id,
    save_user_bank,
    delete_user_saved_bank,
    detect_account_type,
)
from bot.keyboards.main_menu import get_owner_button
from bot.utils.validator import validate_wallet_address
from bot.utils.emojis import CUSTOM_EMOJI_IDS

logger = logging.getLogger(__name__)


def _detect_wallet_network(address: str) -> str:
    """Deteksi kemungkinan network dari format alamat wallet."""
    addr = address.strip()
    if addr.startswith("0x") and len(addr) == 42:
        return "BSC"  # Default EVM
    elif addr.startswith("T") and len(addr) == 34:
        return "TRON"
    elif addr.startswith("EQ") or addr.startswith("UQ"):
        return "TON"
    elif len(addr) >= 32 and len(addr) <= 44 and not addr.startswith("0x"):
        return "SOLANA"
    return "EVM"


# ============================================================
# 1. ALAMAT WALLET VIEW (Screenshot 1)
# ============================================================

def build_saved_wallets_view(telegram_id: int, db) -> tuple[str, InlineKeyboardMarkup]:
    """Menyusun teks dan keyboard untuk menu Alamat Wallet."""
    wallets = get_user_saved_wallets(db, telegram_id)

    if not wallets:
        text = (
            "👛 <b>Alamat Wallet</b>\n"
            "-------------------------------------\n\n"
            "<code>Belum ada alamat tersimpan.</code>\n\n"
            "✍️ <b>Pilih tombol di bawah untuk menambahkan / mengubah Addres.</b>"
        )
        keyboard = [
            [InlineKeyboardButton("📌 Tambah / Simpan Addres", callback_data="act_add_saved_wallet")],
            [InlineKeyboardButton("🔙 Kembali", callback_data="menu_balance", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]
    else:
        wallet_lines = []
        for i, w in enumerate(wallets, 1):
            net_tag = f" ({w.network})" if w.network else ""
            lbl_tag = f" - <i>{_esc(w.label)}</i>" if w.label else ""
            wallet_lines.append(f"<b>{i}.</b> <code>{_esc(w.wallet_address)}</code>{net_tag}{lbl_tag}")

        list_text = "\n".join(wallet_lines)
        text = (
            "👛 <b>Alamat Wallet</b>\n"
            "-------------------------------------\n\n"
            f"<b>Daftar Alamat Tersimpan:</b>\n{list_text}\n\n"
            "✍️ <b>Pilih tombol di bawah untuk menambahkan / mengubah Addres.</b>"
        )
        keyboard = [
            [InlineKeyboardButton("📌 Tambah / Simpan Addres", callback_data="act_add_saved_wallet")],
            [InlineKeyboardButton("🗑 Hapus Alamat", callback_data="act_del_saved_wallet_menu")],
            [InlineKeyboardButton("🔙 Kembali", callback_data="menu_balance", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]

    return text, InlineKeyboardMarkup(keyboard)


async def show_saved_wallets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan tampilan Alamat Wallet tersimpan."""
    user = update.effective_user
    db = SessionLocal()
    try:
        text, reply_markup = build_saved_wallets_view(user.id, db)
    finally:
        db.close()

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


# ============================================================
# 2. REKENING PENCAIRAN VIEW (Screenshot 3)
# ============================================================

def build_saved_banks_view(telegram_id: int, db) -> tuple[str, InlineKeyboardMarkup]:
    """Menyusun teks dan keyboard untuk menu Rekening Pencairan."""
    banks = get_user_saved_banks(db, telegram_id)

    if not banks:
        text = (
            "👛 <b>Rekening Pencairan</b>\n"
            "-------------------------------------\n\n"
            "<code>Belum ada rekening tersimpan</code>\n\n"
            "Pilih tombol dibawah untuk menambah atau ganti rekening."
        )
        keyboard = [
            [InlineKeyboardButton("✍️ Tambah / Ganti Rekening", callback_data="act_add_saved_bank")],
            [InlineKeyboardButton("🔙 Kembali", callback_data="menu_balance", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
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
            "👛 <b>Rekening Pencairan</b>\n"
            "-------------------------------------\n\n"
            f"<b>Daftar Rekening / E-Wallet Tersimpan:</b>\n\n{list_text}\n\n"
            "Pilih tombol dibawah untuk menambah atau ganti rekening."
        )
        keyboard = [
            [InlineKeyboardButton("✍️ Tambah / Ganti Rekening", callback_data="act_add_saved_bank")],
            [InlineKeyboardButton("🗑 Hapus Rekening", callback_data="act_del_saved_bank_menu")],
            [InlineKeyboardButton("🔙 Kembali", callback_data="menu_balance", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))],
            [get_owner_button()]
        ]

    return text, InlineKeyboardMarkup(keyboard)


async def show_saved_banks_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan tampilan Rekening Pencairan tersimpan."""
    user = update.effective_user
    db = SessionLocal()
    try:
        text, reply_markup = build_saved_banks_view(user.id, db)
    finally:
        db.close()

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


# ============================================================
# 3. INTERACTIVE ADD / EDIT HANDLERS
# ============================================================

async def prompt_add_saved_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Meminta user mengetik alamat wallet baru untuk disimpan."""
    query = update.callback_query
    await query.answer()

    context.user_data["awaiting_save_wallet"] = True
    context.user_data.pop("awaiting_save_bank", None)

    keyboard = [
        [InlineKeyboardButton("🔙 Batal", callback_data="menu_saved_wallets", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]
    ]

    await query.edit_message_text(
        text=(
            "📌 <b>Tambah / Simpan Alamat Wallet</b>\n"
            "-------------------------------------\n\n"
            "Silakan kirimkan <b>Alamat Wallet</b> Anda di room chat ini.\n\n"
            "• Contoh EVM (BSC / ETH / Polygon / Arbitrum):\n"
            "  <code>0x71C839556CB3250b716773B3aBE329a4a796c9c6</code>\n"
            "• Contoh TRON (TRC20):\n"
            "  <code>TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t</code>\n"
            "• Contoh SOLANA:\n"
            "  <code>9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM</code>\n\n"
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
# 4. DELETE MENUS & ACTIONS
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
# 5. TEXT INPUT HANDLER (Interactive Router)
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
        await update.message.reply_text("❌ Proses penyimpanan dibatalkan.")
        return True

    # 1. Flow Simpan Wallet
    if context.user_data.get("awaiting_save_wallet"):
        context.user_data.pop("awaiting_save_wallet", None)

        if len(raw_text) < 15 or len(raw_text) > 250:
            keyboard = [[InlineKeyboardButton("Coba Lagi", callback_data="act_add_saved_wallet")]]
            await update.message.reply_text(
                "❌ <b>Alamat Wallet Tidak Valid!</b>\n\n"
                "Panjang karakter alamat wallet tidak sesuai. Silakan pastikan alamat yang Anda kirimkan benar.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return True

        detected_net = _detect_wallet_network(raw_text)
        db = SessionLocal()
        try:
            saved = save_user_wallet(
                db=db,
                telegram_id=user.id,
                wallet_address=raw_text,
                network=detected_net
            )
        finally:
            db.close()

        keyboard = [
            [InlineKeyboardButton("👛 Lihat Alamat Wallet", callback_data="menu_saved_wallets")],
            [InlineKeyboardButton("🔙 Ke Menu Utama", callback_data="menu_back")]
        ]
        await update.message.reply_text(
            text=(
                "✅ <b>Alamat Wallet Berhasil Disimpan!</b>\n\n"
                f"• Network Terdeteksi: <b>{saved.network}</b>\n"
                f"• Alamat: <code>{_esc(saved.wallet_address)}</code>\n\n"
                "<i>Alamat ini akan otomatis muncul sebagai tombol pilihan cepat saat Anda melakukan pembelian koin crypto!</i>"
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
            [InlineKeyboardButton("👛 Lihat Rekening Pencairan", callback_data="menu_saved_banks")],
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
