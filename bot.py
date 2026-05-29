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
STATE: Dict[str, Any] = {"chats": {}, "users": {}}

FLOOD = defaultdict(lambda: deque())
URL_RE = re.compile(r"(https?://|t\.me/)", re.IGNORECASE)

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
        "goodbye": None
    })

# ================= PERMISSION =================

async def is_admin(bot: Bot, chat_id: int, user_id: int):
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")

def mute_perm():
    return ChatPermissions(can_send_messages=False)

# ================= PREMIUM START PANEL =================

def premium_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ KGB GUARD PRO’yu Gruba Ekle 🚀",
            url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
        )],
        [InlineKeyboardButton(text="📖 Komutlar", callback_data="commands")],
        [InlineKeyboardButton(text="📢 Kanal Destek", url="https://t.me/KGBotomasyon")]
    ])

@router.message(CommandStart())
async def start_cmd(message: Message):
    text = f"""
✨ <b>{BOT_NAME}</b> ✨

🛡️ Gelişmiş Premium Koruma Sistemi  
👥 Aylık aktif kullanıcı: <b>{MONTHLY_USERS}</b>

🚀 Grubunu korumaya hemen başla!
"""
    await message.reply(text, reply_markup=premium_menu(), parse_mode=ParseMode.HTML)

# ================= COMMANDS PANEL =================

@router.callback_query(lambda c: c.data == "commands")
async def commands_panel(call: CallbackQuery):
    text = """
<b>🛡️ Moderasyon Komutları</b>

• /ban 10m (reply)
• /ban (süresiz)
• /mute 5m
• /warn
• /unban
• /unmute
• /setwelcome mesaj
• /setgoodbye mesaj

✅ Tüm komutlar / veya . ile çalışır
"""
    await call.message.edit_text(text, parse_mode=ParseMode.HTML)

# ================= MESSAGE HANDLER (TEK MERKEZ) =================

