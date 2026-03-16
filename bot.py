import os
import re
import time
import html
import asyncio
import logging
import sqlite3
import datetime
from collections import defaultdict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from duckduckgo_search import DDGS

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("BOT_TOKEN")

BOT_USERNAME_TEXT = "@KGBKORUMABot"
SUPPORT_URL = "https://t.me/KGBotomasyon"
SOURCE_URL = "https://github.com/"
MAX_WARNS = 3
SPAM_WINDOW = 5
SPAM_LIMIT = 5

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("rose_style_mod_bot")

# =========================
# DATABASE
# =========================

conn = sqlite3.connect("bot_database.db", check_same_thread=False)
cursor = conn.cursor()

cursor.executescript("""
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    antilink INTEGER DEFAULT 0,
    welcome INTEGER DEFAULT 0,
    welcome_text TEXT DEFAULT 'Gruba hoş geldin!',
    log_chat_id INTEGER DEFAULT 0,
    rules_text TEXT DEFAULT 'Henüz kurallar ayarlanmadı.'
);

CREATE TABLE IF NOT EXISTS badwords (
    chat_id INTEGER,
    word TEXT,
    UNIQUE(chat_id, word)
);

CREATE TABLE IF NOT EXISTS warns (
    chat_id INTEGER,
    user_id INTEGER,
    warn_count INTEGER DEFAULT 0,
    UNIQUE(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS stats (
    chat_id INTEGER,
    user_id INTEGER,
    msg_count INTEGER DEFAULT 0,
    UNIQUE(chat_id, user_id)
);
""")
conn.commit()

spam_tracker = defaultdict(list)

# =========================
# HELPERS
# =========================

URL_PATTERN = re.compile(
    r"(?i)\b(?:https?://|www\.|t\.me/|telegram\.me/|discord\.gg/|discord\.com/invite/|[a-z0-9-]+\.(com|net|org|gg|me|io|xyz)\S*)"
)

def is_private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")

def ensure_chat_settings(chat_id: int):
    cursor.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
    conn.commit()

def parse_time(time_str: str):
    if not time_str or len(time_str) < 2:
        return None
    unit = time_str[-1].lower()
    value = time_str[:-1]
    if not value.isdigit():
        return None
    val = int(value)
    if unit == "m":
        return val * 60
    if unit == "h":
        return val * 3600
    if unit == "d":
        return val * 86400
    return None

