import os
import re
import time
import datetime
import logging
from collections import defaultdict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import Forbidden, BadRequest
from duckduckgo_search import DDGS

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("BOT_TOKEN", "8787679143:AAHQWB7HwUr6to3q2Y73M7p8glLZDpDmfUQ")

MAX_WARNS = 3
SPAM_WINDOW = 5
SPAM_LIMIT = 5

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("rose_style_bot")

# =========================
# MEMORY STORE
# =========================
# Not: Bot yeniden başlarsa sıfırlanır.
# Kalıcı kullanım için SQLite/PostgreSQL eklenmeli.

stats = defaultdict(dict)            # stats[chat_id][user_id] = msg_count
warns = defaultdict(int)             # warns[(chat_id, user_id)] = warn_count
badwords = defaultdict(list)         # badwords[chat_id] = ["küfür", ...]
messages = defaultdict(list)         # messages[(chat_id, user_id)] = [timestamps]
antilink = defaultdict(bool)         # antilink[chat_id] = True/False

# =========================
# HELPERS
# =========================

URL_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|discord\.gg/\S+)",
    re.IGNORECASE
)

def is_private(update: Update) -> bool:
    return update.effective_chat.type == "private"

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id
        )
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        )
    except Exception as e:
        logger.warning(f"Admin kontrol hatası: {e}")
        return False

async def is_user_admin_by_id(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        )
    except Exception:
        return False

