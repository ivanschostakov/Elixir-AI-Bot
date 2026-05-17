import logging

from aiogram import Router
from aiogram.types import Message

from config import ELIXIR_CHAT_ID
from src.ai.helpers import with_action
from src.ai.webapp_client import webapp_client
from src.bot.handlers.new_user_helpers import _ensure_user, _resolve_last_used, LAST_USED_EXPERT
from .ai_helpers import (
    MediaGroupFilter,
    media_group_handler,
    send_message_v2_from_media_group,
    send_message_v2_from_telegram,
    safe_ai_response,
    safe_webapp_call,
    schedule_webapp_call,
)

professor_guest_router = Router(name="shop_professor_guest")
guest_logger = logging.getLogger("aiogram.shop_guest")


def _is_guest_payload(message: Message) -> bool:
    return bool(
        (message.text and message.text.strip())
        or (message.caption and message.caption.strip())
        or message.photo
        or message.video
        or message.video_note
        or message.document
        or message.voice
    )


async def _ensure_guest_user(message: Message, expert_client):
    user_id = message.from_user.id
    existing_user = await webapp_client.get_user("tg_id", user_id)
    last_used, has_unknown_last_used = _resolve_last_used(existing_user)
    if has_unknown_last_used:
        schedule_webapp_call(
            safe_webapp_call(
                webapp_client.update_user(user_id, {"last_used": last_used}),
                operation="update_last_used",
            ),
            operation="update_last_used",
        )

    user = await _ensure_user(message, expert_client)
    return user, last_used


async def _write_guest_usage(user_id: int, response: dict):
    schedule_webapp_call(
        safe_webapp_call(
            webapp_client.write_usage(
                user_id,
                response["input_tokens"],
                response["output_tokens"],
                LAST_USED_EXPERT,
                cached_input_tokens=response.get("cached_input_tokens"),
            ),
            operation="write_usage",
        ),
        operation="write_usage",
    )


@professor_guest_router.guest_message(lambda message: message.chat.id == ELIXIR_CHAT_ID, MediaGroupFilter())
@media_group_handler(only_album=True)
async def handle_guest_media_group(messages: list[Message], professor_bot, expert_client=None):
    if not messages:
        return None

    message = messages[0]
    user_id = message.from_user.id
    if expert_client is None:
        guest_logger.error("Guest media group received without expert_client in middleware")
        return await message.answer("Временная ошибка сервиса. Попробуйте ещё раз через 1-2 минуты.")

    user, _ = await _ensure_guest_user(message, expert_client)
    response = await safe_ai_response(
        message,
        send_message_v2_from_media_group(
            messages=messages,
            professor_client=expert_client,
            user_id=user_id,
            conversation_id=user.conversation_id,
        ),
    )
    if response is None:
        return None

    await _write_guest_usage(user_id, response)
    return await professor_bot.parse_guest_query(response, message)


@professor_guest_router.guest_message(
    lambda message: message.chat.id == ELIXIR_CHAT_ID and not message.media_group_id and _is_guest_payload(message)
)
@with_action()
async def handle_guest_single_message(message: Message, professor_bot, expert_client=None):
    user_id = message.from_user.id
    if expert_client is None:
        guest_logger.error("Guest single message received without expert_client in middleware")
        return await message.answer("Временная ошибка сервиса. Попробуйте ещё раз через 1-2 минуты.")

    user, _ = await _ensure_guest_user(message, expert_client)
    response = await safe_ai_response(
        message,
        send_message_v2_from_telegram(
            message=message,
            professor_client=expert_client,
            user_id=user_id,
            conversation_id=user.conversation_id,
        ),
    )
    if response is None:
        return None

    await _write_guest_usage(user_id, response)
    return await professor_bot.parse_guest_query(response, message)
