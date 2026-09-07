"""
bot/handlers/admin.py — Handler Panel Administrator.
===================================================
Berisi perintah dan kontrol administratif khusus untuk owner/admin bot.
Termasuk broadcast, statistik, set spread, un/ban, list pending order, dan konfirmasi order.
"""

import logging
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from config.settings import settings
from database.connection import SessionLocal
from database.models import User, Order
from database import crud
from services.crypto_sender import CryptoSenderFactory
from bot.utils.formatter import format_idr, format_crypto
from bot.utils.emojis import (
    CUSTOM_EMOJI_IDS,
    CUSTOM_EMOJI_ALTS,
    set_custom_emoji,
    reset_custom_emojis,
    sync_from_stickers,
    tg_emoji,
)

logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    """Mengecek apakah user_id terdaftar dalam ADMIN_CHAT_IDS."""
    return user_id in settings.ADMIN_CHAT_IDS


async def admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tampilkan menu bantuan admin."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Anda tidak memiliki akses ke menu administrator.")
        return

    admin_text = (
        "👑 <b>ADMIN PANEL — P2P CRYPTO BOT</b>\n\n"
        "Gunakan perintah-perintah di bawah ini untuk mengelola bot:\n\n"
        "📊 <b>Statistik & Wallet:</b>\n"
        "• /stats — Statistik harian & volume transaksi\n"
        "• /refreshwallet — Sinkronisasi saldo on-chain sekarang\n\n"
        "🛒 <b>Manajemen Order:</b>\n"
        "• /orders — Daftar seluruh order pending/paid aktif\n"
        "• /confirm <code>[ORDER_ID]</code> — Konfirmasi penyelesaian order manual\n\n"
        "🎨 <b>Otomatisasi Emoji Animasi 3D:</b>\n"
        "• /syncpack <code>[URL/NAMA_PACK]</code> — Sinkronisasi 1 pack emoji otomatis\n"
        "• /setemoji <code>[KEY] [EMOJI]</code> — Set/ganti custom emoji langsung\n"
        "• /listemojis — Lihat seluruh custom emoji aktif & preview\n"
        "• /getemoji <code>[EMOJI]</code> — Deteksi custom_emoji_id dari pesan\n"
        "• /resetemojis — Reset emoji ke default bawaan Telegram\n\n"
        "⚙️ <b>Sistem & Pengguna:</b>\n"
        "• /setspread <code>[SYMBOL] [PERCENT]</code> — Set spread koin (e.g. <code>/setspread USDT 1.2</code>)\n"
        "• /broadcast <code>[PESAN]</code> — Kirim siaran pesan ke semua pengguna\n"
        "• /ban <code>[USER_ID]</code> — Blokir pengguna dari bot\n"
        "• /unban <code>[USER_ID]</code> — Buka blokir pengguna\n"
    )
    await update.message.reply_text(admin_text, parse_mode="HTML")


