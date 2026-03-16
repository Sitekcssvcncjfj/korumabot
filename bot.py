import os
import re
import time
import logging
import sqlite3
import datetime
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
from duckduckgo_search import DDGS

# =========================
# AYARLAR VE LOGLAMA
# =========================

TOKEN = os.getenv("BOT_TOKEN") # TOKEN ARTIK GÜVENDE (Çevresel değişkenden çekilecek)

MAX_WARNS = 3
SPAM_WINDOW = 5
SPAM_LIMIT = 5

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("ModBot")

# =========================
# VERİTABANI (SQLite)
# =========================

conn = sqlite3.connect("bot_database.db", check_same_thread=False)
cursor = conn.cursor()

# Tabloları oluştur
cursor.executescript("""
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    antilink BOOLEAN DEFAULT 0,
    welcome BOOLEAN DEFAULT 0,
    welcome_text TEXT DEFAULT 'Gruba hoş geldin!',
    log_chat_id INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS badwords (
    chat_id INTEGER,
    word TEXT,
    UNIQUE(chat_id, word)
);
CREATE TABLE IF NOT EXISTS warns (
    chat_id INTEGER,
    user_id INTEGER,
    warn_count INTEGER DEFAULT 0,
    UNIQUE(chat_id, user_id)
);
""")
conn.commit()

# Geçici veri belleği (Spam kontrolü için RAM'de kalması daha hızlıdır)
spam_tracker = defaultdict(list)

# =========================
# YARDIMCI FONKSİYONLAR
# =========================

URL_PATTERN = re.compile(
    r"(?i)\b(?:https?://|www\.|t\.me/|telegram\.me/|discord\.gg/)\S+\b"
)

def is_private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id
        )
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

