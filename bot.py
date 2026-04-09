# bot.py
# Gerekli ENV:
#   BOT_TOKEN=123456:ABC...
#   DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname   (opsiyonel; yoksa sqlite kullanır)
#   WARN_LIMIT=3
#   MUTE_SECONDS=3600
#
# requirements.txt (Railway için şart):
#   aiogram==3.6.0
#   SQLAlchemy==2.0.30
#   asyncpg==0.29.0
#   python-dotenv==1.0.1
#   aiosqlite==0.20.0

import os
import re
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import BaseMiddleware

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)

# -------- GLOBALS --------
BOT_ID: Optional[int] = None

# ----------------- CONFIG -----------------

@dataclass
class Config:
    bot_token: str
    database_url: str
    warn_limit: int
    mute_seconds: int

def load_config() -> Config:
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN missing")

    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        db_url = "sqlite+aiosqlite:///bot.db"  # local fallback

    return Config(
        bot_token=token,
        database_url=db_url,
        warn_limit=int(os.getenv("WARN_LIMIT", "3")),
        mute_seconds=int(os.getenv("MUTE_SECONDS", "3600")),
    )

# ----------------- DB -----------------

class Base(DeclarativeBase):
    pass

class ChatSettings(Base):
    __tablename__ = "chat_settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    welcome_text: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    rules_text: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    anti_link: Mapped[bool] = mapped_column(Boolean, default=False)

    # ekstra özellikler:
    log_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    captcha: Mapped[bool] = mapped_column(Boolean, default=False)

