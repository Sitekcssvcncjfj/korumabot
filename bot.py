# bot.py (STABİL - DB YOK - Volume(JSON) ile kalıcı - LOG YOK)
# MissRose tarzı: yetki kontrolü, / ve . komutları, anti-spam, notlar/filtreler, lock types, captcha, uyarı sistemi vb.
#
# Önerilen requirements.txt:
#   aiogram==3.6.0
#
# Railway Volume:
#   Volume mount path: /data
#   ENV: STATE_PATH=/data/state.json
#
# ENV:
#   BOT_TOKEN=xxxx
#   STATE_PATH=/data/state.json           (opsiyonel; yoksa ./state.json)
#   DEFAULT_WARN_LIMIT=3
#   DEFAULT_MUTE_SECONDS=3600

import os
import re
import json
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, Deque
from collections import deque

from aiogram import Bot, Dispatcher, Router, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage


# ----------------- GENEL -----------------

CMD_PREFIXES = "/."
URL_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|www\.)\S+", re.IGNORECASE)
DUR_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)

LOCK_TYPES = {
    "links", "all", "media", "photos", "videos", "documents", "stickers", "gifs", "voice", "audio"
}

def CMD(*names: str) -> Command:
    # /komut ve .komut
    return Command(commands=list(names), prefix=CMD_PREFIXES, ignore_case=True, ignore_mention=True)

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def parse_duration_to_seconds(s: str) -> Optional[int]:
    """
    10m, 2h, 1d, 1w, 1h30m
    """
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


# ----------------- CONFIG -----------------

@dataclass
class Config:
    token: str
    state_path: str
    default_warn_limit: int = 3
    default_mute_seconds: int = 3600

def load_config() -> Config:
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN eksik")

    state_path = (os.getenv("STATE_PATH") or "").strip() or "/data/state.json"
    # /data yoksa local fallback
    if state_path.startswith("/data") and not os.path.exists("/data"):
        state_path = "./state.json"

    return Config(
        token=token,
        state_path=state_path,
        default_warn_limit=int(os.getenv("DEFAULT_WARN_LIMIT", "3")),
        default_mute_seconds=int(os.getenv("DEFAULT_MUTE_SECONDS", "3600")),
    )


# ----------------- STATE (JSON, VOLUME KALICI) -----------------
# NOT: Telegram Bot API username -> user_id direkt çözmez.
# Bu yüzden @username ile işlem, bot o kullanıcıyı daha önce görmüşse çalışır (cache).

STATE_LOCK = asyncio.Lock()
SAVE_TASK: Optional[asyncio.Task] = None

STATE: Dict[str, Any] = {
    "username_cache": {},   # "username" -> user_id
    "chats": {},            # str(chat_id) -> chat_state
    "saved_at": None
}

def chat_state(chat_id: int, config: Config) -> Dict[str, Any]:
    chats = STATE.setdefault("chats", {})
    key = str(chat_id)
    cs = chats.get(key)
    if not cs:
        cs = {
            "settings": {
                "welcome": None,
                "rules": None,

                "antilink": False,
                "antiforward": False,
                "antiservice": False,

                "adminonly": False,

                "captcha": False,
                "captcha_timeout_sec": 120,

                "warn_limit": config.default_warn_limit,
                "mute_seconds": config.default_mute_seconds,

                "antiflood": {
                    "enabled": False,
                    "max_msgs": 6,
                    "per_sec": 5,
                    "action": "tmute",       # "warn" veya "tmute"
                    "mute_sec": 300,
                },

                "anticaps": {
                    "enabled": False,
                    "ratio": 0.7,            # büyük harf oranı
                    "min_len": 12,
                    "action": "warn"         # warn veya del
                },

                "antiemoji": {
                    "enabled": False,
                    "max": 12,
                    "action": "warn"
                },

                "antimention": {
                    "enabled": False,
                    "max": 6,
                    "action": "warn"
                },

                "antiraid": {
                    "enabled": False,
                    "threshold": 8,          # pencere içinde kaç join olursa
                    "window_sec": 60,
                    "lock_sec": 120,
                },

                "locks": []  # örn: ["links", "photos"]
            },

            "warns": {
                # "user_id": {"count": int, "reasons": [{"by":id, "at":iso, "reason":text}]}
            },

            "notes": {     # "name": "content"
            },

            "filters": {   # "keyword": "response"
            },

            "captcha_pending": {
                # "user_id": {"expires_at": iso, "message_id": int|None}
            },

            "raid": {
                "lockdown_until": None  # iso
            }
        }
        chats[key] = cs
    return cs

def schedule_save(config: Config, delay: float = 1.5):
    global SAVE_TASK
    if SAVE_TASK and not SAVE_TASK.done():
        return

    async def _job():
        await asyncio.sleep(delay)
        async with STATE_LOCK:
            await save_state(config.state_path)

    SAVE_TASK = asyncio.create_task(_job())

async def load_state(path: str):
    global STATE
    if not os.path.exists(path):
        return
    def _read():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    try:
        data = await asyncio.to_thread(_read)
        if isinstance(data, dict):
            # basit doğrulama
            if "username_cache" in data and "chats" in data:
                STATE = data
    except Exception:
        # bozuk dosya varsa bot yine çalışsın
        pass

