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

# =========================
# KEYBOARDS
# =========================

def main_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Komutlar", callback_data="cmd_list")],
        [
            InlineKeyboardButton("⚙️ Kurulum", callback_data="setup_guide"),
            InlineKeyboardButton("🛡️ Ayarlar", callback_data="settings_list")
        ],
        [
            InlineKeyboardButton("🆘 Destek", url=SUPPORT_URL)
        ]
    ])

def back_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Ana Menüye Dön", callback_data="menu_start")]
    ])

def cmd_category_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👮 Moderasyon", callback_data="cmd_mod"),
            InlineKeyboardButton("⚙️ Ayarlar", callback_data="cmd_settings")
        ],
        [
            InlineKeyboardButton("📌 Diğer", callback_data="cmd_other"),
            InlineKeyboardButton("⬅️ Geri", callback_data="menu_start")
        ]
    ])

# =========================
# CALLBACKS - MENU
# =========================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ANA MENU
    if data == "menu_start":
        text = (
            "👋 Merhaba!\n"
            f"{BOT_USERNAME_TEXT} - Gelişmiş Grup Moderasyon Botu\n\n"
            "📌 Özellikler:\n"
            "✅ Ban / Kick / Mute işlemleri\n"
            "✅ Süreli ceza sistemi (1h, 1d vs)\n"
            "✅ Warn sistemi (3 warn = otomatik ban)\n"
            "✅ Anti-spam, Anti-link koruması\n"
            "✅ Yasaklı kelime filtresi\n"
            "✅ Otomatik hoşgeldin mesajı\n"
            "✅ Grup istatistikleri\n"
            "✅ Moderation log sistemi\n\n"
            "Aşağıdaki menülerden başla:"
        )
        await query.message.edit_text(text, reply_markup=main_menu_markup(), parse_mode="HTML")

    # KOMUTLAR KATEGORISI
    elif data == "cmd_list":
        text = (
            "📚 <b>Komut Kategorileri</b>\n\n"
            "Görmek istediğin kategoriyi seç:"
        )
        await query.message.edit_text(text, reply_markup=cmd_category_buttons(), parse_mode="HTML")

    # MODERASYON KOMUTLARI
    elif data == "cmd_mod":
        text = (
            "👮 <b>Moderasyon Komutları</b>\n\n"
            "<b>Ban İşlemleri:</b>\n"
            "/ban [sebep] - Kullanıcıyı yasakla\n"
            "/tban 1h [sebep] - 1 saat süreli ban (10m/1h/1d)\n"
            "/unban - Ban kaldır\n\n"
            "<b>Mute İşlemleri:</b>\n"
            "/mute [sebep] - Kullanıcıyı sustur\n"
            "/tmute 30m [sebep] - 30 dakika süreli susturma\n"
            "/unmute - Susturmayı kaldır\n\n"
            "<b>Diğer Moderation:</b>\n"
            "/kick [sebep] - Gruptan at\n"
            "/warn [sebep] - Uyarı ver\n"
            "/warns - Uyarı sayısını göster\n"
            "/clearwarns - Uyarıları sıfırla\n"
            "/pin - Mesajı sabitle\n"
            "/unpin - Sabitleri kaldır\n"
            "/purge - Mesajları toplu sil\n\n"
            "💡 <b>Nasıl Kullanılır:</b>\n"
            "Komut yazıp yanıt ver (reply) yap\n"
            "Örnek: /ban reklam yaptı"
        )
        await query.message.edit_text(text, reply_markup=cmd_category_buttons(), parse_mode="HTML")

    # AYAR KOMUTLARI
    elif data == "cmd_settings":
        text = (
            "⚙️ <b>Ayar Komutları</b>\n\n"
            "<b>Koruma Ayarları:</b>\n"
            "/antilink on/off - Link paylaşım koruması\n"
            "/welcome on/off - Hoşgeldin mesajı aç/kapat\n"
            "/setwelcome <metin> - Hoşgeldin mesajını ayarla\n\n"
            "<b>Yasaklı Kelimeler:</b>\n"
            "/addbad <kelime> - Yasaklı kelime ekle\n"
            "/delbad <kelime> - Yasaklı kelime sil\n"
            "/badlist - Yasaklı kelimeleri göster\n\n"
            "<b>Grup Ayarları:</b>\n"
            "/setlog [chat_id] - Log kanalı ayarla\n"
            "/setrules <metin> - Grup kurallarını ayarla\n"
            "/rules - Kuralları göster\n"
            "/settings - Tüm ayarları göster\n\n"
            "💡 <b>Nasıl Kullanılır:</b>\n"
            "Admin haklarına göre değişir\n"
            "Örnek: /antilink on"
        )
        await query.message.edit_text(text, reply_markup=cmd_category_buttons(), parse_mode="HTML")

    # DİĞER KOMUTLAR
    elif data == "cmd_other":
        text = (
            "📌 <b>Diğer Komutlar</b>\n\n"
            "<b>Bilgi Komutları:</b>\n"
            "/ping - Bot gecikmesi\n"
            "/id - User/Chat ID göster\n"
            "/userinfo - Kullanıcı bilgisi\n"
            "/stats - Grup istatistikleri\n"
            "/ara <sorgu> - İnternet araması\n"
            "/rules - Grup kurallarını göster\n"
            "/help - Yardım menüsü\n"
            "/start - Başlangıç menüsü\n\n"
            "<b>İpuçları:</b>\n"
            "• /stats ile grup istatistiklerini görebilirsin\n"
            "• /ara ile google araması yapabilirsin\n"
            "• /userinfo ile kullanıcı detaylarını görebilirsin\n\n"
            "Daha fazla için /help yaz"
        )
        await query.message.edit_text(text, reply_markup=cmd_category_buttons(), parse_mode="HTML")

    # KURULUM REHBERI
    elif data == "setup_guide":
        text = (
            "⚙️ <b>Botu Kurma Rehberi</b>\n\n"
            "📍 <b>Adım 1: Botu Gruba Ekle</b>\n"
            "Botu grubuna davet et ve yönetici yap\n\n"
            "📍 <b>Adım 2: Yetkileri Ayarla</b>\n"
            "Botun şu yetkilere ihtiyacı var:\n"
            "✅ Mesaj silme\n"
            "✅ Üyeleri yasaklama\n"
            "✅ Üyeleri kısıtlama\n"
            "✅ Mesaj sabitleme\n"
            "✅ Grup bilgilerini değiştirme\n\n"
            "📍 <b>Adım 3: Log Kanalı Ayarla</b>\n"
            "Ayrı bir kanal açıp ID'sini al:\n"
            "/setlog -1001234567890\n"
            "Veya mevcut grubu log yap:\n"
            "/setlog\n\n"
            "📍 <b>Adım 4: Temel Ayarlar</b>\n"
            "/antilink on - Link koruması aç\n"
            "/welcome on - Hoşgeldin aç\n"
            "/setwelcome Hoş geldin {first}!\n"
            "/setrules Kuralları buraya yaz\n\n"
            "📍 <b>Adım 5: Test Et</b>\n"
            "/stats - İstatistikleri kontrol et\n\n"
            "✨ Tamamlandı! Bot aktif."
        )
        await query.message.edit_text(text, reply_markup=back_button(), parse_mode="HTML")

    # AYARLAR LISTESI
    elif data == "settings_list":
        if is_private(update.effective_chat):
            return await query.answer("Bu komut grupta kullanılmalı.", show_alert=True)
        
        chat_id = update.effective_chat.id
        ensure_chat_settings(chat_id)
        
        cursor.execute("SELECT antilink, welcome, welcome_text, log_chat_id, rules_text FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        
        if not row:
            text = "Ayar bulunamadı."
        else:
            antilink_status = "✅ Açık" if row[0] else "❌ Kapalı"
            welcome_status = "✅ Açık" if row[1] else "❌ Kapalı"
            
            text = (
                "🛡️ <b>Grup Ayarlarınız</b>\n\n"
                "<b>Koruma Ayarları:</b>\n"
                f"• Antilink: {antilink_status}\n"
                f"• Hoşgeldin: {welcome_status}\n\n"
                "<b>Mesajlar:</b>\n"
                f"• Hoşgeldin: {html.escape(row[2])[:80]}\n"
                f"• Kurallar: {html.escape(row[4])[:80] if row[4] else 'Ayarlanmadı'}\n\n"
                "<b>Log Sistemi:</b>\n"
                f"• Log Kanalı ID: <code>{row[3] if row[3] else 'Ayarlanmadı'}</code>\n\n"
                "<b>Değiştirmek için:</b>\n"
                "/antilink on/off\n"
                "/welcome on/off\n"
                "/setwelcome <metin>\n"
                "/setlog [chat_id]\n"
                "/setrules <metin>"
            )
        
        await query.message.edit_text(text, reply_markup=back_button(), parse_mode="HTML")

# =========================
# START / HELP
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "👋 Merhaba!\n"
            f"{BOT_USERNAME_TEXT} - Gelişmiş Grup Moderasyon Botu\n\n"
            "📌 Özellikler:\n"
            "✅ Ban / Kick / Mute işlemleri\n"
            "✅ Süreli ceza sistemi\n"
            "✅ Warn sistemi\n"
            "✅ Anti-spam, Anti-link\n"
            "✅ Yasaklı kelime filtresi\n"
            "✅ Otomatik hoşgeldin\n"
            "✅ Moderation log\n\n"
            "Başlamak için aşağıdaki menüyü kullan:",
            reply_markup=main_menu_markup(),
            parse_mode="HTML"
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 <b>Yardım Menüsü</b>\n\n"
        "Komutlar için: /start\n"
        "Kurulum için: /start → Kurulum\n"
        "Ayarlar için: /start → Ayarlar\n"
        "İstatistikler: /stats\n"
        "Destek: /start → Destek",
        reply_markup=main_menu_markup(),
        parse_mode="HTML"
    )