class Warnings(Base):
    __tablename__ = "warnings"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uix_chat_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (UniqueConstraint("chat_id", "name", name="uix_chat_note"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(String(4000), nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Filter(Base):
    __tablename__ = "filters"
    __table_args__ = (UniqueConstraint("chat_id", "keyword", name="uix_chat_filter"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    keyword: Mapped[str] = mapped_column(String(64), nullable=False)   # lower
    response: Mapped[str] = mapped_column(String(4000), nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class PendingVerify(Base):
    __tablename__ = "pending_verify"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uix_chat_verify_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

async def init_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_or_create_chat_settings(session: AsyncSession, chat_id: int) -> ChatSettings:
    res = await session.execute(select(ChatSettings).where(ChatSettings.chat_id == chat_id))
    row = res.scalar_one_or_none()
    if row:
        return row
    row = ChatSettings(chat_id=chat_id, anti_link=False, captcha=False, log_chat_id=None)
    session.add(row)
    await session.commit()
    return row

# ----------------- MIDDLEWARE (DI) -----------------

class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def __call__(self, handler, event, data):
        async with self.session_factory() as session:
            data["session"] = session
            return await handler(event, data)

class ConfigMiddleware(BaseMiddleware):
    def __init__(self, config: Config):
        self.config = config

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)

# ----------------- HELPERS -----------------

URL_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|www\.)\S+", re.IGNORECASE)

def permissive_permissions() -> ChatPermissions:
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
        can_manage_topics=True,
        can_change_info=False,
        can_pin_messages=False,
    )

def parse_target_user_id(message: Message) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    parts = (message.text or "").split()
    if len(parts) >= 2 and parts[1].lstrip("-").isdigit():
        return int(parts[1])
    return None

async def _get_member(bot: Bot, chat_id: int, user_id: int):
    return await bot.get_chat_member(chat_id, user_id)

def _is_admin_status(member) -> bool:
    return getattr(member, "status", None) in ("administrator", "creator")

def _has_right(member, right: str) -> bool:
    # creator = full
    if getattr(member, "status", None) == "creator":
        return True
    if getattr(member, "status", None) != "administrator":
        return False
    return bool(getattr(member, right, False))

async def require_user_right(bot: Bot, chat_id: int, user_id: int, right: str | None = None) -> bool:
    m = await _get_member(bot, chat_id, user_id)
    if not _is_admin_status(m):
        return False
    if right is None:
        return True
    return _has_right(m, right)

async def require_bot_right(bot: Bot, chat_id: int, right: str | None = None) -> bool:
    global BOT_ID
    if BOT_ID is None:
        me = await bot.get_me()
        BOT_ID = me.id
    m = await _get_member(bot, chat_id, BOT_ID)
    if not _is_admin_status(m):
        return False
    if right is None:
        return True
    return _has_right(m, right)

async def log_action(session: AsyncSession, bot: Bot, chat_id: int, text: str):
    s = await get_or_create_chat_settings(session, chat_id)
    if not s.log_chat_id:
        return
    try:
        await bot.send_message(s.log_chat_id, f"[LOG] chat:{chat_id}\n{text}")
    except:
        pass

async def add_warning_and_maybe_punish(
    *,
    bot: Bot,
    session: AsyncSession,
    config: Config,
    chat_id: int,
    user_id: int,
) -> tuple[int, bool]:
    q = await session.execute(select(Warnings).where(Warnings.chat_id == chat_id, Warnings.user_id == user_id))
    row = q.scalar_one_or_none()
    if not row:
        row = Warnings(chat_id=chat_id, user_id=user_id, count=0)
        session.add(row)

    row.count += 1
    row.updated_at = datetime.utcnow()
    await session.commit()

    if row.count >= config.warn_limit:
        until = datetime.utcnow() + timedelta(seconds=config.mute_seconds)
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
        except:
            pass
        return row.count, True

    return row.count, False

# ----------------- ROUTER -----------------

router = Router()

# ---- basic ----

@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.reply(
        "Komutlar:\n"
        "Genel: /rules, /get <not>, /notes\n"
        "Admin: /ban /unban, /kick, /mute /unmute, /warn /warnings /resetwarns,\n"
        "       /lock /unlock, /pin /unpin /unpinall, /purge,\n"
        "       /setwelcome, /setrules, /antilink on|off,\n"
        "       /save <ad> <içerik>, /delnote <ad>,\n"
        "       /filter <kelime> <cevap>, /stop <kelime>,\n"
        "       /captcha on|off, /setlog <chat_id>"
    )

@router.message(Command("start"))
async def start_cmd(message: Message):
    if message.chat and message.chat.type != "private":
        return
    await message.answer("Bot aktif. /help yaz.")

@router.message(Command("id"))
async def id_cmd(message: Message):
    if not message.chat:
        return
    await message.reply(f"chat_id: <code>{message.chat.id}</code>")

# ---- settings ----

@router.message(Command("setwelcome"))
async def setwelcome_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    if not await require_bot_right(bot, message.chat.id, None):
        return await message.reply("Bot admin değil.")

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: /setwelcome Hoşgeldin {first_name}!")

    s = await get_or_create_chat_settings(session, message.chat.id)
    s.welcome_text = parts[1]
    await session.commit()
    await message.reply("Welcome kaydedildi.")
    await log_action(session, bot, message.chat.id, f"setwelcome by {message.from_user.id}")

@router.message(Command("setrules"))
async def setrules_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    if not await require_bot_right(bot, message.chat.id, None):
        return await message.reply("Bot admin değil.")

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: /setrules Kurallar...")

    s = await get_or_create_chat_settings(session, message.chat.id)
    s.rules_text = parts[1]
    await session.commit()
    await message.reply("Kurallar kaydedildi.")
    await log_action(session, bot, message.chat.id, f"setrules by {message.from_user.id}")

@router.message(Command("rules"))
async def rules_cmd(message: Message, session: AsyncSession):
    if not message.chat or message.chat.type == "private":
        return
    s = await get_or_create_chat_settings(session, message.chat.id)
    await message.reply(s.rules_text or "Bu grupta henüz kural tanımlanmamış.")

@router.message(Command("antilink"))
async def antilink_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: /antilink on | off")

    s = await get_or_create_chat_settings(session, message.chat.id)
    s.anti_link = (parts[1].lower() == "on")
    await session.commit()
    await message.reply(f"Anti-link: {'AÇIK' if s.anti_link else 'KAPALI'}")
    await log_action(session, bot, message.chat.id, f"antilink={s.anti_link} by {message.from_user.id}")

@router.message(Command("setlog"))
async def setlog_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        return await message.reply("Kullanım: /setlog <log_chat_id>\nÖrn: /setlog -1001234567890")

    s = await get_or_create_chat_settings(session, message.chat.id)
    s.log_chat_id = int(parts[1])
    await session.commit()
    await message.reply(f"Log chat ayarlandı: <code>{s.log_chat_id}</code>")

# ---- captcha ----

@router.message(Command("captcha"))
async def captcha_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    # captcha için restrict yetkisi mantıklı
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: /captcha on | off")

    s = await get_or_create_chat_settings(session, message.chat.id)
    s.captcha = (parts[1].lower() == "on")
    await session.commit()
    await message.reply(f"Captcha: {'AÇIK' if s.captcha else 'KAPALI'}")

# ---- moderation ----

@router.message(Command("ban"))
async def ban_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return

    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok (can_restrict_members).")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok (can_restrict_members).")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yap veya /ban <user_id>.")

    try:
        await bot.ban_chat_member(message.chat.id, target_id)
        await message.reply(f"Banlandı: <code>{target_id}</code>")
        await log_action(session, bot, message.chat.id, f"BAN target={target_id} by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unban"))
async def unban_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return

    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok (can_restrict_members).")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok (can_restrict_members).")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yap veya /unban <user_id>.")

    try:
        await bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
        await message.reply(f"Unban: <code>{target_id}</code>")
        await log_action(session, bot, message.chat.id, f"UNBAN target={target_id} by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("kick"))
async def kick_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return

    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok (can_restrict_members).")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok (can_restrict_members).")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yap veya /kick <user_id>.")

    try:
        await bot.ban_chat_member(message.chat.id, target_id)
        await bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
        await message.reply(f"Kicked: <code>{target_id}</code>")
        await log_action(session, bot, message.chat.id, f"KICK target={target_id} by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("mute"))
async def mute_cmd(message: Message, bot: Bot, session: AsyncSession, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return

    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok (can_restrict_members).")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok (can_restrict_members).")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yap veya /mute <user_id>.")

    until = datetime.utcnow() + timedelta(seconds=config.mute_seconds)
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await message.reply(f"Mute: <code>{target_id}</code> ({config.mute_seconds}s)")
        await log_action(session, bot, message.chat.id, f"MUTE target={target_id} by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unmute"))