async def save_state(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    STATE["saved_at"] = utcnow().isoformat() + "Z"
    tmp = path + ".tmp"

    def _write():
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    await asyncio.to_thread(_write)


# ----------------- YETKİ / PERMISSIONS -----------------

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

def is_admin_status(member) -> bool:
    return getattr(member, "status", None) in ("administrator", "creator")

def has_right(member, right: str) -> bool:
    if getattr(member, "status", None) == "creator":
        return True
    if getattr(member, "status", None) != "administrator":
        return False
    return bool(getattr(member, right, False))

async def require_user_right(bot: Bot, chat_id: int, user_id: int, right: Optional[str]) -> bool:
    m = await bot.get_chat_member(chat_id, user_id)
    if not is_admin_status(m):
        return False
    return True if right is None else has_right(m, right)

async def require_bot_right(bot: Bot, chat_id: int, right: Optional[str]) -> bool:
    me = await bot.get_me()
    m = await bot.get_chat_member(chat_id, me.id)
    if not is_admin_status(m):
        return False
    return True if right is None else has_right(m, right)

async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    m = await bot.get_chat_member(chat_id, user_id)
    return is_admin_status(m)


# ----------------- TARGET (reply / id / @username cache) -----------------

async def resolve_username(username: str) -> Optional[int]:
    u = username.strip()
    if u.startswith("@"):
        u = u[1:]
    u = u.lower().strip()
    if not u:
        return None
    return int(STATE.get("username_cache", {}).get(u)) if STATE.get("username_cache", {}).get(u) else None

async def parse_target(message: Message) -> Tuple[Optional[int], str]:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, "reply"

    # text_mention entity -> direkt user id
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


# ----------------- MIDDLEWARE -----------------

class InjectConfigMiddleware(BaseMiddleware):
    def __init__(self, config: Config):
        self.config = config

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)

class UsernameCacheMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        config: Config = data["config"]
        u = getattr(event, "from_user", None)
        if u and u.username:
            cache = STATE.setdefault("username_cache", {})
            key = u.username.lower()
            if cache.get(key) != u.id:
                cache[key] = u.id
                schedule_save(config)
        return await handler(event, data)


# ----------------- RUNTIME TRACKERS (RAM) -----------------
# Flood: (chat_id,user_id) -> deque[timestamps]
FLOOD: Dict[Tuple[int, int], Deque[float]] = {}

# Raid: chat_id -> deque[join_ts]
JOINS: Dict[int, Deque[float]] = {}


# ----------------- BOT ROUTER -----------------

router = Router()

# ---- YARDIM / BİLGİ ----

@router.message(CMD("help", "yardim", "komutlar"))
async def help_cmd(message: Message):
    await message.reply(
        "<b>Komutlar</b> ( / veya . )\n\n"
        "<b>Genel</b>\n"
        "• .rules\n"
        "• .get <not>\n"
        "• .notes\n\n"
        "<b>Ayar</b>\n"
        "• .setrules <metin>\n"
        "• .setwelcome <metin>\n"
        "• .antilink on|off\n"
        "• .captcha on|off\n"
        "• .setcaptchatimeout 120\n"
        "• .adminonly on|off\n"
        "• .setwarnlimit 3\n"
        "• .setmutetime 1h\n\n"
        "<b>Koruma</b>\n"
        "• .antiflood on|off\n"
        "• .setflood <max> <sec> <warn|tmute> [tmute_saniye]\n"
        "• .antiforward on|off\n"
        "• .antiservice on|off\n"
        "• .anticaps on|off [oran]\n"
        "• .antiemoji on|off [max]\n"
        "• .antimention on|off [max]\n"
        "• .antiraid on|off\n"
        "• .setraid <threshold> <window_sec> <lock_sec>\n\n"
        "<b>Lock Types</b>\n"
        "• .lock <links|media|photos|videos|documents|stickers|gifs|voice|audio|all>\n"
        "• .unlock <type>\n"
        "• .locks\n\n"
        "<b>Moderasyon</b>\n"
        "• .ban / .tban 2d\n"
        "• .unban\n"
        "• .kick\n"
        "• .softban\n"
        "• .mute / .tmute 10m\n"
        "• .unmute\n"
        "• .warn [sebep]\n"
        "• .unwarn\n"
        "• .warnings\n"
        "• .warnslist\n"
        "• .resetwarns\n"
        "• .del (reply)\n"
        "• .purge [limit] (reply)\n\n"
        "<b>Notlar / Filtreler</b>\n"
        "• .save <ad> <içerik>\n"
        "• .delnote <ad>\n"
        "• .filter <kelime> <cevap>\n"
        "• .stop <kelime>\n"
        "• .filters\n\n"
        "<b>Admin</b>\n"
        "• .promote [reply/@user/id] [title]\n"
        "• .demote [reply/@user/id]\n"
        "• .admins\n\n"
        "<i>Not: @username ile işlem, bot kullanıcıyı daha önce görmüşse çalışır.</i>"
    )

@router.message(CMD("rules"))
async def rules_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    cs = chat_state(message.chat.id, config)
    rules = cs["settings"].get("rules")
    await message.reply(rules or "Bu grupta henüz kural ayarlı değil.")

@router.message(CMD("notes"))
async def notes_list_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    cs = chat_state(message.chat.id, config)
    names = sorted(cs.get("notes", {}).keys())
    if not names:
        return await message.reply("Not yok.")
    await message.reply("Notlar:\n" + "\n".join(f"• <code>{n}</code>" for n in names))

@router.message(CMD("get"))
async def get_note_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .get <not_adı>")
    name = parts[1].strip().lower()
    cs = chat_state(message.chat.id, config)
    note = cs.get("notes", {}).get(name)
    if not note:
        return await message.reply("Not bulunamadı.")
    await message.reply(note)

# ---- ADMIN AYARLARI ----

@router.message(CMD("setrules"))
async def setrules_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .setrules <metin>")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["rules"] = parts[1]
    schedule_save(config)
    await message.reply("Kurallar kaydedildi (kalıcı).")

@router.message(CMD("setwelcome"))
async def setwelcome_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .setwelcome Hoşgeldin {first_name}")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["welcome"] = parts[1]
    schedule_save(config)
    await message.reply("Welcome kaydedildi (kalıcı).")

