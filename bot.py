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

BOT_START_TIME = datetime.utcnow()

def get_total_users():
    users = set()
    for chat in STATE["chats"].values():
        users.update(chat.get("stats", {}).keys())
    return len(users)

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

FLOOD = defaultdict(lambda: deque(maxlen=100))

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
        "captcha": "Doğrulamak için butona bas.",
        "unbanned": "Ban kaldırıldı ✅",
        "unmuted": "Mute kaldırıldı ✅",
        "kicked": "Kullanıcı atıldı ✅",
        "softbanned": "Softban uygulandı ✅",
        "warn_reset": "Warn sıfırlandı ✅",
        "global_blacklist": "Global blacklist ✅",
        "note_saved": "Not kaydedildi ✅",
        "note_deleted": "Not silindi ✅",
        "filter_saved": "Filtre kaydedildi ✅",
        "filter_deleted": "Filtre silindi ✅",
        "flood_set": "Flood ayarlandı ✅",
        "raid_set": "Raid ayarlandı ✅",
        "welcome_set": "Hoşgeldin mesajı ayarlandı ✅",
        "goodbye_set": "Çıkış mesajı ayarlandı ✅",
        "captcha_on": "Captcha Açık ✅",
        "captcha_off": "Captcha Kapalı ❌",
        "lang_changed": "Dil değiştirildi ✅",
        "log_set": "Log kanalı ayarlandı ✅",
        "antilink_set": "Antilink güncellendi ✅",
        "no_admin_action": "Adminlere işlem yapamam",
        "flood_warn": "⚠️ Flood tespit edildi! Lütfen yavaşlayın.",
        "raid_detected": "🚨 Raid koruması aktif! Geçici olarak kilit uygulandı.",
        "raid_unlocked": "🔓 Raid koruması süresi doldu, kilit kaldırıldı.",
    },
    "en": {
        "no_perm": "You don't have permission.",
        "admin_only": "Admin only.",
        "muted": "User muted ✅",
        "banned": "User banned ✅",
        "warn": "Warning issued",
        "captcha": "Click button to verify.",
        "unbanned": "Ban removed ✅",
        "unmuted": "Mute removed ✅",
        "kicked": "User kicked ✅",
        "softbanned": "Softban applied ✅",
        "warn_reset": "Warning reset ✅",
        "global_blacklist": "Global blacklist ✅",
        "note_saved": "Note saved ✅",
        "note_deleted": "Note deleted ✅",
        "filter_saved": "Filter saved ✅",
        "filter_deleted": "Filter deleted ✅",
        "flood_set": "Flood settings updated ✅",
        "raid_set": "Raid settings updated ✅",
        "welcome_set": "Welcome message set ✅",
        "goodbye_set": "Goodbye message set ✅",
        "captcha_on": "Captcha Enabled ✅",
        "captcha_off": "Captcha Disabled ❌",
        "lang_changed": "Language changed ✅",
        "log_set": "Log channel set ✅",
        "antilink_set": "Antilink updated ✅",
        "no_admin_action": "Cannot take action against admins",
        "flood_warn": "⚠️ Flood detected! Please slow down.",
        "raid_detected": "🚨 Raid protection active! Temporary lock applied.",
        "raid_unlocked": "🔓 Raid protection expired, lock removed.",
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

def full_perm():
    return ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )

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

# ================= START PANEL =================

@router.message(CommandStart())
async def start_cmd(message: Message, bot: Bot):

    uptime = datetime.utcnow() - BOT_START_TIME
    uptime_str = str(uptime).split(".")[0]

    total_groups = len(STATE.get("chats", {}))
    total_users = get_total_users()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🚀 Botu Gruba Ekle",
                url="https://t.me/KGBKORUMABOT?startgroup=true"
            )
        ],
        [
            InlineKeyboardButton(
                text="⚙️ Admin Paneli",
                callback_data="admin_panel"
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
                text="📢 Destek Kanalı",
                url="https://t.me/KGBotomasyon"
            )
        ]
    ])

    await message.reply(
        f"👑 <b>KGB GUARD ULTIMATE</b>\n\n"
        f"🟢 <b>Sistem Durumu:</b> ONLINE\n"
        f"🛡 <b>Koruma Modu:</b> AKTİF\n\n"
        f"👥 Aktif Kullanıcı: {total_users}\n"
        f"📂 Kayıtlı Grup: {total_groups}\n"
        f"⏳ Uptime: {uptime_str}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Profesyonel Grup Güvenlik Altyapısı",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )

# ================= COMMANDS BUTTON =================

