# bot.py (TAMAMEN TÜRKÇE CEVAPLI) - TEK DOSYA
# STABİL - DB YOK - Volume(JSON) ile KALICI - LOG YOK
# MissRose tarzı: yetki kontrolü, / ve . komutları, anti-spam, notlar/filtreler,
# kilit türleri, captcha, uyarı sistemi (sebepli), anti-raid, purge/del, promote/demote vb.
#
# requirements.txt:
#   aiogram==3.6.0
#
# Railway Volume:
#   Mount path: /data
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

KILIT_TURLERI = {"link", "tum", "medya", "foto", "video", "dosya", "sticker", "gif", "ses", "muzik"}

BOT_ID: Optional[int] = None  # runtime cache


def CMD(*names: str) -> Command:
    # /komut ve .komut
    return Command(commands=list(names), prefix=CMD_PREFIXES, ignore_case=True, ignore_mention=True)


def simdi_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sureyi_saniyeye_cevir(s: str) -> Optional[int]:
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

STATE_LOCK = asyncio.Lock()
SAVE_TASK: Optional[asyncio.Task] = None

STATE: Dict[str, Any] = {
    "kullanici_adi_onbellek": {},  # "username" -> user_id
    "sohbetler": {},               # str(chat_id) -> chat_state
    "kaydedildi": None
}


def sohbet_durumu(chat_id: int, config: Config) -> Dict[str, Any]:
    sohbetler = STATE.setdefault("sohbetler", {})
    key = str(chat_id)
    cs = sohbetler.get(key)
    if not cs:
        cs = {
            "ayarlar": {
                "hosgeldin": None,
                "kurallar": None,

                "antilink": False,
                "antiilet": False,
                "antiservis": False,

                "adminmodu": False,

                "captcha": False,
                "captcha_sure_sn": 120,

                "uyari_limiti": config.default_warn_limit,
                "varsayilan_mute_sn": config.default_mute_seconds,

                "antiflood": {
                    "acik": False,
                    "maks_mesaj": 6,
                    "sure_sn": 5,
                    "eylem": "tmute",   # warn veya tmute
                    "tmute_sn": 300,
                },

                "anticaps": {
                    "acik": False,
                    "oran": 0.7,
                    "min_uzunluk": 12,
                    "eylem": "warn"
                },

                "antiemoji": {
                    "acik": False,
                    "maks": 12,
                    "eylem": "warn"
                },

                "antimention": {
                    "acik": False,
                    "maks": 6,
                    "eylem": "warn"
                },

                "antiraid": {
                    "acik": False,
                    "esik": 8,
                    "pencere_sn": 60,
                    "kilit_sn": 120,
                },

                "kilitler": []  # örn: ["link", "foto"]
            },

            "uyarilar": {
                # "user_id": {"sayi": int, "sebepler": [{"kim":id, "tarih":iso, "sebep":text}]}
            },

            "notlar": {},
            "filtreler": {},

            "captcha_bekleyen": {
                # "user_id": {"bitis": iso, "mesaj_id": int}
            },

            "raid": {
                "kilit_bitis": None  # iso
            }
        }
        sohbetler[key] = cs
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
        if isinstance(data, dict) and "kullanici_adi_onbellek" in data and "sohbetler" in data:
            STATE = data
    except Exception:
        pass


