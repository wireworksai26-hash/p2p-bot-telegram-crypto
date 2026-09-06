"""
bot/utils/messages.py — Template Pesan untuk P2P Crypto Bot.
============================================================
Definisi teks pesan dalam Bahasa Indonesia dengan formatting HTML.
"""

WELCOME_MESSAGE = (
    "👋 <b>Halo {name}!</b>\n\n"
    "Selamat datang di <b>HSN STORE bot</b> — P2P Crypto Trading Automation. 🚀\n\n"
    "🆔 <b>ID Telegram:</b> <code>{chat_id}</code>\n"
    "🏆 <b>Member Ke:</b> #{user_num}\n"
    "👥 <b>Total Pengguna:</b> {total_users} member\n"
    "✅ <b>Total Transaksi Berhasil:</b> {total_success}\n\n"
    "Silakan pilih menu di bawah ini untuk memulai transaksi:"
)

SNK_TEXT = (
    "⚠️ <b>SYARAT & KETENTUAN (S&K)</b>\n\n"
    "1. Bot ini beroperasi secara otomatis 24/7 untuk transaksi instan.\n"
    "2. Minimal pembelian/penjualan adalah <b>Rp 5.000</b>.\n"
    "3. Biaya transaksi (fee) dihitung secara fixed tier transparan sesuai nominal transaksi.\n"
    "4. Pastikan alamat wallet crypto Anda benar. Kesalahan input alamat bukan tanggung jawab kami!\n"
    "5. Transaksi tidak dapat dibatalkan setelah pembayaran diverifikasi.\n"
    "6. Jika mengalami kendala, hubungi owner dengan tombol <b>Hubungi Owner</b>.\n"
    "7. Apabila ada saran atau masukan untuk bot ini silakan chat admin untuk dilakukan perbaikan dan pembaruan.\n"
    "8. Tidak menerima top up USD dan Gram ke Address Exness, HashKey, wallet telegram."
)

ORDER_SUMMARY_BUY = (
    "🛒 <b>RINGKASAN ORDER PEMBELIAN</b>\n\n"
    "📝 <b>ID Order:</b> <code>{order_id}</code>\n"
    "🪙 <b>Aset:</b> {crypto_amount_str}\n"
    "📈 <b>Kurs:</b> {price_per_unit_str} / unit\n"
    "────────────────────\n"
    "💳 <b>Nominal Bayar:</b> {nominal_idr_str}\n"
    "🔌 <b>Fee Layanan (dipotong):</b> -{fee_idr_str}\n"
    "💰 <b>Nilai Koin Diterima:</b> <b>{received_idr_str}</b>\n\n"
    "📍 <b>Wallet Penerima:</b>\n<code>{buyer_wallet}</code>\n\n"
    "Silakan klik tombol konfirmasi di bawah jika data sudah benar."
)

ORDER_SUMMARY_SELL = (
    "📈 <b>RINGKASAN ORDER PENJUALAN</b>\n\n"
    "📝 <b>ID Order:</b> <code>{order_id}</code>\n"
    "🪙 <b>Kirim Aset:</b> {crypto_amount_str}\n"
    "📈 <b>Kurs:</b> {price_per_unit_str} / unit\n"
    "────────────────────\n"
    "💰 <b>Nominal Bersih (IDR):</b> <b>{nominal_idr_str}</b>\n"
    "🔌 <b>Fee Layanan:</b> {fee_idr_str}\n\n"
    "🏦 <b>Rekening Penerima Anda:</b>\n"
    "• Bank: {bank_name}\n"
    "• No Rekening: <code>{bank_acc}</code>\n"
    "• Atas Nama: {bank_holder}\n\n"
    "Silakan klik konfirmasi di bawah untuk memproses penjualan."
)


