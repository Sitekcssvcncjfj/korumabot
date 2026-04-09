# bot.py (DB yok, Volume/JSON kalıcı)
# Komutlar hem "/" hem "." ile çalışır: .ban /ban
#
# requirements.txt:
#   aiogram==3.6.0
#
# ENV:
#   BOT_TOKEN=xxxx
#   WARN_LIMIT=3
#   MUTE_SECONDS=3600
#   LOG_CHAT_ID=-100...      (opsiyonel)
#   STATE_PATH=/data/state.json  (Railway Volume mount ettiysen)

import os
import re
import json
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple

from aiogram import Bot, Dispatcher, Router, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import (
    Message, ChatMemberUpdated, ChatPermissions,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage


# ----------------- AYAR -----------------
CMD_PREFIXES = "/."
URL_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|www\.)\S+", re.IGNORECASE)
DUR_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)

def CMD(*names: str) -> Command:
    return Command(commands=list(names), prefix=CMD_PREFIXES, ignore_case=True, ignore_mention=True)

@dataclass
class Config:
    token: str
    warn_limit: int = 3
    mute_seconds: int = 3600
    log_chat_id: Optional[int] = None
    state_path: str = "/data/state.json"

def load_config() -> Config:
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN eksik")

    state_path = (os.getenv("STATE_PATH") or "").strip() or "/data/state.json"

    # /data yoksa local fallback
    state_dir = os.path.dirname(state_path) or "."
    if state_dir == "/data" and not os.path.exists("/data"):
        state_path = "./state.json"

    return Config(
        token=token,
        warn_limit=int(os.getenv("WARN_LIMIT", "3")),
        mute_seconds=int(os.getenv("MUTE_SECONDS", "3600")),
        log_chat_id=int(os.getenv("LOG_CHAT_ID")) if os.getenv("LOG_CHAT_ID") else None,
        state_path=state_path,
    )

# ----------------- KALICI STATE (JSON) -----------------
state_lock = asyncio.Lock()
_save_task: Optional[asyncio.Task] = None

# RAM state
username_cache: Dict[str, int] = {}                 # "ali" -> id
chat_settings: Dict[int, Dict[str, object]] = {}    # chat_id -> settings
warns: Dict[Tuple[int, int], int] = {}              # (chat_id, user_id) -> count
pending_verify: Dict[Tuple[int, int], int] = {}     # (chat_id, user_id) -> message_id (kalıcıya yazmıyoruz)

def get_settings(chat_id: int) -> Dict[str, object]:
    s = chat_settings.get(chat_id)
    if not s:
        s = {"welcome": None, "rules": None, "antilink": False, "captcha": False}
        chat_settings[chat_id] = s
    return s

def _warn_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"

async def load_state_from_disk(state_path: str):
    global username_cache, chat_settings, warns

    if not os.path.exists(state_path):
        return

    def _read():
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)

    data = await asyncio.to_thread(_read)

    username_cache = {k: int(v) for k, v in (data.get("username_cache") or {}).items()}

    cs = {}
    for k, v in (data.get("chat_settings") or {}).items():
        try:
            cs[int(k)] = dict(v)
        except:
            pass
    chat_settings = cs

    w = {}
    for k, v in (data.get("warns") or {}).items():
        try:
            chat_id_s, user_id_s = k.split(":", 1)
            w[(int(chat_id_s), int(user_id_s))] = int(v)
        except:
            pass
    warns = w

async def save_state_to_disk(state_path: str):
    # atomic write
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)

    data = {
        "username_cache": username_cache,
        "chat_settings": {str(k): v for k, v in chat_settings.items()},
        "warns": {_warn_key(c, u): cnt for (c, u), cnt in warns.items()},
        "saved_at": datetime.utcnow().isoformat() + "Z",
    }

    tmp = state_path + ".tmp"

    def _write():
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, state_path)

    await asyncio.to_thread(_write)

def schedule_save(config: Config, delay: float = 2.0):
    # çok sık yazmayı engellemek için debounce
    global _save_task
    if _save_task and not _save_task.done():
        return

    async def _job():
        await asyncio.sleep(delay)
        async with state_lock:
            await save_state_to_disk(config.state_path)

    _save_task = asyncio.create_task(_job())

