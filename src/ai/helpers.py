import asyncio
import random
import pandas as pd

from typing import Any, Awaitable, Callable, Literal, TypeVar
from datetime import datetime
from functools import wraps
from logging import Logger
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, Message

from config import ELIXIR_CHAT_ID, UFA_TZ
from src.bot.texts import user_texts
from src.ai.webapp_client import webapp_client

MAX_TG_MSG_LEN = 4096
ChatAction = Literal[
    "typing",
    "upload_photo",
    "record_video",
    "upload_video",
    "record_voice",
    "upload_voice",
    "upload_document",
    "choose_sticker",
    "find_location",
    "record_video_note",
    "upload_video_note",
]

R = TypeVar("R")


def _extract_message(*args: Any, **kwargs: Any) -> Message:
    for value in args:
        if isinstance(value, Message): return value
        if isinstance(value, list) and value and all(isinstance(item, Message) for item in value): return value[0]

    for value in kwargs.values():
        if isinstance(value, Message): return value
        if isinstance(value, list) and value and all(isinstance(item, Message) for item in value): return value[0]

    raise RuntimeError("with_action requires handler args to include Message or list[Message]")


async def run_with_action(target: Message | list[Message], awaitable: Awaitable[R], action: ChatAction = "typing") -> R:
    """
    Runs an awaitable while periodically sending a Telegram chat action.
    Usage: `result = await run_with_action(message, some_coro(), action="upload_video")`
    """
    message = _extract_message(target)

    async def loop():
        try:
            while True:
                try: await message.bot.send_chat_action(message.chat.id, action)
                except Exception: pass
                await asyncio.sleep(random.uniform(3, 5))
        except asyncio.CancelledError: return

    task = asyncio.create_task(loop())
    try: return await awaitable
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass


def with_action(func: Callable[..., Awaitable[R]] | None = None, action: ChatAction = "typing"):
    """
    Decorator that periodically sends Telegram chat actions while the handler runs.
    Supports both `@with_action` and `@with_action(action='upload_video')`.
    """
    def decorator(handler: Callable[..., Awaitable[R]]) -> Callable[..., Awaitable[R]]:
        @wraps(handler)
        async def wrapper(*args, **kwargs):
            message = _extract_message(*args, **kwargs)
            async def loop():
                try:
                    while True:
                        try: await message.bot.send_chat_action(message.chat.id, action)
                        except Exception: pass
                        await asyncio.sleep(random.uniform(3, 5))
                except asyncio.CancelledError: return

            task = asyncio.create_task(loop())
            try: return await handler(*args, **kwargs)
            finally:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass

        return wrapper

    if func is None: return decorator
    return decorator(func)


async def split_text(text: str, limit: int = MAX_TG_MSG_LEN) -> list[str]:
    chunks = []
    while len(text) > limit:
        split_idx = text.rfind("\n", 0, limit)
        if split_idx == -1: split_idx = text.rfind(". ", 0, limit)
        if split_idx == -1: split_idx = limit
        chunks.append(text[:split_idx].strip())
        text = text[split_idx:].strip()
    if text: chunks.append(text)
    return chunks


def _fmt(x):
    try:
        if x is None: return "-"
        x = float(x)
        if abs(x) >= 10000: return f"{x:,.0f}".replace(",", " ")
        if abs(x) >= 1000: return f"{x:,.1f}".replace(",", " ")
        if abs(x) >= 100: return f"{x:.1f}"
        if abs(x) >= 10: return f"{x:.2f}"
        if abs(x) >= 1: return f"{x:.2f}"
        return f"{x:.4f}"
    except Exception: return str(x)


async def _notify_user(message: Message, text: str, timer: float | None = None, logger: Logger | None = None) -> None:
    if logger: logger.info("Notify user %s | text_preview=%r | timer=%s", message.from_user.id, text[:100], timer)
    x = await message.answer(text, parse_mode="HTML")
    if timer:
        await asyncio.sleep(timer)
        await x.delete()
        if logger: logger.debug("Deleted notification message for user %s", message.from_user.id)


async def CHAT_NOT_BANNED_FILTER(obj: Message | CallbackQuery) -> bool:
    try:
        user_id = obj.from_user.id
        bot = obj.bot
        member = await bot.get_chat_member(ELIXIR_CHAT_ID, user_id)
        if member.status in [ChatMemberStatus.KICKED]:
            await bot.send_message(user_id, user_texts.banned_in_channel)
            return False
        return True
    except Exception: return True


async def CHAT_ADMIN_REPLY_FILTER(message: Message, bot: Bot) -> bool:
    if getattr(message.chat, "id") not in [ELIXIR_CHAT_ID]: return False
    if not message.reply_to_message: return False
    if message.sender_chat and message.sender_chat.id == message.chat.id: return True
    if message.from_user:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in ("administrator", "creator")
    return False


def _as_dt(value: object) -> datetime | None:
    if isinstance(value, datetime): return value
    if isinstance(value, str):
        try: return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError: return None
    return None


async def check_blocked(obj: Message | CallbackQuery):
    tg_id = int(obj.from_user.id)
    try: user = await webapp_client.get_user("tg_id", tg_id)
    except Exception: return True
    blocked_until = _as_dt(getattr(user, "blocked_until", None))
    if not blocked_until: return True
    if blocked_until.tzinfo is None: blocked_until = blocked_until.replace(tzinfo=UFA_TZ)
    else: blocked_until = blocked_until.astimezone(UFA_TZ)
    if blocked_until > datetime.now(UFA_TZ):
        target_message = obj if isinstance(obj, Message) else obj.message
        if target_message:
            await target_message.answer(user_texts.banned_until.replace("name", obj.from_user.full_name).replace("date", f"{blocked_until.date()}").replace("Блокировка до 9999-12-31, п", "П"))
        return False
    return True


def make_excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    tz_cols = df.select_dtypes(include=["datetimetz"]).columns
    for c in tz_cols: df[c] = df[c].dt.tz_localize(None)
    for c in df.columns:
        if df[c].dtype == "object": df[c] = df[c].apply(lambda x: x.replace(tzinfo=None) if hasattr(x, "tzinfo") and x.tzinfo else x)
    return df
