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

BOT_NAME = "KGB GUARD PRO"
MONTHLY_USERS = 654

TOKEN = os.getenv("BOT_TOKEN")
STATE_PATH = os.getenv("STATE_PATH", "state.json")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN yok!")

# ================= GLOBAL =================

router = Router()
STATE: Dict[str, Any] = {"chats": {}, "users": {}}
FLOOD = defaultdict(lambda: deque())
JOINS = defaultdict(lambda: deque())

URL_RE = re.compile(r"(https?://|t\.me/)", re.IGNORECASE)
TIME_RE = re.compile(r"(\d+)([smhd])")

# ================= LANGUAGE SYSTEM =================

LANGS = {
    "tr": {
        "start": "👋 Merhaba!\n<b>KGB GUARD PRO aktif.</b>\n\nAylık aktif kullanıcı: <b>654</b>",
        "add_group": "➕ Beni Gruba Ekle",
        "my_groups": "📊 Gruplarım",
        "support": "📢 Kanal Destek",
        "commands": "📖 Komutlar",
        "language": "🌍 Dil Seç",
        "no_perm": "aq aveli sence yetkin varmı?",
    },
    "en": {
        "start": "👋 Hello!\n<b>KGB GUARD PRO active.</b>\n\nMonthly active users: <b>654</b>",
        "add_group": "➕ Add to Group",
        "my_groups": "📊 My Groups",
        "support": "📢 Support Channel",
        "commands": "📖 Commands",
        "language": "🌍 Select Language",
        "no_perm": "bro do you even have permission?",
    },
    "az": {
        "start": "👋 Salam!\n<b>KGB GUARD PRO aktivdir.</b>\n\nAylıq aktiv istifadəçi: <b>654</b>",
        "add_group": "➕ Qrupa əlavə et",
        "my_groups": "📊 Qruplarım",
        "support": "📢 Dəstək Kanalı",
        "commands": "📖 Komandalar",
        "language": "🌍 Dil seç",
        "no_perm": "ay bala səndə icazə var?",
    }
}

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
        "lang": "tr",
        "welcome": None,
        "goodbye": None,
        "antilink": False,
        "warns": {},
        "flood": {"enabled": False, "limit": 5, "seconds": 5},
        "antiraid": False
    })

def get_user(user_id: int):
    users = STATE.setdefault("users", {})
    return users.setdefault(str(user_id), {"lang": "tr"})

# ================= PERMISSIONS =================

async def is_admin(bot: Bot, chat_id: int, user_id: int):
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")

def mute_perm():
    return ChatPermissions(can_send_messages=False)

# ================= PREMIUM MENU =================

def main_menu(lang: str):
    l = LANGS[lang]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=l["add_group"], url=f"https://t.me/{BOT_NAME.replace(' ','')}?startgroup=true")],
        [InlineKeyboardButton(text=l["my_groups"], callback_data="mygroups")],
        [InlineKeyboardButton(text=l["commands"], callback_data="commands")],
        [InlineKeyboardButton(text=l["language"], callback_data="language")],
        [InlineKeyboardButton(text=l["support"], url="https://t.me/KGBotomasyon")]
    ])

# ================= START =================

@router.message(CommandStart())
async def start_cmd(message: Message):
    user = get_user(message.from_user.id)
    lang = user["lang"]
    l = LANGS[lang]

    await message.reply(
        l["start"],
        reply_markup=main_menu(lang),
        parse_mode=ParseMode.HTML
    )

# ================= LANGUAGE MENU =================

@router.callback_query(lambda c: c.data == "language")
async def language_menu(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇹🇷 Türkçe", callback_data="lang_tr")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton(text="🇦🇿 Azərbaycan", callback_data="lang_az")]
    ])
    await call.message.edit_text("Dil seçin:", reply_markup=kb)

@router.callback_query(lambda c: c.data.startswith("lang_"))
async def set_language(call: CallbackQuery):
    code = call.data.split("_")[1]
    user = get_user(call.from_user.id)
    user["lang"] = code
    save_state()
    await call.answer("Dil değiştirildi ✅")
    await call.message.delete()

# ================= TIME PARSER =================

def parse_time(text: str):
    if not text:
        return None
    match = TIME_RE.match(text.lower())
    if not match:
        return None
    value, unit = match.groups()
    value = int(value)

    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 3600
    if unit == "d":
        return value * 86400
    return None

# ================= PERMISSION CHECK =================

