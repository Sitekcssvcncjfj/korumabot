# bot.py (MissRose mantığına yakın tek dosya)
#
# ENV:
#   BOT_TOKEN=123456:ABC...
#   DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname   (opsiyonel; yoksa sqlite)
#   WARN_LIMIT=3
#   MUTE_SECONDS=3600
#
# requirements.txt:
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
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any

from aiogram import Bot, Dispatcher, Router, BaseMiddleware
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


class PendingVerify(Base):
    __tablename__ = "pending_verify"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uix_chat_verify_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserRegistry(Base):
    """
    @username ile ban/mute/promote vs çalışsın diye user_id cache.
    Bot kullanıcıyı "görürse" (mesaj/katılım/callback) buraya yazar.
    """
    __tablename__ = "user_registry"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)  # lower, no "@"
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_chat_settings(session: AsyncSession, chat_id: int) -> ChatSettings:
    row = (await session.execute(select(ChatSettings).where(ChatSettings.chat_id == chat_id))).scalar_one_or_none()
    if row:
        return row
    row = ChatSettings(chat_id=chat_id, anti_link=False, captcha=False, log_chat_id=None)
    session.add(row)
    await session.commit()
    return row


# ----------------- MIDDLEWARES -----------------

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


class UserTrackMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        session: AsyncSession | None = data.get("session")
        if session:
            u = getattr(event, "from_user", None)
            if u:
                await upsert_user(session, u.id, u.username, u.first_name, u.last_name)
        return await handler(event, data)


# ----------------- HELPERS -----------------

BOT_ID: Optional[int] = None
URL_RE = re.compile(r"(https?://|t\.me/|telegram\.me/|www\.)\S+", re.IGNORECASE)

# Admin cache (Rose /admincache benzeri)
ADMIN_CACHE_TTL_SEC = 600
_admin_cache: Dict[int, Dict[str, Any]] = {}  # chat_id -> {"expires": datetime, "members": {user_id: chatMember}}

DUR_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC like Telegram expects


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


async def upsert_user(session: AsyncSession, user_id: int, username: str | None, first: str | None, last: str | None):
    uname = username.lower() if username else None
    row = (await session.execute(select(UserRegistry).where(UserRegistry.user_id == user_id))).scalar_one_or_none()
    if row:
        row.username = uname
        row.first_name = first
        row.last_name = last
        row.updated_at = datetime.utcnow()
    else:
        session.add(UserRegistry(
            user_id=user_id,
            username=uname,
            first_name=first,
            last_name=last,
            updated_at=datetime.utcnow()
        ))

    try:
        await session.commit()
    except:
        await session.rollback()
        if uname:
            other = (await session.execute(select(UserRegistry).where(UserRegistry.username == uname))).scalar_one_or_none()
            if other and other.user_id != user_id:
                other.username = None
                other.updated_at = datetime.utcnow()
                await session.commit()
            row2 = (await session.execute(select(UserRegistry).where(UserRegistry.user_id == user_id))).scalar_one_or_none()
            if row2:
                row2.username = uname
                row2.first_name = first
                row2.last_name = last
                row2.updated_at = datetime.utcnow()
                await session.commit()


async def resolve_username_to_user_id(session: AsyncSession, username: str) -> Optional[int]:
    u = username.strip()
    if u.startswith("@"):
        u = u[1:]
    u = u.lower().strip()
    if not u:
        return None
    row = (await session.execute(select(UserRegistry).where(UserRegistry.username == u))).scalar_one_or_none()
    return row.user_id if row else None


def _is_admin_status(member) -> bool:
    return getattr(member, "status", None) in ("administrator", "creator")


def _has_right(member, right: str) -> bool:
    if getattr(member, "status", None) == "creator":
        return True
    if getattr(member, "status", None) != "administrator":
        return False
    return bool(getattr(member, right, False))


