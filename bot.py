import os
import re
import time
import html
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
    ContextTypes,
    filters,
)
from duckduckgo_search import DDGS

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("BOT_TOKEN")

BOT_USERNAME_TEXT = "@KGBKORUMABot"
SUPPORT_URL = "https://t.me/KGBKORUMA"   # kendi kanal / destek linkinle değiştir
SOURCE_URL = "https://github.com/"       # istersen repo linkini koy
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
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member
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
        member = await context.bot.get_chat_member(chat_id, me.id)
        return member
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

# =========================
# START / HELP / PANELS
# =========================

def start_keyboard(bot_username: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Beni Gruba Ekle", url=f"https://t.me/{bot_username}?startgroup=true")
        ],
        [
            InlineKeyboardButton("📚 Komutlar", url=f"https://t.me/{bot_username}?start=help"),
            InlineKeyboardButton("🆘 Destek", url=SUPPORT_URL)
        ],
        [
            InlineKeyboardButton("ℹ️ Yardım / Kurulum", url=f"https://t.me/{bot_username}?start=yardim")
        ]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_user = await context.bot.get_me()
    deep_arg = context.args[0].lower() if context.args else ""

    if deep_arg == "help":
        return await help_cmd(update, context)

    if deep_arg == "yardim":
        return await yardim(update, context)

    text = (
        "👋 Merhaba⁪⁬⁮⁮⁮⁮!\n"
        f"{BOT_USERNAME_TEXT} gruplarınızı kolay ve güvenle yönetmenize yardımcı olması için en eksiksiz Bot!\n\n"
        "👉 Çalışmama izin vermek için beni supergroup'a ekleyin ve yönetici olarak ayarlayın!\n\n"
        "❓ KOMUTLAR NELERDİR?\n"
        "Tüm komutları ve bunların nasıl çalıştığını görmek için /help tuşuna basın!"
    )

    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=start_keyboard(bot_user.username),
            disable_web_page_preview=True
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 <b>Komutlar Menüsü</b>\n\n"
        "<b>Genel Komutlar</b>\n"
        "/start - Bot başlangıç menüsü\n"
        "/help - Komutları gösterir\n"
        "/yardim - Botu kurma / aktif etme rehberi\n"
        "/destek - Destek kanalına yönlendirir\n"
        "/ping - Bot gecikmesini gösterir\n"
        "/id - Kullanıcı / chat id gösterir\n"
        "/ara <sorgu> - Arama yapar\n"
        "/rules - Grup kurallarını gösterir\n\n"

        "<b>Admin Komutları</b>\n"
        "/ban [sebep] - Yanıtlanan kullanıcıyı banlar\n"
        "/tban <10m/1h/1d> [sebep] - Süreli ban\n"
        "/unban - Ban kaldırır\n"
        "/kick [sebep] - Kullanıcıyı gruptan atar\n"
        "/mute [sebep] - Kullanıcıyı susturur\n"
        "/tmute <10m/1h/1d> [sebep] - Süreli susturma\n"
        "/unmute - Susturmayı kaldırır\n"
        "/warn [sebep] - Uyarı verir\n"
        "/warns - Kullanıcının warn sayısını gösterir\n"
        "/clearwarns - Warnları sıfırlar\n"
        "/pin - Mesajı sabitler\n"
        "/unpin - Tüm sabitleri kaldırır\n\n"

        "<b>Koruma / Ayar Komutları</b>\n"
        "/antilink on/off - Link koruması\n"
        "/addbad <kelime> - Yasaklı kelime ekler\n"
        "/delbad <kelime> - Yasaklı kelime siler\n"
        "/badlist - Yasaklı kelime listesini gösterir\n"
        "/welcome on/off - Karşılama aç/kapat\n"
        "/setwelcome <metin> - Karşılama mesajını değiştirir\n"
        "/setlog - Mevcut sohbeti log sohbeti yapar\n"
        "/setrules <metin> - Grup kurallarını ayarlar\n\n"

        "<b>Not</b>\n"
        "Admin komutlarını sadece yöneticiler kullanabilir.\n"
        "Botun işlem yapabilmesi için gerekli yönetici yetkileri verilmelidir."
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆘 Destek", url=SUPPORT_URL),
            InlineKeyboardButton("ℹ️ Kurulum", callback_data="noop")
        ],
        [
            InlineKeyboardButton("➕ Gruba Ekle", url=f"https://t.me/{(await context.bot.get_me()).username}?startgroup=true")
        ]
    ])

    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def yardim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ <b>Bot Nasıl Aktif Edilir?</b>\n\n"
        "1. Beni grubunuza ekleyin.\n"
        "2. Grubu <b>supergroup</b> yapın.\n"
        "3. Beni <b>yönetici</b> yapın.\n"
        "4. Şu yetkileri verin:\n"
        "• Mesaj silme\n"
        "• Üyeleri yasaklama\n"
        "• Üyeleri kısıtlama\n"
        "• Mesaj sabitleme\n\n"
        "5. Sonra admin olarak ayar komutlarını kullanın:\n"
        "• /antilink on\n"
        "• /welcome on\n"
        "• /setwelcome <mesaj>\n"
        "• /setlog\n"
        "• /setrules <kurallar>\n\n"
        "Böylece bot tam aktif şekilde çalışır."
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆘 Destek Kanalı", url=SUPPORT_URL)
        ],
        [
            InlineKeyboardButton("➕ Beni Gruba Ekle", url=f"https://t.me/{(await context.bot.get_me()).username}?startgroup=true")
        ]
    ])

    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def destek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆘 Destek Kanalına Git", url=SUPPORT_URL)]
    ])
    await update.message.reply_text(
        "Destek almak için aşağıdaki butona tıkla.",
        reply_markup=kb
    )

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

