"""Keep mentor entry and unprocessed input across the existing phone gate."""
import asyncio
from weakref import WeakValueDictionary

from aiogram.types import Message

from src.ai import telegram_mentor
from src.bot.states import user_states
from . import new_user_helpers as access


MENTOR_PHONE_RESUME = "mentor_phone_resume"
_registration_locks = WeakValueDictionary()


def phone_registration_lock(state):
    key = state.key
    lock = _registration_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _registration_locks[key] = lock
    return lock


def unwrap_message(message):
    from .mentor_panel import MentorPanel, ReplyCards
    while isinstance(message, (MentorPanel, ReplyCards)):
        message = message.message
    return message


async def _remember_resume(message, uid, state, kind, **extra):
    saved = await state.get_data()
    pending = saved.get(MENTOR_PHONE_RESUME)
    if pending and pending.get("user_id") != uid:
        pending = None
    if pending and pending.get("user_id") == uid:
        if pending.get("kind") != "entry" or kind != "onboarding":
            return
    previous_state = pending.get("state") if pending else await state.get_state()
    if previous_state == user_states.Registration.phone.state:
        previous_state = None
    await state.update_data(**{MENTOR_PHONE_RESUME: {
        "kind": kind,
        "user_id": uid,
        "chat_id": message.chat.id,
        "state": previous_state,
        "opening_question": pending.get("opening_question") if pending else telegram_mentor.opening_question(uid),
        **extra,
    }})


async def _needs_phone(user, uid):
    if getattr(user, "tg_phone", None):
        return False
    mode, _ = access._resolve_last_used(user)
    if mode != access.LAST_USED_PROFESSOR:
        used = await access._get_unverified_requests_count(uid)
        if used < access.UNVERIFIED_REQUEST_LIMIT:
            return False
    return True


async def check_mentor_phone(message, uid, state, full_name=None):
    """Return True after requesting a phone; call before clearing entry state."""
    user = await access.webapp_client.get_user("tg_id", uid)
    if not await _needs_phone(user, uid):
        return False
    message = unwrap_message(message)
    await _remember_resume(message, uid, state, "entry")
    display_name = full_name
    if display_name is None:
        display_name = " ".join(filter(None, (getattr(user, "name", None), getattr(user, "surname", None)))) if message.from_user.is_bot else message.from_user.full_name
    await access._request_phone(message, state, display_name)
    return True


async def check_onboarding_access(message, uid, state, *, actor=None, full_name=None, input_message=None):
    """Stop on blocking or the normal phone policy; normalization needs no credit.

    Pass the CallbackQuery as actor for menu cards and the unmodified incoming
    Message as input_message before transcription. Neither argument is inferred
    from a bot-authored card.
    """
    message = unwrap_message(message)
    event = actor if actor is not None else message
    sender = getattr(event, "from_user", None)
    if sender is None or sender.id != uid or getattr(sender, "is_bot", False):
        return True
    original = unwrap_message(input_message) if input_message is not None else None
    if original is not None and (original.from_user is None or original.from_user.id != uid
            or original.from_user.is_bot or original.chat.id != message.chat.id):
        return True
    from . import new_user
    if not await new_user.check_blocked(event) or not await new_user.CHAT_NOT_BANNED_FILTER(event):
        return True
    user = await access.webapp_client.get_user("tg_id", uid)
    if not await _needs_phone(user, uid):
        return False
    if original is not None:
        await _remember_resume(original, uid, state, "onboarding",
            messages=[original.model_dump(mode="json", exclude_none=True)],
            button_action=telegram_mentor.button_action.get())
    else:
        await _remember_resume(message, uid, state, "entry")
    await access._request_phone(message, state, full_name if full_name is not None else getattr(sender, "full_name", ""))
    return True


async def remember_mentor_phone_input(messages, state):
    messages = [unwrap_message(message) for message in messages]
    message = messages[0]
    uid = message.from_user.id
    if not telegram_mentor.mentor_enabled(uid):
        return
    await _remember_resume(message, uid, state,
        "media_group" if message.media_group_id else "single",
        messages=[item.model_dump(mode="json", exclude_none=True) for item in messages],
        button_action=telegram_mentor.button_action.get())


async def _clear_resume(state):
    saved = await state.get_data()
    saved.pop(MENTOR_PHONE_RESUME, None)
    await state.set_data(saved)


async def consume_mentor_phone_input(messages, state):
    """Drop only the input the AI accepted, before billing or delivery can fail."""
    pending = (await state.get_data()).get(MENTOR_PHONE_RESUME)
    if not pending or pending.get("kind") not in {"single", "media_group"}:
        return
    original_ids = [item["message_id"] for item in pending["messages"]]
    if (pending["user_id"] == messages[0].from_user.id
            and pending["chat_id"] == messages[0].chat.id
            and original_ids == [item.message_id for item in messages]):
        await _clear_resume(state)


async def resume_mentor_phone(message, state, professor_bot, professor_client, expert_client=None):
    """Return True when mentor owns registration, including a retryable failure."""
    pending = (await state.get_data()).get(MENTOR_PHONE_RESUME)
    if not pending:
        return False
    uid = message.from_user.id
    if (pending.get("user_id") != uid or pending.get("chat_id") != message.chat.id
            or pending.get("kind") not in {"entry", "single", "media_group", "onboarding"}):
        await _clear_resume(state)
        return False

    from . import mentor, new_user
    if not await new_user.check_blocked(message) or not await new_user.CHAT_NOT_BANNED_FILTER(message):
        return True
    await state.set_state(pending.get("state"))
    token = telegram_mentor.button_action.set(pending.get("button_action"))
    try:
        if pending["kind"] == "entry":
            # Use the verified user's message, never the bot-authored menu card.
            try:
                await mentor.enter(message, uid, state)
            except BaseException:
                # Entry clears transient FSM data before it sends its reply.
                await state.update_data(**{MENTOR_PHONE_RESUME: pending})
                raise
            if await state.get_state() != user_states.Registration.phone.state:
                await _clear_resume(state)
        else:
            messages = [Message.model_validate(item).as_(message.bot) for item in pending["messages"]]
            if any(item.from_user.id != uid or item.chat.id != message.chat.id for item in messages):
                await _clear_resume(state)
                return True
            telegram_mentor.set_mentor_enabled(uid, True)
            question = pending.get("opening_question")
            if question is not None:
                telegram_mentor.save_opening_question(uid, question)
            else:
                telegram_mentor.clear_opening_question(uid, telegram_mentor.opening_question(uid))
            if pending["kind"] == "onboarding":
                from . import mentor_onboarding
                accepted = await mentor_onboarding.receive(messages[0], state,
                    professor_client=professor_client, professor_bot=professor_bot, expert_client=expert_client)
                if (accepted is True and await state.get_state() != user_states.Registration.phone.state
                        and (await state.get_data()).get(MENTOR_PHONE_RESUME) == pending):
                    await _clear_resume(state)
            elif pending["kind"] == "media_group":
                await new_user._handle_media_group(messages, state, professor_bot, professor_client, expert_client)
            else:
                # Confirmation parsing already ran before the gate. Replay only the AI request.
                await new_user.handle_single_ai_message(messages[0], state, professor_bot,
                    professor_client, expert_client, mentor_followup=True)
    finally:
        telegram_mentor.button_action.reset(token)
        if (await state.get_data()).get(MENTOR_PHONE_RESUME):
            await state.set_state(user_states.Registration.phone)
    return True