@router.callback_query(lambda c: c.data == "commands_menu")
async def commands_menu(call: CallbackQuery):

    text = """
👑 <b>KGB GUARD ULTIMATE — Komut Paneli</b>

━━━━━━━━━━━━━━━━━━

🔨 <b>Yasaklama</b>
• /ban (reply)
• /ban 10m
• /unban (reply)

🦵 <b>Kick / Softban</b>
• /kick (reply)
• /softban (reply)

🔇 <b>Susturma</b>
• /mute (reply)
• /mute 5m
• /unmute (reply)

⚠️ <b>Uyarı Sistemi</b>
• /warn (reply) (3 warn = otomatik mute)
• /warns (reply)
• /resetwarn (reply)

👑 <b>Yetki Yönetimi</b>
• /addmod (reply)
• /delmod (reply)

📦 <b>Not Sistemi</b>
• /save isim içerik
• /get isim
• /delnote isim

🎯 <b>Filtre Sistemi</b>
• /filter kelime cevap
• /stop kelime

🌊 <b>Flood Kontrol</b>
• /setflood limit saniye

🚨 <b>Raid Koruma</b>
• /setraid limit saniye

🔐 <b>Kilit Sistemleri</b>
• /antilink on/off
• /lock media / unlock media
• /lock sticker / unlock sticker

👋 <b>Hoşgeldin / Çıkış</b>
• /setwelcome mesaj
• /setgoodbye mesaj

🔒 <b>Captcha</b>
• /captcha (toggle)

📝 <b>Log / Dil</b>
• /setlog kanal_id
• /lang tr veya en

⚫ <b>Blacklist</b>
• /blacklist (reply)

📊 <b>Bilgi</b>
• /stats
• /id
• /admins
• /purge (reply)

━━━━━━━━━━━━━━━━━━
✅ Tüm komutlar / veya . ile çalışır.
"""

    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Ana Menüye Dön", callback_data="back_main")]
        ])
    )

# ================= ADMIN PANEL =================

@router.callback_query(lambda c: c.data == "admin_panel")
async def admin_panel(call: CallbackQuery):

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌊 Flood Ayarlarını Gör", callback_data="flood_info")
        ],
        [
            InlineKeyboardButton(text="🚨 Raid Ayarlarını Gör", callback_data="raid_info")
        ],
        [
            InlineKeyboardButton(text="📊 Günlük İstatistik", callback_data="daily_stats")
        ],
        [
            InlineKeyboardButton(text="🔙 Ana Menü", callback_data="back_main")
        ]
    ])

    await call.message.edit_text(
        "<b>⚙️ ADMIN KONTROL PANELİ</b>\n\n"
        "Buradan sistem ayarlarını inceleyebilirsin.",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )

# ================= FLOOD INFO =================

@router.callback_query(lambda c: c.data == "flood_info")
async def flood_info(call: CallbackQuery):
    text = "🌊 <b>Flood Bilgileri</b>\n\n"

    if not STATE.get("chats"):
        text += "Henüz kayıtlı grup yok."
    else:
        for chat_id, data in STATE["chats"].items():
            flood = data.get("flood", {"limit": 6, "seconds": 5})
            text += f"• Grup <code>{chat_id}</code>: "
            text += f"{flood['limit']} mesaj / {flood['seconds']} saniye\n"

    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ])
    )

# ================= RAID INFO =================

@router.callback_query(lambda c: c.data == "raid_info")
async def raid_info(call: CallbackQuery):
    text = "🚨 <b>Raid Bilgileri</b>\n\n"

    if not STATE.get("chats"):
        text += "Henüz kayıtlı grup yok."
    else:
        for chat_id, data in STATE["chats"].items():
            raid = data.get("raid", {"limit": 5, "seconds": 30})
            text += f"• Grup <code>{chat_id}</code>: "
            text += f"{raid['limit']} katılma / {raid['seconds']} saniye\n"

    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ])
    )

# ================= DAILY STATS INFO =================

@router.callback_query(lambda c: c.data == "daily_stats")
async def daily_stats_info(call: CallbackQuery):

    total_users = get_total_users()
    total_groups = len(STATE.get("chats", {}))

    text = (
        "📊 <b>Günlük İstatistik</b>\n\n"
        f"👥 Toplam Kullanıcı: {total_users}\n"
        f"📂 Toplam Grup: {total_groups}\n"
        f"⚫ Blacklist: {len(STATE.get('blacklist', []))}\n"
    )

    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
        ])
    )

# ================= MY GROUPS =================

