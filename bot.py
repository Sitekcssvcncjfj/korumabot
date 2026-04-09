# bot.py
# Env:
#   BOT_TOKEN=xxxxx
#   DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db   (opsiyonel; yoksa sqlite kullanır)
#   WARN_LIMIT=3
#   MUTE_SECONDS=3600

import os
import re
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ChatMemberUpdated,
    ChatPermissions,
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
        # local fallback
        db_url = "sqlite+aiosqlite:///bot.db"

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

class Warnings(Base):
    __tablename__ = "warnings"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uix_chat_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

async def init_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_or_create_chat_settings(session: AsyncSession, chat_id: int) -> ChatSettings:
    res = await session.execute(select(ChatSettings).where(ChatSettings.chat_id == chat_id))
    row = res.scalar_one_or_none()
    if row:
        return row
    row = ChatSettings(chat_id=chat_id, anti_link=False)
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

async def is_user_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    m = await bot.get_chat_member(chat_id, user_id)
    return getattr(m, "status", None) in ("administrator", "creator")

def parse_target_user_id(message: Message) -> int | None:
    # reply hedefi
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id

    # /cmd 123
    parts = (message.text or "").split()
    if len(parts) >= 2 and parts[1].lstrip("-").isdigit():
        return int(parts[1])
    return None

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
        # genelde varsayılan olarak false:
        can_change_info=False,
        can_pin_messages=False,
    )

async def add_warning_and_maybe_punish(
    *,
    bot: Bot,
    session: AsyncSession,
    config: Config,
    chat_id: int,
    user_id: int,
    reason: str = "Uyarı"
) -> tuple[int, bool]:
    """Returns (warn_count, punished_bool)."""
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

@router.message(Command("start"))
async def start_cmd(message: Message):
    if message.chat and message.chat.type != "private":
        return
    await message.answer(
        "Grup yönetim botu aktif.\n\n"
        "Komutlar:\n"
        "/setwelcome <text>\n"
        "/setrules <text>\n"
        "/rules\n"
        "/antilink on|off\n"
        "/warn (reply)\n"
        "/warnings (reply)\n"
        "/resetwarns (reply)\n"
        "/mute (reply)\n"
        "/unmute (reply)\n"
        "/ban (reply)\n"
        "/unban (reply)\n"
        "/lock\n"
        "/unlock"
    )

# ---- Admin actions ----

@router.message(Command("ban"))
async def ban_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Kullanıcıyı reply ile yanıtlayın veya /ban <user_id> yazın.")

    try:
        await bot.ban_chat_member(message.chat.id, target_id)
        await message.reply(f"Banlandı: <code>{target_id}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unban"))
async def unban_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yapın veya /unban <user_id> yazın.")

    try:
        await bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
        await message.reply(f"Unban: <code>{target_id}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("mute"))
async def mute_cmd(message: Message, bot: Bot, session: AsyncSession, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yapın veya /mute <user_id> yazın.")

    until = datetime.utcnow() + timedelta(seconds=config.mute_seconds)
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await message.reply(f"Mute: <code>{target_id}</code> ({config.mute_seconds}s)")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unmute"))
async def unmute_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yapın veya /unmute <user_id> yazın.")

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_id,
            permissions=permissive_permissions(),
        )
        await message.reply(f"Unmute: <code>{target_id}</code>")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("lock"))
async def lock_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")
    try:
        await bot.set_chat_permissions(message.chat.id, permissions=ChatPermissions(can_send_messages=False))
        await message.reply("Grup kilitlendi (mesaj gönderme kapalı).")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unlock"))
async def unlock_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")
    try:
        await bot.set_chat_permissions(message.chat.id, permissions=permissive_permissions())
        await message.reply("Grup kilidi açıldı (mesaj gönderme açık).")
    except Exception as e:
        await message.reply(f"Hata: {e}")

# ---- Warnings ----