async def refresh_admin_cache(bot: Bot, chat_id: int):
    admins = await bot.get_chat_administrators(chat_id)
    members = {a.user.id: a for a in admins}
    _admin_cache[chat_id] = {
        "expires": utcnow() + timedelta(seconds=ADMIN_CACHE_TTL_SEC),
        "members": members,
    }


def cache_get_member(chat_id: int, user_id: int):
    c = _admin_cache.get(chat_id)
    if not c:
        return None
    if c["expires"] < utcnow():
        return None
    return c["members"].get(user_id)


async def require_user_right(bot: Bot, chat_id: int, user_id: int, right: str | None) -> bool:
    cached = cache_get_member(chat_id, user_id)
    if cached is not None:
        if right is None:
            return True
        return _has_right(cached, right)

    m = await bot.get_chat_member(chat_id, user_id)
    if not _is_admin_status(m):
        return False
    if right is None:
        return True
    return _has_right(m, right)


async def require_bot_right(bot: Bot, chat_id: int, right: str | None) -> bool:
    global BOT_ID
    if BOT_ID is None:
        BOT_ID = (await bot.get_me()).id

    cached = cache_get_member(chat_id, BOT_ID)
    if cached is not None:
        if right is None:
            return True
        return _has_right(cached, right)

    m = await bot.get_chat_member(chat_id, BOT_ID)
    if not _is_admin_status(m):
        return False
    if right is None:
        return True
    return _has_right(m, right)


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    cached = cache_get_member(chat_id, user_id)
    if cached is not None:
        return True
    m = await bot.get_chat_member(chat_id, user_id)
    return _is_admin_status(m)


async def log_action(session: AsyncSession, bot: Bot, chat_id: int, text: str):
    s = await get_or_create_chat_settings(session, chat_id)
    if not s.log_chat_id:
        return
    try:
        await bot.send_message(s.log_chat_id, f"[LOG] chat:{chat_id}\n{text}")
    except:
        pass