async def bot_can_delete(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        return getattr(member, "can_delete_messages", False) or member.status == ChatMemberStatus.OWNER
    except Exception:
        return False

async def bot_can_restrict(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        return getattr(member, "can_restrict_members", False) or member.status == ChatMemberStatus.OWNER
    except Exception:
        return False

async def bot_can_ban(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        return getattr(member, "can_restrict_members", False) or member.status == ChatMemberStatus.OWNER
    except Exception:
        return False

async def bot_can_pin(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        return getattr(member, "can_pin_messages", False) or member.status == ChatMemberStatus.OWNER
    except Exception:
        return False

def full_unmute_permissions() -> ChatPermissions:
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
    )

def spam_detect(chat_id: int, user_id: int) -> bool:
    now = datetime.datetime.now()
    key = (chat_id, user_id)

    messages[key].append(now)
    messages[key] = [t for t in messages[key] if (now - t).total_seconds() < SPAM_WINDOW]

    return len(messages[key]) > SPAM_LIMIT

def split_text(text: str, size: int = 4000):
    for i in range(0, len(text), size):
        yield text[i:i + size]

# =========================
# COMMANDS
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[
        InlineKeyboardButton(
            "➕ Beni Gruba Ekle",
            url=f"https://t.me/{context.bot.username}?startgroup=true"
        )
    ]]

    text = (
        "🤖 *Rose tarzı moderasyon botu aktif*\n\n"
        "Temel moderasyon, flood koruma, antilink, yasaklı kelime ve arama sistemi hazır."
    )

    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Komutlar*\n\n"
        "/start - Botu başlat\n"
        "/help - Yardım menüsü\n"
        "/ping - Gecikme ölç\n\n"
        "👮 *Moderasyon*\n"
        "/ban - Yanıtlanan kullanıcıyı banla\n"
        "/unban <user_id> - Ban kaldır\n"
        "/kick - Yanıtlanan kullanıcıyı at\n"
        "/mute - Yanıtlanan kullanıcıyı sustur\n"
        "/unmute - Susturmayı kaldır\n\n"
        "⚠️ *Warn*\n"
        "/warn - Uyarı ver\n"
        "/warns - Uyarı sayısını göster\n"
        "/clearwarns - Warn sıfırla\n\n"
        "🧹 *Filtreler*\n"
        "/addbad <kelime> - Yasaklı kelime ekle\n"
        "/delbad <kelime> - Yasaklı kelime sil\n"
        "/badlist - Yasaklı kelimeleri göster\n"
        "/antilink on/off - Link koruması\n\n"
        "📌 *Pin*\n"
        "/pin - Yanıtlanan mesajı sabitle\n"
        "/unpin - Tüm sabitleri kaldır\n\n"
        "🔎 *Arama*\n"
        "Mesaja `ara sorgu` yazarak sonuç getir"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_t = time.time()
    msg = await update.message.reply_text("pong")
    end_t = time.time()
    await msg.edit_text(f"🏓 {round((end_t - start_t) * 1000)} ms")

# =========================
# MODERATION
# =========================

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not await bot_can_ban(update.effective_chat.id, context):
        return await update.message.reply_text("Ban atmak için yetkim yok.")
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /ban kullan.")

    target = update.message.reply_to_message.from_user
    if await is_user_admin_by_id(update.effective_chat.id, target.id, context):
        return await update.message.reply_text("Adminleri banlayamam.")

    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(f"🚫 {target.mention_html()} banlandı.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"ban hatası: {e}")
        await update.message.reply_text("Ban işlemi başarısız oldu.")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not await bot_can_ban(update.effective_chat.id, context):
        return await update.message.reply_text("Ban kaldırmak için yetkim yok.")
    if not context.args:
        return await update.message.reply_text("Kullanım: /unban <user_id>")

    try:
        user_id = int(context.args[0])
        await context.bot.unban_chat_member(update.effective_chat.id, user_id)
        await update.message.reply_text("✅ Ban kaldırıldı.")
    except Exception:
        await update.message.reply_text("Geçerli bir user id gir.")

async def kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not await bot_can_ban(update.effective_chat.id, context):
        return await update.message.reply_text("Üyeyi atmak için yetkim yok.")
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /kick kullan.")

    target = update.message.reply_to_message.from_user
    if await is_user_admin_by_id(update.effective_chat.id, target.id, context):
        return await update.message.reply_text("Adminleri atamam.")

    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(f"👢 {target.full_name} gruptan atıldı.")
    except Exception as e:
        logger.error(f"kick hatası: {e}")
        await update.message.reply_text("Atma işlemi başarısız oldu.")

async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not await bot_can_restrict(update.effective_chat.id, context):
        return await update.message.reply_text("Susturmak için yetkim yok.")
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /mute kullan.")

    target = update.message.reply_to_message.from_user
    if await is_user_admin_by_id(update.effective_chat.id, target.id, context):
        return await update.message.reply_text("Adminleri susturamam.")

    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            target.id,
            ChatPermissions()
        )
        await update.message.reply_text(f"🔇 {target.full_name} susturuldu.")
    except Exception as e:
        logger.error(f"mute hatası: {e}")
        await update.message.reply_text("Susturma işlemi başarısız oldu.")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not await bot_can_restrict(update.effective_chat.id, context):
        return await update.message.reply_text("Susturma kaldırmak için yetkim yok.")
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /unmute kullan.")

    target = update.message.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id,
            target.id,
            full_unmute_permissions()
        )
        await update.message.reply_text(f"🔊 {target.full_name} için susturma kaldırıldı.")
    except Exception as e:
        logger.error(f"unmute hatası: {e}")
        await update.message.reply_text("Susturma kaldırılamadı.")

# =========================
# WARN SYSTEM
# =========================

async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /warn kullan.")

    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    if await is_user_admin_by_id(chat_id, target.id, context):
        return await update.message.reply_text("Adminlere warn veremem.")

    key = (chat_id, target.id)
    warns[key] += 1

    await update.message.reply_text(f"⚠️ {target.full_name} warn aldı: {warns[key]}/{MAX_WARNS}")

    if warns[key] >= MAX_WARNS:
        if not await bot_can_ban(chat_id, context):
            return await update.message.reply_text("3 warn oldu ama ban yetkim yok.")
        try:
            await context.bot.ban_chat_member(chat_id, target.id)
            await update.message.reply_text("🚫 3 warn nedeniyle banlandı.")
        except Exception as e:
            logger.error(f"warn-ban hatası: {e}")
            await update.message.reply_text("3 warn oldu ama ban işlemi başarısız.")

async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /warns kullan.")

    chat_id = update.effective_chat.id
    target = update.message.reply_to_message.from_user
    key = (chat_id, target.id)

    await update.message.reply_text(f"📌 {target.full_name} warn sayısı: {warns[key]}")

async def clearwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /clearwarns kullan.")

    chat_id = update.effective_chat.id
    target = update.message.reply_to_message.from_user
    warns[(chat_id, target.id)] = 0

    await update.message.reply_text(f"🧽 {target.full_name} warn sayısı sıfırlandı.")

# =========================
# BAD WORD SYSTEM
# =========================

async def addbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /addbad <kelime>")

    chat_id = update.effective_chat.id
    word = context.args[0].lower().strip()

    if word not in badwords[chat_id]:
        badwords[chat_id].append(word)

    await update.message.reply_text(f"✅ Yasaklı kelime eklendi: `{word}`", parse_mode="Markdown")

async def delbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /delbad <kelime>")

    chat_id = update.effective_chat.id
    word = context.args[0].lower().strip()

    if word in badwords[chat_id]:
        badwords[chat_id].remove(word)
        await update.message.reply_text(f"🗑️ Silindi: `{word}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("Bu kelime listede yok.")

async def badlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    words = badwords[chat_id]

    if not words:
        return await update.message.reply_text("Liste boş.")

    text = "🧱 Yasaklı kelimeler:\n\n" + "\n".join(f"- {w}" for w in words)
    await update.message.reply_text(text)

# =========================
# PIN
# =========================

async def pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Mesaj sabitlemek için yetkim yok.")
    if not update.message.reply_to_message:
        return await update.message.reply_text("Bir mesaja yanıt verip /pin kullan.")

    try:
        await context.bot.pin_chat_message(
            update.effective_chat.id,
            update.message.reply_to_message.message_id
        )
    except Exception:
        await update.message.reply_text("Mesaj sabitlenemedi.")

async def unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not await bot_can_pin(update.effective_chat.id, context):
        return await update.message.reply_text("Sabit kaldırmak için yetkim yok.")

    try:
        await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        await update.message.reply_text("📍 Tüm sabit mesajlar kaldırıldı.")
    except Exception:
        await update.message.reply_text("Sabit mesajlar kaldırılamadı.")

# =========================
# ANTILINK
# =========================

async def antilink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update):
        return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context):
        return
    if not context.args:
        return await update.message.reply_text("Kullanım: /antilink on veya /antilink off")

    arg = context.args[0].lower()
    chat_id = update.effective_chat.id

    if arg == "on":
        antilink[chat_id] = True
        await update.message.reply_text("🔗 Antilink açıldı.")
    elif arg == "off":
        antilink[chat_id] = False
        await update.message.reply_text("🔓 Antilink kapandı.")
    else:
        await update.message.reply_text("Sadece on/off kullan.")

