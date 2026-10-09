"""
bot/utils/admin_guide.py — Panduan fitur admin (tombol "📖 Panduan Admin").
==========================================================================
Teks dibagi per topik agar tiap pesan < 4096 karakter (batas Telegram). Semua perintah yang
disebut di dalam <code>/perintah</code> dicek oleh tes terhadap handler yang benar-benar
terdaftar, jadi panduan tidak bisa menyebut perintah yang sudah tidak ada.
Gunakan &lt; &gt; untuk tanda kurung sudut (HTML).
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

GUIDE_INDEX_TEXT = (
    "📖 <b>PANDUAN ADMIN</b>\n\n"
    "Pilih topik di bawah untuk melihat penjelasan fitur dan cara memakainya langkah demi langkah.\n\n"
    "💡 <b>Tips cepat</b>\n"
    "• Ketik <code>/</code> untuk melihat semua perintah admin (menu ☰).\n"
    "• <code>/admin</code> membuka dashboard dengan semua tombol.\n"
    "• Kirim perintah sebagai pesan <b>baru</b>; perintah yang diedit diabaikan bot.\n"
    "• Menu ☰ tidak muncul? Kirim <code>/refreshmenu</code>.\n"
    "• Member bingung cara transaksi? Arahkan ke tombol <b>📖 Panduan Transaksi</b> di menu utama "
    "atau perintah <code>/bantuan</code>."
)

# key -> (label tombol, isi)
GUIDE_TOPICS = {
    "alur": ("🔁 Alur Transaksi", (
        "🔁 <b>ALUR TRANSAKSI (RINGKAS)</b>\n\n"
        "🛒 <b>Beli</b>\n"
        "User pilih koin &amp; jaringan → pilih <b>Jumlah Koin</b> atau <b>Nominal Rupiah</b> → isi wallet → "
        "bayar QRIS. Kode unik hanya ada di nominal <b>Rupiah</b> QRIS. Setelah bayar terverifikasi, "
        "bot mengirim koin <b>otomatis</b>.\n\n"
        "💸 <b>Jual</b>\n"
        "User pilih koin → pilih Jumlah Koin / Nominal Rupiah → isi rekening → kirim koin <b>persis sesuai "
        "nominal</b> ke hot wallet (tanpa kode unik) → <b>wajib kirim TX Hash</b> → bot cek on-chain → bila "
        "valid, <b>admin dikabari untuk transfer Rupiah</b>.\n\n"
        "🔄 <b>Convert</b>\n"
        "Sama seperti Jual sampai hash terverifikasi, lalu bot mengirim koin tujuan <b>otomatis</b> ke wallet "
        "user. Bila stok kurang atau pengiriman gagal, order masuk antrean dan admin dikabari.\n\n"
        "📣 Setiap order selesai, bot memposting <b>testimoni</b> ke channel secara otomatis.\n\n"
        "⏰ Jam layanan pencairan Jual: 08.00 – 23.59 WIB (diproses manual oleh admin)."
    )),
    "jual": ("💸 Order Jual", (
        "💸 <b>ORDER JUAL — TRANSFER RUPIAH KE USER</b>\n\n"
        "<b>Kapan admin dikabari?</b>\n"
        "Hanya setelah TX Hash user <b>terverifikasi on-chain</b> (wallet tujuan, nominal, waktu, belum "
        "dipakai order lain). Pesannya: <b>DEPOSIT CRYPTO TERVERIFIKASI (SELL)</b> berisi order, user, "
        "nominal, <b>alamat pengirim</b>, TX Hash, jumlah Rupiah, dan rekening tujuan.\n\n"
        "<b>Langkah admin</b>\n"
        "1. Baca pesan, cek <b>Pengirim</b> bukan wallet Anda sendiri.\n"
        "2. Transfer Rupiah sesuai jumlah ke rekening/e-wallet yang tertera.\n"
        "3. Tekan <b>✅ Sudah Ditransfer</b> (atau <code>/confirm ORDER_ID</code>). User otomatis diberi notifikasi.\n"
        "4. Opsional: <b>📸 Upload Bukti Transfer</b> agar bukti ikut terkirim ke user.\n\n"
        "<b>🕵️ HASH DEPOSIT PERLU DICEK MANUAL</b>\n"
        "Muncul bila hash valid tetapi pemiliknya tidak bisa dipastikan otomatis (mis. ada dua order bernominal "
        "sama, nominal tidak persis, atau pengirimnya wallet owner). Baca <i>Alasan</i>, cek pengirim di explorer, "
        "lalu tekan <b>✅ Konfirmasi Deposit (sudah dicek)</b> bila memang milik user tersebut.\n\n"
        "<b>Cek ulang manual</b>: <code>/verifysell ORDER_ID</code> memverifikasi ulang deposit order jual.\n"
        "<b>Dashboard antrean</b>: <code>/sellorders</code> (atau tombol 📥 Dashboard Jual Crypto).\n\n"
        "⚠️ <b>Jangan transfer Rupiah</b> sebelum ada notifikasi <i>TERVERIFIKASI</i>. Foto bukti dari user "
        "bukan bukti on-chain."
    )),
    "convert": ("🔄 Convert & Stok", (
        "🔄 <b>CONVERT &amp; STOK</b>\n\n"
        "<b>Otomatis</b>\n"
        "Hash terverifikasi → bot mengirim koin tujuan ke wallet user → order <b>COMPLETED</b> → user dan "
        "admin dikabari → testimoni diposting.\n\n"
        "<b>Bila gagal / stok kurang</b>\n"
        "Order masuk <b>PAYOUT_QUEUED</b> dan admin dikabari. Kirim koin manual dari wallet Anda ke wallet "
        "tujuan user, lalu <code>/confirm ORDER_ID</code>. Jangan kirim ulang sebelum memeriksa receipt "
        "on-chain (bot sengaja tidak mengirim ulang otomatis agar tidak dobel).\n\n"
        "<b>Deposit telat setelah quote habis</b>\n"
        "Bot meminta admin cek kurs dulu (tombol <b>✅ Proses Convert (sudah dicek)</b>).\n\n"
        "<b>Stok &amp; wallet</b>\n"
        "• <code>/refreshwallet</code> atau 🔄 Sync On-Chain: sinkron saldo hot wallet.\n"
        "• 💼 Hot Wallets &amp; Saldo: lihat stok per koin/jaringan.\n"
        "• Isi ulang stok memakai wallet owner, lalu isi <code>OWNER_WALLET_ADDRESSES</code> di .env agar "
        "transfer isi ulang tidak pernah dianggap deposit user."
    )),
    "beli": ("🛒 Order Beli", (
        "🛒 <b>ORDER BELI</b>\n\n"
        "• Batas pembelian <b>Rp 5.000 – Rp 5.000.000</b>.\n"
        "• Fee dipotong dari nominal: koin diterima = (nominal − fee) ÷ kurs beli. Mode <b>Jumlah Koin</b> "
        "menghitung terbalik nominal Rupiah terkecil yang cukup.\n"
        "• Pembayaran QRIS dinamis; kode unik 1–400 ditambahkan ke total Rupiah.\n"
        "• Pembayaran terverifikasi → koin dikirim otomatis dari hot wallet.\n\n"
        "<b>Bila koin gagal terkirim otomatis</b>\n"
        "Order masuk antrean review dan admin dikabari. Kirim koin manual, lalu "
        "<code>/confirm ORDER_ID</code> agar status jadi selesai dan user mendapat notifikasi.\n\n"
        "<b>Memantau</b>\n"
        "• <code>/orders</code>: order <i>pending/paid</i> yang butuh review.\n"
        "• 📥 Antrean Order di dashboard.\n"
        "• 📜 Audit Trail Log: riwayat perubahan status tiap order.\n\n"
        "ℹ️ Stok yang tidak cukup akan menolak order di sisi user sebelum pembayaran."
    )),
    "saldo": ("💳 Saldo & Kas Bot", (
        "💳 <b>SALDO USER &amp; KAS BOT</b>\n\n"
        "<b>Kirim saldo ke user</b>\n"
        "• <code>/credit TELEGRAM_ID JUMLAH [keterangan]</code>\n"
        "  Contoh: <code>/credit 123456789 10000 Giveaway Oktober</code>\n"
        "• <code>/bulkcredit JUMLAH ID1 ID2 ID3 ...</code> untuk banyak user sekaligus.\n"
        "• Atau tombol 💳 Kirim Saldo User di dashboard (ikuti langkah di layar).\n"
        "Saldo bisa dipakai user untuk transaksi atau ditarik ke rekening/e-wallet (minimal Rp 10.000).\n\n"
        "<b>Kas Bot</b> (sumber saldo yang Anda bagikan)\n"
        "• <code>/topupbot NOMINAL</code>: lihat &amp; isi kas bot.\n"
        "• <code>/topupqris NOMINAL</code>: buat invoice QRIS kas bot langsung di chat/grup.\n"
        "• 🏦 Dompet &amp; Kas Bot: ringkasan kas dan riwayat.\n\n"
        "⚠️ Kredit saldo bersifat langsung dan tercatat di audit log. Periksa ID dan nominal sebelum mengirim."
    )),
    "user": ("👥 User & Broadcast", (
        "👥 <b>KELOLA USER &amp; BROADCAST</b>\n\n"
        "<b>Blokir</b>\n"
        "• <code>/ban TELEGRAM_ID</code>: user tidak bisa bertransaksi.\n"
        "• <code>/unban TELEGRAM_ID</code>: buka blokir.\n"
        "• 👥 Kelola User di dashboard.\n\n"
        "<b>Broadcast</b>\n"
        "• Teks: <code>/broadcast Halo member...</code>\n"
        "• Segmen: tambahkan <code>--all</code>, <code>--active</code>, <code>--buyers</code>, atau "
        "<code>--balance</code> di depan teks.\n"
        "• Koin ready: <code>/broadcast --ready Base</code> (format otomatis).\n"
        "• Foto: kirim poster dengan caption diawali <code>/broadcast ...</code>, atau balas foto dengan "
        "<code>/broadcast ...</code>.\n\n"
        "💡 Kirim perintah sebagai pesan <b>baru</b>. Mengedit pesan perintah tidak menjalankan ulang "
        "(sengaja, agar broadcast tidak terkirim dobel)."
    )),
    "reward": ("🎁 Campaign & Referral", (
        "🎁 <b>CAMPAIGN, REWARD &amp; REFERRAL</b>\n\n"
        "<b>Campaign / Giveaway / Loyalty</b>\n"
        "<code>/campaign</code> (alias <code>/giveaway</code>) atau 🎁 Pusat Campaign di dashboard: "
        "buat campaign, undi pemenang, atur loyalty. Ikuti wizard di layar.\n\n"
        "<b>Kirim Reward ke User Pilihan</b>\n"
        "Tombol 🎁 Kirim Reward: ketik daftar ID penerima lalu pesan, bot mengirim sesuai wizard. Daftar "
        "<b>Top Milestone</b> bisa mengecualikan user tertentu (mis. admin channel).\n\n"
        "<b>Program Referral</b>\n"
        "• Pengundang: reward Rp 1.000 (transaksi ke-1 teman), Rp 500 (ke-2), plus 7% dari fee tiap "
        "transaksi teman (maks. 10 transaksi/teman). Teman: diskon fee Rp 1.000 di transaksi pertamanya.\n"
        "• Reward pengundang masuk saldo bot setelah masa tahan 24 jam; diskon teman masuk saat transaksi selesai.\n"
        "• Semua angka bisa diubah lewat 🔗 Referral Program ➔ tombol pengaturan, atau "
        "<code>/setreferral NAMA NILAI</code> (reward, reward2, bonus, share, sharemax, hold, min, max, enabled).\n"
        "• Total pembayaran per transaksi dibatasi sebesar fee (anti akun palsu); "
        "<code>referral_fee_guard=false</code> mematikan batas itu.\n"
        "• Sweeper otomatis (tiap 60 detik) menghitung transaksi yang terlewat, merilis reward yang masa tahannya "
        "habis, dan memberi notifikasi.\n\n"
        "Pantau lewat 🔗 Referral Program di dashboard."
    )),
    "harga": ("⚙️ Harga & Spread", (
        "⚙️ <b>HARGA, SPREAD &amp; STATUS API</b>\n\n"
        "<b>Sumber harga</b>\n"
        "OKX × kurs USD/IDR. Koin yang tidak ada di OKX (mis. TON) diisi otomatis dari CoinPaprika, "
        "CoinMarketCap, lalu CoinGecko. Bila semua sumber gagal, harga koin tampil <code>-</code> dan "
        "tercatat di log.\n\n"
        "<b>Spread</b>\n"
        "<code>/setspread KOIN PERSEN</code>, contoh <code>/setspread USDT 1.5</code>. Catatan: bila "
        "<code>DEFAULT_SPREAD_PCT</code> di .env bernilai 0, bot memakai <b>harga pasar murni</b> dan "
        "mengabaikan spread yang diatur di sini.\n\n"
        "<b>Status sistem</b>\n"
        "• <code>/checkapi</code> (alias <code>/cekurl</code>): cek semua API/RPC koin.\n"
        "• 📡 Status API &amp; RPC di dashboard. Jaringan non-EVM punya beberapa RPC cadangan yang "
        "berpindah otomatis bila satu mati.\n"
        "• <code>/refreshwallet</code>: sinkron saldo on-chain.\n\n"
        "<b>Laporan</b>\n"
        "• <code>/stats</code> atau 📊 Statistik &amp; Volume.\n"
        "• <code>/report</code> (alias <code>/weeklyreport</code>): rekap mingguan + CSV."
    )),
    "testi": ("📣 Testimoni Channel", (
        "📣 <b>TESTIMONI CHANNEL</b>\n\n"
        "Setiap order <b>Beli, Jual, dan Convert</b> yang selesai otomatis diposting ke channel testimoni "
        "(default <code>@TokoKoinID</code>, ubah lewat <code>TESTIMONY_CHANNEL</code> di .env). Username user "
        "disensor. Format Convert menampilkan jaringan asal dan tujuan, contoh <i>USDT (BSC) → ETH (BASE)</i>.\n\n"
        "<b>Syarat</b>\n"
        "Bot harus <b>admin channel</b> dengan izin <b>Post Messages</b>.\n\n"
        "<b>Perintah</b>\n"
        "• <code>/testtesti</code>: kirim pesan contoh untuk menguji koneksi. Gagal? Pesan error asli "
        "dari Telegram ditampilkan.\n"
        "• <code>/posttesti ORDER_ID</code>: posting ulang satu order (mis. yang terlewat).\n"
        "• <code>/postlasttesti [jumlah]</code>: posting order selesai terakhir (maks. 20; default 5).\n\n"
        "💡 Satu order hanya diposting sekali per proses bot. Bila ada yang tidak muncul, cari baris log "
        "<code>Gagal mengirim testimoni</code> beserta alasannya."
    )),
    "notif": ("🔔 Notifikasi & Topik", (
        "🔔 <b>NOTIFIKASI &amp; TOPIK GRUP</b>\n\n"
        "Secara bawaan notifikasi order dikirim ke DM semua admin. Anda bisa mengarahkannya ke topik grup "
        "terpisah per jenis.\n\n"
        "<b>Cara memasang</b>\n"
        "1. Masuk ke topik grup yang diinginkan.\n"
        "2. Ketik <code>/chatid</code> untuk melihat ID chat &amp; thread.\n"
        "3. Ketik <code>/settarget JENIS</code> di dalam topik itu.\n"
        "4. Cek hasilnya dengan <code>/targets</code>.\n"
        "5. Lepas dengan <code>/unsettarget JENIS</code> (kembali ke DM).\n\n"
        "<b>Jenis notifikasi</b>\n"
        "<code>beli</code>, <code>jual</code>, <code>convert</code>, <code>error</code>, <code>alarm</code>, "
        "<code>topup</code>, <code>ops</code>.\n\n"
        "💡 Notifikasi Jual (transfer Rupiah) sebaiknya diarahkan ke topik <code>jual</code> yang dipantau "
        "tim pencairan."
    )),
    "cmd": ("📋 Daftar Perintah", (
        "📋 <b>DAFTAR PERINTAH ADMIN</b>\n\n"
        "<b>Order</b>\n"
        "<code>/admin</code> dashboard · <code>/orders</code> antrean · <code>/sellorders</code> dashboard jual · "
        "<code>/confirm ORDER_ID</code> selesaikan · <code>/verifysell ORDER_ID</code> cek deposit jual\n\n"
        "<b>Laporan &amp; sistem</b>\n"
        "<code>/stats</code> · <code>/report</code> · <code>/refreshwallet</code> · <code>/checkapi</code> · "
        "<code>/setspread KOIN PERSEN</code>\n\n"
        "<b>Uang</b>\n"
        "<code>/credit</code> · <code>/bulkcredit</code> · <code>/topupbot</code> · <code>/topupqris</code>\n\n"
        "<b>Program</b>\n"
        "<code>/campaign</code> · <code>/setreferral</code> · <code>/broadcast</code>\n\n"
        "<b>User</b>\n"
        "<code>/ban ID</code> · <code>/unban ID</code>\n\n"
        "<b>Testimoni</b>\n"
        "<code>/testtesti</code> · <code>/posttesti ORDER_ID</code> · <code>/postlasttesti</code>\n\n"
        "<b>Notifikasi</b>\n"
        "<code>/chatid</code> · <code>/settarget</code> · <code>/unsettarget</code> · <code>/targets</code>\n\n"
        "<b>Lainnya</b>\n"
        "<code>/refreshmenu</code> pasang ulang menu ☰ · <code>/txhash</code> kirim hash deposit · "
        "<code>/panduan</code> panduan ini · <code>/bantuan</code> panduan transaksi versi user\n\n"
        "<b>Emoji kustom</b>\n"
        "<code>/getemoji</code> · <code>/syncpack</code> · <code>/setemoji</code> · "
        "<code>/listemojis</code> · <code>/resetemojis</code>"
    )),
    "tips": ("🛠 Troubleshooting", (
        "🛠 <b>TROUBLESHOOTING</b>\n\n"
        "<b>Menu ☰ admin tidak muncul</b>\n"
        "Kirim <code>/refreshmenu</code> dan baca hasilnya per chat. Lalu tutup &amp; buka lagi chat bot. "
        "Chat berstatus ❌ biasanya karena admin belum pernah <code>/start</code> ke bot.\n\n"
        "<b>Perintah tidak merespons setelah diedit</b>\n"
        "Memang diabaikan. Kirim ulang sebagai pesan baru.\n\n"
        "<b>Testimoni tidak masuk channel</b>\n"
        "Cek bot admin channel (Post Messages), jalankan <code>/testtesti</code>, lalu cari log "
        "<code>Gagal mengirim testimoni</code>.\n\n"
        "<b>Harga koin tampil <code>-</code></b>\n"
        "Semua sumber harga gagal untuk koin itu. Cek <code>/checkapi</code> dan log "
        "<code>Harga ... tidak ditemukan</code>.\n\n"
        "<b>Deposit Jual tidak terdeteksi</b>\n"
        "User wajib mengirim TX Hash (<code>/txhash</code> bila keluar dari percakapan). Tanpa hash order "
        "tidak diproses; auto-scan riwayat wallet mati secara bawaan "
        "(<code>DEPOSIT_AUTOSCAN_ENABLED=false</code>). Hash ditolak? Alasannya ditampilkan ke user.\n\n"
        "<b>Setelan penting di .env</b>\n"
        "<code>OWNER_WALLET_ADDRESSES</code> (wallet owner untuk isi ulang stok) · "
        "<code>TESTIMONY_CHANNEL</code> · <code>SELL_DEPOSIT_WINDOW_MINUTES</code> (jendela verifikasi "
        "deposit, bawaan 1440 menit = 24 jam) · <code>DEFAULT_SPREAD_PCT</code>."
    )),
}

_BACK_TO_INDEX = InlineKeyboardButton("📖 Daftar Panduan", callback_data="admin_panel_guide")
_BACK_TO_DASHBOARD = InlineKeyboardButton("🏠 Dashboard Utama", callback_data="admin_panel_main")


def guide_index():
    """(teks, keyboard) halaman daftar topik panduan."""
    buttons = [InlineKeyboardButton(label, callback_data=f"admin_panel_guide_{key}")
               for key, (label, _text) in GUIDE_TOPICS.items()]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_BACK_TO_DASHBOARD])
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
        nav.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"admin_panel_guide_{keys[i - 1]}"))
    if i < len(keys) - 1:
        nav.append(InlineKeyboardButton("Berikutnya ➡️", callback_data=f"admin_panel_guide_{keys[i + 1]}"))
    rows = [nav] if nav else []
    rows.append([_BACK_TO_INDEX, _BACK_TO_DASHBOARD])
    return topic[1], InlineKeyboardMarkup(rows)