# =========================
# RULES
# =========================

async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /setrules <kurallar>")

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
                return await update.message.reply_text("Ban atmak için yetkim yok.")
            await context.bot.ban_chat_member(chat_id, target.id, until_date=until_date)
            act_text = "banlandı" if action == "ban" else f"{context.args[0]} süreyle banlandı"

        elif action in ("mute", "tmute"):
            if not await bot_can_restrict(chat_id, context):
                return await update.message.reply_text("Susturmak için yetkim yok.")
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
                return await update.message.reply_text("Kullanıcıyı atmak için yetkim yok.")
            await context.bot.ban_chat_member(chat_id, target.id)
            await context.bot.unban_chat_member(chat_id, target.id)
            act_text = "gruptan atıldı"

        else:
            return

        msg = (
            f"🔨 <b>{html.escape(target.full_name)}</b> {act_text}.\n"
            f"👮 <b>Yetkili:</b> {html.escape(actor.full_name)}\n"
            f"📝 <b>Sebep:</b> {html.escape(reason)}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
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

async def tban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "tban")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "mute")

async def tmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "tmute")

async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "kick")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /unban kullan.")

    chat_id = update.effective_chat.id
    target = update.message.reply_to_message.from_user

    if not await bot_can_restrict(chat_id, context):
        return await update.message.reply_text("Ban kaldırmak için yetkim yok.")

    try:
        await context.bot.unban_chat_member(chat_id, target.id)
        await update.message.reply_text(f"✅ {target.full_name} için ban kaldırıldı.")
        await send_log(
            context,
            chat_id,
            f"<b>Eylem:</b> UNBAN\n<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}"
        )
    except Exception:
        await update.message.reply_text("Ban kaldırılamadı.")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /unmute kullan.")

    chat_id = update.effective_chat.id
    target = update.message.reply_to_message.from_user

    if not await bot_can_restrict(chat_id, context):
        return await update.message.reply_text("Susturma kaldırmak için yetkim yok.")

    try:
        await context.bot.restrict_chat_member(chat_id, target.id, full_unmute_permissions())
        await update.message.reply_text(f"🔊 {target.full_name} için susturma kaldırıldı.")
        await send_log(
            context,
            chat_id,
            f"<b>Eylem:</b> UNMUTE\n<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}"
        )
    except Exception as e:
        logger.error(f"unmute hatası: {e}")
        await update.message.reply_text("Susturma kaldırılamadı.")