@router.message(Command("warn"))
async def warn_cmd(message: Message, bot: Bot, session: AsyncSession, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Kullanıcıyı reply ile yanıtlayın veya /warn <user_id> yazın.")

    count, punished = await add_warning_and_maybe_punish(
        bot=bot, session=session, config=config,
        chat_id=message.chat.id, user_id=target_id,
        reason="Uyarı"
    )
    if punished:
        await message.reply(
            f"<code>{target_id}</code> uyarı limiti aştı ({count}/{config.warn_limit}). Otomatik mute uygulandı."
        )
    else:
        await message.reply(f"Uyarı verildi: <code>{target_id}</code> ({count}/{config.warn_limit})")

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
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")

    target_id = parse_target_user_id(message)
    if not target_id:
        return await message.reply("Reply yapın veya /resetwarns <user_id> yazın.")

    q = await session.execute(select(Warnings).where(Warnings.chat_id == message.chat.id, Warnings.user_id == target_id))
    row = q.scalar_one_or_none()
    if row:
        row.count = 0
        row.updated_at = datetime.utcnow()
        await session.commit()
    await message.reply(f"Uyarılar sıfırlandı: <code>{target_id}</code>")

# ---- Settings: welcome / rules / antilink ----

@router.message(Command("setwelcome"))
async def setwelcome_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: /setwelcome Hoşgeldin {first_name}!")

    s = await get_or_create_chat_settings(session, message.chat.id)
    s.welcome_text = parts[1]
    await session.commit()
    await message.reply("Welcome mesajı kaydedildi.")

@router.message(Command("setrules"))
async def setrules_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: /setrules Kurallar...")

    s = await get_or_create_chat_settings(session, message.chat.id)
    s.rules_text = parts[1]
    await session.commit()
    await message.reply("Kurallar kaydedildi.")

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
    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("Bu komut sadece adminler için.")

    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: /antilink on | off")

    s = await get_or_create_chat_settings(session, message.chat.id)
    s.anti_link = (parts[1].lower() == "on")
    await session.commit()
    await message.reply(f"Anti-link: {'AÇIK' if s.anti_link else 'KAPALI'}")

# ---- Welcome on join ----

@router.chat_member()
async def welcome_on_join(event: ChatMemberUpdated, bot: Bot, session: AsyncSession):
    if not event.chat:
        return

    old_status = getattr(event.old_chat_member, "status", None)
    new_status = getattr(event.new_chat_member, "status", None)
    if old_status in ("left", "kicked") and new_status == "member":
        s = await get_or_create_chat_settings(session, event.chat.id)
        if not s.welcome_text:
            return

        user = event.new_chat_member.user
        text = s.welcome_text
        text = text.replace("{first_name}", user.first_name or "")
        text = text.replace("{username}", f"@{user.username}" if user.username else (user.first_name or ""))
        await bot.send_message(event.chat.id, text)

# ---- Anti-link (auto delete + optional auto-warn) ----

@router.message()
async def anti_link_handler(message: Message, bot: Bot, session: AsyncSession, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.text:
        return

    s = await get_or_create_chat_settings(session, message.chat.id)
    if not s.anti_link:
        return

    if await is_user_admin(bot, message.chat.id, message.from_user.id):
        return

    if URL_RE.search(message.text):
        try:
            await message.delete()
        except:
            pass

        # İstersen kapatabilirsin: auto-warn
        count, punished = await add_warning_and_maybe_punish(
            bot=bot, session=session, config=config,
            chat_id=message.chat.id, user_id=message.from_user.id,
            reason="Link"
        )
        try:
            if punished:
                await message.answer(
                    f"Link yasak. <code>{message.from_user.id}</code> uyarı limiti aştı ({count}/{config.warn_limit}) → mute."
                )
            else:
                await message.answer(
                    f"Link yasak. Mesaj silindi. Uyarı: {count}/{config.warn_limit}"
                )
        except:
            pass

# ----------------- MAIN -----------------

async def main():
    logging.basicConfig(level=logging.INFO)

    config = load_config()
    engine = create_async_engine(config.database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    await init_db(engine)

    bot = Bot(token=config.bot_token, parse_mode=ParseMode.HTML)
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(DbSessionMiddleware(session_factory))
    dp.update.middleware(ConfigMiddleware(config))

    dp.include_router(router)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