async def unmute_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return

    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok (can_restrict_members).")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok (can_restrict_members).")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yap veya /unmute <user_id>.")

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_id,
            permissions=permissive_permissions(),
        )
        await message.reply(f"Unmute: <code>{target_id}</code>")
        await log_action(session, bot, message.chat.id, f"UNMUTE target={target_id} by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("warn"))
async def warn_cmd(message: Message, bot: Bot, session: AsyncSession, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return

    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok (can_restrict_members).")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok (can_restrict_members).")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yap veya /warn <user_id>.")

    count, punished = await add_warning_and_maybe_punish(
        bot=bot, session=session, config=config,
        chat_id=message.chat.id, user_id=target_id
    )
    if punished:
        await message.reply(f"<code>{target_id}</code> limit aştı ({count}/{config.warn_limit}) → otomatik mute.")
    else:
        await message.reply(f"Uyarı: <code>{target_id}</code> ({count}/{config.warn_limit})")

    await log_action(session, bot, message.chat.id, f"WARN target={target_id} count={count} by={message.from_user.id}")

@router.message(Command("warnings"))
async def warnings_cmd(message: Message, session: AsyncSession):
    if not message.chat or message.chat.type == "private":
        return

    target_id = parse_target_user_id(message) or (message.from_user.id if message.from_user else None)
    if not target_id:
        return

    q = await session.execute(select(Warnings).where(Warnings.chat_id == message.chat.id, Warnings.user_id == target_id))
    row = q.scalar_one_or_none()
    await message.reply(f"<code>{target_id}</code> uyarı sayısı: {row.count if row else 0}")

@router.message(Command("resetwarns"))
async def resetwarns_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return

    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok (can_restrict_members).")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok (can_restrict_members).")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yap veya /resetwarns <user_id>.")

    q = await session.execute(select(Warnings).where(Warnings.chat_id == message.chat.id, Warnings.user_id == target_id))
    row = q.scalar_one_or_none()
    if row:
        row.count = 0
        row.updated_at = datetime.utcnow()
        await session.commit()

    await message.reply(f"Uyarılar sıfırlandı: <code>{target_id}</code>")
    await log_action(session, bot, message.chat.id, f"RESETWARNS target={target_id} by={message.from_user.id}")