# =========================
# WARNS
# =========================

async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /warn kullan.")

    chat_id = update.effective_chat.id
    actor = update.effective_user
    target = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "Sebep belirtilmedi."

    if await is_admin_user(chat_id, target.id, context):
        return await update.message.reply_text("Yöneticilere warn veremem.")

    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
    row = cursor.fetchone()
    current_warns = (row[0] if row else 0) + 1

    cursor.execute(
        "INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)",
        (chat_id, target.id, current_warns)
    )
    conn.commit()

    await update.message.reply_text(
        f"⚠️ <b>{html.escape(target.full_name)}</b> warn aldı. ({current_warns}/{MAX_WARNS})\n"
        f"📝 <b>Sebep:</b> {html.escape(reason)}",
        parse_mode="HTML"
    )

    await send_log(
        context,
        chat_id,
        f"<b>Eylem:</b> WARN\n"
        f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
        f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
        f"<b>Sebep:</b> {html.escape(reason)}\n"
        f"<b>Toplam Warn:</b> {current_warns}/{MAX_WARNS}"
    )

    if current_warns >= MAX_WARNS:
        if not await bot_can_restrict(chat_id, context):
            return await update.message.reply_text("Warn limiti doldu ama ban atmak için yetkim yok.")
        try:
            await context.bot.ban_chat_member(chat_id, target.id)
            cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
            conn.commit()
            await update.message.reply_text(f"🚫 {target.full_name} {MAX_WARNS} warn nedeniyle banlandı.")
            await send_log(
                context,
                chat_id,
                f"<b>Eylem:</b> AUTO BAN\n<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n<b>Neden:</b> Max warn"
            )
        except Exception as e:
            logger.error(f"warn auto ban hatası: {e}")
            await update.message.reply_text("Warn limiti doldu ama ban işlemi başarısız oldu.")

async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /warns kullan.")

    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
    row = cursor.fetchone()
    count = row[0] if row else 0

    await update.message.reply_text(f"📌 {target.full_name} warn sayısı: {count}/{MAX_WARNS}")

async def clearwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /clearwarns kullan.")

    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
    conn.commit()

    await update.message.reply_text(f"🧽 {target.full_name} warnları sıfırlandı.")
    await send_log(
        context,
        chat_id,
        f"<b>Eylem:</b> CLEARWARNS\n<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}"
    )

# =========================
# SETTINGS
# =========================

async def toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, label: str):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        return await update.message.reply_text(f"Kullanım: /{field} on veya /{field} off")

    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)

    val = 1 if context.args[0].lower() == "on" else 0
    cursor.execute(f"UPDATE chat_settings SET {field} = ? WHERE chat_id = ?", (val, chat_id))
    conn.commit()

    state = "açıldı ✅" if val else "kapatıldı ❌"
    await update.message.reply_text(f"⚙️ {label} {state}.")
    await send_log(
        context,
        chat_id,
        f"<b>Eylem:</b> SETTING\n<b>Ayar:</b> {html.escape(field)}\n<b>Durum:</b> {'ON' if val else 'OFF'}\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}"
    )

async def antilink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "antilink", "Antilink koruması")

async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "welcome", "Karşılama sistemi")

async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /setwelcome <mesaj>")

    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    text = " ".join(context.args)

    cursor.execute("UPDATE chat_settings SET welcome_text = ? WHERE chat_id = ?", (text, chat_id))
    conn.commit()

    await update.message.reply_text("✅ Karşılama mesajı güncellendi.")
    await send_log(
        context,
        chat_id,
        f"<b>Eylem:</b> SETWELCOME\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}\n<b>Mesaj:</b> {html.escape(text)}"
    )

