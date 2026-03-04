import datetime
import time
from collections import defaultdict

from telegram import *
from telegram.ext import *
from duckduckgo_search import DDGS
from groq import Groq

TOKEN="8787679143:AAGRBpARDCSG5-ktbmf_fLiIFaDT7IuwP2s"
GROQ="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"

client=Groq(api_key=GROQ)

MODEL="llama3-70b-8192"

stats=defaultdict(dict)
warns=defaultdict(int)
badwords=defaultdict(list)
messages=defaultdict(list)
antilink=defaultdict(bool)

# -----------------
# ADMIN CHECK
# -----------------

async def admin(update,context):

 user=update.effective_user.id
 chat=update.effective_chat.id

 admins=await context.bot.get_chat_administrators(chat)

 return user in [a.user.id for a in admins]

# -----------------
# START
# -----------------

async def start(update,context):

 kb=[[InlineKeyboardButton("➕ Beni Gruba Ekle",
 url=f"https://t.me/{context.bot.username}?startgroup=true")]]

 await update.message.reply_text(
 "🤖 Moderasyon Botu Aktif",
 reply_markup=InlineKeyboardMarkup(kb)
 )

# -----------------
# HELP
# -----------------

async def help(update,context):

 text="""
/ban /kick /mute /unmute
/warn /warns /clearwarns
/addbad /delbad /badlist
/pin /unpin
/antilink on|off
/ai
/search
/ping
"""

 await update.message.reply_text(text)

# -----------------
# BAN
# -----------------

async def ban(update,context):

 if not await admin(update,context):return
 if not update.message.reply_to_message:return

 user=update.message.reply_to_message.from_user.id

 await context.bot.ban_chat_member(update.effective_chat.id,user)

 await update.message.reply_text("🚫 banlandı")

async def unban(update,context):

 if not await admin(update,context):return
 if not context.args:return

 user=int(context.args[0])

 await context.bot.unban_chat_member(update.effective_chat.id,user)

 await update.message.reply_text("✅ ban kaldırıldı")

async def kick(update,context):

 if not await admin(update,context):return
 if not update.message.reply_to_message:return

 user=update.message.reply_to_message.from_user.id

 await context.bot.ban_chat_member(update.effective_chat.id,user)
 await context.bot.unban_chat_member(update.effective_chat.id,user)

 await update.message.reply_text("👢 atıldı")

# -----------------
# MUTE
# -----------------

async def mute(update,context):

 if not await admin(update,context):return
 if not update.message.reply_to_message:return

 user=update.message.reply_to_message.from_user.id

 await context.bot.restrict_chat_member(
 update.effective_chat.id,
 user,
 ChatPermissions()
 )

 await update.message.reply_text("🔇 susturuldu")

async def unmute(update,context):

 if not await admin(update,context):return
 if not update.message.reply_to_message:return

 user=update.message.reply_to_message.from_user.id

 await context.bot.restrict_chat_member(
 update.effective_chat.id,
 user,
 ChatPermissions(can_send_messages=True)
 )

 await update.message.reply_text("🔊 susturma kaldırıldı")

# -----------------
# WARN
# -----------------

async def warn(update,context):

 if not await admin(update,context):return
 if not update.message.reply_to_message:return

 uid=update.message.reply_to_message.from_user.id

 warns[uid]+=1

 await update.message.reply_text(f"⚠ warn {warns[uid]}/3")

 if warns[uid]>=3:

  await context.bot.ban_chat_member(update.effective_chat.id,uid)

  await update.message.reply_text("🚫 3 warn ban")

async def warns_cmd(update,context):

 if not update.message.reply_to_message:return

 uid=update.message.reply_to_message.from_user.id

 await update.message.reply_text(f"warn: {warns[uid]}")

async def clearwarns(update,context):

 if not await admin(update,context):return
 if not update.message.reply_to_message:return

 uid=update.message.reply_to_message.from_user.id

 warns[uid]=0

 await update.message.reply_text("warn temizlendi")

# -----------------
# BAD WORD
# -----------------

