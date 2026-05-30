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

TOKEN = os.getenv("BOT_TOKEN")
STATE_PATH = os.getenv("STATE_PATH", "state.json")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN yok!")

BOT_NAME = "KGB GUARD ULTIMATE"
BOT_USERNAME = "KGBKORUMABOT"
BOT_START_TIME = datetime.utcnow()

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
        "lang": "tr",
        "mods": [],
        "warns": {},
        "notes": {},
        "filters": {},
        "stats": {},
        "welcome": None,
        "goodbye": None,
        "log": None,
        "antilink": False,
        "lock_media": False,
        "lock_sticker": False,
        "flood": {"limit": 6, "seconds": 5},
        "raid": {"limit": 5, "seconds": 30},
        "joins": [],
        "captcha": False,
        "captcha_pending": {}
    })

def get_total_users():
    users = set()
    for chat in STATE["chats"].values():
        users.update(chat.get("stats", {}).keys())
    return len(users)

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

# ================= START PANEL =================

@router.message(CommandStart())
async def start_cmd(message: Message, bot: Bot):

    uptime = datetime.utcnow() - BOT_START_TIME
    uptime_str = str(uptime).split(".")[0]

    total_groups = len(STATE["chats"])
    total_users = get_total_users()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Botu Gruba Ekle", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")],
        [InlineKeyboardButton(text="📖 Komutlar", callback_data="commands_menu")],
        [InlineKeyboardButton(text="📊 Gruplarım", callback_data="my_groups")],
        [InlineKeyboardButton(text="📢 Destek Kanalı", url="https://t.me/KGBotomasyon")]
    ])

    await message.reply(
        f"👑 <b>{BOT_NAME}</b>\n\n"
        f"🟢 Sistem Durumu: ONLINE\n"
        f"🛡 Koruma Modu: AKTİF\n\n"
        f"👥 Aktif Kullanıcı: {total_users}\n"
        f"📂 Kayıtlı Grup: {total_groups}\n"
        f"⏳ Uptime: {uptime_str}\n\n"
        "Profesyonel Grup Güvenlik Sistemi",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )

# ================= MAIN HANDLER =================

