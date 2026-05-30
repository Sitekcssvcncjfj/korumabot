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
    ChatPermissions
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ================= CONFIG =================

TOKEN = os.getenv("BOT_TOKEN")
STATE_PATH = os.getenv("STATE_PATH", "state.json")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN yok!")

router = Router()
STATE: Dict[str, Any] = {"chats": {}}
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
        "warns": {},
        "stats": {},
        "welcome": None,
        "goodbye": None,
        "log": None,
        "antilink": False,
        "flood": {"limit": 6, "seconds": 5},
        "raid": {"limit": 5, "seconds": 30},
        "joins": []
    })

# ================= UTILS =================

async def is_admin(bot: Bot, chat_id: int, user_id: int):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except:
        return False

def mute_perm():
    return ChatPermissions(can_send_messages=False)

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

async def send_log(bot: Bot, chat_id: int, text: str):
    log_id = get_chat(chat_id).get("log")
    if log_id:
        try:
            await bot.send_message(log_id, text, parse_mode=ParseMode.HTML)
        except:
            pass

# ================= MAIN =================

@router.message()
async def main_handler(message: Message, bot: Bot):

    if message.chat.type == "private":
        return

    chat = get_chat(message.chat.id)

    # MESSAGE COUNT
    uid = str(message.from_user.id)
    chat["stats"][uid] = chat["stats"].get(uid, 0) + 1

    # FLOOD
    key = (message.chat.id, message.from_user.id)
    now = asyncio.get_event_loop().time()
    FLOOD[key].append(now)

    while FLOOD[key] and now - FLOOD[key][0] > chat["flood"]["seconds"]:
        FLOOD[key].popleft()

    try:
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

    # RAID
    if message.new_chat_members:
        chat["joins"].append(datetime.utcnow().timestamp())
        chat["joins"] = [
            j for j in chat["joins"]
            if datetime.utcnow().timestamp() - j < chat["raid"]["seconds"]
        ]

        if len(chat["joins"]) >= chat["raid"]["limit"]:
            try:
                await bot.set_chat_permissions(message.chat.id, permissions=mute_perm())
                await message.reply("🚨 Raid algılandı! Grup kilitlendi.")
            except:
                pass

    # ANTILINK
    if chat["antilink"] and message.text and URL_RE.search(message.text):
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            await message.delete()
            return

    # COMMAND CHECK
    if not message.text:
        save_state()
        return

    if not (message.text.startswith("/") or message.text.startswith(".")):
        save_state()
        return

    cmd_parts = message.text.split()
    cmd = cmd_parts[0][1:].lower()

    async def check_admin():
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            await message.reply("Bu komut sadece yöneticiler içindir.")
            return False
        return True

    # BAN
    if cmd == "ban":
        if not message.reply_to_message: return
        if not await check_admin(): return
        target = message.reply_to_message.from_user.id
        member = await bot.get_chat_member(message.chat.id, target)
        if member.status in ("administrator", "creator"):
            return await message.reply("Adminlere işlem yapamam.")

        if len(cmd_parts) == 2:
            sec = parse_time(cmd_parts[1])
            if sec:
                until = datetime.utcnow() + timedelta(seconds=sec)
                await bot.ban_chat_member(message.chat.id, target, until_date=until)
                await message.reply("Süreli ban ✅")
                return

        await bot.ban_chat_member(message.chat.id, target)
        await message.reply("Süresiz ban ✅")

    # WARN
    if cmd == "warn":
        if not message.reply_to_message: return
        if not await check_admin(): return
        target = message.reply_to_message.from_user.id
        chat["warns"][str(target)] = chat["warns"].get(str(target), 0) + 1

        if chat["warns"][str(target)] >= 3:
            await bot.restrict_chat_member(
                message.chat.id,
                target,
                permissions=mute_perm()
            )
            chat["warns"][str(target)] = 0
            await message.reply("3 warn oldu, otomatik mute ✅")
        else:
            await message.reply(f"Warn ({chat['warns'][str(target)]}/3)")

    # SET FLOOD
    if cmd == "setflood":
        if not await check_admin(): return
        if len(cmd_parts) != 3: return
        chat["flood"]["limit"] = int(cmd_parts[1])
        chat["flood"]["seconds"] = int(cmd_parts[2])
        await message.reply("Flood ayarlandı ✅")

    # SET RAID
    if cmd == "setraid":
        if not await check_admin(): return
        if len(cmd_parts) != 3: return
        chat["raid"]["limit"] = int(cmd_parts[1])
        chat["raid"]["seconds"] = int(cmd_parts[2])
        await message.reply("Raid ayarlandı ✅")

    # SET WELCOME
    if cmd == "setwelcome":
        if not await check_admin(): return
        chat["welcome"] = message.text.split(maxsplit=1)[1]
        await message.reply("Hoşgeldin mesajı ayarlandı ✅")

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
            if not stats: continue

            top = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:20]
            text = "📊 Günlük Aktivite\n\n"

            for i, (uid, count) in enumerate(top, 1):
                text += f"{i}. {uid} — {count} mesaj\n"

            try:
                await bot.send_message(int(chat_id), text)
            except:
                pass

            STATE["chats"][chat_id]["stats"] = {}

        save_state()

# ================= MAIN =================

async def main():
    load_state()
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(daily_report(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