# ----------------- PERMISSIONS -----------------
def muted_permissions() -> ChatPermissions:
    return ChatPermissions(can_send_messages=False)

def open_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
        can_manage_topics=True,
        can_change_info=False,
        can_pin_messages=False,
    )

def has_admin_status(member) -> bool:
    return getattr(member, "status", None) in ("administrator", "creator")

def has_right(member, right: str) -> bool:
    if getattr(member, "status", None) == "creator":
        return True
    if getattr(member, "status", None) != "administrator":
        return False
    return bool(getattr(member, right, False))

async def require_user_right(bot: Bot, chat_id: int, user_id: int, right: Optional[str]) -> bool:
    m = await bot.get_chat_member(chat_id, user_id)
    if not has_admin_status(m):
        return False
    if right is None:
        return True
    return has_right(m, right)

async def require_bot_right(bot: Bot, chat_id: int, right: Optional[str]) -> bool:
    me = await bot.get_me()
    m = await bot.get_chat_member(chat_id, me.id)
    if not has_admin_status(m):
        return False
    if right is None:
        return True
    return has_right(m, right)

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    m = await bot.get_chat_member(chat_id, user_id)
    return has_admin_status(m)

async def log(bot: Bot, config: Config, text: str):
    if not config.log_chat_id:
        return
    try:
        await bot.send_message(config.log_chat_id, text)
    except:
        pass

# ----------------- TARGET PARSING -----------------
async def resolve_username(arg: str) -> Optional[int]:
    u = arg.strip()
    if u.startswith("@"):
        u = u[1:]
    u = u.lower()
    return username_cache.get(u)

async def parse_target(message: Message) -> Tuple[Optional[int], str]:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, "reply"

    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                return ent.user.id, "text_mention"

    parts = (message.text or "").split()
    if len(parts) < 2:
        return None, "no_target"

    arg = parts[1].strip()
    if arg.lstrip("-").isdigit():
        return int(arg), "id"

    if arg.startswith("@"):
        uid = await resolve_username(arg)
        if uid:
            return uid, "username_cache"
        return None, "username_unknown"

    return None, "bad_target"

def parse_duration_to_seconds(s: str) -> Optional[int]:
    s = (s or "").strip().lower().replace(" ", "")
    if not s:
        return None
    if s in ("perm", "perma", "forever", "0"):
        return 0
    matches = DUR_RE.findall(s)
    if not matches:
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total = 0
    for num, unit in matches:
        total += int(num) * mult[unit]
    return total if total > 0 else None

async def need_target(message: Message) -> Optional[int]:
    tid, mode = await parse_target(message)
    if tid:
        return tid
    if mode == "username_unknown":
        await message.reply(
            "Bu @username için ID bilmiyorum.\n"
            "Kullanıcı grupta yazsın/katılsın (bot görsün) sonra tekrar dene. (Telegram kısıtı)"
        )
        return None
    await message.reply("Hedef yok. Reply yap veya .komut <id> / .komut @username yaz.")
    return None

# ----------------- MIDDLEWARE (USERNAME CACHE + SAVE) -----------------
class UsernameCacheMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        config: Config = data["config"]
        u = getattr(event, "from_user", None)
        if u and u.username:
            key = u.username.lower()
            old = username_cache.get(key)
            if old != u.id:
                username_cache[key] = u.id
                schedule_save(config)
        return await handler(event, data)

# ----------------- ROUTER -----------------
router = Router()

@router.message(CMD("help", "yardim", "komutlar"))
async def help_cmd(message: Message):
    await message.reply(
        "Komutlar (/ veya .):\n"
        ".ban/.unban/.kick\n"
        ".mute .unmute .tmute 10m\n"
        ".tban 2d\n"
        ".warn .unwarn .warnings\n"
        ".lock .unlock\n"
        ".promote [reply/@user/id] [title]\n"
        ".demote [reply/@user/id]\n"
        ".antilink on|off\n"
        ".setrules <metin>  /  .rules\n"
        ".setwelcome <metin>\n"
        ".captcha on|off\n\n"
        "Not: @username ile işlem, bot kullanıcıyı daha önce görmüşse çalışır."
    )

