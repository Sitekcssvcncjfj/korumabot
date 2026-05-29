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

STATE: Dict[str, Any] = {
    "chats": {},
    "blacklist": []
}

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
        "joins": []
    })

def get_blacklist():
    return STATE.setdefault("blacklist", [])

# ================= PERMISSIONS =================

async def is_admin(bot: Bot, chat_id: int, user_id: int):
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")

async def has_permission(bot: Bot, chat_id: int, user_id: int):
    if await is_admin(bot, chat_id, user_id):
        return True
    chat = get_chat(chat_id)
    return user_id in chat["mods"]

def mute_perm():
    return ChatPermissions(can_send_messages=False)

# ================= LOG =================

async def send_log(bot: Bot, chat_id: int, text: str):
    chat = get_chat(chat_id)
    if chat.get("log_channel"):
        try:
            await bot.send_message(chat["log_channel"], text, parse_mode=ParseMode.HTML)
        except:
            pass

# ================= PREMIUM PANEL =================

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

🛡️ Ultimate Premium Koruma Sistemi
👥 Aylık aktif kullanıcı: <b>{MONTHLY_USERS}</b>

🚀 Grubunu korumaya hemen başla!
"""
    await message.reply(text, reply_markup=premium_menu(), parse_mode=ParseMode.HTML)

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
• /addmod
• /delmod
• /setflood 6 5
• /setraid 5 30
• /blacklist
• /setlog -100xxxx

✅ Tüm komutlar / veya . ile çalışır
"""
    await call.message.edit_text(text, parse_mode=ParseMode.HTML)

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

# ================= MAIN MESSAGE HANDLER =================