async def addbad(update,context):

 if not await admin(update,context):return
 if not context.args:return

 chat=update.effective_chat.id

 badwords[chat].append(context.args[0])

 await update.message.reply_text("kelime eklendi")

async def delbad(update,context):

 if not await admin(update,context):return
 if not context.args:return

 chat=update.effective_chat.id
 word=context.args[0]

 if word in badwords[chat]:
  badwords[chat].remove(word)

 await update.message.reply_text("silindi")

async def badlist(update,context):

 chat=update.effective_chat.id

 await update.message.reply_text(str(badwords[chat]))

# -----------------
# PIN
# -----------------

async def pin(update,context):

 if not await admin(update,context):return
 if not update.message.reply_to_message:return

 await context.bot.pin_chat_message(
 update.effective_chat.id,
 update.message.reply_to_message.message_id
 )

async def unpin(update,context):

 if not await admin(update,context):return

 await context.bot.unpin_all_chat_messages(update.effective_chat.id)

# -----------------
# ANTILINK
# -----------------

async def antilink_cmd(update,context):

 if not await admin(update,context):return
 if not context.args:return

 chat=update.effective_chat.id

 if context.args[0]=="on":

  antilink[chat]=True
  await update.message.reply_text("🔗 antilink açık")

 else:

  antilink[chat]=False
  await update.message.reply_text("🔗 antilink kapalı")

# -----------------
# SPAM
# -----------------

def spam(chat,user):

 now=datetime.datetime.now()

 messages[(chat,user)].append(now)

 messages[(chat,user)]=[
 t for t in messages[(chat,user)]
 if (now-t).seconds<5
 ]

 return len(messages[(chat,user)])>5

# -----------------
# AI
# -----------------

def ai(text):

 r=""

 stream=client.chat.completions.create(
 model=MODEL,
 messages=[{"role":"user","content":text}],
 stream=True
 )

 for c in stream:

  if c.choices[0].delta.content:

   r+=c.choices[0].delta.content

 return r

# -----------------
# MESSAGE
# -----------------

async def message(update,context):

 chat=update.effective_chat.id
 user=update.message.from_user.id
 text=update.message.text or ""

 if spam(chat,user):

  await update.message.delete()
  return

 if antilink[chat] and "http" in text:

  await update.message.delete()
  return

 for w in badwords[chat]:

  if w in text.lower():

   await update.message.delete()
   return

 stats[chat][user]=stats[chat].get(user,0)+1

 if text.startswith("ara "):

  q=text.replace("ara ","")

  r=""

  with DDGS() as d:
   for s in d.text(q,max_results=3):

    r+=s["title"]+"\n"

  await update.message.reply_text(r)

 if text.startswith("ai "):

  await update.message.reply_text(ai(text))

# -----------------
# PING
# -----------------

async def ping(update,context):

 start=time.time()

 msg=await update.message.reply_text("pong")

 end=time.time()

 await msg.edit_text(f"🏓 {round((end-start)*1000)} ms")

# -----------------
# MAIN
# -----------------

app=Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start",start))
app.add_handler(CommandHandler("help",help))

app.add_handler(CommandHandler("ban",ban))
app.add_handler(CommandHandler("unban",unban))
app.add_handler(CommandHandler("kick",kick))

app.add_handler(CommandHandler("mute",mute))
app.add_handler(CommandHandler("unmute",unmute))

app.add_handler(CommandHandler("warn",warn))
app.add_handler(CommandHandler("warns",warns_cmd))
app.add_handler(CommandHandler("clearwarns",clearwarns))

app.add_handler(CommandHandler("addbad",addbad))
app.add_handler(CommandHandler("delbad",delbad))
app.add_handler(CommandHandler("badlist",badlist))

app.add_handler(CommandHandler("pin",pin))
app.add_handler(CommandHandler("unpin",unpin))

app.add_handler(CommandHandler("antilink",antilink_cmd))

app.add_handler(CommandHandler("ping",ping))

app.add_handler(MessageHandler(filters.TEXT,message))

print("BOT ÇALIŞIYOR")

app.run_polling()