# =========================
# BASIC COMMANDS
# =========================

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_t = time.time()
    msg = await update.message.reply_text("🔄 Pong...")
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
        f"• Kullanıcı Adı: @{target.username if target.username else 'yok'}\n"
        f"• Bot: {'Evet ✅' if target.is_bot else 'Hayır ❌'}"
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
        return await update.message.reply_text("Yeterli yetkin yok.")

    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    rules_text = " ".join(context.args)

    cursor.execute("UPDATE chat_settings SET rules_text = ? WHERE chat_id = ?", (rules_text, chat_id))
    conn.commit()

    msg = await update.message.reply_text("✅ Kurallar güncellendi.")
    context.application.create_task(delete_later(msg, 5))

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

    if action in ("ban", "tban", "mute", "tmute", "kick", "warn", "clearwarns") and \
       actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False):
        return await update.message.reply_text("Bu işlem için yetkin yok.")

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text(f"Bir mesaja yanıt vererek /{action} kullan.")

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
                ChatPermissions(can_send_messages=False, can_send_polls=False, can_add_web_page_previews=False, can_invite_users=False),
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
        await send_log(context, chat_id, f"<b>Eylem:</b> {action.upper()}\n<b>Hedef:</b> {html.escape(target.full_name)} ({target.id})\n<b>Yetkili:</b> {html.escape(actor.full_name)}\n<b>Sebep:</b> {html.escape(reason)}")
    except Exception as e:
        logger.error(f"mod action hatası: {e}")
        await update.message.reply_text("İşlem başarısız oldu.")

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

    actor_member = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False):
        return await update.message.reply_text("Yetkin yok.")

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir mesaja yanıt verip /unban kullan.")

    chat_id = update.effective_chat.id
    target = update.message.reply_to_message.from_user

    if not await bot_can_restrict(chat_id, context):
        return await update.message.reply_text("Botun yetki yok.")

    try:
        await context.bot.unban_chat_member(chat_id, target.id)
        msg = await update.message.reply_text(f"✅ {target.full_name} ban kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, chat_id, f"<b>Eylem:</b> UNBAN\n<b>Hedef:</b> {html.escape(target.full_name)}\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}")
    except Exception:
        await update.message.reply_text("Ban kaldırılamadı.")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    actor_member = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False):
        return await update.message.reply_text("Yetkin yok.")

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir mesaja yanıt verip /unmute kullan.")

    chat_id = update.effective_chat.id
    target = update.message.reply_to_message.from_user

    if not await bot_can_restrict(chat_id, context):
        return await update.message.reply_text("Botun yetki yok.")

    try:
        await context.bot.restrict_chat_member(chat_id, target.id, full_unmute_permissions())
        msg = await update.message.reply_text(f"🔊 {target.full_name} susturması kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, chat_id, f"<b>Eylem:</b> UNMUTE\n<b>Hedef:</b> {html.escape(target.full_name)}")
    except Exception:
        await update.message.reply_text("Susturma kaldırılamadı.")

# =========================
# WARNS
# =========================

async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    actor_member = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False):
        return await update.message.reply_text("Yetkin yok.")

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir mesaja yanıt verip /warn kullan.")

    chat_id = update.effective_chat.id
    target = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "Sebep belirtilmedi."

    if await is_admin_user(chat_id, target.id, context):
        return await update.message.reply_text("Yöneticilere warn veremem.")

    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
    row = cursor.fetchone()
    current_warns = (row[0] if row else 0) + 1

    cursor.execute("INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)", (chat_id, target.id, current_warns))
    conn.commit()

    msg = await update.message.reply_text(
        f"⚠️ <b>{html.escape(target.full_name)}</b> warn aldı. ({current_warns}/{MAX_WARNS})\n"
        f"📝 <b>Sebep:</b> {html.escape(reason)}",
        parse_mode="HTML"
    )
    context.application.create_task(delete_later(msg, 5))

    if current_warns >= MAX_WARNS:
        if not await bot_can_restrict(chat_id, context):
            return
        try:
            await context.bot.ban_chat_member(chat_id, target.id)
            cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
            conn.commit()
            await send_log(context, chat_id, f"<b>Eylem:</b> AUTO BAN\n<b>Hedef:</b> {html.escape(target.full_name)}\n<b>Neden:</b> Max warn")
        except Exception:
            pass