async def save_state(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    STATE["kaydedildi"] = simdi_utc().isoformat() + "Z"
    tmp = path + ".tmp"

    def _write():
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    await asyncio.to_thread(_write)


# ----------------- YETKİ / PERMISSIONS -----------------

def sustur_izinleri() -> ChatPermissions:
    return ChatPermissions(can_send_messages=False)


def acik_izinler() -> ChatPermissions:
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


def admin_mi_durum(member) -> bool:
    return getattr(member, "status", None) in ("administrator", "creator")


def yetki_var_mi(member, right: str) -> bool:
    if getattr(member, "status", None) == "creator":
        return True
    if getattr(member, "status", None) != "administrator":
        return False
    return bool(getattr(member, right, False))


async def kullanici_yetki_kontrol(bot: Bot, chat_id: int, user_id: int, right: Optional[str]) -> bool:
    m = await bot.get_chat_member(chat_id, user_id)
    if not admin_mi_durum(m):
        return False
    return True if right is None else yetki_var_mi(m, right)


async def bot_yetki_kontrol(bot: Bot, chat_id: int, right: Optional[str]) -> bool:
    global BOT_ID
    if BOT_ID is None:
        BOT_ID = (await bot.get_me()).id

    m = await bot.get_chat_member(chat_id, BOT_ID)
    if not admin_mi_durum(m):
        return False
    return True if right is None else yetki_var_mi(m, right)


async def admin_mi(bot: Bot, chat_id: int, user_id: int) -> bool:
    m = await bot.get_chat_member(chat_id, user_id)
    return admin_mi_durum(m)


# ----------------- HEDEF (reply / id / @username cache) -----------------

async def kullanici_adindan_id(username: str) -> Optional[int]:
    u = username.strip()
    if u.startswith("@"):
        u = u[1:]
    u = u.lower().strip()
    if not u:
        return None
    v = (STATE.get("kullanici_adi_onbellek") or {}).get(u)
    return int(v) if v else None


async def hedef_coz(message: Message) -> Tuple[Optional[int], str]:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, "yanit"

    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                return ent.user.id, "metin_mention"

    parts = (message.text or "").split()
    if len(parts) < 2:
        return None, "hedef_yok"

    arg = parts[1].strip()
    if arg.lstrip("-").isdigit():
        return int(arg), "id"

    if arg.startswith("@"):
        uid = await kullanici_adindan_id(arg)
        if uid:
            return uid, "kullaniciadi"
        return None, "kullaniciadi_bilinmiyor"

    return None, "gecersiz"


async def hedef_gerekli(message: Message) -> Optional[int]:
    tid, mode = await hedef_coz(message)
    if tid:
        return tid
    if mode == "kullaniciadi_bilinmiyor":
        await message.reply(
            "Bu kullanıcı adının ID'sini bilmiyorum.\n"
            "Kullanıcı grupta yazsın/katılsın (bot görsün), sonra tekrar dene. (Telegram kısıtı)"
        )
        return None
    await message.reply("Hedef yok. Reply yap veya .komut <id> / .komut @kullaniciadi yaz.")
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
            cache = STATE.setdefault("kullanici_adi_onbellek", {})
            key = u.username.lower()
            if cache.get(key) != u.id:
                cache[key] = u.id
                schedule_save(config)
        return await handler(event, data)


# ----------------- RUNTIME TRACKERS (RAM) -----------------
FLOOD: Dict[Tuple[int, int], Deque[float]] = {}
JOINS: Dict[int, Deque[float]] = {}


# ----------------- GUARD HELPERS -----------------

def mention_sayisi(message: Message) -> int:
    c = 0
    if not message.entities:
        return 0
    for e in message.entities:
        if e.type in ("mention", "text_mention"):
            c += 1
    return c


def emoji_sayisi(text: str) -> int:
    n = 0
    for ch in text:
        o = ord(ch)
        if (0x1F300 <= o <= 0x1FAFF) or (0x2600 <= o <= 0x26FF) or (0x2700 <= o <= 0x27BF):
            n += 1
    return n


def buyukharf_orani(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    upper = sum(1 for c in letters if c.isupper())
    return upper / max(1, len(letters))


async def mesaj_sil(message: Message, bot: Bot) -> bool:
    try:
        if await bot_yetki_kontrol(bot, message.chat.id, "can_delete_messages"):
            await message.delete()
            return True
    except:
        pass
    return False


def uyari_getir(chat_id: int, user_id: int, config: Config) -> Dict[str, Any]:
    cs = sohbet_durumu(chat_id, config)
    return (cs.get("uyarilar", {}) or {}).get(str(user_id)) or {"sayi": 0, "sebepler": []}


async def uyari_ekle(chat_id: int, user_id: int, kim: int, sebep: str, config: Config) -> int:
    cs = sohbet_durumu(chat_id, config)
    w = cs.setdefault("uyarilar", {})
    u = w.get(str(user_id)) or {"sayi": 0, "sebepler": []}

    u["sayi"] = int(u.get("sayi", 0)) + 1
    sebepler = (u.get("sebepler") or [])[-9:]  # son 10 kayıt
    sebepler.append({"kim": kim, "tarih": simdi_utc().isoformat() + "Z", "sebep": sebep or "-"})
    u["sebepler"] = sebepler

    w[str(user_id)] = u
    schedule_save(config)
    return u["sayi"]


async def uyari_azalt(chat_id: int, user_id: int, config: Config) -> int:
    cs = sohbet_durumu(chat_id, config)
    w = cs.setdefault("uyarilar", {})
    u = w.get(str(user_id))
    if not u:
        return 0
    u["sayi"] = max(0, int(u.get("sayi", 0)) - 1)
    w[str(user_id)] = u
    schedule_save(config)
    return u["sayi"]


async def cezalandir_uyari_veya_tmute(
    message: Message,
    bot: Bot,
    config: Config,
    sebep: str,
    eylem: str,
    tmute_sn: int = 300
):
    if not message.chat or not message.from_user:
        return

    if await admin_mi(bot, message.chat.id, message.from_user.id):
        return

    if eylem == "warn":
        sayi = await uyari_ekle(message.chat.id, message.from_user.id, kim=0, sebep=sebep, config=config)  # 0=sistem
        cs = sohbet_durumu(message.chat.id, config)
        limit = int(cs["ayarlar"].get("uyari_limiti") or config.default_warn_limit)

        if sayi >= limit and await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
            mute_sn = int(cs["ayarlar"].get("varsayilan_mute_sn") or config.default_mute_seconds)
            until = datetime.utcnow() + timedelta(seconds=mute_sn)
            try:
                await bot.restrict_chat_member(message.chat.id, message.from_user.id, permissions=sustur_izinleri(), until_date=until)
            except:
                pass
    else:
        if await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
            until = datetime.utcnow() + timedelta(seconds=tmute_sn)
            try:
                await bot.restrict_chat_member(message.chat.id, message.from_user.id, permissions=sustur_izinleri(), until_date=until)
            except:
                pass


# ----------------- ROUTER -----------------

router = Router()

# ---- ID KOMUTU ----
@router.message(CMD("id", "kimlik"))
async def id_cmd(message: Message):
    """
    /id veya .id
    - reply varsa hedef ID
    - .id @kullaniciadi (onbellekte varsa)
    - .id 123 (id)
    """
    if not message.chat:
        return

    chat_id = message.chat.id
    benim_id = message.from_user.id if message.from_user else None

    hedef_id = None

    if message.reply_to_message and message.reply_to_message.from_user:
        hedef_id = message.reply_to_message.from_user.id
    else:
        parts = (message.text or "").split()
        if len(parts) >= 2:
            arg = parts[1].strip()
            if arg.lstrip("-").isdigit():
                hedef_id = int(arg)
            elif arg.startswith("@"):
                hedef_id = await kullanici_adindan_id(arg)

    satirlar = [f"Sohbet ID: <code>{chat_id}</code>"]
    if benim_id:
        satirlar.append(f"Senin ID: <code>{benim_id}</code>")
    if hedef_id:
        satirlar.append(f"Hedef ID: <code>{hedef_id}</code>")
    elif (message.text or "").split().__len__() >= 2 and (message.text or "").split()[1].startswith("@"):
        satirlar.append("Hedef ID: Bulunamadı (bot bu kullanıcıyı daha önce görmemiş olabilir).")

    await message.reply("\n".join(satirlar))


# ---- YARDIM / GENEL ----

@router.message(CMD("help", "yardim", "komutlar"))
async def help_cmd(message: Message):
    await message.reply(
        "<b>Komutlar</b> ( / veya . )\n\n"
        "<b>Genel</b>\n"
        "• .id (reply/@kullaniciadi/id)\n"
        "• .rules (kurallar)\n"
        "• .get <not>\n"
        "• .notes\n\n"
        "<b>Ayarlar</b>\n"
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
        "• .setflood <maks> <sn> <warn|tmute> [tmute_sn]\n"
        "• .antiforward on|off\n"
        "• .antiservice on|off\n"
        "• .anticaps on|off [oran]\n"
        "• .antiemoji on|off [maks]\n"
        "• .antimention on|off [maks]\n"
        "• .antiraid on|off\n"
        "• .setraid <esik> <pencere_sn> <kilit_sn>\n\n"
        "<b>Kilit Türleri</b>\n"
        "• .lock link|medya|foto|video|dosya|sticker|gif|ses|muzik|tum\n"
        "• .unlock <tur>\n"
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
        "<b>Yönetici</b>\n"
        "• .promote [reply/@kullaniciadi/id] [başlık]\n"
        "• .demote [reply/@kullaniciadi/id]\n"
        "• .admins\n\n"
        "<i>Not: @kullaniciadi ile işlem, bot kullanıcıyı daha önce görmüşse çalışır.</i>"
    )

# Komut isimleri İngilizce kalsa da cevaplar Türkçe: /rules ve /kurallar
@router.message(CMD("rules", "kurallar"))
async def rules_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    cs = sohbet_durumu(message.chat.id, config)
    rules = cs["ayarlar"].get("kurallar")
    await message.reply(rules or "Bu grupta henüz kural ayarlı değil.")

@router.message(CMD("notes", "notlar"))
async def notes_list_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    cs = sohbet_durumu(message.chat.id, config)
    names = sorted((cs.get("notlar") or {}).keys())
    if not names:
        return await message.reply("Not yok.")
    await message.reply("Notlar:\n" + "\n".join(f"• <code>{n}</code>" for n in names))

@router.message(CMD("get", "not"))
async def get_note_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .get <not_adı>")
    name = parts[1].strip().lower()
    cs = sohbet_durumu(message.chat.id, config)
    note = (cs.get("notlar") or {}).get(name)
    if not note:
        return await message.reply("Not bulunamadı.")
    await message.reply(note)


# ----------------- ADMIN AYARLARI -----------------

@router.message(CMD("setrules", "kurallarayarla"))
async def setrules_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .setrules <metin>")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["kurallar"] = parts[1]
    schedule_save(config)
    await message.reply("Kurallar kaydedildi (kalıcı).")

@router.message(CMD("setwelcome", "hosgeldinayarla"))
async def setwelcome_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .setwelcome Hoşgeldin {first_name}")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["hosgeldin"] = parts[1]
    schedule_save(config)
    await message.reply("Hoşgeldin mesajı kaydedildi (kalıcı).")

@router.message(CMD("antilink"))
async def antilink_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antilink on | off")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["antilink"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Link engeli: {'AÇIK' if cs['ayarlar']['antilink'] else 'KAPALI'}")

@router.message(CMD("antiforward", "antiilet"))
async def antiforward_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antiforward on | off")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["antiilet"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"İletilen mesaj engeli: {'AÇIK' if cs['ayarlar']['antiilet'] else 'KAPALI'}")

@router.message(CMD("antiservice", "antiservis"))
async def antiservice_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antiservice on | off")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["antiservis"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Servis mesajlarını silme: {'AÇIK' if cs['ayarlar']['antiservis'] else 'KAPALI'}")

