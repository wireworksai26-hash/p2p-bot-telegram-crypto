# PLAN — Phase 4: UI/UX & Brand Text Improvements (TokoKoin ID Polish)

> **Phase:** 4 (UI/UX Bot Improvements)
> **Branch:** `main`
> **Dependencies:** None
> **Target Files:**
> - `main.py`
> - `bot/handlers/buy.py`
> - `bot/handlers/sell.py`
> - `bot/handlers/start.py`
> - `bot/handlers/history.py`
> - `bot/utils/messages.py`
> - `tests/test_command_menu.py`
> - `tests/test_admin_command_menu.py`

---

## 1. Ringkasan Kebutuhan User

1. **Menu Tombol ☰ (Icon Strip 3) untuk User:**
   - Tambahkan perintah `/beli`, `/jual`, dan `/convert` ke daftar default command menu Telegram (`set_my_commands`).
   - Pastikan handler `/beli` dan `/jual` dapat dieksekusi via teks perintah langsung atau tombol popup.

2. **Pembersihan Emoji Berlebih di Riwayat Transaksi (Gambar 2):**
   - Hapus emoji dekoratif baris detail: `🪙 Aset:` → `Aset:`, `💼 Total:` → `Total:`, `🚦 Status:` → `Status:`, `📅 Waktu:` → `Waktu:`.
   - Sisakan emoji uang `💸` di samping jenis transaksi `JUAL` (dan `🛒` di samping `BELI`).

3. **Penyesuaian Kalimat Ajakan Transaksi (Gambar 3):**
   - Ganti teks `Silakan gunakan menu di bawah untuk memulai transaksi:` menjadi `Silahkan pilih menu di bawah untuk memulai transaksi:`.

4. **Rebranding TokoKoin ID & Badge Terverifikasi (Gambar 4):**
   - Ganti teks sambutan selamat datang menjadi:
     `Selamat Datang di <b>TokoKoin ID</b> ☑️, Platform Jual Beli Koin terpercaya di Telegram.`
   - Sertakan emoji centang biru terverifikasi (`☑️` / badge custom terpercaya).

---

## 2. Rincian Pekerjaan Teknis (Task Breakdown)

### Task 4.1 — Command Menu User (`/beli`, `/jual`, `/convert`)
- **File:** `main.py`
  - Perbarui `BOT_COMMAND_MENU`:
    ```python
    BOT_COMMAND_MENU = [
        ("start", "Menu utama"),
        ("beli", "Beli crypto"),
        ("jual", "Jual crypto"),
        ("convert", "Convert / Swap crypto"),
        ("cancel", "Membatalkan transaksi"),
    ]
    ```
- **File:** `bot/handlers/buy.py`
  - Tambahkan alias command `beli` pada entry points:
    ```python
    CommandHandler(["buy", "beli"], start_buy_command)
    ```
- **File:** `bot/handlers/sell.py`
  - Tambahkan alias command `jual` pada entry points:
    ```python
    CommandHandler(["sell", "jual"], start_sell_command)
    ```
- **File:** `tests/test_command_menu.py` & `tests/test_admin_command_menu.py`
  - Sesuaikan ekspektasi test dengan daftar menu command baru.

### Task 4.2 — Penyederhanaan Emoji Riwayat Transaksi
- **File:** `bot/handlers/history.py`
  - Ubah format item riwayat transaksi pada `show_history()`:
    ```python
    text_lines.append(
        f"{idx}. <b>{order_type_str} | {order.order_id}</b>\n"
        f"   Aset: <code>{crypto_str} ({order.network})</code>\n"
        f"   Total: <code>{format_idr(order.total_idr)}</code>\n"
        f"   Status: <b>{status_str}</b>\n"
        f"   Waktu: <i>{date_str}</i>\n"
    )
    ```
  - Menghilangkan `E_COIN()`, `E_CARD()`, `🚦`, dan `E_CALENDAR()`, serta mempertahankan `{E_DOLLAR()} JUAL` dan `{E_CART()} BELI`.

### Task 4.3 — Update Kalimat Menu Transaksi
- **File:** `bot/handlers/start.py` (baris 67 di `build_welcome_message`):
  - Ubah menjadi `f"Silahkan pilih menu di bawah untuk memulai transaksi:"`.
- **File:** `bot/utils/messages.py`:
  - Sinkronkan `WELCOME_MESSAGE` agar menggunakan kalimat yang identik.

### Task 4.4 — Rebranding Welcome Message & Centang Biru
- **File:** `bot/handlers/start.py` (baris 60 di `build_welcome_message`):
  - Ubah menjadi:
    `f"Selamat Datang di <b>TokoKoin ID</b> ☑️, Platform Jual Beli Koin terpercaya di Telegram.\n\n"`
- **File:** `bot/utils/messages.py`:
  - Sinkronkan pada konstanta `WELCOME_MESSAGE`.

---

## 3. Rencana Verifikasi (Testing Plan)

1. **Unit Test Command Menu:**
   - Jalankan `python -m pytest tests/test_command_menu.py tests/test_admin_command_menu.py` untuk memastikan menu bot dan admin terdaftar presisi.
2. **Smoke Test Format Pesan:**
   - Pastikan fungsi `build_welcome_message()` dan `show_history()` merender teks HTML yang valid tanpa tag corrupt.
3. **Regression Test Suite:**
   - Jalankan test suite buy, sell, swap untuk memastikan alur conversation handler tidak terganggu oleh alias command baru.
