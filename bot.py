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
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from duckduckgo_search import DDGS

# ====================== AYARLAR ======================
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "bot_database.db")

BOT_USERNAME = "@KGBKORUMABot"
SUPPORT_URL = "https://t.me/KGBotomasyon"

# Sabitler
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
RAID_MUTE_MINUTES = 30
SEARCH_COOLDOWN = 15
CALLBACK_COOLDOWN = 1.5
JOIN_WINDOW = 30
JOIN_LIMIT = 5
STICKER_WINDOW = 10
STICKER_LIMIT = 5
MEDIA_WINDOW = 10
MEDIA_LIMIT = 5

# ====================== LOGGING ======================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
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
    chat_id INTEGER PRIMARY KEY,
    antilink INTEGER DEFAULT 0,
    welcome INTEGER DEFAULT 0,
    goodbye INTEGER DEFAULT 0,
    welcome_text TEXT DEFAULT 'Hoş geldin!',
    goodbye_text TEXT DEFAULT 'Görüşürüz!',
    log_chat_id INTEGER DEFAULT 0,
    rules_text TEXT DEFAULT 'Kurallar ayarlanmadı.',
    lock_link INTEGER DEFAULT 0,
    lock_badword INTEGER DEFAULT 1,
    lock_flood INTEGER DEFAULT 1,
    antispam INTEGER DEFAULT 1,
    raid_mode INTEGER DEFAULT 0,
    warn_limit INTEGER DEFAULT 3,
    warn_mode TEXT DEFAULT 'ban',
    lock_sticker INTEGER DEFAULT 0,
    lock_media INTEGER DEFAULT 0,
    clean_commands INTEGER DEFAULT 0,
    reports_enabled INTEGER DEFAULT 1,
    clean_service INTEGER DEFAULT 0,
    lock_forward INTEGER DEFAULT 0,
    lock_bots INTEGER DEFAULT 0,
    lock_photo INTEGER DEFAULT 0,
    lock_video INTEGER DEFAULT 0,
    lock_document INTEGER DEFAULT 0,
    lock_voice INTEGER DEFAULT 0
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
callback_cooldowns = {}
join_tracker = defaultdict(list)
sticker_tracker = defaultdict(list)
media_tracker = defaultdict(list)

URL_PATTERN = re.compile(
    r"(?i)\b(?:https?://|www\.|t\.me/|telegram\.me/|discord\.gg/|"
    r"[a-z0-9-]+\.(?:com|net|org|me|io|xyz|ru|co|gg))\S*"
)