@router.message(CMD("adminonly", "adminmodu"))
async def adminonly_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Bu işlem için mesaj silme yetkisi gerekiyor.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .adminonly on | off")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["adminmodu"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Sadece yönetici konuşsun modu: {'AÇIK' if cs['ayarlar']['adminmodu'] else 'KAPALI'}")

@router.message(CMD("captcha"))
async def captcha_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .captcha on | off")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["captcha"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Captcha: {'AÇIK' if cs['ayarlar']['captcha'] else 'KAPALI'}")

@router.message(CMD("setcaptchatimeout", "captchasuresi"))
async def setcaptchatimeout_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.reply("Kullanım: .setcaptchatimeout 120")
    sec = max(10, min(3600, int(parts[1])))
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["captcha_sure_sn"] = sec
    schedule_save(config)
    await message.reply(f"Captcha süresi: {sec} saniye")

@router.message(CMD("setwarnlimit", "uyarilimiti"))
async def setwarnlimit_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.reply("Kullanım: .setwarnlimit 3")
    limit = max(1, min(20, int(parts[1])))
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["uyari_limiti"] = limit
    schedule_save(config)
    await message.reply(f"Uyarı limiti: {limit}")

@router.message(CMD("setmutetime", "mutesuresi"))
async def setmutetime_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    parts = (message.text or "").split()
    if len(parts) != 2:
        return await message.reply("Kullanım: .setmutetime 1h (örn: 30m, 2h, 1d)")
    sec = sureyi_saniyeye_cevir(parts[1])
    if sec is None or sec == 0:
        return await message.reply("Geçersiz süre. Örn: 30m, 2h, 1d")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["varsayilan_mute_sn"] = sec
    schedule_save(config)
    await message.reply(f"Varsayılan susturma süresi: {parts[1]} ({sec} sn)")


# ----------------- KORUMA AYARLARI (FLOOD/RAID/CAPS/EMOJI/MENTION) -----------------

@router.message(CMD("antiflood", "mesajseli"))
async def antiflood_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antiflood on | off")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["antiflood"]["acik"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Mesaj seli koruması: {'AÇIK' if cs['ayarlar']['antiflood']['acik'] else 'KAPALI'}")

