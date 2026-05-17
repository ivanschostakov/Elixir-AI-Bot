import logging
import time

from typing import Callable, Awaitable, Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class ContextMiddleware(BaseMiddleware):
    def __init__(
        self,
        bot_instance,
        bot_client,
        *,
        role: str = "expert",
        expert_bot=None,
        expert_client=None,
    ):
        super().__init__()
        self.bot_instance = bot_instance
        self.bot_client = bot_client
        self.role = role
        self.expert_bot = expert_bot
        self.expert_client = expert_client
        self.logger = logging.getLogger("aiogram.update_lifecycle")

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]):
        if self.role == "professor":
            data["professor_bot"] = self.bot_instance
            data["professor_client"] = self.bot_client
        else:
            data["expert_bot"] = self.bot_instance
            data["expert_client"] = self.bot_client

        if self.expert_bot is not None:
            data["expert_bot"] = self.expert_bot
        if self.expert_client is not None:
            data["expert_client"] = self.expert_client

        update = data.get("event_update")
        update_id = getattr(update, "update_id", None)
        event_name = event.__class__.__name__
        user_id = getattr(getattr(event, "from_user", None), "id", None)
        chat_id = getattr(getattr(event, "chat", None), "id", None)
        started = time.monotonic()

        self.logger.info("Update start | update_id=%s | event=%s | user_id=%s | chat_id=%s", update_id, event_name, user_id, chat_id)
        try: result = await handler(event, data)
        except Exception:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self.logger.exception("Update failed | update_id=%s | event=%s | user_id=%s | chat_id=%s | elapsed_ms=%d", update_id, event_name, user_id, chat_id, elapsed_ms)
            raise

        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.logger.info("Update done | update_id=%s | event=%s | user_id=%s | chat_id=%s | elapsed_ms=%d", update_id, event_name, user_id, chat_id, elapsed_ms)
        return result