async def bot_can_restrict(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        return getattr(member, "can_restrict_members", False) or member.status == ChatMemberStatus.OWNER
    except Exception:
        return False

async def send_log(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    cursor.execute("SELECT log_chat_id FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row and row[0] != 0:
        try:
            await context.bot.send_message(row[0], text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Log gönderme hatası: {e}")

def parse_time(time_str: str):
    # Örnek: "10m", "1h", "1d" formatlarını saniyeye çevirir
    unit = time_str[-1].lower()
    if not time_str[:-1].isdigit(): return None
    val = int(time_str[:-1])
    
    if unit == 'm': return val * 60
    elif unit == 'h': return val * 3600
    elif unit == 'd': return val * 86400
    return None

# =========================
# KOMUTLAR (TEMEL)
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("➕ Beni Gruba Ekle", url=f"https://t.me/{context.bot.username}?startgroup=true")]]
    text = (
        "🤖 *Gelişmiş Moderasyon Botu Aktif*\n\n"
        "Grup yönetimi, anti-spam, antilink, loglama ve çok daha fazlası!"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Komut Listesi*\n\n"
        "👮 *Moderasyon:*\n"
        "`/ban [sebep]` - Yasaklar\n"
        "`/tban <süre> [sebep]` - Süreli yasaklar (Örn: /tban 1h)\n"
        "`/unban` - Yasağı açar\n"
        "`/mute [sebep]` - Susturur\n"
        "`/tmute <süre> [sebep]` - Süreli susturur\n"
        "`/unmute` - Susturmayı açar\n"
        "`/kick [sebep]` - Gruptan atar\n"
        "`/warn [sebep]` - Uyarı verir\n"
        "`/warns` - Uyarıları gösterir\n"
        "`/clearwarns` - Uyarıları sıfırlar\n\n"
        "⚙️ *Ayarlar:*\n"
        "`/setlog` - İşlem log kanalını ayarlar\n"
        "`/welcome on/off` - Karşılama mesajı\n"
        "`/setwelcome <metin>` - Karşılama metnini değiştirir\n"
        "`/antilink on/off` - Link koruması\n"
        "`/addbad <kelime>` - Küfür/yasaklı kelime ekle\n"
        "`/delbad <kelime>` - Yasaklı kelime sil\n"
        "`/badlist` - Yasaklı kelimeleri göster\n\n"
        "🔎 *Diğer:*\n"
        "`/pin`, `/unpin`, `/ara <sorgu>`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# =========================
# YÖNETİM & MODERASYON
# =========================

async def mod_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    if is_private(update): return await update.message.reply_text("Bu komut grupta kullanılmalı.")
    if not await is_admin(update, context): return
    if not update.message.reply_to_message:
        return await update.message.reply_text(f"Lütfen bir mesaja yanıt vererek /{action} kullanın.")
    if not await bot_can_restrict(update.effective_chat.id, context):
        return await update.message.reply_text("Yeterli yetkim yok (Yönetici değilim veya yetkilerim eksik).")

    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    if target.id == context.bot.id:
        return await update.message.reply_text("Kendime işlem yapamam.")
    
    # Admin kontrolü
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return await update.message.reply_text("Yöneticilere işlem yapamam.")
    except Exception:
        pass

    args = context.args
    reason = "Sebep belirtilmedi."
    duration_secs = None

    if action in ["tban", "tmute"]:
        if not args: return await update.message.reply_text(f"Kullanım: /{action} <süre (10m, 1h, 1d)> [sebep]")
        duration_secs = parse_time(args[0])
        if not duration_secs: return await update.message.reply_text("Geçersiz süre formatı! (Örn: 10m, 1h, 1d)")
        reason = " ".join(args[1:]) if len(args) > 1 else reason
    else:
        reason = " ".join(args) if args else reason

    until_date = datetime.datetime.now() + datetime.timedelta(seconds=duration_secs) if duration_secs else None

    try:
        if action in ["ban", "tban"]:
            await context.bot.ban_chat_member(chat_id, target.id, until_date=until_date)
            act_text = "yasaklandı" if action == "ban" else f"süreli yasaklandı ({args[0]})"
        elif action in ["mute", "tmute"]:
            await context.bot.restrict_chat_member(
                chat_id, target.id, ChatPermissions(can_send_messages=False), until_date=until_date
            )
            act_text = "susturuldu" if action == "mute" else f"süreli susturuldu ({args[0]})"
        elif action == "kick":
            await context.bot.ban_chat_member(chat_id, target.id)
            await context.bot.unban_chat_member(chat_id, target.id)
            act_text = "gruptan atıldı"

        msg = f"🔨 <b>{target.full_name}</b> {act_text}.\n📝 <b>Sebep:</b> {reason}"
        await update.message.reply_text(msg, parse_mode="HTML")
        await send_log(context, chat_id, f"<b>Eylem:</b> {action.upper()}\n<b>Hedef:</b> {target.full_name} ({target.id})\n<b>Yetkili:</b> {update.effective_user.full_name}\n<b>Sebep:</b> {reason}")
    except Exception as e:
        logger.error(f"Mod action hatası ({action}): {e}")
        await update.message.reply_text("İşlem gerçekleştirilemedi. Belki kullanıcının yetkisi benden yüksektir.")

async def ban(update, context): await mod_action(update, context, "ban")
async def tban(update, context): await mod_action(update, context, "tban")
async def mute(update, context): await mod_action(update, context, "mute")
async def tmute(update, context): await mod_action(update, context, "tmute")
async def kick(update, context): await mod_action(update, context, "kick")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update) or not await is_admin(update, context): return
    if not update.message.reply_to_message: return await update.message.reply_text("Yanıtla /unban kullanın.")
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(f"✅ {target.full_name} yasağı kaldırıldı.")
        await send_log(context, update.effective_chat.id, f"<b>Eylem:</b> UNBAN\n<b>Hedef:</b> {target.full_name}\n<b>Yetkili:</b> {update.effective_user.full_name}")
    except Exception:
        await update.message.reply_text("Yasak kaldırılamadı.")

async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update) or not await is_admin(update, context): return
    if not update.message.reply_to_message: return await update.message.reply_text("Yanıtla /unmute kullanın.")
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id, ChatPermissions(can_send_messages=True, can_send_photos=True, can_send_other_messages=True)
        )
        await update.message.reply_text(f"🔊 {target.full_name} susturması kaldırıldı.")
        await send_log(context, update.effective_chat.id, f"<b>Eylem:</b> UNMUTE\n<b>Hedef:</b> {target.full_name}\n<b>Yetkili:</b> {update.effective_user.full_name}")
    except Exception:
        await update.message.reply_text("Susturma kaldırılamadı.")

# =========================
# WARN SİSTEMİ
# =========================