# =========================
# SEARCH
# =========================

async def search_inline(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    try:
        result_lines = []
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=5)
            for i, item in enumerate(results, start=1):
                title = item.get("title", "Başlıksız")
                href = item.get("href", "")
                body = item.get("body", "")

                block = f"{i}. {title}"
                if body:
                    block += f"\n{body}"
                if href:
                    block += f"\n{href}"
                result_lines.append(block)

        if not result_lines:
            return await update.message.reply_text("Sonuç bulunamadı.")

        text = "🔎 Arama sonuçları:\n\n" + "\n\n".join(result_lines)

        for part in split_text(text):
            await update.message.reply_text(part)
    except Exception as e:
        logger.error(f"arama hatası: {e}")
        await update.message.reply_text("Arama sırasında hata oluştu.")

# =========================
# MESSAGE HANDLER
# =========================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()
    lowered = text.lower()

    # İstatistik
    stats[chat_id][user_id] = stats[chat_id].get(user_id, 0) + 1

    # Admin muafiyeti
    user_is_admin = False
    if not is_private(update):
        user_is_admin = await is_user_admin_by_id(chat_id, user_id, context)

    if not user_is_admin and not is_private(update):
        # Spam
        if spam_detect(chat_id, user_id):
            try:
                if await bot_can_delete(chat_id, context):
                    await update.message.delete()
                return
            except Exception:
                pass

        # Antilink
        if antilink[chat_id] and URL_PATTERN.search(text):
            try:
                if await bot_can_delete(chat_id, context):
                    await update.message.delete()
                return
            except Exception:
                pass

        # Badwords
        lowered_text = lowered
        for w in badwords[chat_id]:
            if re.search(rf"\b{re.escape(w)}\b", lowered_text, re.IGNORECASE):
                try:
                    if await bot_can_delete(chat_id, context):
                        await update.message.delete()
                    return
                except Exception:
                    pass

    # Arama
    if lowered.startswith("ara "):
        query = text[4:].strip()
        if query:
            await search_inline(update, context, query)

# =========================
# ERROR HANDLER
# =========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)

# =========================
# MAIN
# =========================

def main():
    if TOKEN == "BURAYA_BOT_TOKEN":
        print("HATA: Token girilmemiş. BOT_TOKEN env ya da koddaki TOKEN alanını doldur.")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping))

    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("kick", kick))

    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("unmute", unmute))

    app.add_handler(CommandHandler("warn", warn))
    app.add_handler(CommandHandler("warns", warns_cmd))
    app.add_handler(CommandHandler("clearwarns", clearwarns))

    app.add_handler(CommandHandler("addbad", addbad))
    app.add_handler(CommandHandler("delbad", delbad))
    app.add_handler(CommandHandler("badlist", badlist))

    app.add_handler(CommandHandler("pin", pin))
    app.add_handler(CommandHandler("unpin", unpin))

    app.add_handler(CommandHandler("antilink", antilink_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    print("BOT ÇALIŞIYOR")
    app.run_polling()

if __name__ == "__main__":
    main()