TIME_RE = re.compile(r"(\d+)([smhd])")

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
@router.message()
async def main_handler(message: Message, bot: Bot):

    if message.chat.type == "private":
        return

    chat = get_chat(message.chat.id)

    # ================= MESSAGE COUNT =================
    uid = str(message.from_user.id)
    chat["stats"][uid] = chat["stats"].get(uid, 0) + 1

    # ================= FLOOD SYSTEM =================
    key = (message.chat.id, message.from_user.id)
    now = asyncio.get_event_loop().time()
    FLOOD[key].append(now)

    while FLOOD[key] and now - FLOOD[key][0] > 5:
        FLOOD[key].popleft()

    if len(FLOOD[key]) >= 6:
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            await message.delete()
            await bot.restrict_chat_member(
                message.chat.id,
                message.from_user.id,
                permissions=mute_perm(),
                until_date=datetime.utcnow() + timedelta(seconds=60)
            )
            return

    # ================= WELCOME / GOODBYE =================
    if message.new_chat_members:
        if chat["welcome"]:
            await message.reply(escape(chat["welcome"]))

    if message.left_chat_member:
        if chat["goodbye"]:
            await message.reply(escape(chat["goodbye"]))

    # ================= COMMAND CHECK =================
    if not message.text:
        save_state()
        return

    if not (message.text.startswith("/") or message.text.startswith(".")):
        save_state()
        return

    cmd_parts = message.text.split()
    cmd = cmd_parts[0][1:].lower()

    # ================= ADMIN CHECK FUNCTION =================
    async def check_admin():
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            await message.reply("aq aveli sence yetkin varmı?")
            return False
        return True

    # ================= BAN =================
    if cmd == "ban":
        if not message.reply_to_message:
            return
        if not await check_admin():
            return

        target = message.reply_to_message.from_user.id

        if len(cmd_parts) == 2:
            seconds = parse_time(cmd_parts[1])
            if seconds:
                until = datetime.utcnow() + timedelta(seconds=seconds)
                await bot.ban_chat_member(message.chat.id, target, until_date=until)
                await message.reply(f"✅ {seconds} saniye banlandı")
                save_state()
                return

        await bot.ban_chat_member(message.chat.id, target)
        await message.reply("✅ Süresiz banlandı")

    # ================= UNBAN =================
    if cmd == "unban":
        if not message.reply_to_message:
            return
        if not await check_admin():
            return
        await bot.unban_chat_member(message.chat.id, message.reply_to_message.from_user.id)
        await message.reply("✅ Ban kaldırıldı")

    # ================= MUTE =================
    if cmd == "mute":
        if not message.reply_to_message:
            return
        if not await check_admin():
            return

        target = message.reply_to_message.from_user.id

        if len(cmd_parts) == 2:
            seconds = parse_time(cmd_parts[1])
            if seconds:
                until = datetime.utcnow() + timedelta(seconds=seconds)
                await bot.restrict_chat_member(
                    message.chat.id,
                    target,
                    permissions=mute_perm(),
                    until_date=until
                )
                await message.reply("✅ Süreli mute atıldı")
                save_state()
                return

        await bot.restrict_chat_member(
            message.chat.id,
            target,
            permissions=mute_perm()
        )
        await message.reply("✅ Süresiz mute atıldı")

    # ================= UNMUTE =================
    if cmd == "unmute":
        if not message.reply_to_message:
            return
        if not await check_admin():
            return

        await bot.restrict_chat_member(
            message.chat.id,
            message.reply_to_message.from_user.id,
            permissions=ChatPermissions(can_send_messages=True)
        )
        await message.reply("✅ Mute kaldırıldı")

    # ================= WARN SYSTEM =================
    if cmd == "warn":
        if not message.reply_to_message:
            return
        if not await check_admin():
            return

        target = message.reply_to_message.from_user.id
        warns = chat["warns"]
        warns[str(target)] = warns.get(str(target), 0) + 1

        if warns[str(target)] >= 3:
            await bot.restrict_chat_member(
                message.chat.id,
                target,
                permissions=mute_perm()
            )
            warns[str(target)] = 0
            await message.reply("⚠️ 3 warn oldu, otomatik mute ✅")
        else:
            await message.reply(f"⚠️ Warn verildi ({warns[str(target)]}/3)")

    # ================= ANTILINK =================
    if URL_RE.search(message.text):
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            await message.delete()

    # ================= ANTI RAID =================
    if message.new_chat_members:
        joins = chat.setdefault("joins", [])
        joins.append(datetime.utcnow().timestamp())

        joins = [j for j in joins if datetime.utcnow().timestamp() - j < 30]
        chat["joins"] = joins

        if len(joins) >= 5:
            await bot.set_chat_permissions(message.chat.id, permissions=mute_perm())
            await message.reply("🚨 Raid algılandı! Grup kilitlendi.")

    save_state()
    # ========= BAN =========
    if cmd == "ban":
        if not message.reply_to_message:
            return
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("aq aveli sence yetkin varmı?")
        await bot.ban_chat_member(message.chat.id, message.reply_to_message.from_user.id)
        await message.reply("✅ Banlandı")

    # ========= MUTE =========
    if cmd == "mute":
        if not message.reply_to_message:
            return
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("aq aveli sence yetkin varmı?")
        await bot.restrict_chat_member(
            message.chat.id,
            message.reply_to_message.from_user.id,
            permissions=mute_perm()
        )
        await message.reply("✅ Mute atıldı")

    # ========= WARN =========
    if cmd == "warn":
        if not message.reply_to_message:
            return
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("aq aveli sence yetkin varmı?")
        target = message.reply_to_message.from_user.id
        warns = chat["warns"]
        warns[str(target)] = warns.get(str(target), 0) + 1
        if warns[str(target)] >= 3:
            await bot.restrict_chat_member(
                message.chat.id,
                target,
                permissions=mute_perm()
            )
            await message.reply("⚠️ 3 warn oldu, otomatik mute ✅")
        else:
            await message.reply(f"Warn verildi ({warns[str(target)]}/3)")

    # ========= SET WELCOME =========
    if cmd == "setwelcome":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            return
        chat["welcome"] = parts[1]
        await message.reply("✅ Hoşgeldin mesajı ayarlandı")

    # ========= SET GOODBYE =========
    if cmd == "setgoodbye":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            return
        chat["goodbye"] = parts[1]
        await message.reply("✅ Çıkış mesajı ayarlandı")

    save_state()

# ================= DAILY REPORT =================

async def daily_report(bot: Bot):
    while True:
        now = datetime.now()
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)

        await asyncio.sleep((next_run - now).total_seconds())

        for chat_id, data in STATE["chats"].items():
            stats = data.get("stats", {})
            if not stats:
                continue

            top = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:20]

            text = "📊 <b>Günlük Aktivite Raporu</b>\n\n"

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