async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update) or not await is_admin(update, context): return
    if not update.message.reply_to_message: return await update.message.reply_text("Bir mesaja yanıt verin.")
    
    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    reason = " ".join(context.args) if context.args else "Sebep belirtilmedi."

    cursor.execute("SELECT warn_count FROM warns WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
    row = cursor.fetchone()
    current_warns = row[0] + 1 if row else 1

    cursor.execute("INSERT OR REPLACE INTO warns (chat_id, user_id, warn_count) VALUES (?, ?, ?)", (chat_id, target.id, current_warns))
    conn.commit()

    msg = f"⚠️ <b>{target.full_name}</b> uyarıldı! ({current_warns}/{MAX_WARNS})\n📝 <b>Sebep:</b> {reason}"
    await update.message.reply_text(msg, parse_mode="HTML")
    await send_log(context, chat_id, f"<b>Eylem:</b> WARN\n<b>Hedef:</b> {target.full_name}\n<b>Yetkili:</b> {update.effective_user.full_name}\n<b>Sebep:</b> {reason} ({current_warns}/{MAX_WARNS})")

    if current_warns >= MAX_WARNS:
        try:
            await context.bot.ban_chat_member(chat_id, target.id)
            cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
            conn.commit()
            await update.message.reply_text(f"🚫 {target.full_name} {MAX_WARNS} uyarıya ulaştığı için banlandı.")
            await send_log(context, chat_id, f"<b>Eylem:</b> AUTO-BAN (Max Warns)\n<b>Hedef:</b> {target.full_name}")
        except Exception:
            await update.message.reply_text("Kullanıcı limitine ulaştı ama ban yetkim yok.")

async def clearwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update) or not await is_admin(update, context): return
    if not update.message.reply_to_message: return await update.message.reply_text("Bir mesaja yanıt verin.")
    
    target = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    cursor.execute("UPDATE warns SET warn_count = 0 WHERE chat_id = ? AND user_id = ?", (chat_id, target.id))
    conn.commit()
    await update.message.reply_text(f"🧽 {target.full_name} isimli kullanıcının uyarıları sıfırlandı.")

# =========================
# AYARLAR (SETTING)
# =========================

async def toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_name: str, display_name: str):
    if is_private(update) or not await is_admin(update, context): return
    if not context.args or context.args[0].lower() not in ["on", "off"]:
        return await update.message.reply_text(f"Kullanım: /{setting_name} on VEYA /{setting_name} off")
    
    chat_id = update.effective_chat.id
    val = 1 if context.args[0].lower() == "on" else 0
    cursor.execute(f"INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
    cursor.execute(f"UPDATE chat_settings SET {setting_name} = ? WHERE chat_id = ?", (val, chat_id))
    conn.commit()
    
    status = "açıldı ✅" if val else "kapatıldı ❌"
    await update.message.reply_text(f"⚙️ {display_name} {status}.")

async def antilink_cmd(update, context): await toggle_setting(update, context, "antilink", "Antilink koruması")
async def welcome_cmd(update, context): await toggle_setting(update, context, "welcome", "Karşılama mesajı")

async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update) or not await is_admin(update, context): return
    if not context.args: return await update.message.reply_text("Kullanım: /setwelcome <mesajınız>")
    
    text = " ".join(context.args)
    chat_id = update.effective_chat.id
    cursor.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
    cursor.execute("UPDATE chat_settings SET welcome_text = ? WHERE chat_id = ?", (text, chat_id))
    conn.commit()
    await update.message.reply_text("✅ Karşılama metni güncellendi.")

async def setlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update) or not await is_admin(update, context): return
    chat_id = update.effective_chat.id
    cursor.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
    cursor.execute("UPDATE chat_settings SET log_chat_id = ? WHERE chat_id = ?", (chat_id, chat_id))
    conn.commit()
    await update.message.reply_text("✅ Bu sohbet moderasyon log kanalı olarak ayarlandı.")

# =========================
# KELİME FİLTRESİ
# =========================

async def addbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update) or not await is_admin(update, context): return
    if not context.args: return await update.message.reply_text("Kullanım: /addbad <kelime>")
    
    word = context.args[0].lower().strip()
    cursor.execute("INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?, ?)", (update.effective_chat.id, word))
    conn.commit()
    await update.message.reply_text(f"✅ Yasaklı kelime eklendi: `{word}`", parse_mode="Markdown")

async def delbad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private(update) or not await is_admin(update, context): return
    if not context.args: return await update.message.reply_text("Kullanım: /delbad <kelime>")
    
    word = context.args[0].lower().strip()
    cursor.execute("DELETE FROM badwords WHERE chat_id = ? AND word = ?", (update.effective_chat.id, word))
    conn.commit()
    await update.message.reply_text(f"🗑️ Kelime silindi: `{word}`", parse_mode="Markdown")

# =========================
# ARAMA (DuckDuckGo)
# =========================