async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Grupta kullan.")

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir mesaja yanıt verip /warns kullan.")

    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
    row = cursor.fetchone()
    count = row[0] if row else 0

    await update.message.reply_text(f"📌 {target.full_name} warn: {count}/{MAX_WARNS}")

async def clearwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    actor_member = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False):
        return await update.message.reply_text("Yetkin yok.")

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir mesaja yanıt verip /clearwarns kullan.")

    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
    conn.commit()

    msg = await update.message.reply_text(f"🧽 {target.full_name} warnları sıfırlandı.")
    context.application.create_task(delete_later(msg, 5))

# =========================
# SETTINGS
# =========================

async def toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, label: str):
    if not await require_admin(update, context):
        return

    actor = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor.status != ChatMemberStatus.OWNER and not getattr(actor, "can_change_info", False):
        return await update.message.reply_text("Yetkin yok.")

    if not context.args or context.args[0].lower() not in ("on", "off"):
        return await update.message.reply_text(f"Kullanım: /{field} on veya /{field} off")

    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)

    val = 1 if context.args[0].lower() == "on" else 0
    cursor.execute(f"UPDATE chat_settings SET {field} = ? WHERE chat_id = ?", (val, chat_id))
    conn.commit()

    state = "✅ Açık" if val else "❌ Kapalı"
    msg = await update.message.reply_text(f"⚙️ {label} {state}")
    context.application.create_task(delete_later(msg, 5))

