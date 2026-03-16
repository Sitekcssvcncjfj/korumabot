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

def split_text(text: str, size: int = 4000):
    for i in range(0, len(text), size):
        yield text[i:i + size]

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

async def bot_can_promote(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await bot_rights(chat_id, context)
    if not member:
        return False
    return getattr(member, "can_promote_members", False) or member.status == ChatMemberStatus.OWNER

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

def normalize_dot_command(text: str):
    if not text:
        return None, []
    text = text.strip()
    if not text.startswith("."):
        return None, []
    parts = text[1:].split()
    if not parts:
        return None, []
    cmd = parts[0].lower()
    args = parts[1:]
    return cmd, args

def main_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Beni Gruba Ekle", url=f"https://t.me/{BOT_USERNAME_TEXT.replace('@', '')}?startgroup=true")],
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"👋 Merhaba!\n"
        f"{BOT_USERNAME_TEXT} gruplarınızı kolay ve güvenle yönetmenize yardımcı olması için en eksiksiz Bot!\n\n"
        "👉 Çalışmama izin vermek için beni supergroup'a ekleyin ve yönetici olarak ayarlayın!\n\n"
        "❓ Tüm komutları görmek için aşağıdaki menüyü kullanın."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_markup(), parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_start":
        text = (
            f"👋 Merhaba!\n"
            f"{BOT_USERNAME_TEXT} gruplarınızı kolay ve güvenle yönetmenize yardımcı olması için en eksiksiz Bot!\n\n"
            "👉 Çalışmama izin vermek için beni supergroup'a ekleyin ve yönetici olarak ayarlayın!\n\n"
            "❓ Tüm komutları görmek için aşağıdaki menüyü kullanın."
        )
        await query.message.edit_text(text, reply_markup=main_menu_markup(), parse_mode="HTML")

    elif data == "menu_help":
        text = "📚 Aşağıdaki kategorilere tıklayarak komutları keşfedin:"
        await query.message.edit_text(text, reply_markup=help_menu_markup(), parse_mode="HTML")

    elif data == "menu_setup":
        text = (
            "🔧 <b>Kurulum Rehberi</b>\n\n"
            "1️⃣ Botu gruba ekleyin.\n"
            "2️⃣ Grubu <b>supergroup</b> yapın.\n"
            "3️⃣ Botu <b>yönetici</b> yapın.\n"
            "4️⃣ Şu yetkileri verin:\n"
            "   • Mesaj silme\n"
            "   • Üyeleri yasaklama\n"
            "   • Üyeleri kısıtlama\n"
            "   • Mesaj sabitleme\n"
            "   • Üyeleri yönetme (admin verme/alma için)\n\n"
            "5️⃣ Ardından şu komutları çalıştırın:\n"
            "   • /antilink on\n"
            "   • /welcome on\n"
            "   • /setwelcome Hoş geldin {first}\n"
            "   • /setlog\n"
            "   • /setrules Kurallar buraya\n\n"
            "✅ Artık botunuz tam aktif!"
        )
        await query.message.edit_text(text, reply_markup=back_menu_markup(), parse_mode="HTML")

    elif data == "menu_settings":
        text = (
            "⚙️ <b>Ayarlar Menüsü</b>\n\n"
            "• /antilink on/off\n"
            "• /welcome on/off\n"
            "• /setwelcome <mesaj>\n"
            "• /setlog [chat_id]\n"
            "• /setrules <metin>\n"
            "• /addbad <kelime>\n"
            "• /delbad <kelime>\n"
            "• /badlist\n"
            "• /settings"
        )
        await query.message.edit_text(text, reply_markup=back_menu_markup(), parse_mode="HTML")

    elif data == "help_mod":
        text = (
            "👮 <b>Moderasyon Komutları</b>\n\n"
            "Hem / hem . ile çalışır.\n\n"
            "• /ban veya .ban\n"
            "• /tban veya .tban\n"
            "• /unban veya .unban\n"
            "• /kick veya .kick\n"
            "• /mute veya .mute\n"
            "• /tmute veya .tmute\n"
            "• /unmute veya .unmute\n"
            "• /warn veya .warn\n"
            "• /warns veya .warns\n"
            "• /clearwarns veya .clearwarns\n"
            "• /pin veya .pin\n"
            "• /unpin veya .unpin\n"
            "• /purge veya .purge\n"
            "• /admin veya .admin\n"
            "• /unadmin veya .unadmin"
        )
        await query.message.edit_text(text, reply_markup=help_menu_markup(), parse_mode="HTML")

    elif data == "help_settings":
        text = (
            "⚙️ <b>Ayar Komutları</b>\n\n"
            "• /antilink veya .antilink\n"
            "• /welcome veya .welcome\n"
            "• /setwelcome veya .setwelcome\n"
            "• /setlog veya .setlog\n"
            "• /setrules veya .setrules\n"
            "• /addbad veya .addbad\n"
            "• /delbad veya .delbad\n"
            "• /badlist veya .badlist\n"
            "• /settings veya .settings"
        )
        await query.message.edit_text(text, reply_markup=help_menu_markup(), parse_mode="HTML")

    elif data == "help_other":
        text = (
            "📌 <b>Diğer Komutlar</b>\n\n"
            "• /start\n"
            "• /help\n"
            "• /yardim\n"
            "• /destek\n"
            "• /ping veya .ping\n"
            "• /id veya .id\n"
            "• /userinfo veya .userinfo\n"
            "• /stats veya .stats\n"
            "• /ara veya .ara\n"
            "• /rules veya .rules"
        )
        await query.message.edit_text(text, reply_markup=help_menu_markup(), parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 Aşağıdaki kategorilere tıklayarak komutları keşfedin:", reply_markup=help_menu_markup(), parse_mode="HTML")

async def yardim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔧 <b>Kurulum Rehberi</b>\n\n"
        "1. Botu gruba ekle\n"
        "2. Yönetici yap\n"
        "3. Gerekli yetkileri ver\n"
        "4. Ayar komutlarını çalıştır\n\n"
        "Not: Komutları hem /ban hem .ban şeklinde kullanabilirsin."
    )
    await update.message.reply_text(text, reply_markup=back_menu_markup(), parse_mode="HTML")

async def destek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🆘 Destek Kanalı", url=SUPPORT_URL)]])
    await update.message.reply_text("Destek için butona tıkla.", reply_markup=kb)

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
        f"• Username: @{target.username if target.username else 'yok'}\n"
        f"• Bot: {'Evet' if target.is_bot else 'Hayır'}"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    chat_id = update.effective_chat.id
    cursor.execute("SELECT COUNT(*) FROM badwords WHERE chat_id = ?", (chat_id,))
    bad_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM warns WHERE chat_id = ? AND warn_count > 0", (chat_id,))
    warned_users = cursor.fetchone()[0]
    cursor.execute("SELECT SUM(msg_count) FROM stats WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    total_msgs = row[0] if row and row[0] else 0
    text = (
        "📊 <b>Grup İstatistikleri</b>\n\n"
        f"• Toplam mesaj: <code>{total_msgs}</code>\n"
        f"• Warnlı kullanıcı: <code>{warned_users}</code>\n"
        f"• Yasaklı kelime: <code>{bad_count}</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    cursor.execute("SELECT rules_text FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    text = row[0] if row and row[0] else "Henüz kurallar ayarlanmadı."
    await update.message.reply_text(f"📜 <b>Grup Kuralları</b>\n\n{html.escape(text)}", parse_mode="HTML")

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
                lines.append(f"{i}. {title}\n{body[:100]}\n{href}")
        if not lines:
            return await msg.edit_text("Sonuç bulunamadı.")
        text = "🔎 Arama Sonuçları\n\n" + "\n\n".join(lines)
        await msg.edit_text(text[:4000], disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"arama hatası: {e}")
        await msg.edit_text("Arama sırasında hata oluştu.")

async def mod_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    if not await require_admin(update, context):
        return
    actor_member = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if not actor_member:
        return await update.message.reply_text("Yetki bilgisi alınamadı.")
    if actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False):
        return await update.message.reply_text("Bu işlem için yetkin yok.")
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text(f"Bir kullanıcı mesajına yanıt vererek /{action} kullan.")

    chat_id = update.effective_chat.id
    actor = update.effective_user
    target = update.message.reply_to_message.from_user

    if target.id == actor.id:
        return await update.message.reply_text("Kendine işlem yapamazsın.")
    me = await context.bot.get_me()
    if target.id == me.id:
        return await update.message.reply_text("Bana işlem yapamazsın.")
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

    until_date = datetime.datetime.now() + datetime.timedelta(seconds=duration_secs) if duration_secs else None

    try:
        if action in ("ban", "tban"):
            if not await bot_can_restrict(chat_id, context):
                return await update.message.reply_text("Botun ban yetkisi yok.")
            await context.bot.ban_chat_member(chat_id, target.id, until_date=until_date)
            act_text = "banlandı" if action == "ban" else f"{context.args[0]} süreyle banlandı"
        elif action in ("mute", "tmute"):
            if not await bot_can_restrict(chat_id, context):
                return await update.message.reply_text("Botun susturma yetkisi yok.")
            await context.bot.restrict_chat_member(chat_id, target.id, ChatPermissions(can_send_messages=False), until_date=until_date)
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
            f"🔨 <b>{html.escape(target.full_name)}</b> {act_text}.\n👮 <b>Yetkili:</b> {html.escape(actor.full_name)}\n📝 <b>Sebep:</b> {html.escape(reason)}",
            parse_mode="HTML"
        )
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, chat_id, f"<b>Eylem:</b> {action.upper()}\n<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n<b>Yetkili:</b> {html.escape(actor.full_name)}\n<b>Sebep:</b> {html.escape(reason)}")
    except Exception as e:
        logger.error(f"mod action hatası {action}: {e}")
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
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /unban kullan.")
    chat_id = update.effective_chat.id
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.unban_chat_member(chat_id, target.id)
        msg = await update.message.reply_text(f"✅ {target.full_name} için ban kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, chat_id, f"<b>Eylem:</b> UNBAN\n<b>Hedef:</b> {html.escape(target.full_name)}\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}")
    except Exception:
        await update.message.reply_text("Ban kaldırılamadı.")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /unmute kullan.")
    chat_id = update.effective_chat.id
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(chat_id, target.id, full_unmute_permissions())
        msg = await update.message.reply_text(f"🔊 {target.full_name} için susturma kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, chat_id, f"<b>Eylem:</b> UNMUTE\n<b>Hedef:</b> {html.escape(target.full_name)}\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}")
    except Exception:
        await update.message.reply_text("Susturma kaldırılamadı.")

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not await bot_can_promote(update.effective_chat.id, context):
        return await update.message.reply_text("Botun admin verme yetkisi yok.")
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /admin kullan.")
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.promote_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            can_manage_chat=True,
            can_delete_messages=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=True,
            can_manage_video_chats=True,
        )
        msg = await update.message.reply_text(f"👑 {target.full_name} admin yapıldı.")
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, update.effective_chat.id, f"<b>Eylem:</b> ADMIN VER\n<b>Hedef:</b> {html.escape(target.full_name)}\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}")
    except Exception as e:
        logger.error(f"admin verme hatası: {e}")
        await update.message.reply_text("Kullanıcı admin yapılamadı.")

