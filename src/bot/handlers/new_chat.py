import asyncio
from time import monotonic
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import PROFESSOR_ASSISTANT_ID, BOT_KEYWORDS, ELIXIR_CHAT_ID
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
from .new_user_helpers import _resolve_mode_client, _resolve_last_used

professor_chat_router = Router(name="professor_chat")
professor_chat_router.message.filter(lambda message: message.chat.id == ELIXIR_CHAT_ID)
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



async def _ensure_target_user(reply_message: Message, conversation_client):
    target_user_id = reply_message.from_user.id
    user = await webapp_client.get_user("tg_id", target_user_id)
    if user and user.conversation_id: return user
    conversation_id = await conversation_client.create_conversation(user_id=target_user_id)
    user = await webapp_client.upsert_user(
        {
            "tg_id": target_user_id,
            "name": reply_message.from_user.first_name,
            "surname": reply_message.from_user.last_name,
            "conversation_id": conversation_id,
        }
    )
    return user


@professor_chat_router.message(MediaGroupFilter())
@media_group_handler(only_album=True)
async def remember_media_group(messages: list[Message]):
    _cache_media_group(messages)

@professor_chat_router.message(Command('new_chat'))
async def new_chat(message: Message, professor_client, expert_client=None):
    user = await webapp_client.get_user("tg_id", message.from_user.id)
    last_used, has_unknown_last_used = _resolve_last_used(user)
    if user and has_unknown_last_used: schedule_webapp_call(safe_webapp_call(webapp_client.update_user(message.from_user.id, {"last_used": last_used}), operation="update_last_used"), operation="update_last_used")
    active_client = _resolve_mode_client(last_used, professor_client, expert_client)
    conversation_id = await active_client.create_conversation(user_id=message.from_user.id)
    await safe_webapp_call(webapp_client.upsert_user({"tg_id": message.from_user.id, "name": message.from_user.first_name, "surname": message.from_user.last_name, "conversation_id": conversation_id, "last_used": last_used}), operation="upsert_conversation_id")


    async def _(x: Message):
        new = await x.reply("Для продолжения разговора <b>перейдите в личный диалог</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть бота", url=f"https://t.me/{(await message.bot.get_me()).username}")]]))
        await asyncio.sleep(120)
        await x.delete()
        await new.delete()

    asyncio.create_task(_(message))

@professor_chat_router.message(CHAT_ADMIN_REPLY_FILTER, Command('answer_ai'))
async def answer_ai(message: Message, professor_bot, expert_client=None):
    if expert_client is None:
        professor_bot.log.error("/answer_ai called without expert_client (professor)")
        return None

    reply_message = message.reply_to_message
    if not isinstance(reply_message, Message) or not reply_message.from_user: return False

    prompt_tail = (reply_message.text or reply_message.caption or "").strip()
    prompt_override = (
        "ОТВЕЧАЙ КРАТКО, ПОСЛЕ ОТВЕТА СКАЖИ МНЕ ПЕРЕЙТИ С ТОБОЙ В ЛИЧНЫЙ ДИАЛОГ ЕСЛИ ЗАХОЧУ ОТВЕТЫ ДЛИННЕЕ "
        f"{prompt_tail}"
    ).strip()

    target_user_id = reply_message.from_user.id
    user = await _ensure_target_user(reply_message, expert_client)
    if reply_message.media_group_id:
        album_messages = _get_cached_media_group(reply_message) or [reply_message]
        response = await safe_ai_response(message, send_message_v2_from_media_group(messages=album_messages, professor_client=expert_client, user_id=target_user_id, conversation_id=user.conversation_id, input_text_override=prompt_override))

    else: response = await safe_ai_response(message, send_message_v2_from_telegram(message=reply_message, professor_client=expert_client, user_id=target_user_id, conversation_id=user.conversation_id, input_text_override=prompt_override))
    if response is None: return None

    schedule_webapp_call(
        safe_webapp_call(webapp_client.write_usage(
            target_user_id,
            response["input_tokens"],
            response["output_tokens"],
            BOT_KEYWORDS[PROFESSOR_ASSISTANT_ID],
            cached_input_tokens=response.get("cached_input_tokens"),
        ), operation="write_usage",
    ), operation="write_usage")
    await message.delete()
    return await professor_bot.parse_response(response, reply_message, back_menu=False)


@professor_chat_router.message(lambda message: not (message.text and message.text.strip().startswith("/")) and ((message.text and message.text.strip()) or (message.caption and message.caption.strip()) or message.photo or message.video or message.video_note or message.document or message.voice))
async def handle_mentioned_message(message: Message, professor_bot, expert_client=None):
    bot_username = f"@{(await message.bot.get_me()).username}"

    if not (message.text.strip().startswith(bot_username) and message.text.removeprefix(bot_username).strip()): return None
    if not message.from_user: return None
    if expert_client is None:
        professor_bot.log.error("Mention received without expert_client")
        return None

    target_user_id = message.from_user.id
    user = await _ensure_target_user(message, expert_client)

    prompt_override = (
        "ОТВЕЧАЙ КРАТКО, не упомянай об этом в диалоге, ПОСЛЕ ОТВЕТА СКАЖИ МНЕ ПЕРЕЙТИ С ТОБОЙ В ЛИЧНЫЙ ДИАЛОГ ЕСЛИ ЗАХОЧУ ОТВЕТЫ ДЛИННЕЕ "
        f"{(message.text or message.caption or '').strip()}"
    ).strip()

    if message.media_group_id:
        album_messages = _get_cached_media_group(message) or [message]
        response = await safe_ai_response(message, send_message_v2_from_media_group(messages=album_messages, professor_client=expert_client, user_id=target_user_id, conversation_id=user.conversation_id, input_text_override=prompt_override))

    else: response = await safe_ai_response(message, send_message_v2_from_telegram(message=message, professor_client=expert_client, user_id=target_user_id, conversation_id=user.conversation_id, input_text_override=prompt_override))
    if response is None: return None

    schedule_webapp_call(
        safe_webapp_call(webapp_client.write_usage(
            target_user_id,
            response["input_tokens"],
            response["output_tokens"],
            BOT_KEYWORDS[PROFESSOR_ASSISTANT_ID],
            cached_input_tokens=response.get("cached_input_tokens"),
        ), operation="write_usage"), operation="write_usage",
    )
    return await professor_bot.parse_response(response, message, back_menu=False)