async def antilink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "antilink", "Antilink")

async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "welcome", "Hoşgeldin")

async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    actor = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor.status != ChatMemberStatus.OWNER and not getattr(actor, "can_change_info", False):
        return await update.message.reply_text("Yetkin yok.")

    if not context.args:
        return await update.message.reply_text("Kullanım: /setwelcome <mesaj>")

    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    text = " ".join(context.args)

    cursor.execute("UPDATE chat_settings SET welcome_text = ? WHERE chat_id = ?", (text, chat_id))
    conn.commit()

    msg = await update.message.reply_text("✅ Hoşgeldin mesajı güncellendi.")
    context.application.create_task(delete_later(msg, 5))

async def setlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    actor = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor.status != ChatMemberStatus.OWNER and not getattr(actor, "can_change_info", False):
        return await update.message.reply_text("Yetkin yok.")

    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)

    target_log_id = chat_id
    if context.args:
        try:
            target_log_id = int(context.args[0])
        except Exception:
            return await update.message.reply_text("Kullanım: /setlog veya /setlog <chat_id>")

    cursor.execute("UPDATE chat_settings SET log_chat_id = ? WHERE chat_id = ?", (target_log_id, chat_id))
    conn.commit()

    msg = await update.message.reply_text(f"✅ Log kanalı ayarlandı: <code>{target_log_id}</code>", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Grupta kullan.")
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)

    cursor.execute("SELECT antilink, welcome, welcome_text, log_chat_id, rules_text FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    
    antilink_str = "✅" if row[0] else "❌"
    welcome_str = "✅" if row[1] else "❌"

    text = (
        "🛡️ <b>Grup Ayarları</b>\n\n"
        f"Antilink: {antilink_str}\n"
        f"Hoşgeldin: {welcome_str}\n"
        f"Log: <code>{row[3]}</code>\n"
        f"Hoşgeldin Metni: {html.escape(row[2])[:60]}\n"
        f"Kurallar: {html.escape(row[4])[:60]}"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# =========================
# BADWORDS
# =========================

async def addbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    actor = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor.status != ChatMemberStatus.OWNER and not getattr(actor, "can_delete_messages", False):
        return await update.message.reply_text("Yetkin yok.")

    if not context.args:
        return await update.message.reply_text("Kullanım: /addbad <kelime>")

    word = context.args[0].lower().strip()
    chat_id = update.effective_chat.id

    cursor.execute("INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?, ?)", (chat_id, word))
    conn.commit()

    msg = await update.message.reply_text(f"✅ Kelime eklendi: <code>{html.escape(word)}</code>", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))

async def delbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    actor = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor.status != ChatMemberStatus.OWNER and not getattr(actor, "can_delete_messages", False):
        return await update.message.reply_text("Yetkin yok.")

    if not context.args:
        return await update.message.reply_text("Kullanım: /delbad <kelime>")

    word = context.args[0].lower().strip()
    chat_id = update.effective_chat.id

    cursor.execute("DELETE FROM badwords WHERE chat_id = ? AND word = ?", (chat_id, word))
    conn.commit()

    msg = await update.message.reply_text(f"🗑️ Silindi: <code>{html.escape(word)}</code>", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))

