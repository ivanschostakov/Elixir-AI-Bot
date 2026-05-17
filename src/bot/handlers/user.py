import asyncio

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from config import EXPERT_BOT_TOKEN, OWNER_TG_IDS, BOT_KEYWORDS, EXPERT_ASSISTANT_ID, DOSE_ASSISTANT_ID, PROFESSOR_ASSISTANT_ID
from src.bot.keyboards import user_keyboards
from src.bot.states import user_states
from src.bot.texts import user_texts
from src.ai.helpers import with_action, CHAT_NOT_BANNED_FILTER, check_blocked
from src.ai.webapp_client import webapp_client
from src.tg_methods import normalize_phone
from .ai_helpers import (
    MediaGroupFilter,
    media_group_handler,
    send_message_v2_from_telegram,
    safe_ai_response,
    safe_webapp_call,
    schedule_webapp_call,
)

expert_user_router = Router(name="user_expert")
dose_user_router = Router(name="user3")
UNVERIFIED_REQUEST_LIMIT = 5
PHONE_GATE_BOTS = ("professor", "dose")
FEATURE_ONLY_BOT_MSG = "Эта функция доступна только в @elixirpeptidebot"

expert_user_router.message.filter(lambda message: message.from_user.id not in OWNER_TG_IDS and message.chat.type == ChatType.PRIVATE, check_blocked)
expert_user_router.callback_query.filter(lambda call: call.data.startswith("user") and call.from_user.id not in OWNER_TG_IDS and call.message.chat.type == ChatType.PRIVATE, check_blocked)
dose_user_router.message.filter(lambda message: message.from_user.id not in OWNER_TG_IDS and message.chat.type == ChatType.PRIVATE, check_blocked)
dose_user_router.callback_query.filter(lambda call: call.data.startswith("user") and call.from_user.id not in OWNER_TG_IDS and call.message.chat.type == ChatType.PRIVATE, check_blocked)

def _resolve_assistant_id(message: Message) -> str:
    bot_id = str(message.bot.id)
    if bot_id == EXPERT_BOT_TOKEN.split(':')[0]: return EXPERT_ASSISTANT_ID
    return DOSE_ASSISTANT_ID

async def _request_phone(message: Message, state: FSMContext):
    await state.set_state(user_states.Registration.phone)
    return await message.answer(user_texts.verify_phone.replace('*', message.from_user.full_name), reply_markup=user_keyboards.phone)

def _should_request_phone(user, assistant_id: str, used_requests: int) -> bool:
    if user and user.tg_phone: return False
    if assistant_id == PROFESSOR_ASSISTANT_ID: return True
    return used_requests >= UNVERIFIED_REQUEST_LIMIT

async def _ensure_user(message: Message, expert_client):
    user_id = message.from_user.id
    user = await webapp_client.get_user("tg_id", user_id)
    if not user:
        conversation_id = await expert_client.create_conversation(user_id=user_id)
        user = await webapp_client.upsert_user({"tg_id": user_id, "name": message.from_user.first_name, "surname": message.from_user.last_name, "conversation_id": conversation_id})
        return user
    if not user.conversation_id:
        conversation_id = await expert_client.create_conversation(user_id=user_id)
        user = await webapp_client.update_user(user_id, {"conversation_id": conversation_id})
    return user


async def _get_unverified_requests_count(user_id: int) -> int: return await webapp_client.get_user_total_requests(user_id, PHONE_GATE_BOTS)

@expert_user_router.message(CommandStart())
@dose_user_router.message(CommandStart())
async def handle_user_start(message: Message, state: FSMContext, expert_bot, expert_client):
    user_id = message.from_user.id
    result = await CHAT_NOT_BANNED_FILTER(message)
    if not result: return await message.answer(user_texts.banned_in_channel)
    user = await _ensure_user(message, expert_client)
    assistant_id = _resolve_assistant_id(message)
    used_requests = 0 if user.tg_phone else await _get_unverified_requests_count(user_id)
    if _should_request_phone(user, assistant_id, used_requests): return await _request_phone(message, state)
    if user.tg_phone:
        schedule_webapp_call(
            safe_webapp_call(
                webapp_client.update_user_name(user_id, message.from_user.first_name, message.from_user.last_name),
                operation="update_user_name",
            ),
            operation="update_user_name",
        )
    response = await safe_ai_response(message, send_message_v2_from_telegram(message=message, professor_client=expert_client, user_id=user_id, conversation_id=user.conversation_id, input_text_override=f"Я написал первое сообщение или возобновил наш диалог. Начни/возобнови диалог. Мое имя в Telegram — {message.from_user.full_name}."))
    if response is None: return None
    schedule_webapp_call(
        safe_webapp_call(
            webapp_client.write_usage(
                message.from_user.id,
                response['input_tokens'],
                response['output_tokens'],
                BOT_KEYWORDS[assistant_id],
                cached_input_tokens=response.get("cached_input_tokens"),
            ),
            operation="write_usage",
        ),
        operation="write_usage",
    )
    return await expert_bot.parse_response(response, message, back_menu=True)


