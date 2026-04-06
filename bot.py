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
DB_PATH = os.getenv("DB_PATH", "bot_database.db")

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
MAX_BLACKLIST_TRIGGER = 128

REPEAT_WINDOW = 120
REPEAT_LIMIT = 5
AUTO_MUTE_MINUTES = 30

SEARCH_COOLDOWN = 15
CALLBACK_COOLDOWN = 1.5

JOIN_WINDOW = 30
JOIN_LIMIT = 5
RAID_MUTE_MINUTES = 30

STICKER_WINDOW = 10
STICKER_LIMIT = 5

MEDIA_WINDOW = 10
MEDIA_LIMIT = 5

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("kgb_rose_plus_single_final")

db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
conn.execute("PRAGMA foreign_keys=ON;")
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
    lock_badword INTEGER DEFAULT 1,
    lock_flood INTEGER DEFAULT 1,
    antispam INTEGER DEFAULT 1,
    raid_mode INTEGER DEFAULT 0
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

CREATE TABLE IF NOT EXISTS punish_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    action TEXT,
    reason TEXT,
    actor_id INTEGER,
    ts INTEGER
);

CREATE TABLE IF NOT EXISTS strikes (
    chat_id INTEGER,
    user_id INTEGER,
    strike_count INTEGER DEFAULT 0,
    UNIQUE(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS sudo_users (
    user_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS blacklists (
    chat_id INTEGER,
    trigger_text TEXT,
    UNIQUE(chat_id, trigger_text)
);

CREATE TABLE IF NOT EXISTS approvals (
    chat_id INTEGER,
    user_id INTEGER,
    UNIQUE(chat_id, user_id)
);
""")
conn.commit()


def safe_add_column(table: str, column_def: str):
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        conn.commit()
    except sqlite3.OperationalError:
        pass


safe_add_column("chat_settings", "warn_limit INTEGER DEFAULT 3")
safe_add_column("chat_settings", "warn_mode TEXT DEFAULT 'ban'")
safe_add_column("chat_settings", "lock_sticker INTEGER DEFAULT 0")
safe_add_column("chat_settings", "lock_media INTEGER DEFAULT 0")
safe_add_column("chat_settings", "clean_commands INTEGER DEFAULT 0")
safe_add_column("chat_settings", "reports_enabled INTEGER DEFAULT 1")
safe_add_column("chat_settings", "clean_service INTEGER DEFAULT 0")
safe_add_column("chat_settings", "lock_forward INTEGER DEFAULT 0")
safe_add_column("chat_settings", "lock_bots INTEGER DEFAULT 0")
safe_add_column("chat_settings", "lock_photo INTEGER DEFAULT 0")
safe_add_column("chat_settings", "lock_video INTEGER DEFAULT 0")
safe_add_column("chat_settings", "lock_document INTEGER DEFAULT 0")
safe_add_column("chat_settings", "lock_voice INTEGER DEFAULT 0")

spam_tracker = defaultdict(list)
repeat_tracker = defaultdict(list)
search_cooldowns = {}
callback_cooldowns = {}
join_tracker = defaultdict(list)
sticker_tracker = defaultdict(list)
media_tracker = defaultdict(list)

URL_PATTERN = re.compile(
    r"(?i)\b(?:https?://|www\.|t\.me/|telegram\.me/|discord\.gg/|discord\.com/invite/|telegram\.dog/|[a-z0-9-]+\.(?:com|net|org|gg|me|io|xyz|ru|co)\S*)"
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


def normalize_message_for_repeat(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def normalize_text_strong(text: str) -> str:
    text = text.lower()
    text = text.replace("hxxp", "http")
    text = text.replace("[.]", ".")
    text = text.replace("(.)", ".")
    text = text.replace(" dot ", ".")
    text = text.replace(" d0t ", ".")
    text = text.replace(" t me ", " t.me ")
    text = text.replace("telegram dot me", "telegram.me")
    text = text.replace("discord dot gg", "discord.gg")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))

    tr_map = str.maketrans({
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
        "@": "a", "$": "s", "€": "e", "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"
    })
    text = text.translate(tr_map)
    text = re.sub(r"[^\w\s./:-]", "", text)
    text = re.sub(r"(.)\1{2,}", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_badword_word(word: str) -> str:
    return normalize_text_strong(word).replace(" ", "")


def normalize_link_text(text: str) -> str:
    text = normalize_text_strong(text)
    text = text.replace(" / ", "/").replace(" . ", ".")
    text = re.sub(r"\s*\.\s*", ".", text)
    text = re.sub(r"\s*/\s*", "/", text)
    return text


def apply_placeholders(template: str, user, chat, count: int = 0) -> str:
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    username = f"@{user.username}" if getattr(user, "username", None) else "yok"
    fullname = (f"{first} {last}").strip()
    chatname = getattr(chat, "title", "") or ""
    result = template
    result = result.replace("{first}", first)
    result = result.replace("{last}", last)
    result = result.replace("{fullname}", fullname)
    result = result.replace("{username}", username)
    result = result.replace("{id}", str(user.id))
    result = result.replace("{chatname}", chatname)
    result = result.replace("{count}", str(count))
    return result


def add_punish_history(chat_id: int, user_id: int, action: str, reason: str, actor_id: int):
    cursor.execute("""
    INSERT INTO punish_history (chat_id, user_id, action, reason, actor_id, ts)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (chat_id, user_id, action, reason, actor_id, int(time.time())))
    conn.commit()


def get_strike_count(chat_id: int, user_id: int) -> int:
    cursor.execute("SELECT strike_count FROM strikes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    row = cursor.fetchone()
    return row[0] if row else 0


def inc_strike(chat_id: int, user_id: int) -> int:
    current = get_strike_count(chat_id, user_id) + 1
    cursor.execute("""
    INSERT OR REPLACE INTO strikes (chat_id, user_id, strike_count)
    VALUES (?, ?, ?)
    """, (chat_id, user_id, current))
    conn.commit()
    return current


def clear_strikes(chat_id: int, user_id: int):
    cursor.execute("DELETE FROM strikes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()


def is_sudo_id(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM sudo_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None


def get_warn_limit(chat_id: int) -> int:
    ensure_chat_settings(chat_id)
    cursor.execute("SELECT warn_limit FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row and row[0]:
        return int(row[0])
    return MAX_WARNS


def get_warn_mode(chat_id: int) -> str:
    ensure_chat_settings(chat_id)
    cursor.execute("SELECT warn_mode FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    mode = row[0] if row and row[0] else "ban"
    if mode not in ("ban", "mute", "kick"):
        return "ban"
    return mode


def is_approved(chat_id: int, user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM approvals WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    return cursor.fetchone() is not None


async def get_member_status(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        return await context.bot.get_chat_member(chat_id, user_id)
    except Exception:
        return None


async def is_admin_user(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_sudo_id(user_id):
        return True
    member = await get_member_status(chat_id, user_id, context)
    return bool(member and member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER))


async def is_owner(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await get_member_status(chat_id, user_id, context)
    return bool(member and member.status == ChatMemberStatus.OWNER)


async def role_of(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    if await is_owner(chat_id, user_id, context):
        return "owner"
    if is_sudo_id(user_id):
        return "sudo"
    if await is_admin_user(chat_id, user_id, context):
        return "admin"
    return "member"


PERMS = {
    "view_basic": {"owner", "sudo", "admin", "member"},
    "mod_basic": {"owner", "sudo", "admin"},
    "settings_basic": {"owner", "sudo", "admin"},
    "promote": {"owner", "sudo"},
    "setlog": {"owner", "sudo"},
    "sudo_manage": {"owner"},
    "history_view": {"owner", "sudo", "admin"},
}


async def has_perm(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, perm: str) -> bool:
    role = await role_of(chat_id, user_id, context)
    return role in PERMS.get(perm, set())


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_private(update):
        if update.message:
            await update.message.reply_text("Bu komut grupta kullanılmalı.")
        return False
    ok = await has_perm(update.effective_chat.id, update.effective_user.id, context, "mod_basic")
    if not ok and update.message:
        await update.message.reply_text("Bu komutu kullanamazsın.")
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


async def bot_can_change_info(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    m = await bot_rights(chat_id, context)
    return bool(m and (getattr(m, "can_change_info", False) or m.status == ChatMemberStatus.OWNER))


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
    allowed = {"total_bans", "total_mutes", "total_warns", "total_deleted"}
    if field not in allowed:
        return
    cursor.execute(f"UPDATE mod_stats SET {field} = {field} + 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()


def inc_user_deleted(chat_id: int, user_id: int):
    cursor.execute("""
    INSERT INTO stats (chat_id, user_id, msg_count, deleted_count)
    VALUES (?, ?, 0, 1)
    ON CONFLICT(chat_id, user_id) DO UPDATE SET deleted_count = deleted_count + 1
    """, (chat_id, user_id))
    conn.commit()


async def silent_delete_command_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("SELECT clean_commands FROM chat_settings WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    clean_commands = bool(row and row[0] == 1)
    if clean_commands and await bot_can_delete(cid, context):
        try:
            await update.message.delete()
        except Exception:
            pass


async def maybe_delete_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("SELECT clean_service FROM chat_settings WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    if row and row[0] == 1 and await bot_can_delete(cid, context):
        try:
            await update.message.delete()
        except Exception:
            pass


async def extract_target_user_and_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_user = None
    reason = "Sebep belirtilmedi."
    raw_args = list(context.args) if context.args else []

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user = update.message.reply_to_message.from_user
        if raw_args:
            reason = " ".join(raw_args).strip()
        return target_user, reason

    if not raw_args:
        return None, reason

    first = raw_args[0]

    if first.isdigit():
        try:
            user_id = int(first)
            member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
            target_user = member.user
            if len(raw_args) > 1:
                reason = " ".join(raw_args[1:]).strip()
            return target_user, reason
        except Exception:
            return None, reason

    if first.startswith("@"):
        username = first[1:].lower()
        try:
            admins = await context.bot.get_chat_administrators(update.effective_chat.id)
            for adm in admins:
                if adm.user.username and adm.user.username.lower() == username:
                    target_user = adm.user
                    break
        except Exception:
            pass
        if target_user:
            if len(raw_args) > 1:
                reason = " ".join(raw_args[1:]).strip()
            return target_user, reason

    return None, reason


async def extract_target_time_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = None
    duration = None
    reason = "Sebep belirtilmedi."
    args = list(context.args) if context.args else []

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
        if not args:
            return target, None, reason
        duration = parse_time(args[0])
        if not duration:
            return target, None, reason
        if len(args) > 1:
            reason = " ".join(args[1:]).strip()
        return target, duration, reason

    if len(args) < 2:
        return None, None, reason

    first = args[0]
    second = args[1]

    if first.isdigit():
        try:
            member = await context.bot.get_chat_member(update.effective_chat.id, int(first))
            target = member.user
            duration = parse_time(second)
            if not duration:
                return target, None, reason
            if len(args) > 2:
                reason = " ".join(args[2:]).strip()
            return target, duration, reason
        except Exception:
            return None, None, reason

    return None, None, reason


async def can_act_on_target(chat_id: int, actor_id: int, target_id: int, context: ContextTypes.DEFAULT_TYPE):
    if actor_id == target_id:
        return False, "Kendine işlem yapamazsın."

    me = await context.bot.get_me()
    if target_id == me.id:
        return False, "Bana işlem yapamazsın."

    actor_role = await role_of(chat_id, actor_id, context)
    target_role = await role_of(chat_id, target_id, context)

    if target_role == "owner":
        return False, "Owner'a işlem yapamam."
    if target_role == "sudo" and actor_role != "owner":
        return False, "Sudo kullanıcıya işlem yapamazsın."
    if target_role == "admin" and actor_role not in ("owner", "sudo"):
        return False, "Admin kullanıcıya işlem yapamazsın."

    return True, None


async def auto_mute_user(chat_id: int, user, context: ContextTypes.DEFAULT_TYPE, reason: str, minutes: int = AUTO_MUTE_MINUTES):
    if not await bot_can_restrict(chat_id, context):
        return False
    until_date = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
    try:
        await context.bot.restrict_chat_member(
            chat_id,
            user.id,
            ChatPermissions(
                can_send_messages=False,
                can_send_polls=False,
                can_add_web_page_previews=False,
                can_invite_users=False
            ),
            until_date=until_date
        )
        inc_mod_stat(chat_id, "total_mutes")
        add_punish_history(chat_id, user.id, "AUTO_MUTE", reason, 0)
        await send_log(
            context,
            chat_id,
            f"<b>Eylem:</b> AUTO MUTE\n"
            f"<b>Hedef:</b> {html.escape(user.full_name)} (<code>{user.id}</code>)\n"
            f"<b>Süre:</b> {minutes} dakika\n"
            f"<b>Neden:</b> {html.escape(reason)}"
        )
        return True
    except Exception as e:
        logger.error(f"auto mute hatası: {e}")
        return False


async def execute_warn_limit_action(chat_id: int, target, actor, reason: str, context: ContextTypes.DEFAULT_TYPE):
    mode = get_warn_mode(chat_id)
    try:
        if mode == "ban":
            await context.bot.ban_chat_member(chat_id, target.id)
            inc_mod_stat(chat_id, "total_bans")
            add_punish_history(chat_id, target.id, "AUTO_BAN", reason, actor.id)
            cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
            conn.commit()
            await context.bot.send_message(chat_id, f"🚫 {html.escape(target.full_name)} warn limiti nedeniyle banlandı.", parse_mode="HTML")

        elif mode == "mute":
            until_date = datetime.datetime.now() + datetime.timedelta(days=1)
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
            inc_mod_stat(chat_id, "total_mutes")
            add_punish_history(chat_id, target.id, "AUTO_MUTE_WARNLIMIT", reason, actor.id)
            cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
            conn.commit()
            await context.bot.send_message(chat_id, f"🔇 {html.escape(target.full_name)} warn limiti nedeniyle 1 gün susturuldu.", parse_mode="HTML")

        elif mode == "kick":
            await context.bot.ban_chat_member(chat_id, target.id)
            await context.bot.unban_chat_member(chat_id, target.id)
            add_punish_history(chat_id, target.id, "AUTO_KICK_WARNLIMIT", reason, actor.id)
            cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
            conn.commit()
            await context.bot.send_message(chat_id, f"👢 {html.escape(target.full_name)} warn limiti nedeniyle gruptan atıldı.", parse_mode="HTML")

        await send_log(
            context,
            chat_id,
            f"<b>Eylem:</b> WARN LIMIT ACTION\n"
            f"<b>Mod:</b> {html.escape(mode)}\n"
            f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
            f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
            f"<b>Sebep:</b> {html.escape(reason)}"
        )
    except Exception as e:
        logger.error(f"warn limit action hatası: {e}")


async def apply_escalation(chat_id: int, target, actor_id: int, context: ContextTypes.DEFAULT_TYPE, base_reason: str):
    strike = inc_strike(chat_id, target.id)

    if strike == 1:
        cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
        row = cursor.fetchone()
        current_warns = (row[0] if row else 0) + 1
        cursor.execute(
            "INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)",
            (chat_id, target.id, current_warns)
        )
        conn.commit()
        inc_mod_stat(chat_id, "total_warns")
        add_punish_history(chat_id, target.id, "WARN", base_reason, actor_id)
        return f"⚠️ Strike {strike}: warn verildi. ({current_warns}/{get_warn_limit(chat_id)})"

    elif strike == 2:
        if not await bot_can_restrict(chat_id, context):
            return "Strike arttı ama mute yetkim yok."
        until_date = datetime.datetime.now() + datetime.timedelta(minutes=30)
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
        inc_mod_stat(chat_id, "total_mutes")
        add_punish_history(chat_id, target.id, "MUTE_30M", base_reason, actor_id)
        return "⚠️ Strike 2: 30 dakika susturuldu."

    elif strike == 3:
        if not await bot_can_restrict(chat_id, context):
            return "Strike arttı ama mute yetkim yok."
        until_date = datetime.datetime.now() + datetime.timedelta(days=1)
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
        inc_mod_stat(chat_id, "total_mutes")
        add_punish_history(chat_id, target.id, "MUTE_1D", base_reason, actor_id)
        return "⚠️ Strike 3: 1 gün susturuldu."

    else:
        if not await bot_can_restrict(chat_id, context):
            return "Strike arttı ama ban yetkim yok."
        await context.bot.ban_chat_member(chat_id, target.id)
        inc_mod_stat(chat_id, "total_bans")
        add_punish_history(chat_id, target.id, "BAN", base_reason, actor_id)
        clear_strikes(chat_id, target.id)
        return "🚫 Strike limiti doldu: kullanıcı banlandı."


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
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu_start")]])


def help_menu_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👮 Moderasyon", callback_data="help_mod"),
            InlineKeyboardButton("⚙️ Ayarlar", callback_data="help_settings")
        ],
        [
            InlineKeyboardButton("📝 Notes/Filters", callback_data="help_notes"),
            InlineKeyboardButton("📌 Diğer", callback_data="help_other")
        ],
        [InlineKeyboardButton("⬅️ Ana Menü", callback_data="menu_start")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"👋 Merhaba!\n"
        f"{BOT_USERNAME_TEXT} gelişmiş grup koruma, anti-raid ve moderasyon botudur.\n\n"
        "👉 Beni gruba ekleyin ve yönetici yapın.\n"
        "👉 Komutları hem / hem . ile kullanabilirsiniz."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_markup(), parse_mode="HTML")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    key = (q.from_user.id, q.message.chat_id if q.message else 0)
    now = time.time()
    last = callback_cooldowns.get(key, 0)
    if now - last < CALLBACK_COOLDOWN:
        return await q.answer("Yavaş.", show_alert=False)
    callback_cooldowns[key] = now

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
            "• /setlog\n• /antilink on\n• /welcome on\n• /setwelcome Hoş geldin {first}\n"
            "• /setrules Kurallar\n• /raid on\n• /antispam on\n• /reports on",
            reply_markup=back_menu_markup(),
            parse_mode="HTML"
        )
    if data == "menu_settings":
        return await q.message.edit_text(
            "⚙️ <b>Ayar Menüsü</b>\n\n"
            "/antilink on/off\n"
            "/welcome on/off\n"
            "/goodbye on/off\n"
            "/antispam on/off\n"
            "/raid on/off\n"
            "/reports on/off\n"
            "/cleancommands on/off\n"
            "/cleanservice on/off\n"
            "/setwelcome <mesaj>\n"
            "/setgoodbye <mesaj>\n"
            "/setlog [chat_id]\n"
            "/logoff\n"
            "/setrules <metin>\n"
            "/lock <tip>\n"
            "/unlock <tip>\n"
            "/setwarnlimit <sayı>\n"
            "/warnmode <ban|mute|kick>\n"
            "/settings",
            reply_markup=back_menu_markup(),
            parse_mode="HTML"
        )
    if data == "help_mod":
        return await q.message.edit_text(
            "👮 <b>Moderasyon</b>\n\n"
            "/ban /tban /sban /unban\n"
            "/mute /tmute /smute /stmute /unmute\n"
            "/kick\n"
            "/warn /swarn /dwarn /warns /clearwarns /resetwarns /delwarn\n"
            "/promote /demote\n"
            "/pin /unpin /unpinall /del /purge\n"
            "/approve /unapprove /approved",
            reply_markup=help_menu_markup(),
            parse_mode="HTML"
        )
    if data == "help_settings":
        return await q.message.edit_text(
            "⚙️ <b>Ayarlar</b>\n\n"
            "/antilink /welcome /goodbye /antispam /raid /reports\n"
            "/setwelcome /setgoodbye /setlog /logoff /setrules /settings\n"
            "/setwarnlimit /warnmode /cleancommands /cleanservice\n"
            "/lock /unlock /locks\n"
            "/addbad /delbad /badlist\n"
            "/blacklist /rmblacklist /blacklists\n"
            "/addsudo /delsudo /sudolist",
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
            "/ping /id /userinfo /stats /ara /rules\n"
            "/report\n/admins\n/invitelink\n/zombies\n/settitle\n/setdesc",
            reply_markup=help_menu_markup(),
            parse_mode="HTML"
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("📚 Kategori seç:", reply_markup=help_menu_markup(), parse_mode="HTML")


async def yardim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "🔧 Botu gruba ekle, yönetici yap, gerekli yetkileri ver. Komutlar / veya . ile çalışır.",
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
    role = await role_of(update.effective_chat.id, target.id, context) if update.effective_chat else "member"
    approved = is_approved(update.effective_chat.id, target.id) if update.effective_chat else False
    text = (
        f"👤 <b>Kullanıcı Bilgisi</b>\n\n"
        f"• ID: <code>{target.id}</code>\n"
        f"• Ad: {html.escape(target.full_name)}\n"
        f"• Username: @{target.username if target.username else 'yok'}\n"
        f"• Bot: {'Evet' if target.is_bot else 'Hayır'}\n"
        f"• Rol: {role}\n"
        f"• Approved: {'Evet' if approved else 'Hayır'}"
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
    cursor.execute("SELECT COUNT(*) FROM blacklists WHERE chat_id = ?", (cid,))
    blacklist_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM approvals WHERE chat_id = ?", (cid,))
    approved_count = cursor.fetchone()[0]
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
        f"• Blacklist tetik: <code>{blacklist_count}</code>\n"
        f"• Approved kullanıcı: <code>{approved_count}</code>\n"
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
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🆘 Destek", url=SUPPORT_URL)]])
    await update.message.reply_text(f"📜 <b>Grup Kuralları</b>\n\n{html.escape(text)}", parse_mode="HTML", reply_markup=kb)


async def ara(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Kullanım: /ara <sorgu>")

    key = (update.effective_chat.id, update.effective_user.id)
    now = time.time()
    if now - search_cooldowns.get(key, 0) < SEARCH_COOLDOWN:
        return await update.message.reply_text("Arama yapmak için biraz bekle.")
    search_cooldowns[key] = now

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
        result_text = "🔎 Arama Sonuçları\n\n" + "\n\n".join(lines)
        parts = list(split_text(result_text))
        await msg.edit_text(parts[0], disable_web_page_preview=True)
        for p in parts[1:]:
            await update.message.reply_text(p, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"arama hatası: {e}")
        await msg.edit_text("Arama sırasında hata oluştu.")


async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        lines = []
        for a in admins:
            user = a.user
            title = a.custom_title or ("Owner" if a.status == ChatMemberStatus.OWNER else "Admin")
            uname = f"@{user.username}" if user.username else f"<code>{user.id}</code>"
            lines.append(f"• {html.escape(user.full_name)} - {title} - {uname}")
        text = "👮 <b>Yöneticiler</b>\n\n" + "\n".join(lines)
        for p in split_text(text):
            await update.message.reply_text(p, parse_mode="HTML")
    except Exception:
        await update.message.reply_text("Admin listesi alınamadı.")


async def invitelink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    try:
        link = await context.bot.export_chat_invite_link(update.effective_chat.id)
        await update.message.reply_text(f"🔗 Davet linki:\n{link}")
    except Exception:
        await update.message.reply_text("Davet linki alınamadı. Botun davet linki yetkisi olmayabilir.")


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir mesaja yanıt verip /report kullan.")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("SELECT reports_enabled FROM chat_settings WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    if not row or row[0] != 1:
        return await update.message.reply_text("Report sistemi kapalı.")
    target = update.message.reply_to_message.from_user
    actor = update.effective_user
    reason = " ".join(context.args) if context.args else "Sebep belirtilmedi."
    try:
        admins = await context.bot.get_chat_administrators(cid)
        mentions = []
        for adm in admins:
            if adm.user.is_bot:
                continue
            if adm.user.username:
                mentions.append(f"@{adm.user.username}")
        mention_text = " ".join(mentions[:5]) if mentions else "Adminler bilgilendirildi."
        msg = await update.message.reply_text(
            f"🚨 <b>Report</b>\n"
            f"• Bildiren: {actor.mention_html()}\n"
            f"• Hedef: {target.mention_html()}\n"
            f"• Sebep: {html.escape(reason)}\n\n"
            f"{mention_text}",
            parse_mode="HTML"
        )
        context.application.create_task(delete_later(msg, 10))
        await send_log(
            context, cid,
            f"<b>Eylem:</b> REPORT\n"
            f"<b>Bildiren:</b> {html.escape(actor.full_name)} (<code>{actor.id}</code>)\n"
            f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
            f"<b>Sebep:</b> {html.escape(reason)}"
        )
    except Exception:
        await update.message.reply_text("Report gönderilemedi.")


async def mod_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    if not await require_admin(update, context):
        return

    cid = update.effective_chat.id
    uid = update.effective_user.id
    role = await role_of(cid, uid, context)
    actor_member = await get_member_status(cid, uid, context)

    if role not in ("owner", "sudo", "admin"):
        return await update.message.reply_text("Bu işlem için yetkin yok.")

    if role == "admin":
        if not actor_member or (actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False)):
            return await update.message.reply_text("Bu işlem için Telegram admin yetkin yok.")

    target, reason = await extract_target_user_and_reason(update, context)
    if not target:
        if action in ("tban", "tmute"):
            return await update.message.reply_text(f"Kullanım: /{action} <reply/id> <10m/1h/1d> [sebep]")
        return await update.message.reply_text(f"Kullanım: /{action} <reply/id> [sebep]")

    allowed, err = await can_act_on_target(cid, uid, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)

    duration_secs = None

    if action in ("tban", "tmute"):
        args = list(context.args) if context.args else []
        if update.message.reply_to_message:
            if not args:
                return await update.message.reply_text(f"Kullanım: /{action} <10m/1h/1d> [sebep]")
            duration_secs = parse_time(args[0])
            if not duration_secs:
                return await update.message.reply_text("Geçersiz süre. Örnek: 10m, 1h, 1d")
            reason = " ".join(args[1:]).strip() if len(args) > 1 else "Sebep belirtilmedi."
        else:
            if len(args) < 2:
                return await update.message.reply_text(f"Kullanım: /{action} <id> <10m/1h/1d> [sebep]")
            duration_secs = parse_time(args[1])
            if not duration_secs:
                return await update.message.reply_text("Geçersiz süre. Örnek: 10m, 1h, 1d")
            reason = " ".join(args[2:]).strip() if len(args) > 2 else "Sebep belirtilmedi."

    until_date = datetime.datetime.now() + datetime.timedelta(seconds=duration_secs) if duration_secs else None

    try:
        if action in ("ban", "tban"):
            if not await bot_can_restrict(cid, context):
                return await update.message.reply_text("Botun ban yetkisi yok.")
            await context.bot.ban_chat_member(cid, target.id, until_date=until_date)
            act_text = "banlandı" if action == "ban" else "süreli olarak banlandı"
            inc_mod_stat(cid, "total_bans")
            add_punish_history(cid, target.id, action.upper(), reason, uid)

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
            act_text = "susturuldu" if action == "mute" else "süreli olarak susturuldu"
            inc_mod_stat(cid, "total_mutes")
            add_punish_history(cid, target.id, action.upper(), reason, uid)

        elif action == "kick":
            if not await bot_can_restrict(cid, context):
                return await update.message.reply_text("Botun atma yetkisi yok.")
            await context.bot.ban_chat_member(cid, target.id)
            await context.bot.unban_chat_member(cid, target.id)
            act_text = "gruptan atıldı"
            add_punish_history(cid, target.id, "KICK", reason, uid)
        else:
            return

        msg = await update.message.reply_text(
            f"🔨 <b>{html.escape(target.full_name)}</b> {act_text}.\n"
            f"👮 <b>Yetkili:</b> {html.escape(update.effective_user.full_name)}\n"
            f"📝 <b>Sebep:</b> {html.escape(reason)}",
            parse_mode="HTML"
        )
        context.application.create_task(delete_later(msg, 5))

        await send_log(
            context,
            cid,
            f"<b>Eylem:</b> {action.upper()}\n"
            f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
            f"<b>Yetkili:</b> {html.escape(update.effective_user.full_name)}\n"
            f"<b>Sebep:</b> {html.escape(reason)}"
        )
        await silent_delete_command_message(update, context)
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


async def sban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    actor = update.effective_user
    target, reason = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /sban <reply/id> [sebep]")
    allowed, err = await can_act_on_target(cid, actor.id, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)
    if not await bot_can_restrict(cid, context):
        return await update.message.reply_text("Botun ban yetkisi yok.")
    try:
        await context.bot.ban_chat_member(cid, target.id)
        inc_mod_stat(cid, "total_bans")
        add_punish_history(cid, target.id, "SBAN", reason, actor.id)
        await silent_delete_command_message(update, context)
        await send_log(
            context, cid,
            f"<b>Eylem:</b> SBAN\n"
            f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
            f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
            f"<b>Sebep:</b> {html.escape(reason)}"
        )
    except Exception:
        await update.message.reply_text("Silent ban başarısız oldu.")


async def smute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    actor = update.effective_user
    target, reason = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /smute <reply/id> [sebep]")
    allowed, err = await can_act_on_target(cid, actor.id, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)
    if not await bot_can_restrict(cid, context):
        return await update.message.reply_text("Botun mute yetkisi yok.")
    try:
        await context.bot.restrict_chat_member(
            cid,
            target.id,
            ChatPermissions(
                can_send_messages=False,
                can_send_polls=False,
                can_add_web_page_previews=False,
                can_invite_users=False
            )
        )
        inc_mod_stat(cid, "total_mutes")
        add_punish_history(cid, target.id, "SMUTE", reason, actor.id)
        await silent_delete_command_message(update, context)
        await send_log(
            context, cid,
            f"<b>Eylem:</b> SMUTE\n"
            f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
            f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
            f"<b>Sebep:</b> {html.escape(reason)}"
        )
    except Exception:
        await update.message.reply_text("Silent mute başarısız oldu.")


async def stmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    actor = update.effective_user
    target, duration, reason = await extract_target_time_reason(update, context)
    if not target or not duration:
        return await update.message.reply_text("Kullanım: /stmute <reply/id> <10m/1h/1d> [sebep]")
    allowed, err = await can_act_on_target(cid, actor.id, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)
    if not await bot_can_restrict(cid, context):
        return await update.message.reply_text("Botun mute yetkisi yok.")
    try:
        until_date = datetime.datetime.now() + datetime.timedelta(seconds=duration)
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
        inc_mod_stat(cid, "total_mutes")
        add_punish_history(cid, target.id, "STMUTE", reason, actor.id)
        await silent_delete_command_message(update, context)
        await send_log(
            context, cid,
            f"<b>Eylem:</b> STMUTE\n"
            f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
            f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
            f"<b>Sebep:</b> {html.escape(reason)}"
        )
    except Exception:
        await update.message.reply_text("Silent temp mute başarısız oldu.")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    uid = update.effective_user.id
    role = await role_of(cid, uid, context)
    actor_member = await get_member_status(cid, uid, context)

    if role == "admin":
        if not actor_member or (actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False)):
            return await update.message.reply_text("Ban kaldırmak için Telegram yetkin yok.")
    elif role not in ("owner", "sudo", "admin"):
        return await update.message.reply_text("Ban kaldırmak için yetkin yok.")

    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /unban <reply/id>")

    try:
        await context.bot.unban_chat_member(cid, target.id)
        add_punish_history(cid, target.id, "UNBAN", "Manual unban", uid)
        msg = await update.message.reply_text(f"✅ {target.full_name} için ban kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, cid, f"<b>Eylem:</b> UNBAN\n<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)")
        await silent_delete_command_message(update, context)
    except Exception:
        await update.message.reply_text("Ban kaldırılamadı.")


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    uid = update.effective_user.id
    role = await role_of(cid, uid, context)
    actor_member = await get_member_status(cid, uid, context)

    if role == "admin":
        if not actor_member or (actor_member.status != ChatMemberStatus.OWNER and not getattr(actor_member, "can_restrict_members", False)):
            return await update.message.reply_text("Susturma kaldırmak için Telegram yetkin yok.")
    elif role not in ("owner", "sudo", "admin"):
        return await update.message.reply_text("Susturma kaldırmak için yetkin yok.")

    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /unmute <reply/id>")

    try:
        await context.bot.restrict_chat_member(cid, target.id, full_unmute_permissions())
        add_punish_history(cid, target.id, "UNMUTE", "Manual unmute", uid)
        msg = await update.message.reply_text(f"🔊 {target.full_name} için susturma kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
        await send_log(context, cid, f"<b>Eylem:</b> UNMUTE\n<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)")
        await silent_delete_command_message(update, context)
    except Exception:
        await update.message.reply_text("Susturma kaldırılamadı.")


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    uid = update.effective_user.id
    role = await role_of(cid, uid, context)
    actor_member = await get_member_status(cid, uid, context)

    if role not in ("owner", "sudo"):
        return await update.message.reply_text("Admin vermek için yetkin yok.")

    if role == "sudo":
        if not actor_member or not getattr(actor_member, "can_promote_members", False):
            return await update.message.reply_text("Sudo olsan da bu grupta promote yetkin yok.")

    if not await bot_can_promote(cid, context):
        return await update.message.reply_text("Botun admin verme yetkisi yok.")

    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip veya id yazarak /promote kullan.")

    if target.id == uid:
        return await update.message.reply_text("Kendine admin veremezsin.")

    target_role = await role_of(cid, target.id, context)
    if target_role in ("owner", "sudo", "admin"):
        return await update.message.reply_text("Bu kullanıcı zaten yetkili.")

    try:
        await context.bot.promote_chat_member(
            chat_id=cid,
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
        add_punish_history(cid, target.id, "PROMOTE", "Admin verildi", uid)
        await send_log(context, cid, f"<b>Eylem:</b> PROMOTE\n<b>Hedef:</b> {html.escape(target.full_name)}")
        await silent_delete_command_message(update, context)
    except Exception:
        await update.message.reply_text("Kullanıcı admin yapılamadı.")


async def unadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    uid = update.effective_user.id
    role = await role_of(cid, uid, context)
    actor_member = await get_member_status(cid, uid, context)

    if role not in ("owner", "sudo"):
        return await update.message.reply_text("Admin almak için yetkin yok.")

    if role == "sudo":
        if not actor_member or not getattr(actor_member, "can_promote_members", False):
            return await update.message.reply_text("Sudo olsan da bu grupta promote yetkin yok.")

    if not await bot_can_promote(cid, context):
        return await update.message.reply_text("Botun admin alma yetkisi yok.")

    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip veya id yazarak /demote kullan.")

    target_member = await get_member_status(cid, target.id, context)
    target_role = await role_of(cid, target.id, context)

    if target_role == "owner":
        return await update.message.reply_text("Owner'dan adminlik alınamaz.")
    if target_role == "sudo":
        return await update.message.reply_text("Sudo kullanıcıyı adminlikten alamazsın, önce sudoyu kaldırmalısın.")
    if not target_member:
        return await update.message.reply_text("Hedef kullanıcı bulunamadı.")

    try:
        await context.bot.promote_chat_member(
            chat_id=cid,
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
        add_punish_history(cid, target.id, "DEMOTE", "Adminlik alındı", uid)
        await send_log(context, cid, f"<b>Eylem:</b> DEMOTE\n<b>Hedef:</b> {html.escape(target.full_name)}")
        await silent_delete_command_message(update, context)
    except Exception:
        await update.message.reply_text("Kullanıcının adminliği alınamadı.")


async def promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_cmd(update, context)


async def demote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await unadmin_cmd(update, context)


async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    actor = update.effective_user
    target, reason = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /warn <reply/id> [sebep]")
    allowed, err = await can_act_on_target(cid, actor.id, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)
    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    current_warns = (row[0] if row else 0) + 1
    warn_limit = get_warn_limit(cid)
    cursor.execute("INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)", (cid, target.id, current_warns))
    conn.commit()
    inc_mod_stat(cid, "total_warns")
    add_punish_history(cid, target.id, "WARN", reason, actor.id)

    msg = await update.message.reply_text(
        f"⚠️ <b>{html.escape(target.full_name)}</b> warn aldı. ({current_warns}/{warn_limit})\n"
        f"📝 <b>Sebep:</b> {html.escape(reason)}",
        parse_mode="HTML"
    )
    context.application.create_task(delete_later(msg, 5))

    await send_log(
        context, cid,
        f"<b>Eylem:</b> WARN\n<b>Hedef:</b> {html.escape(target.full_name)}\n"
        f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
        f"<b>Sebep:</b> {html.escape(reason)}\n<b>Warn:</b> {current_warns}/{warn_limit}"
    )
    await silent_delete_command_message(update, context)

    if current_warns >= warn_limit:
        await execute_warn_limit_action(cid, target, actor, reason, context)


async def swarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    actor = update.effective_user
    target, reason = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /swarn <reply/id> [sebep]")
    allowed, err = await can_act_on_target(cid, actor.id, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)
    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    current_warns = (row[0] if row else 0) + 1
    warn_limit = get_warn_limit(cid)

    cursor.execute(
        "INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)",
        (cid, target.id, current_warns)
    )
    conn.commit()

    inc_mod_stat(cid, "total_warns")
    add_punish_history(cid, target.id, "SWARN", reason, actor.id)

    await silent_delete_command_message(update, context)

    await send_log(
        context, cid,
        f"<b>Eylem:</b> SWARN\n"
        f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
        f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
        f"<b>Sebep:</b> {html.escape(reason)}\n"
        f"<b>Warn:</b> {current_warns}/{warn_limit}"
    )

    if current_warns >= warn_limit:
        await execute_warn_limit_action(cid, target, actor, reason, context)


async def dwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return await update.message.reply_text("Bir kullanıcı mesajına yanıt verip /dwarn kullan.")

    cid = update.effective_chat.id
    actor = update.effective_user
    target = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "Sebep belirtilmedi."

    allowed, err = await can_act_on_target(cid, actor.id, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)

    if await bot_can_delete(cid, context):
        try:
            await update.message.reply_to_message.delete()
            inc_mod_stat(cid, "total_deleted")
            inc_user_deleted(cid, target.id)
        except Exception:
            pass

    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    current_warns = (row[0] if row else 0) + 1
    warn_limit = get_warn_limit(cid)

    cursor.execute(
        "INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)",
        (cid, target.id, current_warns)
    )
    conn.commit()

    inc_mod_stat(cid, "total_warns")
    add_punish_history(cid, target.id, "DWARN", reason, actor.id)

    msg = await update.message.reply_text(
        f"⚠️ {html.escape(target.full_name)} silinmiş mesaj üzerinden warn aldı. ({current_warns}/{warn_limit})",
        parse_mode="HTML"
    )
    context.application.create_task(delete_later(msg, 5))
    await silent_delete_command_message(update, context)

    await send_log(
        context, cid,
        f"<b>Eylem:</b> DWARN\n"
        f"<b>Hedef:</b> {html.escape(target.full_name)} (<code>{target.id}</code>)\n"
        f"<b>Yetkili:</b> {html.escape(actor.full_name)}\n"
        f"<b>Sebep:</b> {html.escape(reason)}\n"
        f"<b>Warn:</b> {current_warns}/{warn_limit}"
    )

    if current_warns >= warn_limit:
        await execute_warn_limit_action(cid, target, actor, reason, context)


async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip veya id yazarak /warns kullan.")
    cid = update.effective_chat.id
    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    count = row[0] if row else 0
    warn_limit = get_warn_limit(cid)
    await update.message.reply_text(f"📌 {target.full_name} warn sayısı: {count}/{warn_limit}")


async def clearwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip veya id yazarak /clearwarns kullan.")
    cid = update.effective_chat.id
    cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    conn.commit()
    msg = await update.message.reply_text(f"🧽 {target.full_name} warnları sıfırlandı.")
    context.application.create_task(delete_later(msg, 5))
    await silent_delete_command_message(update, context)


async def resetwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /resetwarns <reply/id>")
    cid = update.effective_chat.id
    cursor.execute("DELETE FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    conn.commit()
    msg = await update.message.reply_text(f"✅ {html.escape(target.full_name)} warn kayıtları sıfırlandı.", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))
    await silent_delete_command_message(update, context)


async def delwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /delwarn <reply/id>")
    cid = update.effective_chat.id
    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    current = row[0] if row else 0
    new_count = max(current - 1, 0)
    cursor.execute("INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)", (cid, target.id, new_count))
    conn.commit()
    await update.message.reply_text(f"✅ {target.full_name} için 1 warn silindi. Yeni sayı: {new_count}")
    await silent_delete_command_message(update, context)


async def strikes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip veya id yazarak /strikes kullan.")
    count = get_strike_count(update.effective_chat.id, target.id)
    await update.message.reply_text(f"🎯 {target.full_name} strike sayısı: {count}")


async def clearstrikes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip veya id yazarak /clearstrikes kullan.")
    clear_strikes(update.effective_chat.id, target.id)
    await update.message.reply_text(f"✅ {target.full_name} strike kayıtları temizlendi.")
    await silent_delete_command_message(update, context)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "history_view"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip veya id yazarak /history kullan.")
    cid = update.effective_chat.id
    cursor.execute("""
    SELECT action, reason, ts FROM punish_history
    WHERE chat_id = ? AND user_id = ?
    ORDER BY id DESC LIMIT 10
    """, (cid, target.id))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Ceza geçmişi yok.")
    lines = []
    for action, reason, ts in rows:
        dt = datetime.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
        lines.append(f"• {dt} - {action} - {reason}")
    text = "📜 <b>Ceza Geçmişi</b>\n\n" + "\n".join(html.escape(x) for x in lines)
    await update.message.reply_text(text, parse_mode="HTML")


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /approve <reply/id>")
    cid = update.effective_chat.id
    cursor.execute("INSERT OR IGNORE INTO approvals (chat_id, user_id) VALUES (?, ?)", (cid, target.id))
    conn.commit()
    await update.message.reply_text(f"✅ {target.full_name} approved olarak eklendi.")
    await silent_delete_command_message(update, context)


async def unapprove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /unapprove <reply/id>")
    cid = update.effective_chat.id
    cursor.execute("DELETE FROM approvals WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    conn.commit()
    await update.message.reply_text(f"✅ {target.full_name} approval listesinden çıkarıldı.")
    await silent_delete_command_message(update, context)


async def approved_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    cid = update.effective_chat.id
    cursor.execute("SELECT user_id FROM approvals WHERE chat_id = ? ORDER BY user_id", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Approved listesi boş.")
    text = "✅ <b>Approved Kullanıcılar</b>\n\n" + "\n".join(f"• <code>{r[0]}</code>" for r in rows)
    await update.message.reply_text(text, parse_mode="HTML")


async def addsudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "sudo_manage"):
        return await update.message.reply_text("Sadece owner sudo ekleyebilir.")
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip veya id yazarak /addsudo kullan.")
    cursor.execute("INSERT OR IGNORE INTO sudo_users (user_id) VALUES (?)", (target.id,))
    conn.commit()
    await update.message.reply_text(f"✅ {target.full_name} sudo olarak eklendi.")


async def delsudo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "sudo_manage"):
        return await update.message.reply_text("Sadece owner sudo silebilir.")
    target, _ = await extract_target_user_and_reason(update, context)
    if not target:
        return await update.message.reply_text("Bir kullanıcıya yanıt verip veya id yazarak /delsudo kullan.")
    cursor.execute("DELETE FROM sudo_users WHERE user_id = ?", (target.id,))
    conn.commit()
    await update.message.reply_text(f"✅ {target.full_name} sudo listesinden çıkarıldı.")


async def sudolist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "sudo_manage"):
        return await update.message.reply_text("Sadece owner sudo listesini görebilir.")
    cursor.execute("SELECT user_id FROM sudo_users ORDER BY user_id")
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Sudo listesi boş.")
    text = "👑 <b>Sudo Listesi</b>\n\n" + "\n".join(f"• <code>{r[0]}</code>" for r in rows)
    await update.message.reply_text(text, parse_mode="HTML")


async def toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, label: str):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu ayar için yetkin yok.")
    if not context.args or context.args[0].lower() not in ("on", "off"):
        return await update.message.reply_text(f"Kullanım: /{field} on veya /{field} off")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    val = 1 if context.args[0].lower() == "on" else 0
    cursor.execute(f"UPDATE chat_settings SET {field} = ? WHERE chat_id = ?", (val, cid))
    conn.commit()
    msg = await update.message.reply_text(f"⚙️ {label} {'açıldı ✅' if val else 'kapatıldı ❌'}.")
    context.application.create_task(delete_later(msg, 5))
    await silent_delete_command_message(update, context)


async def antilink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "antilink", "Antilink")


async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "welcome", "Karşılama")


async def goodbye_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "goodbye", "Çıkış mesajı")


async def antispam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "antispam", "Anti-spam")


async def raid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "raid_mode", "Raid modu")


async def cleancommands_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "clean_commands", "Komut temizleme")


async def cleanservice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "clean_service", "Servis mesaj temizleme")


async def reports_toggle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await toggle_setting(update, context, "reports_enabled", "Report sistemi")


async def raidmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("SELECT raid_mode, antispam FROM chat_settings WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    await update.message.reply_text(
        f"🛡️ Raid Mode: {'Açık' if row and row[0] else 'Kapalı'}\n"
        f"⚡ Anti-Spam: {'Açık' if row and row[1] else 'Kapalı'}"
    )


async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /setwelcome <mesaj>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET welcome_text = ? WHERE chat_id = ?", (" ".join(context.args), cid))
    conn.commit()
    msg = await update.message.reply_text("✅ Karşılama mesajı güncellendi.")
    context.application.create_task(delete_later(msg, 5))
    await silent_delete_command_message(update, context)


async def setgoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /setgoodbye <mesaj>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET goodbye_text = ? WHERE chat_id = ?", (" ".join(context.args), cid))
    conn.commit()
    msg = await update.message.reply_text("✅ Çıkış mesajı güncellendi.")
    context.application.create_task(delete_later(msg, 5))
    await silent_delete_command_message(update, context)


async def setlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "setlog"):
        return await update.message.reply_text("Log ayarı için yetkin yok.")

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
    await silent_delete_command_message(update, context)


async def logoff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "setlog"):
        return await update.message.reply_text("Log ayarı için yetkin yok.")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET log_chat_id = 0 WHERE chat_id = ?", (cid,))
    conn.commit()
    await update.message.reply_text("✅ Log kapatıldı.")
    await silent_delete_command_message(update, context)


async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /setrules <kurallar>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET rules_text = ? WHERE chat_id = ?", (" ".join(context.args), cid))
    conn.commit()
    msg = await update.message.reply_text("✅ Grup kuralları güncellendi.")
    context.application.create_task(delete_later(msg, 5))
    await silent_delete_command_message(update, context)


async def setwarnlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Kullanım: /setwarnlimit <sayı>")
    limit_val = int(context.args[0])
    if limit_val < 1 or limit_val > 20:
        return await update.message.reply_text("Warn limiti 1 ile 20 arasında olmalı.")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET warn_limit = ? WHERE chat_id = ?", (limit_val, cid))
    conn.commit()
    await update.message.reply_text(f"✅ Warn limiti {limit_val} olarak ayarlandı.")
    await silent_delete_command_message(update, context)


async def warnmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args or context.args[0].lower() not in ("ban", "mute", "kick"):
        return await update.message.reply_text("Kullanım: /warnmode <ban|mute|kick>")
    mode = context.args[0].lower()
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("UPDATE chat_settings SET warn_mode = ? WHERE chat_id = ?", (mode, cid))
    conn.commit()
    await update.message.reply_text(f"✅ Warn limiti aşılınca uygulanacak işlem: {mode}")
    await silent_delete_command_message(update, context)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    cursor.execute("""
    SELECT antilink, welcome, goodbye, welcome_text, goodbye_text, log_chat_id, rules_text,
           lock_link, lock_badword, lock_flood, antispam, raid_mode, warn_limit, warn_mode,
           lock_sticker, lock_media, clean_commands, reports_enabled, clean_service,
           lock_forward, lock_bots, lock_photo, lock_video, lock_document, lock_voice
    FROM chat_settings WHERE chat_id = ?
    """, (cid,))
    row = cursor.fetchone()
    text = (
        "⚙️ <b>Grup Ayarları</b>\n\n"
        f"• Antilink: {'Açık' if row[0] else 'Kapalı'}\n"
        f"• Welcome: {'Açık' if row[1] else 'Kapalı'}\n"
        f"• Goodbye: {'Açık' if row[2] else 'Kapalı'}\n"
        f"• Anti-Spam: {'Açık' if row[10] else 'Kapalı'}\n"
        f"• Raid Mode: {'Açık' if row[11] else 'Kapalı'}\n"
        f"• Warn Limit: <code>{row[12]}</code>\n"
        f"• Warn Mode: <code>{html.escape(row[13])}</code>\n"
        f"• Reports: {'Açık' if row[17] else 'Kapalı'}\n"
        f"• Clean Commands: {'Açık' if row[16] else 'Kapalı'}\n"
        f"• Clean Service: {'Açık' if row[18] else 'Kapalı'}\n"
        f"• Log Chat ID: <code>{row[5]}</code>\n"
        f"• Lock Link: {'Açık' if row[7] else 'Kapalı'}\n"
        f"• Lock Badword: {'Açık' if row[8] else 'Kapalı'}\n"
        f"• Lock Flood: {'Açık' if row[9] else 'Kapalı'}\n"
        f"• Lock Sticker: {'Açık' if row[14] else 'Kapalı'}\n"
        f"• Lock Media: {'Açık' if row[15] else 'Kapalı'}\n"
        f"• Lock Forward: {'Açık' if row[19] else 'Kapalı'}\n"
        f"• Lock Bots: {'Açık' if row[20] else 'Kapalı'}\n"
        f"• Lock Photo: {'Açık' if row[21] else 'Kapalı'}\n"
        f"• Lock Video: {'Açık' if row[22] else 'Kapalı'}\n"
        f"• Lock Document: {'Açık' if row[23] else 'Kapalı'}\n"
        f"• Lock Voice: {'Açık' if row[24] else 'Kapalı'}\n"
        f"• Welcome Mesajı: {html.escape(row[3])[:80]}\n"
        f"• Goodbye Mesajı: {html.escape(row[4])[:80]}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /lock <link|badword|flood|sticker|media|forward|bot|photo|video|document|voice>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    arg = context.args[0].lower()
    field_map = {
        "link": "lock_link",
        "badword": "lock_badword",
        "flood": "lock_flood",
        "sticker": "lock_sticker",
        "media": "lock_media",
        "forward": "lock_forward",
        "bot": "lock_bots",
        "bots": "lock_bots",
        "photo": "lock_photo",
        "video": "lock_video",
        "document": "lock_document",
        "voice": "lock_voice",
    }
    field = field_map.get(arg)
    if not field:
        return await update.message.reply_text("Sadece: link, badword, flood, sticker, media, forward, bot, photo, video, document, voice")
    cursor.execute(f"UPDATE chat_settings SET {field} = 1 WHERE chat_id = ?", (cid,))
    conn.commit()
    await update.message.reply_text(f"🔒 {arg} kilitlendi.")
    await silent_delete_command_message(update, context)


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /unlock <link|badword|flood|sticker|media|forward|bot|photo|video|document|voice>")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)
    arg = context.args[0].lower()
    field_map = {
        "link": "lock_link",
        "badword": "lock_badword",
        "flood": "lock_flood",
        "sticker": "lock_sticker",
        "media": "lock_media",
        "forward": "lock_forward",
        "bot": "lock_bots",
        "bots": "lock_bots",
        "photo": "lock_photo",
        "video": "lock_video",
        "document": "lock_document",
        "voice": "lock_voice",
    }
    field = field_map.get(arg)
    if not field:
        return await update.message.reply_text("Sadece: link, badword, flood, sticker, media, forward, bot, photo, video, document, voice")
    cursor.execute(f"UPDATE chat_settings SET {field} = 0 WHERE chat_id = ?", (cid,))
    conn.commit()
    await update.message.reply_text(f"🔓 {arg} kilidi kaldırıldı.")
    await silent_delete_command_message(update, context)


async def locks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    cid = update.effective_chat.id
    ensure_chat_settings(cid)

    cursor.execute("""
    SELECT lock_link, lock_badword, lock_flood, lock_sticker, lock_media,
           lock_forward, lock_bots, lock_photo, lock_video, lock_document, lock_voice
    FROM chat_settings WHERE chat_id = ?
    """, (cid,))
    row = cursor.fetchone()

    text = (
        "🔐 <b>Aktif Lock Ayarları</b>\n\n"
        f"• Link: {'Açık' if row and row[0] else 'Kapalı'}\n"
        f"• Badword: {'Açık' if row and row[1] else 'Kapalı'}\n"
        f"• Flood: {'Açık' if row and row[2] else 'Kapalı'}\n"
        f"• Sticker: {'Açık' if row and row[3] else 'Kapalı'}\n"
        f"• Media: {'Açık' if row and row[4] else 'Kapalı'}\n"
        f"• Forward: {'Açık' if row and row[5] else 'Kapalı'}\n"
        f"• Bots: {'Açık' if row and row[6] else 'Kapalı'}\n"
        f"• Photo: {'Açık' if row and row[7] else 'Kapalı'}\n"
        f"• Video: {'Açık' if row and row[8] else 'Kapalı'}\n"
        f"• Document: {'Açık' if row and row[9] else 'Kapalı'}\n"
        f"• Voice: {'Açık' if row and row[10] else 'Kapalı'}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def addbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /addbad <kelime>")
    word = normalize_badword_word(context.args[0].lower().strip())
    cid = update.effective_chat.id
    cursor.execute("INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?, ?)", (cid, word))
    conn.commit()
    msg = await update.message.reply_text(f"✅ Yasaklı kelime eklendi: <code>{html.escape(word)}</code>", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))
    await silent_delete_command_message(update, context)


async def delbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /delbad <kelime>")
    word = normalize_badword_word(context.args[0].lower().strip())
    cid = update.effective_chat.id
    cursor.execute("DELETE FROM badwords WHERE chat_id = ? AND word = ?", (cid, word))
    conn.commit()
    msg = await update.message.reply_text(f"🗑️ Silindi: <code>{html.escape(word)}</code>", parse_mode="HTML")
    context.application.create_task(delete_later(msg, 5))
    await silent_delete_command_message(update, context)


async def badlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    cid = update.effective_chat.id
    cursor.execute("SELECT word FROM badwords WHERE chat_id = ? ORDER BY word ASC", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Liste boş.")
    text = "🧱 <b>Yasaklı Kelimeler</b>\n\n" + "\n".join(f"• <code>{html.escape(r[0])}</code>" for r in rows)
    for p in split_text(text):
        await update.message.reply_text(p, parse_mode="HTML")


async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /blacklist <tetik>")
    trigger = " ".join(context.args).strip().lower()
    if len(trigger) > MAX_BLACKLIST_TRIGGER:
        return await update.message.reply_text(f"Blacklist en fazla {MAX_BLACKLIST_TRIGGER} karakter olabilir.")
    cid = update.effective_chat.id
    cursor.execute("INSERT OR IGNORE INTO blacklists (chat_id, trigger_text) VALUES (?, ?)", (cid, trigger))
    conn.commit()
    await update.message.reply_text(f"✅ Blacklist eklendi: <code>{html.escape(trigger)}</code>", parse_mode="HTML")
    await silent_delete_command_message(update, context)


async def rmblacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await has_perm(update.effective_chat.id, update.effective_user.id, context, "settings_basic"):
        return await update.message.reply_text("Bu komut için yetkin yok.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /rmblacklist <tetik>")
    trigger = " ".join(context.args).strip().lower()
    cid = update.effective_chat.id
    cursor.execute("DELETE FROM blacklists WHERE chat_id = ? AND trigger_text = ?", (cid, trigger))
    conn.commit()
    await update.message.reply_text(f"🗑️ Blacklist silindi: <code>{html.escape(trigger)}</code>", parse_mode="HTML")
    await silent_delete_command_message(update, context)


async def blacklists_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    cid = update.effective_chat.id
    cursor.execute("SELECT trigger_text FROM blacklists WHERE chat_id = ? ORDER BY trigger_text", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Blacklist listesi boş.")
    text = "⛔ <b>Blacklist Tetikleri</b>\n\n" + "\n".join(f"• <code>{html.escape(r[0])}</code>" for r in rows)
    for p in split_text(text):
        await update.message.reply_text(p, parse_mode="HTML")


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
        await silent_delete_command_message(update, context)
    except Exception:
        await update.message.reply_text("Mesaj sabitlenemedi.")


async def unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Botun sabit kaldırma yetkisi yok.")
    try:
        await context.bot.unpin_chat_message(update.effective_chat.id)
        msg = await update.message.reply_text("📍 Sabit mesaj kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
        await silent_delete_command_message(update, context)
    except Exception:
        await update.message.reply_text("Sabit mesaj kaldırılamadı.")


async def unpinall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Botun sabit kaldırma yetkisi yok.")
    try:
        await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        msg = await update.message.reply_text("📍 Tüm sabit mesajlar kaldırıldı.")
        context.application.create_task(delete_later(msg, 5))
        await silent_delete_command_message(update, context)
    except Exception:
        await update.message.reply_text("Tüm sabit mesajlar kaldırılamadı.")


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /del kullan.")
    if not await bot_can_delete(update.effective_chat.id, context):
        return await update.message.reply_text("Botun mesaj silme yetkisi yok.")

    try:
        await update.message.reply_to_message.delete()
        try:
            await update.message.delete()
        except Exception:
            pass
    except Exception:
        await update.message.reply_text("Mesaj silinemedi.")


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


async def zombies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    await update.message.reply_text(
        "🧟 Telegram Bot API ile tam üye taraması sınırlı olduğu için klasik zombie temizliği burada kısıtlı.\n"
        "Bu komut bilgi amaçlıdır. Gelişmiş üye cache sistemi olmadan tam zombie temizliği yapılamaz."
    )


async def settitle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /settitle <yeni başlık>")
    if not await bot_can_change_info(update.effective_chat.id, context):
        return await update.message.reply_text("Botun grup bilgilerini değiştirme yetkisi yok.")
    try:
        title = " ".join(context.args).strip()[:128]
        await context.bot.set_chat_title(update.effective_chat.id, title)
        await update.message.reply_text("✅ Grup başlığı güncellendi.")
        await silent_delete_command_message(update, context)
    except Exception:
        await update.message.reply_text("Grup başlığı değiştirilemedi.")


async def setdesc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /setdesc <açıklama>")
    if not await bot_can_change_info(update.effective_chat.id, context):
        return await update.message.reply_text("Botun grup bilgilerini değiştirme yetkisi yok.")
    try:
        desc = " ".join(context.args).strip()[:255]
        await context.bot.set_chat_description(update.effective_chat.id, desc)
        await update.message.reply_text("✅ Grup açıklaması güncellendi.")
        await silent_delete_command_message(update, context)
    except Exception:
        await update.message.reply_text("Grup açıklaması değiştirilemedi.")


async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
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
    await silent_delete_command_message(update, context)


async def get_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
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
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /clear <isim>")
    cid = update.effective_chat.id
    name = context.args[0].lower()
    cursor.execute("DELETE FROM notes WHERE chat_id = ? AND note_name = ?", (cid, name))
    conn.commit()
    await update.message.reply_text(f"🗑️ Not silindi: #{name}")
    await silent_delete_command_message(update, context)


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
    if not await require_admin(update, context):
        return
    if len(context.args) < 2:
        return await update.message.reply_text("Kullanım: /filter <tetik> <cevap>")
    cid = update.effective_chat.id
    trigger = context.args[0].lower()
    reply = " ".join(context.args[1:])
    if len(trigger) > MAX_FILTER_TRIGGER:
        return await update.message.reply_text(f"Tetik en fazla {MAX_FILTER_TRIGGER} karakter olabilir.")
    if len(reply) > MAX_FILTER_REPLY:
        return await update.message.reply_text(f"Cevap en fazla {MAX_FILTER_REPLY} karakter olabilir.")
    cursor.execute(
        "INSERT OR REPLACE INTO filters_table (chat_id, trigger_text, reply_text) VALUES (?, ?, ?)",
        (cid, trigger, reply)
    )
    conn.commit()
    await update.message.reply_text(f"✅ Filter eklendi: {trigger}")
    await silent_delete_command_message(update, context)


async def stop_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /stop <tetik>")
    cid = update.effective_chat.id
    trigger = context.args[0].lower()
    cursor.execute("DELETE FROM filters_table WHERE chat_id = ? AND trigger_text = ?", (cid, trigger))
    conn.commit()
    await update.message.reply_text(f"🗑️ Filter silindi: {trigger}")
    await silent_delete_command_message(update, context)


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
        "mute": mute, "tmute": tmute, "smute": smute, "stmute": stmute, "unmute": unmute,
        "sban": sban,
        "warn": warn, "swarn": swarn, "dwarn": dwarn, "warns": warns_cmd,
        "clearwarns": clearwarns, "resetwarns": resetwarns, "delwarn": delwarn_cmd,
        "strikes": strikes_cmd, "clearstrikes": clearstrikes_cmd, "history": history_cmd,
        "approve": approve_cmd, "unapprove": unapprove_cmd, "approved": approved_cmd,
        "admin": admin_cmd, "unadmin": unadmin_cmd, "promote": promote, "demote": demote,
        "addsudo": addsudo_cmd, "delsudo": delsudo_cmd, "sudolist": sudolist_cmd,
        "antilink": antilink_cmd, "welcome": welcome_cmd, "goodbye": goodbye_cmd,
        "antispam": antispam_cmd, "raid": raid_cmd, "raidmode": raidmode_cmd,
        "reports": reports_toggle_cmd,
        "setwelcome": setwelcome, "setgoodbye": setgoodbye,
        "setlog": setlog, "logoff": logoff_cmd, "setrules": setrules, "settings": settings_cmd,
        "setwarnlimit": setwarnlimit, "warnmode": warnmode_cmd, "cleancommands": cleancommands_cmd, "cleanservice": cleanservice_cmd,
        "addbad": addbad, "delbad": delbad, "badlist": badlist,
        "blacklist": blacklist_cmd, "rmblacklist": rmblacklist_cmd, "blacklists": blacklists_cmd,
        "pin": pin, "unpin": unpin, "unpinall": unpinall, "purge": purge, "del": del_cmd,
        "ping": ping, "id": id_cmd, "userinfo": userinfo,
        "stats": stats_cmd, "ara": ara, "rules": rules,
        "report": report_cmd, "admins": admins_cmd, "invitelink": invitelink_cmd, "zombies": zombies_cmd,
        "settitle": settitle_cmd, "setdesc": setdesc_cmd,
        "save": save_note, "get": get_note, "clear": clear_note, "notes": notes_cmd,
        "filter": add_filter, "stop": stop_filter, "filters": filters_cmd,
        "lock": lock_cmd, "unlock": unlock_cmd, "locks": locks_cmd,
    }

    func = mapping.get(cmd)
    if func:
        await func(update, context)
    else:
        msg = await context.bot.send_message(update.effective_chat.id, "Bilinmeyen noktalı komut.")
        context.application.create_task(delete_later(msg, 5))


async def handle_new_members_raid(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    now = time.time()
    join_tracker[chat_id].append(now)
    join_tracker[chat_id] = [t for t in join_tracker[chat_id] if now - t < JOIN_WINDOW]

    cursor.execute("SELECT raid_mode FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    raid_mode = bool(row and row[0] == 1)

    if len(join_tracker[chat_id]) >= JOIN_LIMIT:
        if not raid_mode:
            cursor.execute("UPDATE chat_settings SET raid_mode = 1 WHERE chat_id = ?", (chat_id,))
            conn.commit()
            await send_log(context, chat_id, f"<b>Eylem:</b> RAID MODE AUTO ON\n<b>Neden:</b> {JOIN_WINDOW} sn içinde {JOIN_LIMIT}+ giriş")
            try:
                msg = await context.bot.send_message(chat_id, "🚨 Raid tespit edildi! Raid Mode otomatik açıldı.")
                context.application.create_task(delete_later(msg, 5))
            except Exception:
                pass
            return True

    return raid_mode


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
        await maybe_delete_service_message(update, context)
        raid_now = await handle_new_members_raid(update, context, cid)

        cursor.execute("SELECT welcome, welcome_text, lock_bots FROM chat_settings WHERE chat_id = ?", (cid,))
        row = cursor.fetchone()

        try:
            member_count = await context.bot.get_chat_member_count(cid)
        except Exception:
            member_count = 0

        for nm in update.message.new_chat_members:
            if row and row[2] == 1 and nm.is_bot:
                try:
                    if await bot_can_restrict(cid, context):
                        await context.bot.ban_chat_member(cid, nm.id)
                        await send_log(context, cid, f"<b>Eylem:</b> AUTO BOT BAN\n<b>Hedef:</b> {html.escape(nm.full_name)}")
                except Exception:
                    pass
                continue

            if nm.is_bot:
                continue

            if raid_now:
                muted = await auto_mute_user(cid, nm, context, "Raid mode yeni üye koruması", RAID_MUTE_MINUTES)
                if muted:
                    try:
                        info = await context.bot.send_message(
                            cid,
                            f"🛡️ {nm.mention_html()} raid modu nedeniyle {RAID_MUTE_MINUTES} dakika kısıtlandı.",
                            parse_mode="HTML"
                        )
                        context.application.create_task(delete_later(info, 5))
                    except Exception:
                        pass

            if row and row[0] == 1:
                welcome_text_value = apply_placeholders(row[1], nm, update.effective_chat, member_count)
                await context.bot.send_message(cid, f"👋 {nm.mention_html()}\n{html.escape(welcome_text_value)}", parse_mode="HTML")
        return

    if update.message.left_chat_member:
        await maybe_delete_service_message(update, context)
        cursor.execute("SELECT goodbye, goodbye_text FROM chat_settings WHERE chat_id = ?", (cid,))
        row = cursor.fetchone()
        left = update.message.left_chat_member
        try:
            member_count = await context.bot.get_chat_member_count(cid)
        except Exception:
            member_count = 0
        if row and row[0] == 1 and left:
            goodbye_text_value = apply_placeholders(row[1], left, update.effective_chat, member_count)
            await context.bot.send_message(cid, html.escape(goodbye_text_value), parse_mode="HTML")
        return

    if is_private(update):
        return

    text = (update.message.text or update.message.caption or "").strip()
    lowered = text.lower()
    normalized_text = normalize_text_strong(text)
    normalized_link_text = normalize_link_text(text)

    if text.startswith("#") and len(text) > 1:
        name = text[1:].split()[0].lower()
        cursor.execute("SELECT note_text FROM notes WHERE chat_id = ? AND note_name = ?", (cid, name))
        row = cursor.fetchone()
        if row:
            return await update.message.reply_text(row[0])

    if lowered:
        cursor.execute("SELECT trigger_text, reply_text FROM filters_table WHERE chat_id = ?", (cid,))
        for trig, rep in cursor.fetchall():
            if lowered == trig.lower() or trig.lower() in lowered:
                await update.message.reply_text(rep)
                break

    if await is_admin_user(cid, uid, context) or is_approved(cid, uid):
        return

    cursor.execute("""
    SELECT antilink, lock_link, lock_badword, lock_flood, antispam, raid_mode, lock_sticker, lock_media,
           lock_forward, lock_bots, lock_photo, lock_video, lock_document, lock_voice
    FROM chat_settings WHERE chat_id = ?
    """, (cid,))
    row = cursor.fetchone()
    antilink_on = row[0] == 1
    lock_link = row[1] == 1
    lock_badword = row[2] == 1
    lock_flood = row[3] == 1
    antispam_on = row[4] == 1
    raid_mode_on = row[5] == 1
    lock_sticker = row[6] == 1
    lock_media = row[7] == 1
    lock_forward = row[8] == 1
    lock_bots = row[9] == 1
    lock_photo = row[10] == 1
    lock_video = row[11] == 1
    lock_document = row[12] == 1
    lock_voice = row[13] == 1

    delete_reason = None
    should_auto_mute_repeat = False
    should_auto_mute_sticker = False
    should_auto_mute_media = False

    if text:
        cursor.execute("SELECT trigger_text FROM blacklists WHERE chat_id = ?", (cid,))
        for row_bl in cursor.fetchall():
            trig = row_bl[0].lower()
            if trig and trig in lowered:
                delete_reason = "Blacklist tetik"
                break

    if text and not delete_reason and (antilink_on or lock_link):
        if URL_PATTERN.search(text) or URL_PATTERN.search(normalized_link_text):
            delete_reason = "Link paylaşımı"

    if not delete_reason and normalized_text and lock_badword:
        cursor.execute("SELECT word FROM badwords WHERE chat_id = ?", (cid,))
        compact_text = normalized_text.replace(" ", "")
        for r in cursor.fetchall():
            bw = normalize_badword_word(r[0])
            if bw and bw in compact_text:
                delete_reason = "Yasaklı kelime"
                break

    if not delete_reason and lock_forward and (update.message.forward_origin or update.message.forward_from_chat or update.message.forward_from):
        delete_reason = "Forward kilidi aktif"

    if not delete_reason and lock_sticker and update.message.sticker:
        delete_reason = "Sticker kilidi aktif"

    if not delete_reason and lock_media and (update.message.photo or update.message.video or update.message.document or update.message.animation or update.message.voice):
        delete_reason = "Medya kilidi aktif"

    if not delete_reason and lock_photo and update.message.photo:
        delete_reason = "Fotoğraf kilidi aktif"

    if not delete_reason and lock_video and update.message.video:
        delete_reason = "Video kilidi aktif"

    if not delete_reason and lock_document and update.message.document:
        delete_reason = "Doküman kilidi aktif"

    if not delete_reason and lock_voice and update.message.voice:
        delete_reason = "Ses kilidi aktif"

    if not delete_reason and lock_flood and antispam_on:
        now = time.time()
        spam_tracker[(cid, uid)].append(now)
        spam_tracker[(cid, uid)] = [t for t in spam_tracker[(cid, uid)] if now - t < SPAM_WINDOW]
        if len(spam_tracker[(cid, uid)]) > SPAM_LIMIT:
            delete_reason = "Spam / flood"
            spam_tracker[(cid, uid)].clear()

    if text and antispam_on:
        normalized = normalize_message_for_repeat(normalized_text)
        now = time.time()
        key = (cid, uid, normalized)
        repeat_tracker[key].append(now)
        repeat_tracker[key] = [t for t in repeat_tracker[key] if now - t < REPEAT_WINDOW]
        if len(repeat_tracker[key]) >= REPEAT_LIMIT:
            should_auto_mute_repeat = True
            repeat_tracker[key].clear()

    if antispam_on and update.message.sticker:
        now = time.time()
        sticker_tracker[(cid, uid)].append(now)
        sticker_tracker[(cid, uid)] = [t for t in sticker_tracker[(cid, uid)] if now - t < STICKER_WINDOW]
        if len(sticker_tracker[(cid, uid)]) >= STICKER_LIMIT:
            should_auto_mute_sticker = True
            sticker_tracker[(cid, uid)].clear()

    if antispam_on and (update.message.photo or update.message.video or update.message.document or update.message.animation or update.message.voice):
        now = time.time()
        media_tracker[(cid, uid)].append(now)
        media_tracker[(cid, uid)] = [t for t in media_tracker[(cid, uid)] if now - t < MEDIA_WINDOW]
        if len(media_tracker[(cid, uid)]) >= MEDIA_LIMIT:
            should_auto_mute_media = True
            media_tracker[(cid, uid)].clear()

    if raid_mode_on and not delete_reason and text:
        delete_reason = "Raid mode aktif"

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
                add_punish_history(cid, uid, "AUTO_DELETE", delete_reason, 0)
                result = await apply_escalation(cid, update.effective_user, 0, context, delete_reason)
                if result:
                    info = await context.bot.send_message(cid, html.escape(result), parse_mode="HTML")
                    context.application.create_task(delete_later(info, 5))
                await send_log(
                    context,
                    cid,
                    f"<b>Eylem:</b> AUTO DELETE\n"
                    f"<b>Kullanıcı:</b> {html.escape(update.effective_user.full_name)}\n"
                    f"<b>Neden:</b> {html.escape(delete_reason)}\n"
                    f"<b>Mesaj:</b> {html.escape(text[:150])}"
                )
        except Exception as e:
            logger.error(f"otomatik silme hatası: {e}")

    if should_auto_mute_repeat:
        muted = await auto_mute_user(cid, update.effective_user, context, "Aynı mesajı 5 defa tekrar etme", AUTO_MUTE_MINUTES)
        if muted:
            try:
                info = await context.bot.send_message(
                    cid,
                    f"🔇 {update.effective_user.mention_html()} aynı mesajı 5 kez tekrar ettiği için {AUTO_MUTE_MINUTES} dakika susturuldu.",
                    parse_mode="HTML"
                )
                context.application.create_task(delete_later(info, 5))
            except Exception:
                pass

    if should_auto_mute_sticker:
        muted = await auto_mute_user(cid, update.effective_user, context, "Sticker spam", AUTO_MUTE_MINUTES)
        if muted:
            try:
                info = await context.bot.send_message(
                    cid,
                    f"🧩 {update.effective_user.mention_html()} sticker spam nedeniyle {AUTO_MUTE_MINUTES} dakika susturuldu.",
                    parse_mode="HTML"
                )
                context.application.create_task(delete_later(info, 5))
            except Exception:
                pass

    if should_auto_mute_media:
        muted = await auto_mute_user(cid, update.effective_user, context, "Medya spam", AUTO_MUTE_MINUTES)
        if muted:
            try:
                info = await context.bot.send_message(
                    cid,
                    f"📦 {update.effective_user.mention_html()} medya spam nedeniyle {AUTO_MUTE_MINUTES} dakika susturuldu.",
                    parse_mode="HTML"
                )
                context.application.create_task(delete_later(info, 5))
            except Exception:
                pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update işlenirken hata oluştu", exc_info=context.error)


def main():
    if not TOKEN:
        print("HATA: BOT_TOKEN tanımlı değil.")
        return

    print(f"DB PATH: {DB_PATH}")

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
    app.add_handler(CommandHandler("sban", sban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("kick", kick))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("tmute", tmute))
    app.add_handler(CommandHandler("smute", smute))
    app.add_handler(CommandHandler("stmute", stmute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("warn", warn))
    app.add_handler(CommandHandler("swarn", swarn))
    app.add_handler(CommandHandler("dwarn", dwarn))
    app.add_handler(CommandHandler("warns", warns_cmd))
    app.add_handler(CommandHandler("clearwarns", clearwarns))
    app.add_handler(CommandHandler("resetwarns", resetwarns))
    app.add_handler(CommandHandler("delwarn", delwarn_cmd))
    app.add_handler(CommandHandler("strikes", strikes_cmd))
    app.add_handler(CommandHandler("clearstrikes", clearstrikes_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("unapprove", unapprove_cmd))
    app.add_handler(CommandHandler("approved", approved_cmd))
    app.add_handler(CommandHandler("promote", promote))
    app.add_handler(CommandHandler("demote", demote))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("unadmin", unadmin_cmd))

    app.add_handler(CommandHandler("addsudo", addsudo_cmd))
    app.add_handler(CommandHandler("delsudo", delsudo_cmd))
    app.add_handler(CommandHandler("sudolist", sudolist_cmd))

    app.add_handler(CommandHandler("antilink", antilink_cmd))
    app.add_handler(CommandHandler("welcome", welcome_cmd))
    app.add_handler(CommandHandler("goodbye", goodbye_cmd))
    app.add_handler(CommandHandler("antispam", antispam_cmd))
    app.add_handler(CommandHandler("raid", raid_cmd))
    app.add_handler(CommandHandler("raidmode", raidmode_cmd))
    app.add_handler(CommandHandler("reports", reports_toggle_cmd))
    app.add_handler(CommandHandler("cleancommands", cleancommands_cmd))
    app.add_handler(CommandHandler("cleanservice", cleanservice_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome))
    app.add_handler(CommandHandler("setgoodbye", setgoodbye))
    app.add_handler(CommandHandler("setlog", setlog))
    app.add_handler(CommandHandler("logoff", logoff_cmd))
    app.add_handler(CommandHandler("setrules", setrules))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))
    app.add_handler(CommandHandler("locks", locks_cmd))
    app.add_handler(CommandHandler("setwarnlimit", setwarnlimit))
    app.add_handler(CommandHandler("warnmode", warnmode_cmd))

    app.add_handler(CommandHandler("addbad", addbad))
    app.add_handler(CommandHandler("delbad", delbad))
    app.add_handler(CommandHandler("badlist", badlist))
    app.add_handler(CommandHandler("blacklist", blacklist_cmd))
    app.add_handler(CommandHandler("rmblacklist", rmblacklist_cmd))
    app.add_handler(CommandHandler("blacklists", blacklists_cmd))

    app.add_handler(CommandHandler("pin", pin))
    app.add_handler(CommandHandler("unpin", unpin))
    app.add_handler(CommandHandler("unpinall", unpinall))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("purge", purge))

    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("invitelink", invitelink_cmd))
    app.add_handler(CommandHandler("zombies", zombies_cmd))
    app.add_handler(CommandHandler("settitle", settitle_cmd))
    app.add_handler(CommandHandler("setdesc", setdesc_cmd))
    app.add_handler(CommandHandler("report", report_cmd))

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