async def setlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)

    cursor.execute("UPDATE chat_settings SET log_chat_id = ? WHERE chat_id = ?", (chat_id, chat_id))
    conn.commit()

    await update.message.reply_text("✅ Bu sohbet log sohbeti olarak ayarlandı.")
    await send_log(
        context,
        chat_id,
        f"<b>Eylem:</b> SETLOG\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}"
    )

# =========================
# BADWORDS
# =========================

async def addbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /addbad <kelime>")

    word = context.args[0].lower().strip()
    chat_id = update.effective_chat.id

    cursor.execute("INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?, ?)", (chat_id, word))
    conn.commit()

    await update.message.reply_text(f"✅ Yasaklı kelime eklendi: <code>{html.escape(word)}</code>", parse_mode="HTML")

async def delbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /delbad <kelime>")

    word = context.args[0].lower().strip()
    chat_id = update.effective_chat.id

    cursor.execute("DELETE FROM badwords WHERE chat_id = ? AND word = ?", (chat_id, word))
    conn.commit()

    await update.message.reply_text(f"🗑️ Silindi: <code>{html.escape(word)}</code>", parse_mode="HTML")

async def badlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    chat_id = update.effective_chat.id

    cursor.execute("SELECT word FROM badwords WHERE chat_id = ? ORDER BY word ASC", (chat_id,))
    rows = cursor.fetchall()

    if not rows:
        return await update.message.reply_text("Liste boş.")

    text = "🧱 <b>Yasaklı Kelimeler</b>\n\n" + "\n".join(f"• <code>{html.escape(r[0])}</code>" for r in rows)
    for part in split_text(text):
        await update.message.reply_text(part, parse_mode="HTML")

# =========================
# PIN
# =========================

async def pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /pin kullan.")
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Mesaj sabitlemek için yetkim yok.")

    try:
        await context.bot.pin_chat_message(
            update.effective_chat.id,
            update.message.reply_to_message.message_id,
            disable_notification=True
        )
        await update.message.reply_text("📌 Mesaj sabitlendi.")
    except Exception:
        await update.message.reply_text("Mesaj sabitlenemedi.")

async def unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Sabit kaldırmak için yetkim yok.")

    try:
        await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        await update.message.reply_text("📍 Tüm sabit mesajlar kaldırıldı.")
    except Exception:
        await update.message.reply_text("Sabit mesajlar kaldırılamadı.")

# =========================
# SEARCH
# =========================

async def ara(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Kullanım: /ara <sorgu>")

    query = " ".join(context.args)
    msg = await update.message.reply_text("🔍 Aranıyor...")

    try:
        lines = []
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=5)
            for i, item in enumerate(results, start=1):
                title = item.get("title", "Başlıksız")
                href = item.get("href", "")
                body = item.get("body", "")
                block = f"{i}. {title}\n"
                if body:
                    block += f"{body}\n"
                if href:
                    block += f"{href}\n"
                lines.append(block.strip())

        if not lines:
            return await msg.edit_text("Sonuç bulunamadı.")

        text = "🔎 Arama Sonuçları\n\n" + "\n\n".join(lines)
        parts = list(split_text(text))
        await msg.edit_text(parts[0], disable_web_page_preview=True)
        for part in parts[1:]:
            await update.message.reply_text(part, disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"arama hatası: {e}")
        await msg.edit_text("Arama sırasında hata oluştu.")

