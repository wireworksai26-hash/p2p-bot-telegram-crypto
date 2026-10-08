"""
bot/handlers/start.py — Handler /start & Navigasi Menu Utama.
==============================================================
Menangani command /start untuk menyapa pengguna dan mendaftarkannya ke DB.
Juga berfungsi sebagai router untuk callback query tombol menu navigasi dasar.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes

from database.connection import SessionLocal
from database.models import User
from database.crud import create_user, get_user_count, get_user, get_completed_order_count
from bot.keyboards.main_menu import get_main_menu_keyboard
from bot.keyboards.crypto_select import get_owner_button
from bot.utils.messages import WELCOME_MESSAGE, SNK_TEXT
from bot.utils.emojis import CUSTOM_EMOJI_IDS

logger = logging.getLogger(__name__)

def get_wib_datetime_info() -> tuple[str, str]:
    from datetime import datetime, timezone, timedelta
    wib = timezone(timedelta(hours=7))
    now = datetime.now(wib)
    
    hour = now.hour
    if 4 <= hour < 11:
        greeting = "Pagi"
    elif 11 <= hour < 15:
        greeting = "Siang"
    elif 15 <= hour < 18:
        greeting = "Sore"
    else:
        greeting = "Malam"
        
    days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    months = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    
    day_str = days[now.weekday()]
    month_str = months[now.month - 1]
    time_str = f"{day_str}, {now.day} {month_str} {now.year} pukul {now.strftime('%H.%M.%S')} WIB"
    
    return greeting, time_str


def build_welcome_message(user, db_user, total_users: int, total_success: int) -> str:
    import html
    from bot.utils.formatter import format_idr

    greeting, time_str = get_wib_datetime_info()
    user_name = html.escape(user.first_name or "Kak")
    user_bal = int(getattr(db_user, "balance_idr", 0) or 0) if db_user else 0
    user_orders = int(getattr(db_user, "total_orders", 0) or 0) if db_user else 0

    return (
        f"👋 <b>Selamat {greeting}, {user_name}!</b>\n"
        f"📆 <i>{time_str}</i>\n\n"
        f"Selamat Datang di <b>TokoKoin ID</b> , Platform Jual Beli Koin terpercaya di Telegram. ☑️\n\n"
        f"📊 <b>STATISTIK AKUN</b>\n"
        f"├── 💰 <b>Saldo Aktif</b>   : <b>{format_idr(user_bal)}</b>\n"
        f"└── 🛒 <b>Total Order</b>   : <b>{user_orders} Transaksi</b>\n\n"
        f"🤖 <b>STATISTIK BOT</b>\n"
        f"├── 👥 <b>Total Pengguna</b> : <b>{total_users:,} Member</b>\n"
        f"└── ✅ <b>Total Transaksi</b>: <b>{total_success:,}x Berhasil</b>\n\n"
        f"Silahkan pilih menu di bawah untuk memulai transaksi:"
    )


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Kirim atau edit pesan kembali ke menu utama dengan tampilan 3D Dashboard.
    """
    user = update.effective_user
    query = update.callback_query
    
    db = SessionLocal()
    try:
        db_user = get_user(db, user.id)
        if not db_user:
            db_user = create_user(
                db=db,
                telegram_id=user.id,
                username=user.username,
                full_name=user.full_name
            )
        total_users = get_user_count(db)
        total_success = get_completed_order_count(db)
    finally:
        db.close()
        
    welcome_text = build_welcome_message(
        user=user,
        db_user=db_user,
        total_users=total_users,
        total_success=total_success
    )
    
    if query:
        try:
            await query.edit_message_text(
                text=welcome_text,
                reply_markup=get_main_menu_keyboard(),
                parse_mode="HTML"
            )
        except Exception:
            try:
                await query.message.reply_text(
                    text=welcome_text,
                    reply_markup=get_main_menu_keyboard(),
                    parse_mode="HTML"
                )
            except Exception:
                pass
    else:
        await update.message.reply_text(
            text=welcome_text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode="HTML"
        )