@expert_user_router.message(user_states.Registration.phone)
@dose_user_router.message(user_states.Registration.phone)
async def handle_user_registration(message: Message, state: FSMContext, expert_bot, expert_client):
    result = await CHAT_NOT_BANNED_FILTER(message)
    if not result: return await message.answer(user_texts.banned_in_channel)
    if not message.contact: return await message.answer(user_texts.verify_phone.replace('*', message.from_user.full_name), reply_markup=user_keyboards.phone)
    phone = message.contact.phone_number
    await state.clear()
    conversation_id = await expert_bot.create_user(message.from_user.id, normalize_phone(phone), message.from_user.first_name, message.from_user.last_name)
    assistant_id = _resolve_assistant_id(message)
    response = await safe_ai_response(message, send_message_v2_from_telegram(message=message, professor_client=expert_client, user_id=message.from_user.id, conversation_id=conversation_id, input_text_override=f"ОБРАЩАЙСЯ ТОЛЬКО НА ВЫ, Я написал первое сообщение. Мое имя в Telegram — {message.from_user.full_name}."))
    if response is None: return None
    schedule_webapp_call(
        safe_webapp_call(
            webapp_client.write_usage(
                message.from_user.id,
                response['input_tokens'],
                response['output_tokens'],
                BOT_KEYWORDS[assistant_id],
                cached_input_tokens=response.get("cached_input_tokens"),
            ),
            operation="write_usage",
        ),
        operation="write_usage",
    )
    await expert_bot.parse_response(response, message)
    return await message.delete()


@expert_user_router.message(Command('new_chat'))
@dose_user_router.message(Command('new_chat'))
async def handle_new_chat(message: Message, state: FSMContext, expert_client):
    result = await CHAT_NOT_BANNED_FILTER(message)
    if not result: return await message.answer(user_texts.banned_in_channel)
    conversation_id = await expert_client.create_conversation(user_id=message.from_user.id)
    await safe_webapp_call(
        webapp_client.update_user(message.from_user.id, {"conversation_id": conversation_id}),
        operation="update_conversation_id",
    )
    await state.update_data(conversation_id=conversation_id)
    return await message.answer(user_texts.new_chat)

@expert_user_router.message(lambda message: not message.media_group_id and (message.photo or message.video or message.video_note or message.document or message.voice))
@dose_user_router.message(lambda message: not message.media_group_id and (message.photo or message.video or message.video_note or message.document or message.voice))
@with_action()
async def handle_single_non_text_message(message: Message): return await message.answer(FEATURE_ONLY_BOT_MSG)


@expert_user_router.message(MediaGroupFilter())
@dose_user_router.message(MediaGroupFilter())
@media_group_handler(only_album=True)
async def handle_media_group(messages: list[Message]):
    message = messages[0]
    return await message.answer(FEATURE_ONLY_BOT_MSG)


@expert_user_router.message(lambda message: message.text and message.text.strip())
@dose_user_router.message(lambda message: message.text and message.text.strip())
@with_action()
async def handle_text_message(message: Message, state: FSMContext, expert_bot, expert_client):
    user_id = message.from_user.id
    result = await CHAT_NOT_BANNED_FILTER(message)
    if not result: return await message.answer(user_texts.banned_in_channel)
    user = await _ensure_user(message, expert_client)
    assistant_id = _resolve_assistant_id(message)
    used_requests = 0 if user.tg_phone else await _get_unverified_requests_count(user_id)
    if _should_request_phone(user, assistant_id, used_requests): return await _request_phone(message, state)
    if user.tg_phone:
        schedule_webapp_call(
            safe_webapp_call(
                webapp_client.update_user_name(user_id, message.from_user.first_name, message.from_user.last_name),
                operation="update_user_name",
            ),
            operation="update_user_name",
        )
    response = await safe_ai_response(message, send_message_v2_from_telegram(message=message, professor_client=expert_client, user_id=user_id, conversation_id=user.conversation_id))
    if response is None: return None
    schedule_webapp_call(
        safe_webapp_call(
            webapp_client.write_usage(
                message.from_user.id,
                response['input_tokens'],
                response['output_tokens'],
                BOT_KEYWORDS[assistant_id],
                cached_input_tokens=response.get("cached_input_tokens"),
            ),
            operation="write_usage",
        ),
        operation="write_usage",
    )
    return await expert_bot.parse_response(response, message, back_menu=True)

@expert_user_router.callback_query()
@dose_user_router.callback_query()
async def handle_call(call: CallbackQuery): await call.message.answer("Напишите сообщение или очистите историю диалога командой /new_chat")