@router.message()
async def main_handler(message: Message, bot: Bot):

    if message.chat.type == "private":
        return

    chat = get_chat(message.chat.id)

    # ================= GLOBAL BLACKLIST =================
    if message.from_user.id in get_blacklist():
        try:
            await bot.ban_chat_member(message.chat.id, message.from_user.id)
        except:
            pass
        return

    # ================= MESSAGE COUNT =================
    uid = str(message.from_user.id)
    chat["stats"][uid] = chat["stats"].get(uid, 0) + 1

    # ================= FLOOD SYSTEM =================
    key = (message.chat.id, message.from_user.id)
    now = asyncio.get_event_loop().time()
    FLOOD[key].append(now)

    flood_limit = chat["flood"]["limit"]
    flood_seconds = chat["flood"]["seconds"]

    while FLOOD[key] and now - FLOOD[key][0] > flood_seconds:
        FLOOD[key].popleft()

    if len(FLOOD[key]) >= flood_limit:
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            await message.delete()
            await bot.restrict_chat_member(
                message.chat.id,
                message.from_user.id,
                permissions=mute_perm(),
                until_date=datetime.utcnow() + timedelta(seconds=60)
            )
            await send_log(bot, message.chat.id, f"⚠️ Flood mute: {message.from_user.id}")
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

    async def check_perm():
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            await message.reply("aq aveli sence yetkin varmı?")
            return False
        return True

    # ================= MOD ROLE =================
    if cmd == "addmod":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("aq aveli sence yetkin varmı?")
        if not message.reply_to_message:
            return
        target = message.reply_to_message.from_user.id
        if target not in chat["mods"]:
            chat["mods"].append(target)
            await message.reply("✅ Mod eklendi")
            await send_log(bot, message.chat.id, f"👑 Mod eklendi: {target}")

    if cmd == "delmod":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("aq aveli sence yetkin varmı?")
        if not message.reply_to_message:
            return
        target = message.reply_to_message.from_user.id
        if target in chat["mods"]:
            chat["mods"].remove(target)
            await message.reply("✅ Mod silindi")
            await send_log(bot, message.chat.id, f"❌ Mod silindi: {target}")

    # ================= BAN =================
    if cmd == "ban":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return
        target = message.reply_to_message.from_user.id

        if len(cmd_parts) == 2:
            seconds = parse_time(cmd_parts[1])
            if seconds:
                until = datetime.utcnow() + timedelta(seconds=seconds)
                await bot.ban_chat_member(message.chat.id, target, until_date=until)
                await message.reply("✅ Süreli ban")
                await send_log(bot, message.chat.id, f"🚫 Süreli ban: {target}")
                save_state()
                return

        await bot.ban_chat_member(message.chat.id, target)
        await message.reply("✅ Süresiz ban")
        await send_log(bot, message.chat.id, f"🚫 Süresiz ban: {target}")

    # ================= UNBAN =================
    if cmd == "unban":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return
        target = message.reply_to_message.from_user.id
        await bot.unban_chat_member(message.chat.id, target)
        await message.reply("✅ Ban kaldırıldı")
        await send_log(bot, message.chat.id, f"✅ Unban: {target}")

    # ================= MUTE =================
    if cmd == "mute":
        if not message.reply_to_message:
            return
        if not await check_perm():
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
                await message.reply("✅ Süreli mute")
                await send_log(bot, message.chat.id, f"🔇 Süreli mute: {target}")
                save_state()
                return

        await bot.restrict_chat_member(
            message.chat.id,
            target,
            permissions=mute_perm()
        )
        await message.reply("✅ Süresiz mute")
        await send_log(bot, message.chat.id, f"🔇 Süresiz mute: {target}")

    # ================= UNMUTE =================
    if cmd == "unmute":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return
        target = message.reply_to_message.from_user.id
        await bot.restrict_chat_member(
            message.chat.id,
            target,
            permissions=ChatPermissions(can_send_messages=True)
        )
        await message.reply("✅ Mute kaldırıldı")
        await send_log(bot, message.chat.id, f"✅ Unmute: {target}")

    # ================= WARN =================
    if cmd == "warn":
        if not message.reply_to_message:
            return
        if not await check_perm():
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
            await send_log(bot, message.chat.id, f"⚠️ Auto mute (3 warn): {target}")
        else:
            await message.reply(f"⚠️ Warn ({warns[str(target)]}/3)")
            await send_log(bot, message.chat.id, f"⚠️ Warn: {target}")

    # ================= SET FLOOD =================
    if cmd == "setflood":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return
        if len(cmd_parts) != 3:
            return await message.reply("Kullanım: /setflood 6 5")
        chat["flood"]["limit"] = int(cmd_parts[1])
        chat["flood"]["seconds"] = int(cmd_parts[2])
        await message.reply("✅ Flood ayarlandı")

    # ================= SET RAID =================
    if cmd == "setraid":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return
        if len(cmd_parts) != 3:
            return await message.reply("Kullanım: /setraid 5 30")
        chat["raid"]["limit"] = int(cmd_parts[1])
        chat["raid"]["seconds"] = int(cmd_parts[2])
        await message.reply("✅ Raid ayarlandı")

    # ================= SET LOG =================
    if cmd == "setlog":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return
        if len(cmd_parts) != 2:
            return
        chat["log_channel"] = int(cmd_parts[1])
        await message.reply("✅ Log kanalı ayarlandı")

    # ================= BLACKLIST =================
    if cmd == "blacklist":
        if not message.reply_to_message:
            return
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("aq aveli sence yetkin varmı?")
        target = message.reply_to_message.from_user.id
        bl = get_blacklist()
        if target not in bl:
            bl.append(target)
            await bot.ban_chat_member(message.chat.id, target)
            await message.reply("🚫 Global kara listeye alındı")
            await send_log(bot, message.chat.id, f"🚫 Blacklist: {target}")

    if cmd == "unblacklist":
        if not message.reply_to_message:
            return
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("aq aveli sence yetkin varmı?")
        target = message.reply_to_message.from_user.id
        bl = get_blacklist()
        if target in bl:
            bl.remove(target)
            await message.reply("✅ Kara listeden çıkarıldı")
            await send_log(bot, message.chat.id, f"✅ UnBlacklist: {target}")

    # ================= ANTILINK =================
    if URL_RE.search(message.text):
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            await message.delete()

    # ================= ANTI RAID =================
    if message.new_chat_members:
        joins = chat["joins"]
        joins.append(datetime.utcnow().timestamp())

        raid_limit = chat["raid"]["limit"]
        raid_seconds = chat["raid"]["seconds"]

        joins = [j for j in joins if datetime.utcnow().timestamp() - j < raid_seconds]
        chat["joins"] = joins

        if len(joins) >= raid_limit:
            await bot.set_chat_permissions(message.chat.id, permissions=mute_perm())
            await message.reply("🚨 Raid algılandı! Grup kilitlendi.")
            await send_log(bot, message.chat.id, "🚨 Raid kilidi")

    save_state()

# ================= DAILY REPORT =================

async def daily_report(bot: Bot):
    while True:
        now = datetime.now()
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if now >= next_run:
            next_run += timedelta(days=1)

        wait_seconds = (next_run - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        for chat_id, data in STATE["chats"].items():
            stats = data.get("stats", {})
            if not stats:
                continue

            top_users = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:20]

            text = "📊 <b>Günlük Aktivite Raporu</b>\n\n"

            for i, (uid, count) in enumerate(top_users, 1):
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

            # Günlük reset
            STATE["chats"][chat_id]["stats"] = {}

        save_state()


# ================= AUTO UNLOCK RAID =================

async def auto_unlock(bot: Bot):
    while True:
        await asyncio.sleep(30)

        for chat_id, data in STATE["chats"].items():
            joins = data.get("joins", [])

            if not joins:
                continue

            raid_seconds = data["raid"]["seconds"]

            # Eğer son join raid süresini geçtiyse kilidi aç
            if joins and (datetime.utcnow().timestamp() - joins[-1]) > raid_seconds:
                try:
                    await bot.set_chat_permissions(
                        int(chat_id),
                        permissions=ChatPermissions(
                            can_send_messages=True,
                            can_send_media_messages=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True
                        )
                    )
                except:
                    pass

                data["joins"] = []
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

    # Background Tasks
    asyncio.create_task(daily_report(bot))
    asyncio.create_task(auto_unlock(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