@router.message(Command("lock"))
async def lock_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok (can_restrict_members).")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok (can_restrict_members).")

    try:
        await bot.set_chat_permissions(message.chat.id, permissions=ChatPermissions(can_send_messages=False))
        await message.reply("Grup kilitlendi.")
        await log_action(session, bot, message.chat.id, f"LOCK by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unlock"))
async def unlock_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok (can_restrict_members).")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok (can_restrict_members).")

    try:
        await bot.set_chat_permissions(message.chat.id, permissions=permissive_permissions())
        await message.reply("Grup kilidi açıldı.")
        await log_action(session, bot, message.chat.id, f"UNLOCK by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

# ---- pin / purge ----

@router.message(Command("pin"))
async def pin_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_pin_messages"):
        return await message.reply("Yetki yok (can_pin_messages).")
    if not await require_bot_right(bot, message.chat.id, "can_pin_messages"):
        return await message.reply("Bot yetkisi yok (can_pin_messages).")
    if not message.reply_to_message:
        return await message.reply("Pin için bir mesaja reply yap.")

    try:
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
        await message.reply("Pinlendi.")
        await log_action(session, bot, message.chat.id, f"PIN msg={message.reply_to_message.message_id} by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unpin"))
async def unpin_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_pin_messages"):
        return await message.reply("Yetki yok (can_pin_messages).")
    if not await require_bot_right(bot, message.chat.id, "can_pin_messages"):
        return await message.reply("Bot yetkisi yok (can_pin_messages).")

    # reply varsa o mesajı unpin dene, yoksa son pini kaldır
    try:
        if message.reply_to_message:
            await bot.unpin_chat_message(message.chat.id, message.reply_to_message.message_id)
        else:
            await bot.unpin_chat_message(message.chat.id)
        await message.reply("Unpin OK.")
        await log_action(session, bot, message.chat.id, f"UNPIN by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unpinall"))