async def syncpack_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Otomatisasi: Sinkronisasi satu set emoji pack Telegram ke bot secara instan.
    Format: /syncpack [NAMA_PACK_ATAU_URL]
    Contoh: /syncpack Crypto3DEmoji
            /syncpack https://t.me/addemoji/Crypto3DEmoji
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    msg = update.effective_message
    if not msg:
        return

    args = context.args or []
    if not args:
        await msg.reply_text(
            "⚡️ <b>OTOMASI SINKRONISASI EMOJI PACK</b>\n\n"
            "Kirimkan tautan atau nama paket emoji Telegram Anda:\n\n"
            "<b>Format:</b> <code>/syncpack [NAMA_PACK_ATAU_URL]</code>\n"
            "<b>Contoh:</b>\n"
            "• <code>/syncpack Crypto3DAnimated</code>\n"
            "• <code>/syncpack https://t.me/addemoji/Crypto3DAnimated</code>\n\n"
            "<i>Bot akan otomatis menarik seluruh custom_emoji_id dari pack tersebut dan langsung mengaktifkannya di bot tanpa perlu restart!</i>",
            parse_mode="HTML"
        )
        return

    pack_input = args[0].strip()
    pack_name = pack_input.split("/")[-1].replace("t.me/addemoji/", "").strip()

    status_msg = await msg.reply_text(f"⏳ Sedang membaca emoji pack Telegram: <code>{pack_name}</code>...", parse_mode="HTML")

    try:
        sticker_set = await context.bot.get_sticker_set(name=pack_name)
        if not sticker_set or not sticker_set.stickers:
            await status_msg.edit_text(f"❌ Emoji pack <code>{pack_name}</code> tidak ditemukan atau kosong.", parse_mode="HTML")
            return

        synced = sync_from_stickers(sticker_set.stickers)
        if not synced:
            await status_msg.edit_text(
                f"⚠️ Berhasil membaca pack <b>{sticker_set.title}</b> ({len(sticker_set.stickers)} item), "
                "namun tidak ada Custom Emoji ID yang valid ditemukan.",
                parse_mode="HTML"
            )
            return

        lines = [
            f"🎉 <b>BERHASIL SINKRONISASI EMOJI PACK!</b>",
            f"📦 <b>Pack:</b> {sticker_set.title} (<code>{pack_name}</code>)",
            f"✨ <b>Total Disinkronkan:</b> {len(synced)} emoji\n",
            "<b>Daftar Emoji yang Diperbarui:</b>"
        ]

        for key, (cid, alt) in synced.items():
            preview = tg_emoji(key, alt)
            lines.append(f"• <b>{key}:</b> {preview} (ID: <code>{cid}</code>)")

        lines.append("\n✅ <i>Semua menu bot kini otomatis menggunakan emoji dari pack baru Anda!</i>")
        await status_msg.edit_text("\n".join(lines), parse_mode="HTML")

    except Exception as exc:
        logger.error("Error in syncpack_handler: %s", exc, exc_info=True)
        await status_msg.edit_text(f"❌ Gagal menyinkronkan emoji pack: <code>{str(exc)}</code>", parse_mode="HTML")


async def setemoji_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Otomatisasi: Daftarkan atau ganti satu custom emoji secara instan.
    Format: /setemoji [KEY] [EMOJI_CUSTOM]
    Contoh: /setemoji BOT 🤖
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    msg = update.effective_message
    if not msg:
        return

    args = context.args or []
    if not args or len(args) < 1:
        await msg.reply_text(
            "⚡️ <b>SET SINGLE CUSTOM EMOJI</b>\n\n"
            "<b>Format:</b> <code>/setemoji [KEY] [EMOJI_PREMIUM]</code>\n\n"
            "<b>Contoh:</b> <code>/setemoji BOT 🤖</code>\n\n"
            "<b>Daftar KEY yang Tersedia:</b>\n"
            "<code>BOT, USER, CROWN, VERIFIED, CHART, MONEY_BAG, DOLLAR, CARD, COIN, CART, BOX, SWAP, CHECK, CROSS, WARNING, PHONE, CHAT, HISTORY, FIRE, ROCKET, DIAMOND, SPARKLES, STAR, PARTY, CALENDAR, WAVE</code>",
            parse_mode="HTML"
        )
        return

    key = args[0].upper().strip()
    entities = msg.entities or []
    custom_emojis = [e for e in entities if getattr(e, "type", None) == "custom_emoji" or str(getattr(e, "type", "")) == "MessageEntityType.CUSTOM_EMOJI"]

    if not custom_emojis:
        await msg.reply_text(
            f"❌ Tidak ada custom emoji premium yang terdeteksi di pesan.\n\n"
            f"Pastikan Anda mengirim emoji dari custom emoji pack Telegram: <code>/setemoji {key} [EMOJI]</code>",
            parse_mode="HTML"
        )
        return

    target_entity = custom_emojis[0]
    emoji_id = getattr(target_entity, "custom_emoji_id", "")
    offset = getattr(target_entity, "offset", 0)
    length = getattr(target_entity, "length", 1)
    emoji_char = msg.text[offset:offset+length] if msg.text else "✨"

    set_custom_emoji(key, emoji_id, emoji_char)
    preview = tg_emoji(key, emoji_char)

    await msg.reply_text(
        f"✅ <b>CUSTOM EMOJI BERHASIL DIPERBARUI!</b>\n\n"
        f"• <b>Key:</b> <code>{key}</code>\n"
        f"• <b>Preview:</b> {preview}\n"
        f"• <b>ID:</b> <code>{emoji_id}</code>\n"
        f"• <b>Alt:</b> {emoji_char}\n\n"
        f"<i>Perubahan langsung aktif di seluruh menu bot!</i>",
        parse_mode="HTML"
    )