# =========================
# MESSAGE HANDLER
# =========================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return

    if update.effective_user.is_bot:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # welcome
    if update.message.new_chat_members:
        ensure_chat_settings(chat_id)
        cursor.execute("SELECT welcome, welcome_text FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        if row and row[0] == 1:
            for new_member in update.message.new_chat_members:
                if not new_member.is_bot:
                    text = row[1].replace("{first}", new_member.first_name or "")
                    try:
                        await update.message.reply_text(
                            f"👋 {new_member.mention_html()}\n{text}",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        return

    if is_private(update):
        return

    text = (update.message.text or update.message.caption or "").strip()
    lowered = text.lower()

    user_is_admin = await is_admin_user(chat_id, user_id, context)
    if user_is_admin:
        return

    delete_reason = None

    # antilink
    ensure_chat_settings(chat_id)
    cursor.execute("SELECT antilink FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row and row[0] == 1 and text and URL_PATTERN.search(text):
        delete_reason = "Link paylaşımı"

    # badwords
    if not delete_reason and lowered:
        cursor.execute("SELECT word FROM badwords WHERE chat_id = ?", (chat_id,))
        rows = cursor.fetchall()
        for r in rows:
            w = r[0]
            if re.search(rf"\b{re.escape(w)}\b", lowered, re.IGNORECASE):
                delete_reason = "Yasaklı kelime"
                break

    # spam
    if not delete_reason:
        now = time.time()
        spam_tracker[(chat_id, user_id)].append(now)
        spam_tracker[(chat_id, user_id)] = [
            t for t in spam_tracker[(chat_id, user_id)] if now - t < SPAM_WINDOW
        ]
        if len(spam_tracker[(chat_id, user_id)]) > SPAM_LIMIT:
            delete_reason = "Spam / flood"
            spam_tracker[(chat_id, user_id)].clear()

    if delete_reason:
        try:
            if await bot_can_delete(chat_id, context):
                await update.message.delete()
                warn_msg = await context.bot.send_message(
                    chat_id,
                    f"⚠️ {update.effective_user.mention_html()} mesajı silindi.\nNeden: <b>{html.escape(delete_reason)}</b>",
                    parse_mode="HTML"
                )
                await send_log(
                    context,
                    chat_id,
                    f"<b>Eylem:</b> AUTO DELETE\n"
                    f"<b>Kullanıcı:</b> {html.escape(update.effective_user.full_name)} (<code>{user_id}</code>)\n"
                    f"<b>Neden:</b> {html.escape(delete_reason)}\n"
                    f"<b>Mesaj:</b> {html.escape(text[:200])}"
                )
        except Exception as e:
            logger.error(f"otomatik silme hatası: {e}")

# =========================
# MAIN
# =========================

def main():
    if not TOKEN:
        print("HATA: BOT_TOKEN tanımlı değil.")
        print("Windows: set BOT_TOKEN=token")
        print("Linux/Mac: export BOT_TOKEN=token")
        return

    app = Application.builder().token(TOKEN).build()

    # genel
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("yardim", yardim))
    app.add_handler(CommandHandler("destek", destek))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("ara", ara))
    app.add_handler(CommandHandler("rules", rules))

    # moderasyon
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("tban", tban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("kick", kick))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("tmute", tmute))
    app.add_handler(CommandHandler("unmute", unmute))

    # warns
    app.add_handler(CommandHandler("warn", warn))
    app.add_handler(CommandHandler("warns", warns_cmd))
    app.add_handler(CommandHandler("clearwarns", clearwarns))

    # settings
    app.add_handler(CommandHandler("antilink", antilink_cmd))
    app.add_handler(CommandHandler("welcome", welcome_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome))
    app.add_handler(CommandHandler("setlog", setlog))
    app.add_handler(CommandHandler("setrules", setrules))

    # badwords
    app.add_handler(CommandHandler("addbad", addbad))
    app.add_handler(CommandHandler("delbad", delbad))
    app.add_handler(CommandHandler("badlist", badlist))

    # pin
    app.add_handler(CommandHandler("pin", pin))
    app.add_handler(CommandHandler("unpin", unpin))

    # messages
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    print("BOT ÇALIŞIYOR...")
    app.run_polling()

if __name__ == "__main__":
    main()