def reset_user_conversations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bersihkan state percakapan aktif user di semua ConversationHandler & user_data."""
    if context and hasattr(context, "user_data") and context.user_data is not None:
        context.user_data.clear()

    user = update.effective_user if update else None
    chat = update.effective_chat if update else None
    if not user:
        return

    try:
        from bot.handlers.buy import buy_conversation_handler
        from bot.handlers.sell import sell_conversation_handler
        from bot.handlers.swap import swap_conv_handler
        from bot.handlers.balance import topup_conversation_handler
        from bot.handlers.calculator import calculator_conversation_handler

        handlers = [
            buy_conversation_handler,
            sell_conversation_handler,
            swap_conv_handler,
            topup_conversation_handler,
            calculator_conversation_handler,
        ]

        u_id = user.id
        c_id = chat.id if chat else u_id
        keys_to_remove = [(c_id, u_id), (u_id,), (u_id, u_id), (c_id,)]

        for handler in handlers:
            if hasattr(handler, "_conversations") and isinstance(handler._conversations, dict):
                for k in keys_to_remove:
                    handler._conversations.pop(k, None)
    except Exception as exc:
        logger.debug("Debug reset conversations: %s", exc)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler untuk command /start.
    Mendaftarkan user ke database jika baru, mereset state percakapan lama,
    kemudian mengirim welcome message & dashboard menu.
    Mendukung deep-link referral: /start ref_<TELEGRAM_ID>
    """
    try:
        user = update.effective_user
        
        # Reset state percakapan lama jika user pernah stuck di flow sebelumnya
        reset_user_conversations(update, context)

        # Daftarkan / update profil user ke database
        db = SessionLocal()
        try:
            create_user(
                db=db,
                telegram_id=user.id,
                username=user.username,
                full_name=user.full_name
            )

            # Deep-link referral detection
            if context.args and context.args[0].startswith("ref_"):
                try:
                    referrer_id = int(context.args[0].replace("ref_", ""))
                    if referrer_id != user.id:  # Tidak bisa refer diri sendiri
                        from database.crud import create_referral, get_referral_by_referee
                        from services.referral_rewards import get_referral_settings
                        existing_ref = get_referral_by_referee(db, user.id)
                        if not existing_ref:
                            ref = create_referral(db, referrer_id, user.id)
                            if ref:
                                cfg = get_referral_settings(db)
                                min_trade_note = (
                                    f" minimal <b>Rp {cfg['min_trade']:,}</b>" if cfg["min_trade"] > 0 else ""
                                )

                                # Notifikasi ke referee (user baru): diskon fee transaksi pertama
                                if cfg["bonus"] > 0:
                                    try:
                                        await update.message.reply_text(
                                            f"🎁 <b>Selamat Datang!</b>\n\n"
                                            f"Anda bergabung lewat link undangan teman.\n"
                                            f"Dapatkan <b>diskon fee Rp {cfg['bonus']:,}</b> di transaksi pertama Anda "
                                            f"(beli/jual/convert{min_trade_note}). Diskon masuk ke saldo bot "
                                            f"setelah transaksi selesai.",
                                            parse_mode="HTML"
                                        )
                                    except Exception:
                                        pass

                                # Notifikasi ke referrer
                                try:
                                    await context.bot.send_message(
                                        referrer_id,
                                        f"🎉 <b>Referral Baru!</b>\n\n"
                                        f"Teman baru bergabung via link referral Anda.\n"
                                        f"Anda mendapat <b>Rp {cfg['reward']:,}</b> saat transaksi pertama mereka "
                                        f"selesai{min_trade_note}, <b>Rp {cfg['reward2']:,}</b> di transaksi kedua, "
                                        f"plus <b>{cfg['share']:g}%</b> dari fee transaksi mereka. "
                                        f"Reward masuk ke saldo bot setelah masa tahan {cfg['hold']} jam.",
                                        parse_mode="HTML",
                                    )
                                except Exception:
                                    pass
                except (ValueError, TypeError):
                    pass  # Invalid referral link, ignore silently
        finally:
            db.close()
            
        await send_main_menu(update, context)
        
    except Exception as e:
        logger.error(f"Error di start_handler: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Terjadi kesalahan saat memproses data Anda. Silakan coba lagi.")