async def badlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Grupta kullan.")
    chat_id = update.effective_chat.id

    cursor.execute("SELECT word FROM badwords WHERE chat_id = ? ORDER BY word ASC", (chat_id,))
    rows = cursor.fetchall()

    if not rows:
        return await update.message.reply_text("Liste boş.")

    text = "🧱 <b>Yasaklı Kelimeler</b>\n\n" + "\n".join(f"• <code>{html.escape(r[0])}</code>" for r in rows)
    await update.message.reply_text(text, parse_mode="HTML")

# =========================
# PIN
# =========================

async def pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    actor = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor.status != ChatMemberStatus.OWNER and not getattr(actor, "can_pin_messages", False):
        return await update.message.reply_text("Yetkin yok.")

    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /pin kullan.")
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Botun yetki yok.")

    try:
        await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id, disable_notification=True)
        msg = await update.message.reply_text("📌 Sabitlendi.")
        context.application.create_task(delete_later(msg, 5))
    except Exception:
        await update.message.reply_text("Sabitlenemedi.")

async def unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    actor = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor.status != ChatMemberStatus.OWNER and not getattr(actor, "can_pin_messages", False):
        return await update.message.reply_text("Yetkin yok.")

    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Botun yetki yok.")

    try:
        await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        msg = await update.message.reply_text("📍 Sabitler kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
    except Exception:
        await update.message.reply_text("Kaldırılamadı.")

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
                body = item.get("body", "")[:100]
                lines.append(f"{i}. {title}\n{body}\n{href}")

        if not lines:
            return await msg.edit_text("Sonuç yok.")

        text = "🔎 <b>Arama Sonuçları</b>\n\n" + "\n\n".join(lines)
        for part in split_text(text):
            await update.message.reply_text(part)
        await msg.delete()
    except Exception:
        await msg.edit_text("Hata oluştu.")

# =========================
# STATS / PURGE
# =========================

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Grupta kullan.")

    chat_id = update.effective_chat.id

    cursor.execute("SELECT COUNT(*) FROM badwords WHERE chat_id = ?", (chat_id,))
    bad_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM warns WHERE chat_id = ? AND warn_count > 0", (chat_id,))
    warned_users = cursor.fetchone()[0]

    cursor.execute("SELECT SUM(msg_count) FROM stats WHERE chat_id = ?", (chat_id,))
    total_msgs = cursor.fetchone()[0]
    if total_msgs is None:
        total_msgs = 0

    text = (
        "📊 <b>Grup İstatistikleri</b>\n\n"
        f"📝 Toplam Mesaj: {total_msgs}\n"
        f"⚠️ Warnlı Kullanıcı: {warned_users}\n"
        f"🚫 Yasaklı Kelime: {bad_count}"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    actor = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor.status != ChatMemberStatus.OWNER and not getattr(actor, "can_delete_messages", False):
        return await update.message.reply_text("Yetkin yok.")

    if not await bot_can_delete(update.effective_chat.id, context):
        return await update.message.reply_text("Botun yetki yok.")

    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /purge kullan.")

    try:
        start_id = update.message.reply_to_message.message_id
        end_id = update.message.message_id
        deleted = 0

        for msg_id in range(start_id, end_id + 1):
            try:
                await context.bot.delete_message(update.effective_chat.id, msg_id)
                deleted += 1
            except:
                pass

        info = await update.message.reply_text(f"🧹 {deleted} mesaj silindi.")
        context.application.create_task(delete_later(info, 5))
    except Exception:
        await update.message.reply_text("Hata oluştu.")

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

    cursor.execute("""
        INSERT INTO stats (chat_id, user_id, msg_count) VALUES (?, ?, 1)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET msg_count = msg_count + 1
    """, (chat_id, user_id))
    conn.commit()

    if update.message.new_chat_members:
        ensure_chat_settings(chat_id)
        cursor.execute("SELECT welcome, welcome_text FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        if row and row[0] == 1:
            for new_member in update.message.new_chat_members:
                if not new_member.is_bot:
                    text = row[1].replace("{first}", new_member.first_name or "")
                    try:
                        await update.message.reply_text(f"👋 {new_member.mention_html()}\n{text}", parse_mode="HTML")
                    except:
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

    ensure_chat_settings(chat_id)
    cursor.execute("SELECT antilink FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row and row[0] == 1 and text and URL_PATTERN.search(text):
        delete_reason = "Link"

    if not delete_reason and lowered:
        cursor.execute("SELECT word FROM badwords WHERE chat_id = ?", (chat_id,))
        rows = cursor.fetchall()
        for r in rows:
            if re.search(rf"\b{re.escape(r[0])}\b", lowered, re.IGNORECASE):
                delete_reason = "Yasaklı Kelime"
                break

    if not delete_reason:
        now = time.time()
        spam_tracker[(chat_id, user_id)].append(now)
        spam_tracker[(chat_id, user_id)] = [t for t in spam_tracker[(chat_id, user_id)] if now - t < SPAM_WINDOW]
        if len(spam_tracker[(chat_id, user_id)]) > SPAM_LIMIT:
            delete_reason = "Spam"
            spam_tracker[(chat_id, user_id)].clear()

    if delete_reason:
        try:
            if await bot_can_delete(chat_id, context):
                await update.message.delete()
                warn_msg = await context.bot.send_message(chat_id, f"⚠️ Mesaj silindi: {delete_reason}")
                context.application.create_task(delete_later(warn_msg, 5))
                await send_log(context, chat_id, f"<b>Silinen Mesaj</b>\n<b>Neden:</b> {delete_reason}\n<b>Kullanıcı:</b> {html.escape(update.effective_user.full_name)}")
        except:
            pass

# =========================
# MAIN
# =========================

def main():
    if not TOKEN:
        print("HATA: BOT_TOKEN tanımlı değil.\nWindows: set BOT_TOKEN=token\nLinux/Mac: export BOT_TOKEN=token")
        return

    app = Application.builder().token(TOKEN).build()

    # Butonlar
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Komutlar
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("userinfo", userinfo))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("ara", ara))
    app.add_handler(CommandHandler("rules", rules))

    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("tban", tban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("kick", kick))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("tmute", tmute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("warn", warn))
    app.add_handler(CommandHandler("warns", warns_cmd))
    app.add_handler(CommandHandler("clearwarns", clearwarns))

    app.add_handler(CommandHandler("antilink", antilink_cmd))
    app.add_handler(CommandHandler("welcome", welcome_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome))
    app.add_handler(CommandHandler("setlog", setlog))
    app.add_handler(CommandHandler("setrules", setrules))
    app.add_handler(CommandHandler("settings", settings_cmd))

    app.add_handler(CommandHandler("addbad", addbad))
    app.add_handler(CommandHandler("delbad", delbad))
    app.add_handler(CommandHandler("badlist", badlist))

    app.add_handler(CommandHandler("pin", pin))
    app.add_handler(CommandHandler("unpin", unpin))
    app.add_handler(CommandHandler("purge", purge))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    print("✅ BOT ÇALIŞIYOR...")
    app.run_polling()

if __name__ == "__main__":
    main()