@router.message(CMD("setflood", "mesajseliayarla"))
async def setflood_cmd(message: Message, bot: Bot, config: Config):
    """
    .setflood <maks> <sn> <warn|tmute> [tmute_sn]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split()
    if len(parts) < 4:
        return await message.reply("Kullanım: .setflood 6 5 tmute 300  (veya warn)")
    if not parts[1].isdigit() or not parts[2].isdigit():
        return await message.reply("Hatalı sayı. Örnek: .setflood 6 5 tmute 300")
    maks_mesaj = max(2, min(30, int(parts[1])))
    sure_sn = max(2, min(30, int(parts[2])))
    eylem = parts[3].lower()
    if eylem not in ("warn", "tmute"):
        return await message.reply("Eylem sadece: warn veya tmute")
    tmute_sn = 300
    if eylem == "tmute":
        if len(parts) >= 5 and parts[4].isdigit():
            tmute_sn = max(10, min(86400, int(parts[4])))
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["antiflood"].update({"maks_mesaj": maks_mesaj, "sure_sn": sure_sn, "eylem": eylem, "tmute_sn": tmute_sn})
    schedule_save(config)
    await message.reply(f"Mesaj seli ayarı: {maks_mesaj} mesaj / {sure_sn} sn, eylem={eylem}, tmute={tmute_sn} sn")

@router.message(CMD("antiraid", "baskinkorumasi"))
async def antiraid_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antiraid on | off")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["antiraid"]["acik"] = (parts[1].lower() == "on")
    schedule_save(config)
    await message.reply(f"Baskın koruması: {'AÇIK' if cs['ayarlar']['antiraid']['acik'] else 'KAPALI'}")

@router.message(CMD("setraid", "baskinayarla"))
async def setraid_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split()
    if len(parts) != 4 or not all(p.isdigit() for p in parts[1:]):
        return await message.reply("Kullanım: .setraid 8 60 120")
    esik = max(3, min(50, int(parts[1])))
    pencere = max(10, min(600, int(parts[2])))
    kilit = max(10, min(3600, int(parts[3])))
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["antiraid"].update({"esik": esik, "pencere_sn": pencere, "kilit_sn": kilit})
    schedule_save(config)
    await message.reply(f"Baskın ayarı: eşik={esik}, pencere={pencere} sn, kilit={kilit} sn")

@router.message(CMD("anticaps", "buyukharf"))
async def anticaps_cmd(message: Message, bot: Bot, config: Config):
    """
    .anticaps on|off [oran]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .anticaps on|off 0.7")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["anticaps"]["acik"] = (parts[1].lower() == "on")
    if len(parts) >= 3:
        try:
            oran = float(parts[2])
            oran = max(0.4, min(0.95, oran))
            cs["ayarlar"]["anticaps"]["oran"] = oran
        except:
            pass
    schedule_save(config)
    await message.reply(f"Büyük harf koruması: {'AÇIK' if cs['ayarlar']['anticaps']['acik'] else 'KAPALI'} (oran={cs['ayarlar']['anticaps']['oran']})")

@router.message(CMD("antiemoji", "emojikorumasi"))
async def antiemoji_cmd(message: Message, bot: Bot, config: Config):
    """
    .antiemoji on|off [maks]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antiemoji on|off 12")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["antiemoji"]["acik"] = (parts[1].lower() == "on")
    if len(parts) >= 3 and parts[2].isdigit():
        cs["ayarlar"]["antiemoji"]["maks"] = max(3, min(100, int(parts[2])))
    schedule_save(config)
    await message.reply(f"Emoji koruması: {'AÇIK' if cs['ayarlar']['antiemoji']['acik'] else 'KAPALI'} (maks={cs['ayarlar']['antiemoji']['maks']})")

@router.message(CMD("antimention", "etiketkorumasi"))
async def antimention_cmd(message: Message, bot: Bot, config: Config):
    """
    .antimention on|off [maks]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: .antimention on|off 6")
    cs = sohbet_durumu(message.chat.id, config)
    cs["ayarlar"]["antimention"]["acik"] = (parts[1].lower() == "on")
    if len(parts) >= 3 and parts[2].isdigit():
        cs["ayarlar"]["antimention"]["maks"] = max(1, min(50, int(parts[2])))
    schedule_save(config)
    await message.reply(f"Etiket koruması: {'AÇIK' if cs['ayarlar']['antimention']['acik'] else 'KAPALI'} (maks={cs['ayarlar']['antimention']['maks']})")


# ----------------- KİLİT TÜRLERİ -----------------

def kilit_haritasi(t: str) -> set[str]:
    t = t.lower()
    if t == "tum":
        return {"link", "foto", "video", "dosya", "sticker", "gif", "ses", "muzik"}
    if t == "medya":
        return {"foto", "video", "dosya", "sticker", "gif", "ses", "muzik"}
    return {t}

@router.message(CMD("lock", "kilit"))
async def lock_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Bu işlem için mesaj silme yetkisi gerekiyor.")
    parts = (message.text or "").split()
    if len(parts) != 2:
        return await message.reply("Kullanım: .lock link|medya|foto|video|dosya|sticker|gif|ses|muzik|tum")
    t = parts[1].lower()
    if t not in KILIT_TURLERI:
        return await message.reply("Geçersiz kilit türü.")
    cs = sohbet_durumu(message.chat.id, config)
    kilitler = set(cs["ayarlar"].get("kilitler") or [])
    kilitler.update(kilit_haritasi(t))
    cs["ayarlar"]["kilitler"] = sorted(list(kilitler))
    schedule_save(config)
    await message.reply("Aktif kilitler: " + ", ".join(cs["ayarlar"]["kilitler"]))