async def require_admin(message: Message, bot: Bot):
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        lang = get_chat(message.chat.id)["lang"]
        await message.reply(LANGS[lang]["no_perm"])
        return False
    return True

def is_command(text: str):
    return text.startswith("/") or text.startswith(".")

# ================= WARN SYSTEM =================

async def add_warn(chat_id: int, user_id: int):
    chat = get_chat(chat_id)
    warns = chat["warns"]
    warns[str(user_id)] = warns.get(str(user_id), 0) + 1
    save_state()
    return warns[str(user_id)]

# ================= BAN =================

@router.message(lambda m: is_command(m.text) and m.text.split()[0][1:] == "ban")
async def ban_cmd(message: Message, bot: Bot):
    if not message.reply_to_message:
        return await message.reply("Reply yapman lazım.")

    if not await require_admin(message, bot):
        return

    args = message.text.split()
    target = message.reply_to_message.from_user.id

    if len(args) == 2:
        seconds = parse_time(args[1])
        if seconds:
            until = datetime.utcnow() + timedelta(seconds=seconds)
            await bot.ban_chat_member(message.chat.id, target, until_date=until)
            return await message.reply(f"{seconds} saniye banlandı.")
    
    await bot.ban_chat_member(message.chat.id, target)
    await message.reply("Süresiz banlandı ✅")

# ================= UNBAN =================

@router.message(lambda m: is_command(m.text) and m.text.split()[0][1:] == "unban")
async def unban_cmd(message: Message, bot: Bot):
    if not message.reply_to_message:
        return
    if not await require_admin(message, bot):
        return
    await bot.unban_chat_member(message.chat.id, message.reply_to_message.from_user.id)
    await message.reply("Ban kaldırıldı ✅")

# ================= MUTE =================

@router.message(lambda m: is_command(m.text) and m.text.split()[0][1:] == "mute")
async def mute_cmd(message: Message, bot: Bot):
    if not message.reply_to_message:
        return await message.reply("Reply yap.")

    if not await require_admin(message, bot):
        return

    args = message.text.split()
    target = message.reply_to_message.from_user.id

    if len(args) == 2:
        seconds = parse_time(args[1])
        if seconds:
            until = datetime.utcnow() + timedelta(seconds=seconds)
            await bot.restrict_chat_member(
                message.chat.id,
                target,
                permissions=mute_perm(),
                until_date=until
            )
            return await message.reply("Süreli mute ✅")

    await bot.restrict_chat_member(
        message.chat.id,
        target,
        permissions=mute_perm()
    )
    await message.reply("Süresiz mute ✅")

# ================= UNMUTE =================

@router.message(lambda m: is_command(m.text) and m.text.split()[0][1:] == "unmute")
async def unmute_cmd(message: Message, bot: Bot):
    if not message.reply_to_message:
        return
    if not await require_admin(message, bot):
        return
    await bot.restrict_chat_member(
        message.chat.id,
        message.reply_to_message.from_user.id,
        permissions=ChatPermissions(can_send_messages=True)
    )
    await message.reply("Mute kaldırıldı ✅")

# ================= WARN COMMAND =================

@router.message(lambda m: is_command(m.text) and m.text.split()[0][1:] == "warn")
async def warn_cmd(message: Message, bot: Bot):
    if not message.reply_to_message:
        return
    if not await require_admin(message, bot):
        return

    target = message.reply_to_message.from_user.id
    count = await add_warn(message.chat.id, target)

    if count >= 3:
        await bot.restrict_chat_member(
            message.chat.id,
            target,
            permissions=mute_perm()
        )
        await message.reply("3 warn oldu, otomatik mute ✅")
    else:
        await message.reply(f"Warn verildi. Toplam: {count}")

# ================= FLOOD SYSTEM =================

@router.message()
async def flood_system(message: Message, bot: Bot):
    if message.chat.type == "private":
        return

    chat = get_chat(message.chat.id)
    flood = chat["flood"]

    if not flood["enabled"]:
        return

    key = (message.chat.id, message.from_user.id)
    now = asyncio.get_event_loop().time()
    FLOOD[key].append(now)

    while FLOOD[key] and now - FLOOD[key][0] > flood["seconds"]:
        FLOOD[key].popleft()

    if len(FLOOD[key]) >= flood["limit"]:
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            await message.delete()
            await bot.restrict_chat_member(
                message.chat.id,
                message.from_user.id,
                permissions=mute_perm(),
                until_date=datetime.utcnow() + timedelta(seconds=60)
            )