@router.message(CMD("antilink"))
async def antilink_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antilink on | off")
    get_settings(message.chat.id)["antilink"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Anti-link: {'AÇIK' if get_settings(message.chat.id)['antilink'] else 'KAPALI'}")

@router.message(CMD("setrules"))
async def setrules_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .setrules <metin>")
    get_settings(message.chat.id)["rules"] = parts[1]
    schedule_save(config)
    await message.reply("Kurallar kaydedildi (volume).")

@router.message(CMD("rules"))
async def rules_cmd(message: Message):
    if not message.chat or message.chat.type == "private":
        return
    await message.reply(get_settings(message.chat.id).get("rules") or "Kural ayarlı değil.")

@router.message(CMD("setwelcome"))
async def setwelcome_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .setwelcome Hoşgeldin {first_name}")
    get_settings(message.chat.id)["welcome"] = parts[1]
    schedule_save(config)
    await message.reply("Welcome kaydedildi (volume).")

@router.message(CMD("captcha"))
async def captcha_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .captcha on | off")
    get_settings(message.chat.id)["captcha"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Captcha: {'AÇIK' if get_settings(message.chat.id)['captcha'] else 'KAPALI'}")

# ---- Moderasyon ----

@router.message(CMD("ban"))
async def ban_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return
    await bot.ban_chat_member(message.chat.id, tid)
    await message.reply(f"Banlandı: <code>{tid}</code>")
    await log(bot, config, f"BAN chat={message.chat.id} target={tid} by={message.from_user.id}")

@router.message(CMD("unban"))
async def unban_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return
    await bot.unban_chat_member(message.chat.id, tid, only_if_banned=True)
    await message.reply(f"Unban: <code>{tid}</code>")
    await log(bot, config, f"UNBAN chat={message.chat.id} target={tid} by={message.from_user.id}")

@router.message(CMD("kick"))
async def kick_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return
    await bot.ban_chat_member(message.chat.id, tid)
    await bot.unban_chat_member(message.chat.id, tid, only_if_banned=True)
    await message.reply(f"Atıldı: <code>{tid}</code>")
    await log(bot, config, f"KICK chat={message.chat.id} target={tid} by={message.from_user.id}")

@router.message(CMD("mute"))
async def mute_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return
    until = datetime.utcnow() + timedelta(seconds=config.mute_seconds)
    await bot.restrict_chat_member(message.chat.id, tid, permissions=muted_permissions(), until_date=until)
    await message.reply(f"Mute: <code>{tid}</code> ({config.mute_seconds}s)")
    await log(bot, config, f"MUTE chat={message.chat.id} target={tid} by={message.from_user.id}")

@router.message(CMD("unmute"))
async def unmute_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return
    await bot.restrict_chat_member(message.chat.id, tid, permissions=open_permissions())
    await message.reply(f"Unmute: <code>{tid}</code>")
    await log(bot, config, f"UNMUTE chat={message.chat.id} target={tid} by={message.from_user.id}")

@router.message(CMD("tmute"))
async def tmute_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    parts = (message.text or "").split()
    if message.reply_to_message:
        if len(parts) < 2:
            return await message.reply("Kullanım: (reply) .tmute 10m [sebep]")
        dur_str = parts[1]
    else:
        if len(parts) < 3:
            return await message.reply("Kullanım: .tmute @user 10m [sebep]")
        dur_str = parts[2]

    sec = parse_duration_to_seconds(dur_str)
    if sec is None or sec == 0:
        return await message.reply("Süre formatı: 10m, 2h, 1d, 1w, 1h30m (perm olmaz)")

    tid = await need_target(message)
    if not tid:
        return

    until = datetime.utcnow() + timedelta(seconds=sec)
    await bot.restrict_chat_member(message.chat.id, tid, permissions=muted_permissions(), until_date=until)
    await message.reply(f"Süreli mute: <code>{tid}</code> süre={dur_str}")
    await log(bot, config, f"TMUTE chat={message.chat.id} target={tid} dur={dur_str} by={message.from_user.id}")

@router.message(CMD("tban"))
async def tban_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    parts = (message.text or "").split()
    if message.reply_to_message:
        if len(parts) < 2:
            return await message.reply("Kullanım: (reply) .tban 2d [sebep]")
        dur_str = parts[1]
    else:
        if len(parts) < 3:
            return await message.reply("Kullanım: .tban @user 2d [sebep]")
        dur_str = parts[2]

    sec = parse_duration_to_seconds(dur_str)
    if sec is None:
        return await message.reply("Süre formatı: 10m, 2h, 1d, 1w, 1h30m (perm: perm)")

    tid = await need_target(message)
    if not tid:
        return

    until = None if sec == 0 else (datetime.utcnow() + timedelta(seconds=sec))
    await bot.ban_chat_member(message.chat.id, tid, until_date=until)
    await message.reply(f"Süreli ban: <code>{tid}</code> süre={dur_str}")
    await log(bot, config, f"TBAN chat={message.chat.id} target={tid} dur={dur_str} by={message.from_user.id}")

# ---- Warn (kalıcı) ----
@router.message(CMD("warn"))
async def warn_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return

    key = (message.chat.id, tid)
    warns[key] = warns.get(key, 0) + 1
    schedule_save(config)
    count = warns[key]

    if count >= config.warn_limit and await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        until = datetime.utcnow() + timedelta(seconds=config.mute_seconds)
        try:
            await bot.restrict_chat_member(message.chat.id, tid, permissions=muted_permissions(), until_date=until)
        except:
            pass
        await message.reply(f"Uyarı: <code>{tid}</code> ({count}/{config.warn_limit}) -> limit aşıldı, mute atıldı.")
    else:
        await message.reply(f"Uyarı verildi: <code>{tid}</code> ({count}/{config.warn_limit})")

@router.message(CMD("unwarn"))
async def unwarn_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return

    key = (message.chat.id, tid)
    warns[key] = max(0, warns.get(key, 0) - 1)
    schedule_save(config)
    await message.reply(f"Uyarı düşürüldü: <code>{tid}</code> (şimdi: {warns[key]})")

@router.message(CMD("warnings"))
async def warnings_cmd(message: Message):
    if not message.chat or message.chat.type == "private":
        return
    tid = message.reply_to_message.from_user.id if (message.reply_to_message and message.reply_to_message.from_user) else (message.from_user.id if message.from_user else None)
    if not tid:
        return
    await message.reply(f"<code>{tid}</code> uyarı sayısı: {warns.get((message.chat.id, tid), 0)}")

# ---- Lock/Unlock ----
@router.message(CMD("lock"))
async def lock_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")
    await bot.set_chat_permissions(message.chat.id, permissions=muted_permissions())
    await message.reply("Grup kilitlendi.")

@router.message(CMD("unlock"))
async def unlock_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")
    await bot.set_chat_permissions(message.chat.id, permissions=open_permissions())
    await message.reply("Grup kilidi açıldı.")

# ---- Promote/Demote ----
@router.message(CMD("promote"))
async def promote_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_promote_members"):
        return await message.reply("Yetki yok: can_promote_members")
    if not await require_bot_right(bot, message.chat.id, "can_promote_members"):
        return await message.reply("Bot yetkisi yok: can_promote_members")

    tid = await need_target(message)
    if not tid:
        return

    parts = (message.text or "").split(maxsplit=2)
    title = parts[2].strip() if len(parts) >= 3 else None

    await bot.promote_chat_member(
        message.chat.id, tid,
        can_manage_chat=False,
        can_change_info=False,
        can_post_messages=False,
        can_edit_messages=False,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=True,
        can_promote_members=False,
        can_invite_users=True,
        can_pin_messages=True,
        can_manage_topics=True,
        is_anonymous=False,
    )
    if title:
        try:
            await bot.set_chat_administrator_custom_title(message.chat.id, tid, title[:16])
        except:
            pass
    await message.reply(f"Yönetici yapıldı: <code>{tid}</code>")

@router.message(CMD("demote"))
async def demote_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_promote_members"):
        return await message.reply("Yetki yok: can_promote_members")
    if not await require_bot_right(bot, message.chat.id, "can_promote_members"):
        return await message.reply("Bot yetkisi yok: can_promote_members")

    tid = await need_target(message)
    if not tid:
        return

    await bot.promote_chat_member(
        message.chat.id, tid,
        can_manage_chat=False,
        can_change_info=False,
        can_post_messages=False,
        can_edit_messages=False,
        can_delete_messages=False,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
        is_anonymous=False,
    )
    await message.reply(f"Yöneticilik alındı: <code>{tid}</code>")

# ---- Welcome + Captcha ----
@router.chat_member()
async def on_join(event: ChatMemberUpdated, bot: Bot, config: Config):
    if not event.chat:
        return
    old_status = getattr(event.old_chat_member, "status", None)
    new_status = getattr(event.new_chat_member, "status", None)
    if not (old_status in ("left", "kicked") and new_status == "member"):
        return

    s = get_settings(event.chat.id)
    user = event.new_chat_member.user

    if user.username:
        key = user.username.lower()
        if username_cache.get(key) != user.id:
            username_cache[key] = user.id
            schedule_save(config)

    if s.get("captcha") and await require_bot_right(bot, event.chat.id, "can_restrict_members"):
        try:
            await bot.restrict_chat_member(event.chat.id, user.id, permissions=muted_permissions())
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Doğrula", callback_data=f"verify:{user.id}")
            ]])
            msg = await bot.send_message(event.chat.id, f"{user.first_name} doğrulama gerekli. Butona bas.", reply_markup=kb)
            pending_verify[(event.chat.id, user.id)] = msg.message_id
        except:
            pass

    if s.get("welcome"):
        text = str(s["welcome"]).replace("{first_name}", user.first_name or "")
        text = text.replace("{username}", f"@{user.username}" if user.username else (user.first_name or ""))
        try:
            await bot.send_message(event.chat.id, text)
        except:
            pass