@router.callback_query(lambda c: c.data == "my_groups")
async def my_groups(call: CallbackQuery, bot: Bot):

    user_id = call.from_user.id
    common_groups = []

    for chat_id in STATE.get("chats", {}):
        try:
            member = await bot.get_chat_member(int(chat_id), user_id)
            if member.status in ("administrator", "creator", "member"):
                chat = await bot.get_chat(int(chat_id))
                count = await bot.get_chat_member_count(int(chat_id))
                common_groups.append(f"{chat.title} ({count} üye)")
        except:
            continue

    if not common_groups:
        text = "Bot ile ortak grubun bulunamadı."
    else:
        text = "<b>Ortak Gruplar:</b>\n\n"
        for g in common_groups[:20]:
            text += f"• {escape(g)}\n"

    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Ana Menü", callback_data="back_main")]
        ])
    )

# ================= BACK BUTTON =================

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

# ================= CAPTCHA VERIFY =================

@router.callback_query(lambda c: c.data.startswith("verify_"))
async def verify_user(call: CallbackQuery, bot: Bot):
    user_id = int(call.data.split("_")[1])
    chat = get_chat(call.message.chat.id)

    if call.from_user.id != user_id:
        await call.answer("Bu buton sana ait değil ❌", show_alert=True)
        return

    if str(user_id) in chat.get("captcha_pending", {}):
        try:
            await bot.restrict_chat_member(
                call.message.chat.id,
                user_id,
                permissions=full_perm()
            )
        except:
            pass

        if str(user_id) in chat.get("captcha_pending", {}):
            del chat["captcha_pending"][str(user_id)]

        await call.message.delete()
        await call.answer("Doğrulandı ✅")
        save_state()
    else:
        await call.answer("Doğrulama süresi dolmuş ❌", show_alert=True)

# ================= MAIN HANDLER =================

