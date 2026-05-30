import os
import re
import json
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict, deque
from html import escape
from typing import Dict, Any

from aiogram import Bot, Dispatcher, Router
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties

# ================= CONFIG =================

BOT_USERNAME = "KGBKORUMABOT"
BOT_NAME = "KGB GUARD PRO"
MONTHLY_USERS = 654

TOKEN = os.getenv("BOT_TOKEN")
STATE_PATH = os.getenv("STATE_PATH", "state.json")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN yok!")

router = Router()
STATE: Dict[str, Any] = {"chats": {}, "blacklist": []}
FLOOD = defaultdict(lambda: deque())

URL_RE = re.compile(r"(https?://|t\.me/)", re.IGNORECASE)
TIME_RE = re.compile(r"(\d+)([smhd])")

# ================= STATE =================

def load_state():
    global STATE
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            STATE = json.load(f)

def save_state():
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)

def get_chat(chat_id: int):
    chats = STATE.setdefault("chats", {})
    return chats.setdefault(str(chat_id), {
        "stats": {},
        "warns": {},
        "welcome": None,
        "goodbye": None,
        "mods": [],
        "log_channel": None,
        "flood": {"limit": 6, "seconds": 5},
        "raid": {"limit": 5, "seconds": 30},
        "joins": [],
        "antilink": True
    })

def get_blacklist():
    return STATE.setdefault("blacklist", [])

# ================= PERMISSIONS =================

async def is_admin(bot: Bot, chat_id: int, user_id: int):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except:
        return False

async def has_permission(bot: Bot, chat_id: int, user_id: int):
    if await is_admin(bot, chat_id, user_id):
        return True
    return user_id in get_chat(chat_id)["mods"]

def mute_perm():
    return ChatPermissions(can_send_messages=False)

# ================= TIME =================

def parse_time(text: str):
    match = TIME_RE.match(text.lower())
    if not match:
        return None
    value, unit = match.groups()
    value = int(value)
    if unit == "s": return value
    if unit == "m": return value * 60
    if unit == "h": return value * 3600
    if unit == "d": return value * 86400

# ================= LOG =================

async def send_log(bot: Bot, chat_id: int, text: str):
    log_id = get_chat(chat_id).get("log_channel")
    if log_id:
        try:
            await bot.send_message(log_id, text, parse_mode=ParseMode.HTML)
        except:
            pass

# ================= PANEL =================

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ KGB GUARD PRO’yu Gruba Ekle 🚀",
            url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
        )],
        [InlineKeyboardButton(text="📢 Kanal Destek", url="https://t.me/KGBotomasyon")]
    ])

@router.message(CommandStart())
async def start_cmd(message: Message):
    text = f"""
✨ <b>{BOT_NAME}</b> ✨

🛡 Ultimate Protection
👥 Aylık aktif kullanıcı: <b>{MONTHLY_USERS}</b>
"""
    await message.reply(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)

# ================= MAIN HANDLER =================

@router.message()
async def main_handler(message: Message, bot: Bot):

    if message.chat.type == "private":
        return

    chat = get_chat(message.chat.id)

    # BLACKLIST SAFE
    try:
        if message.from_user.id in get_blacklist():
            await bot.ban_chat_member(message.chat.id, message.from_user.id)
            return
    except:
        pass

    # MESSAGE COUNT
    uid = str(message.from_user.id)
    chat["stats"][uid] = chat["stats"].get(uid, 0) + 1

    # FLOOD SAFE
    try:
        key = (message.chat.id, message.from_user.id)
        now = asyncio.get_event_loop().time()
        FLOOD[key].append(now)

        while FLOOD[key] and now - FLOOD[key][0] > chat["flood"]["seconds"]:
            FLOOD[key].popleft()

        member = await bot.get_chat_member(message.chat.id, message.from_user.id)

        if len(FLOOD[key]) >= chat["flood"]["limit"]:
            if member.status not in ("administrator", "creator"):
                await message.delete()
                await bot.restrict_chat_member(
                    message.chat.id,
                    message.from_user.id,
                    permissions=mute_perm(),
                    until_date=datetime.utcnow() + timedelta(seconds=60)
                )
                return
    except:
        pass

    # COMMAND CHECK
    if not message.text:
        save_state()
        return

    if not (message.text.startswith("/") or message.text.startswith(".")):
        save_state()
        return

    cmd_parts = message.text.split()
    cmd = cmd_parts[0][1:].lower()

    async def check_perm():
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            await message.reply("aq aveli sence yetkin varmı?")
            return False
        return True

    # BAN SAFE
    if cmd == "ban":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return
        target = message.reply_to_message.from_user.id

        try:
            member = await bot.get_chat_member(message.chat.id, target)
            if member.status in ("administrator", "creator"):
                return await message.reply("⚠ Adminlere işlem yapamam")
        except:
            return

        try:
            if len(cmd_parts) == 2:
                sec = parse_time(cmd_parts[1])
                if sec:
                    until = datetime.utcnow() + timedelta(seconds=sec)
                    await bot.ban_chat_member(message.chat.id, target, until_date=until)
                    await message.reply("✅ Süreli ban")
                    return

            await bot.ban_chat_member(message.chat.id, target)
            await message.reply("✅ Süresiz ban")
        except:
            pass

    save_state()

# ================= DAILY REPORT =================

async def daily_report(bot: Bot):
    while True:
        now = datetime.now()
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)

        await asyncio.sleep((target - now).total_seconds())

        for chat_id, data in STATE["chats"].items():
            stats = data.get("stats", {})
            if not stats:
                continue

            top = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:20]
            text = "📊 <b>Günlük Aktivite</b>\n\n"

            for i, (uid, count) in enumerate(top, 1):
                try:
                    member = await bot.get_chat_member(int(chat_id), int(uid))
                    name = member.user.full_name
                except:
                    name = uid
                text += f"{i}. {escape(name)} — {count} mesaj\n"

            text += f"\n👥 Aktif üye: {len(stats)}"

            try:
                await bot.send_message(int(chat_id), text, parse_mode=ParseMode.HTML)
            except:
                pass

            STATE["chats"][chat_id]["stats"] = {}

        save_state()

# ================= MAIN =================

async def main():
    load_state()

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(daily_report(bot))

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