@router.callback_query()
async def verify_callback(call: CallbackQuery, bot: Bot):
    if not call.data or not call.message or not call.from_user:
        return
    if not call.data.startswith("verify:"):
        return

    chat_id = call.message.chat.id
    target_id = int(call.data.split(":", 1)[1])
    if call.from_user.id != target_id:
        return await call.answer("Bu buton sana ait değil.", show_alert=True)

    if not await require_bot_right(bot, chat_id, "can_restrict_members"):
        return await call.answer("Botun yetkisi yok.", show_alert=True)

    try:
        await bot.restrict_chat_member(chat_id, target_id, permissions=open_permissions())
        try:
            await call.message.delete()
        except:
            pass
        pending_verify.pop((chat_id, target_id), None)
        await call.answer("Doğrulandı!")
    except:
        await call.answer("Açılamadı.", show_alert=True)

# ---- Anti-link otomatik ----
@router.message()
async def anti_link_auto(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.text:
        return
    if message.text.startswith("/") or message.text.startswith("."):
        return

    s = get_settings(message.chat.id)
    if not s.get("antilink"):
        return
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return

    if URL_RE.search(message.text):
        if await require_bot_right(bot, message.chat.id, "can_delete_messages"):
            try:
                await message.delete()
            except:
                pass

        key = (message.chat.id, message.from_user.id)
        warns[key] = warns.get(key, 0) + 1
        schedule_save(config)

        if warns[key] >= config.warn_limit and await require_bot_right(bot, message.chat.id, "can_restrict_members"):
            until = datetime.utcnow() + timedelta(seconds=config.mute_seconds)
            try:
                await bot.restrict_chat_member(message.chat.id, message.from_user.id, permissions=muted_permissions(), until_date=until)
            except:
                pass

        try:
            await message.answer(f"Link yasak. Uyarı: {warns[key]}/{config.warn_limit}")
        except:
            pass

# ----------------- MAIN -----------------
async def main():
    logging.basicConfig(level=logging.INFO)
    config = load_config()

    async with state_lock:
        await load_state_from_disk(config.state_path)

    bot = Bot(
        token=config.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())

    class ConfigMW(BaseMiddleware):
        async def __call__(self, handler, event, data):
            data["config"] = config
            return await handler(event, data)

    dp.update.middleware(ConfigMW())
    dp.update.middleware(UsernameCacheMiddleware())

    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
