import datetime
from telegram import *
from telegram.ext import *
from collections import defaultdict
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

# -----------------
# START
# -----------------

async def start(update,context):

 kb=[
 [
 InlineKeyboardButton("➕ Beni Gruba Ekle",url=f"https://t.me/{context.bot.username}?startgroup=true")
 ],
 [
 InlineKeyboardButton("👥 Grup",url="https://t.me/pavyonfre"),
 InlineKeyboardButton("📢 Kanal",url="https://t.me/aminogluotomasyon")
 ],
 [
 InlineKeyboardButton("💬 Destek",url="https://t.me/garibansikenholding"),
 InlineKeyboardButton("🌍 Dil",callback_data="lang")
 ]
 ]

 await update.message.reply_text(
 "🤖 Moderation AI Bot",
 reply_markup=InlineKeyboardMarkup(kb)
 )

# -----------------
# BAN
# -----------------

async def ban(update,context):

 if not update.message.reply_to_message:return

 user=update.message.reply_to_message.from_user.id

 await context.bot.ban_chat_member(update.effective_chat.id,user)

 await update.message.reply_text("🚫 banlandı")

# -----------------
# MUTE
# -----------------

async def mute(update,context):

 if not update.message.reply_to_message:return

 user=update.message.reply_to_message.from_user.id

 await context.bot.restrict_chat_member(
 update.effective_chat.id,
 user,
 ChatPermissions()
 )

 await update.message.reply_text("🔇 sessize alındı")

# -----------------
# WARN
# -----------------

async def warn(update,context):

 if not update.message.reply_to_message:return

 uid=update.message.reply_to_message.from_user.id

 warns[uid]+=1

 await update.message.reply_text(f"⚠️ uyarı ({warns[uid]})")

# -----------------
# BAD WORD
# -----------------

async def addbad(update,context):

 chat=update.effective_chat.id
 word=context.args[0]

 badwords[chat].append(word)

 await update.message.reply_text("kelime eklendi")

async def delbad(update,context):

 chat=update.effective_chat.id
 word=context.args[0]

 if word in badwords[chat]:
  badwords[chat].remove(word)

 await update.message.reply_text("kelime silindi")

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

 if len(messages[(chat,user)])>5:
  return True

 return False

# -----------------
# AI
# -----------------

def ai(text):

 stream=client.chat.completions.create(
 model=MODEL,
 messages=[{"role":"user","content":text}],
 stream=True
 )

 r=""

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
 text=update.message.text

 if spam(chat,user):

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

  return

 if text.startswith("ai "):

  r=ai(text)

  await update.message.reply_text(r)

# -----------------
# MAIN
# -----------------

app=Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start",start))

app.add_handler(CommandHandler("ban",ban))
app.add_handler(CommandHandler("mute",mute))

app.add_handler(CommandHandler("warn",warn))

app.add_handler(CommandHandler("addbad",addbad))
app.add_handler(CommandHandler("delbad",delbad))

app.add_handler(MessageHandler(filters.TEXT,message))

print("BOT ÇALIŞIYOR")

app.run_polling()
