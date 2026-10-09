"""
bot/utils/user_guide.py — Panduan transaksi untuk user (tombol "Panduan Transaksi").
===================================================================================
Bahasa sederhana, langkah demi langkah, dengan peringatan di bagian yang sering bikin gagal.
Tiap halaman < 4096 karakter (batas Telegram). Perintah yang disebut di <code>/perintah</code>
dicek tes terhadap handler yang benar-benar terdaftar. Gunakan &lt; &gt; untuk kurung sudut.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.main_menu import get_owner_button
from config.settings import settings
from services import quote_guard

GUIDE_INDEX_TEXT = (
    "📖 <b>PANDUAN TRANSAKSI</b>\n\n"
    "Bingung cara transaksi? Pilih topik di bawah. Semua dijelaskan pelan-pelan, langkah demi langkah.\n\n"
    "🚀 Baru pertama kali? Mulai dari <b>Mulai Cepat</b>.\n"
    "🔗 Mau jual atau convert? Baca juga <b>Cara Dapat TX Hash</b>, itu kunci supaya transaksimu "
    "cepat diproses.\n"
    "❓ Ada kendala? Lihat <b>Kendala Umum</b> atau hubungi owner."
)

# key -> (label tombol, isi)
GUIDE_TOPICS = {
    "mulai": ("🚀 Mulai Cepat", (
        "🚀 <b>MULAI CEPAT</b>\n\n"
        "Ada 3 jenis transaksi:\n\n"
        "🛒 <b>Beli</b>: kamu bayar Rupiah, kamu dapat koin crypto di wallet-mu.\n"
        "💸 <b>Jual</b>: kamu kirim koin crypto, kamu dapat Rupiah di rekening/e-wallet.\n"
        "🔄 <b>Convert</b>: kamu kirim satu koin, kamu dapat koin lain di wallet-mu.\n\n"
        "<b>Sebelum mulai, siapkan:</b>\n"
        "• Beli → alamat <b>wallet</b> penerima koin.\n"
        "• Jual → data <b>rekening bank / e-wallet</b> (Nama Bank, No Rekening, Atas Nama) dan koin di wallet-mu.\n"
        "• Convert → alamat <b>wallet tujuan</b> dan koin di wallet-mu.\n\n"
        "<b>3 aturan emas agar selalu berhasil:</b>\n"
        "1️⃣ <b>Jaringan harus sama.</b> Pilih jaringan (BSC, TRON, dll.) yang sama dengan yang kamu pakai di wallet.\n"
        "2️⃣ <b>Nominal harus persis</b> seperti yang tertulis di bot.\n"
        "3️⃣ <b>Salin-tempel</b> alamat dan hash, jangan diketik manual.\n\n"
        "Pilih topik di bawah untuk panduan lengkap tiap transaksi."
    )),
    "beli": ("🛒 Cara Beli", (
        "🛒 <b>CARA BELI CRYPTO</b>\n\n"
        "<b>Langkah 1 — Pilih koin</b>\n"
        "Menu utama → <b>Beli Crypto</b> → pilih koin → pilih <b>jaringan</b>.\n\n"
        "<b>Langkah 2 — Pilih cara isi jumlah</b>\n"
        "Ada dua tombol:\n"
        "🪙 <b>Jumlah Koin</b>: ketik berapa koin yang mau diterima, contoh <code>10</code> atau <code>0.5</code>.\n"
        "💵 <b>Nominal Rupiah</b>: ketik uang yang mau dibayar, contoh <code>50000</code>, <code>Rp 50.000</code>, atau <code>50k</code>.\n"
        "Batas pembelian: <b>Rp 5.000 sampai Rp 5.000.000</b>.\n\n"
        "<b>Langkah 3 — Cek simulasi</b>\n"
        "Bot menampilkan kurs, fee, dan koin yang kamu terima. Fee dipotong dari nominal yang kamu bayar.\n\n"
        "<b>Langkah 4 — Isi alamat wallet</b>\n"
        "Tempel alamat wallet penerima, atau pilih wallet tersimpan. Pastikan alamatnya untuk "
        "<b>jaringan yang sama</b>.\n\n"
        "<b>Langkah 5 — Pilih pembayaran</b>\n"
        "• <b>Saldo Bot</b>: langsung terpotong, instan.\n"
        "• <b>QRIS</b>: scan dengan e-wallet/m-banking apa saja.\n\n"
        "<b>Langkah 6 — Konfirmasi &amp; bayar</b>\n"
        "Cek ringkasan lalu tekan <b>Konfirmasi &amp; Bayar</b>. Untuk QRIS, bayar "
        "<b>persis sesuai total yang tampil</b> (sudah termasuk pajak QRIS dan angka kode unik di belakang, jangan dibulatkan) "
        f"sebelum waktunya habis ({settings.ORDER_EXPIRE_MINUTES} menit).\n"
        "Kode unik di belakang nominal dipakai untuk pengecekan pembayaran QRIS otomatis, "
        "jadi tetap bayar persis sesuai total.\n\n"
        "<b>Selesai!</b> Bot mengirim koin otomatis ke wallet-mu dan menampilkan TX Hash serta link explorer. "
        "Jika belum ada kabar setelah membayar, tekan <b>Cek Ulang</b>."
    )),
    "jual": ("💸 Cara Jual", (
        "💸 <b>CARA JUAL CRYPTO</b>\n\n"
        "<b>Langkah 1 — Pilih koin</b>\n"
        "Menu utama → <b>Jual Crypto</b> → pilih koin → pilih <b>jaringan</b> (samakan dengan wallet-mu).\n\n"
        "<b>Langkah 2 — Isi jumlah</b>\n"
        "🪙 <b>Jumlah Koin</b>: contoh <code>0.5</code> atau <code>10</code>.\n"
        "💵 <b>Nominal Rupiah</b>: contoh <code>Rp 50.000</code>. Nominal ini yang masuk ke rekeningmu; fee ditambahkan ke koin yang harus dikirim.\n"
        "Minimal nilai jual <b>Rp 5.000</b>.\n\n"
        "<b>Langkah 3 — Isi rekening penerima Rupiah</b>\n"
        "Ketik dengan format: <code>Nama Bank, No Rekening, Atas Nama</code>\n"
        "Contoh: <code>BCA, 882049281, Budi Santoso</code>\n"
        "E-wallet juga bisa: <code>GOPAY, 081234567890, Budi Santoso</code>\n"
        "Rekening yang sudah dipakai akun lain akan ditolak. Rekening kamu otomatis tersimpan untuk "
        "transaksi berikutnya.\n\n"
        "<b>Langkah 4 — Konfirmasi</b>\n"
        "Cek ringkasan, lalu tekan <b>Konfirmasi Jual</b>. Bot memberi <b>alamat hot wallet</b> dan "
        "<b>jumlah koin yang harus dikirim</b>.\n\n"
        "<b>Langkah 5 — Kirim koin</b>\n"
        "Dari wallet-mu, kirim <b>tepat jumlah itu</b> ke alamat tersebut, di <b>jaringan yang sama</b>. "
        "Kirim token yang dipilih (mis. USDT), bukan koin lain.\n\n"
        "<b>Langkah 6 — Kirim TX Hash (wajib!)</b>\n"
        "Setelah transfer, tekan <b>✍️ Kirim TX Hash</b>, lalu tempel hash transaksinya. "
        "Cara mendapatkannya ada di topik <b>Cara Dapat TX Hash</b>. Tanpa hash, pesananmu tidak diproses.\n\n"
        "<b>Langkah 7 — Tunggu</b>\n"
        "Bot memeriksa transaksimu di blockchain (biasanya kurang dari 1 menit). Kalau valid, admin "
        "mentransfer Rupiah ke rekeningmu pada jam layanan <b>08.00 – 23.59 WIB</b> (estimasi "
        f"maksimal <b>{quote_guard.SELL_PAYOUT_ETA_MINUTES} menit</b>; mohon tunggu). Kamu dapat "
        "notifikasi saat selesai."
    )),
    "convert": ("🔄 Cara Convert", (
        "🔄 <b>CARA CONVERT CRYPTO</b>\n\n"
        "Convert = tukar satu koin ke koin lain, hasilnya dikirim ke wallet-mu.\n\n"
        "<b>Langkah 1 — Pilih koin asal &amp; tujuan</b>\n"
        "Menu utama → <b>Convert Crypto</b> → pilih koin yang kamu <b>punya</b> + jaringannya → pilih koin "
        "yang kamu <b>mau</b> + jaringannya.\n\n"
        "<b>Langkah 2 — Isi jumlah</b>\n"
        "🪙 <b>Jumlah Koin</b> asal, contoh <code>20</code>.\n"
        "💵 <b>Nominal Rupiah</b>, contoh <code>Rp 100.000</code> (atau <code>$10</code> untuk dollar).\n\n"
        "<b>Langkah 3 — Cek simulasi</b>\n"
        "Bot menampilkan berapa yang kamu kirim, fee, dan berapa koin tujuan yang kamu terima. "
        f"Simulasi ini berlaku {quote_guard.QUOTE_MINUTES} menit; kalau terlewat, buat order baru.\n\n"
        "<b>Langkah 4 — Alamat wallet tujuan</b>\n"
        "Tempel alamat wallet yang akan menerima koin hasil convert. Alamatnya harus untuk "
        "<b>jaringan tujuan</b> (bukan jaringan asal).\n\n"
        "<b>Langkah 5 — Konfirmasi &amp; kirim koin</b>\n"
        "Tekan konfirmasi. Bot memberi alamat setoran dan <b>jumlah persis</b> yang harus dikirim. "
        "Kirim dari wallet-mu di jaringan asal.\n\n"
        "<b>Langkah 6 — Kirim TX Hash (wajib!)</b>\n"
        "Tekan tombol <b>Masukkan TX Hash</b> lalu tempel hash transfermu. Foto bukti tidak bisa "
        "diverifikasi, jadi harus hash.\n\n"
        "<b>Selesai!</b> Setelah hash terverifikasi, koin tujuan <b>dikirim otomatis</b> ke wallet-mu "
        "tanpa menunggu admin, lengkap dengan hash dan link explorer.\n\n"
        "⚠️ Jika bot bilang stok koin tujuan tidak cukup atau jaringan tujuan belum otomatis, "
        "<b>jangan kirim koin dulu</b>; hubungi owner."
    )),
    "hash": ("🔗 Cara Dapat TX Hash", (
        "🔗 <b>CARA DAPAT TX HASH</b>\n\n"
        "TX Hash (atau TxID) adalah \"nomor resi\" transfer crypto. Bot memakainya untuk memastikan "
        "koinmu benar-benar sudah masuk.\n\n"
        "<b>Dari wallet (Trust Wallet, MetaMask, dll.)</b>\n"
        "1. Buka koin yang kamu kirim → <b>Riwayat / Activity</b>.\n"
        "2. Tekan transaksi pengirimanmu yang terbaru.\n"
        "3. Cari <b>Transaction Hash / TxID / Hash</b> lalu tekan <b>salin</b>.\n\n"
        "<b>Dari exchange</b>\n"
        "Buka <b>Riwayat Penarikan (Withdraw)</b> → pilih transaksinya → salin <b>TxID</b>. "
        "TxID muncul beberapa menit setelah penarikan diproses.\n\n"
        "<b>Atau pakai link explorer</b>\n"
        "Kalau yang kamu dapat berupa link (bscscan, tronscan, solscan, dll.), kirim saja linknya. "
        "Bot mengambil hash-nya sendiri.\n\n"
        "<b>Bentuk hash yang benar</b>\n"
        "• BSC / ETH / Polygon / Base dll.: diawali <code>0x</code> + 64 karakter.\n"
        "• TRON: 64 karakter huruf &amp; angka.\n"
        "• Solana: deretan huruf &amp; angka panjang (sekitar 88 karakter).\n\n"
        "<b>Cara mengirim hash ke bot</b>\n"
        "• Di layar pesanan, tekan <b>Kirim TX Hash</b> lalu tempel.\n"
        "• Sudah menutup percakapan? Ketik <code>/txhash</code>, pilih pesananmu, tempel hash.\n\n"
        "💡 Kalau hash belum muncul di wallet, tunggu 1–2 menit lalu cek lagi. Sudah terkirim tapi bot "
        "bilang belum terkonfirmasi? Itu normal, bot terus memeriksa dan memberi kabar otomatis."
    )),
    "tips": ("✅ Tips Agar Berhasil", (
        "✅ <b>TIPS AGAR TRANSAKSI BERHASIL</b>\n\n"
        "<b>Sebelum kirim koin (Jual / Convert)</b>\n"
        "☑️ <b>Jaringan sama persis.</b> USDT di BSC beda dengan USDT di TRON. Salah jaringan = koin "
        "bisa hilang.\n"
        "☑️ <b>Token yang benar.</b> Kirim token yang kamu pilih (mis. USDT), bukan koin gas "
        "(BNB/ETH/POL). Koin gas tidak bisa diverifikasi otomatis.\n"
        "☑️ <b>Nominal persis.</b> Jumlah yang <b>sampai</b> ke hot wallet harus sama dengan yang "
        "diminta bot. Kurang → ditolak. Lebih sedikit → dicek admin dulu (lebih lama).\n"
        "☑️ <b>Alamat benar.</b> Salin dari tombol <b>Salin Alamat</b> atau QR di bot.\n\n"
        "<b>Kalau kirim dari exchange</b>\n"
        "Exchange biasanya memotong biaya penarikan dari jumlah yang kamu tarik. Naikkan jumlah "
        "penarikan supaya yang <b>diterima</b> tetap persis seperti yang diminta bot.\n\n"
        "<b>Setelah kirim</b>\n"
        "☑️ <b>Langsung kirim TX Hash</b> ke bot.\n"
        "☑️ Simpan <b>Order ID</b> pesananmu (cek di <b>Riwayat Transaksi</b>).\n"
        "☑️ Jangan buat pesanan ganda untuk transaksi yang sama.\n\n"
        "<b>Waktu penting</b>\n"
        f"• QRIS Beli: bayar dalam <b>{settings.ORDER_EXPIRE_MINUTES} menit</b>.\n"
        f"• Order Jual/Convert berlaku <b>{quote_guard.QUOTE_MINUTES} menit</b>; terlambat = buat order baru. "
        "Koin yang terlanjur dikirim telat tetap aman (maks. <b>24 jam</b>), tapi dicek admin dan "
        "dibayar sesuai harga terkini.\n"
        "• Rupiah Jual dicairkan pada jam layanan <b>08.00 – 23.59 WIB</b>.\n\n"
        "<b>Rekening &amp; wallet terkunci</b>\n"
        "Setelah transaksi sukses, rekening/wallet yang dipakai terkunci ke akun Telegram-mu. "
        "Gunakan yang utama."
    )),
    "biaya": ("💰 Fee, Batas & Harga", (
        "💰 <b>FEE, BATAS &amp; HARGA</b>\n\n"
        "<b>Batas transaksi</b>\n"
        "• Minimal <b>Rp 5.000</b> (untuk ETH di jaringan ETH, TRX di TRON, serta USDT/USDC di jaringan "
        "ETH, minimal <b>Rp 7.500</b>).\n"
        "• Maksimal <b>Rp 5.000.000</b> per transaksi. Di atas itu, hubungi owner.\n\n"
        "<b>Fee layanan</b>\n"
        "Fee mengikuti tier nominal. Semua biaya (fee, tambahan gas bila ada, dan pajak QRIS bila bayar "
        "lewat QRIS) ditampilkan di simulasi/ringkasan <b>sebelum</b> kamu konfirmasi.\n"
        "• <b>Beli</b>: fee dipotong dari nominal yang kamu bayar.\n"
        "• <b>Jual</b>: Rupiah yang kamu terima = nilai koin dikurangi fee.\n"
        "• <b>Convert</b>: fee dihitung dari nilai koin yang dikirim.\n\n"
        "<b>Harga</b>\n"
        "Memakai harga pasar terkini. Jika harga bergerak lebih dari 0,5% sejak simulasi dibuat, "
        "bot meminta kamu mengulang supaya adil untuk kedua pihak.\n\n"
        "<b>Cek dulu sebelum transaksi</b>\n"
        "• <b>Cek Harga</b>: harga terbaru tiap koin.\n"
        "• <b>Cek Stok</b>: koin yang tersedia untuk Beli dan Convert.\n\n"
        "💡 Biaya jaringan (gas) untuk mengirim koin dari wallet-mu dibayar oleh wallet-mu sendiri, "
        "terpisah dari fee layanan."
    )),
    "masalah": ("❓ Kendala Umum", (
        "❓ <b>KENDALA UMUM &amp; SOLUSINYA</b>\n\n"
        "<b>\"Nominal deposit kurang\"</b>\n"
        "Koin yang masuk kurang dari yang diminta (sering karena fee exchange terpotong). Hubungi owner "
        "dengan Order ID dan TX Hash.\n\n"
        "<b>\"Menunggu konfirmasi jaringan\"</b>\n"
        "Normal. Transaksimu belum cukup dikonfirmasi blockchain. Tunggu; bot memberi kabar otomatis.\n\n"
        "<b>\"Deposit sedang dicek admin\"</b>\n"
        "Transaksimu terbaca, tapi perlu dicocokkan manual (mis. ada nominal yang sama dengan pesanan "
        "lain). Cukup tunggu, kamu akan dapat notifikasi.\n\n"
        "<b>\"TX Hash sudah dipakai order lain\"</b>\n"
        "Satu transaksi hanya untuk satu pesanan. Pastikan kamu menempel hash transaksi yang benar.\n\n"
        "<b>Keluar dari percakapan, belum kirim hash</b>\n"
        "Ketik <code>/txhash</code> lalu pilih pesananmu.\n\n"
        "<b>QRIS kedaluwarsa</b>\n"
        "Buat pesanan baru. Kalau kamu <b>sudah terlanjur bayar</b>, jangan bayar lagi; hubungi owner "
        "dengan bukti pembayaran dan Order ID.\n\n"
        "<b>\"Harga pasar sudah berubah\"</b>\n"
        "Ulangi transaksi untuk mendapat harga terbaru.\n\n"
        "<b>Salah jaringan / salah alamat</b>\n"
        "Segera hubungi owner. Makin cepat dilaporkan, makin besar peluang dibantu.\n\n"
        "<b>Cek status pesanan</b>\n"
        "Menu utama → <b>Riwayat Transaksi</b>.\n\n"
        "Tidak ketemu solusinya? Tekan <b>Hubungi Owner</b> di bawah dan sertakan <b>Order ID</b> serta "
        "<b>TX Hash</b>."
    )),
}

_BACK_TO_INDEX = InlineKeyboardButton("📖 Daftar Panduan", callback_data="menu_guide")
_BACK_TO_MENU = InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_back")


def guide_index():
    """(teks, keyboard) halaman daftar topik panduan user."""
    buttons = [InlineKeyboardButton(label, callback_data=f"guide_{key}")
               for key, (label, _text) in GUIDE_TOPICS.items()]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_BACK_TO_MENU])
    rows.append([get_owner_button()])
    return GUIDE_INDEX_TEXT, InlineKeyboardMarkup(rows)


def guide_topic(key: str):
    """(teks, keyboard) satu topik, atau None bila key tidak dikenal."""
    topic = GUIDE_TOPICS.get(key)
    if topic is None:
        return None
    keys = list(GUIDE_TOPICS)
    i = keys.index(key)
    nav = []
    if i > 0:
        nav.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"guide_{keys[i - 1]}"))
    if i < len(keys) - 1:
        nav.append(InlineKeyboardButton("Berikutnya ➡️", callback_data=f"guide_{keys[i + 1]}"))
    rows = [nav] if nav else []
    rows.append([_BACK_TO_INDEX, _BACK_TO_MENU])
    if key in ("masalah", "hash"):
        rows.append([get_owner_button()])
    return topic[1], InlineKeyboardMarkup(rows)