@router.message()
async def main_handler(message: Message, bot: Bot):

    if message.chat.type == "private":
        return

    chat = get_chat(message.chat.id)
    lang = LANG[chat["lang"]]
    uid = str(message.from_user.id)

    # GLOBAL BLACKLIST
    if message.from_user.id in STATE["blacklist"]:
        try:
            await bot.ban_chat_member(message.chat.id, message.from_user.id)
        except:
            pass
        return

    # MESSAGE COUNT
    chat["stats"][uid] = chat["stats"].get(uid, 0) + 1

    # ================= HOŞGELDİN / ÇIKIŞ MESAJI =================
    if message.new_chat_members:
        if chat.get("welcome"):
            await message.reply(chat["welcome"])

        # CAPTCHA SİSTEMİ
        if chat.get("captcha"):
            for user in message.new_chat_members:
                if user.id == message.from_user.id:
                    continue
                try:
                    await bot.restrict_chat_member(
                        message.chat.id,
                        user.id,
                        permissions=mute_perm()
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="✅ Doğrula",
                            callback_data=f"verify_{user.id}"
                        )]
                    ])
                    chat.setdefault("captcha_pending", {})[str(user.id)] = True
                    await message.reply(
                        f"🔐 {escape(user.full_name)}, doğrulamak için butona bas.",
                        reply_markup=kb
                    )
                except:
                    pass

        # RAID KONTROLÜ
        now = datetime.utcnow().timestamp()
        joins = chat.setdefault("joins", [])
        joins.append(now)

        raid_limit = chat["raid"]["limit"]
        raid_seconds = chat["raid"]["seconds"]

        recent = [t for t in joins if (now - t) <= raid_seconds]
        chat["joins"] = recent

        if len(recent) >= raid_limit:
            try:
                await bot.set_chat_permissions(
                    message.chat.id,
                    permissions=mute_perm()
                )
                await message.reply(lang["raid_detected"])
                await send_log(bot, message.chat.id, f"🚨 Raid koruması aktif! Grup kilitlendi.")
            except:
                pass

    if message.left_chat_member:
        if chat.get("goodbye"):
            await message.reply(chat["goodbye"])

    # ================= FİLTRE KONTROLÜ =================
    if message.text and not (message.text.startswith("/") or message.text.startswith(".")):
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            for word, response in chat.get("filters", {}).items():
                if word.lower() in message.text.lower():
                    try:
                        await message.delete()
                    except:
                        pass
                    await message.reply(response)
                    save_state()
                    return

    # ================= ANTILINK =================
    if chat["antilink"] and message.text and URL_RE.search(message.text):
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            try:
                await message.delete()
            except:
                pass
            await send_log(bot, message.chat.id, f"🔗 Antilink: {message.from_user.id} link attı.")
            save_state()
            return

    # ================= MEDIA LOCK =================
    if chat["lock_media"] and (message.photo or message.video or message.document):
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            try:
                await message.delete()
            except:
                pass
            return

    # ================= STICKER LOCK =================
    if chat["lock_sticker"] and message.sticker:
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            try:
                await message.delete()
            except:
                pass
            return

    # ================= COMMAND CHECK =================
    if not message.text:
        save_state()
        return

    if not (message.text.startswith("/") or message.text.startswith(".")):
        save_state()
        return

    cmd_parts = message.text.split()
    cmd = cmd_parts[0][1:].lower().split("@")[0]

    async def check_perm():
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            await message.reply(lang["no_perm"])
            return False
        return True

    # ================= FLOOD KONTROLÜ =================
    flood_key = f"{message.chat.id}_{message.from_user.id}"
    flood_data = chat["flood"]
    now = datetime.utcnow().timestamp()
    FLOOD[flood_key].append(now)

    recent_flood = [t for t in FLOOD[flood_key] if (now - t) <= flood_data["seconds"]]
    FLOOD[flood_key] = deque(recent_flood, maxlen=100)

    if len(recent_flood) >= flood_data["limit"]:
        if not await has_permission(bot, message.chat.id, message.from_user.id):
            try:
                await message.delete()
            except:
                pass
            try:
                until = datetime.utcnow() + timedelta(seconds=flood_data["seconds"])
                await bot.restrict_chat_member(
                    message.chat.id,
                    message.from_user.id,
                    permissions=mute_perm(),
                    until_date=until
                )
                await send_log(bot, message.chat.id,
                    f"🌊 Flood mute: {message.from_user.id} ({flood_data['limit']} mesaj / {flood_data['seconds']}sn)"
                )
            except:
                pass
            save_state()
            return

    # ================= KICK =================
    if cmd == "kick":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return
        target = message.reply_to_message.from_user.id
        member = await bot.get_chat_member(message.chat.id, target)
        if member.status in ("administrator", "creator"):
            return await message.reply(lang["no_admin_action"])
        await bot.ban_chat_member(message.chat.id, target)
        await bot.unban_chat_member(message.chat.id, target)
        await message.reply(lang["kicked"])
        await send_log(bot, message.chat.id, f"👢 Kick: {target}")

    # ================= SOFTBAN =================
    if cmd == "softban":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return
        target = message.reply_to_message.from_user.id
        member = await bot.get_chat_member(message.chat.id, target)
        if member.status in ("administrator", "creator"):
            return await message.reply(lang["no_admin_action"])
        await bot.ban_chat_member(message.chat.id, target)
        await bot.unban_chat_member(message.chat.id, target)
        await message.reply(lang["softbanned"])
        await send_log(bot, message.chat.id, f"🔨 Softban: {target}")

    # ================= RESET WARN =================
    if cmd == "resetwarn":
        if not message.reply_to_message:
            return
        if not await check_perm():
            return
        target = message.reply_to_message.from_user.id
        chat["warns"][str(target)] = 0
        await message.reply(lang["warn_reset"])
        await send_log(bot, message.chat.id, f"🔄 Warn sıfırlandı: {target}")

    # ================= WARNS =================
    if cmd == "warns":
        if not message.reply_to_message:
            return
        target = message.reply_to_message.from_user.id
        count = chat["warns"].get(str(target), 0)
        await message.reply(f"⚠️ Toplam warn: {count}/3")

    # ================= ANTILINK =================
    if cmd == "antilink":
        if not await check_perm():
            return
        if len(cmd_parts) < 2:
            await message.reply("Kullanım: /antilink on veya /antilink off")
            return
        chat["antilink"] = cmd_parts[1].lower() == "on"
        await message.reply(lang["antilink_set"])
        await send_log(bot, message.chat.id, f"🔗 Antilink: {'ON' if chat['antilink'] else 'OFF'}")

    # ================= LOCK / UNLOCK =================
    if cmd == "lock":
        if not await check_perm():
            return
        if len(cmd_parts) < 2:
            await message.reply("Kullanım: /lock media veya /lock sticker")
            return
        lock_type = cmd_parts[1].lower()
        if lock_type == "media":
            chat["lock_media"] = True
            await message.reply("🔒 Medya kilidi aktif ✅")
            await send_log(bot, message.chat.id, "🔒 Medya kilidi aktif edildi.")
        elif lock_type == "sticker":
            chat["lock_sticker"] = True
            await message.reply("🔒 Sticker kilidi aktif ✅")
            await send_log(bot, message.chat.id, "🔒 Sticker kilidi aktif edildi.")
        elif lock_type == "link":
            chat["antilink"] = True
            await message.reply("🔒 Link kilidi aktif ✅")
            await send_log(bot, message.chat.id, "🔒 Link kilidi aktif edildi.")
        else:
            await message.reply("Bilinmeyen kilit türü. (media / sticker / link)")

    if cmd == "unlock":
        if not await check_perm():
            return
        if len(cmd_parts) < 2:
            await message.reply("Kullanım: /unlock media veya /unlock sticker")
            return
        lock_type = cmd_parts[1].lower()
        if lock_type == "media":
            chat["lock_media"] = False
            await message.reply("🔓 Medya kilidi kaldırıldı ✅")
            await send_log(bot, message.chat.id, "🔓 Medya kilidi kaldırıldı.")
        elif lock_type == "sticker":
            chat["lock_sticker"] = False
            await message.reply("🔓 Sticker kilidi kaldırıldı ✅")
            await send_log(bot, message.chat.id, "🔓 Sticker kilidi kaldırıldı.")
        elif lock_type == "link":
            chat["antilink"] = False
            await message.reply("🔓 Link kilidi kaldırıldı ✅")
            await send_log(bot, message.chat.id, "🔓 Link kilidi kaldırıldı.")
        else:
            await message.reply("Bilinmeyen kilit türü. (media / sticker / link)")

    # ================= SETLOG =================
    if cmd == "setlog":
        if not await check_perm():
            return
        if len(cmd_parts) < 2:
            await message.reply("Kullanım: /setlog kanal_id")
            return
        chat["log"] = int(cmd_parts[1])
        await message.reply(lang["log_set"])

    # ================= STATS =================
    if cmd == "stats":
        total_users = get_total_users()
        total_groups = len(STATE["chats"])
        uptime = datetime.utcnow() - BOT_START_TIME
        uptime_str = str(uptime).split(".")[0]
        await message.reply(
            f"📊 <b>İstatistik</b>\n\n"
            f"👥 Aktif Kullanıcı: {total_users}\n"
            f"📂 Grup Sayısı: {total_groups}\n"
            f"⚫ Blacklist: {len(STATE.get('blacklist', []))}\n"
            f"⏳ Uptime: {uptime_str}",
            parse_mode=ParseMode.HTML
        )

    # ================= ID =================
    if cmd == "id":
        text = f"🆔 Kullanıcı ID: <code>{message.from_user.id}</code>"
        if message.reply_to_message:
            text = f"🆔 Kullanıcı ID: <code>{message.reply_to_message.from_user.id}</code>"
        text += f"\n💬 Chat ID: <code>{message.chat.id}</code>"
        await message.reply(text, parse_mode=ParseMode.HTML)

    # ================= ADMINS =================
    if cmd == "admins":
        admins = await bot.get_chat_administrators(message.chat.id)
        text = "👑 <b>Yöneticiler:</b>\n\n"
        for a in admins:
            role = "👑 Kurucu" if a.status == "creator" else "⚙️ Admin"
            text += f"{role} — {escape(a.user.full_name)}\n"
        await message.reply(text, parse_mode=ParseMode.HTML)

    # ================= PURGE =================
    if cmd == "purge":
        if not message.reply_to_message:
            await message.reply("Bir mesaja reply verin.")
            return
        if not await check_perm():
            return
        start_id = message.reply_to_message.message_id
        end_id = message.message_id
        deleted = 0
        for msg_id in range(start_id, end_id + 1):
            try:
                await bot.delete_message(message.chat.id, msg_id)
                deleted += 1
            except:
                pass
        await send_log(bot, message.chat.id, f"🗑 Purge: {deleted} mesaj silindi.")

    save_state()

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
            await send_log(bot, message.chat.id, f"👤 Mod eklendi: {target}")

    if cmd == "delmod":
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply(lang["admin_only"])
        if not message.reply_to_message:
            return
        target = message.reply_to_message.from_user.id
        if target in chat["mods"]:
            chat["mods"].remove(target)
            await message.reply("✅ Mod silindi")
            await send_log(bot, message.chat.id, f"👤 Mod kaldırıldı: {target}")

    # ================= BAN =================
    if cmd == "ban":
        if not message.reply_to_message:
            await message.reply("Bir kullanıcıya reply verin.")
            return
        if not await check_perm():
            return

        target = message.reply_to_message.from_user.id
        member = await bot.get_chat_member(message.chat.id, target)

        if member.status in ("administrator", "creator"):
            return await message.reply(lang["no_admin_action"])

        if len(cmd_parts) == 2:
            sec = parse_time(cmd_parts[1])
            if sec:
                until = datetime.utcnow() + timedelta(seconds=sec)
                await bot.ban_chat_member(message.chat.id, target, until_date=until)
                await message.reply(f"⏳ Süreli ban ✅ ({cmd_parts[1]})")
                await send_log(bot, message.chat.id, f"🚫 Süreli ban: {target} ({cmd_parts[1]})")
                save_state()
                return

        await bot.ban_chat_member(message.chat.id, target)
        await message.reply("🚫 Süresiz ban ✅")
        await send_log(bot, message.chat.id, f"🚫 Süresiz ban: {target}")

    # ================= UNBAN =================
    if cmd == "unban":
        if not message.reply_to_message:
            await message.reply("Bir kullanıcıya reply verin.")
            return
        if not await check_perm():
            return
        target = message.reply_to_message.from_user.id
        try:
            await bot.unban_chat_member(message.chat.id, target)
            await message.reply(lang["unbanned"])
            await send_log(bot, message.chat.id, f"✅ Unban: {target}")
        except Exception as e:
            await message.reply(f"Unban başarısız: {e}")

    # ================= MUTE =================
    if cmd == "mute":
        if not message.reply_to_message:
            await message.reply("Bir kullanıcıya reply verin.")
            return
        if not await check_perm():
            return

        target = message.reply_to_message.from_user.id
        member = await bot.get_chat_member(message.chat.id, target)

        if member.status in ("administrator", "creator"):
            return await message.reply(lang["no_admin_action"])

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
                await message.reply(f"🔇 {lang['muted']} ({cmd_parts[1]})")
                await send_log(bot, message.chat.id, f"🔇 Süreli mute: {target} ({cmd_parts[1]})")
                save_state()
                return

        await bot.restrict_chat_member(
            message.chat.id,
            target,
            permissions=mute_perm()
        )
        await message.reply(lang["muted"])
        await send_log(bot, message.chat.id, f"🔇 Mute: {target}")

    # ================= UNMUTE =================
    if cmd == "unmute":
        if not message.reply_to_message:
            await message.reply("Bir kullanıcıya reply verin.")
            return
        if not await check_perm():
            return
        target = message.reply_to_message.from_user.id
        try:
            await bot.restrict_chat_member(
                message.chat.id,
                target,
                permissions=full_perm()
            )
            await message.reply(lang["unmuted"])
            await send_log(bot, message.chat.id, f"🔊 Unmute: {target}")
        except Exception as e:
            await message.reply(f"Unmute başarısız: {e}")

    # ================= WARN =================
    if cmd == "warn":
        if not message.reply_to_message:
            await message.reply("Bir kullanıcıya reply verin.")
            return
        if not await check_perm():
            return

        target = message.reply_to_message.from_user.id

        member = await bot.get_chat_member(message.chat.id, target)
        if member.status in ("administrator", "creator"):
            return await message.reply(lang["no_admin_action"])

        chat["warns"][str(target)] = chat["warns"].get(str(target), 0) + 1
        warn_count = chat["warns"][str(target)]

        if warn_count >= 3:
            await bot.restrict_chat_member(
                message.chat.id,
                target,
                permissions=mute_perm()
            )
            chat["warns"][str(target)] = 0
            await message.reply(f"⚠️ 3 warn oldu, otomatik mute uygulandı ✅")
            await send_log(bot, message.chat.id, f"⚠️ 3 Warn → Otomatik Mute: {target}")
        else:
            await message.reply(f"⚠️ Warn verildi ({warn_count}/3)")
            await send_log(bot, message.chat.id, f"⚠️ Warn: {target} ({warn_count}/3)")

    # ================= BLACKLIST =================
    if cmd == "blacklist":
        if not message.reply_to_message:
            return
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply(lang["admin_only"])
        target = message.reply_to_message.from_user.id
        if target not in STATE["blacklist"]:
            STATE["blacklist"].append(target)
            try:
                await bot.ban_chat_member(message.chat.id, target)
            except:
                pass
            await message.reply(lang["global_blacklist"])
            await send_log(bot, message.chat.id, f"⚫ Global Blacklist: {target}")
        else:
            await message.reply("Bu kullanıcı zaten blacklist'te.")

    save_state()

    # ================= NOTES =================
    if cmd == "save":
        if not await check_perm():
            return
        if len(cmd_parts) < 3:
            await message.reply("Kullanım: /save isim içerik")
            return
        name = cmd_parts[1].lower()
        content = message.text.split(maxsplit=2)[2]
        chat["notes"][name] = content
        await message.reply(f"{lang['note_saved']} → <code>{escape(name)}</code>", parse_mode=ParseMode.HTML)

    if cmd == "get":
        if len(cmd_parts) < 2:
            await message.reply("Kullanım: /get isim")
            return
        name = cmd_parts[1].lower()
        if name in chat.get("notes", {}):
            await message.reply(chat["notes"][name])
        else:
            await message.reply("Bu isimde not bulunamadı ❌")

    if cmd == "delnote":
        if not await check_perm():
            return
        if len(cmd_parts) < 2:
            await message.reply("Kullanım: /delnote isim")
            return
        name = cmd_parts[1].lower()
        if name in chat.get("notes", {}):
            del chat["notes"][name]
            await message.reply(lang["note_deleted"])
        else:
            await message.reply("Bu isimde not bulunamadı ❌")

    # ================= FILTER SYSTEM =================
    if cmd == "filter":
        if not await check_perm():
            return
        if len(cmd_parts) < 3:
            await message.reply("Kullanım: /filter kelime cevap")
            return
        key_word = cmd_parts[1].lower()
        response = message.text.split(maxsplit=2)[2]
        chat["filters"][key_word] = response
        await message.reply(f"{lang['filter_saved']} → <code>{escape(key_word)}</code>", parse_mode=ParseMode.HTML)

    if cmd == "stop":
        if not await check_perm():
            return
        if len(cmd_parts) < 2:
            await message.reply("Kullanım: /stop kelime")
            return
        key_word = cmd_parts[1].lower()
        if key_word in chat.get("filters", {}):
            del chat["filters"][key_word]
            await message.reply(lang["filter_deleted"])
        else:
            await message.reply("Bu filtre bulunamadı ❌")

    # ================= FLOOD AYAR =================
    if cmd == "setflood":
        if not await check_perm():
            return
        if len(cmd_parts) != 3:
            await message.reply("Kullanım: /setflood limit saniye")
            return
        try:
            chat["flood"]["limit"] = int(cmd_parts[1])
            chat["flood"]["seconds"] = int(cmd_parts[2])
            await message.reply(f"🌊 Flood: {cmd_parts[1]} mesaj / {cmd_parts[2]} saniye ✅")
            await send_log(bot, message.chat.id,
                f"🌊 Flood ayarı: {cmd_parts[1]} mesaj / {cmd_parts[2]} saniye"
            )
        except ValueError:
            await message.reply("Geçersiz değer. Sadece sayı girin.")

    # ================= RAID AYAR =================
    if cmd == "setraid":
        if not await check_perm():
            return
        if len(cmd_parts) != 3:
            await message.reply("Kullanım: /setraid limit saniye")
            return
        try:
            chat["raid"]["limit"] = int(cmd_parts[1])
            chat["raid"]["seconds"] = int(cmd_parts[2])
            await message.reply(f"🚨 Raid: {cmd_parts[1]} katılma / {cmd_parts[2]} saniye ✅")
            await send_log(bot, message.chat.id,
                f"🚨 Raid ayarı: {cmd_parts[1]} katılma / {cmd_parts[2]} saniye"
            )
        except ValueError:
            await message.reply("Geçersiz değer. Sadece sayı girin.")

    # ================= WELCOME =================
    if cmd == "setwelcome":
        if not await check_perm():
            return
        if len(cmd_parts) < 2:
            await message.reply("Kullanım: /setwelcome mesajınız")
            return
        chat["welcome"] = message.text.split(maxsplit=1)[1]
        await message.reply(lang["welcome_set"])
        await send_log(bot, message.chat.id, "👋 Hoşgeldin mesajı ayarlandı.")

    # ================= GOODBYE =================
    if cmd == "setgoodbye":
        if not await check_perm():
            return
        if len(cmd_parts) < 2:
            await message.reply("Kullanım: /setgoodbye mesajınız")
            return
        chat["goodbye"] = message.text.split(maxsplit=1)[1]
        await message.reply(lang["goodbye_set"])
        await send_log(bot, message.chat.id, "👋 Çıkış mesajı ayarlandı.")

    # ================= CAPTCHA =================
    if cmd == "captcha":
        if not await check_perm():
            return
        chat["captcha"] = not chat.get("captcha", False)
        if chat["captcha"]:
            await message.reply(lang["captcha_on"])
        else:
            await message.reply(lang["captcha_off"])
        await send_log(bot, message.chat.id,
            f"🔐 Captcha: {'ON' if chat['captcha'] else 'OFF'}"
        )

    # ================= LANGUAGE =================
    if cmd == "lang":
        if not await check_perm():
            return
        if len(cmd_parts) != 2:
            await message.reply("Kullanım: /lang tr veya /lang en")
            return
        if cmd_parts[1] in LANG:
            chat["lang"] = cmd_parts[1]
            await message.reply(lang["lang_changed"])
        else:
            await message.reply(f"Desteklenmeyen dil. Seçenekler: {', '.join(LANG.keys())}")

    # ================= BOT DURUMU KONTROL =================
    if cmd == "botstatus":
        await message.reply(
            f"🟢 <b>Bot Aktif</b>\n"
            f"📂 Bu grup: <code>{message.chat.id}</code>\n"
            f"🔗 Antilink: {'ON ✅' if chat['antilink'] else 'OFF ❌'}\n"
            f"🔒 Medya Kilit: {'ON ✅' if chat['lock_media'] else 'OFF ❌'}\n"
            f"🎭 Sticker Kilit: {'ON ✅' if chat['lock_sticker'] else 'OFF ❌'}\n"
            f"🔐 Captcha: {'ON ✅' if chat.get('captcha') else 'OFF ❌'}\n"
            f"🌊 Flood: {chat['flood']['limit']} msg / {chat['flood']['seconds']}sn\n"
            f"🚨 Raid: {chat['raid']['limit']} join / {chat['raid']['seconds']}sn",
            parse_mode=ParseMode.HTML
        )

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

                text = "📊 <b>Günlük Aktivite Raporu</b>\n\n"
                text += "━━━━━━━━━━━━━━━━━━\n\n"

                for i, (uid, count) in enumerate(top, 1):
                    text += f"{i}. <code>{uid}</code> — {count} mesaj\n"

                text += f"\n👥 Toplam aktif üye: {len(stats)}"

                try:
                    await bot.send_message(int(chat_id), text, parse_mode=ParseMode.HTML)
                except:
                    pass

                STATE["chats"][chat_id]["stats"] = {}

            save_state()

        except Exception as e:
            print(f"[DAILY REPORT ERROR] {e}")
            await asyncio.sleep(10)