async def parse_target(message: Message, session: AsyncSession) -> Tuple[Optional[int], str]:
    """
    target çözümü:
      - reply
      - text_mention entity
      - arg user_id
      - arg @username (DB)
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        await upsert_user(session, u.id, u.username, u.first_name, u.last_name)
        return u.id, "reply"

    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                u = ent.user
                await upsert_user(session, u.id, u.username, u.first_name, u.last_name)
                return u.id, "text_mention"

    parts = (message.text or "").split()
    if len(parts) < 2:
        return None, "no_target"

    arg = parts[1].strip()
    if arg.lstrip("-").isdigit():
        return int(arg), "id"

    if arg.startswith("@"):
        uid = await resolve_username_to_user_id(session, arg)
        if uid:
            return uid, "username_db"
        return None, "username_unknown"

    return None, "bad_target"


def parse_duration_to_seconds(s: str) -> Optional[int]:
    """
    10m, 2h, 1d, 1w, 1h30m gibi.
    """
    if not s:
        return None
    s = s.strip().lower()
    if s in ("0", "perm", "perma", "permanent", "forever"):
        return 0

    total = 0
    matches = DUR_RE.findall(s.replace(" ", ""))
    if not matches:
        return None

    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    for num, unit in matches:
        total += int(num) * mult[unit.lower()]
    return total if total >= 1 else None


def get_msg_link(chat_id: int, message_id: int) -> str:
    """
    Private/supergroup link: https://t.me/c/<id_without_-100>/<msg_id>
    Works if group is supergroup (usually).
    """
    cid = str(chat_id)
    if cid.startswith("-100"):
        internal = cid[4:]
        return f"https://t.me/c/{internal}/{message_id}"
    return ""


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
        until = utcnow() + timedelta(seconds=config.mute_seconds)
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


async def remove_warning(session: AsyncSession, chat_id: int, user_id: int, n: int = 1) -> int:
    q = await session.execute(select(Warnings).where(Warnings.chat_id == chat_id, Warnings.user_id == user_id))
    row = q.scalar_one_or_none()
    if not row:
        return 0
    row.count = max(0, row.count - max(1, n))
    row.updated_at = datetime.utcnow()
    await session.commit()
    return row.count


def permissive_admin_rights_kwargs() -> dict:
    # Promote sırasında verilecek tipik yetkiler
    return dict(
        can_manage_chat=False,
        can_change_info=False,
        can_delete_messages=True,
        can_manage_video_chats=False,
        can_restrict_members=True,
        can_promote_members=False,
        can_invite_users=True,
        can_pin_messages=True,
        can_manage_topics=True,
        can_post_messages=False,
        can_edit_messages=False,
    )


# ----------------- ROUTER -----------------

router = Router()

@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.reply(
        "Komutlar:\n"
        "Genel: /rules, /id, /whois @username, /report (reply)\n"
        "Ayar: /setwelcome, /setrules, /antilink on|off, /captcha on|off, /setlog <chat_id>\n"
        "Mod: /ban /unban, /kick, /mute /unmute, /tmute, /tban, /warn /unwarn /warnings /resetwarns, /lock /unlock\n"
        "Admin: /promote, /demote, /admincache\n"
        "Pin/Sil: /pin, /unpin, /unpinall, /purge"
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

@router.message(Command("whois"))
async def whois_cmd(message: Message, session: AsyncSession):
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].startswith("@"):
        return await message.reply("Kullanım: /whois @username")
    uid = await resolve_username_to_user_id(session, parts[1])
    if not uid:
        return await message.reply("Bu kullanıcıyı daha önce görmedim (DB'de yok).")
    await message.reply(f"{parts[1]} -> <code>{uid}</code>")

@router.message(Command("admincache"))
async def admincache_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    await refresh_admin_cache(bot, message.chat.id)
    c = _admin_cache.get(message.chat.id, {})
    count = len((c.get("members") or {}).keys())
    await message.reply(f"Admin cache güncellendi. Admin sayısı: {count}")

# ---- settings ----

@router.message(Command("setwelcome"))
async def setwelcome_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Kullanım: /setwelcome Hoşgeldin {first_name}!")
    s = await get_or_create_chat_settings(session, message.chat.id)
    s.welcome_text = parts[1]
    await session.commit()
    await message.reply("Welcome kaydedildi.")

@router.message(Command("setrules"))
async def setrules_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
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
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: /antilink on | off")
    s = await get_or_create_chat_settings(session, message.chat.id)
    s.anti_link = (parts[1].lower() == "on")
    await session.commit()
    await message.reply(f"Anti-link: {'AÇIK' if s.anti_link else 'KAPALI'}")

@router.message(Command("setlog"))
async def setlog_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        return await message.reply("Kullanım: /setlog -1001234567890")
    s = await get_or_create_chat_settings(session, message.chat.id)
    s.log_chat_id = int(parts[1])
    await session.commit()
    await message.reply(f"Log chat ayarlandı: <code>{s.log_chat_id}</code>")

@router.message(Command("captcha"))
async def captcha_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, None):
        return await message.reply("Sadece admin.")
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        return await message.reply("Kullanım: /captcha on | off")
    s = await get_or_create_chat_settings(session, message.chat.id)
    s.captcha = (parts[1].lower() == "on")
    await session.commit()
    await message.reply(f"Captcha: {'AÇIK' if s.captcha else 'KAPALI'}")

# ---- report (users) ----

@router.message(Command("report"))
async def report_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.reply_to_message:
        return await message.reply("Report için bir mesaja reply yap. Örn: (reply) /report spam")

    s = await get_or_create_chat_settings(session, message.chat.id)
    reason = (message.text or "").split(maxsplit=1)
    reason_text = reason[1] if len(reason) > 1 else "-"

    target = message.reply_to_message.from_user
    reporter = message.from_user
    link = get_msg_link(message.chat.id, message.reply_to_message.message_id)

    text = (
        f"🚨 REPORT\n"
        f"Chat: <code>{message.chat.id}</code>\n"
        f"Reporter: <code>{reporter.id}</code> @{reporter.username or '-'}\n"
        f"Target: <code>{target.id}</code> @{target.username or '-'}\n"
        f"Reason: {reason_text}\n"
        f"Link: {link or '-'}"
    )

    if s.log_chat_id:
        try:
            await bot.send_message(s.log_chat_id, text)
            try:
                await bot.forward_message(
                    chat_id=s.log_chat_id,
                    from_chat_id=message.chat.id,
                    message_id=message.reply_to_message.message_id
                )
            except:
                pass
            return await message.reply("Rapor adminlere iletildi.")
        except Exception as e:
            return await message.reply(f"Log'a gönderemedim: {e}")

    # log ayarlı değilse grupta adminleri mentionla (kısa)
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        tags = []
        for a in admins[:6]:
            if a.user.username:
                tags.append("@" + a.user.username)
        tag_text = " ".join(tags) if tags else "Admin"
        await message.reply(f"{tag_text} report: {reason_text}")
    except:
        await message.reply("Report alındı (log ayarlı değil).")

# ---- moderation: ban/unban/kick/mute/unmute ----

async def _target_or_usage(message: Message, session: AsyncSession, usage: str) -> Tuple[Optional[int], bool]:
    target_id, mode = await parse_target(message, session)
    if not target_id:
        if mode == "username_unknown":
            await message.reply("Bu @username DB'de yok. Kullanıcı önce bot tarafından görülmeli (grupta yazsın/katılsın).")
            return None, False
        await message.reply(usage)
        return None, False
    return target_id, True

@router.message(Command("ban"))
async def ban_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    target_id, ok = await _target_or_usage(message, session, "Kullanım: reply ile /ban veya /ban <id> veya /ban @username")
    if not ok:
        return

    try:
        await bot.ban_chat_member(message.chat.id, target_id)
        await message.reply(f"Banlandı: <code>{target_id}</code>")
        await log_action(session, bot, message.chat.id, f"BAN target={target_id} by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("tban"))
async def tban_cmd(message: Message, bot: Bot, session: AsyncSession):
    """
    /tban (reply) 2d [reason]
    /tban @user 2d [reason]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    parts = (message.text or "").split()
    if message.reply_to_message:
        if len(parts) < 2:
            return await message.reply("Kullanım: (reply) /tban 2d [reason]")
        dur_str = parts[1]
        reason = " ".join(parts[2:]) if len(parts) > 2 else ""
    else:
        if len(parts) < 3:
            return await message.reply("Kullanım: /tban @username 2d [reason]")
        dur_str = parts[2]
        reason = " ".join(parts[3:]) if len(parts) > 3 else ""

    target_id, ok = await _target_or_usage(
        message, session, "Kullanım: (reply) /tban 2d [reason] veya /tban @username 2d [reason]"
    )
    if not ok:
        return

    sec = parse_duration_to_seconds(dur_str)
    if sec is None:
        return await message.reply("Süre formatı: 10m, 2h, 1d, 1w, 1h30m")

    until = None if sec == 0 else (utcnow() + timedelta(seconds=sec))
    try:
        await bot.ban_chat_member(message.chat.id, target_id, until_date=until)
        await message.reply(f"T-Ban: <code>{target_id}</code> süre={dur_str}")
        await log_action(session, bot, message.chat.id, f"TBAN target={target_id} dur={dur_str} by={message.from_user.id} reason={reason}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unban"))