@router.message(CMD("antilink"))
async def antilink_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antilink on | off")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["antilink"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Anti-link: {'AÇIK' if cs['settings']['antilink'] else 'KAPALI'}")

@router.message(CMD("antiforward"))
async def antiforward_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antiforward on | off")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["antiforward"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Anti-forward: {'AÇIK' if cs['settings']['antiforward'] else 'KAPALI'}")

@router.message(CMD("antiservice"))
async def antiservice_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antiservice on | off")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["antiservice"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Anti-service: {'AÇIK' if cs['settings']['antiservice'] else 'KAPALI'}")

@router.message(CMD("adminonly"))
async def adminonly_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Yetki yok: can_delete_messages")

    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .adminonly on | off")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["adminonly"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Admin-only: {'AÇIK' if cs['settings']['adminonly'] else 'KAPALI'}")

@router.message(CMD("captcha"))
async def captcha_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")

    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .captcha on | off")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["captcha"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Captcha: {'AÇIK' if cs['settings']['captcha'] else 'KAPALI'}")

@router.message(CMD("setcaptchatimeout"))
async def setcaptchatimeout_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")

    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.reply("Kullanım: .setcaptchatimeout 120")

    sec = max(10, min(3600, int(parts[1])))
    cs = chat_state(message.chat.id, config)
    cs["settings"]["captcha_timeout_sec"] = sec
    schedule_save(config)
    await message.reply(f"Captcha timeout: {sec} saniye")

@router.message(CMD("setwarnlimit"))
async def setwarnlimit_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")

    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.reply("Kullanım: .setwarnlimit 3")

    limit = max(1, min(20, int(parts[1])))
    cs = chat_state(message.chat.id, config)
    cs["settings"]["warn_limit"] = limit
    schedule_save(config)
    await message.reply(f"Uyarı limiti: {limit}")

@router.message(CMD("setmutetime"))
async def setmutetime_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")

    parts = (message.text or "").split()
    if len(parts) != 2:
        return await message.reply("Kullanım: .setmutetime 1h  (örn: 30m, 2h, 1d)")

    sec = parse_duration_to_seconds(parts[1])
    if sec is None or sec == 0:
        return await message.reply("Geçersiz süre. Örn: 30m, 2h, 1d")
    cs = chat_state(message.chat.id, config)
    cs["settings"]["mute_seconds"] = sec
    schedule_save(config)
    await message.reply(f"Varsayılan mute süresi: {parts[1]} ({sec}s)")

# ---- AntiFlood / AntiRaid / AntiCaps / AntiEmoji / AntiMention ----

@router.message(CMD("antiflood"))
async def antiflood_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antiflood on | off")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["antiflood"]["enabled"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Anti-flood: {'AÇIK' if cs['settings']['antiflood']['enabled'] else 'KAPALI'}")

@router.message(CMD("setflood"))
async def setflood_cmd(message: Message, bot: Bot, config: Config):
    """
    .setflood <max> <sec> <warn|tmute> [tmute_saniye]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split()
    if len(parts) < 4:
        return await message.reply("Kullanım: .setflood 6 5 tmute 300  (veya warn)")

    if not parts[1].isdigit() or not parts[2].isdigit():
        return await message.reply("Hatalı sayı. Örn: .setflood 6 5 tmute 300")

    max_msgs = max(2, min(30, int(parts[1])))
    per_sec = max(2, min(30, int(parts[2])))
    action = parts[3].lower()
    if action not in ("warn", "tmute"):
        return await message.reply("Action sadece: warn veya tmute")

    mute_sec = 300
    if action == "tmute":
        if len(parts) >= 5 and parts[4].isdigit():
            mute_sec = max(10, min(86400, int(parts[4])))

    cs = chat_state(message.chat.id, config)
    cs["settings"]["antiflood"].update({
        "max_msgs": max_msgs,
        "per_sec": per_sec,
        "action": action,
        "mute_sec": mute_sec
    })
    schedule_save(config)
    await message.reply(f"Flood ayarı: {max_msgs} mesaj / {per_sec}s, action={action}, tmute={mute_sec}s")

@router.message(CMD("antiraid"))
async def antiraid_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antiraid on | off")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["antiraid"]["enabled"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Anti-raid: {'AÇIK' if cs['settings']['antiraid']['enabled'] else 'KAPALI'}")

@router.message(CMD("setraid"))
async def setraid_cmd(message: Message, bot: Bot, config: Config):
    """
    .setraid <threshold> <window_sec> <lock_sec>
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split()
    if len(parts) != 4 or not all(p.isdigit() for p in parts[1:]):
        return await message.reply("Kullanım: .setraid 8 60 120")

    threshold = max(3, min(50, int(parts[1])))
    window_sec = max(10, min(600, int(parts[2])))
    lock_sec = max(10, min(3600, int(parts[3])))

    cs = chat_state(message.chat.id, config)
    cs["settings"]["antiraid"].update({
        "threshold": threshold,
        "window_sec": window_sec,
        "lock_sec": lock_sec
    })
    schedule_save(config)
    await message.reply(f"Raid ayarı: threshold={threshold}, window={window_sec}s, lock={lock_sec}s")