@router.message(CMD("unlock", "kilitac"))
async def unlock_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Bu işlem için mesaj silme yetkisi gerekiyor.")
    parts = (message.text or "").split()
    if len(parts) != 2:
        return await message.reply("Kullanım: .unlock link|medya|foto|video|dosya|sticker|gif|ses|muzik|tum")
    t = parts[1].lower()
    if t not in KILIT_TURLERI:
        return await message.reply("Geçersiz kilit türü.")
    cs = sohbet_durumu(message.chat.id, config)
    kilitler = set(cs["ayarlar"].get("kilitler") or [])
    if t == "tum":
        kilitler.clear()
    elif t == "medya":
        kilitler.difference_update({"foto", "video", "dosya", "sticker", "gif", "ses", "muzik"})
    else:
        kilitler.discard(t)
    cs["ayarlar"]["kilitler"] = sorted(list(kilitler))
    schedule_save(config)
    await message.reply("Aktif kilitler: " + (", ".join(cs["ayarlar"]["kilitler"]) if cs["ayarlar"]["kilitler"] else "YOK"))

@router.message(CMD("locks", "kilitler"))
async def locks_list_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    cs = sohbet_durumu(message.chat.id, config)
    kilitler = cs["ayarlar"].get("kilitler") or []
    await message.reply("Aktif kilitler: " + (", ".join(kilitler) if kilitler else "YOK"))


# ----------------- NOTLAR / FİLTRELER -----------------

@router.message(CMD("save", "kaydet"))
async def save_note_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply("Kullanım: .save <ad> <içerik>")
    name = parts[1].strip().lower()
    content = parts[2].strip()
    cs = sohbet_durumu(message.chat.id, config)
    cs.setdefault("notlar", {})[name] = content
    schedule_save(config)
    await message.reply(f"Not kaydedildi: <code>{name}</code>")

@router.message(CMD("delnote", "notsil"))
async def delnote_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .delnote <ad>")
    name = parts[1].strip().lower()
    cs = sohbet_durumu(message.chat.id, config)
    if name in (cs.get("notlar") or {}):
        del cs["notlar"][name]
        schedule_save(config)
        return await message.reply("Not silindi.")
    await message.reply("Not bulunamadı.")

@router.message(CMD("filter", "filtre"))
async def add_filter_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply("Kullanım: .filter <kelime> <cevap>")
    key = parts[1].strip().lower()
    resp = parts[2].strip()
    cs = sohbet_durumu(message.chat.id, config)
    cs.setdefault("filtreler", {})[key] = resp
    schedule_save(config)
    await message.reply(f"Filtre kaydedildi: <code>{key}</code>")

@router.message(CMD("stop", "filtrekapat"))
async def del_filter_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: .stop <kelime>")
    key = parts[1].strip().lower()
    cs = sohbet_durumu(message.chat.id, config)
    if key in (cs.get("filtreler") or {}):
        del cs["filtreler"][key]
        schedule_save(config)
        return await message.reply("Filtre silindi.")
    await message.reply("Filtre bulunamadı.")

@router.message(CMD("filters", "filtreler"))
async def filters_list_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    cs = sohbet_durumu(message.chat.id, config)
    keys = sorted((cs.get("filtreler") or {}).keys())
    if not keys:
        return await message.reply("Filtre yok.")
    await message.reply("Filtreler:\n" + "\n".join(f"• <code>{k}</code>" for k in keys))


# ----------------- MODERASYON -----------------

@router.message(CMD("warn", "uyari"))
async def warn_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    parts = (message.text or "").split(maxsplit=2)
    sebep = parts[2] if len(parts) >= 3 else ""
    sayi = await uyari_ekle(message.chat.id, tid, message.from_user.id, sebep, config)
    cs = sohbet_durumu(message.chat.id, config)
    limit = int(cs["ayarlar"].get("uyari_limiti") or config.default_warn_limit)

    if sayi >= limit:
        if await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
            mute_sn = int(cs["ayarlar"].get("varsayilan_mute_sn") or config.default_mute_seconds)
            until = datetime.utcnow() + timedelta(seconds=mute_sn)
            try:
                await bot.restrict_chat_member(message.chat.id, tid, permissions=sustur_izinleri(), until_date=until)
            except:
                pass
            await message.reply(f"Uyarı verildi: <code>{tid}</code> ({sayi}/{limit})\nLimit aşıldı, susturma uygulandı.")
        else:
            await message.reply(f"Uyarı verildi: <code>{tid}</code> ({sayi}/{limit})\nLimit aşıldı ama botun susturma yetkisi yok.")
    else:
        await message.reply(f"Uyarı verildi: <code>{tid}</code> ({sayi}/{limit})")

@router.message(CMD("unwarn", "uyarigerial"))
async def unwarn_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    yeni = await uyari_azalt(message.chat.id, tid, config)
    await message.reply(f"Uyarı azaltıldı: <code>{tid}</code> (şimdi: {yeni})")

@router.message(CMD("warnings", "uyarilarim"))
async def warnings_cmd(message: Message, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    tid = message.reply_to_message.from_user.id if (message.reply_to_message and message.reply_to_message.from_user) else (message.from_user.id if message.from_user else None)
    if not tid:
        return
    w = uyari_getir(message.chat.id, tid, config)
    await message.reply(f"<code>{tid}</code> uyarı sayısı: {w['sayi']}")

@router.message(CMD("warnslist", "uyaridetay"))
async def warnslist_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private":
        return
    if message.from_user and not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Bu komut sadece yöneticiler içindir.")

    tid = None
    if message.reply_to_message and message.reply_to_message.from_user:
        tid = message.reply_to_message.from_user.id
    else:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) >= 2:
            arg = parts[1].strip()
            if arg.lstrip("-").isdigit():
                tid = int(arg)
            elif arg.startswith("@"):
                tid = await kullanici_adindan_id(arg)
        if tid is None and message.from_user:
            tid = message.from_user.id

    if tid is None:
        return

    w = uyari_getir(message.chat.id, tid, config)
    sebepler = w.get("sebepler") or []
    if not sebepler:
        return await message.reply("Kayıtlı uyarı detayı yok.")

    lines = [f"<b>{tid}</b> uyarı detayları (son {len(sebepler)}):"]
    for i, r in enumerate(sebepler[-10:], 1):
        lines.append(f"{i}) veren: <code>{r.get('kim')}</code> | tarih: {r.get('tarih')} | sebep: {r.get('sebep')}")
    await message.reply("\n".join(lines))