async def unpinall_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_pin_messages"):
        return await message.reply("Yetki yok (can_pin_messages).")
    if not await require_bot_right(bot, message.chat.id, "can_pin_messages"):
        return await message.reply("Bot yetkisi yok (can_pin_messages).")
    try:
        await bot.unpin_all_chat_messages(message.chat.id)
        await message.reply("Tüm pinler kaldırıldı.")
        await log_action(session, bot, message.chat.id, f"UNPINALL by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("purge"))
async def purge_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Yetki yok (can_delete_messages).")
    if not await require_bot_right(bot, message.chat.id, "can_delete_messages"):
        return await message.reply("Bot yetkisi yok (can_delete_messages).")
    if not message.reply_to_message:
        return await message.reply("Purge için bir mesaja reply yap (o mesajdan bu komuta kadar siler).")

    start_id = message.reply_to_message.message_id
    end_id = message.message_id
    if end_id <= start_id:
        return await message.reply("Geçersiz aralık.")

    # güvenlik limiti
    MAX_DELETE = 200
    count = 0
    for mid in range(start_id, end_id + 1):
        try:
            await bot.delete_message(message.chat.id, mid)
            count += 1
        except:
            pass
        if count >= MAX_DELETE:
            break
        if count % 25 == 0:
            await asyncio.sleep(0.5)

    await log_action(session, bot, message.chat.id, f"PURGE deleted~{count} by={message.from_user.id}")

# ---- notes ----

@router.message(Command("save"))
async def save_note_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        # not sistemi için illa delete yetkisi şart değil, ama admin şart diyelim
        if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
            return await message.reply("Sadece admin.")

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply("Kullanım: /save <ad> <içerik>")

    name = parts[1].strip().lower()
    content = parts[2].strip()

    q = await session.execute(select(Note).where(Note.chat_id == message.chat.id, Note.name == name))
    row = q.scalar_one_or_none()
    if row:
        row.content = content
        row.created_by = message.from_user.id
        row.created_at = datetime.utcnow()
    else:
        session.add(Note(chat_id=message.chat.id, name=name, content=content, created_by=message.from_user.id))

    await session.commit()
    await message.reply(f"Not kaydedildi: <code>{name}</code>")
    await log_action(session, bot, message.chat.id, f"SAVE note={name} by={message.from_user.id}")

@router.message(Command("get"))
async def get_note_cmd(message: Message, session: AsyncSession):
    if not message.chat or message.chat.type == "private":
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: /get <ad>")

    name = parts[1].strip().lower()
    q = await session.execute(select(Note).where(Note.chat_id == message.chat.id, Note.name == name))
    row = q.scalar_one_or_none()
    if not row:
        return await message.reply("Not bulunamadı.")
    await message.reply(row.content)

@router.message(Command("notes"))
async def list_notes_cmd(message: Message, session: AsyncSession):
    if not message.chat or message.chat.type == "private":
        return
    q = await session.execute(select(Note.name).where(Note.chat_id == message.chat.id).order_by(Note.name.asc()))
    names = [r[0] for r in q.all()]
    if not names:
        return await message.reply("Not yok.")
    await message.reply("Notlar:\n" + "\n".join(f"• <code>{n}</code>" for n in names))

@router.message(Command("delnote"))
async def delnote_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: /delnote <ad>")

    name = parts[1].strip().lower()
    q = await session.execute(select(Note).where(Note.chat_id == message.chat.id, Note.name == name))
    row = q.scalar_one_or_none()
    if not row:
        return await message.reply("Not bulunamadı.")
    await session.delete(row)
    await session.commit()
    await message.reply("Silindi.")
    await log_action(session, bot, message.chat.id, f"DELNOTE note={name} by={message.from_user.id}")

# ---- filters ----

@router.message(Command("filter"))
async def add_filter_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply("Kullanım: /filter <kelime> <cevap>")

    keyword = parts[1].strip().lower()
    response = parts[2].strip()

    q = await session.execute(select(Filter).where(Filter.chat_id == message.chat.id, Filter.keyword == keyword))
    row = q.scalar_one_or_none()
    if row:
        row.response = response
        row.created_by = message.from_user.id
        row.created_at = datetime.utcnow()
    else:
        session.add(Filter(chat_id=message.chat.id, keyword=keyword, response=response, created_by=message.from_user.id))
    await session.commit()

    await message.reply(f"Filter kaydedildi: <code>{keyword}</code>")
    await log_action(session, bot, message.chat.id, f"FILTER keyword={keyword} by={message.from_user.id}")

@router.message(Command("stop"))
async def del_filter_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: /stop <kelime>")

    keyword = parts[1].strip().lower()
    q = await session.execute(select(Filter).where(Filter.chat_id == message.chat.id, Filter.keyword == keyword))
    row = q.scalar_one_or_none()
    if not row:
        return await message.reply("Filter bulunamadı.")
    await session.delete(row)
    await session.commit()
    await message.reply("Filter silindi.")
    await log_action(session, bot, message.chat.id, f"STOP keyword={keyword} by={message.from_user.id}")

# ---- welcome + captcha join ----

@router.chat_member()
async def on_join(event: ChatMemberUpdated, bot: Bot, session: AsyncSession):
    if not event.chat:
        return
    old_status = getattr(event.old_chat_member, "status", None)
    new_status = getattr(event.new_chat_member, "status", None)
    if not (old_status in ("left", "kicked") and new_status == "member"):
        return

    s = await get_or_create_chat_settings(session, event.chat.id)
    user = event.new_chat_member.user

    # captcha açıksa: yeni üyeyi kısıtla + buton
    if s.captcha:
        # bot restrict yetkisi olmalı
        if await require_bot_right(bot, event.chat.id, "can_restrict_members"):
            try:
                await bot.restrict_chat_member(
                    chat_id=event.chat.id,
                    user_id=user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Doğrula", callback_data=f"verify:{user.id}")
                ]])
                msg = await bot.send_message(
                    event.chat.id,
                    f"{user.first_name} doğrulama gerekli. Butona bas.",
                    reply_markup=kb
                )

                # DB'ye yaz
                q = await session.execute(select(PendingVerify).where(
                    PendingVerify.chat_id == event.chat.id,
                    PendingVerify.user_id == user.id
                ))
                row = q.scalar_one_or_none()
                if row:
                    row.message_id = msg.message_id
                    row.created_at = datetime.utcnow()
                else:
                    session.add(PendingVerify(chat_id=event.chat.id, user_id=user.id, message_id=msg.message_id))
                await session.commit()
            except:
                pass

    # welcome
    if s.welcome_text:
        text = s.welcome_text
        text = text.replace("{first_name}", user.first_name or "")
        text = text.replace("{username}", f"@{user.username}" if user.username else (user.first_name or ""))
        try:
            await bot.send_message(event.chat.id, text)
        except:
            pass