async def listemojis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan daftar seluruh custom emoji yang sedang aktif di bot."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    lines = ["✨ <b>DAFTAR CUSTOM EMOJI AKTIF BOT</b>\n"]
    for key, cid in sorted(CUSTOM_EMOJI_IDS.items()):
        alt = CUSTOM_EMOJI_ALTS.get(key, "✨")
        preview = tg_emoji(key, alt)
        lines.append(f"• <b>{key}:</b> {preview} — ID: <code>{cid}</code>")

    lines.append("\n💡 <i>Gunakan <code>/setemoji [KEY] [EMOJI]</code> atau <code>/syncpack [PACK]</code> untuk mengubah.</i>")
    lines.append("🔄 <i>Gunakan <code>/resetemojis</code> untuk mereset ke default bawaan.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def resetemojis_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mereset seluruh custom emoji kembali ke default bawaan."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    reset_custom_emojis()
    await update.message.reply_text("🔄 <b>Custom emoji berhasil direset ke konfigurasi default bawaan Telegram!</b>", parse_mode="HTML")


async def getemoji_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Mendeteksi custom_emoji_id dari pesan untuk dimasukkan ke bot/utils/emojis.py.
    Cara pakai: Kirim custom emoji 3D premium ke bot dengan command /getemoji [EMOJI]
    """
    msg = update.effective_message
    if not msg:
        return
    
    entities = msg.entities or []
    custom_emojis = [e for e in entities if getattr(e, "type", None) == "custom_emoji" or str(getattr(e, "type", "")) == "MessageEntityType.CUSTOM_EMOJI"]
    
    if not custom_emojis:
        await msg.reply_text(
            "💡 <b>CARA MENDAPATKAN ID EMOJI 3D PREMIUM:</b>\n\n"
            "Ketik <code>/getemoji</code> lalu sertakan emoji premium 3D dari sticker/emoji pack Telegram Anda.\n\n"
            "<i>Contoh:</i> Kirim pesan <code>/getemoji [EMOJI_PREMIUM]</code>",
            parse_mode="HTML"
        )
        return

    res_lines = ["✨ <b>CUSTOM EMOJI 3D TERDETEKSI:</b>\n"]
    for i, e in enumerate(custom_emojis, 1):
        emoji_id = getattr(e, "custom_emoji_id", "")
        offset = getattr(e, "offset", 0)
        length = getattr(e, "length", 1)
        emoji_char = msg.text[offset:offset+length] if msg.text else "✨"
        
        res_lines.append(
            f"<b>{i}. Emoji:</b> {emoji_char}\n"
            f"• <b>Custom Emoji ID:</b> <code>{emoji_id}</code>\n"
            f"• <b>Format HTML:</b>\n"
            f"<code>&lt;tg-emoji emoji-id=\"{emoji_id}\"&gt;{emoji_char}&lt;/tg-emoji&gt;</code>\n"
        )
    
    res_lines.append("💡 <i>Gunakan perintah <code>/setemoji [KEY] [EMOJI]</code> untuk langsung memasangnya ke bot.</i>")
    await msg.reply_text("\n".join(res_lines), parse_mode="HTML")


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan statistik harian transaksi bot."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    db = SessionLocal()
    try:
        stats = crud.get_daily_stats(db)
        total_users = crud.get_user_count(db)
        
        stats_text = (
            "📊 <b>STATISTIK HARIAN BOT</b>\n"
            f"📅 Tanggal: {datetime.now(timezone.utc).strftime('%d-%m-%Y')}\n\n"
            f"👤 <b>Total Pengguna:</b> {total_users} member\n"
            f"🛒 <b>Order Hari Ini:</b> {stats['total_orders_today']} order\n"
            f"✅ <b>Order Sukses Hari Ini:</b> {stats['completed_orders_today']} order\n"
            f"💳 <b>Volume Transaksi Hari Ini:</b> {format_idr(stats['total_volume_idr_today'])}\n"
        )
        await update.message.reply_text(stats_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error stats_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memuat statistik.")
    finally:
        db.close()


async def setspread_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mengupdate markup spread persentase untuk symbol tertentu."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    # Validasi argument: /setspread USDT 1.2
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ Format salah. Contoh penggunaan:\n"
            "<code>/setspread USDT 1.5</code>",
            parse_mode="HTML"
        )
        return

    symbol = context.args[0].upper()
    try:
        spread_pct = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Nilai persentase spread harus berupa angka desimal.")
        return

    db = SessionLocal()
    try:
        config = crud.update_price_config(db, symbol, spread_pct)
        if config:
            await update.message.reply_text(
                f"✅ Spread harga untuk <b>{symbol}</b> berhasil diperbarui menjadi <b>{spread_pct}%</b>.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(f"❌ Koin/Token <b>{symbol}</b> tidak didukung atau tidak aktif.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error setspread: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal mengupdate spread harga.")
    finally:
        db.close()


async def orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan list order yang berstatus pending atau paid (butuh review admin)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    db = SessionLocal()
    try:
        # Ambil order yang statusnya pending, paid, payout_processing, atau manual_review
        orders = (
            db.query(Order)
            .filter(Order.status.in_(["pending", "paid", "payout_processing", "manual_review"]))
            .order_by(Order.created_at.desc())
            .limit(20)
            .all()
        )
        
        if not orders:
            await update.message.reply_text("📥 <b>Tidak ada order aktif/tertunda saat ini.</b>", parse_mode="HTML")
            return

        text_lines = ["📥 <b>DAFTAR ORDER AKTIF (PENDING/PAID)</b>\n"]
        for o in orders:
            o_type = "🛒 BELI" if o.order_type == "buy" else "💵 JUAL"
            crypto_str = format_crypto(float(o.crypto_amount), o.crypto_symbol)
            
            text_lines.append(
                f"• <b>{o.order_id}</b> ({o_type})\n"
                f"  🚦 Status: <b>{o.status.upper()}</b>\n"
                f"  🪙 Koin: <code>{crypto_str} ({o.network})</code>\n"
                f"  💳 IDR: <code>{format_idr(o.total_idr)}</code>\n"
                f"  👤 User ID: <code>{o.telegram_id}</code>\n"
            )
        
        await update.message.reply_text("\n".join(text_lines), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error orders_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memuat daftar order.")
    finally:
        db.close()


async def confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Mengonfirmasi penyelesaian order secara manual oleh admin.
    Digunakan jika:
      - Sell order: Admin sudah mengirim Rupiah ke rekening customer.
      - Buy order: Crypto gagal terkirim otomatis lalu dikirim manual oleh admin.
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Format salah. Sertakan ID Order. Contoh:\n"
            "<code>/confirm ORD-20260527-XYZ</code>",
            parse_mode="HTML"
        )
        return

    order_id = context.args[0].strip()
    
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await update.message.reply_text(f"❌ Order <code>{order_id}</code> tidak ditemukan.", parse_mode="HTML")
            return

        was_already_completed = (order.status == "completed")

        if not was_already_completed:
            # Update status order ke completed jika belum completed
            crud.update_order_status(
                db, 
                order_id, 
                new_status="completed", 
                completed_at=datetime.utcnow()
            )
            crud.release_order_inventory(db, order_id)
        
        # Kirim notifikasi sukses ke user
        from bot.utils.telegram_utils import safe_send_message
        from bot.utils.messages import build_sell_completion_message, build_buy_completion_message
        
        if order.order_type == "sell":
            user_msg = build_sell_completion_message(order)
        else:
            user_msg = build_buy_completion_message(order)
            
        menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]])
        sent = await safe_send_message(context.bot, order.telegram_id, user_msg, reply_markup=menu_keyboard)

        if was_already_completed:
            status_text = "sudah berstatus COMPLETED sebelumnya"
            notif_text = "Notifikasi berhasil dikirim ulang ke user" if sent else "Gagal mengirim notifikasi ke user"
            await update.message.reply_text(
                f"ℹ️ Order <code>{order_id}</code> {status_text}.\n"
                f"📬 <b>{notif_text}</b> (User ID: <code>{order.telegram_id}</code>).",
                parse_mode="HTML"
            )
        else:
            notif_text = "notifikasi sukses telah dikirim ke user" if sent else "namun pengiriman notifikasi ke user gagal"
            await update.message.reply_text(
                f"✅ Order <code>{order_id}</code> berhasil dikonfirmasi sebagai <b>COMPLETED</b> dan {notif_text} (User ID: <code>{order.telegram_id}</code>).",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"Error confirm_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memproses konfirmasi order.")
    finally:
        db.close()


async def admin_confirm_sell_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Konfirmasi transfer Rupiah untuk order Sell."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_confirm_sell_", "")
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await query.answer("❌ Order tidak ditemukan.", show_alert=True)
            return

        was_already_completed = (order.status == "completed")

        if not was_already_completed:
            # Update status order ke completed
            crud.update_order_status(
                db, 
                order_id, 
                new_status="completed", 
                completed_at=datetime.utcnow()
            )
            crud.release_order_inventory(db, order_id)

        # Kirim notifikasi sukses ke user
        from bot.utils.telegram_utils import safe_send_message
        from bot.utils.messages import build_sell_completion_message
        user_msg = build_sell_completion_message(order)
        
        menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]])
        sent = await safe_send_message(context.bot, order.telegram_id, user_msg, reply_markup=menu_keyboard)

        alert_text = "✅ Berhasil konfirmasi & notifikasi terkirim ke user!" if sent else "⚠️ Order COMPLETED namun gagal mengirim notifikasi ke user."
        if was_already_completed:
            alert_text = "ℹ️ Order sudah COMPLETED. Notifikasi dikirim ulang ke user." if sent else "ℹ️ Order sudah COMPLETED."
        await query.answer(alert_text, show_alert=True)

        msg = query.message
        completion_tag = "\n\n✅ <b>RUPIAH SUDAH DITRANSFER OLEH ADMIN (COMPLETED)</b>"
        if msg.caption and completion_tag not in msg.caption:
            caption_now = msg.caption or ""
            await query.edit_message_caption(
                caption=f"{caption_now}{completion_tag}",
                parse_mode="HTML"
            )
        elif msg.text and completion_tag not in msg.text:
            text_now = msg.text or ""
            await query.edit_message_text(
                text=f"{text_now}{completion_tag}",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error admin_confirm_sell_callback {order_id}: {e}", exc_info=True)
        await query.answer("❌ Gagal memproses konfirmasi.", show_alert=True)
    finally:
        db.close()


async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mengirim pesan broadcast/siaran ke seluruh user terdaftar di bot."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    # Validasi argument: /broadcast Pengumuman penting
    if not context.args:
        await update.message.reply_text("⚠️ Format salah. Masukkan pesan siaran. Contoh:\n`/broadcast Halo member...`")
        return

    broadcast_msg = update.message.text.replace("/broadcast", "").strip()

    db = SessionLocal()
    try:
        users = db.query(User).filter(User.is_banned == False).all() # noqa: E712
        if not users:
            await update.message.reply_text("ℹ️ Tidak ada pengguna terdaftar untuk dikirim broadcast.")
            return

        await update.message.reply_text(f"⏳ Mengirim siaran ke {len(users)} pengguna...")
        
        success_count = 0
        fail_count = 0
        
        for u in users:
            try:
                await context.bot.send_message(
                    chat_id=u.telegram_id,
                    text=f"📢 <b>PENGUMUMAN DARI OWNER</b>\n\n{broadcast_msg}",
                    parse_mode="HTML"
                )
                success_count += 1
            except Exception:
                fail_count += 1
                
        await update.message.reply_text(
            f"📢 <b>Broadcast Selesai</b>\n"
            f"• Sukses terkirim: <code>{success_count} user</code>\n"
            f"• Gagal/Blokir bot: <code>{fail_count} user</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error broadcast: {e}", exc_info=True)
        await update.message.reply_text("❌ Terjadi kesalahan saat mengirim broadcast.")
    finally:
        db.close()


async def ban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Memblokir user agar tidak bisa bertransaksi."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Contoh: `/ban 123456789`")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID harus berupa angka.")
        return

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == target_id).first()
        if not user:
            await update.message.reply_text("❌ Pengguna tidak ditemukan di database.")
            return

        user.is_banned = True
        db.commit()
        await update.message.reply_text(f"✅ Pengguna dengan ID <code>{target_id}</code> berhasil DI-BAN.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error ban_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal memblokir pengguna.")
    finally:
        db.close()


async def unban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Membuka blokir user."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Contoh: `/unban 123456789`")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID harus berupa angka.")
        return

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == target_id).first()
        if not user:
            await update.message.reply_text("❌ Pengguna tidak ditemukan di database.")
            return

        user.is_banned = False
        db.commit()
        await update.message.reply_text(f"✅ Blokir pengguna dengan ID <code>{target_id}</code> berhasil DIBUKA.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error unban_handler: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal membuka blokir pengguna.")
    finally:
        db.close()


async def refreshwallet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Memicu sinkronisasi saldo blockchain secara manual."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    await update.message.reply_text("⏳ <i>Melakukan sinkronisasi saldo on-chain wallet...</i>", parse_mode="HTML")
    
    from config.assets import STOCK_ASSETS

    db = SessionLocal()
    try:
        success_list = []
        fail_list = []
        
        for sym, net in STOCK_ASSETS:
            try:
                sender = CryptoSenderFactory.get_sender(net)
                balance = await sender.get_balance(symbol=sym)
                addr = getattr(sender, "wallet_address", None)
                crud.update_wallet_balance(
                    db, network=net, symbol=sym, balance=balance, address=addr
                )
                success_list.append(f"{sym} ({net})")
            except Exception as net_exc:
                logger.warning(f"Refresh wallet manual gagal untuk {sym} ({net}): {net_exc}")
                fail_list.append(f"{sym} ({net})")
                
        status_msg = (
            "✅ <b>SINKRONISASI WALLET SELESAI</b>\n\n"
            f"• Sukses: <code>{', '.join(success_list)}</code>\n"
            f"• Gagal: <code>{', '.join(fail_list) if fail_list else 'Tidak ada'}</code>"
        )
        await update.message.reply_text(status_msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error refreshwallet: {e}", exc_info=True)
        await update.message.reply_text("❌ Gagal menyinkronisasikan wallet.")
    finally:
        db.close()


async def admin_approve_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Approve order Buy & trigger auto-send crypto."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_approve_buy_", "")
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await query.answer("❌ Order tidak ditemukan.", show_alert=True)
            return

        if order.payout_tx_hash or order.status == "completed":
            await query.answer("ℹ️ Order ini sudah COMPLETED.", show_alert=True)
            return

        if order.status not in ("pending", "paid", "manual_review", "expired"):
            await query.answer(
                f"ℹ️ Order berstatus {order.status.upper()} — tidak bisa di-approve.", show_alert=True
            )
            return

        await query.answer("⏳ Memproses pengiriman crypto otomatis...", show_alert=True)
        import asyncio
        from bot.handlers.buy import _run_finalize_background
        asyncio.create_task(
            _run_finalize_background(order.order_id, context.bot, allow_admin=True)
        )

        caption_now = query.message.caption or ""
        await query.edit_message_caption(
            caption=f"{caption_now}\n\n✅ <b>APPROVED & DIESEKUSI OTOMATIS OLEH ADMIN</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error admin_approve_buy_callback {order_id}: {e}", exc_info=True)
        await query.answer("❌ Gagal memproses approval.", show_alert=True)
    finally:
        db.close()


async def admin_reject_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Reject order Buy."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_reject_buy_", "")
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if order:
            crud.update_order_status(db, order_id, new_status="rejected", failure_reason="Ditolak oleh admin")
            crud.release_order_inventory(db, order_id)
            from bot.utils.telegram_utils import safe_send_message
            await safe_send_message(
                context.bot, order.telegram_id,
                f"❌ <b>Order {order_id} Ditolak</b>\n"
                f"Bukti pembayaran Anda tidak dapat diverifikasi oleh admin. Silakan hubungi admin jika ada kendala."
            )
        await query.answer("Order berhasil ditolak.")
        caption_now = query.message.caption or ""
        await query.edit_message_caption(
            caption=f"{caption_now}\n\n❌ <b>DITOLAK OLEH ADMIN</b>",
            parse_mode="HTML"
        )
    finally:
        db.close()


async def admin_approve_topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Approve topup saldo IDR."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    topup_id = query.data.replace("admin_approve_topup_", "")
    db = SessionLocal()
    try:
        topup = crud.get_topup_order_by_id(db, topup_id)
        if not topup:
            await query.answer("❌ Topup tidak ditemukan.", show_alert=True)
            return

        if topup.status == "SUCCESS":
            await query.answer("ℹ️ Topup ini sudah LUNAS.", show_alert=True)
            return

        if not crud.claim_topup_success(db, topup_id):
            await query.answer("ℹ️ Topup ini sudah diproses sistem.", show_alert=True)
            return

        new_bal = crud.credit_user_balance(db, topup.telegram_id, topup.amount_idr)

        from bot.utils.telegram_utils import safe_send_message
        menu_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Menu Utama", callback_data="menu_back", icon_custom_emoji_id=CUSTOM_EMOJI_IDS.get("BACK", "5202123071053381850"))]])
        await safe_send_message(
            context.bot, topup.telegram_id,
            f"✅ <b>PEMBAYARAN TOPUP TERVERIFIKASI ADMIN!</b>\n\n"
            f"🎉 Topup saldo sebesar <b>{format_idr(topup.amount_idr)}</b> telah berhasil di-approve!\n"
            f"💳 <b>Total Saldo Bot Anda Saat Ini</b>: <b>{format_idr(int(new_bal))}</b>",
            reply_markup=menu_keyboard
        )

        await query.answer("Topup berhasil di-approve!")
        caption_now = query.message.caption or ""
        await query.edit_message_caption(
            caption=f"{caption_now}\n\n✅ <b>APPROVED & SALDO DITAMBAHKAN OLEH ADMIN</b>",
            parse_mode="HTML"
        )
    finally:
        db.close()


async def admin_reject_topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Reject topup saldo IDR."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    topup_id = query.data.replace("admin_reject_topup_", "")
    db = SessionLocal()
    try:
        crud.update_topup_status(db, topup_id, "CANCELLED")
        topup = crud.get_topup_order_by_id(db, topup_id)
        if topup:
            from bot.utils.telegram_utils import safe_send_message
            await safe_send_message(
                context.bot, topup.telegram_id,
                f"❌ <b>Top-up {topup_id} Ditolak</b>\n"
                f"Bukti pembayaran Anda tidak dapat diverifikasi oleh admin."
            )
        await query.answer("Topup berhasil ditolak.")
        caption_now = query.message.caption or ""
        await query.edit_message_caption(
            caption=f"{caption_now}\n\n❌ <b>DITOLAK OLEH ADMIN</b>",
            parse_mode="HTML"
        )
    finally:
        db.close()


async def admin_approve_swap_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Approve & eksekusi pengiriman koin tujuan Swap."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_approve_swap_", "")
    db = SessionLocal()
    try:
        order = crud.get_order_by_id(db, order_id)
        if not order:
            await query.answer("❌ Order tidak ditemukan.", show_alert=True)
            return

        if order.status == "COMPLETED" or order.payout_tx_hash:
            await query.answer("ℹ️ Order ini sudah COMPLETED.", show_alert=True)
            return

        await query.answer("⚡ Menyetujui deposit & mengeksekusi payout swap...", show_alert=False)

        # Ubah status ke CRYPTO_CONFIRMED agar dapat dipayout
        order.status = "CRYPTO_CONFIRMED"
        order.confirmed_at = datetime.utcnow()
        db.commit()

        from services.detector import deposit_detector
        await deposit_detector._execute_payout(db, order, context.application)

        caption_now = query.message.caption or ""
        text_now = query.message.text or ""
        if caption_now:
            await query.edit_message_caption(
                caption=f"{caption_now}\n\n✅ <b>SWAP DI-APPROVE & DIEKSEKUSI OLEH ADMIN</b>",
                parse_mode="HTML"
            )
        elif text_now:
            await query.edit_message_text(
                text=f"{text_now}\n\n✅ <b>SWAP DI-APPROVE & DIEKSEKUSI OLEH ADMIN</b>",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error admin_approve_swap_callback {order_id}: {e}", exc_info=True)
        await query.answer(f"❌ Error: {e}", show_alert=True)
    finally:
        db.close()


async def admin_reject_swap_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback tombol Admin: Tolak pesanan swap."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("❌ Akses ditolak.", show_alert=True)
        return

    order_id = query.data.replace("admin_reject_swap_", "")
    db = SessionLocal()
    try:
        crud.update_order_status(db, order_id, "CANCELLED")
        order = crud.get_order_by_id(db, order_id)
        if order:
            from bot.utils.telegram_utils import safe_send_message
            await safe_send_message(
                context.bot, order.telegram_id,
                f"❌ <b>Pesanan Swap {order_id} Dibatalkan</b>\n\n"
                f"Bukti transfer deposit tidak dapat diverifikasi oleh admin. "
                f"Silakan hubungi admin jika terdapat kekeliruan."
            )
        await query.answer("Swap berhasil ditolak/dibatalkan.")
        caption_now = query.message.caption or ""
        text_now = query.message.text or ""
        if caption_now:
            await query.edit_message_caption(
                caption=f"{caption_now}\n\n❌ <b>DITOLAK OLEH ADMIN</b>",
                parse_mode="HTML"
            )
        elif text_now:
            await query.edit_message_text(
                text=f"{text_now}\n\n❌ <b>DITOLAK OLEH ADMIN</b>",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Error admin_reject_swap_callback {order_id}: {e}", exc_info=True)
    finally:
        db.close()