async def get_member_status(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        return await context.bot.get_chat_member(chat_id, user_id)
    except Exception:
        return None

async def is_admin_user(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await get_member_status(chat_id, user_id, context)
    if not member:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_private(update):
        if update.message:
            await update.message.reply_text("Bu komut grupta kullanılmalı.")
        return False

    ok = await is_admin_user(update.effective_chat.id, update.effective_user.id, context)
    if not ok and update.message:
        await update.message.reply_text("Bu komutu sadece grup yöneticileri kullanabilir.")
    return ok

async def bot_rights(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        me = await context.bot.get_me()
        return await context.bot.get_chat_member(chat_id, me.id)
    except Exception:
        return None

async def bot_can_delete(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await bot_rights(chat_id, context)
    if not member:
        return False
    return getattr(member, "can_delete_messages", False) or member.status == ChatMemberStatus.OWNER

async def bot_can_restrict(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await bot_rights(chat_id, context)
    if not member:
        return False
    return getattr(member, "can_restrict_members", False) or member.status == ChatMemberStatus.OWNER

async def bot_can_pin(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await bot_rights(chat_id, context)
    if not member:
        return False
    return getattr(member, "can_pin_messages", False) or member.status == ChatMemberStatus.OWNER

async def actor_can_restrict(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await get_member_status(chat_id, user_id, context)
    if not member:
        return False
    return (
        member.status == ChatMemberStatus.OWNER or
        getattr(member, "can_restrict_members", False)
    )

async def actor_can_delete(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await get_member_status(chat_id, user_id, context)
    if not member:
        return False
    return (
        member.status == ChatMemberStatus.OWNER or
        getattr(member, "can_delete_messages", False)
    )

async def actor_can_pin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await get_member_status(chat_id, user_id, context)
    if not member:
        return False
    return (
        member.status == ChatMemberStatus.OWNER or
        getattr(member, "can_pin_messages", False)
    )

async def send_temp_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, parse_mode=None, seconds: int = 5):
    if not update.effective_chat:
        return
    try:
        msg = await context.bot.send_message(update.effective_chat.id, text, parse_mode=parse_mode)
        await asyncio.sleep(seconds)
        await msg.delete()
    except Exception:
        pass

async def delete_later(msg, seconds=5):
    try:
        await asyncio.sleep(seconds)
        await msg.delete()
    except Exception:
        pass

async def send_log(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    ensure_chat_settings(chat_id)
    cursor.execute("SELECT log_chat_id FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row and row[0]:
        try:
            await context.bot.send_message(row[0], text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"log gönderim hatası: {e}")

def full_unmute_permissions():
    return ChatPermissions(
        can_send_messages=True,
        can_send_polls=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )

def split_text(text: str, size: int = 4000):
    for i in range(0, len(text), size):
        yield text[i:i + size]

def main_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Beni Gruba Ekle", url="https://t.me/KGBKORUMABot?startgroup=true")],
        [
            InlineKeyboardButton("📚 Komutlar", callback_data="menu_help"),
            InlineKeyboardButton("⚙️ Kurulum", callback_data="menu_setup")
        ],
        [
            InlineKeyboardButton("🛡️ Ayarlar", callback_data="menu_settings"),
            InlineKeyboardButton("🆘 Destek", url=SUPPORT_URL)
        ]
    ])

def back_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu_start")]
    ])

def help_menu_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👮 Moderasyon", callback_data="help_mod"),
            InlineKeyboardButton("⚙️ Ayarlar", callback_data="help_settings")
        ],
        [
            InlineKeyboardButton("📌 Diğer", callback_data="help_other"),
            InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu_start")
        ]
    ])

# =========================
# START / HELP / PANELS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Merhaba⁪⁬⁮⁮⁮⁮!\n"
        f"{BOT_USERNAME_TEXT} gruplarınızı kolay ve güvenle yönetmenize yardımcı olması için en eksiksiz Bot!\n\n"
        "👉 Çalışmama izin vermek için beni supergroup'a ekleyin ve yönetici olarak ayarlayın!\n\n"
        "❓ KOMUTLAR NELERDİR?\n"
        "Aşağıdaki menüden komutları ve kurulumu görebilirsiniz."
    )

    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=main_menu_markup(),
            disable_web_page_preview=True
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 <b>Komutlar Menüsü</b>\n\n"
        "Aşağıdaki butonlardan kategori seç:\n"
        "• Moderasyon\n"
        "• Ayarlar\n"
        "• Diğer"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=help_menu_markup())

async def yardim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ <b>Kısa Kurulum Rehberi</b>\n\n"
        "1. Botu grubuna ekle.\n"
        "2. Grubu supergroup yap.\n"
        "3. Botu yönetici yap.\n"
        "4. Şu yetkileri ver:\n"
        "• Mesaj silme\n"
        "• Üyeleri yasaklama\n"
        "• Üyeleri kısıtlama\n"
        "• Mesaj sabitleme\n\n"
        "5. Sonra ayarla:\n"
        "• /setlog <chat_id>\n"
        "• /antilink on\n"
        "• /welcome on\n"
        "• /setwelcome Hoş geldin {first}\n"
        "• /setrules Kuralları buraya yaz\n\n"
        "Bot bu adımlardan sonra aktif çalışır."
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=back_menu_markup())