async def unadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not await bot_can_promote(update.effective_chat.id, context):
        return await update.message.reply_text("Botun admin alma yetkisi yok.")
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /unadmin kullan.")
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.promote_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            can_manage_chat=False,
            can_delete_messages=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_manage_video_chats=False,
        )
        msg = await update.message.reply_text(f"⬇️ {target.full_name} adminlikten alındı.")
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, update.effective_chat.id, f"<b>Eylem:</b> ADMIN AL\n<b>Hedef:</b> {html.escape(target.full_name)}\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}")
    except Exception as e:
        logger.error(f"admin alma hatası: {e}")
        await update.message.reply_text("Kullanıcının adminliği alınamadı.")

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
    cursor.execute("INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)", (chat_id, target.id, current_warns))
    conn.commit()

    msg = await update.message.reply_text(f"⚠️ <b>{html.escape(target.full_name)}</b> warn aldı. ({current_warns}/{MAX_WARNS})\n📝 <b>Sebep:</b> {html.escape(reason)}", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))
    await send_log(context, chat_id, f"<b>Eylem:</b> WARN\n<b>Hedef:</b> {html.escape(target.full_name)}\n<b>Yetkili:</b> {html.escape(actor.full_name)}\n<b>Sebep:</b> {html.escape(reason)}\n<b>Warn:</b> {current_warns}/{MAX_WARNS}")

    if current_warns >= MAX_WARNS:
        try:
            await context.bot.ban_chat_member(chat_id, target.id)
            cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
            conn.commit()
            await send_log(context, chat_id, f"<b>Eylem:</b> AUTO BAN\n<b>Hedef:</b> {html.escape(target.full_name)}\n<b>Neden:</b> Max warn")
        except Exception:
            pass

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
    msg = await update.message.reply_text(f"🧽 {target.full_name} warnları sıfırlandı.")
    context.application.create_task(delete_later(msg, 5))
    await send_log(context, chat_id, f"<b>Eylem:</b> CLEARWARNS\n<b>Hedef:</b> {html.escape(target.full_name)}\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}")

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
    msg = await update.message.reply_text(f"⚙️ {label} {state}.")
    context.application.create_task(delete_later(msg, 5))

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
    msg = await update.message.reply_text("✅ Karşılama mesajı güncellendi.")
    context.application.create_task(delete_later(msg, 5))

