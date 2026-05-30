import os
import re
import json
import asyncio
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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
from aiogram.client.default import DefaultBotProperties

# ================= CONFIG =================

TOKEN = os.getenv("BOT_TOKEN")
STATE_PATH = os.getenv("STATE_PATH", "state.json")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN yok!")

BOT_NAME = "KGB GUARD ULTIMATE"

router = Router()
STATE: Dict[str, Any] = {"chats": {}, "blacklist": []}

@router.callback_query(lambda c: c.data == "back_main")
async def back_main(call: CallbackQuery):

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="➕ Beni Gruba Ekle 🚀",
                url="https://t.me/KGBKORUMABOT?startgroup=true"
            )
        ],
        [
            InlineKeyboardButton(
                text="📖 Komutlar",
                callback_data="commands_menu"
            )
        ],
        [
            InlineKeyboardButton(
                text="📊 Gruplarım",
                callback_data="my_groups"
            )
        ],
        [
            InlineKeyboardButton(
                text="📢 Kanal Destek",
                url="https://t.me/KGBotomasyon"
            )
        ]
    ])

    await call.message.edit_text(
        "👋 <b>KGB GUARD ULTIMATE</b>\n\n"
        "🛡 Profesyonel Grup Koruma Botu\n"
        "✅ Rose mantığı\n"
        "✅ Mod sistemi\n"
        "✅ AntiSpam\n\n"
        "Aşağıdan işlem seç 👇",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )

@router.callback_query(lambda c: c.data == "commands_menu")
async def commands_menu(call: CallbackQuery):

    text = """
<b>🛡 Moderasyon Komutları</b>

<b>🔨 Ban</b>
/ban (reply)
 /ban 10m

<b>🔇 Mute</b>
/mute
/mute 5m

<b>⚠ Warn</b>
/warn (3 warn = auto mute)

<b>👑 Mod Sistemi</b>
/addmod
/delmod

<b>📦 Not</b>
/save isim içerik
/get isim

<b>🎯 Filtre</b>
/filter kelime cevap
/stop kelime

<b>🌊 Flood</b>
/setflood 6 5

<b>🚨 Raid</b>
/setraid 5 30

<b>🔐 Kilit</b>
Antilink
Medya kilidi
Sticker kilidi

✅ Tüm komutlar / veya . ile çalışır.
"""

    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Geri", callback_data="back_main")]
        ])
    )

@router.callback_query(lambda c: c.data == "my_groups")
async def my_groups(call: CallbackQuery, bot: Bot):

    user_id = call.from_user.id
    common_groups = []

    for chat_id in STATE["chats"]:
        try:
            member = await bot.get_chat_member(int(chat_id), user_id)
            if member.status in ("administrator", "creator", "member"):
                chat = await bot.get_chat(int(chat_id))
                common_groups.append(chat.title)
        except:
            continue

    if not common_groups:
        text = "Bot ile ortak grubun yok."
    else:
        text = "<b>Ortak Gruplar:</b>\n\n"
        for g in common_groups[:20]:
            text += f"• {escape(g)}\n"

    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Geri", callback_data="back_main")]
        ])
    )

@router.callback_query(lambda c: c.data == "back_main")
async def back_main(call: CallbackQuery, bot: Bot):
    await start_cmd(call.message, bot)


FLOOD = defaultdict(lambda: deque())

URL_RE = re.compile(r"(https?://|t\.me/)", re.IGNORECASE)
TIME_RE = re.compile(r"(\d+)([smhd])")

# ================= LANGUAGE =================