# ====================== YARDIMCI FONKSİYONLAR ======================
def ensure_chat_settings(chat_id: int):
    cursor.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
    cursor.execute("INSERT OR IGNORE INTO mod_stats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()


def is_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"


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
    return [text[i:i + size] for i in range(0, len(text), size)]


def normalize_text(text: str) -> str:
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    tr_map = str.maketrans("çğışöü@$€", "cgisouase")
    text = text.translate(tr_map)
    text = re.sub(r"[^\w\s./:-]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_badword(word: str) -> str:
    return normalize_text(word).replace(" ", "")


def apply_placeholders(template: str, user, chat, count: int = 0) -> str:
    first = user.first_name or ""
    last = user.last_name or ""
    username = f"@{user.username}" if user.username else "yok"
    fullname = f"{first} {last}".strip()
    chatname = chat.title or ""
    result = template.replace("{first}", first).replace("{last}", last)
    result = result.replace("{fullname}", fullname).replace("{username}", username)
    result = result.replace("{id}", str(user.id)).replace("{chatname}", chatname)
    return result.replace("{count}", str(count))


def inc_mod_stat(chat_id: int, field: str):
    cursor.execute(f"UPDATE mod_stats SET {field} = {field} + 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()


async def send_log(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    cursor.execute("SELECT log_chat_id FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row and row[0]:
        try:
            await context.bot.send_message(row[0], f"<b>LOG:</b>\n{text}", parse_mode="HTML", disable_web_page_preview=True)
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


# ====================== BOT YETKİ KONTROL ======================
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


# ====================== KULLANICI YETKİ KONTROL ======================
def is_sudo(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM sudo_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None


async def get_member_status(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        return await context.bot.get_chat_member(chat_id, user_id)
    except:
        return None


async def is_admin_user(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_sudo(user_id):
        return True
    member = await get_member_status(chat_id, user_id, context)
    return bool(member and member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER))


async def is_owner(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await get_member_status(chat_id, user_id, context)
    return bool(member and member.status == ChatMemberStatus.OWNER)


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
    await update.message.reply_text("Bu komutu kullanma yetkin yok.")
    return False


# ====================== HEDEF KULLANICI BULMA (DÜZELTİLDİ) ======================
async def extract_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply, @username veya ID ile hedef bulma - TAM DÜZELTİLMİŞ"""
    target = None
    reason = "Sebep belirtilmedi."
    args = context.args or []

    # Reply kontrolü
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
        if args:
            reason = " ".join(args).strip()
        return target, reason

    if not args:
        return None, reason

    identifier = args[0]

    try:
        # ID ile
        if identifier.isdigit():
            member = await get_member_status(update.effective_chat.id, int(identifier), context)
            if member:
                target = member.user

        # @username ile - DOĞRU YÖNTEM
        elif identifier.startswith("@"):
            username = identifier[1:].lower()
            try:
                # Önce grup üyeleri arasında ara
                chat_id = update.effective_chat.id
                member = await get_member_status(chat_id, identifier, context)
                if member:
                    target = member.user
                else:
                    # Telegram API ile doğrudan kullanıcı bilgisi al
                    try:
                        user = await context.bot.get_chat(identifier)
                        # Grupta olup olmadığını kontrol et
                        try:
                            check = await get_member_status(chat_id, user.id, context)
                            if check:
                                target = user
                        except:
                            pass
                    except:
                        pass
            except Exception as e:
                logger.error(f"@username hatası: {e}")

        if target and len(args) > 1:
            reason = " ".join(args[1:]).strip()

    except Exception as e:
        logger.error(f"Hedef bulma hatası: {e}")

    return target, reason


async def extract_target_with_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Süreli işlemler için hedef + süre + sebep"""
    target, reason = await extract_target(update, context)
    args = context.args or []
    duration = None

    if target:
        if args:
            duration = parse_time(args[0])
            if duration and len(args) > 1:
                reason = " ".join(args[1:]).strip()
        return target, duration, reason

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
        return False, "Admin'e işlem yapamazsın."

    return True, None


# ====================== MODERASYON İŞLEMLERİ ======================
async def mod_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    """Tek bir fonksiyonda ban/mute/kick"""
    if not await require_admin(update, context):
        return

    cid = update.effective_chat.id
    actor = update.effective_user
    duration = None

    # Süreli işlemler
    if action in ("tban", "tmute"):
        target, duration, reason = await extract_target_with_time(update, context)
        if not duration and action in ("tban", "tmute"):
            return await update.message.reply_text(f"Kullanım: /{action} <reply/@/id> <10m/1h/1d> [sebep]")
    else:
        target, reason = await extract_target(update, context)

    if not target:
        return await update.message.reply_text(f"Kullanım: /{action} <reply | @username | ID> [sebep]")

    allowed, err = await can_act_on_target(cid, actor.id, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)

    if not await bot_can_restrict(cid, context):
        return await update.message.reply_text("Botun yetkisi yok.")

    until_date = None
    if duration:
        until_date = datetime.datetime.now() + datetime.timedelta(seconds=duration)

    try:
        if action in ("ban", "tban", "sban"):
            await context.bot.ban_chat_member(cid, target.id, until_date=until_date)
            act_text = "süreli banlandı" if duration else "banlandı"
            inc_mod_stat(cid, "total_bans")
            add_punish_history(cid, target.id, action.upper(), reason, actor.id)

        elif action in ("mute", "tmute", "smute"):
            await context.bot.restrict_chat_member(
                cid, target.id,
                ChatPermissions(can_send_messages=False)
            )
            act_text = "süreli susturuldu" if duration else "susturuldu"
            inc_mod_stat(cid, "total_mutes")
            add_punish_history(cid, target.id, action.upper(), reason, actor.id)

        elif action == "kick":
            await context.bot.ban_chat_member(cid, target.id)
            await context.bot.unban_chat_member(cid, target.id)
            act_text = "atıldı"
            add_punish_history(cid, target.id, "KICK", reason, actor.id)

        else:
            return

        # Mesajı gönder (reply yerine send_message - patlama önlendi)
        display_name = html.escape(target.full_name)
        actor_name = html.escape(actor.full_name)
        time_info = f"\n⏰ Süre: {duration // 60 if duration else 'Kalıcı'} dakika" if duration else ""

        await context.bot.send_message(
            cid,
            f"🔨 <b>{display_name}</b> {act_text}.\n"
            f"👮 Yetkili: {actor_name}\n"
            f"📝 Sebep: {html.escape(reason)}{time_info}",
            parse_mode="HTML"
        )

        await send_log(context, cid,
            f"Eylem: {action.upper()}\nHedef: {target.full_name} ({target.id})\n"
            f"Yetkili: {actor.full_name}\nSebep: {reason}"
        )

        await silent_delete_command(update, context)

    except Exception as e:
        logger.error(f"Mod action hatası ({action}): {e}")
        await update.message.reply_text("İşlem başarısız. Botun yetkilerini kontrol edin.")


def add_punish_history(chat_id: int, user_id: int, action: str, reason: str, actor_id: int):
    cursor.execute(
        "INSERT INTO punish_history (chat_id, user_id, action, reason, actor_id, ts) VALUES (?,?,?,?,?,?)",
        (chat_id, user_id, action, reason, actor_id, int(time.time()))
    )
    conn.commit()


# ====================== WARN SİSTEMİ ======================
async def warn_action(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str = "normal"):
    """warn / swarn / dwarn"""
    if not await require_admin(update, context):
        return

    cid = update.effective_chat.id
    actor = update.effective_user
    target, reason = await extract_target(update, context)

    if not target:
        return await update.message.reply_text("Kullanım: /warn <reply | @username | ID> [sebep]")

    allowed, err = await can_act_on_target(cid, actor.id, target.id, context)
    if not allowed:
        return await update.message.reply_text(err)

    # Mesaj sil (dwarn)
    if mode == "delete" and update.message.reply_to_message:
        try:
            await update.message.reply_to_message.delete()
            inc_mod_stat(cid, "total_deleted")
        except:
            pass

    # Warn ekle
    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    row = cursor.fetchone()
    current = (row[0] if row else 0) + 1
    warn_limit = get_warn_limit(cid)

    cursor.execute(
        "INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)",
        (cid, target.id, current)
    )
    conn.commit()
    inc_mod_stat(cid, "total_warns")
    add_punish_history(cid, target.id, f"WARN_{mode.upper()}", reason, actor.id)

    # Bildirim
    if mode != "silent":
        msg = await context.bot.send_message(
            cid,
            f"⚠️ <b>{html.escape(target.full_name)}</b> warn aldı. ({current}/{warn_limit})\n"
            f"📝 Sebep: {html.escape(reason)}",
            parse_mode="HTML"
        )
        asyncio.create_task(delete_later(msg, 5))

    await send_log(context, cid,
        f"Eylem: WARN ({mode})\nHedef: {target.full_name}\n"
        f"Yetkili: {actor.full_name}\nSebep: {reason}\n{curent}/{warn_limit}"
    )

    # Limit kontrolü
    if current >= warn_limit:
        await execute_warn_limit(cid, target, actor, reason, context)

    await silent_delete_command(update, context)


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

        await context.bot.send_message(chat_id, text, parse_mode="HTML")
        cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
        conn.commit()
    except Exception as e:
        logger.error(f"Warn limit hatası: {e}")


# ====================== AUTO MUTE ======================
async def auto_mute_user(chat_id: int, user, context: ContextTypes.DEFAULT_TYPE, reason: str, minutes: int = AUTO_MUTE_MINUTES):
    if not await bot_can_restrict(chat_id, context):
        return False
    until = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
    try:
        await context.bot.restrict_chat_member(
            chat_id, user.id,
            ChatPermissions(can_send_messages=False),
            until_date=until
        )
        inc_mod_stat(chat_id, "total_mutes")
        add_punish_history(chat_id, user.id, "AUTO_MUTE", reason, 0)
        await send_log(context, chat_id, f"AUTO_MUTE: {user.full_name} - {reason}")
        return True
    except Exception as e:
        logger.error(f"Auto mute hatası: {e}")
        return False


# ====================== STRIKE SİSTEMİ ======================
def get_strike(chat_id: int, user_id: int) -> int:
    cursor.execute("SELECT strike_count FROM strikes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    row = cursor.fetchone()
    return row[0] if row else 0


async def apply_strike(chat_id: int, user, context: ContextTypes.DEFAULT_TYPE, reason: str):
    current = get_strike(chat_id, user.id) + 1
    cursor.execute(
        "INSERT OR REPLACE INTO strikes (chat_id, user_id, strike_count) VALUES (?, ?, ?)",
        (chat_id, user.id, current)
    )
    conn.commit()

    if current == 1:
        return f"⚠️ 1. Strike: Warn"
    elif current == 2:
        await context.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False),
                                               until_date=datetime.datetime.now() + datetime.timedelta(minutes=30))
        inc_mod_stat(chat_id, "total_mutes")
        return "🔇 2. Strike: 30dk mute"
    elif current == 3:
        await context.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False),
                                               until_date=datetime.datetime.now() + datetime.timedelta(days=1))
        inc_mod_stat(chat_id, "total_mutes")
        return "🔇 3. Strike: 1g mute"
    else:
        await context.bot.ban_chat_member(chat_id, user.id)
        inc_mod_stat(chat_id, "total_bans")
        cursor.execute("DELETE FROM strikes WHERE chat_id = ? AND user_id = ?", (chat_id, user.id))
        conn.commit()
        return "🚫 Strike limiti: Banlandı"


# ====================== UNBAN / UNMUTE ======================
async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /unban <reply | @username | ID>")
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        await context.bot.send_message(update.effective_chat.id, f"✅ {target.full_name} banı kaldırıldı.")
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Ban kaldırılamadı.")


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /unmute <reply | @username | ID>")
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            ChatPermissions(can_send_messages=True, can_send_polls=True, can_add_web_page_previews=True,
                            can_invite_users=True)
        )
        await context.bot.send_message(update.effective_chat.id, f"🔊 {target.full_name} susturma kaldırıldı.")
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Susturma kaldırılamadı.")


# ====================== PROMOTE / DEMOTE ======================
async def promote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    actor = update.effective_user

    if not await is_owner(cid, actor.id, context) and not is_sudo(actor.id):
        return await update.message.reply_text("Sadece owner promote yapabilir.")

    if not await bot_can_promote(cid, context):
        return await update.message.reply_text("Botun promote yetkisi yok.")

    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /promote <reply | @username | ID>")

    if await is_admin_user(cid, target.id, context):
        return await update.message.reply_text("Zaten admin.")

    try:
        await context.bot.promote_chat_member(
            chat_id=cid, user_id=target.id,
            can_manage_chat=True, can_delete_messages=True,
            can_restrict_members=True, can_pin_messages=True,
            can_invite_users=True, can_manage_video_chats=True
        )
        await context.bot.send_message(cid, f"👑 {target.full_name} admin yapıldı.")
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Promote başarısız.")


async def demote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    actor = update.effective_user

    if not await is_owner(cid, actor.id, context) and not is_sudo(actor.id):
        return await update.message.reply_text("Sadece owner demote yapabilir.")

    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /demote <reply | @username | ID>")

    if not await is_admin_user(cid, target.id, context):
        return await update.message.reply_text("Zaten admin değil.")

    try:
        await context.bot.promote_chat_member(
            chat_id=cid, user_id=target.id,
            can_manage_chat=False, can_delete_messages=False,
            can_restrict_members=False, can_pin_messages=False,
            can_invite_users=False, can_manage_video_chats=False
        )
        await context.bot.send_message(cid, f"⬇️ {target.full_name} adminlikten alındı.")
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Demote başarısız.")


# ====================== WARN KOMUTLARI ======================
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
    conn.commit
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
    conn.commit
    await update.message.reply_text(f"✅ {target.full_name} 1 warn silindi. Kalan: {current}")
    await silent_delete_command(update, context)


# ====================== STRIKE KOMUTLARI ======================
async def strikes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /strikes <reply | @username | ID>")
    count = get_strike(update.effective_chat.id, target.id)
    await update.message.reply_text(f"🎯 {target.full_name} strike: {count}")


async def clearstrikes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    target, _ = await extract_target(update, context)
    if not target:
        return await update.message.reply_text("Kullanım: /clearstrikes <reply | @username | ID>")
    cid = update.effective_chat.id
    cursor.execute("DELETE FROM strikes WHERE chat_id = ? AND user_id = ?", (cid, target.id))
    conn.commit()
    await update.message.reply_text(f"✅ {target.full_name} strike'lar temizlendi.")
    await silent_delete_command(update, context)


# ====================== APPROVE SİSTEMİ ======================
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


def is_approved(chat_id: int, user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM approvals WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    return cursor.fetchone() is not None


# ====================== SUDO SİSTEMİ ======================
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
    text = "👑 Sudo Listesi:\n" + "\n".join([f"• <code>{r[0]}</code>" for r in rows])
    await update.message.reply_text(text, parse_mode="HTML")


# ====================== PIN / UNPIN / PURGE / DEL ======================
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
        await update.message.reply_text("Pinlenemedi.")


async def unpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Botun pin yetkisi yok.")
    try:
        await context.bot.unpin_chat_message(update.effective_chat.id)
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Unpin başarısız.")


async def unpinall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Botun pin yetkisi yok.")
    try:
        await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        await silent_delete_command(update, context)
    except:
        await update.message.reply_text("Unpin all başarısız.")


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
        await update.message.reply_text("Silinemedi.")


async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Mesaja reply at.")
    if not await bot_can_delete(update.effective_chat.id, context):
        return await update.message.reply_text("Botun silme yetkisi yok.")

    start_id = update.message.reply_to_message.message_id
    end_id = update.message.message_id
    amount = end_id - start_id + 1
    if amount > PURGE_LIMIT:
        return await update.message.reply_text(f"Max {PURGE_LIMIT} mesaj.")

    deleted = 0
    for msg_id in range(start_id, end_id + 1):
        try:
            await context.bot.delete_message(update.effective_chat.id, msg_id)
            deleted += 1
        except:
            pass

    info = await context.bot.send_message(update.effective_chat.id, f"🧹 {deleted} mesaj silindi.")
    asyncio.create_task(delete_later(info, 5))
    await silent_delete_command(update, context)


# ====================== TOGGLE AYARLAR ======================
async def toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, label: str):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        return await update.message.reply_text(f"Kullanım: /{field} on|off")
    cid = update.effective_chat.id
    val = 1 if context.args[0].lower() == "on" else 0
    cursor.execute(f"UPDATE chat_settings SET {field} = ? WHERE chat_id = ?", (val, cid))
    conn.commit
    await update.message.reply_text(f"⚙️ {label} {'✅' if val else '❌'}")
    await silent_delete_command(update, context)


# ====================== SET TEXT KOMUTLARI ======================
async def set_text_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, label: str):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text(f"Kullanım: /{field} <metin>")
    cid = update.effective_chat.id
    text = " ".join(context.args)
    cursor.execute(f"UPDATE chat_settings SET {field}_text = ? WHERE chat_id = ?", (text, cid))
    conn.commit()
    await update.message.reply_text(f"✅ {label} güncellendi.")
    await silent_delete_command(update, context)


# ====================== SETLOG ======================
async def setlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    target = int(context.args[0]) if context.args and context.args[0].isdigit() else cid

    try:
        await context.bot.send_message(target, f"✅ Log test - Kaynak: {cid}", parse_mode="HTML")
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


# ====================== SETRULES ======================
async def setrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /setrules <kurallar>")
    cid = update.effective_chat.id
    text = " ".join(context.args)
    cursor.execute("UPDATE chat_settings SET rules_text = ? WHERE chat_id = ?", (text, cid))
    conn.commit()
    await update.message.reply_text("✅ Kurallar güncellendi.")
    await silent_delete_command(update, context)


# ====================== SETWARNLIMIT / WARNMODE ======================
async def setwarnlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Kullanım: /setwarnlimit <1-20>")
    val = int(context.args[0])
    if not 1 <= val <= 20:
        return await update.message.reply_text("1-20 arası.")
    cid = update.effective_chat.id
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
    cursor.execute("UPDATE chat_settings SET warn_mode = ? WHERE chat_id = ?", (context.args[0].lower(), cid))
    conn.commit()
    await update.message.reply_text(f"✅ Warn mode: {context.args[0]}")
    await silent_delete_command(update, context)


# ====================== SETTINGS ======================
async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    cid = update.effective_chat.id
    cursor.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (cid,))
    row = cursor.fetchone()
    if not row:
        return await update.message.reply_text("Ayarlar yok.")

    fields = [
        ("antilink", "Antilink"), ("welcome", "Welcome"), ("goodbye", "Goodbye"),
        ("lock_link", "Lock Link"), ("lock_badword", "Lock Badword"),
        ("lock_flood", "Lock Flood"), ("antispam", "AntiSpam"),
        ("raid_mode", "Raid Mode"), ("lock_sticker", "Lock Sticker"),
        ("lock_media", "Lock Media"), ("lock_forward", "Lock Forward"),
        ("lock_bots", "Lock Bots"), ("clean_commands", "Clean Cmd"),
        ("clean_service", "Clean Service"), ("reports_enabled", "Reports")
    ]

    text = "⚙️ <b>Ayarlar</b>\n\n"
    for i, (field, label) in enumerate(fields):
        val = row[i + 1] if i + 1 < len(row) else 0
        text += f"• {label}: {'✅' if val else '❌'}\n"

    cursor.execute("SELECT warn_limit, warn_mode FROM chat_settings WHERE chat_id = ?", (cid,))
    wrow = cursor.fetchone()
    if wrow:
        text += f"\n• Warn Limit: {wrow[0]}\n• Warn Mode: {wrow[1]}"

    await update.message.reply_text(text, parse_mode="HTML")


# ====================== LOCK / UNLOCK ======================
async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /lock link|badword|flood|sticker|media|forward|bot|photo|video|document|voice")
    cid = update.effective_chat.id
    field_map = {
        "link": "lock_link", "badword": "lock_badword", "flood": "lock_flood",
        "sticker": "lock_sticker", "media": "lock_media", "forward": "lock_forward",
        "bot": "lock_bots", "bots": "lock_bots", "photo": "lock_photo",
        "video": "lock_video", "document": "lock_document", "voice": "lock_voice"
    }
    field = field_map.get(context.args[0].lower())
    if not field:
        return await update.message.reply_text("Geçersiz.")
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
    field_map = {
        "link": "lock_link", "badword": "lock_badword", "flood": "lock_flood",
        "sticker": "lock_sticker", "media": "lock_media", "forward": "lock_forward",
        "bot": "lock_bots", "bots": "lock_bots", "photo": "lock_photo",
        "video": "lock_video", "document": "lock_document", "voice": "lock_voice"
    }
    field = field_map.get(context.args[0].lower())
    if not field:
        return await update.message.reply_text("Geçersiz.")
    cursor.execute(f"UPDATE chat_settings SET {field} = 0 WHERE chat_id = ?", (cid,))
    conn.commit()
    await update.message.reply_text(f"🔓 {context.args[0]} açıldı.")
    await silent_delete_command(update, context)


async def locks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    cursor.execute("SELECT lock_link, lock_badword, lock_flood, lock_sticker, lock_media, lock_forward, lock_bots, lock_photo, lock_video, lock_document, lock_voice FROM chat_settings WHERE chat_id = ?", (cid,))
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
    word = normalize_badword(context.args[0])
    cursor.execute("INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?, ?)", (cid, word))
    conn.commit()
    await update.message.reply_text(f"✅ Badword: {word}")
    await silent_delete_command(update, context)


async def delbad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /delbad <kelime>")
    cid = update.effective_chat.id
    word = normalize_badword(context.args[0])
    cursor.execute("DELETE FROM badwords WHERE chat_id = ? AND word = ?", (cid, word))
    conn.commit()
    await update.message.reply_text(f"🗑️ Silindi: {word}")
    await silent_delete_command(update, context)


async def badlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    cursor.execute("SELECT word FROM badwords WHERE chat_id = ?", (cid,))
    rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("Liste bo