async def setlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
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
    msg = await update.message.reply_text(f"✅ Log sohbeti ayarlandı: <code>{target_log_id}</code>", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))

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
    msg = await update.message.reply_text("✅ Grup kuralları güncellendi.")
    context.application.create_task(delete_later(msg, 5))

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    cursor.execute("SELECT antilink, welcome, welcome_text, log_chat_id, rules_text FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if not row:
        return await update.message.reply_text("Ayar bulunamadı.")
    text = (
        "⚙️ <b>Grup Ayarları</b>\n\n"
        f"• Antilink: {'Açık' if row[0] else 'Kapalı'}\n"
        f"• Welcome: {'Açık' if row[1] else 'Kapalı'}\n"
        f"• Log Chat ID: <code>{row[3]}</code>\n"
        f"• Welcome Mesajı: {html.escape(row[2])[:100]}\n"
        f"• Rules: {html.escape(row[4])[:100]}"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def addbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /addbad <kelime>")
    word = context.args[0].lower().strip()
    chat_id = update.effective_chat.id
    cursor.execute("INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?, ?)", (chat_id, word))
    conn.commit()
    msg = await update.message.reply_text(f"✅ Yasaklı kelime eklendi: <code>{html.escape(word)}</code>", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))

async def delbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
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
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    chat_id = update.effective_chat.id
    cursor.execute("SELECT word FROM badwords WHERE chat_id = ? ORDER BY word ASC", (chat_id,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Liste boş.")
    text = "🧱 <b>Yasaklı Kelimeler</b>\n\n" + "\n".join(f"• <code>{html.escape(r[0])}</code>" for r in rows)
    await update.message.reply_text(text[:4000], parse_mode="HTML")

async def pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /pin kullan.")
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Botun sabitleme yetkisi yok.")
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id, disable_notification=True)
        msg = await update.message.reply_text("📌 Mesaj sabitlendi.")
        context.application.create_task(delete_later(msg, 5))
    except Exception:
        await update.message.reply_text("Mesaj sabitlenemedi.")

async def unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Botun sabit kaldırma yetkisi yok.")
    try:
        await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        msg = await update.message.reply_text("📍 Tüm sabit mesajlar kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
    except Exception:
        await update.message.reply_text("Sabit mesajlar kaldırılamadı.")

async def purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not await bot_can_delete(update.effective_chat.id, context):
        return await update.message.reply_text("Botun mesaj silme yetkisi yok.")
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
            except Exception:
                pass
        info = await context.bot.send_message(update.effective_chat.id, f"🧹 {deleted} mesaj temizlendi.")
        context.application.create_task(delete_later(info, 5))
        await send_log(context, update.effective_chat.id, f"<b>Eylem:</b> PURGE\n<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}\n<b>Silinen:</b> {deleted}")
    except Exception as e:
        logger.error(f"purge hatası: {e}")
        await update.message.reply_text("Purge işlemi başarısız oldu.")

# =========================
# DOT COMMAND HANDLER
# =========================

async def dot_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    cmd, args = normalize_dot_command(update.message.text)
    if not cmd:
        return

    context.args = args

    mapping = {
        "ban": ban,
        "tban": tban,
        "unban": unban,
        "kick": kick,
        "mute": mute,
        "tmute": tmute,
        "unmute": unmute,
        "warn": warn,
        "warns": warns_cmd,
        "clearwarns": clearwarns,
        "antilink": antilink_cmd,
        "welcome": welcome_cmd,
        "setwelcome": setwelcome,
        "setlog": setlog,
        "setrules": setrules,
        "settings": settings_cmd,
        "addbad": addbad,
        "delbad": delbad,
        "badlist": badlist,
        "pin": pin,
        "unpin": unpin,
        "purge": purge,
        "ping": ping,
        "id": id_cmd,
        "userinfo": userinfo,
        "stats": stats_cmd,
        "ara": ara,
        "rules": rules,
        "admin": admin_cmd,
        "unadmin": unadmin_cmd,
    }

    func = mapping.get(cmd)
    if func:
        await func(update, context)

# =========================
# MESSAGE HANDLER
# =========================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    if update.effective_user.is_bot:
        return

    if update.message.text and update.message.text.startswith("."):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    cursor.execute(
        "INSERT INTO stats (chat_id, user_id, msg_count) VALUES (?, ?, 1) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET msg_count = msg_count + 1",
        (chat_id, user_id)
    )
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
                    except Exception:
                        pass
        return

    if is_private(update):
        return

    text = (update.message.text or update.message.caption or "").strip()
    lowered = text.lower()

    if await is_admin_user(chat_id, user_id, context):
        return

    delete_reason = None

    ensure_chat_settings(chat_id)
    cursor.execute("SELECT antilink FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row and row[0] == 1 and text and URL_PATTERN.search(text):
        delete_reason = "Link paylaşımı"

    if not delete_reason and lowered:
        cursor.execute("SELECT word FROM badwords WHERE chat_id = ?", (chat_id,))
        rows = cursor.fetchall()
        for r in rows:
            if re.search(rf"\b{re.escape(r[0])}\b", lowered, re.IGNORECASE):
                delete_reason = "Yasaklı kelime"
                break

    if not delete_reason:
        now = time.time()
        spam_tracker[(chat_id, user_id)].append(now)
        spam_tracker[(chat_id, user_id)] = [t for t in spam_tracker[(chat_id, user_id)] if now - t < SPAM_WINDOW]
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
                context.application.create_task(delete_later(warn_msg, 5))
                await send_log(
                    context,
                    chat_id,
                    f"<b>Eylem:</b> AUTO DELETE\n<b>Kullanıcı:</b> {html.escape(update.effective_user.full_name)}\n<b>Neden:</b> {html.escape(delete_reason)}\n<b>Mesaj:</b> {html.escape(text[:100])}"
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("yardim", yardim))
    app.add_handler(CommandHandler("destek", destek))
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
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("unadmin", unadmin_cmd))

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

    app.add_handler(CallbackQueryHandler(callback_handler))

    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\."), dot_command_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    print("✅ BOT ÇALIŞIYOR")
    app.run_polling()

if __name__ == "__main__":
    main()
