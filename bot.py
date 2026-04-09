import os
import re
import time
import html
import asyncio
import logging
import sqlite3
import datetime
import unicodedata
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from duckduckgo_search import DDGS

# ====================== AYARLAR ======================
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "bot_database.db")

BOT_USERNAME = "@KGBKORUMABot"
SUPPORT_URL = "https://t.me/KGBotomasyon"

MAX_WARNS = 3
SPAM_WINDOW = 5
SPAM_LIMIT = 5
PURGE_LIMIT = 100
REPEAT_WINDOW = 120
REPEAT_LIMIT = 5
AUTO_MUTE_MINUTES = 30
RAID_MUTE_MINUTES = 30
SEARCH_COOLDOWN = 15
JOIN_WINDOW = 30
JOIN_LIMIT = 5
STICKER_WINDOW = 10
STICKER_LIMIT = 5
MEDIA_WINDOW = 10
MEDIA_LIMIT = 5

# ====================== LOGGING ======================
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("KGB_Rose")

# ====================== DATABASE ======================
db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
cursor = conn.cursor()

cursor.executescript("""
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY, antilink INTEGER DEFAULT 0, welcome INTEGER DEFAULT 0,
    goodbye INTEGER DEFAULT 0, welcome_text TEXT DEFAULT 'Hoş geldin!',
    goodbye_text TEXT DEFAULT 'Görüşürüz!', log_chat_id INTEGER DEFAULT 0,
    rules_text TEXT DEFAULT 'Kurallar ayarlanmadı.', lock_link INTEGER DEFAULT 0,
    lock_badword INTEGER DEFAULT 1, lock_flood INTEGER DEFAULT 1, antispam INTEGER DEFAULT 1,
    raid_mode INTEGER DEFAULT 0, warn_limit INTEGER DEFAULT 3, warn_mode TEXT DEFAULT 'ban',
    lock_sticker INTEGER DEFAULT 0, lock_media INTEGER DEFAULT 0, clean_commands INTEGER DEFAULT 0,
    reports_enabled INTEGER DEFAULT 1, clean_service INTEGER DEFAULT 0, lock_forward INTEGER DEFAULT 0,
    lock_bots INTEGER DEFAULT 0, lock_photo INTEGER DEFAULT 0, lock_video INTEGER DEFAULT 0,
    lock_document INTEGER DEFAULT 0, lock_voice INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS badwords (chat_id INTEGER, word TEXT, UNIQUE(chat_id, word));
CREATE TABLE IF NOT EXISTS warns (chat_id INTEGER, user_id INTEGER, warn_count INTEGER DEFAULT 0, UNIQUE(chat_id, user_id));
CREATE TABLE IF NOT EXISTS stats (chat_id INTEGER, user_id INTEGER, msg_count INTEGER DEFAULT 0, deleted_count INTEGER DEFAULT 0, UNIQUE(chat_id, user_id));
CREATE TABLE IF NOT EXISTS notes (chat_id INTEGER, note_name TEXT, note_text TEXT, UNIQUE(chat_id, note_name));
CREATE TABLE IF NOT EXISTS filters_table (chat_id INTEGER, trigger_text TEXT, reply_text TEXT, UNIQUE(chat_id, trigger_text));
CREATE TABLE IF NOT EXISTS mod_stats (chat_id INTEGER PRIMARY KEY, total_bans INTEGER DEFAULT 0, total_mutes INTEGER DEFAULT 0, total_warns INTEGER DEFAULT 0, total_deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS punish_history (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, action TEXT, reason TEXT, actor_id INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS strikes (chat_id INTEGER, user_id INTEGER, strike_count INTEGER DEFAULT 0, UNIQUE(chat_id, user_id));
CREATE TABLE IF NOT EXISTS sudo_users (user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS blacklists (chat_id INTEGER, trigger_text TEXT, UNIQUE(chat_id, trigger_text));
CREATE TABLE IF NOT EXISTS approvals (chat_id INTEGER, user_id INTEGER, UNIQUE(chat_id, user_id));
""")
conn.commit()

# ====================== GLOBAL TRACKERS ======================
spam_tracker = defaultdict(list)
repeat_tracker = defaultdict(list)
search_cooldowns = {}
join_tracker = defaultdict(list)
sticker_tracker = defaultdict(list)
media_tracker = defaultdict(list)

URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.|t\.me/|telegram\.me/|discord\.gg/|[a-z0-9-]+\.(?:com|net|org|me|io|xyz|ru|co|gg))\S*")