async def unban_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    target_id, ok = await _target_or_usage(message, session, "Kullanım: reply ile /unban veya /unban <id> veya /unban @username")
    if not ok:
        return

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
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    target_id, ok = await _target_or_usage(message, session, "Kullanım: reply ile /kick veya /kick <id> veya /kick @username")
    if not ok:
        return

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
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    target_id, ok = await _target_or_usage(message, session, "Kullanım: reply ile /mute veya /mute <id> veya /mute @username")
    if not ok:
        return

    until = utcnow() + timedelta(seconds=config.mute_seconds)
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

@router.message(Command("tmute"))
async def tmute_cmd(message: Message, bot: Bot, session: AsyncSession):
    """
    /tmute (reply) 10m [reason]
    /tmute @user 2h [reason]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    parts = (message.text or "").split()
    if message.reply_to_message:
        if len(parts) < 2:
            return await message.reply("Kullanım: (reply) /tmute 10m [reason]")
        dur_str = parts[1]
        reason = " ".join(parts[2:]) if len(parts) > 2 else ""
    else:
        if len(parts) < 3:
            return await message.reply("Kullanım: /tmute @username 10m [reason]")
        dur_str = parts[2]
        reason = " ".join(parts[3:]) if len(parts) > 3 else ""

    target_id, ok = await _target_or_usage(
        message, session, "Kullanım: (reply) /tmute 10m [reason] veya /tmute @username 10m [reason]"
    )
    if not ok:
        return

    sec = parse_duration_to_seconds(dur_str)
    if sec is None or sec == 0:
        return await message.reply("Süre formatı: 10m, 2h, 1d, 1w, 1h30m (0/perm kabul edilmez)")

    until = utcnow() + timedelta(seconds=sec)
    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await message.reply(f"T-Mute: <code>{target_id}</code> süre={dur_str}")
        await log_action(session, bot, message.chat.id, f"TMUTE target={target_id} dur={dur_str} by={message.from_user.id} reason={reason}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unmute"))
async def unmute_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    target_id, ok = await _target_or_usage(message, session, "Kullanım: reply ile /unmute veya /unmute <id> veya /unmute @username")
    if not ok:
        return

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

# ---- warnings ----

@router.message(Command("warn"))
async def warn_cmd(message: Message, bot: Bot, session: AsyncSession, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    target_id, ok = await _target_or_usage(message, session, "Kullanım: reply ile /warn veya /warn <id> veya /warn @username")
    if not ok:
        return

    count, punished = await add_warning_and_maybe_punish(
        bot=bot, session=session, config=config, chat_id=message.chat.id, user_id=target_id
    )
    if punished:
        await message.reply(f"<code>{target_id}</code> limit aştı ({count}/{config.warn_limit}) → otomatik mute.")
    else:
        await message.reply(f"Uyarı: <code>{target_id}</code> ({count}/{config.warn_limit})")

    await log_action(session, bot, message.chat.id, f"WARN target={target_id} count={count} by={message.from_user.id}")

@router.message(Command("unwarn"))
async def unwarn_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")

    target_id, ok = await _target_or_usage(message, session, "Kullanım: reply ile /unwarn veya /unwarn <id> veya /unwarn @username")
    if not ok:
        return

    new_count = await remove_warning(session, message.chat.id, target_id, n=1)
    await message.reply(f"Uyarı düşürüldü: <code>{target_id}</code> (şimdi: {new_count})")
    await log_action(session, bot, message.chat.id, f"UNWARN target={target_id} now={new_count} by={message.from_user.id}")

@router.message(Command("warnings"))
async def warnings_cmd(message: Message, session: AsyncSession):
    if not message.chat or message.chat.type == "private":
        return
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        target_id = message.from_user.id if message.from_user else None
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
        return await message.reply("Yetki yok: can_restrict_members")

    target_id, ok = await _target_or_usage(message, session, "Kullanım: reply ile /resetwarns veya /resetwarns <id> veya /resetwarns @username")
    if not ok:
        return

    q = await session.execute(select(Warnings).where(Warnings.chat_id == message.chat.id, Warnings.user_id == target_id))
    row = q.scalar_one_or_none()
    if row:
        row.count = 0
        row.updated_at = datetime.utcnow()
        await session.commit()

    await message.reply(f"Uyarılar sıfırlandı: <code>{target_id}</code>")
    await log_action(session, bot, message.chat.id, f"RESETWARNS target={target_id} by={message.from_user.id}")

# ---- lock/unlock ----

@router.message(Command("lock"))
async def lock_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_restrict_members"):
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

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
        return await message.reply("Yetki yok: can_restrict_members")
    if not await require_bot_right(bot, message.chat.id, "can_restrict_members"):
        return await message.reply("Bot yetkisi yok: can_restrict_members")

    try:
        await bot.set_chat_permissions(message.chat.id, permissions=permissive_permissions())
        await message.reply("Grup kilidi açıldı.")
        await log_action(session, bot, message.chat.id, f"UNLOCK by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

# ---- promote/demote (yönetici ekleme-alma) ----

@router.message(Command("promote"))
async def promote_cmd(message: Message, bot: Bot, session: AsyncSession):
    """
    /promote (reply) [title]
    /promote @username [title]
    /promote <id> [title]
    """
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_promote_members"):
        return await message.reply("Yetki yok: can_promote_members")
    if not await require_bot_right(bot, message.chat.id, "can_promote_members"):
        return await message.reply("Bot yetkisi yok: can_promote_members")

    target_id, mode = await parse_target(message, session)
    if not target_id:
        if mode == "username_unknown":
            return await message.reply("Bu @username DB'de yok. Kullanıcı önce bot tarafından görülmeli.")
        return await message.reply("Kullanım: reply ile /promote veya /promote <id> veya /promote @username [title]")

    parts = (message.text or "").split(maxsplit=2)
    title = parts[2].strip() if len(parts) >= 3 else None

    try:
        await bot.promote_chat_member(chat_id=message.chat.id, user_id=target_id, **permissive_admin_rights_kwargs())
        if title:
            try:
                await bot.set_chat_administrator_custom_title(message.chat.id, target_id, title[:16])
            except:
                pass
        await message.reply(f"Promote OK: <code>{target_id}</code>")
        await log_action(session, bot, message.chat.id, f"PROMOTE target={target_id} by={message.from_user.id} title={title}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("demote"))
async def demote_cmd(message: Message, bot: Bot, session: AsyncSession):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_promote_members"):
        return await message.reply("Yetki yok: can_promote_members")
    if not await require_bot_right(bot, message.chat.id, "can_promote_members"):
        return await message.reply("Bot yetkisi yok: can_promote_members")

    target_id, mode = await parse_target(message, session)
    if not target_id:
        if mode == "username_unknown":
            return await message.reply("Bu @username DB'de yok. Kullanıcı önce bot tarafından görülmeli.")
        return await message.reply("Kullanım: reply ile /demote veya /demote <id> veya /demote @username")

    try:
        await bot.promote_chat_member(
            chat_id=message.chat.id,
            user_id=target_id,
            can_manage_chat=False,
            can_change_info=False,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_manage_topics=False,
            can_post_messages=False,
            can_edit_messages=False,
        )
        await message.reply(f"Demote OK: <code>{target_id}</code>")
        await log_action(session, bot, message.chat.id, f"DEMOTE target={target_id} by={message.from_user.id}")
    except Exception as e:
        await message.reply(f"Hata: {e}")

# ---- pin/purge ----

@router.message(Command("pin"))
async def pin_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.reply_to_message:
        return await message.reply("Pin için bir mesaja reply yap.")
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_pin_messages"):
        return await message.reply("Yetki yok: can_pin_messages")
    if not await require_bot_right(bot, message.chat.id, "can_pin_messages"):
        return await message.reply("Bot yetkisi yok: can_pin_messages")
    try:
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
        await message.reply("Pinlendi.")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unpin"))
async def unpin_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_pin_messages"):
        return await message.reply("Yetki yok: can_pin_messages")
    if not await require_bot_right(bot, message.chat.id, "can_pin_messages"):
        return await message.reply("Bot yetkisi yok: can_pin_messages")
    try:
        if message.reply_to_message:
            await bot.unpin_chat_message(message.chat.id, message.reply_to_message.message_id)
        else:
            await bot.unpin_chat_message(message.chat.id)
        await message.reply("Unpin OK.")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("unpinall"))
async def unpinall_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_pin_messages"):
        return await message.reply("Yetki yok: can_pin_messages")
    if not await require_bot_right(bot, message.chat.id, "can_pin_messages"):
        return await message.reply("Bot yetkisi yok: can_pin_messages")
    try:
        await bot.unpin_all_chat_messages(message.chat.id)
        await message.reply("Tüm pinler kaldırıldı.")
    except Exception as e:
        await message.reply(f"Hata: {e}")

@router.message(Command("purge"))
async def purge_cmd(message: Message, bot: Bot):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.reply_to_message:
        return await message.reply("Purge için bir mesaja reply yap (o mesajdan bu komuta kadar siler).")
    if not await require_user_right(bot, message.chat.id, message.from_user.id, "can_delete_messages"):
        return await message.reply("Yetki yok: can_delete_messages")
    if not await require_bot_right(bot, message.chat.id, "can_delete_messages"):
        return await message.reply("Bot yetkisi yok: can_delete_messages")

    start_id = message.reply_to_message.message_id
    end_id = message.message_id
    MAX_DELETE = 200
    deleted = 0
    for mid in range(start_id, end_id + 1):
        try:
            await bot.delete_message(message.chat.id, mid)
            deleted += 1
        except:
            pass
        if deleted >= MAX_DELETE:
            break
        if deleted % 25 == 0:
            await asyncio.sleep(0.5)

# ---- welcome + captcha ----

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
    await upsert_user(session, user.id, user.username, user.first_name, user.last_name)

    if s.captcha and await require_bot_right(bot, event.chat.id, "can_restrict_members"):
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
            session.add(PendingVerify(chat_id=event.chat.id, user_id=user.id, message_id=msg.message_id))
            await session.commit()
        except:
            pass

    if s.welcome_text:
        text = s.welcome_text.replace("{first_name}", user.first_name or "")
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

    if call.from_user.id != target_id:
        return await call.answer("Bu buton sana ait değil.", show_alert=True)

    s = await get_or_create_chat_settings(session, chat_id)
    if not s.captcha:
        return await call.answer("Captcha kapalı.", show_alert=True)

    if not await require_bot_right(bot, chat_id, "can_restrict_members"):
        return await call.answer("Botun yetkisi yok.", show_alert=True)

    try:
        await bot.restrict_chat_member(chat_id=chat_id, user_id=target_id, permissions=permissive_permissions())
    except:
        return await call.answer("Açılamadı. Bot yetkilerini kontrol et.", show_alert=True)

    q = await session.execute(select(PendingVerify).where(
        PendingVerify.chat_id == chat_id,
        PendingVerify.user_id == target_id
    ))
    row = q.scalar_one_or_none()
    if row:
        await session.delete(row)
        await session.commit()

    try:
        await call.message.delete()
    except:
        pass

    await call.answer("Doğrulandı!")

# ---- auto anti-link ----

@router.message()
async def anti_link_handler(message: Message, bot: Bot, session: AsyncSession, config: Config):
    if not message.chat or message.chat.type == "private" or not message.from_user:
        return
    if not message.text or message.text.startswith("/"):
        return

    s = await get_or_create_chat_settings(session, message.chat.id)
    if not s.anti_link:
        return
    if await is_admin(bot, message.chat.id, message.from_user.id):
        return

    if URL_RE.search(message.text):
        if await require_bot_right(bot, message.chat.id, "can_delete_messages"):
            try:
                await message.delete()
            except:
                pass

        if await require_bot_right(bot, message.chat.id, "can_restrict_members"):
            count, punished = await add_warning_and_maybe_punish(
                bot=bot, session=session, config=config,
                chat_id=message.chat.id, user_id=message.from_user.id
            )
            try:
                if punished:
                    await message.answer(f"Link yasak. Limit aştın ({count}/{config.warn_limit}) → mute.")
                else:
                    await message.answer(f"Link yasak. Uyarı: {count}/{config.warn_limit}")
            except:
                pass

        await log_action(session, bot, message.chat.id, f"ANTILINK user={message.from_user.id}")

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
    dp.update.middleware(UserTrackMiddleware())

    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