@router.callback_query()
async def verify_callback(call: CallbackQuery, bot: Bot, session: AsyncSession):
    if not call.data or not call.message or not call.from_user:
        return
    if not call.data.startswith("verify:"):
        return

    chat_id = call.message.chat.id
    target_id = int(call.data.split(":", 1)[1])

    # sadece o kullanıcı doğrulayabilsin
    if call.from_user.id != target_id:
        return await call.answer("Bu buton sana ait değil.", show_alert=True)

    s = await get_or_create_chat_settings(session, chat_id)
    if not s.captcha:
        return await call.answer("Captcha kapalı.", show_alert=True)

    # bot yetki kontrol
    if not await require_bot_right(bot, chat_id, "can_restrict_members"):
        return await call.answer("Botun yetkisi yok.", show_alert=True)

    try:
        await bot.restrict_chat_member(chat_id=chat_id, user_id=target_id, permissions=permissive_permissions())
    except:
        return await call.answer("Açılamadı. Admin bot yetkilerini kontrol et.", show_alert=True)

    # pending kaydını sil
    q = await session.execute(select(PendingVerify).where(PendingVerify.chat_id == chat_id, PendingVerify.user_id == target_id))
    row = q.scalar_one_or_none()
    if row:
        await session.delete(row)
        await session.commit()

    try:
        await call.message.delete()
    except:
        pass

    await call.answer("Doğrulandı!")

# ---- auto handlers: antilink + filters ----

@router.message()
async def auto_handlers(message: Message, bot: Bot, session: AsyncSession, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.text:
        return

    # komutları elleme
    if message.text.startswith("/"):
        return

    s = await get_or_create_chat_settings(session, message.chat.id)

    # anti-link: admin değilse sil + warn
    if s.anti_link and URL_RE.search(message.text):
        if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
            if await require_bot_right(bot, message.chat.id, "can_delete_messages"):
                try:
                    await message.delete()
                except:
                    pass

            # warn + otomatik mute
            if await require_bot_right(bot, message.chat.id, "can_restrict_members"):
                count, punished = await add_warning_and_maybe_punish(
                    bot=bot, session=session, config=config,
                    chat_id=message.chat.id, user_id=message.from_user.id,
                )
                try:
                    if punished:
                        await message.answer(f"Link yasak. Limit aştın ({count}/{config.warn_limit}) → mute.")
                    else:
                        await message.answer(f"Link yasak. Uyarı: {count}/{config.warn_limit}")
                except:
                    pass

            await log_action(session, bot, message.chat.id, f"ANTILINK user={message.from_user.id}")

        return  # link yakalandıysa filter cevap verme

    # filters (basit: mesaj içinde keyword geçiyorsa cevapla)
    txt = message.text.lower()
    q = await session.execute(select(Filter).where(Filter.chat_id == message.chat.id))
    rows = q.scalars().all()
    for f in rows:
        if f.keyword and f.keyword in txt:
            try:
                await message.reply(f.response)
            except:
                pass
            break

# ----------------- MAIN -----------------

async def main():
    logging.basicConfig(level=logging.INFO)

    config = load_config()
    engine = create_async_engine(config.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    await init_db(engine)

    bot = Bot(token=config.bot_token, parse_mode=ParseMode.HTML)

    global BOT_ID
    BOT_ID = (await bot.get_me()).id

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(DbSessionMiddleware(session_factory))
    dp.update.middleware(ConfigMiddleware(config))

    dp.include_router(router)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