# ====================== YARDIMCI FONKSİYONLAR ======================
def ensure_chat_settings(chat_id: int):
    cursor.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
    cursor.execute("INSERT OR IGNORE INTO mod_stats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()

def is_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"

def parse_time(s: str):
    if not s or len(s) < 2:
        return None
    unit, val = s[-1].lower(), s[:-1]
    if not val.isdigit():
        return None
    v = int(val)
    if unit == "m": return v * 60
    if unit == "h": return v * 3600
    if unit == "d": return v * 86400
    return None

def normalize_text(text: str) -> str:
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.translate(str.maketrans("çğışöü@$€", "cgisouase"))
    text = re.sub(r"[^\w\s./:-]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def inc_mod_stat(chat_id: int, field: str):
    cursor.execute(f"UPDATE mod_stats SET {field} = {field} + 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()

def add_punish_history(chat_id: int, user_id: int, action: str, reason: str, actor_id: int):
    cursor.execute("INSERT INTO punish_history (chat_id, user_id, action, reason, actor_id, ts) VALUES (?,?,?,?,?,?)",
                   (chat_id, user_id, action, reason, actor_id, int(time.time())))
    conn.commit()

async def send_log(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    cursor.execute("SELECT log_chat_id FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row and row[0]:
        try:
            await context.bot.send_message(row[0], f"<b>LOG:</b>\n{text}", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Log hatası: {e}")

async def delete_later(msg, seconds=5):
    try:
        await asyncio.sleep(seconds)
        await msg.delete()
    except:
        pass

async def silent_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    cid = update.effective_chat.id
    cursor.execute("SELECT clean_commands FROM chat_settings WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    if row and row[0] == 1:
        try:
            await update.message.delete()
        except:
            pass

# ====================== BOT YETKİ ======================
async def bot_rights(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        me = await context.bot.get_me()
        return await context.bot.get_chat_member(chat_id, me.id)
    except:
        return None

async def bot_can_restrict(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    m = await bot_rights(chat_id, context)
    return bool(m and (getattr(m, "can_restrict_members", False) or m.status == ChatMemberStatus.OWNER))

async def bot_can_delete(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    m = await bot_rights(chat_id, context)
    return bool(m and (getattr(m, "can_delete_messages", False) or m.status == ChatMemberStatus.OWNER))

async def bot_can_pin(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    m = await bot_rights(chat_id, context)
    return bool(m and (getattr(m, "can_pin_messages", False) or m.status == ChatMemberStatus.OWNER))

async def bot_can_promote(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    m = await bot_rights(chat_id, context)
    return bool(m and (getattr(m, "can_promote_members", False) or m.status == ChatMemberStatus.OWNER))

# ====================== KULLANICI YETKİ ======================
def is_sudo(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM sudo_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

async def get_member(chat_id: int, user_id, context: ContextTypes.DEFAULT_TYPE):
    try:
        return await context.bot.get_chat_member(chat_id, user_id)
    except:
        return None

async def is_admin_user(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_sudo(user_id):
        return True
    m = await get_member(chat_id, user_id, context)
    return bool(m and m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER))

async def is_owner(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    m = await get_member(chat_id, user_id, context)
    return bool(m and m.status == ChatMemberStatus.OWNER)

async def role_of(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    if await is_owner(chat_id, user_id, context):
        return "owner"
    if is_sudo(user_id):
        return "sudo"
    if await is_admin_user(chat_id, user_id, context):
        return "admin"
    return "member"

async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_private(update):
        await update.message.reply_text("Bu komut grupta kullanılmalı.")
        return False
    if await is_admin_user(update.effective_chat.id, update.effective_user.id, context):
        return True
    await update.message.reply_text("Yetkin yok.")
    return False

def is_approved(chat_id: int, user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM approvals WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    return cursor.fetchone() is not None

# ====================== HEDEF BULMA ======================
async def extract_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = None
    reason = "Sebep belirtilmedi."
    args = context.args or []

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
        if args:
            reason = " ".join(args).strip()
        return target, reason

    if not args:
        return None, reason

    identifier = args[0]
    cid = update.effective_chat.id

    try:
        if identifier.isdigit():
            m = await get_member(cid, int(identifier), context)
            if m:
                target = m.user
        elif identifier.startswith("@"):
            try:
                m = await get_member(cid, identifier, context)
                if m:
                    target = m.user
            except:
                try:
                    u = await context.bot.get_chat(identifier)
                    m2 = await get_member(cid, u.id, context)
                    if m2:
                        target = u
                except:
                    pass
        if target and len(args) > 1:
            reason = " ".join(args[1:]).strip()
    except Exception as e:
        logger.error(f"Hedef bulma hatası: {e}")

    return target, reason

async def can_act_on(chat_id: int, actor_id: int, target_id: int, context: ContextTypes.DEFAULT_TYPE):
    if actor_id == target_id:
        return False, "Kendine işlem yapamazsın."
    me = await context.bot.get_me()
    if target_id == me.id:
        return False, "Bana işlem yapamazsın."
    a_role = await role_of(chat_id, actor_id, context)
    t_role = await role_of(chat_id, target_id, context)
    if t_role == "owner":
        return False, "Owner'a işlem yapılamaz."
    if t_role == "sudo" and a_role != "owner":
        return False, "Sudo'ya işlem yapamazsın."
    if t_role == "admin" and a_role not in ("owner", "sudo"):
        return False, "Admin'e işlem yapamazsın."
    return True, None

# ====================== MODERASYON ======================
async def mod_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    if not await require_admin(update, context):
        return

    cid = update.effective_chat.id
    actor = update.effective_user
    target, reason = await extract_target(update, context)

    if not target:
        return await update.message.reply_text(f"Kullanım: /{action} <reply | @username | ID> [sebep]")

    allowed, err = await can_act_on(cid, actor.id, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)

    if not await bot_can_restrict(cid, context):
        return await update.message.reply_text("Botun yetkisi yok.")

    duration = None
    if action in ("tban", "tmute"):
        args = context.args or []
        time_arg = args[1] if len(args) > 1 else (args[0] if args else None)
        duration = parse_time(time_arg) if time_arg else None
        if not duration:
            return await update.message.reply_text(f"Kullanım: /{action} <hedef> <10m/1h/1d> [sebep]")

    until_date = datetime.datetime.now() + datetime.timedelta(seconds=duration) if duration else None

    try:
        if action in ("ban", "tban", "sban"):
            await context.bot.ban_chat_member(cid, target.id, until_date=until_date)
            act_text = "banlandı"
            inc_mod_stat(cid, "total_bans")

        elif action in ("mute", "tmute", "smute"):
            await context.bot.restrict_chat_member(cid, target.id, ChatPermissions(can_send_messages=False), until_date=until_date)
            act_text = "susturuldu"
            inc_mod_stat(cid, "total_mutes")

        elif action == "kick":
            await context.bot.ban_chat_member(cid, target.id)
            await context.bot.unban_chat_member(cid, target.id)
            act_text = "atıldı"

        else:
            return

        add_punish_history(cid, target.id, action.upper(), reason, actor.id)

        if action not in ("sban", "smute"):
            await context.bot.send_message(
                cid,
                f"🔨 <b>{html.escape(target.full_name)}</b> {act_text}.\n"
                f"👮 Yetkili: {html.escape(actor.full_name)}\n"
                f"📝 Sebep: {html.escape(reason)}",
                parse_mode="HTML"
            )

        await send_log(context, cid, f"Eylem: {action.upper()}\nHedef: {target.full_name} ({target.id})\nYetkili: {actor.full_name}\nSebep: {reason}")
        await silent_delete_command(update, context)

    except Exception as e:
        logger.error(f"Mod action hatası ({action}): {e}")
        await update.message.reply_text("İşlem başarısız.")

# ====================== WARN ======================
def get_warn_limit(chat_id: int) -> int:
    cursor.execute("SELECT warn_limit FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    return row[0] if row and row[0] else MAX_WARNS

def get_warn_mode(chat_id: int) -> str:
    cursor.execute("SELECT warn_mode FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    return row[0] if row and row[0] in ("ban", "mute", "kick") else "ban"

async def execute_warn_limit(chat_id: int, target, actor, reason: str, context: ContextTypes.DEFAULT_TYPE):
    mode = get_warn_mode(chat_id)
    try:
        if mode == "ban":
            await context.bot.ban_chat_member(chat_id, target.id)
            text = f"🚫 {html.escape(target.full_name)} warn limitiyle banlandı."
            inc_mod_stat(chat_id, "total_bans")
        elif mode == "mute":
            await context.bot.restrict_chat_member(chat_id, target.id, ChatPermissions(can_send_messages=False))
            text = f"🔇 {html.escape(target.full_name)} warn limitiyle susturuldu."
            inc_mod_stat(chat_id, "total_mutes")
        elif mode == "kick":
            await context.bot.ban_chat_member(chat_id, target.id)
            await context.bot.unban_chat_member(chat_id, target.id)
            text = f"👢 {html.escape(target.full_name)} warn limitiyle atıldı."
        else:
            return
        await context.bot.send_message(chat_id, text, parse_mode="HTML")
        cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
        conn.commit()
    except Exception as e:
        logger.error(f"Warn limit hatası: {e}")

async def warn_action(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str = "normal"):
    if not await require_admin(update, context):
        return

    cid = update.effective_chat.id
    actor = update.effective_user
    target, reason = await extract_target(update, context)

    if not target:
        return await update.message.reply_text("Kullanım: /warn <reply | @username | ID> [sebep]")

    allowed, err = await can_act_on(cid, actor.id, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)

    if mode == "delete" and update.message.reply_to_message:
        try:
            await update.message.reply_to_message.delete()
            inc_mod_stat(cid, "total_deleted")
        except:
            pass

    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    current = (row[0] if row else 0) + 1
    limit = get_warn_limit(cid)

    cursor.execute("INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)", (cid, target.id, current))
    conn.commit()
    inc_mod_stat(cid, "total_warns")
    add_punish_history(cid, target.id, f"WARN_{mode.upper()}", reason, actor.id)

    if mode != "silent":
        msg = await context.bot.send_message(
            cid,
            f"⚠️ <b>{html.escape(target.full_name)}</b> warn aldı. ({current}/{limit})\n📝 Sebep: {html.escape(reason)}",
            parse_mode="HTML"
        )
        asyncio.create_task(delete_later(msg, 5))

    await send_log(context, cid, f"WARN ({mode}): {target.full_name} - {current}/{limit} - {reason}")

    if current >= limit:
        await execute_warn_limit(cid, target, actor, reason, context)

    await silent_delete_command(update, context)

# ====================== KOMUTLAR ======================
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "ban")

async def tban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "tban")

async def sban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "sban")

async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "mute")

async def tmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "tmute")

async def smute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "smute")

async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mod_action(update, context, "kick")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /unban <reply | @username | ID>")
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(f"✅ {target.full_name} banı kaldırıldı.")
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Başarısız.")

async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /unmute <reply | @username | ID>")
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            ChatPermissions(can_send_messages=True, can_send_polls=True, can_add_web_page_previews=True, can_invite_users=True)
        )
        await update.message.reply_text(f"🔊 {target.full_name} susturması kaldırıldı.")
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Başarısız.")

async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await warn_action(update, context, "normal")

async def swarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await warn_action(update, context, "silent")

async def dwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await warn_action(update, context, "delete")

async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /warns <reply | @username | ID>")
    cid = update.effective_chat.id
    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    count = row[0] if row else 0
    await update.message.reply_text(f"📌 {target.full_name} warn: {count}/{get_warn_limit(cid)}")

async def clearwarns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /clearwarns <reply | @username | ID>")
    cid = update.effective_chat.id
    cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    conn.commit()
    await update.message.reply_text(f"🧽 {target.full_name} warnları sıfırlandı.")
    await silent_delete_command(update, context)

async def resetwarns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /resetwarns <reply | @username | ID>")
    cid = update.effective_chat.id
    cursor.execute("DELETE FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    conn.commit()
    await update.message.reply_text(f"✅ {target.full_name} warn kayıtları silindi.")
    await silent_delete_command(update, context)

async def delwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /delwarn <reply | @username | ID>")
    cid = update.effective_chat.id
    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    current = max((row[0] if row else 0) - 1, 0)
    cursor.execute("INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)", (cid, target.id, current))
    conn.commit()
    await update.message.reply_text(f"✅ 1 warn silindi. Kalan: {current}")
    await silent_delete_command(update, context)

# ====================== PROMOTE / DEMOTE ======================
async def promote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    actor = update.effective_user
    if not await is_owner(cid, actor.id, context) and not is_sudo(actor.id):
        return await update.message.reply_text("Sadece owner.")
    if not await bot_can_promote(cid, context):
        return await update.message.reply_text("Botun promote yetkisi yok.")
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /promote <reply | @username | ID>")
    try:
        await context.bot.promote_chat_member(
            cid, target.id, can_manage_chat=True, can_delete_messages=True,
            can_restrict_members=True, can_pin_messages=True, can_invite_users=True
        )
        await update.message.reply_text(f"👑 {target.full_name} admin yapıldı.")
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Başarısız.")

async def demote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    actor = update.effective_user
    if not await is_owner(cid, actor.id, context) and not is_sudo(actor.id):
        return await update.message.reply_text("Sadece owner.")
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /demote <reply | @username | ID>")
    try:
        await context.bot.promote_chat_member(
            cid, target.id, can_manage_chat=False, can_delete_messages=False,
            can_restrict_members=False, can_pin_messages=False, can_invite_users=False
        )
        await update.message.reply_text(f"⬇️ {target.full_name} adminlikten alındı.")
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Başarısız.")

# ====================== APPROVE ======================
async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /approve <reply | @username | ID>")
    cid = update.effective_chat.id
    cursor.execute("INSERT OR IGNORE INTO approvals (chat_id, user_id) VALUES (?, ?)", (cid, target.id))
    conn.commit()
    await update.message.reply_text(f"✅ {target.full_name} approved.")
    await silent_delete_command(update, context)

async def unapprove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /unapprove <reply | @username | ID>")
    cid = update.effective_chat.id
    cursor.execute("DELETE FROM approvals WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    conn.commit()
    await update.message.reply_text(f"✅ {target.full_name} unapproved.")
    await silent_delete_command(update, context)

async def approved_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    cursor.execute("SELECT user_id FROM approvals WHERE chat_id = ?", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Approved listesi boş.")
    text = "✅ Approved:\n" + "\n".join([f"• <code>{r[0]}</code>" for r in rows])
    await update.message.reply_text(text, parse_mode="HTML")

# ====================== SUDO ======================
async def addsudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_chat.id, update.effective_user.id, context):
        return await update.message.reply_text("Sadece owner.")
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /addsudo <reply | @username | ID>")
    cursor.execute("INSERT OR IGNORE INTO sudo_users (user_id) VALUES (?)", (target.id,))
    conn.commit()
    await update.message.reply_text(f"👑 {target.full_name} sudo eklendi.")

async def delsudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update.effective_chat.id, update.effective_user.id, context):
        return await update.message.reply_text("Sadece owner.")
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /delsudo <reply | @username | ID>")
    cursor.execute("DELETE FROM sudo_users WHERE user_id = ?", (target.id,))
    conn.commit()
    await update.message.reply_text(f"✅ {target.full_name} sudo silindi.")

async def sudolist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT user_id FROM sudo_users")
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Sudo listesi boş.")
    text = "👑 Sudo:\n" + "\n".join([f"• <code>{r[0]}</code>" for r in rows])
    await update.message.reply_text(text, parse_mode="HTML")

# ====================== PIN / PURGE / DEL ======================
async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Mesaja reply at.")
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Botun pin yetkisi yok.")
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, update.message.reply_to_message.message_id)
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Başarısız.")

async def unpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    try:
        await context.bot.unpin_chat_message(update.effective_chat.id)
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Başarısız.")

async def unpinall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    try:
        await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Başarısız.")

async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Mesaja reply at.")
    if not await bot_can_delete(update.effective_chat.id, context):
        return await update.message.reply_text("Botun silme yetkisi yok.")
    try:
        await update.message.reply_to_message.delete()
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Başarısız.")

async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Mesaja reply at.")
    if not await bot_can_delete(update.effective_chat.id, context):
        return await update.message.reply_text("Botun silme yetkisi yok.")
    start_id = update.message.reply_to_message.message_id
    end_id = update.message.message_id
    if end_id - start_id > PURGE_LIMIT:
        return await update.message.reply_text(f"Max {PURGE_LIMIT} mesaj.")
    deleted = 0
    for mid in range(start_id, end_id + 1):
        try:
            await context.bot.delete_message(update.effective_chat.id, mid)
            deleted += 1
        except:
            pass
    info = await context.bot.send_message(update.effective_chat.id, f"🧹 {deleted} mesaj silindi.")
    asyncio.create_task(delete_later(info, 5))

# ====================== AYARLAR ======================
async def toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, label: str):
    if not await require_admin(update, context):
        return
    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        return await update.message.reply_text(f"Kullanım: /{field} on|off")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    val = 1 if args[0].lower() == "on" else 0
    cursor.execute(f"UPDATE chat_settings SET {field} = ? WHERE chat_id = ?", (val, cid))
    conn.commit()
    await update.message.reply_text(f"⚙️ {label}: {'✅' if val else '❌'}")
    await silent_delete_command(update, context)

async def antilink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "antilink", "Antilink")

async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "welcome", "Welcome")

async def goodbye_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "goodbye", "Goodbye")

async def antispam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "antispam", "AntiSpam")

async def raid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "raid_mode", "Raid Mode")

async def cleancommands_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "clean_commands", "Clean Commands")

async def cleanservice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "clean_service", "Clean Service")

async def reports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "reports_enabled", "Reports")

async def setwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /setwelcome <mesaj>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    text = " ".join(context.args)
    cursor.execute("UPDATE chat_settings SET welcome_text = ? WHERE chat_id = ?", (text, cid))
    conn.commit()
    await update.message.reply_text("✅ Karşılama mesajı güncellendi.")
    await silent_delete_command(update, context)

async def setgoodbye_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /setgoodbye <mesaj>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    text = " ".join(context.args)
    cursor.execute("UPDATE chat_settings SET goodbye_text = ? WHERE chat_id = ?", (text, cid))
    conn.commit()
    await update.message.reply_text("✅ Güle güle mesajı güncellendi.")
    await silent_delete_command(update, context)

async def setrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /setrules <kurallar>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    text = " ".join(context.args)
    cursor.execute("UPDATE chat_settings SET rules_text = ? WHERE chat_id = ?", (text, cid))
    conn.commit()
    await update.message.reply_text("✅ Kurallar güncellendi.")
    await silent_delete_command(update, context)

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("SELECT rules_text FROM chat_settings WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    text = row[0] if row and row[0] else "Kurallar ayarlanmadı."
    await update.message.reply_text(f"📜 Kurallar:\n{text}")

async def setlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    target = int(context.args[0]) if context.args and context.args[0].lstrip("-").isdigit() else cid
    try:
        await context.bot.send_message(target, f"✅ Log test - Kaynak: {cid}")
    except:
        return await update.message.reply_text("Bu chat'e mesaj atamıyorum.")
    cursor.execute("UPDATE chat_settings SET log_chat_id = ? WHERE chat_id = ?", (target, cid))
    conn.commit()
    await update.message.reply_text(f"✅ Log: <code>{target}</code>", parse_mode="HTML")
    await silent_delete_command(update, context)

async def logoff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    cursor.execute("UPDATE chat_settings SET log_chat_id = 0 WHERE chat_id = ?", (cid,))
    conn.commit()
    await update.message.reply_text("✅ Log kapatıldı.")
    await silent_delete_command(update, context)

async def setwarnlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Kullanım: /setwarnlimit <1-20>")
    val = int(context.args[0])
    if not 1 <= val <= 20:
        return await update.message.reply_text("1-20 arası olmalı.")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET warn_limit = ? WHERE chat_id = ?", (val, cid))
    conn.commit()
    await update.message.reply_text(f"✅ Warn limit: {val}")
    await silent_delete_command(update, context)

async def warnmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("ban", "mute", "kick"):
        return await update.message.reply_text("Kullanım: /warnmode ban|mute|kick")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET warn_mode = ? WHERE chat_id = ?", (context.args[0].lower(), cid))
    conn.commit()
    await update.message.reply_text(f"✅ Warn mode: {context.args[0]}")
    await silent_delete_command(update, context)

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("""SELECT antilink, welcome, goodbye, lock_link, lock_badword, lock_flood, antispam,
                      raid_mode, lock_sticker, lock_media, lock_forward, lock_bots, clean_commands,
                      clean_service, reports_enabled, warn_limit, warn_mode FROM chat_settings WHERE chat_id = ?""", (cid,))
    row = cursor.fetchone()
    if not row:
        return await update.message.reply_text("Ayarlar yok.")
    labels = ["Antilink", "Welcome", "Goodbye", "Lock Link", "Lock Badword", "Lock Flood", "AntiSpam",
              "Raid Mode", "Lock Sticker", "Lock Media", "Lock Forward", "Lock Bots", "Clean Cmd",
              "Clean Service", "Reports"]
    text = "⚙️ <b>Ayarlar</b>\n\n"
    for i, lbl in enumerate(labels):
        text += f"• {lbl}: {'✅' if row[i] else '❌'}\n"
    text += f"\n• Warn Limit: {row[15]}\n• Warn Mode: {row[16]}"
    await update.message.reply_text(text, parse_mode="HTML")

# ====================== LOCK / UNLOCK ======================
async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /lock link|badword|flood|sticker|media|forward|bot|photo|video|document|voice")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    field_map = {
        "link": "lock_link", "badword": "lock_badword", "flood": "lock_flood",
        "sticker": "lock_sticker", "media": "lock_media", "forward": "lock_forward",
        "bot": "lock_bots", "bots": "lock_bots", "photo": "lock_photo",
        "video": "lock_video", "document": "lock_document", "voice": "lock_voice"
    }
    field = field_map.get(context.args[0].lower())
    if not field:
        return await update.message.reply_text("Geçersiz tip.")
    cursor.execute(f"UPDATE chat_settings SET {field} = 1 WHERE chat_id = ?", (cid,))
    conn.commit()
    await update.message.reply_text(f"🔒 {context.args[0]} kilitlendi.")
    await silent_delete_command(update, context)

async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /unlock link|badword|flood|sticker|media|forward|bot|photo|video|document|voice")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    field_map = {
        "link": "lock_link", "badword": "lock_badword", "flood": "lock_flood",
        "sticker": "lock_sticker", "media": "lock_media", "forward": "lock_forward",
        "bot": "lock_bots", "bots": "lock_bots", "photo": "lock_photo",
        "video": "lock_video", "document": "lock_document", "voice": "lock_voice"
    }
    field = field_map.get(context.args[0].lower())
    if not field:
        return await update.message.reply_text("Geçersiz tip.")
    cursor.execute(f"UPDATE chat_settings SET {field} = 0 WHERE chat_id = ?", (cid,))
    conn.commit()
    await update.message.reply_text(f"🔓 {context.args[0]} açıldı.")
    await silent_delete_command(update, context)

async def locks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("""SELECT lock_link, lock_badword, lock_flood, lock_sticker, lock_media, lock_forward,
                      lock_bots, lock_photo, lock_video, lock_document, lock_voice FROM chat_settings WHERE chat_id = ?""", (cid,))
    row = cursor.fetchone()
    if not row:
        return await update.message.reply_text("Ayarlar yok.")
    labels = ["Link", "Badword", "Flood", "Sticker", "Media", "Forward", "Bots", "Photo", "Video", "Document", "Voice"]
    text = "🔐 <b>Locks</b>\n\n" + "\n".join([f"• {l}: {'✅' if row[i] else '❌'}" for i, l in enumerate(labels)])
    await update.message.reply_text(text, parse_mode="HTML")

# ====================== BADWORD ======================
async def addbad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /addbad <kelime>")
    cid = update.effective_chat.id
    word = context.args[0].lower().strip()
    cursor.execute("INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?, ?)", (cid, word))
    conn.commit()
    await update.message.reply_text(f"✅ Badword eklendi: {word}")
    await silent_delete_command(update, context)

async def delbad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /delbad <kelime>")
    cid = update.effective_chat.id
    word = context.args[0].lower().strip()
    cursor.execute("DELETE FROM badwords WHERE chat_id = ? AND word = ?", (cid, word))
    conn.commit()
    await update.message.reply_text(f"🗑️ Silindi: {word}")
    await silent_delete_command(update, context)

async def badlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    cursor.execute("SELECT word FROM badwords WHERE chat_id = ?", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Badword listesi boş.")
    text = "🚫 Badwords:\n" + "\n".join([f"• {r[0]}" for r in rows])
    await update.message.reply_text(text)

# ====================== BLACKLIST ======================
async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /blacklist <tetik>")
    cid = update.effective_chat.id
    trigger = " ".join(context.args).lower().strip()
    cursor.execute("INSERT OR IGNORE INTO blacklists (chat_id, trigger_text) VALUES (?, ?)", (cid, trigger))
    conn.commit()
    await update.message.reply_text(f"✅ Blacklist eklendi: {trigger}")
    await silent_delete_command(update, context)

async def rmblacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /rmblacklist <tetik>")
    cid = update.effective_chat.id
    trigger = " ".join(context.args).lower().strip()
    cursor.execute("DELETE FROM blacklists WHERE chat_id = ? AND trigger_text = ?", (cid, trigger))
    conn.commit()
    await update.message.reply_text(f"🗑️ Silindi: {trigger}")
    await silent_delete_command(update, context)

async def blacklists_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    cursor.execute("SELECT trigger_text FROM blacklists WHERE chat_id = ?", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Blacklist boş.")
    text = "⛔ Blacklist:\n" + "\n".join([f"• {r[0]}" for r in rows])
    await update.message.reply_text(text)

# ====================== NOTES ======================
async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if len(context.args) < 2:
        return await update.message.reply_text("Kullanım: /save <isim> <metin>")
    cid = update.effective_chat.id
    name = context.args[0].lower()
    text = " ".join(context.args[1:])
    cursor.execute("INSERT OR REPLACE INTO notes (chat_id, note_name, note_text) VALUES (?, ?, ?)", (cid, name, text))
    conn.commit()
    await update.message.reply_text(f"💾 Not kaydedildi: #{name}")
    await silent_delete_command(update, context)

async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Kullanım: /get <isim>")
    cid = update.effective_chat.id
    name = context.args[0].lower()
    cursor.execute("SELECT note_text FROM notes WHERE chat_id = ? AND note_name = ?", (cid, name))
    row = cursor.fetchone()
    if not row:
        return await update.message.reply_text("Not bulunamadı.")
    await update.message.reply_text(row[0])

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /clear <isim>")
    cid = update.effective_chat.id
    name = context.args[0].lower()
    cursor.execute("DELETE FROM notes WHERE chat_id = ? AND note_name = ?", (cid, name))
    conn.commit()
    await update.message.reply_text(f"🗑️ Not silindi: #{name}")
    await silent_delete_command(update, context)

async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    cursor.execute("SELECT note_name FROM notes WHERE chat_id = ?", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Not yok.")
    text = "📝 Notlar:\n" + "\n".join([f"• #{r[0]}" for r in rows])
    await update.message.reply_text(text)

# ====================== FILTERS ======================
async def filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if len(context.args) < 2:
        return await update.message.reply_text("Kullanım: /filter <tetik> <cevap>")
    cid = update.effective_chat.id
    trigger = context.args[0].lower()
    reply = " ".join(context.args[1:])
    cursor.execute("INSERT OR REPLACE INTO filters_table (chat_id, trigger_text, reply_text) VALUES (?, ?, ?)", (cid, trigger, reply))
    conn.commit()
    await update.message.reply_text(f"✅ Filter eklendi: {trigger}")
    await silent_delete_command(update, context)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /stop <tetik>")
    cid = update.effective_chat.id
    trigger = context.args[0].lower()
    cursor.execute("DELETE FROM filters_table WHERE chat_id = ? AND trigger_text = ?", (cid, trigger))
    conn.commit()
    await update.message.reply_text(f"🗑️ Filter silindi: {trigger}")
    await silent_delete_command(update, context)

async def filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    cursor.execute("SELECT trigger_text FROM filters_table WHERE chat_id = ?", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Filter yok.")
    text = "🔎 Filters:\n" + "\n".join([f"• {r[0]}" for r in rows])
    await update.message.reply_text(text)

# ====================== INFO KOMUTLARI ======================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"👋 Merhaba! Ben {BOT_USERNAME}\n\nGrup yönetim ve koruma botuyum.\n/help ile komutları gör."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Gruba Ekle", url=f"https://t.me/{BOT_USERNAME[1:]}?startgroup=true")],
        [InlineKeyboardButton("🆘 Destek", url=SUPPORT_URL)]
    ])
    await update.message.reply_text(text, reply_markup=kb)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """📚 <b>Komutlar</b>

<b>👮 Moderasyon:</b>
/ban /tban /sban /unban /kick
/mute /tmute /smute /unmute
/warn /swarn /dwarn /warns /clearwarns /resetwarns /delwarn
/promote /demote /pin /unpin /unpinall /del /purge

<b>⚙️ Ayarlar:</b>
/antilink /welcome /goodbye /antispam /raid
/cleancommands /cleanservice /reports
/setwelcome /setgoodbye /setrules /setlog /logoff
/setwarnlimit /warnmode /settings

<b>🔒 Kilitler:</b>
/lock /unlock /locks

<b>🚫 Filtreleme:</b>
/addbad /delbad /badlist
/blacklist /rmblacklist /blacklists

<b>📝 Notlar:</b>
/save /get /clear /notes
/filter /stop /filters

<b>👑 Yetki:</b>
/approve /unapprove /approved
/addsudo /delsudo /sudolist

<b>ℹ️ Bilgi:</b>
/start /help /ping /id /rules /stats /admins"""
    await update.message.reply_text(text, parse_mode="HTML")

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start = time.time()
    msg = await update.message.reply_text("🏓 Pong...")
    await msg.edit_text(f"🏓 {round((time.time() - start) * 1000)}ms")

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = update.effective_chat.id
    text = f"👤 User ID: <code>{uid}</code>\n💬 Chat ID: <code>{cid}</code>"
    if update.message.reply_to_message:
        rid = update.message.reply_to_message.from_user.id
        text += f"\n🎯 Reply ID: <code>{rid}</code>"
    await update.message.reply_text(text, parse_mode="HTML")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("SELECT total_bans, total_mutes, total_warns, total_deleted FROM mod_stats WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    if not row:
        return await update.message.reply_text("İstatistik yok.")
    text = f"📊 <b>Grup İstatistikleri</b>\n\n• Banlar: {row[0]}\n• Muteler: {row[1]}\n• Warnlar: {row[2]}\n• Silinen: {row[3]}"
    await update.message.reply_text(text, parse_mode="HTML")

async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        text = "👮 <b>Adminler</b>\n\n"
        for a in admins:
            title = a.custom_title or ("Owner" if a.status == ChatMemberStatus.OWNER else "Admin")
            text += f"• {html.escape(a.user.full_name)} - {title}\n"
        await update.message.reply_text(text, parse_mode="HTML")
    except:
        await update.message.reply_text("Admin listesi alınamadı.")

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("Mesaja reply at.")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("SELECT reports_enabled FROM chat_settings WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    if not row or row[0] != 1:
        return await update.message.reply_text("Report kapalı.")
    target = update.message.reply_to_message.from_user
    reporter = update.effective_user
    try:
        admins = await context.bot.get_chat_administrators(cid)
        mentions = " ".join([f"@{a.user.username}" for a in admins if a.user.username and not a.user.is_bot][:5])
        await update.message.reply_text(
            f"🚨 <b>Report</b>\n• Bildiren: {reporter.mention_html()}\n• Hedef: {target.mention_html()}\n\n{mentions}",
            parse_mode="HTML"
        )
    except:
        await update.message.reply_text("Report gönderilemedi.")

async def invitelink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    try:
        link = await context.bot.export_chat_invite_link(update.effective_chat.id)
        await update.message.reply_text(f"🔗 {link}")
    except:
        await update.message.reply_text("Link alınamadı.")

# ====================== MESAJ HANDLER ======================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or update.effective_user.is_bot:
        return
    if is_private(update):
        return

    cid = update.effective_chat.id
    uid = update.effective_user.id
    ensure_chat_settings(cid)

    # İstatistik
    cursor.execute("INSERT INTO stats (chat_id, user_id, msg_count, deleted_count) VALUES (?, ?, 1, 0) ON CONFLICT(chat_id, user_id) DO UPDATE SET msg_count = msg_count + 1", (cid, uid))
    conn.commit()

    # Yeni üye
    if update.message.new_chat_members:
        cursor.execute("SELECT welcome, welcome_text, lock_bots FROM chat_settings WHERE chat_id = ?", (cid,))
        row = cursor.fetchone()
        for nm in update.message.new_chat_members:
            if row and row[2] == 1 and nm.is_bot:
                try:
                    await context.bot.ban_chat_member(cid, nm.id)
                except:
                    pass
                continue
            if row and row[0] == 1 and not nm.is_bot:
                text = row[1].replace("{first}", nm.first_name or "").replace("{username}", f"@{nm.username}" if nm.username else "")
                await context.bot.send_message(cid, f"👋 {nm.mention_html()}\n{text}", parse_mode="HTML")
        return

    # Çıkan üye
    if update.message.left_chat_member:
        cursor.execute("SELECT goodbye, goodbye_text FROM chat_settings WHERE chat_id = ?", (cid,))
        row = cursor.fetchone()
        if row and row[0] == 1:
            left = update.message.left_chat_member
            text = row[1].replace("{first}", left.first_name or "")
            await context.bot.send_message(cid, text)
        return

    text = (update.message.text or update.message.caption or "").strip()
    lowered = text.lower()

    # Admin veya approved ise atla
    if await is_admin_user(cid, uid, context) or is_approved(cid, uid):
        # Sadece not ve filter kontrol et
        if text.startswith("#"):
            name = text[1:].split()[0].lower()
            cursor.execute("SELECT note_text FROM notes WHERE chat_id = ? AND note_name = ?", (cid, name))
            row = cursor.fetchone()
            if row:
                await update.message.reply_text(row[0])
        if lowered:
            cursor.execute("SELECT trigger_text, reply_text FROM filters_table WHERE chat_id = ?", (cid,))
            for trig, rep in cursor.fetchall():
                if trig in lowered:
                    await update.message.reply_text(rep)
                    break
        return

    # Ayarları al
    cursor.execute("""SELECT antilink, lock_link, lock_badword, lock_flood, antispam, raid_mode,
                      lock_sticker, lock_media, lock_forward FROM chat_settings WHERE chat_id = ?""", (cid,))
    row = cursor.fetchone()
    if not row:
        return

    antilink, lock_link, lock_badword, lock_flood, antispam, raid_mode, lock_sticker, lock_media, lock_forward = row
    delete_reason = None

    # Blacklist
    cursor.execute("SELECT trigger_text FROM blacklists WHERE chat_id = ?", (cid,))
    for r in cursor.fetchall():
        if r[0] in lowered:
            delete_reason = "Blacklist"
            break

    # Antilink / Lock link
    if not delete_reason and (antilink or lock_link):
        if URL_PATTERN.search(text):
            delete_reason = "Link"

    # Badword
    if not delete_reason and lock_badword:
        cursor.execute("SELECT word FROM badwords WHERE chat_id = ?", (cid,))
        for r in cursor.fetchall():
            if r[0] in lowered:
                delete_reason = "Badword"
                break

    # Forward
    if not delete_reason and lock_forward and (update.message.forward_from or update.message.forward_from_chat):
        delete_reason = "Forward"

    # Sticker
    if not delete_reason and lock_sticker and update.message.sticker:
        delete_reason = "Sticker"

    # Media
    if not delete_reason and lock_media and (update.message.photo or update.message.video or update.message.document):
        delete_reason = "Media"

    # Flood/Spam
    if not delete_reason and lock_flood and antispam:
        now = time.time()
        spam_tracker[(cid, uid)].append(now)
        spam_tracker[(cid, uid)] = [t for t in spam_tracker[(cid, uid)] if now - t < SPAM_WINDOW]
        if len(spam_tracker[(cid, uid)]) > SPAM_LIMIT:
            delete_reason = "Spam"
            spam_tracker[(cid, uid)].clear()

    # Raid mode
    if not delete_reason and raid_mode:
        delete_reason = "Raid mode"

    # Sil ve uyar
    if delete_reason:
        try:
            if await bot_can_delete(cid, context):
                await update.message.delete()
                inc_mod_stat(cid, "total_deleted")
                warn = await context.bot.send_message(
                    cid,
                    f"⚠️ {update.effective_user.mention_html()} mesajı silindi: <b>{delete_reason}</b>",
                    parse_mode="HTML"
                )
                asyncio.create_task(delete_later(warn, 5))
                await send_log(context, cid, f"Silindi: {update.effective_user.full_name} - {delete_reason}")
        except Exception as e:
            logger.error(f"Silme hatası: {e}")
        return

    # Not kontrolü
    if text.startswith("#"):
        name = text[1:].split()[0].lower()
        cursor.execute("SELECT note_text FROM notes WHERE chat_id = ? AND note_name = ?", (cid, name))
        row = cursor.fetchone()
        if row:
            await update.message.reply_text(row[0])

    # Filter kontrolü
    if lowered:
        cursor.execute("SELECT trigger_text, reply_text FROM filters_table WHERE chat_id = ?", (cid,))
        for trig, rep in cursor.fetchall():
            if trig in lowered:
                await update.message.reply_text(rep)
                break

# ====================== DOT COMMAND HANDLER ======================
async def dot_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text.startswith("."):
        return

    parts = text[1:].split()
    if not parts:
        return

    cmd = parts[0].lower()
    context.args = parts[1:]

    mapping = {
        "ban": ban_cmd, "tban": tban_cmd, "sban": sban_cmd, "unban": unban_cmd, "kick": kick_cmd,
        "mute": mute_cmd, "tmute": tmute_cmd, "smute": smute_cmd, "unmute": unmute_cmd,
        "warn": warn_cmd, "swarn": swarn_cmd, "dwarn": dwarn_cmd, "warns": warns_cmd,
        "clearwarns": clearwarns_cmd, "resetwarns": resetwarns_cmd, "delwarn": delwarn_cmd,
        "promote": promote_cmd, "demote": demote_cmd,
        "approve": approve_cmd, "unapprove": unapprove_cmd, "approved": approved_cmd,
        "addsudo": addsudo_cmd, "delsudo": delsudo_cmd, "sudolist": sudolist_cmd,
        "pin": pin_cmd, "unpin": unpin_cmd, "unpinall": unpinall_cmd, "del": del_cmd, "purge": purge_cmd,
        "antilink": antilink_cmd, "welcome": welcome_cmd, "goodbye": goodbye_cmd,
        "antispam": antispam_cmd, "raid": raid_cmd, "cleancommands": cleancommands_cmd,
        "cleanservice": cleanservice_cmd, "reports": reports_cmd,
        "setwelcome": setwelcome_cmd, "setgoodbye": setgoodbye_cmd, "setrules": setrules_cmd,
        "setlog": setlog_cmd, "logoff": logoff_cmd, "setwarnlimit": setwarnlimit_cmd,
        "warnmode": warnmode_cmd, "settings": settings_cmd,
        "lock": lock_cmd, "unlock": unlock_cmd, "locks": locks_cmd,
        "addbad": addbad_cmd, "delbad": delbad_cmd, "badlist": badlist_cmd,
        "blacklist": blacklist_cmd, "rmblacklist": rmblacklist_cmd, "blacklists": blacklists_cmd,
        "save": save_cmd, "get": get_cmd, "clear": clear_cmd, "notes": notes_cmd,
        "filter": filter_cmd, "stop": stop_cmd, "filters": filters_cmd,
        "ping": ping_cmd, "id": id_cmd, "rules": rules_cmd, "stats": stats_cmd, "admins": admins_cmd,
        "report": report_cmd, "invitelink": invitelink_cmd,
    }

    func = mapping.get(cmd)
    if func:
        await func(update, context)

# ====================== MAIN ======================
def main():
    if not TOKEN:
        print("❌ BOT_TOKEN bulunamadı!")
        return

    app = Application.builder().token(TOKEN).build()

    # Komutlar
    commands = [
        ("start", start_cmd), ("help", help_cmd), ("ping", ping_cmd), ("id", id_cmd),
        ("ban", ban_cmd), ("tban", tban_cmd), ("sban", sban_cmd), ("unban", unban_cmd), ("kick", kick_cmd),
        ("mute", mute_cmd), ("tmute", tmute_cmd), ("smute", smute_cmd), ("unmute", unmute_cmd),
        ("warn", warn_cmd), ("swarn", swarn_cmd), ("dwarn", dwarn_cmd), ("warns", warns_cmd),
        ("clearwarns", clearwarns_cmd), ("resetwarns", resetwarns_cmd), ("delwarn", delwarn_cmd),
        ("promote", promote_cmd), ("demote", demote_cmd),
        ("approve", approve_cmd), ("unapprove", unapprove_cmd), ("approved", approved_cmd),
        ("addsudo", addsudo_cmd), ("delsudo", delsudo_cmd), ("sudolist", sudolist_cmd),
        ("pin
