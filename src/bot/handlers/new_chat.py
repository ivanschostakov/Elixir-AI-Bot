from time import monotonic

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ELIXIR_CHAT_ID, NEW_ASSISTANT_ID, BOT_KEYWORDS
from src.ai.helpers import CHAT_ADMIN_REPLY_FILTER
from src.ai.webapp_client import webapp_client
from .ai_helpers import (
    MediaGroupFilter,
    media_group_handler,
    send_message_v2_from_media_group,
    send_message_v2_from_telegram,
    safe_ai_response,
    safe_webapp_call,
    schedule_webapp_call,
)

new_chat_router = Router(name="new_chat")
_MEDIA_GROUP_CACHE_TTL_SECONDS = 900
_media_group_cache: dict[tuple[int, str], tuple[float, list[Message]]] = {}


def _cleanup_cached_media_groups() -> None:
    now = monotonic()
    expired_keys = [key for key, (ts, _) in _media_group_cache.items() if now - ts > _MEDIA_GROUP_CACHE_TTL_SECONDS]
    for key in expired_keys: _media_group_cache.pop(key, None)


def _cache_media_group(messages: list[Message]) -> None:
    if not messages: return
    media_group_id = messages[0].media_group_id
    if not media_group_id: return
    key = (messages[0].chat.id, media_group_id)
    _media_group_cache[key] = (monotonic(), sorted(messages, key=lambda msg: msg.message_id))
    _cleanup_cached_media_groups()


def _get_cached_media_group(message: Message) -> list[Message] | None:
    media_group_id = message.media_group_id
    if not media_group_id: return None
    key = (message.chat.id, media_group_id)
    cached_value = _media_group_cache.get(key)
    if not cached_value: return None
    ts, messages = cached_value
    if monotonic() - ts > _MEDIA_GROUP_CACHE_TTL_SECONDS:
        _media_group_cache.pop(key, None)
        return None
    return messages


async def _ensure_target_user(reply_message: Message, professor_client):
    target_user_id = reply_message.from_user.id
    user = await webapp_client.get_user("tg_id", target_user_id)
    if user and user.conversation_id: return user
    conversation_id = await professor_client.create_conversation(user_id=target_user_id)
    user = await webapp_client.upsert_user(
        {
            "tg_id": target_user_id,
            "name": reply_message.from_user.first_name,
            "surname": reply_message.from_user.last_name,
            "conversation_id": conversation_id,
        }
    )
    return user


@new_chat_router.message(lambda message: message.chat.id == ELIXIR_CHAT_ID, MediaGroupFilter())
@media_group_handler(only_album=True)
async def remember_media_group(messages: list[Message]):
    _cache_media_group(messages)


@new_chat_router.message(CHAT_ADMIN_REPLY_FILTER, Command('answer_ai'))
async def answer_ai(message: Message, professor_bot, professor_client):
    reply_message = message.reply_to_message
    if not isinstance(reply_message, Message) or not reply_message.from_user: return False

    prompt_tail = (reply_message.text or reply_message.caption or "").strip()
    prompt_override = (
        "ОТВЕЧАЙ КРАТКО ОТВЕЧАЙ КРАТКО ОТВЕЧАЙ КРАТКО ОТВЕЧАЙ КРАТКО. "
        "ПОСЛЕ ОТВЕТА СКАЖИ МНЕ ПЕРЕЙТИ С ТОБОЙ В ЛИЧНЫЙ ДИАЛОГ ЕСЛИ ЗАХОЧУ ОТВЕТЫ ДЛИННЕЕ "
        f"{prompt_tail}"
    ).strip()

    target_user_id = reply_message.from_user.id
    user = await _ensure_target_user(reply_message, professor_client)
    if reply_message.media_group_id:
        album_messages = _get_cached_media_group(reply_message) or [reply_message]
        response = await safe_ai_response(message, send_message_v2_from_media_group(messages=album_messages, professor_client=professor_client, user_id=target_user_id, conversation_id=user.conversation_id, input_text_override=prompt_override))
    else: response = await safe_ai_response(message, send_message_v2_from_telegram(message=reply_message, professor_client=professor_client, user_id=target_user_id, conversation_id=user.conversation_id, input_text_override=prompt_override))
    if response is None: return None

    schedule_webapp_call(
        safe_webapp_call(
            webapp_client.write_usage(
                target_user_id,
                response["input_tokens"],
                response["output_tokens"],
                BOT_KEYWORDS[NEW_ASSISTANT_ID],
                cached_input_tokens=response.get("cached_input_tokens"),
            ),
            operation="write_usage",
        ),
        operation="write_usage",
    )
    await message.delete()
    return await professor_bot.parse_response(response, reply_message, back_menu=False)