def format_sell_bank_info(buyer_wallet: str) -> str:
    """Format string bank info dari buyer_wallet menjadi tampilan HTML yang rapi."""
    import html
    if not buyer_wallet:
        return "• <b>Tujuan:</b> <i>Rekening Penerima</i>"
    parts = [p.strip() for p in str(buyer_wallet).split("|")]
    if len(parts) >= 3:
        bank = html.escape(parts[0])
        acc = html.escape(parts[1])
        holder = html.escape(parts[2])
        return (
            f"• <b>Bank / E-Wallet:</b> {bank}\n"
            f"• <b>No Rekening / HP:</b> <code>{acc}</code>\n"
            f"• <b>Atas Nama:</b> <b>{holder}</b>"
        )
    elif len(parts) == 2:
        bank = html.escape(parts[0])
        acc = html.escape(parts[1])
        return (
            f"• <b>Bank / E-Wallet:</b> {bank}\n"
            f"• <b>No Rekening / HP:</b> <code>{acc}</code>"
        )
    else:
        return f"• <b>Tujuan Rekening:</b> <code>{html.escape(buyer_wallet)}</code>"


def build_sell_completion_message(order) -> str:
    """Template notifikasi ke user saat Rupiah hasil penjualan crypto telah ditransfer oleh admin."""
    import html
    from bot.utils.formatter import format_idr, format_crypto

    bank_info_str = format_sell_bank_info(order.buyer_wallet or "")
    crypto_amount_val = float(order.crypto_amount) if order.crypto_amount else 0.0
    crypto_str = format_crypto(crypto_amount_val, order.crypto_symbol)
    nominal_str = format_idr(int(order.total_idr or 0))
    order_id = html.escape(str(order.order_id or ""))
    network = html.escape(str(order.network or ""))

    return (
        f"💸 <b>TRANSFER RUPIAH TELAH BERHASIL!</b>\n\n"
        f"Halo! Uang pembayaran hasil penjualan crypto Anda telah berhasil dikirimkan ke rekening tujuan:\n\n"
        f"📝 <b>Detail Pesanan:</b>\n"
        f"• <b>ID Order:</b> <code>{order_id}</code>\n"
        f"• <b>Koin Terjual:</b> <b>{crypto_str}</b> ({network})\n"
        f"• <b>Total Uang Diterima:</b> <b>{nominal_str}</b>\n\n"
        f"🏦 <b>Rekening Tujuan:</b>\n"
        f"{bank_info_str}\n\n"
        f"✅ <b>Status: SELESAI / COMPLETED</b>\n\n"
        f"<i>Uang telah berhasil ditransfer ke rekeningmu. Silakan periksa saldo atau mutasi rekening Anda. Terima kasih telah bertransaksi!</i> 🙏"
    )


def build_buy_completion_message(order) -> str:
    """Template notifikasi ke user saat koin crypto pesanan buy telah dikirim."""
    import html
    from bot.utils.formatter import format_idr, format_crypto

    crypto_amount_val = float(order.crypto_amount) if order.crypto_amount else 0.0
    crypto_str = format_crypto(crypto_amount_val, order.crypto_symbol)
    nominal_str = format_idr(int(order.total_idr or 0))
    order_id = html.escape(str(order.order_id or ""))
    network = html.escape(str(order.network or ""))
    wallet = html.escape(str(order.buyer_wallet or ""))

    return (
        f"✅ <b>PENGIRIMAN CRYPTO BERHASIL!</b>\n\n"
        f"Pesanan <code>{order_id}</code> telah selesai diproses oleh admin.\n\n"
        f"📝 <b>Detail Pesanan:</b>\n"
        f"• <b>Koin Dikirim:</b> <b>{crypto_str}</b> ({network})\n"
        f"• <b>Nominal:</b> <b>{nominal_str}</b>\n"
        f"• <b>Alamat Wallet:</b> <code>{wallet}</code>\n\n"
        f"✅ <b>Status: SELESAI / COMPLETED</b>\n\n"
        f"<i>Koin crypto telah berhasil dikirimkan ke wallet Anda. Terima kasih telah bertransaksi!</i> 🙏"
    )