LANG = {
    "tr": {
        "no_perm": "Bu komut için yetkin yok.",
        "admin_only": "Sadece yöneticiler kullanabilir.",
        "muted": "Kullanıcı susturuldu ✅",
        "banned": "Kullanıcı yasaklandı ✅",
        "warn": "Uyarı verildi",
        "captcha": "Doğrulamak için butona bas."
    },
    "en": {
        "no_perm": "You don't have permission.",
        "admin_only": "Admin only.",
        "muted": "User muted ✅",
        "banned": "User banned ✅",
        "warn": "Warning issued",
        "captcha": "Click button to verify."
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

# ================= PERMISSION =================

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

# ================= LOG =================

async def send_log(bot: Bot, chat_id: int, text: str):
    log_id = get_chat(chat_id).get("log")
    if log_id:
        try:
            await bot.send_message(log_id, text, parse_mode=ParseMode.HTML)
        except:
            pass

# ================= MAIN HANDLER =================

@router.message()
async def main_handler(message: Message, bot: Bot):

    if message.chat.type == "private":
        return

    chat = get_chat(message.chat.id)
    lang = LANG[chat["lang"]]

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

    # ANTILINK
    if chat["antilink"] and message.text and URL_RE.search(message.text):
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            await message.delete()
            return

    # MEDIA LOCK
    if chat["lock_media"] and (
        message.photo or message.video or message.document
    ):
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            await message.delete()
            return

    # STICKER LOCK
    if chat["lock_sticker"] and message.sticker:
        if not await has_permission(bot, message.chat.id, message.from_user.id):
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
            await message.reply(lang["no_perm"])
            return False
        return True

    # ================= MOD ROLE =================
    if cmd == "addmod":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply(lang["admin_only"])
        if not message.reply_to_message:
            return
        target = message.reply_to_message.from_user.id
        if target not in chat["mods"]:
            chat["mods"].append(target)
            await message.reply("✅ Mod eklendi")

    if cmd == "delmod":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply(lang["admin_only"])
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
        member = await bot.get_chat_member(message.chat.id, target)

        if member.status in ("administrator", "creator"):
            return await message.reply("Adminlere işlem yapamam")

        if len(cmd_parts) == 2:
            sec = parse_time(cmd_parts[1])
            if sec:
                until = datetime.utcnow() + timedelta(seconds=sec)
                await bot.ban_chat_member(message.chat.id, target, until_date=until)
                await message.reply("Süreli ban ✅")
                await send_log(bot, message.chat.id, f"🚫 Süreli ban: {target}")
                save_state()
                return

        await bot.ban_chat_member(message.chat.id, target)
        await message.reply("Süresiz ban ✅")
        await send_log(bot, message.chat.id, f"🚫 Süresiz ban: {target}")

    if cmd == "unban":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return
        await bot.unban_chat_member(message.chat.id, message.reply_to_message.from_user.id)
        await message.reply("Ban kaldırıldı ✅")

    # ================= MUTE =================
    if cmd == "mute":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return

        target = message.reply_to_message.from_user.id
        member = await bot.get_chat_member(message.chat.id, target)

        if member.status in ("administrator", "creator"):
            return

        if len(cmd_parts) == 2:
            sec = parse_time(cmd_parts[1])
            if sec:
                until = datetime.utcnow() + timedelta(seconds=sec)
                await bot.restrict_chat_member(
                    message.chat.id,
                    target,
                    permissions=mute_perm(),
                    until_date=until
                )
                await message.reply(lang["muted"])
                save_state()
                return

        await bot.restrict_chat_member(
            message.chat.id,
            target,
            permissions=mute_perm()
        )
        await message.reply(lang["muted"])

    if cmd == "unmute":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return
        await bot.restrict_chat_member(
            message.chat.id,
            message.reply_to_message.from_user.id,
            permissions=ChatPermissions(can_send_messages=True)
        )
        await message.reply("Mute kaldırıldı ✅")

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

    # ================= BLACKLIST =================
    if cmd == "blacklist":
        if not message.reply_to_message:
            return
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return
        target = message.reply_to_message.from_user.id
        if target not in STATE["blacklist"]:
            STATE["blacklist"].append(target)
            await bot.ban_chat_member(message.chat.id, target)
            await message.reply("Global blacklist ✅")

    save_state()

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

    # ================= FLOOD AYAR =================
    if cmd == "setflood":
        if not await check_perm():
            return
        if len(cmd_parts) != 3:
            return
        chat["flood"]["limit"] = int(cmd_parts[1])
        chat["flood"]["seconds"] = int(cmd_parts[2])
        await message.reply("Flood ayarlandı ✅")

    # ================= RAID AYAR =================
    if cmd == "setraid":
        if not await check_perm():
            return
        if len(cmd_parts) != 3:
            return
        chat["raid"]["limit"] = int(cmd_parts[1])
        chat["raid"]["seconds"] = int(cmd_parts[2])
        await message.reply("Raid ayarlandı ✅")

    # ================= WELCOME =================
    if cmd == "setwelcome":
        if not await check_perm():
            return
        chat["welcome"] = message.text.split(maxsplit=1)[1]
        await message.reply("Hoşgeldin mesajı ayarlandı ✅")

    if cmd == "setgoodbye":
        if not await check_perm():
            return
        chat["goodbye"] = message.text.split(maxsplit=1)[1]
        await message.reply("Çıkış mesajı ayarlandı ✅")

    if message.new_chat_members and chat["welcome"]:
        await message.reply(chat["welcome"])

    if message.left_chat_member and chat["goodbye"]:
        await message.reply(chat["goodbye"])

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

    # ================= LANGUAGE =================
    if cmd == "lang":
        if not await check_perm():
            return
        if len(cmd_parts) != 2:
            return
        if cmd_parts[1] in LANG:
            chat["lang"] = cmd_parts[1]
            await message.reply("Dil değiştirildi ✅")

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