# ================= AUTO RAID UNLOCK =================

async def auto_unlock(bot: Bot):
    while True:
        try:
            await asyncio.sleep(15)

            for chat_id, data in STATE["chats"].items():
                joins = data.get("joins", [])

                if not joins:
                    continue

                raid_seconds = data["raid"]["seconds"]
                now = datetime.utcnow().timestamp()

                recent = [t for t in joins if (now - t) <= raid_seconds]

                if not recent:
                    try:
                        await bot.set_chat_permissions(
                            int(chat_id),
                            permissions=full_perm()
                        )
                        lang = LANG[data.get("lang", "tr")]
                        await bot.send_message(int(chat_id), lang["raid_unlocked"])
                        await send_log(bot, int(chat_id), "🔓 Raid kilidi otomatik kaldırıldı.")
                    except:
                        pass

                    data["joins"] = []
                    save_state()
                else:
                    data["joins"] = recent

        except Exception as e:
            print(f"[AUTO UNLOCK ERROR] {e}")
            await asyncio.sleep(10)

# ================= AUTO CAPTCHA CLEANUP =================

async def auto_captcha_cleanup(bot: Bot):
    while True:
        try:
            await asyncio.sleep(120)

            for chat_id, data in STATE["chats"].items():
                pending = data.get("captcha_pending", {})
                if not pending:
                    continue

                if not chat.get("captcha"):
                    continue

                for user_id_str in list(pending.keys()):
                    try:
                        user_id = int(user_id_str)
                        member = await bot.get_chat_member(int(chat_id), user_id)
                        if member.status in ("member", "restricted"):
                            if member.status == "restricted" and not member.can_send_messages:
                                pass
                        else:
                            del pending[user_id_str]
                    except:
                        del pending[user_id_str]

            save_state()

        except Exception as e:
            print(f"[CAPTCHA CLEANUP ERROR] {e}")
            await asyncio.sleep(30)

# ================= SAVE STATE PERIODICALLY =================

async def auto_save_state():
    while True:
        try:
            await asyncio.sleep(60)
            save_state()
        except Exception as e:
            print(f"[AUTO SAVE ERROR] {e}")
            await asyncio.sleep(30)

# ================= MAIN =================

async def main():
    load_state()

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp = Dispatcher()
    dp.include_router(router)

    print(f"🚀 {BOT_NAME} başlatılıyor...")
    print(f"📂 State dosyası: {STATE_PATH}")
    print(f"📂 Kayıtlı grup: {len(STATE.get('chats', {}))}")

    asyncio.create_task(daily_report(bot))
    asyncio.create_task(auto_unlock(bot))
    asyncio.create_task(auto_captcha_cleanup(bot))
    asyncio.create_task(auto_save_state())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