@router.message(CMD("anticaps"))
async def anticaps_cmd(message: Message, bot: Bot, config: Config):
    """
    .anticaps on|off [ratio]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .anticaps on|off 0.7")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["anticaps"]["enabled"] = (parts[1].lower() == "on")
    if len(parts) >= 3:
        try:
            ratio = float(parts[2])
            ratio = max(0.4, min(0.95, ratio))
            cs["settings"]["anticaps"]["ratio"] = ratio
        except:
            pass
    schedule_save(config)
    await message.reply(f"Anti-caps: {'AÇIK' if cs['settings']['anticaps']['enabled'] else 'KAPALI'} (ratio={cs['settings']['anticaps']['ratio']})")

@router.message(CMD("antiemoji"))
async def antiemoji_cmd(message: Message, bot: Bot, config: Config):
    """
    .antiemoji on|off [max]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antiemoji on|off 12")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["antiemoji"]["enabled"] = (parts[1].lower() == "on")
    if len(parts) >= 3 and parts[2].isdigit():
        cs["settings"]["antiemoji"]["max"] = max(3, min(100, int(parts[2])))
    schedule_save(config)
    await message.reply(f"Anti-emoji: {'AÇIK' if cs['settings']['antiemoji']['enabled'] else 'KAPALI'} (max={cs['settings']['antiemoji']['max']})")

