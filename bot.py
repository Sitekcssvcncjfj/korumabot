import os
import re
import json
import asyncio
from html import escape
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Dict, Any

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, ChatPermissions, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.client.default import DefaultBotProperties

# ================== CONFIG ==================

TOKEN = os.getenv("BOT_TOKEN")
STATE_PATH = os.getenv("STATE_PATH", "state.json")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN yok!")

# ================== GLOBAL STATE ==================

router = Router()
FLOOD = defaultdict(lambda: deque())
STATE: Dict[str, Any] = {"chats": {}}

URL_RE = re.compile(r"(https?://|t\.me/)", re.IGNORECASE)

# ================== STATE ==================

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
        "antilink": False,
        "warns": {},
        "notes": {},
        "filters": {},
        "flood": {"enabled": False, "limit": 5, "seconds": 5}
    })

# ================== UTIL ==================

def mute_perm():
    return ChatPermissions(can_send_messages=False)

def unmute_perm():
    return ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )

async def is_admin(bot: Bot, chat_id: int, user_id: int):
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")

# ================== START ==================

@router.message(CommandStart())
async def start_cmd(message: Message):
    await message.reply(
        "👋 Merhaba!\n"
        "<b>Guard Bot aktif.</b>\n"
        "Komutlar için /help yaz.",
        parse_mode=ParseMode.HTML
    )

@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.reply(
        "<b>Komutlar</b>\n\n"
        "• /antilink on|off\n"
        "• /warn (reply)\n"
        "• /mute 10m (reply)\n"
        "• /ban (reply)\n"
        "• /note isim içerik\n"
        "• /get isim\n"
        "• /filter kelime cevap\n",
        parse_mode=ParseMode.HTML
    )

# ================== ANTILINK ==================

@router.message(Command("antilink"))
async def antilink_cmd(message: Message, bot: Bot):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Admin değilsin.")

    args = message.text.split()
    if len(args) != 2:
        return await message.reply("Kullanım: /antilink on|off")

    chat = get_chat(message.chat.id)
    chat["antilink"] = args[1].lower() == "on"
    save_state()

    await message.reply(f"Antilink {'AÇIK' if chat['antilink'] else 'KAPALI'}")

# ================== WARN ==================

@router.message(Command("warn"))
async def warn_cmd(message: Message, bot: Bot):
    if not message.reply_to_message:
        return await message.reply("Bir mesaja reply yap.")

    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Admin değilsin.")

    user_id = message.reply_to_message.from_user.id
    chat = get_chat(message.chat.id)
    warns = chat["warns"]
    warns[str(user_id)] = warns.get(str(user_id), 0) + 1
    save_state()

    await message.reply(f"Uyarı verildi. Toplam: {warns[str(user_id)]}")

# ================== MUTE ==================

@router.message(Command("mute"))
async def mute_cmd(message: Message, bot: Bot):
    if not message.reply_to_message:
        return await message.reply("Reply yap.")

    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Admin değilsin.")

    args = message.text.split()
    if len(args) != 2:
        return await message.reply("Kullanım: /mute 10m")

    seconds = int(args[1][:-1]) * 60
    until = datetime.utcnow() + timedelta(seconds=seconds)

    await bot.restrict_chat_member(
        message.chat.id,
        message.reply_to_message.from_user.id,
        permissions=mute_perm(),
        until_date=until
    )

    await message.reply("Susturuldu.")

# ================== BAN ==================

@router.message(Command("ban"))
async def ban_cmd(message: Message, bot: Bot):
    if not message.reply_to_message:
        return await message.reply("Reply yap.")

    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Admin değilsin.")

    await bot.ban_chat_member(
        message.chat.id,
        message.reply_to_message.from_user.id
    )

    await message.reply("Banlandı.")

# ================== NOTES ==================

@router.message(Command("note"))
async def note_cmd(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply("Kullanım: /note isim içerik")

    chat = get_chat(message.chat.id)
    chat["notes"][args[1]] = args[2]
    save_state()

    await message.reply("Not kaydedildi.")

@router.message(Command("get"))
async def get_note(message: Message):
    args = message.text.split()
    if len(args) != 2:
        return await message.reply("Kullanım: /get isim")

    chat = get_chat(message.chat.id)
    note = chat["notes"].get(args[1])

    if not note:
        return await message.reply("Not yok.")

    await message.reply(escape(note))

# ================== FILTER ==================

@router.message(Command("filter"))
async def filter_cmd(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.reply("Kullanım: /filter kelime cevap")

    chat = get_chat(message.chat.id)
    chat["filters"][args[1].lower()] = args[2]
    save_state()

    await message.reply("Filtre kaydedildi.")

# ================== AUTO GUARD ==================

@router.message()
async def auto_guard(message: Message, bot: Bot):
    if message.chat.type == "private":
        return

    chat = get_chat(message.chat.id)

    # Antilink
    if chat["antilink"] and message.text and URL_RE.search(message.text):
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            await message.delete()
            return

    # Filters
    if message.text:
        for k, v in chat["filters"].items():
            if k in message.text.lower():
                await message.reply(escape(v))
                break

# ================== MAIN ==================

async def main():
    load_state()

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp = Dispatcher()
    dp.include_router(router)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
