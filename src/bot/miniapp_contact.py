"""Forward verified self-contacts from the existing aiogram polling receiver."""

import asyncio
import logging
import os
import time

import aiohttp

logger = logging.getLogger("elixir.miniapp_contact")
WEBHOOK_URL = "https://elixirlink.online/api/webhooks/telegram-miniapp"
BOT_ID = 8566381393


def contact_payload(update):
    message = update.message
    if not message or not message.contact or not message.from_user:
        return None
    user = message.from_user
    if (user.is_bot or user.id <= 0 or message.chat.type != "private"
            or message.chat.id != user.id or message.contact.user_id != user.id
            or message.forward_origin or getattr(message, "forward_date", None)):
        return None
    age = time.time() - message.date.timestamp()
    phone = "".join(c for c in message.contact.phone_number if c in "0123456789")
    if not -300 <= age <= 3600 or not 10 <= len(phone) <= 15:
        return None
    # Send only fields needed for verification; omit names, vCards and other data.
    return {"update_id": update.update_id, "message": {
        "message_id": message.message_id, "date": int(message.date.timestamp()),
        "from": {"id": user.id, "is_bot": False},
        "chat": {"id": user.id, "type": "private"},
        "contact": {"user_id": user.id, "phone_number": "+" + phone},
    }}


class MiniAppContactMiddleware:
    def __init__(self, secret=None):
        self.secret = secret if secret is not None else os.environ.get("TELEGRAM_MINIAPP_WEBHOOK_SECRET", "")
        if len(self.secret) < 32:
            raise ValueError("TELEGRAM_MINIAPP_WEBHOOK_SECRET must be configured")

    async def forward(self, payload):
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(3):
                try:
                    async with session.post(
                        WEBHOOK_URL, json=payload, allow_redirects=False,
                        headers={"X-Telegram-Bot-Api-Secret-Token": self.secret},
                    ) as response:
                        if response.status == 200 and (await response.json()).get("ok") is True:
                            logger.info("Mini-app contact delivered")
                            return True
                        if 400 <= response.status < 500 and response.status != 429:
                            logger.error("Mini-app contact rejected: HTTP %d", response.status)
                            return False
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    # Never log exception bodies, headers, phone numbers or tokens.
                    pass
                if attempt < 2:
                    await asyncio.sleep(attempt + 1)
        logger.error("Mini-app contact delivery failed after 3 attempts")
        return False

    async def __call__(self, handler, event, data):
        if getattr(data.get("bot"), "id", None) == BOT_ID:
            payload = contact_payload(event)
            if payload:
                try:
                    await self.forward(payload)
                except Exception:
                    logger.error("Mini-app contact forwarding failed")
        # Preserve the bot's normal handlers and registration flow, even on failure.
        return await handler(event, data)