# ================= ANTIRAID =================

@router.message()
async def antiraid_system(message: Message, bot: Bot):
    if message.chat.type == "private":
        return

    chat = get_chat(message.chat.id)

    if not chat["antiraid"]:
        return

    if message.new_chat_members:
        now = asyncio.get_event_loop().time()
        JOINS[message.chat.id].append(now)

        while JOINS[message.chat.id] and now - JOINS[message.chat.id][0] > 30:
            JOINS[message.chat.id].popleft()

        if len(JOINS[message.chat.id]) >= 5:
            await bot.set_chat_permissions(message.chat.id, permissions=mute_perm())
            await message.reply("⚠️ Raid algılandı! Grup geçici kilitlendi.")

# ================= MESSAGE STATS =================

def get_stats(chat_id: int):
    chat = get_chat(chat_id)
    return chat.setdefault("stats", {})

@router.message()
async def message_counter(message: Message):
    if message.chat.type == "private":
        return
    stats = get_stats(message.chat.id)
    uid = str(message.from_user.id)
    stats[uid] = stats.get(uid, 0) + 1
    save_state()

# ================= DAILY REPORT =================

async def daily_report(bot: Bot):
    while True:
        now = datetime.now()
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if now > target:
            target = target + timedelta(days=1)

        await asyncio.sleep((target - now).total_seconds())

        for chat_id in STATE["chats"]:
            stats = STATE["chats"][chat_id].get("stats", {})
            if not stats:
                continue

            sorted_users = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:20]

            text = "📊 <b>Günlük Aktivite Raporu</b>\n\n"
            active_count = len(stats)

            for i, (uid, count) in enumerate(sorted_users, 1):
                try:
                    member = await bot.get_chat_member(int(chat_id), int(uid))
                    name = member.user.full_name
                except:
                    name = uid
                text += f"{i}. {escape(name)} - {count} mesaj\n"

            text += f"\n👥 Aktif üye: {active_count}"

            try:
                await bot.send_message(int(chat_id), text, parse_mode=ParseMode.HTML)
            except:
                pass

            STATE["chats"][chat_id]["stats"] = {}

        save_state()

# ================= WELCOME / GOODBYE =================

@router.message(lambda m: is_command(m.text) and m.text.split()[0][1:] == "setwelcome")
async def set_welcome(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return
    get_chat(message.chat.id)["welcome"] = text[1]
    save_state()
    await message.reply("Hoşgeldin mesajı ayarlandı ✅")

@router.message(lambda m: is_command(m.text) and m.text.split()[0][1:] == "setgoodbye")
async def set_goodbye(message: Message, bot: Bot):
    if not await require_admin(message, bot):
        return
    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return
    get_chat(message.chat.id)["goodbye"] = text[1]
    save_state()
    await message.reply("Çıkış mesajı ayarlandı ✅")

@router.message()
async def welcome_goodbye(message: Message):
    if message.new_chat_members:
        chat = get_chat(message.chat.id)
        if chat["welcome"]:
            await message.reply(escape(chat["welcome"]))
    if message.left_chat_member:
        chat = get_chat(message.chat.id)
        if chat["goodbye"]:
            await message.reply(escape(chat["goodbye"]))

# ================= COMMANDS BUTTON =================

@router.callback_query(lambda c: c.data == "commands")
async def commands_panel(call: CallbackQuery):
    text = """
<b>Komutlar</b>

• /ban 10m (reply)
• /ban (süresiz)
• /mute 5m
• /warn
• /setwelcome mesaj
• /setgoodbye mesaj
• /unban
• /unmute

Tüm komutlar . veya / ile çalışır.
"""
    await call.message.edit_text(text, parse_mode=ParseMode.HTML)

# ================= MY GROUPS =================

@router.callback_query(lambda c: c.data == "mygroups")
async def my_groups(call: CallbackQuery, bot: Bot):
    groups = []
    for chat_id in STATE["chats"]:
        try:
            member = await bot.get_chat_member(int(chat_id), call.from_user.id)
            if member.status in ("administrator", "creator", "member"):
                chat = await bot.get_chat(int(chat_id))
                groups.append(chat.title)
        except:
            continue

    if not groups:
        return await call.answer("Ortak grup yok.")

    text = "<b>Ortak Gruplar:</b>\n\n"
    for g in groups[:20]:
        text += f"• {escape(g)}\n"

    await call.message.edit_text(text, parse_mode=ParseMode.HTML)

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