@router.message()
async def main_handler(message: Message, bot: Bot):

    if message.chat.type == "private":
        return

    chat = get_chat(message.chat.id)

    # GLOBAL BLACKLIST
    if message.from_user.id in STATE["blacklist"]:
        try:
            await bot.ban_chat_member(message.chat.id, message.from_user.id)
        except:
            pass
        return

    # MESSAGE COUNT
    uid = str(message.from_user.id)
    chat["stats"][uid] = chat["stats"].get(uid, 0) + 1

    # FLOOD SYSTEM
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

    # RAID SYSTEM
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
        if member.status not in ("administrator", "creator"):
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

    async def check_perm():
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            await message.reply("Avel sence yetkin var mı? 😏")
            return False
        return True

    # ================= MOD ROLE =================
    if cmd == "addmod":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("Sadece admin mod ekleyebilir.")
        if not message.reply_to_message:
            return
        target = message.reply_to_message.from_user.id
        if target not in chat["mods"]:
            chat["mods"].append(target)
            await message.reply("✅ Mod eklendi")

    if cmd == "delmod":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("Sadece admin mod silebilir.")
        if not message.reply_to_message:
            return
        target = message.reply_to_message.from_user.id
        if target in chat["mods"]:
            chat["mods"].remove(target)
            await message.reply("✅ Mod silindi")

    # ================= BAN =================
    if cmd == "ban":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return

        target = message.reply_to_message.from_user.id
        target_member = await bot.get_chat_member(message.chat.id, target)

        if target_member.status in ("administrator", "creator"):
            return await message.reply("Yöneticiyi banlayamam 🚫")

        if len(cmd_parts) == 2:
            sec = parse_time(cmd_parts[1])
            if sec:
                until = datetime.utcnow() + timedelta(seconds=sec)
                await bot.ban_chat_member(message.chat.id, target, until_date=until)
                await message.reply("Süreli ban ✅")
                save_state()
                return

        await bot.ban_chat_member(message.chat.id, target)
        await message.reply("Süresiz ban ✅")

    # ================= KICK =================
    if cmd == "kick":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return

        target = message.reply_to_message.from_user.id
        target_member = await bot.get_chat_member(message.chat.id, target)

        if target_member.status in ("administrator", "creator"):
            return await message.reply("Yöneticiyi atamam 🚫")

        await bot.ban_chat_member(message.chat.id, target)
        await bot.unban_chat_member(message.chat.id, target)
        await message.reply("Kullanıcı atıldı ✅")

    # ================= MUTE =================
    if cmd == "mute":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return

        target = message.reply_to_message.from_user.id
        target_member = await bot.get_chat_member(message.chat.id, target)

        if target_member.status in ("administrator", "creator"):
            return await message.reply("Yöneticiyi susturamam 🚫")

        await bot.restrict_chat_member(
            message.chat.id,
            target,
            permissions=mute_perm()
        )
        await message.reply("Susturuldu ✅")

    save_state()

    # ================= WARN =================
    if cmd == "warn":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return

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

    # ================= NOTES =================
    if cmd == "save":
        if not await check_perm():
            return
        if len(cmd_parts) < 3:
            return
        name = cmd_parts[1].lower()
        content = message.text.split(maxsplit=2)[2]
        chat["notes"][name] = content
        await message.reply("Not kaydedildi ✅")

    if cmd == "get":
        if len(cmd_parts) < 2:
            return
        name = cmd_parts[1].lower()
        if name in chat["notes"]:
            await message.reply(chat["notes"][name])

    if cmd == "delnote":
        if not await check_perm():
            return
        if len(cmd_parts) < 2:
            return
        name = cmd_parts[1].lower()
        if name in chat["notes"]:
            del chat["notes"][name]
            await message.reply("Not silindi ✅")

    # ================= FILTER SYSTEM =================
    if cmd == "filter":
        if not await check_perm():
            return
        if len(cmd_parts) < 3:
            return
        key_word = cmd_parts[1].lower()
        response = message.text.split(maxsplit=2)[2]
        chat["filters"][key_word] = response
        await message.reply("Filtre kaydedildi ✅")

    if cmd == "stop":
        if not await check_perm():
            return
        if len(cmd_parts) < 2:
            return
        key_word = cmd_parts[1].lower()
        if key_word in chat["filters"]:
            del chat["filters"][key_word]
            await message.reply("Filtre silindi ✅")

    # ================= CAPTCHA =================
    if cmd == "captcha":
        if not await check_perm():
            return
        chat["captcha"] = not chat.get("captcha", False)
        await message.reply(f"Captcha {'Açık ✅' if chat['captcha'] else 'Kapalı ❌'}")

    if chat.get("captcha") and message.new_chat_members:
        for user in message.new_chat_members:
            try:
                await bot.restrict_chat_member(
                    message.chat.id,
                    user.id,
                    permissions=mute_perm()
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Doğrula", callback_data=f"verify_{user.id}")]
                ])
                chat["captcha_pending"][str(user.id)] = True
                await message.reply("Doğrulamak için butona bas.", reply_markup=kb)
            except:
                pass

    save_state()


# ================= CAPTCHA VERIFY =================

@router.callback_query(lambda c: c.data.startswith("verify_"))
async def verify_user(call: CallbackQuery, bot: Bot):
    user_id = int(call.data.split("_")[1])
    chat = get_chat(call.message.chat.id)

    if str(user_id) in chat["captcha_pending"]:
        try:
            await bot.restrict_chat_member(
                call.message.chat.id,
                user_id,
                permissions=ChatPermissions(can_send_messages=True)
            )
        except:
            pass

        del chat["captcha_pending"][str(user_id)]
        await call.message.delete()
        await call.answer("Doğrulandı ✅")

    save_state()


# ================= DAILY REPORT =================

async def daily_report(bot: Bot):
    while True:
        try:
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

                text = "📊 Günlük Aktivite Raporu\n\n"

                for i, (uid, count) in enumerate(top, 1):
                    text += f"{i}. {uid} — {count} mesaj\n"

                text += f"\n👥 Aktif üye: {len(stats)}"

                try:
                    await bot.send_message(int(chat_id), text)
                except:
                    pass

                STATE["chats"][chat_id]["stats"] = {}

            save_state()

        except:
            await asyncio.sleep(10)


# ================= AUTO RAID UNLOCK =================

async def auto_unlock(bot: Bot):
    while True:
        try:
            await asyncio.sleep(30)

            for chat_id, data in STATE["chats"].items():
                joins = data.get("joins", [])

                if not joins:
                    continue

                raid_seconds = data["raid"]["seconds"]

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

        except:
            await asyncio.sleep(10)


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
    asyncio.create_task(auto_unlock(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