async def destek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆘 Destek Kanalına Git", url=SUPPORT_URL)]
    ])
    await update.message.reply_text("Destek için butona tıkla.", reply_markup=kb)

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ <b>Ayarlar Menüsü</b>\n\n"
        "/antilink on-off\n"
        "/welcome on-off\n"
        "/setwelcome <mesaj>\n"
        "/setlog [chat_id]\n"
        "/setrules <metin>\n"
        "/badlist\n"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=back_menu_markup())

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "menu_start":
        text = (
            "👋 Merhaba⁪⁬⁮⁮⁮⁮!\n"
            f"{BOT_USERNAME_TEXT} gruplarınızı kolay ve güvenle yönetmenize yardımcı olması için en eksiksiz Bot!\n\n"
            "👉 Çalışmama izin vermek için beni supergroup'a ekleyin ve yönetici olarak ayarlayın!\n\n"
            "❓ KOMUTLAR NELERDİR?\n"
            "Aşağıdaki menüden komutları ve kurulumu görebilirsiniz."
        )
        return await query.message.edit_text(text, reply_markup=main_menu_markup())

    if data == "menu_help":
        text = (
            "📚 <b>Komutlar Menüsü</b>\n\n"
            "Aşağıdaki kategori butonlarına basarak komutları görüntüleyebilirsin."
        )
        return await query.message.edit_text(text, parse_mode="HTML", reply_markup=help_menu_markup())

    if data == "menu_setup":
        text = (
            "⚙️ <b>Kısa Kurulum Rehberi</b>\n\n"
            "1. Botu grubuna ekle.\n"
            "2. Yönetici yap.\n"
            "3. Mesaj silme, yasaklama, kısıtlama ve sabitleme yetkilerini ver.\n"
            "4. Grupta admin olarak şu komutları kullan:\n"
            "• /setlog <chat_id>\n"
            "• /antilink on\n"
            "• /welcome on\n"
            "• /setwelcome Hoş geldin {first}\n"
            "• /setrules Kurallar buraya\n\n"
            "Bu kadar."
        )
        return await query.message.edit_text(text, parse_mode="HTML", reply_markup=back_menu_markup())

    if data == "menu_settings":
        text = (
            "⚙️ <b>Ayar Komutları</b>\n\n"
            "/antilink on-off\n"
            "/welcome on-off\n"
            "/setwelcome <mesaj>\n"
            "/setlog [chat_id]\n"
            "/setrules <metin>\n"
            "/addbad <kelime>\n"
            "/delbad <kelime>\n"
            "/badlist"
        )
        return await query.message.edit_text(text, parse_mode="HTML", reply_markup=back_menu_markup())

    if data == "help_mod":
        text = (
            "👮 <b>Moderasyon Komutları</b>\n\n"
            "/ban [sebep]\n"
            "/tban <10m/1h/1d> [sebep]\n"
            "/unban\n"
            "/kick [sebep]\n"
            "/mute [sebep]\n"
            "/tmute <10m/1h/1d> [sebep]\n"
            "/unmute\n"
            "/warn [sebep]\n"
            "/warns\n"
            "/clearwarns\n"
            "/pin\n"
            "/unpin\n"
            "/purge"
        )
        return await query.message.edit_text(text, parse_mode="HTML", reply_markup=help_menu_markup())

    if data == "help_settings":
        text = (
            "⚙️ <b>Ayar Komutları</b>\n\n"
            "/antilink on-off\n"
            "/welcome on-off\n"
            "/setwelcome <mesaj>\n"
            "/setlog [chat_id]\n"
            "/setrules <metin>\n"
            "/addbad <kelime>\n"
            "/delbad <kelime>\n"
            "/badlist\n"
            "/settings"
        )
        return await query.message.edit_text(text, parse_mode="HTML", reply_markup=help_menu_markup())

    if data == "help_other":
        text = (
            "📌 <b>Diğer Komutlar</b>\n\n"
            "/start\n"
            "/help\n"
            "/yardim\n"
            "/destek\n"
            "/ping\n"
            "/id\n"
            "/userinfo\n"
            "/stats\n"
            "/ara <sorgu>\n"
            "/rules"
        )
        return await query.message.edit_text(text, parse_mode="HTML", reply_markup=help_menu_markup())

# =========================
# BASIC
# =========================

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_t = time.time()
    msg = await update.message.reply_text("Pong...")
    end_t = time.time()
    await msg.edit_text(f"🏓 {round((end_t - start_t) * 1000)} ms")

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else "Yok"
    chat_id = update.effective_chat.id if update.effective_chat else "Yok"
    text = f"👤 User ID: <code>{user_id}</code>\n💬 Chat ID: <code>{chat_id}</code>"
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        text += f"\n🎯 Reply User ID: <code>{update.message.reply_to_message.from_user.id}</code>"
    await update.message.reply_text(text, parse_mode="HTML")