@router.message(CMD("resetwarns", "uyarilarisifirla"))
async def resetwarns_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    cs = sohbet_durumu(message.chat.id, config)
    cs.setdefault("uyarilar", {}).pop(str(tid), None)
    schedule_save(config)
    await message.reply(f"Uyarılar sıfırlandı: <code>{tid}</code>")

@router.message(CMD("ban", "yasakla"))
async def ban_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Botun üye kısıtlama yetkisi yok.")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    try:
        await bot.ban_chat_member(message.chat.id, tid)
        await message.reply(f"Yasaklandı (ban): <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("tban", "sureliban"))
async def tban_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Botun üye kısıtlama yetkisi yok.")
    parts = (message.text or "").split()
    if message.reply_to_message:
        if len(parts) < 2:
            return await message.reply("Kullanım: (reply) .tban 2d [sebep]")
        dur_str = parts[1]
    else:
        if len(parts) < 3:
            return await message.reply("Kullanım: .tban @kullaniciadi 2d [sebep]")
        dur_str = parts[2]
    sec = sureyi_saniyeye_cevir(dur_str)
    if sec is None:
        return await message.reply("Süre formatı: 10m, 2h, 1d, 1w, 1h30m (kalıcı: perm)")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    until = None if sec == 0 else (datetime.utcnow() + timedelta(seconds=sec))
    try:
        await bot.ban_chat_member(message.chat.id, tid, until_date=until)
        await message.reply(f"Süreli yasaklandı: <code>{tid}</code> | süre: {dur_str}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("unban", "yasakkaldir"))
async def unban_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Botun üye kısıtlama yetkisi yok.")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    try:
        await bot.unban_chat_member(message.chat.id, tid, only_if_banned=True)
        await message.reply(f"Yasak kaldırıldı: <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("kick", "at"))
async def kick_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Botun üye kısıtlama yetkisi yok.")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    try:
        await bot.ban_chat_member(message.chat.id, tid)
        await bot.unban_chat_member(message.chat.id, tid, only_if_banned=True)
        await message.reply(f"Gruptan atıldı: <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("softban", "yumusakban"))
async def softban_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Botun üye kısıtlama yetkisi yok.")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    try:
        await bot.ban_chat_member(message.chat.id, tid)
        await bot.unban_chat_member(message.chat.id, tid, only_if_banned=True)
        await message.reply(f"Yumuşak ban uygulandı: <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("mute", "sustur"))
async def mute_cmd(message: Message, bot: Bot, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Botun üye kısıtlama yetkisi yok.")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    cs = sohbet_durumu(message.chat.id, config)
    mute_sn = int(cs["ayarlar"].get("varsayilan_mute_sn") or config.default_mute_seconds)
    until = datetime.utcnow() + timedelta(seconds=mute_sn)
    try:
        await bot.restrict_chat_member(message.chat.id, tid, permissions=sustur_izinleri(), until_date=until)
        await message.reply(f"Susturuldu: <code>{tid}</code> ({mute_sn} sn)")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("tmute", "surelisustur"))
async def tmute_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Botun üye kısıtlama yetkisi yok.")
    parts = (message.text or "").split()
    if message.reply_to_message:
        if len(parts) < 2:
            return await message.reply("Kullanım: (reply) .tmute 10m [sebep]")
        dur_str = parts[1]
    else:
        if len(parts) < 3:
            return await message.reply("Kullanım: .tmute @kullaniciadi 10m [sebep]")
        dur_str = parts[2]
    sec = sureyi_saniyeye_cevir(dur_str)
    if sec is None or sec == 0:
        return await message.reply("Süre formatı: 10m, 2h, 1d, 1w, 1h30m (kalıcı susturma yok)")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    until = datetime.utcnow() + timedelta(seconds=sec)
    try:
        await bot.restrict_chat_member(message.chat.id, tid, permissions=sustur_izinleri(), until_date=until)
        await message.reply(f"Süreli susturuldu: <code>{tid}</code> | süre: {dur_str}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("unmute", "susturmakaldir"))
async def unmute_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Bu işlem için üye kısıtlama yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Botun üye kısıtlama yetkisi yok.")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    try:
        await bot.restrict_chat_member(message.chat.id, tid, permissions=acik_izinler())
        await message.reply(f"Susturma kaldırıldı: <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("del", "sil"))
async def del_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.reply_to_message:
        return await message.reply("Silmek için bir mesaja reply yap.")
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Bu işlem için mesaj silme yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_delete_messages"):
        return await message.reply("Botun mesaj silme yetkisi yok.")
    try:
        await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
    except:
        pass
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except:
        pass

@router.message(CMD("purge", "temizle"))
async def purge_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.reply_to_message:
        return await message.reply("Temizlemek için bir mesaja reply yap.")
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Bu işlem için mesaj silme yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_delete_messages"):
        return await message.reply("Botun mesaj silme yetkisi yok.")
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

@router.message(CMD("promote", "yetkiver"))
async def promote_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_promote_members"):
        return await message.reply("Bu işlem için üye yükseltme yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_promote_members"):
        return await message.reply("Botun yönetici atama yetkisi yok.")
    tid = await hedef_gerekli(message)
    if not tid:
        return
    parts = (message.text or "").split(maxsplit=2)
    baslik = parts[2].strip() if len(parts) >= 3 else None
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
        if baslik:
            try:
                await bot.set_chat_administrator_custom_title(message.chat.id, tid, baslik[:16])
            except:
                pass
        await message.reply(f"Yönetici yapıldı: <code>{tid}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(CMD("demote", "yetkial"))
async def demote_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await kullanici_yetki_kontrol(bot, message.chat.id, message.from_user.id, "can_promote_members"):
        return await message.reply("Bu işlem için üye yükseltme yetkisi gerekiyor.")
    if not await bot_yetki_kontrol(bot, message.chat.id, "can_promote_members"):
        return await message.reply("Botun yönetici atama yetkisi yok.")
    tid = await hedef_gerekli(message)
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

@router.message(CMD("admins", "yoneticiler"))
async def admins_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private":
        return
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        lines = ["Yöneticiler:"]
        for a in admins:
            u = a.user
            tag = f"@{u.username}" if u.username else u.full_name
            lines.append(f"• {tag} (<code>{u.id}</code>)")
        await message.reply("\n".join(lines))
    except Exception as e:
        await message.reply(f"Hata: {e}")


# ----------------- JOIN: HOŞGELDİN + CAPTCHA + RAID -----------------

@router.chat_member()
async def on_join(event: ChatMemberUpdated, bot: Bot, config: Config):
    if not event.chat:
        return
    old_status = getattr(event.old_chat_member, "status", None)
    new_status = getattr(event.new_chat_member, "status", None)
    if not (old_status in ("left", "kicked") and new_status == "member"):
        return

    cs = sohbet_durumu(event.chat.id, config)
    a = cs["ayarlar"]
    user = event.new_chat_member.user

    # kullanıcı adı cache
    if user.username:
        cache = STATE.setdefault("kullanici_adi_onbellek", {})
        key = user.username.lower()
        if cache.get(key) != user.id:
            cache[key] = user.id
            schedule_save(config)

    # baskın koruması (çok hızlı katılım)
    if a.get("antiraid", {}).get("acik"):
        q = JOINS.setdefault(event.chat.id, deque())
        now = asyncio.get_event_loop().time()
        q.append(now)
        pencere = int(a["antiraid"].get("pencere_sn", 60))
        while q and (now - q[0] > pencere):
            q.popleft()

        esik = int(a["antiraid"].get("esik", 8))
        if len(q) >= esik:
            kilit_sn = int(a["antiraid"].get("kilit_sn", 120))
            if await bot_yetki_kontrol(bot, event.chat.id, "can_restrict_members"):
                try:
                    await bot.set_chat_permissions(event.chat.id, permissions=sustur_izinleri())
                    cs["raid"]["kilit_bitis"] = (simdi_utc() + timedelta(seconds=kilit_sn)).isoformat() + "Z"
                    schedule_save(config)
                    try:
                        await bot.send_message(event.chat.id, f"Baskın koruması: Grup {kilit_sn} saniye kilitlendi.")
                    except:
                        pass
                except:
                    pass

    # captcha
    if a.get("captcha") and await bot_yetki_kontrol(bot, event.chat.id, "can_restrict_members"):
        try:
            await bot.restrict_chat_member(event.chat.id, user.id, permissions=sustur_izinleri())
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Doğrula", callback_data=f"verify:{user.id}")
            ]])
            msg = await bot.send_message(event.chat.id, f"{user.first_name} doğrulama gerekli. Butona bas.", reply_markup=kb)
            timeout = int(a.get("captcha_sure_sn") or 120)
            cs.setdefault("captcha_bekleyen", {})[str(user.id)] = {
                "bitis": (simdi_utc() + timedelta(seconds=timeout)).isoformat() + "Z",
                "mesaj_id": msg.message_id
            }
            schedule_save(config)
        except:
            pass

    # hoşgeldin
    welcome = a.get("hosgeldin")
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

    cs = sohbet_durumu(chat_id, config)
    if not cs["ayarlar"].get("captcha"):
        return await call.answer("Captcha kapalı.", show_alert=True)

    if not await bot_yetki_kontrol(bot, chat_id, "can_restrict_members"):
        return await call.answer("Botun üye kısıtlama yetkisi yok.", show_alert=True)

    try:
        await bot.restrict_chat_member(chat_id, target_id, permissions=acik_izinler())
        cs.get("captcha_bekleyen", {}).pop(str(target_id), None)
        schedule_save(config)
        try:
            await call.message.delete()
        except:
            pass
        await call.answer("Doğrulandı!")
    except:
        await call.answer("İşlem başarısız.", show_alert=True)


# ----------------- ARKA PLAN GÖREVLERİ -----------------

async def background_tasks(bot: Bot, config: Config):
    """
    - captcha süresi dolarsa: kick
    - baskın kilidi süresi dolarsa: kilidi aç
    """
    while True:
        await asyncio.sleep(5)
        sohbetler = list((STATE.get("sohbetler") or {}).items())
        now = simdi_utc()

        for chat_id_str, cs in sohbetler:
            try:
                chat_id = int(chat_id_str)
            except:
                continue

            # baskın kilidini aç
            bitis_iso = (cs.get("raid") or {}).get("kilit_bitis")
            if bitis_iso:
                try:
                    bitis = datetime.fromisoformat(bitis_iso.replace("Z", ""))
                    if now >= bitis:
                        if await bot_yetki_kontrol(bot, chat_id, "can_restrict_members"):
                            try:
                                await bot.set_chat_permissions(chat_id, permissions=acik_izinler())
                            except:
                                pass
                        cs["raid"]["kilit_bitis"] = None
                        schedule_save(config)
                except:
                    cs["raid"]["kilit_bitis"] = None
                    schedule_save(config)

            # captcha süresi dolunca kick
            bekleyen = (cs.get("captcha_bekleyen") or {})
            if bekleyen:
                to_kick = []
                for uid_str, info in list(bekleyen.items()):
                    exp = info.get("bitis")
                    if not exp:
                        continue
                    try:
                        exp_dt = datetime.fromisoformat(exp.replace("Z", ""))
                        if now >= exp_dt:
                            to_kick.append(int(uid_str))
                    except:
                        to_kick.append(int(uid_str))

                if to_kick and await bot_yetki_kontrol(bot, chat_id, "can_restrict_members"):
                    for uid in to_kick:
                        try:
                            await bot.ban_chat_member(chat_id, uid)
                            await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
                        except:
                            pass
                        bekleyen.pop(str(uid), None)
                    schedule_save(config)


# ----------------- OTOMATİK KORUMA / FİLTRELER -----------------

@router.message()
async def auto_guard_handler(message: Message, bot: Bot, config: Config):
    """
    Sıra:
    - servis mesajı silme
    - komutları es geç
    - sadece admin konuşsun (adminmodu)
    - kilit türleri
    - iletilen mesaj engeli
    - link engeli
    - büyük harf / emoji / etiket
    - mesaj seli
    - filtre yanıtı
    """
    if not message.chat or message.chat.type == "private":
        return

    cs = sohbet_durumu(message.chat.id, config)
    a = cs["ayarlar"]

    # servis mesajları
    if a.get("antiservis"):
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
            await mesaj_sil(message, bot)
            return

    # komutları es geç
    if message.text and (message.text.startswith("/") or message.text.startswith(".")):
        return

    if not message.from_user:
        return

    # sadece admin konuşsun
    if a.get("adminmodu") and not await admin_mi(bot, message.chat.id, message.from_user.id):
        await mesaj_sil(message, bot)
        return

    # kilit türleri (admin değilse)
    if not await admin_mi(bot, message.chat.id, message.from_user.id):
        kilitler = set(a.get("kilitler") or [])

        if "link" in kilitler and message.text and URL_RE.search(message.text):
            await mesaj_sil(message, bot)
            return
        if "foto" in kilitler and message.photo:
            await mesaj_sil(message, bot)
            return
        if "video" in kilitler and (message.video or message.video_note):
            await mesaj_sil(message, bot)
            return
        if "dosya" in kilitler and message.document:
            await mesaj_sil(message, bot)
            return
        if "sticker" in kilitler and message.sticker:
            await mesaj_sil(message, bot)
            return
        if "gif" in kilitler and message.animation:
            await mesaj_sil(message, bot)
            return
        if "ses" in kilitler and message.voice:
            await mesaj_sil(message, bot)
            return
        if "muzik" in kilitler and message.audio:
            await mesaj_sil(message, bot)
            return

    # iletilen mesaj engeli
    if a.get("antiilet") and not await admin_mi(bot, message.chat.id, message.from_user.id):
        is_forward = bool(
            getattr(message, "forward_origin", None) or
            getattr(message, "forward_date", None) or
            getattr(message, "forward_from_chat", None) or
            getattr(message, "forward_from", None)
        )
        if is_forward:
            await mesaj_sil(message, bot)
            await cezalandir_uyari_veya_tmute(message, bot, config, "İletilen mesaj", eylem="warn")
            return

    # link engeli
    if a.get("antilink") and message.text and URL_RE.search(message.text) and not await admin_mi(bot, message.chat.id, message.from_user.id):
        await mesaj_sil(message, bot)
        await cezalandir_uyari_veya_tmute(message, bot, config, "Link paylaşımı", eylem="warn")
        return

    # büyük harf koruması
    if a.get("anticaps", {}).get("acik") and message.text and not await admin_mi(bot, message.chat.id, message.from_user.id):
        min_len = int(a["anticaps"].get("min_uzunluk", 12))
        if len(message.text) >= min_len:
            oran = float(a["anticaps"].get("oran", 0.7))
            if buyukharf_orani(message.text) >= oran:
                await mesaj_sil(message, bot)
                await cezalandir_uyari_veya_tmute(message, bot, config, "Aşırı büyük harf", eylem=a["anticaps"].get("eylem", "warn"))
                return

    # emoji koruması
    if a.get("antiemoji", {}).get("acik") and message.text and not await admin_mi(bot, message.chat.id, message.from_user.id):
        mx = int(a["antiemoji"].get("maks", 12))
        if emoji_sayisi(message.text) > mx:
            await mesaj_sil(message, bot)
            await cezalandir_uyari_veya_tmute(message, bot, config, "Aşırı emoji", eylem=a["antiemoji"].get("eylem", "warn"))
            return

    # etiket koruması
    if a.get("antimention", {}).get("acik") and message.text and not await admin_mi(bot, message.chat.id, message.from_user.id):
        mx = int(a["antimention"].get("maks", 6))
        if mention_sayisi(message) > mx:
            await mesaj_sil(message, bot)
            await cezalandir_uyari_veya_tmute(message, bot, config, "Aşırı etiket", eylem=a["antimention"].get("eylem", "warn"))
            return

    # mesaj seli
    flood = a.get("antiflood") or {}
    if flood.get("acik") and not await admin_mi(bot, message.chat.id, message.from_user.id):
        key = (message.chat.id, message.from_user.id)
        q = FLOOD.setdefault(key, deque())
        now = asyncio.get_event_loop().time()
        q.append(now)

        sure_sn = int(flood.get("sure_sn", 5))
        maks = int(flood.get("maks_mesaj", 6))
        while q and (now - q[0] > sure_sn):
            q.popleft()

        if len(q) >= maks:
            eylem = str(flood.get("eylem", "tmute"))
            if eylem == "warn":
                await cezalandir_uyari_veya_tmute(message, bot, config, "Mesaj seli", eylem="warn")
            else:
                tmute_sn = int(flood.get("tmute_sn", 300))
                await cezalandir_uyari_veya_tmute(message, bot, config, "Mesaj seli", eylem="tmute", tmute_sn=tmute_sn)
            await mesaj_sil(message, bot)
            q.clear()
            return

    # filtreler
    if message.text:
        filtreler = cs.get("filtreler") or {}
        txt = message.text.lower()
        for k, resp in filtreler.items():
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

    global BOT_ID
    BOT_ID = (await bot.get_me()).id

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(InjectConfigMiddleware(config))
    dp.update.middleware(UsernameCacheMiddleware())

    dp.include_router(router)

    asyncio.create_task(background_tasks(bot, config))

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