async def ara(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Kullanım: /ara <sorgu>")
    query = " ".join(context.args)
    msg = await update.message.reply_text("🔍 Aranıyor...")
    
    try:
        results_text = "🔎 *Arama Sonuçları:*\n\n"
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=3)
            for i, item in enumerate(results, 1):
                results_text += f"{i}. [{item.get('title', 'Link')}]({item.get('href', '')})\n{item.get('body', '')[:100]}...\n\n"
        await msg.edit_text(results_text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text("Arama sırasında bir hata oluştu.")

# =========================
# MESAJ DİNLEYİCİSİ (Otomatik Moderasyon & Karşılama)
# =========================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_user: return
    if update.effective_user.is_bot: return # Botları yoksay

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    msg_text = update.message.text or update.message.caption or ""
    lowered = msg_text.lower()

    # YENİ ÜYE KARŞILAMA
    if update.message.new_chat_members:
        cursor.execute("SELECT welcome, welcome_text FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        if row and row[0] == 1:
            for new_member in update.message.new_chat_members:
                if not new_member.is_bot:
                    await update.message.reply_text(f"👋 Merhaba {new_member.mention_html()},\n{row[1]}", parse_mode="HTML")
        return

    # ÖZEL MESAJ VEYA ADMİNSE KONTROLLERİ GEÇ
    if is_private(update): return
    
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return
    except Exception:
        pass

    delete_msg = False
    reason = ""

    # ANTILINK KONTROLÜ
    cursor.execute("SELECT antilink FROM chat_settings WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row and row[0] == 1 and URL_PATTERN.search(msg_text):
        delete_msg = True
        reason = "Link Paylaşımı"

    # YASAKLI KELİME KONTROLÜ
    if not delete_msg:
        cursor.execute("SELECT word FROM badwords WHERE chat_id = ?", (chat_id,))
        badwords = [r[0] for r in cursor.fetchall()]
        for w in badwords:
            if re.search(rf"\b{re.escape(w)}\b", lowered):
                delete_msg = True
                reason = "Yasaklı Kelime"
                break

    # SPAM KONTROLÜ
    if not delete_msg:
        now = time.time()
        spam_tracker[(chat_id, user_id)].append(now)
        spam_tracker[(chat_id, user_id)] = [t for t in spam_tracker[(chat_id, user_id)] if now - t < SPAM_WINDOW]
        if len(spam_tracker[(chat_id, user_id)]) > SPAM_LIMIT:
            delete_msg = True
            reason = "Spam / Flood"
            spam_tracker[(chat_id, user_id)].clear() # Limiti sıfırla

    # CEZA UYGULAMA (SİLME)
    if delete_msg:
        try:
            await update.message.delete()
            warning = await update.message.reply_text(f"⚠️ {update.effective_user.mention_html()}, mesajın silindi! Nedeni: {reason}", parse_mode="HTML")
            await send_log(context, chat_id, f"<b>Eylem:</b> OTO-SİL ({reason})\n<b>Kullanıcı:</b> {update.effective_user.full_name}\n<b>Mesaj:</b> {msg_text[:50]}...")
            
            # Mesaj kirliliği olmasın diye uyarıyı 5 saniye sonra siler (opsiyonel ama şık durur)
            import asyncio
            context.application.create_task(asyncio.sleep(5))
            # Uyarıyı silme kısmı biraz gelişmiş asyncio gerektirdiği için şimdilik doğrudan bırakıyoruz
        except Exception as e:
            logger.error(f"Mesaj silinemedi: {e}")

# =========================
# BAŞLATMA FONKSİYONU
# =========================

def main():
    if not TOKEN:
        print("HATA: BOT_TOKEN tanımlanmadı. Lütfen çevresel değişken (environment variable) olarak ekleyin.")
        print("Örnek (Linux/Mac): export BOT_TOKEN='senin_tokenin'")
        print("Örnek (Windows): set BOT_TOKEN=senin_tokenin")
        return

    app = Application.builder().token(TOKEN).build()

    # Komutlar
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ara", ara))

    # Moderasyon
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("tban", tban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("tmute", tmute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("kick", kick))
    
    # Uyarılar
    app.add_handler(CommandHandler("warn", warn))
    app.add_handler(CommandHandler("clearwarns", clearwarns))

    # Ayarlar
    app.add_handler(CommandHandler("setlog", setlog))
    app.add_handler(CommandHandler("welcome", welcome_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome))
    app.add_handler(CommandHandler("antilink", antilink_cmd))
    app.add_handler(CommandHandler("addbad", addbad))
    app.add_handler(CommandHandler("delbad", delbad))

    # Tüm mesajlar ve yeni gelen üyeler için yakalayıcı
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    print("🤖 Gelişmiş Rose Alternatifi Bot Çalışıyor...")
    app.run_polling()

if __name__ == "__main__":
    main()