@router.message(CMD("antimention"))
async def antimention_cmd(message: Message, bot: Bot, config: Config):
    """
    .antimention on|off [max]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antimention on|off 6")

    cs = chat_state(message.chat.id, config)
    cs["settings"]["antimention"]["enabled"] = (parts[1].lower() == "on")
    if len(parts) >= 3 and parts[2].isdigit():
        cs["settings"]["antimention"]["max"] = max(1, min(50, int(parts[2])))
    schedule_save(config)
    await message.reply(f"Anti-mention: {'AÇIK' if cs['settings']['antimention']['enabled'] else 'KAPALI'} (max={cs['settings']['antimention']['max']})")


# ---- LOCK TYPES ----

@router.message(CMD("lock"))
async def lock_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Yetki yok: can_delete_messages")

    parts = (message.text or "").split()
    if len(parts) != 2:
        return await message.reply("Kullanım: .lock links|media|photos|videos|documents|stickers|gifs|voice|audio|all")

    t = parts[1].lower()
    if t not in LOCK_TYPES:
        return await message.reply("Geçersiz type.")

    cs = chat_state(message.chat.id, config)
    locks = set(cs["settings"].get("locks") or [])
    if t == "all":
        locks.update({"links", "media", "photos", "videos", "documents", "stickers", "gifs", "voice", "audio"})
    elif t == "media":
        locks.update({"photos", "videos", "documents", "stickers", "gifs", "voice", "audio"})
    else:
        locks.add(t)

    cs["settings"]["locks"] = sorted(list(locks))
    schedule_save(config)
    await message.reply("Lock aktif: " + ", ".join(cs["settings"]["locks"]))

@router.message(CMD("unlock"))
async def unlock_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Yetki yok: can_delete_messages")

    parts = (message.text or "").split()
    if len(parts) != 2:
        return await message.reply("Kullanım: .unlock links|media|photos|videos|documents|stickers|gifs|voice|audio|all")

    t = parts[1].lower()
    if t not in LOCK_TYPES:
        return await message.reply("Geçersiz type.")

    cs = chat_state(message.chat.id, config)
    locks = set(cs["settings"].get("locks") or [])

    if t == "all":
        locks.clear()
    elif t == "media":
        locks.difference_update({"photos", "videos", "documents", "stickers", "gifs", "voice", "audio"})
    else:
        locks.discard(t)

    cs["settings"]["locks"] = sorted(list(locks))
    schedule_save(config)
    await message.reply("Lock aktif: " + (", ".join(cs["settings"]["locks"]) if cs["settings"]["locks"] else "YOK"))

@router.message(CMD("locks"))
async def locks_list_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    cs = chat_state(message.chat.id, config)
    locks = cs["settings"].get("locks") or []
    await message.reply("Lock aktif: " + (", ".join(locks) if locks else "YOK"))


# ----------------- NOTLAR / FİLTRELER -----------------

@router.message(CMD("save"))
async def save_note_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply("Kullanım: .save <ad> <içerik>")

    name = parts[1].strip().lower()
    content = parts[2].strip()

    cs = chat_state(message.chat.id, config)
    cs.setdefault("notes", {})[name] = content
    schedule_save(config)
    await message.reply(f"Not kaydedildi: <code>{name}</code>")

@router.message(CMD("delnote"))
async def delnote_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .delnote <ad>")

    name = parts[1].strip().lower()
    cs = chat_state(message.chat.id, config)
    if name in cs.get("notes", {}):
        del cs["notes"][name]
        schedule_save(config)
        return await message.reply("Silindi.")
    await message.reply("Not bulunamadı.")

@router.message(CMD("filter"))
async def add_filter_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply("Kullanım: .filter <kelime> <cevap>")

    key = parts[1].strip().lower()
    resp = parts[2].strip()

    cs = chat_state(message.chat.id, config)
    cs.setdefault("filters", {})[key] = resp
    schedule_save(config)
    await message.reply(f"Filter kaydedildi: <code>{key}</code>")

@router.message(CMD("stop"))
async def del_filter_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .stop <kelime>")

    key = parts[1].strip().lower()
    cs = chat_state(message.chat.id, config)
    if key in cs.get("filters", {}):
        del cs["filters"][key]
        schedule_save(config)
        return await message.reply("Filter silindi.")
    await message.reply("Filter yok.")

@router.message(CMD("filters"))
async def filters_list_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    cs = chat_state(message.chat.id, config)
    keys = sorted(cs.get("filters", {}).keys())
    if not keys:
        return await message.reply("Filter yok.")
    await message.reply("Filterler:\n" + "\n".join(f"• <code>{k}</code>" for k in keys))


# ----------------- MODERASYON -----------------

async def add_warn(chat_id: int, user_id: int, by_id: int, reason: str, config: Config) -> int:
    cs = chat_state(chat_id, config)
    w = cs.setdefault("warns", {})
    u = w.get(str(user_id)) or {"count": 0, "reasons": []}
    u["count"] = int(u.get("count", 0)) + 1
    u["reasons"] = (u.get("reasons") or [])[-9:]  # son 10 kayıt
    u["reasons"].append({
        "by": by_id,
        "at": utcnow().isoformat() + "Z",
        "reason": reason or "-"
    })
    w[str(user_id)] = u
    schedule_save(config)
    return u["count"]

async def remove_warn(chat_id: int, user_id: int, config: Config) -> int:
    cs = chat_state(chat_id, config)
    w = cs.setdefault("warns", {})
    u = w.get(str(user_id))
    if not u:
        return 0
    u["count"] = max(0, int(u.get("count", 0)) - 1)
    w[str(user_id)] = u
    schedule_save(config)
    return u["count"]

def get_warn(chat_id: int, user_id: int, config: Config) -> Dict[str, Any]:
    cs = chat_state(chat_id, config)
    return (cs.get("warns", {}) or {}).get(str(user_id)) or {"count": 0, "reasons": []}

@router.message(CMD("warn"))
async def warn_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return

    # sebep
    txt = (message.text or "")
    parts = txt.split(maxsplit=2)
    reason = parts[2] if len(parts) >= 3 else ""

    count = await add_warn(message.chat.id, tid, message.from_user.id, reason, config)
    cs = chat_state(message.chat.id, config)
    limit = int(cs["settings"].get("warn_limit") or config.default_warn_limit)

    if count >= limit:
        # limit aşınca otomatik tmute (varsayılan mute_seconds)
        if await require_bot_right(bot, message.chat.id, "can_restrict_members"):
            mute_sec = int(cs["settings"].get("mute_seconds") or config.default_mute_seconds)
            until = datetime.utcnow() + timedelta(seconds=mute_sec)
            try:
                await bot.restrict_chat_member(message.chat.id, tid, permissions=muted_permissions(), until_date=until)
            except:
                pass
            await message.reply(f"Uyarı: <code>{tid}</code> ({count}/{limit}) -> limit aşıldı, mute atıldı.")
        else:
            await message.reply(f"Uyarı: <code>{tid}</code> ({count}/{limit}) -> limit aşıldı (bot mute yetkisi yok).")
    else:
        await message.reply(f"Uyarı verildi: <code>{tid}</code> ({count}/{limit})")

@router.message(CMD("unwarn"))
async def unwarn_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return

    new_count = await remove_warn(message.chat.id, tid, config)
    await message.reply(f"Uyarı düşürüldü: <code>{tid}</code> (şimdi: {new_count})")

@router.message(CMD("warnings"))
async def warnings_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    tid = message.reply_to_message.from_user.id if (message.reply_to_message and message.reply_to_message.from_user) else (message.from_user.id if message.from_user else None)
    if not tid:
        return
    w = get_warn(message.chat.id, tid, config)
    await message.reply(f"<code>{tid}</code> uyarı sayısı: {w['count']}")

@router.message(CMD("warnslist"))
async def warnslist_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    # admin olan herkes bakabilsin (Rose gibi)
    if message.from_user and not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    tid = await need_target(message) if (message.text or "").split(maxsplit=1).__len__() > 1 or message.reply_to_message else None
    if not tid:
        # hedef yoksa reply değilse, kendi
        tid = message.from_user.id if message.from_user else None
    if not tid:
        return

    w = get_warn(message.chat.id, tid, config)
    reasons = w.get("reasons") or []
    if not reasons:
        return await message.reply("Kayıtlı uyarı sebebi yok.")

    lines = [f"<b>{tid}</b> uyarı detayları (son {len(reasons)}):"]
    for i, r in enumerate(reasons[-10:], 1):
        lines.append(f"{i}) by <code>{r.get('by')}</code> | {r.get('at')} | {r.get('reason')}")
    await message.reply("\n".join(lines))

@router.message(CMD("resetwarns"))
async def resetwarns_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return
    cs = chat_state(message.chat.id, config)
    cs.setdefault("warns", {}).pop(str(tid), None)
    schedule_save(config)
    await message.reply(f"Uyarılar sıfırlandı: <code>{tid}</code>")

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
    try:
        await bot.ban_chat_member(message.chat.id, tid)
        await message.reply(f"Banlandı: <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("tban"))
async def tban_cmd(message: Message, bot: Bot, config: Config):
    # .tban @user 2d [sebep]  veya reply .tban 2d
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
            return await message.reply("Kullanım: .tban @username 2d [sebep]")
        dur_str = parts[2]

    sec = parse_duration_to_seconds(dur_str)
    if sec is None:
        return await message.reply("Süre formatı: 10m, 2h, 1d, 1w, 1h30m (perm: perm)")

    tid = await need_target(message)
    if not tid:
        return

    until = None if sec == 0 else (datetime.utcnow() + timedelta(seconds=sec))
    try:
        await bot.ban_chat_member(message.chat.id, tid, until_date=until)
        await message.reply(f"Süreli ban: <code>{tid}</code> süre={dur_str}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("unban"))
async def unban_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return
    try:
        await bot.unban_chat_member(message.chat.id, tid, only_if_banned=True)
        await message.reply(f"Unban: <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("kick"))
async def kick_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return
    try:
        await bot.ban_chat_member(message.chat.id, tid)
        await bot.unban_chat_member(message.chat.id, tid, only_if_banned=True)
        await message.reply(f"Atıldı: <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("softban"))
async def softban_cmd(message: Message, bot: Bot):
    # ban + unban (kullanıcıyı atar)
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return
    try:
        await bot.ban_chat_member(message.chat.id, tid)
        await bot.unban_chat_member(message.chat.id, tid, only_if_banned=True)
        await message.reply(f"Softban: <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

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

    cs = chat_state(message.chat.id, config)
    mute_sec = int(cs["settings"].get("mute_seconds") or config.default_mute_seconds)
    until = datetime.utcnow() + timedelta(seconds=mute_sec)

    try:
        await bot.restrict_chat_member(message.chat.id, tid, permissions=muted_permissions(), until_date=until)
        await message.reply(f"Mute: <code>{tid}</code> ({mute_sec}s)")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("tmute"))
async def tmute_cmd(message: Message, bot: Bot):
    # .tmute @user 10m [sebep]  veya reply .tmute 10m
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
            return await message.reply("Kullanım: .tmute @username 10m [sebep]")
        dur_str = parts[2]

    sec = parse_duration_to_seconds(dur_str)
    if sec is None or sec == 0:
        return await message.reply("Süre formatı: 10m, 2h, 1d, 1w, 1h30m (perm olmaz)")

    tid = await need_target(message)
    if not tid:
        return

    until = datetime.utcnow() + timedelta(seconds=sec)
    try:
        await bot.restrict_chat_member(message.chat.id, tid, permissions=muted_permissions(), until_date=until)
        await message.reply(f"Süreli mute: <code>{tid}</code> süre={dur_str}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("unmute"))
async def unmute_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    tid = await need_target(message)
    if not tid:
        return
    try:
        await bot.restrict_chat_member(message.chat.id, tid, permissions=open_permissions())
        await message.reply(f"Unmute: <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("del"))
async def del_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.reply_to_message:
        return await message.reply("Silmek için bir mesaja reply yap.")
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Yetki yok: can_delete_messages")
    if not await require_bot_right(bot, message.chat.id, "can_delete_messages"):
        return await message.reply("Bot yetkisi yok: can_delete_messages")

    try:
        await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
    except:
        pass
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except:
        pass

@router.message(CMD("purge"))
async def purge_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.reply_to_message:
        return await message.reply("Purge için bir mesaja reply yap.")
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Yetki yok: can_delete_messages")
    if not await require_bot_right(bot, message.chat.id, "can_delete_messages"):
        return await message.reply("Bot yetkisi yok: can_delete_messages")

    # opsiyonel limit: .purge 200
    parts = (message.text or "").split()
    limit = 200
    if len(parts) >= 2 and parts[1].isdigit():
        limit = max(1, min(1000, int(parts[1])))

    start_id = message.reply_to_message.message_id
    end_id = message.message_id
    deleted = 0

    for mid in range(start_id, end_id + 1):
        try:
            await bot.delete_message(message.chat.id, mid)
            deleted += 1
        except:
            pass
        if deleted >= limit:
            break
        if deleted % 25 == 0:
            await asyncio.sleep(0.4)

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

    try:
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
    except Exception as e:
        await message.reply(f"Hata: {e}")

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

    try:
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
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("admins"))
async def admins_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private":
        return
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        lines = ["Adminler:"]
        for a in admins:
            u = a.user
            tag = f"@{u.username}" if u.username else u.full_name
            lines.append(f"• {tag} (<code>{u.id}</code>)")
        await message.reply("\n".join(lines))
    except Exception as e:
        await message.reply(f"Hata: {e}")


# ----------------- JOIN: WELCOME + CAPTCHA + RAID -----------------

@router.chat_member()
async def on_join(event: ChatMemberUpdated, bot: Bot, config: Config):
    if not event.chat:
        return
    old_status = getattr(event.old_chat_member, "status", None)
    new_status = getattr(event.new_chat_member, "status", None)
    if not (old_status in ("left", "kicked") and new_status == "member"):
        return

    cs = chat_state(event.chat.id, config)
    settings = cs["settings"]
    user = event.new_chat_member.user

    # username cache
    if user.username:
        cache = STATE.setdefault("username_cache", {})
        key = user.username.lower()
        if cache.get(key) != user.id:
            cache[key] = user.id
            schedule_save(config)

    # anti-raid tracker
    if settings.get("antiraid", {}).get("enabled"):
        q = JOINS.setdefault(event.chat.id, deque())
        now = asyncio.get_event_loop().time()
        q.append(now)
        window = int(settings["antiraid"].get("window_sec", 60))
        while q and (now - q[0] > window):
            q.popleft()

        threshold = int(settings["antiraid"].get("threshold", 8))
        if len(q) >= threshold:
            # lockdown: group lock (send_messages false)
            lock_sec = int(settings["antiraid"].get("lock_sec", 120))
            if await require_bot_right(bot, event.chat.id, "can_restrict_members"):
                try:
                    await bot.set_chat_permissions(event.chat.id, permissions=muted_permissions())
                    cs["raid"]["lockdown_until"] = (utcnow() + timedelta(seconds=lock_sec)).isoformat() + "Z"
                    schedule_save(config)
                    # grupte bilgilendir
                    try:
                        await bot.send_message(event.chat.id, f"Anti-raid: Grup {lock_sec}s kilitlendi.")
                    except:
                        pass
                except:
                    pass

    # captcha
    if settings.get("captcha") and await require_bot_right(bot, event.chat.id, "can_restrict_members"):
        try:
            await bot.restrict_chat_member(event.chat.id, user.id, permissions=muted_permissions())
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Doğrula", callback_data=f"verify:{user.id}")
            ]])
            msg = await bot.send_message(event.chat.id, f"{user.first_name} doğrulama gerekli. Butona bas.", reply_markup=kb)
            timeout = int(settings.get("captcha_timeout_sec") or 120)
            cs.setdefault("captcha_pending", {})[str(user.id)] = {
                "expires_at": (utcnow() + timedelta(seconds=timeout)).isoformat() + "Z",
                "message_id": msg.message_id
            }
            schedule_save(config)
        except:
            pass

    # welcome
    welcome = settings.get("welcome")
    if welcome:
        text = str(welcome).replace("{first_name}", user.first_name or "")
        text = text.replace("{username}", f"@{user.username}" if user.username else (user.first_name or ""))
        try:
            await bot.send_message(event.chat.id, text)
        except:
            pass

@router.callback_query()
async def verify_callback(call: CallbackQuery, bot: Bot, config: Config):
    if not call.data or not call.message or not call.from_user:
        return
    if not call.data.startswith("verify:"):
        return

    chat_id = call.message.chat.id
    target_id = int(call.data.split(":", 1)[1])

    if call.from_user.id != target_id:
        return await call.answer("Bu buton sana ait değil.", show_alert=True)

    cs = chat_state(chat_id, config)
    if not cs["settings"].get("captcha"):
        return await call.answer("Captcha kapalı.", show_alert=True)

    if not await require_bot_right(bot, chat_id, "can_restrict_members"):
        return await call.answer("Botun yetkisi yok.", show_alert=True)

    try:
        await bot.restrict_chat_member(chat_id, target_id, permissions=open_permissions())
        # pending temizle
        cs.get("captcha_pending", {}).pop(str(target_id), None)
        schedule_save(config)
        try:
            await call.message.delete()
        except:
            pass
        await call.answer("Doğrulandı!")
    except:
        await call.answer("Açılamadı.", show_alert=True)


# ----------------- ARKA PLAN GÖREVLERİ -----------------

async def background_tasks(bot: Bot, config: Config):
    """
    - captcha timeout -> kick
    - anti-raid lockdown süresi dolunca unlock
    """
    while True:
        await asyncio.sleep(5)

        # tüm chatlerde dolaş (state küçük olmalı)
        chats = list((STATE.get("chats") or {}).items())
        now = utcnow()

        for chat_id_str, cs in chats:
            try:
                chat_id = int(chat_id_str)
            except:
                continue

            # lockdown unlock
            until_iso = (cs.get("raid") or {}).get("lockdown_until")
            if until_iso:
                try:
                    until = datetime.fromisoformat(until_iso.replace("Z", ""))
                    if now >= until:
                        # unlock
                        if await require_bot_right(bot, chat_id, "can_restrict_members"):
                            try:
                                await bot.set_chat_permissions(chat_id, permissions=open_permissions())
                            except:
                                pass
                        cs["raid"]["lockdown_until"] = None
                        schedule_save(config)
                except:
                    # bozuk tarih
                    cs["raid"]["lockdown_until"] = None
                    schedule_save(config)

            # captcha timeout
            pending = (cs.get("captcha_pending") or {})
            if not pending:
                continue

            to_kick = []
            for uid_str, info in pending.items():
                exp = info.get("expires_at")
                if not exp:
                    continue
                try:
                    exp_dt = datetime.fromisoformat(exp.replace("Z", ""))
                    if now >= exp_dt:
                        to_kick.append(int(uid_str))
                except:
                    to_kick.append(int(uid_str))

            if to_kick:
                if await require_bot_right(bot, chat_id, "can_restrict_members"):
                    for uid in to_kick:
                        try:
                            # kick (ban+unban)
                            await bot.ban_chat_member(chat_id, uid)
                            await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
                        except:
                            pass
                        pending.pop(str(uid), None)
                    schedule_save(config)


# ----------------- OTOMATİK KORUMA / FİLTRELER -----------------

def count_mentions(message: Message) -> int:
    c = 0
    if not message.entities:
        return 0
    for e in message.entities:
        if e.type in ("mention", "text_mention"):
            c += 1
    return c

def count_emojis(text: str) -> int:
    # basit emoji sayacı (unicode aralığı)
    # stabil ve hızlı olsun diye basit tuttuk.
    n = 0
    for ch in text:
        o = ord(ch)
        if (
            0x1F300 <= o <= 0x1FAFF or
            0x2600 <= o <= 0x26FF or
            0x2700 <= o <= 0x27BF
        ):
            n += 1
    return n

def caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    upper = sum(1 for c in letters if c.isupper())
    return upper / max(1, len(letters))

async def try_delete(message: Message, bot: Bot) -> bool:
    try:
        if await require_bot_right(bot, message.chat.id, "can_delete_messages"):
            await message.delete()
            return True
    except:
        pass
    return False

async def punish_warn_or_tmute(
    message: Message,
    bot: Bot,
    config: Config,
    reason: str,
    action: str,
    tmute_sec: int = 300
):
    """
    action: "warn" | "tmute"
    """
    if not message.chat or not message.from_user:
        return

    # admin dokunma
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return

    if action == "warn":
        await add_warn(message.chat.id, message.from_user.id, by_id=0, reason=reason, config=config)  # by=0 -> sistem
        # uyarı limiti kontrolü
        cs = chat_state(message.chat.id, config)
        limit = int(cs["settings"].get("warn_limit") or config.default_warn_limit)
        w = get_warn(message.chat.id, message.from_user.id, config)
        if w["count"] >= limit and await require_bot_right(bot, message.chat.id, "can_restrict_members"):
            mute_sec = int(cs["settings"].get("mute_seconds") or config.default_mute_seconds)
            until = datetime.utcnow() + timedelta(seconds=mute_sec)
            try:
                await bot.restrict_chat_member(message.chat.id, message.from_user.id, permissions=muted_permissions(), until_date=until)
            except:
                pass
    else:
        if await require_bot_right(bot, message.chat.id, "can_restrict_members"):
            until = datetime.utcnow() + timedelta(seconds=tmute_sec)
            try:
                await bot.restrict_chat_member(message.chat.id, message.from_user.id, permissions=muted_permissions(), until_date=until)
            except:
                pass

@router.message()
async def auto_guard_handler(message: Message, bot: Bot, config: Config):
    """
    Sıra:
    - service mesaj silme
    - adminonly
    - lock types
    - anti-forward
    - anti-link
    - anti-caps / anti-emoji / anti-mention
    - anti-flood
    - filters
    """
    if not message.chat or message.chat.type == "private":
        return

    cs = chat_state(message.chat.id, config)
    s = cs["settings"]

    # 0) service mesajlar
    if s.get("antiservice"):
        is_service = bool(
            getattr(message, "new_chat_members", None) or
            getattr(message, "left_chat_member", None) or
            getattr(message, "new_chat_title", None) or
            getattr(message, "new_chat_photo", None) or
            getattr(message, "delete_chat_photo", None) or
            getattr(message, "group_chat_created", None) or
            getattr(message, "supergroup_chat_created", None) or
            getattr(message, "message_auto_delete_timer_changed", None)
        )
        if is_service:
            await try_delete(message, bot)
            return

    # komutları elleme (/ veya .)
    if message.text and (message.text.startswith("/") or message.text.startswith(".")):
        return

    if not message.from_user:
        return

    # adminonly (admin değilse mesaj sil)
    if s.get("adminonly") and not await is_admin(bot, message.chat.id, message.from_user.id):
        await try_delete(message, bot)
        return

    # Lock types (admin değilse)
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        locks = set(s.get("locks") or [])

        # all/media zaten lock komutunda genişletiliyor ama yine de kontrol edelim
        if "links" in locks and message.text and URL_RE.search(message.text):
            await try_delete(message, bot)
            return

        if "photos" in locks and message.photo:
            await try_delete(message, bot)
            return
        if "videos" in locks and (message.video or message.video_note):
            await try_delete(message, bot)
            return
        if "documents" in locks and message.document:
            await try_delete(message, bot)
            return
        if "stickers" in locks and message.sticker:
            await try_delete(message, bot)
            return
        if "gifs" in locks and message.animation:
            await try_delete(message, bot)
            return
        if "voice" in locks and message.voice:
            await try_delete(message, bot)
            return
        if "audio" in locks and (message.audio or message.voice):
            await try_delete(message, bot)
            return

    # anti-forward
    if s.get("antiforward") and not await is_admin(bot, message.chat.id, message.from_user.id):
        is_forward = bool(
            getattr(message, "forward_origin", None) or
            getattr(message, "forward_date", None) or
            getattr(message, "forward_from_chat", None)
        )
        if is_forward:
            await try_delete(message, bot)
            await punish_warn_or_tmute(message, bot, config, "forward", action="warn")
            return

    # anti-link
    if s.get("antilink") and message.text and URL_RE.search(message.text) and not await is_admin(bot, message.chat.id, message.from_user.id):
        await try_delete(message, bot)
        await punish_warn_or_tmute(message, bot, config, "link", action="warn")
        return

    # anti-caps
    if s.get("anticaps", {}).get("enabled") and message.text and not await is_admin(bot, message.chat.id, message.from_user.id):
        min_len = int(s["anticaps"].get("min_len", 12))
        if len(message.text) >= min_len:
            ratio = float(s["anticaps"].get("ratio", 0.7))
            if caps_ratio(message.text) >= ratio:
                await try_delete(message, bot)
                await punish_warn_or_tmute(message, bot, config, "caps", action=s["anticaps"].get("action", "warn"))
                return

    # anti-emoji
    if s.get("antiemoji", {}).get("enabled") and message.text and not await is_admin(bot, message.chat.id, message.from_user.id):
        mx = int(s["antiemoji"].get("max", 12))
        if count_emojis(message.text) > mx:
            await try_delete(message, bot)
            await punish_warn_or_tmute(message, bot, config, "emoji", action=s["antiemoji"].get("action", "warn"))
            return

    # anti-mention
    if s.get("antimention", {}).get("enabled") and message.text and not await is_admin(bot, message.chat.id, message.from_user.id):
        mx = int(s["antimention"].get("max", 6))
        if count_mentions(message) > mx:
            await try_delete(message, bot)
            await punish_warn_or_tmute(message, bot, config, "mention", action=s["antimention"].get("action", "warn"))
            return

    # anti-flood
    flood = s.get("antiflood") or {}
    if flood.get("enabled") and not await is_admin(bot, message.chat.id, message.from_user.id):
        key = (message.chat.id, message.from_user.id)
        q = FLOOD.setdefault(key, deque())
        now = asyncio.get_event_loop().time()
        q.append(now)

        per_sec = int(flood.get("per_sec", 5))
        max_msgs = int(flood.get("max_msgs", 6))
        while q and (now - q[0] > per_sec):
            q.popleft()

        if len(q) >= max_msgs:
            # flood olunca
            action = str(flood.get("action", "tmute"))
            if action == "warn":
                await punish_warn_or_tmute(message, bot, config, "flood", action="warn")
            else:
                tmute_sec = int(flood.get("mute_sec", 300))
                await punish_warn_or_tmute(message, bot, config, "flood", action="tmute", tmute_sec=tmute_sec)
            # mesajı silmeye çalış
            await try_delete(message, bot)
            # kuyruğu biraz boşalt
            q.clear()
            return

    # filters (komut değilse)
    if message.text:
        filters = cs.get("filters") or {}
        txt = message.text.lower()
        for k, resp in filters.items():
            if k and k in txt:
                try:
                    await message.reply(resp)
                except:
                    pass
                break


# ----------------- MAIN -----------------

async def main():
    logging.basicConfig(level=logging.INFO)

    config = load_config()
    async with STATE_LOCK:
        await load_state(config.state_path)

    bot = Bot(
        token=config.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(InjectConfigMiddleware(config))
    dp.update.middleware(UsernameCacheMiddleware())

    dp.include_router(router)

    # background task
    asyncio.create_task(background_tasks(bot, config))

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