async def userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.effective_user
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user

    text = (
        f"👤 <b>Kullanıcı Bilgisi</b>\n\n"
        f"• ID: <code>{target.id}</code>\n"
        f"• Ad: {html.escape(target.full_name)}\n"
        f"• Kullanıcı adı: @{target.username if target.username else 'yok'}\n"
        f"• Bot mu: {'Evet' if target.is_bot else 'Hayır'}"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# =========================
# RULES
# =========================

async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /setrules <kurallar>")

    actor = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor.status != ChatMemberStatus.OWNER and not getattr(actor, "can_change_info", False):
        return await update.message.reply_text("Kuralları ayarlamak için uygun admin yetkin yok.")

    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    rules_text = " ".join(context.args)

    cursor.execute("UPDATE chat_settings SET rules_text = ? WHERE chat_id = ?", (rules_text, chat_id))
    conn.commit()

    await update.message.reply_text("✅ Grup kuralları güncellendi.")
    await send_log(context, chat_id, f"<b>Eylem:</b> SETRULES\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}")

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    cursor.execute("SELECT rules_text FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    text = row[0] if row and row[0] else "Henüz kurallar ayarlanmadı."
    await update.message.reply_text(f"📜 <b>Grup Kuralları</b>\n\n{html.escape(text)}", parse_mode="HTML")

# =========================
# MODERATION
# =========================

async def mod_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    if not await require_admin(update, context):
        return

    actor_member = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if not actor_member:
        return await update.message.reply_text("Yetki bilgisi alınamadı.")

    if action in ("ban", "tban", "mute", "tmute", "kick", "unban", "unmute", "warn", "clearwarns") and \
       actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False):
        return await update.message.reply_text("Bu işlem için yeterli yönetici yetkin yok.")

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text(f"Bir kullanıcı mesajına yanıt vererek /{action} kullan.")

    chat_id = update.effective_chat.id
    actor = update.effective_user
    target = update.message.reply_to_message.from_user

    if target.id == actor.id:
        return await update.message.reply_text("Kendine bu işlemi uygulayamazsın.")

    me = await context.bot.get_me()
    if target.id == me.id:
        return await update.message.reply_text("Bana bu işlemi uygulayamazsın.")

    if await is_admin_user(chat_id, target.id, context):
        return await update.message.reply_text("Yöneticilere işlem yapamam.")

    reason = "Sebep belirtilmedi."
    duration_secs = None

    if action in ("tban", "tmute"):
        if not context.args:
            return await update.message.reply_text(f"Kullanım: /{action} <10m/1h/1d> [sebep]")
        duration_secs = parse_time(context.args[0])
        if not duration_secs:
            return await update.message.reply_text("Geçersiz süre. Örnek: 10m, 1h, 1d")
        if len(context.args) > 1:
            reason = " ".join(context.args[1:])
    else:
        if context.args:
            reason = " ".join(context.args)

    until_date = None
    if duration_secs:
        until_date = datetime.datetime.now() + datetime.timedelta(seconds=duration_secs)

    try:
        if action in ("ban", "tban"):
            if not await bot_can_restrict(chat_id, context):
                return await update.message.reply_text("Botun ban yetkisi yok.")
            await context.bot.ban_chat_member(chat_id, target.id, until_date=until_date)
            act_text = "banlandı" if action == "ban" else f"{context.args[0]} süreyle banlandı"

        elif action in ("mute", "tmute"):
            if not await bot_can_restrict(chat_id, context):
                return await update.message.reply_text("Botun susturma yetkisi yok.")
            await context.bot.restrict_chat_member(
                chat_id,
                target.id,
                ChatPermissions(
                    can_send_messages=False,
                    can_send_polls=False,
                    can_add_web_page_previews=False,
                    can_invite_users=False
                ),
                until_date=until_date
            )
            act_text = "susturuldu" if action == "mute" else f"{context.args[0]} süreyle susturuldu"

        elif action == "kick":
            if not await bot_can_restrict(chat_id, context):
                return await update.message.reply_text("Botun atma yetkisi yok.")
            await context.bot.ban_chat_member(chat_id, target.id)
            await context.bot.unban_chat_member(chat_id, target.id)
            act_text = "gruptan atıldı"
        else:
            return

        msg = await update.message.reply_text(
            f"🔨 <b>{html.escape(target.full_name)}</b> {act_text}.\n"
            f"👮 <b>Yetkili:</b> {html.escape(actor.full_name)}\n"
            f"📝 <b>Sebep:</b> {html.escape(reason)}",
            parse_mode="HTML"
        )
        context.application.create_task(delete_later(msg, 5))

        await send_log(
            context,
            chat_id,
            f"<b>Eylem:</b> {html.escape(action.upper())}\n"
            f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
            f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
            f"<b>Sebep:</b> {html.escape(reason)}"
        )
    except Exception as e:
        logger.error(f"mod action hatası {action}: {e}")
        await update.message.reply_text("İşlem başarısız oldu. Bot yetkilerini kontrol et.")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "ban")

async def tban(update: Update, context: 