async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catch-all CallbackQueryHandler untuk menangani tombol menu statis.
    Mengarahkan menu_* callback ke fungsinya masing-masing.
    """
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass
    
    data = query.data if query else None
    logger.info(f"Callback query received: {data}")

    # Tombol baru membatalkan prompt teks admin yang masih menunggu (reward, pengecualian,
    # kirim saldo, kas bot…): kalau tidak, teks berikutnya "ditelan" wizard lama yang sudah
    # ditinggalkan (mis. mengecualikan user dari milestone tanpa sengaja). Callback wizard
    # reward/pengecualian mengatur flag-nya sendiri.
    if data and not data.startswith(("admin_reward_", "admin_milestone_")):
        from bot.handlers.admin import _clear_reward_flags
        _clear_reward_flags(context)

    if data == "menu_guide" or (data and data.startswith("guide_")):
        # Panduan transaksi user (Beli / Jual / Convert, cara dapat TX Hash, kendala umum)
        from bot.utils.user_guide import guide_index, guide_topic
        page = guide_index() if data == "menu_guide" else guide_topic(data[len("guide_"):])
        if page is None:
            await query.answer("Topik panduan tidak ditemukan.", show_alert=True)
        else:
            guide_text, guide_markup = page
            await query.edit_message_text(text=guide_text, reply_markup=guide_markup, parse_mode="HTML")

    elif data == "menu_snk":
        # Tampilkan Syarat & Ketentuan
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        keyboard = [
            [InlineKeyboardButton("🔙 Kembali ke Menu", callback_data="menu_back")],
            [get_owner_button()]
        ]
        await query.edit_message_text(
            text=SNK_TEXT,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        
    elif data in ["menu_back", "buy_cancel", "sell_cancel", "calc_cancel", "cancel_swap", "cancel_topup"]:
        reset_user_conversations(update, context)
        await send_main_menu(update, context)
        
    elif data in ["menu_balance", "show_balance"]:
        from bot.handlers.balance import show_balance_menu
        await show_balance_menu(update, context)

    elif data in ["buy_saved_wallets", "menu_saved_wallets"]:
        back_target = "buy_back_to_menu" if data == "buy_saved_wallets" else "menu_balance"
        from bot.handlers.saved_accounts import show_saved_wallets_menu
        await show_saved_wallets_menu(update, context, back_callback=back_target)

    elif data in ["sell_saved_banks", "menu_saved_banks"]:
        back_target = "sell_back_to_menu" if data == "sell_saved_banks" else "menu_balance"
        from bot.handlers.saved_accounts import show_saved_banks_menu
        await show_saved_banks_menu(update, context, back_callback=back_target)

    elif data == "buy_back_to_menu":
        from bot.handlers.buy import start_buy_callback
        await start_buy_callback(update, context)

    elif data == "sell_back_to_menu":
        from bot.handlers.sell import start_sell_callback
        await start_sell_callback(update, context)

    elif data == "act_add_saved_wallet":
        from bot.handlers.saved_accounts import prompt_add_saved_wallet
        await prompt_add_saved_wallet(update, context)

    elif data.startswith("act_add_wallet_"):
        from bot.handlers.saved_accounts import prompt_add_wallet_specific
        await prompt_add_wallet_specific(update, context)

    elif data == "act_set_default_wallet_menu":
        from bot.handlers.saved_accounts import show_set_default_wallet_menu
        await show_set_default_wallet_menu(update, context)

    elif data.startswith("act_set_default_"):
        from bot.handlers.saved_accounts import handle_set_default_wallet_action
        await handle_set_default_wallet_action(update, context)

    elif data.startswith("act_evm_chain_"):
        from bot.handlers.saved_accounts import handle_confirm_evm_chain
        await handle_confirm_evm_chain(update, context)

    elif data == "act_add_saved_bank":
        from bot.handlers.saved_accounts import prompt_add_saved_bank
        await prompt_add_saved_bank(update, context)

    elif data == "act_del_saved_wallet_menu":
        from bot.handlers.saved_accounts import show_delete_wallet_menu
        await show_delete_wallet_menu(update, context)

    elif data.startswith("act_del_wallet_"):
        from bot.handlers.saved_accounts import handle_delete_wallet_action
        await handle_delete_wallet_action(update, context)

    elif data == "act_del_saved_bank_menu":
        from bot.handlers.saved_accounts import show_delete_bank_menu
        await show_delete_bank_menu(update, context)

    elif data.startswith("act_del_bank_"):
        from bot.handlers.saved_accounts import handle_delete_bank_action
        await handle_delete_bank_action(update, context)

        
    elif data == "menu_buy":
        from bot.handlers.buy import start_buy_callback
        await start_buy_callback(update, context)

    elif data == "menu_sell":
        from bot.handlers.sell import start_sell_callback
        await start_sell_callback(update, context)

    elif data in ["start_swap", "menu_swap"]:
        from bot.handlers.swap import start_swap
        await start_swap(update, context)

    elif data == "wd_locked":
        from bot.handlers.withdraw import withdraw_locked_handler
        await withdraw_locked_handler(update, context)

    elif data == "wd_start":
        from bot.handlers.withdraw import withdraw_start_handler
        await withdraw_start_handler(update, context)

    elif data.startswith("wd_bank_"):
        from bot.handlers.withdraw import withdraw_bank_selected_handler
        await withdraw_bank_selected_handler(update, context)

    elif data == "wd_all":
        from bot.handlers.withdraw import withdraw_all_handler
        await withdraw_all_handler(update, context)

    elif data == "wd_custom":
        from bot.handlers.withdraw import withdraw_custom_prompt_handler
        await withdraw_custom_prompt_handler(update, context)

    elif data == "wd_confirm":
        from bot.handlers.withdraw import withdraw_confirm_handler
        await withdraw_confirm_handler(update, context)

    elif data.startswith(("admin_wd_ok_", "admin_wd_no_")):
        from bot.handlers.withdraw import admin_withdraw_callback
        await admin_withdraw_callback(update, context)

    elif data == "start_topup_qris":
        from bot.handlers.balance import start_topup_callback
        await start_topup_callback(update, context)

    elif data in ["menu_calc", "calc_again"]:
        from bot.handlers.calculator import start_calculator_callback
        await start_calculator_callback(update, context)

    elif data.startswith("check_topup_"):
        from bot.handlers.balance import check_topup_payment_manual
        await check_topup_payment_manual(update, context)

    elif data.startswith("cancel_topup_"):
        from bot.handlers.balance import cancel_topup_manual
        await cancel_topup_manual(update, context)

    elif data.startswith("check_buy_payment_"):
        from bot.handlers.buy import check_buy_payment
        await check_buy_payment(update, context)

    elif data.startswith("admin_approve_buy_"):
        from bot.handlers.admin import admin_approve_buy_callback
        await admin_approve_buy_callback(update, context)

    elif data.startswith("admin_reject_buy_"):
        from bot.handlers.admin import admin_reject_buy_callback
        await admin_reject_buy_callback(update, context)

    elif data.startswith("admin_approve_topup_"):
        from bot.handlers.admin import admin_approve_topup_callback
        await admin_approve_topup_callback(update, context)

    elif data.startswith("admin_reject_topup_"):
        from bot.handlers.admin import admin_reject_topup_callback
        await admin_reject_topup_callback(update, context)

    elif data.startswith("admin_confirm_sell_"):
        from bot.handlers.admin import admin_confirm_sell_callback
        await admin_confirm_sell_callback(update, context)

    elif data.startswith("admin_force_sell_"):
        from bot.handlers.admin import admin_force_sell_callback
        await admin_force_sell_callback(update, context)

    elif data.startswith("admin_verify_sell_deposit_"):
        from bot.handlers.admin import admin_verify_sell_deposit_callback
        await admin_verify_sell_deposit_callback(update, context)

    elif data.startswith("admin_upload_proof_"):
        from bot.handlers.admin import admin_upload_proof_callback
        await admin_upload_proof_callback(update, context)

    elif data.startswith("admin_manual_sent_"):
        from bot.handlers.admin import admin_manual_payout_callback
        await admin_manual_payout_callback(update, context)

    elif data.startswith("admin_approve_swap_"):
        from bot.handlers.admin import admin_approve_swap_callback
        await admin_approve_swap_callback(update, context)

    elif data.startswith("admin_reject_swap_"):
        from bot.handlers.admin import admin_reject_swap_callback
        await admin_reject_swap_callback(update, context)

    elif data.startswith("admin_sellorders_"):
        from bot.handlers.admin import sellorders_handler
        await sellorders_handler(update, context)

    elif (
        data.startswith("admin_panel_")
        or data.startswith("admin_send_bal_")
        or data.startswith("admin_treasury_")
        or data.startswith("admin_ref_")
        or data.startswith("admin_top_spender_")
        or data.startswith("admin_reward_")
        or data.startswith("admin_milestone_")
        or data.startswith("admin_draw_")
        or data.startswith("admin_loyalty_")
        or data.startswith("admin_weekly_")
        or data.startswith("admin_export_")
    ):
        if data == "admin_panel_campaign":
            from bot.handlers.admin_campaign import campaign_callback_handler
            await campaign_callback_handler(update, context)
        else:
            from bot.handlers.admin import admin_panel_callback
            await admin_panel_callback(update, context)

    elif data.startswith("camp_"):
        from bot.handlers.admin_campaign import campaign_callback_handler
        await campaign_callback_handler(update, context)

    elif data == "menu_price":
        from bot.handlers.price import show_prices
        await show_prices(update, context)

    elif data == "price_fee_list":
        from bot.handlers.price import show_fee_list
        await show_fee_list(update, context)

    elif data == "show_live_market":
        from bot.handlers.price import show_live_market
        await show_live_market(update, context)
        
    elif data == "menu_stocks" or data.startswith("menu_stocks_page_"):
        from bot.handlers.stocks import show_stocks
        await show_stocks(update, context)

    elif data == "menu_referral":
        from bot.handlers.referral import referral_menu_handler
        await referral_menu_handler(update, context)

    elif data == "referral_leaderboard":
        from bot.handlers.referral import referral_leaderboard_handler
        await referral_leaderboard_handler(update, context)
        
    elif data == "menu_history":
        from bot.handlers.history import show_history
        await show_history(update, context)
        
    else:
        # Tombol dari sesi yang sudah berakhir (dibatalkan, timeout, atau bot
        # baru restart/deploy — state ConversationHandler hanya di memori).
        # Tanpa balasan, user mengira bot macet.
        logger.warning(f"Unhandled callback query: {data}")
        if query and query.message:
            await query.message.reply_text(
                SESSION_EXPIRED_TEXT,
                reply_markup=_back_to_menu_markup(),
                parse_mode="HTML",
            )


SESSION_EXPIRED_TEXT = (
    "⌛ <b>Sesi tombol ini sudah berakhir.</b>\n"
    "Silakan mulai lagi dari menu utama."
)
NO_ACTIVE_FLOW_TEXT = "ℹ️ Tidak ada proses yang sedang berjalan."


def _back_to_menu_markup():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Menu Utama", callback_data="menu_back")
    ]])


async def idle_cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel di luar ConversationHandler (flow aktif ditangani fallback-nya).

    Juga membatalkan input teks yang menunggu (simpan wallet/rekening, campaign
    admin): router teks memakai filter ~COMMAND sehingga /cancel tak pernah sampai.
    """
    user_data = context.user_data if context.user_data is not None else {}
    waiting = [k for k in list(user_data)
               if k.startswith(("awaiting_", "admin_awaiting_")) and user_data.get(k)]
    for key in waiting + ["target_chain_type", "pending_wallet_address",
                          "admin_reward_batch_id", "admin_reward_skipped"]:
        user_data.pop(key, None)
    text = "❌ Proses dibatalkan." if waiting else NO_ACTIVE_FLOW_TEXT
    if update.message:
        await update.message.reply_text(text, reply_markup=_back_to_menu_markup())

