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
PURGE_LIMIT = 100
MAX_NOTE_NAME = 32
MAX_NOTE_TEXT = 3000
MAX_FILTER_TRIGGER = 64
MAX_FILTER_REPLY = 3000

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("kgb_rose_plus")

conn = sqlite3.connect("bot_database.db", check_same_thread=False)
cursor = conn.cursor()

cursor.executescript("""
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    antilink INTEGER DEFAULT 0,
    welcome INTEGER DEFAULT 0,
    goodbye INTEGER DEFAULT 0,
    welcome_text TEXT DEFAULT 'Gruba hoş geldin!',
    goodbye_text TEXT DEFAULT 'Görüşürüz!',
    log_chat_id INTEGER DEFAULT 0,
    rules_text TEXT DEFAULT 'Henüz kurallar ayarlanmadı.',
    lock_link INTEGER DEFAULT 0,
    lock_badword INTEGER DEFAULT 0,
    lock_flood INTEGER DEFAULT 1
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
    deleted_count INTEGER DEFAULT 0,
    UNIQUE(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS notes (
    chat_id INTEGER,
    note_name TEXT,
    note_text TEXT,
    UNIQUE(chat_id, note_name)
);

CREATE TABLE IF NOT EXISTS filters_table (
    chat_id INTEGER,
    trigger_text TEXT,
    reply_text TEXT,
    UNIQUE(chat_id, trigger_text)
);

CREATE TABLE IF NOT EXISTS mod_stats (
    chat_id INTEGER PRIMARY KEY,
    total_bans INTEGER DEFAULT 0,
    total_mutes INTEGER DEFAULT 0,
    total_warns INTEGER DEFAULT 0,
    total_deleted INTEGER DEFAULT 0
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
    cursor.execute("INSERT OR IGNORE INTO mod_stats (chat_id) VALUES (?)", (chat_id,))
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

def normalize_dot_command(text: str):
    if not text:
        return None, []
    text = text.strip()
    if not text.startswith("."):
        return None, []
    parts = text[1:].split()
    if not parts:
        return None, []
    return parts[0].lower(), parts[1:]

async def get_member_status(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        return await context.bot.get_chat_member(chat_id, user_id)
    except Exception:
        return None

async def is_admin_user(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await get_member_status(chat_id, user_id, context)
    return bool(member and member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER))

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
    m = await bot_rights(chat_id, context)
    return bool(m and (getattr(m, "can_delete_messages", False) or m.status == ChatMemberStatus.OWNER))

async def bot_can_restrict(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    m = await bot_rights(chat_id, context)
    return bool(m and (getattr(m, "can_restrict_members", False) or m.status == ChatMemberStatus.OWNER))

async def bot_can_pin(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    m = await bot_rights(chat_id, context)
    return bool(m and (getattr(m, "can_pin_messages", False) or m.status == ChatMemberStatus.OWNER))

async def bot_can_promote(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    m = await bot_rights(chat_id, context)
    return bool(m and (getattr(m, "can_promote_members", False) or m.status == ChatMemberStatus.OWNER))

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
            logger.error(f"log hatası: {e}")

def full_unmute_permissions():
    return ChatPermissions(
        can_send_messages=True,
        can_send_polls=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )

def inc_mod_stat(chat_id: int, field: str):
    cursor.execute(f"UPDATE mod_stats SET {field} = {field} + 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()

def inc_user_deleted(chat_id: int, user_id: int):
    cursor.execute("""
        INSERT INTO stats (chat_id, user_id, msg_count, deleted_count)
        VALUES (?, ?, 0, 1)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET deleted_count = deleted_count + 1
    """, (chat_id, user_id))
    conn.commit()

def main_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Beni Gruba Ekle", url=f"https://t.me/{BOT_USERNAME_TEXT.replace('@', '')}?startgroup=true")],
        [InlineKeyboardButton("📚 Komutlar", callback_data="menu_help"),
         InlineKeyboardButton("⚙️ Kurulum", callback_data="menu_setup")],
        [InlineKeyboardButton("🛡️ Ayarlar", callback_data="menu_settings"),
         InlineKeyboardButton("🆘 Destek", url=SUPPORT_URL)]
    ])

def back_menu_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu_start")]])

def help_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👮 Moderasyon", callback_data="help_mod"),
         InlineKeyboardButton("⚙️ Ayarlar", callback_data="help_settings")],
        [InlineKeyboardButton("📝 Notes/Filters", callback_data="help_notes"),
         InlineKeyboardButton("📌 Diğer", callback_data="help_other")],
        [InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu_start")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"👋 Merhaba!\n"
        f"{BOT_USERNAME_TEXT} gruplarınızı kolay ve güvenle yönetmenize yardımcı olması için gelişmiş bir moderasyon botudur.\n\n"
        "👉 Beni gruba ekleyin ve yönetici yapın.\n"
        "👉 Komutları hem / hem . ile kullanabilirsiniz."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_markup(), parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "menu_start":
        return await q.message.edit_text(
            f"👋 Merhaba!\n{BOT_USERNAME_TEXT} gelişmiş grup koruma ve moderasyon botudur.\n\nKomutlar için aşağıdaki menüyü kullanın.",
            reply_markup=main_menu_markup(),
            parse_mode="HTML"
        )
    if data == "menu_help":
        return await q.message.edit_text("📚 Kategori seç:", reply_markup=help_menu_markup(), parse_mode="HTML")
    if data == "menu_setup":
        return await q.message.edit_text(
            "🔧 <b>Kurulum</b>\n\n"
            "1. Botu gruba ekle\n"
            "2. Yönetici yap\n"
            "3. Mesaj silme / yasaklama / kısıtlama / sabitleme / admin verme yetkilerini ver\n"
            "4. Ayarla:\n"
            "• /setlog\n• /antilink on\n• /welcome on\n• /setwelcome Hoş geldin {first}\n• /setrules Kurallar\n",
            reply_markup=back_menu_markup(),
            parse_mode="HTML"
        )
    if data == "menu_settings":
        return await q.message.edit_text(
            "⚙️ <b>Ayar Menüsü</b>\n\n"
            "/antilink on/off\n"
            "/welcome on/off\n"
            "/goodbye on/off\n"
            "/setwelcome <mesaj>\n"
            "/setgoodbye <mesaj>\n"
            "/setlog [chat_id]\n"
            "/setrules <metin>\n"
            "/lock <link|badword|flood>\n"
            "/unlock <link|badword|flood>\n"
            "/settings",
            reply_markup=back_menu_markup(),
            parse_mode="HTML"
        )
    if data == "help_mod":
        return await q.message.edit_text(
            "👮 <b>Moderasyon</b>\n\n"
            "/ban / .ban\n/tban / .tban\n/unban / .unban\n"
            "/kick / .kick\n/mute / .mute\n/tmute / .tmute\n/unmute / .unmute\n"
            "/warn / .warn\n/warns / .warns\n/clearwarns / .clearwarns\n"
            "/admin / .admin\n/unadmin / .unadmin\n"
            "/pin / .pin\n/unpin / .unpin\n/purge / .purge",
            reply_markup=help_menu_markup(),
            parse_mode="HTML"
        )
    if data == "help_settings":
        return await q.message.edit_text(
            "⚙️ <b>Ayarlar</b>\n\n"
            "/antilink\n/welcome\n/goodbye\n/setwelcome\n/setgoodbye\n/setlog\n/setrules\n/settings\n/lock\n/unlock\n/addbad\n/delbad\n/badlist",
            reply_markup=help_menu_markup(),
            parse_mode="HTML"
        )
    if data == "help_notes":
        return await q.message.edit_text(
            "📝 <b>Notes & Filters</b>\n\n"
            "/save <isim> <metin>\n"
            "/get <isim>\n"
            "/clear <isim>\n"
            "/notes\n"
            "/filter <tetik> <cevap>\n"
            "/stop <tetik>\n"
            "/filters",
            reply_markup=help_menu_markup(),
            parse_mode="HTML"
        )
    if data == "help_other":
        return await q.message.edit_text(
            "📌 <b>Diğer</b>\n\n"
            "/start /help /yardim /destek\n"
            "/ping /id /userinfo /stats /ara /rules",
            reply_markup=help_menu_markup(),
            parse_mode="HTML"
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("📚 Kategori seç:", reply_markup=help_menu_markup(), parse_mode="HTML")

async def yardim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "🔧 Botu gruba ekle, yönetici yap, gerekli yetkileri ver. Komutları / veya . ile kullan.",
            parse_mode="HTML"
        )

async def destek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🆘 Destek", url=SUPPORT_URL)]])
    if update.message:
        await update.message.reply_text("Destek için tıkla.", reply_markup=kb)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = time.time()
    msg = await update.message.reply_text("Pong...")
    await msg.edit_text(f"🏓 {round((time.time() - st) * 1000)} ms")

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else "Yok"
    cid = update.effective_chat.id if update.effective_chat else "Yok"
    text = f"👤 User ID: <code>{uid}</code>\n💬 Chat ID: <code>{cid}</code>"
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
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("SELECT COUNT(*) FROM badwords WHERE chat_id = ?", (cid,))
    bad_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM warns WHERE chat_id = ? AND warn_count > 0", (cid,))
    warned_users = cursor.fetchone()[0]
    cursor.execute("SELECT SUM(msg_count), SUM(deleted_count) FROM stats WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    total_msgs = row[0] if row and row[0] else 0
    total_deleted = row[1] if row and row[1] else 0
    cursor.execute("SELECT total_bans, total_mutes, total_warns, total_deleted FROM mod_stats WHERE chat_id = ?", (cid,))
    mod = cursor.fetchone()
    text = (
        "📊 <b>Grup İstatistikleri</b>\n\n"
        f"• Toplam mesaj: <code>{total_msgs}</code>\n"
        f"• Silinen mesaj: <code>{total_deleted}</code>\n"
        f"• Warnlı kullanıcı: <code>{warned_users}</code>\n"
        f"• Yasaklı kelime: <code>{bad_count}</code>\n"
        f"• Toplam ban: <code>{mod[0] if mod else 0}</code>\n"
        f"• Toplam mute: <code>{mod[1] if mod else 0}</code>\n"
        f"• Toplam warn: <code>{mod[2] if mod else 0}</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("SELECT rules_text FROM chat_settings WHERE chat_id = ?", (cid,))
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
        parts = list(split_text(text))
        await msg.edit_text(parts[0], disable_web_page_preview=True)
        for p in parts[1:]:
            await update.message.reply_text(p, disable_web_page_preview=True)
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

    cid = update.effective_chat.id
    actor = update.effective_user
    target = update.message.reply_to_message.from_user

    if target.id == actor.id:
        return await update.message.reply_text("Kendine işlem yapamazsın.")
    me = await context.bot.get_me()
    if target.id == me.id:
        return await update.message.reply_text("Bana işlem yapamazsın.")
    if await is_admin_user(cid, target.id, context):
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
            if not await bot_can_restrict(cid, context):
                return await update.message.reply_text("Botun ban yetkisi yok.")
            await context.bot.ban_chat_member(cid, target.id, until_date=until_date)
            act_text = "banlandı" if action == "ban" else f"{context.args[0]} süreyle banlandı"
            inc_mod_stat(cid, "total_bans")
        elif action in ("mute", "tmute"):
            if not await bot_can_restrict(cid, context):
                return await update.message.reply_text("Botun susturma yetkisi yok.")
            await context.bot.restrict_chat_member(
                cid,
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
            inc_mod_stat(cid, "total_mutes")
        elif action == "kick":
            if not await bot_can_restrict(cid, context):
                return await update.message.reply_text("Botun atma yetkisi yok.")
            await context.bot.ban_chat_member(cid, target.id)
            await context.bot.unban_chat_member(cid, target.id)
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
        await send_log(context, cid,
            f"<b>Eylem:</b> {action.upper()}\n"
            f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
            f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
            f"<b>Sebep:</b> {html.escape(reason)}"
        )
    except Exception as e:
        logger.error(f"mod action hatası {action}: {e}")
        await update.message.reply_text("İşlem başarısız oldu.")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE): await mod_action(update, context, "ban")
async def tban(update: Update, context: ContextTypes.DEFAULT_TYPE): await mod_action(update, context, "tban")
async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE): await mod_action(update, context, "mute")
async def tmute(update: Update, context: ContextTypes.DEFAULT_TYPE): await mod_action(update, context, "tmute")
async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE): await mod_action(update, context, "kick")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    actor_member = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False):
        return await update.message.reply_text("Ban kaldırmak için yetkin yok.")
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /unban kullan.")
    cid = update.effective_chat.id
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.unban_chat_member(cid, target.id)
        msg = await update.message.reply_text(f"✅ {target.full_name} için ban kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, cid, f"<b>Eylem:</b> UNBAN\n<b>Hedef:</b> {html.escape(target.full_name)}")
    except Exception:
        await update.message.reply_text("Ban kaldırılamadı.")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    actor_member = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False):
        return await update.message.reply_text("Susturma kaldırmak için yetkin yok.")
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /unmute kullan.")
    cid = update.effective_chat.id
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(cid, target.id, full_unmute_permissions())
        msg = await update.message.reply_text(f"🔊 {target.full_name} için susturma kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, cid, f"<b>Eylem:</b> UNMUTE\n<b>Hedef:</b> {html.escape(target.full_name)}")
    except Exception:
        await update.message.reply_text("Susturma kaldırılamadı.")

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    actor_member = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if not actor_member:
        return await update.message.reply_text("Yetki bilgisi alınamadı.")
    if actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_promote_members", False):
        return await update.message.reply_text("Admin vermek için yetkin yok.")
    if not await bot_can_promote(update.effective_chat.id, context):
        return await update.message.reply_text("Botun admin verme yetkisi yok.")
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip /admin kullan.")
    target = update.message.reply_to_message.from_user
    if target.id == update.effective_user.id:
        return await update.message.reply_text("Kendine admin veremezsin.")
    if await is_admin_user(update.effective_chat.id, target.id, context):
        return await update.message.reply_text("Bu kullanıcı zaten yönetici.")
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
        await send_log(context, update.effective_chat.id, f"<b>Eylem:</b> ADMIN VER\n<b>Hedef:</b> {html.escape(target.full_name)}")
    except Exception:
        await update.message.reply_text("Kullanıcı admin yapılamadı.")

async def unadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    actor_member = await get_member_status(update.effective_chat.id, update.effective_user.id, context)
    if not actor_member:
        return await update.message.reply_text("Yetki bilgisi alınamadı.")
    if actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_promote_members", False):
        return await update.message.reply_text("Admin almak için yetkin yok.")
    if not await bot_can_promote(update.effective_chat.id, context):
        return await update.message.reply_text("Botun admin alma yetkisi yok.")
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip /unadmin kullan.")
    target = update.message.reply_to_message.from_user
    target_member = await get_member_status(update.effective_chat.id, target.id, context)
    if not target_member or target_member.status == ChatMemberStatus.OWNER:
        return await update.message.reply_text("Bu kullanıcıdan adminlik alınamaz.")
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
        await send_log(context, update.effective_chat.id, f"<b>Eylem:</b> ADMIN AL\n<b>Hedef:</b> {html.escape(target.full_name)}")
    except Exception:
        await update.message.reply_text("Kullanıcının adminliği alınamadı.")

async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip /warn kullan.")
    cid = update.effective_chat.id
    actor = update.effective_user
    target = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "Sebep belirtilmedi."

    if await is_admin_user(cid, target.id, context):
        return await update.message.reply_text("Yöneticilere warn veremem.")

    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    current_warns = (row[0] if row else 0) + 1
    cursor.execute("INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)", (cid, target.id, current_warns))
    conn.commit()
    inc_mod_stat(cid, "total_warns")

    msg = await update.message.reply_text(
        f"⚠️ <b>{html.escape(target.full_name)}</b> warn aldı. ({current_warns}/{MAX_WARNS})\n"
        f"📝 <b>Sebep:</b> {html.escape(reason)}",
        parse_mode="HTML"
    )
    context.application.create_task(delete_later(msg, 5))

    await send_log(context, cid,
        f"<b>Eylem:</b> WARN\n<b>Hedef:</b> {html.escape(target.full_name)}\n"
        f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
        f"<b>Sebep:</b> {html.escape(reason)}\n<b>Warn:</b> {current_warns}/{MAX_WARNS}"
    )

    if current_warns >= MAX_WARNS:
        try:
            await context.bot.ban_chat_member(cid, target.id)
            cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (cid, target.id))
            conn.commit()
            inc_mod_stat(cid, "total_bans")
            auto = await update.message.reply_text(f"🚫 {target.full_name} max warn nedeniyle banlandı.")
            context.application.create_task(delete_later(auto, 5))
            await send_log(context, cid, f"<b>Eylem:</b> AUTO BAN\n<b>Hedef:</b> {html.escape(target.full_name)}\n<b>Neden:</b> Max warn")
        except Exception:
            pass

async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip /warns kullan.")
    target = update.message.reply_to_message.from_user
    cid = update.effective_chat.id
    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    count = row[0] if row else 0
    await update.message.reply_text(f"📌 {target.full_name} warn sayısı: {count}/{MAX_WARNS}")

async def clearwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip /clearwarns kullan.")
    target = update.message.reply_to_message.from_user
    cid = update.effective_chat.id
    cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    conn.commit()
    msg = await update.message.reply_text(f"🧽 {target.full_name} warnları sıfırlandı.")
    context.application.create_task(delete_later(msg, 5))

async def toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, label: str):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        return await update.message.reply_text(f"Kullanım: /{field} on veya /{field} off")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    val = 1 if context.args[0].lower() == "on" else 0
    cursor.execute(f"UPDATE chat_settings SET {field} = ? WHERE chat_id = ?", (val, cid))
    conn.commit()
    msg = await update.message.reply_text(f"⚙️ {label} {'açıldı ✅' if val else 'kapatıldı ❌'}.")
    context.application.create_task(delete_later(msg, 5))

async def antilink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await toggle_setting(update, context, "antilink", "Antilink")
async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await toggle_setting(update, context, "welcome", "Karşılama")
async def goodbye_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE): await toggle_setting(update, context, "goodbye", "Çıkış mesajı")

async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args: return await update.message.reply_text("Kullanım: /setwelcome <mesaj>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET welcome_text = ? WHERE chat_id = ?", (" ".join(context.args), cid))
    conn.commit()
    msg = await update.message.reply_text("✅ Karşılama mesajı güncellendi.")
    context.application.create_task(delete_later(msg, 5))

async def setgoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args: return await update.message.reply_text("Kullanım: /setgoodbye <mesaj>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET goodbye_text = ? WHERE chat_id = ?", (" ".join(context.args), cid))
    conn.commit()
    msg = await update.message.reply_text("✅ Çıkış mesajı güncellendi.")
    context.application.create_task(delete_later(msg, 5))

async def setlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    target_log_id = cid
    if context.args:
        try:
            target_log_id = int(context.args[0])
        except Exception:
            return await update.message.reply_text("Kullanım: /setlog veya /setlog <chat_id>")

    try:
        await context.bot.send_message(target_log_id, f"✅ Test log mesajı.\nKaynak chat: <code>{cid}</code>", parse_mode="HTML")
    except Exception:
        return await update.message.reply_text("Bu log chat id'ye mesaj atamıyorum. Beni o sohbete ekleyip yetki ver.")

    cursor.execute("UPDATE chat_settings SET log_chat_id = ? WHERE chat_id = ?", (target_log_id, cid))
    conn.commit()
    msg = await update.message.reply_text(f"✅ Log sohbeti ayarlandı: <code>{target_log_id}</code>", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))

async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args: return await update.message.reply_text("Kullanım: /setrules <kurallar>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET rules_text = ? WHERE chat_id = ?", (" ".join(context.args), cid))
    conn.commit()
    msg = await update.message.reply_text("✅ Grup kuralları güncellendi.")
    context.application.create_task(delete_later(msg, 5))

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("""
        SELECT antilink, welcome, goodbye, welcome_text, goodbye_text, log_chat_id, rules_text,
               lock_link, lock_badword, lock_flood
        FROM chat_settings WHERE chat_id = ?
    """, (cid,))
    row = cursor.fetchone()
    text = (
        "⚙️ <b>Grup Ayarları</b>\n\n"
        f"• Antilink: {'Açık' if row[0] else 'Kapalı'}\n"
        f"• Welcome: {'Açık' if row[1] else 'Kapalı'}\n"
        f"• Goodbye: {'Açık' if row[2] else 'Kapalı'}\n"
        f"• Log Chat ID: <code>{row[5]}</code>\n"
        f"• Lock Link: {'Açık' if row[7] else 'Kapalı'}\n"
        f"• Lock Badword: {'Açık' if row[8] else 'Kapalı'}\n"
        f"• Lock Flood: {'Açık' if row[9] else 'Kapalı'}\n"
        f"• Welcome Mesajı: {html.escape(row[3])[:80]}\n"
        f"• Goodbye Mesajı: {html.escape(row[4])[:80]}"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args: return await update.message.reply_text("Kullanım: /lock <link|badword|flood>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    arg = context.args[0].lower()
    field_map = {"link": "lock_link", "badword": "lock_badword", "flood": "lock_flood"}
    field = field_map.get(arg)
    if not field:
        return await update.message.reply_text("Sadece: link, badword, flood")
    cursor.execute(f"UPDATE chat_settings SET {field} = 1 WHERE chat_id = ?", (cid,))
    conn.commit()
    await update.message.reply_text(f"🔒 {arg} kilidi açıldı.")

async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args: return await update.message.reply_text("Kullanım: /unlock <link|badword|flood>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    arg = context.args[0].lower()
    field_map = {"link": "lock_link", "badword": "lock_badword", "flood": "lock_flood"}
    field = field_map.get(arg)
    if not field:
        return await update.message.reply_text("Sadece: link, badword, flood")
    cursor.execute(f"UPDATE chat_settings SET {field} = 0 WHERE chat_id = ?", (cid,))
    conn.commit()
    await update.message.reply_text(f"🔓 {arg} kilidi kapatıldı.")

async def addbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args: return await update.message.reply_text("Kullanım: /addbad <kelime>")
    word = context.args[0].lower().strip()
    cid = update.effective_chat.id
    cursor.execute("INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?, ?)", (cid, word))
    conn.commit()
    msg = await update.message.reply_text(f"✅ Yasaklı kelime eklendi: <code>{html.escape(word)}</code>", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))

async def delbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args: return await update.message.reply_text("Kullanım: /delbad <kelime>")
    word = context.args[0].lower().strip()
    cid = update.effective_chat.id
    cursor.execute("DELETE FROM badwords WHERE chat_id = ? AND word = ?", (cid, word))
    conn.commit()
    msg = await update.message.reply_text(f"🗑️ Silindi: <code>{html.escape(word)}</code>", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))

async def badlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    cursor.execute("SELECT word FROM badwords WHERE chat_id = ? ORDER BY word ASC", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Liste boş.")
    text = "🧱 <b>Yasaklı Kelimeler</b>\n\n" + "\n".join(f"• <code>{html.escape(r[0])}</code>" for r in rows)
    for p in split_text(text):
        await update.message.reply_text(p, parse_mode="HTML")

async def pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not update.message.reply_to_message: return await update.message.reply_text("Bir mesaja yanıt verip /pin kullan.")
    if not await bot_can_pin(update.effective_chat.id, context): return await update.message.reply_text("Botun sabitleme yetkisi yok.")
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id, disable_notification=True)
        msg = await update.message.reply_text("📌 Mesaj sabitlendi.")
        context.application.create_task(delete_later(msg, 5))
    except Exception:
        await update.message.reply_text("Mesaj sabitlenemedi.")

async def unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not await bot_can_pin(update.effective_chat.id, context): return await update.message.reply_text("Botun sabit kaldırma yetkisi yok.")
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
        amount = end_id - start_id + 1
        if amount > PURGE_LIMIT:
            return await update.message.reply_text(f"En fazla {PURGE_LIMIT} mesaj temizleyebilirim.")
        deleted = 0
        for msg_id in range(start_id, end_id + 1):
            try:
                await context.bot.delete_message(update.effective_chat.id, msg_id)
                deleted += 1
            except Exception:
                pass
        info = await context.bot.send_message(update.effective_chat.id, f"🧹 {deleted} mesaj temizlendi.")
        context.application.create_task(delete_later(info, 5))
        await send_log(context, update.effective_chat.id, f"<b>Eylem:</b> PURGE\n<b>Silinen:</b> {deleted}")
    except Exception as e:
        logger.error(f"purge hatası: {e}")
        await update.message.reply_text("Purge işlemi başarısız oldu.")

async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if len(context.args) < 2:
        return await update.message.reply_text("Kullanım: /save <isim> <metin>")
    cid = update.effective_chat.id
    name = context.args[0].lower()
    text = " ".join(context.args[1:])
    if len(name) > MAX_NOTE_NAME:
        return await update.message.reply_text(f"Not adı en fazla {MAX_NOTE_NAME} karakter olabilir.")
    if len(text) > MAX_NOTE_TEXT:
        return await update.message.reply_text(f"Not içeriği en fazla {MAX_NOTE_TEXT} karakter olabilir.")
    cursor.execute("INSERT OR REPLACE INTO notes (chat_id, note_name, note_text) VALUES (?, ?, ?)", (cid, name, text))
    conn.commit()
    await update.message.reply_text(f"💾 Not kaydedildi: #{name}")

async def get_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update): return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /get <isim>")
    cid = update.effective_chat.id
    name = context.args[0].lower()
    cursor.execute("SELECT note_text FROM notes WHERE chat_id = ? AND note_name = ?", (cid, name))
    row = cursor.fetchone()
    if not row:
        return await update.message.reply_text("Not bulunamadı.")
    await update.message.reply_text(row[0])

async def clear_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args:
        return await update.message.reply_text("Kullanım: /clear <isim>")
    cid = update.effective_chat.id
    name = context.args[0].lower()
    cursor.execute("DELETE FROM notes WHERE chat_id = ? AND note_name = ?", (cid, name))
    conn.commit()
    await update.message.reply_text(f"🗑️ Not silindi: #{name}")

async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    cursor.execute("SELECT note_name FROM notes WHERE chat_id = ? ORDER BY note_name", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Hiç not yok.")
    text = "📝 <b>Notlar</b>\n\n" + "\n".join(f"• #{r[0]}" for r in rows)
    await update.message.reply_text(text, parse_mode="HTML")

async def add_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if len(context.args) < 2:
        return await update.message.reply_text("Kullanım: /filter <tetik> <cevap>")
    cid = update.effective_chat.id
    trigger = context.args[0].lower()
    reply = " ".join(context.args[1:])
    if len(trigger) > MAX_FILTER_TRIGGER:
        return await update.message.reply_text(f"Tetik en fazla {MAX_FILTER_TRIGGER} karakter olabilir.")
    if len(reply) > MAX_FILTER_REPLY:
        return await update.message.reply_text(f"Cevap en fazla {MAX_FILTER_REPLY} karakter olabilir.")
    cursor.execute("INSERT OR REPLACE INTO filters_table (chat_id, trigger_text, reply_text) VALUES (?, ?, ?)", (cid, trigger, reply))
    conn.commit()
    await update.message.reply_text(f"✅ Filter eklendi: {trigger}")

async def stop_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context): return
    if not context.args:
        return await update.message.reply_text("Kullanım: /stop <tetik>")
    cid = update.effective_chat.id
    trigger = context.args[0].lower()
    cursor.execute("DELETE FROM filters_table WHERE chat_id = ? AND trigger_text = ?", (cid, trigger))
    conn.commit()
    await update.message.reply_text(f"🗑️ Filter silindi: {trigger}")

async def filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    cursor.execute("SELECT trigger_text FROM filters_table WHERE chat_id = ? ORDER BY trigger_text", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Filter yok.")
    text = "🔎 <b>Filters</b>\n\n" + "\n".join(f"• {r[0]}" for r in rows)
    await update.message.reply_text(text, parse_mode="HTML")

async def dot_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    cmd, args = normalize_dot_command(update.message.text)
    if not cmd:
        return

    if await bot_can_delete(update.effective_chat.id, context):
        try:
            await update.message.delete()
        except Exception:
            pass

    context.args = args

    mapping = {
        "ban": ban, "tban": tban, "unban": unban, "kick": kick,
        "mute": mute, "tmute": tmute, "unmute": unmute,
        "warn": warn, "warns": warns_cmd, "clearwarns": clearwarns,
        "admin": admin_cmd, "unadmin": unadmin_cmd,
        "antilink": antilink_cmd, "welcome": welcome_cmd, "goodbye": goodbye_cmd,
        "setwelcome": setwelcome, "setgoodbye": setgoodbye,
        "setlog": setlog, "setrules": setrules, "settings": settings_cmd,
        "addbad": addbad, "delbad": delbad, "badlist": badlist,
        "pin": pin, "unpin": unpin, "purge": purge,
        "ping": ping, "id": id_cmd, "userinfo": userinfo,
        "stats": stats_cmd, "ara": ara, "rules": rules,
        "save": save_note, "get": get_note, "clear": clear_note, "notes": notes_cmd,
        "filter": add_filter, "stop": stop_filter, "filters": filters_cmd,
        "lock": lock_cmd, "unlock": unlock_cmd,
    }

    func = mapping.get(cmd)
    if func:
        await func(update, context)
    else:
        msg = await context.bot.send_message(update.effective_chat.id, "Bilinmeyen noktalı komut.")
        context.application.create_task(delete_later(msg, 5))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    if update.effective_user.is_bot:
        return
    if update.message.text and update.message.text.startswith("."):
        return

    cid = update.effective_chat.id
    uid = update.effective_user.id

    ensure_chat_settings(cid)

    cursor.execute("""
        INSERT INTO stats (chat_id, user_id, msg_count, deleted_count)
        VALUES (?, ?, 1, 0)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET msg_count = msg_count + 1
    """, (cid, uid))
    conn.commit()

    if update.message.new_chat_members:
        cursor.execute("SELECT welcome, welcome_text FROM chat_settings WHERE chat_id = ?", (cid,))
        row = cursor.fetchone()
        if row and row[0] == 1:
            for nm in update.message.new_chat_members:
                if not nm.is_bot:
                    text = row[1].replace("{first}", nm.first_name or "")
                    await update.message.reply_text(f"👋 {nm.mention_html()}\n{text}", parse_mode="HTML")
        return

    if update.message.left_chat_member:
        cursor.execute("SELECT goodbye, goodbye_text FROM chat_settings WHERE chat_id = ?", (cid,))
        row = cursor.fetchone()
        left = update.message.left_chat_member
        if row and row[0] == 1 and left:
            text = row[1].replace("{first}", left.first_name or "")
            await update.message.reply_text(text)
        return

    if is_private(update):
        return

    text = (update.message.text or update.message.caption or "").strip()
    lowered = text.lower()

    if text.startswith("#") and len(text) > 1:
        name = text[1:].split()[0].lower()
        cursor.execute("SELECT note_text FROM notes WHERE chat_id = ? AND note_name = ?", (cid, name))
        row = cursor.fetchone()
        if row:
            return await update.message.reply_text(row[0])

    if lowered:
        cursor.execute("SELECT trigger_text, reply_text FROM filters_table WHERE chat_id = ?", (cid,))
        for trig, rep in cursor.fetchall():
            if lowered == trig.lower():
                await update.message.reply_text(rep)
                break

    if await is_admin_user(cid, uid, context):
        return

    cursor.execute("SELECT antilink, lock_link, lock_badword, lock_flood FROM chat_settings WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    antilink_on = row[0] == 1
    lock_link = row[1] == 1
    lock_badword = row[2] == 1
    lock_flood = row[3] == 1

    delete_reason = None

    if text and (antilink_on or lock_link) and URL_PATTERN.search(text):
        delete_reason = "Link paylaşımı"

    if not delete_reason and lowered and lock_badword:
        cursor.execute("SELECT word FROM badwords WHERE chat_id = ?", (cid,))
        for r in cursor.fetchall():
            if re.search(rf"\b{re.escape(r[0])}\b", lowered, re.IGNORECASE):
                delete_reason = "Yasaklı kelime"
                break

    if not delete_reason and lock_flood:
        now = time.time()
        spam_tracker[(cid, uid)].append(now)
        spam_tracker[(cid, uid)] = [t for t in spam_tracker[(cid, uid)] if now - t < SPAM_WINDOW]
        if len(spam_tracker[(cid, uid)]) > SPAM_LIMIT:
            delete_reason = "Spam / flood"
            spam_tracker[(cid, uid)].clear()

    if delete_reason:
        try:
            if await bot_can_delete(cid, context):
                await update.message.delete()
                warn_msg = await context.bot.send_message(
                    cid,
                    f"⚠️ {update.effective_user.mention_html()} mesajı silindi.\nNeden: <b>{html.escape(delete_reason)}</b>",
                    parse_mode="HTML"
                )
                context.application.create_task(delete_later(warn_msg, 5))
                inc_mod_stat(cid, "total_deleted")
                inc_user_deleted(cid, uid)
                await send_log(context, cid,
                    f"<b>Eylem:</b> AUTO DELETE\n<b>Kullanıcı:</b> {html.escape(update.effective_user.full_name)}\n"
                    f"<b>Neden:</b> {html.escape(delete_reason)}\n<b>Mesaj:</b> {html.escape(text[:150])}"
                )
        except Exception as e:
            logger.error(f"otomatik silme hatası: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update işlenirken hata oluştu", exc_info=context.error)

def main():
    if not TOKEN:
        print("HATA: BOT_TOKEN tanımlı değil.")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_error_handler(error_handler)

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
    app.add_handler(CommandHandler("goodbye", goodbye_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome))
    app.add_handler(CommandHandler("setgoodbye", setgoodbye))
    app.add_handler(CommandHandler("setlog", setlog))
    app.add_handler(CommandHandler("setrules", setrules))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))

    app.add_handler(CommandHandler("addbad", addbad))
    app.add_handler(CommandHandler("delbad", delbad))
    app.add_handler(CommandHandler("badlist", badlist))

    app.add_handler(CommandHandler("pin", pin))
    app.add_handler(CommandHandler("unpin", unpin))
    app.add_handler(CommandHandler("purge", purge))

    app.add_handler(CommandHandler("save", save_note))
    app.add_handler(CommandHandler("get", get_note))
    app.add_handler(CommandHandler("clear", clear_note))
    app.add_handler(CommandHandler("notes", notes_cmd))

    app.add_handler(CommandHandler("filter", add_filter))
    app.add_handler(CommandHandler("stop", stop_filter))
    app.add_handler(CommandHandler("filters", filters_cmd))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\."), dot_command_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    print("✅ BOT ÇALIŞIYOR")
    app.run_polling()

if __name__ == "__main__":
    main()
