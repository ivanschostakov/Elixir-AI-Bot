import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from config import UFA_TZ
from src.ai import telegram_mentor as t
from src.bot.handlers import mentor, mentor_access as access, mentor_flows, mentor_onboarding, new_user
from src.bot.handlers import new_user_helpers as helpers
from src.bot.handlers.mentor_panel import MentorPanel
from src.bot.states import user_states


UID = 123
RESUME = access.MENTOR_PHONE_RESUME


def message(mid=10, **values):
    return Message.model_validate({
        "message_id": mid, "date": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "chat": {"id": UID, "type": "private"},
        "from_user": {"id": UID, "is_bot": False, "first_name": "Test", "last_name": "User"},
        **values,
    })


def contact(owner=UID, mid=20):
    return message(mid, contact={"user_id": owner, "phone_number": "+14155552671", "first_name": "Test"})


def state_context():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=999, chat_id=UID, user_id=UID))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "send", AsyncMock(side_effect=AssertionError("Unexpected network call")))
    monkeypatch.setattr(t.config, "DATA_DIR", tmp_path)
    t.set_mentor_enabled(UID, True)
    user = SimpleNamespace(tg_phone=None, last_used=helpers.LAST_USED_EXPERT,
        conversation_id="conv_original", premium_requests=2, premium_until=None)
    get_user = AsyncMock(return_value=user)
    count = AsyncMock(return_value=5)
    monkeypatch.setattr(helpers.webapp_client, "get_user", get_user)
    monkeypatch.setattr(helpers.webapp_client, "get_user_total_requests", count)
    write_usage, update_user = AsyncMock(), AsyncMock()
    monkeypatch.setattr(helpers.webapp_client, "write_usage", write_usage)
    monkeypatch.setattr(helpers.webapp_client, "update_user", update_user)
    monkeypatch.setattr(helpers.webapp_client, "update_user_name", AsyncMock())
    monkeypatch.setattr(new_user, "check_blocked", AsyncMock(return_value=True))
    monkeypatch.setattr(new_user, "CHAT_NOT_BANNED_FILTER", AsyncMock(return_value=True))
    monkeypatch.setattr(new_user, "_notify_user", AsyncMock())
    monkeypatch.setattr(new_user, "wrap_client", lambda client, uid, mode: client)
    pending_text = AsyncMock(return_value=False)
    monkeypatch.setattr(mentor_flows, "pending_text", pending_text)
    start = AsyncMock()
    monkeypatch.setattr(new_user, "handle_user_start", start)
    response = {"mentor": True, "input_tokens": 11, "output_tokens": 7, "text": "Draft only"}
    single = AsyncMock(return_value=response)
    album = AsyncMock(return_value=response)
    monkeypatch.setattr(new_user, "send_message_v2_from_telegram", single)
    monkeypatch.setattr(new_user, "send_message_v2_from_media_group", album)
    answer = AsyncMock(return_value=message(99))
    monkeypatch.setattr(Message, "answer", answer)
    bot = SimpleNamespace(id=999, send_chat_action=AsyncMock())
    professor_client, expert_client = object(), object()

    async def create_user(uid, phone, first, last):
        user.tg_phone = phone

    professor_bot = SimpleNamespace(create_user=AsyncMock(side_effect=create_user), parse_response=AsyncMock(return_value=None))
    tasks = []

    def schedule(coro, **kwargs):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(new_user, "schedule_webapp_call", schedule)

    async def drain():
        if tasks:
            await asyncio.gather(*tasks)

    async def send(msg, state):
        await new_user.handle_single_ai_message(msg.as_(bot), state, professor_bot, professor_client, expert_client)
        await drain()

    async def register(state, msg=None):
        await new_user.handle_user_registration((msg or contact()).as_(bot), state,
            professor_bot, professor_client, expert_client)
        await drain()

    return SimpleNamespace(**locals())


@pytest.mark.parametrize("mode,used,phone,expected", [
    (helpers.LAST_USED_EXPERT, 0, None, False),
    (helpers.LAST_USED_EXPERT, 4, None, False),
    (helpers.LAST_USED_EXPERT, 5, None, True),
    (helpers.LAST_USED_EXPERT, 6, None, True),
    ("unknown", 4, None, False),
    (helpers.LAST_USED_PROFESSOR, 0, None, True),
    (helpers.LAST_USED_PROFESSOR, 99, "+14155552671", False),
])
def test_entry_uses_existing_phone_policy(env, mode, used, phone, expected):
    env.user.last_used, env.user.tg_phone = mode, phone
    env.count.return_value = used

    async def run():
        state = state_context()
        await state.update_data(mentor_onboarding={"step": "goal"})
        assert await access.check_mentor_phone(message(text="/mentor"), UID, state) is expected
        saved = await state.get_data()
        assert saved["mentor_onboarding"] == {"step": "goal"}
        assert bool(saved.get(RESUME)) is expected
        assert await state.get_state() == (user_states.Registration.phone.state if expected else None)

    asyncio.run(run())
    if phone or mode == helpers.LAST_USED_PROFESSOR:
        env.count.assert_not_awaited()
    else:
        env.count.assert_awaited_once_with(UID, helpers.PHONE_GATE_BOTS)


def test_new_user_keeps_five_free_requests(env):
    env.get_user.return_value = None
    env.count.return_value = 0
    assert not asyncio.run(access.check_mentor_phone(message(), UID, state_context()))
    env.answer.assert_not_awaited()


def test_entry_from_bot_panel_resumes_with_verified_user_and_preserves_state(env, monkeypatch):
    async def run():
        state = state_context()
        onboarding = {"step": "goal", "answers": {"goal": "maintain"}}
        await state.update_data(mentor_onboarding=onboarding, mentor_panel={"message_id": 7})
        bot_message = message(from_user={"id": 999, "is_bot": True, "first_name": "Bot"})
        panel = MentorPanel(bot_message, state)
        assert await access.check_mentor_phone(panel, UID, state, full_name="Test User")
        assert not panel.blocks
        assert "Test User" in env.answer.await_args.args[0]
        env.get_user.assert_awaited_with("tg_id", UID)

        async def enter(msg, uid, resumed_state):
            assert msg.from_user.id == uid == UID
            assert msg.contact.user_id == UID
            assert (await resumed_state.get_data())["mentor_onboarding"] == onboarding
            assert not await access.check_mentor_phone(msg, uid, resumed_state)
            t.set_mentor_enabled(uid, True)

        enter_mock = AsyncMock(side_effect=enter)
        monkeypatch.setattr(mentor, "enter", enter_mock)
        await env.register(state)
        enter_mock.assert_awaited_once()
        assert RESUME not in await state.get_data()
        assert (await state.get_data())["mentor_onboarding"] == onboarding
        assert (await state.get_data())["mentor_panel"] == {"message_id": 7}
        assert await state.get_state() is None
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_text_resumes_exact_original_context_once_and_bills_normal_pipeline(env):
    original = message(text="Rice 200 g and chicken", entities=[{"type": "bold", "offset": 0, "length": 4}])

    async def run():
        state = state_context()
        await state.set_state("MentorOnboarding:answer")
        await state.update_data(mentor_onboarding={"step": "goal"}, pending_reviews=[{"id": 88}])
        t.save_opening_question(UID, "What did you eat?\nContext: meal")
        token = t.button_action.set("meal")
        try:
            await env.send(original, state)
        finally:
            t.button_action.reset(token)
        saved = await state.get_data()
        assert json.loads(json.dumps(saved))[RESUME]["messages"][0]["text"] == original.text
        assert saved[RESUME]["messages"][0]["entities"] == [{"type": "bold", "offset": 0, "length": 4}]
        env.single.assert_not_awaited()
        env.write_usage.assert_not_awaited()
        t.save_opening_question(UID, "Changed question")

        async def respond(**kwargs):
            replayed = kwargs["message"]
            assert replayed.model_dump() == original.model_dump()
            assert replayed.bot is env.bot
            assert kwargs["user_id"] == UID
            assert kwargs["professor_client"] is env.expert_client
            assert t.opening_question(UID) == "What did you eat?\nContext: meal"
            assert t.button_action.get() == "meal"
            return env.response

        env.single.side_effect = respond
        await env.register(state)
        env.single.assert_awaited_once()
        env.pending_text.assert_awaited_once()
        assert RESUME not in await state.get_data()
        assert (await state.get_data())["mentor_onboarding"] == {"step": "goal"}
        assert await state.get_state() == "MentorOnboarding:answer"
        assert t.button_action.get() is None
        assert t.mentor_enabled(UID)
        env.write_usage.assert_awaited_once_with(UID, 11, 7, helpers.LAST_USED_EXPERT, cached_input_tokens=None)
        env.professor_bot.parse_response.assert_awaited_once()
        assert env.professor_bot.parse_response.await_args.kwargs == {"back_menu": True, "mentor_state": state}
        await env.register(state)
        env.single.assert_awaited_once()
        env.professor_bot.create_user.assert_awaited_once_with(UID, "+14155552671", "Test", "User")
        env.start.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("owner", [None, 456])
def test_contact_must_belong_to_sender(env, owner):
    async def run():
        state = state_context()
        await env.send(message(text="Lunch"), state)
        saved = await state.get_data()
        await env.register(state, contact(owner))
        assert await state.get_data() == saved
        assert await state.get_state() == user_states.Registration.phone.state
        env.professor_bot.create_user.assert_not_awaited()
        env.single.assert_not_awaited()
        env.start.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("kind,payload", [
    ("photo", [{"file_id": "photo-file", "file_unique_id": "photo-unique", "width": 10, "height": 10}]),
    ("voice", {"file_id": "voice-file", "file_unique_id": "voice-unique", "duration": 5}),
    ("video", {"file_id": "video-file", "file_unique_id": "video-unique", "width": 10, "height": 10, "duration": 5}),
    ("video_note", {"file_id": "note-file", "file_unique_id": "note-unique", "length": 10, "duration": 5}),
    ("document", {"file_id": "doc-file", "file_unique_id": "doc-unique", "file_name": "meal.pdf"}),
])
def test_single_media_preserves_caption_entities_and_file_ids(env, kind, payload):
    env.user.last_used = helpers.LAST_USED_PROFESSOR
    original = message(**{kind: payload}, caption="My meal", caption_entities=[{"type": "bold", "offset": 0, "length": 2}])

    async def run():
        state = state_context()
        await env.send(original, state)
        await env.register(state)
        replayed = env.single.await_args.kwargs["message"]
        assert replayed.model_dump() == original.model_dump()
        assert RESUME not in await state.get_data()
        assert env.single.await_args.kwargs["professor_client"] is env.professor_client
        env.update_user.assert_awaited_once_with(UID, {"premium_requests": 1})
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_album_retries_all_original_messages_through_shared_pipeline(env):
    env.user.last_used = helpers.LAST_USED_PROFESSOR
    originals = [message(mid, media_group_id="album-original", caption="Meal" if mid == 10 else None,
        photo=[{"file_id": "photo-" + str(mid), "file_unique_id": str(mid), "width": 10, "height": 10}]).as_(env.bot)
        for mid in (10, 11)]

    async def run():
        state = state_context()
        await new_user._handle_media_group(originals, state, env.professor_bot, env.professor_client, env.expert_client)
        env.album.assert_not_awaited()
        assert (await state.get_data())[RESUME]["kind"] == "media_group"
        await env.register(state)
        replayed = env.album.await_args.kwargs["messages"]
        assert [msg.model_dump() for msg in replayed] == [msg.model_dump() for msg in originals]
        assert all(msg.bot is env.bot for msg in replayed)
        assert RESUME not in await state.get_data()
        env.album.assert_awaited_once()
        env.write_usage.assert_awaited_once_with(UID, 11, 7, helpers.LAST_USED_PROFESSOR, cached_input_tokens=None)
        env.update_user.assert_awaited_once_with(UID, {"premium_requests": 1})
        assert env.professor_bot.parse_response.await_args.kwargs["mentor_state"] is state
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_premium_quota_is_not_bypassed_and_pending_request_is_retained(env):
    env.user.last_used = helpers.LAST_USED_PROFESSOR
    env.user.premium_requests = 0

    async def run():
        state = state_context()
        await env.send(message(text="Meal"), state)
        await env.register(state)
        env.single.assert_not_awaited()
        env.write_usage.assert_not_awaited()
        assert (await state.get_data())[RESUME]["messages"][0]["text"] == "Meal"
        assert await state.get_state() == user_states.Registration.phone.state
        assert env.answer.await_args.args[0] == new_user.user_texts.premium_limit_0
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_active_subscription_does_not_spend_request_credit(env):
    env.user.last_used = helpers.LAST_USED_PROFESSOR
    env.user.premium_requests = 0
    env.user.premium_until = datetime.now(UFA_TZ) + timedelta(days=1)

    async def run():
        state = state_context()
        await env.send(message(text="Meal"), state)
        await env.register(state)
        env.single.assert_awaited_once()
        env.write_usage.assert_awaited_once()
        env.update_user.assert_not_awaited()

    asyncio.run(run())


def test_replay_error_keeps_input_and_onboarding_for_retry(env):
    async def run():
        state = state_context()
        await state.update_data(mentor_onboarding={"step": "goal"})
        await env.send(message(text="My meal"), state)
        env.single.return_value = None
        await env.register(state)
        assert (await state.get_data())[RESUME]["messages"][0]["text"] == "My meal"
        assert (await state.get_data())["mentor_onboarding"] == {"step": "goal"}
        assert await state.get_state() == user_states.Registration.phone.state
        env.write_usage.assert_not_awaited()
        env.start.assert_not_awaited()
        env.single.return_value = env.response
        await env.register(state, contact(mid=21))
        assert RESUME not in await state.get_data()
        env.write_usage.assert_awaited_once()

    asyncio.run(run())


def test_parallel_contact_delivery_consumes_request_once(env):
    async def run():
        state = state_context()
        await env.send(message(text="My meal"), state)
        await asyncio.gather(env.register(state), env.register(state, contact(mid=21)))
        env.single.assert_awaited_once()
        env.professor_bot.create_user.assert_awaited_once()
        env.write_usage.assert_awaited_once()
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_delivery_failure_does_not_replay_accepted_input(env):
    async def run():
        state = state_context()
        await env.send(message(text="My meal"), state)
        env.professor_bot.parse_response.side_effect = RuntimeError("Telegram unavailable")
        with pytest.raises(RuntimeError, match="Telegram unavailable"):
            await env.register(state)
        await env.drain()
        assert RESUME not in await state.get_data()
        await env.register(state)
        env.single.assert_awaited_once()
        env.write_usage.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("filter_name", ["check_blocked", "CHAT_NOT_BANNED_FILTER"])
def test_registration_resume_rechecks_blocking_filters(env, monkeypatch, filter_name):
    monkeypatch.setattr(new_user, filter_name, AsyncMock(return_value=False))

    async def run():
        state = state_context()
        await env.send(message(text="My meal"), state)
        await env.register(state)
        assert RESUME in await state.get_data()
        env.single.assert_not_awaited()
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_non_mentor_registration_keeps_legacy_start_path(env):
    t.set_mentor_enabled(UID, False)

    async def run():
        state = state_context()
        await env.send(message(text="Plain AI"), state)
        assert RESUME not in await state.get_data()
        await env.register(state)
        env.start.assert_awaited_once()
        env.single.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("album", [False, True])
def test_non_mentor_response_does_not_receive_mentor_state(env, album):
    env.user.tg_phone = "+14155552671"
    env.user.last_used = helpers.LAST_USED_PROFESSOR
    env.response.pop("mentor")

    async def run():
        state = state_context()
        if album:
            await new_user._handle_media_group([message(media_group_id="album").as_(env.bot)], state,
                env.professor_bot, env.professor_client, env.expert_client)
            await env.drain()
        else:
            await env.send(message(text="Normal AI"), state)
        assert env.professor_bot.parse_response.await_args.kwargs == {"back_menu": True}

    asyncio.run(run())


def test_repeated_entry_check_does_not_replace_pending_original_input(env):
    async def run():
        state = state_context()
        await env.send(message(text="Keep this meal"), state)
        pending = (await state.get_data())[RESUME]
        assert await access.check_mentor_phone(message(11, text="/mentor"), UID, state)
        assert (await state.get_data())[RESUME] == pending
        await env.register(state)
        assert env.single.await_args.kwargs["message"].text == "Keep this meal"

    asyncio.run(run())


def test_entry_failure_after_state_reset_keeps_resume_marker(env, monkeypatch):
    async def run():
        state = state_context()
        await access.check_mentor_phone(message(text="/mentor"), UID, state)
        pending = (await state.get_data())[RESUME]

        async def failed_entry(msg, uid, state):
            await state.clear()
            raise RuntimeError("Entry delivery failed")

        monkeypatch.setattr(mentor, "enter", AsyncMock(side_effect=failed_entry))
        with pytest.raises(RuntimeError, match="Entry delivery failed"):
            await env.register(state)
        assert (await state.get_data())[RESUME] == pending
        assert await state.get_state() == user_states.Registration.phone.state
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_registration_backend_failure_keeps_original_pending_state(env):
    async def run():
        state = state_context()
        await state.update_data(mentor_onboarding={"step": "goal"})
        await env.send(message(text="My meal"), state)
        saved = await state.get_data()
        env.professor_bot.create_user.side_effect = RuntimeError("Registration unavailable")
        with pytest.raises(RuntimeError, match="Registration unavailable"):
            await env.register(state)
        assert await state.get_data() == saved
        assert await state.get_state() == user_states.Registration.phone.state
        env.single.assert_not_awaited()
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_replay_cannot_confirm_a_new_pending_mutation(env):
    async def run():
        state = state_context()
        await env.send(message(text="yes"), state)
        await state.update_data(pending_input={"token": "unrelated-draft"})
        env.pending_text.side_effect = AssertionError("Replay must not execute confirmation parsing")
        await env.register(state)
        assert env.single.await_args.kwargs["message"].text == "yes"
        assert (await state.get_data())["pending_input"] == {"token": "unrelated-draft"}
        env.pending_text.assert_awaited_once()

    asyncio.run(run())


def test_free_media_remains_unsupported_without_forcing_phone_gate(env):
    async def run():
        state = state_context()
        await env.send(message(photo=[{"file_id": "file", "file_unique_id": "unique", "width": 10, "height": 10}]), state)
        assert RESUME not in await state.get_data()
        assert await state.get_state() is None
        assert env.answer.await_args.args[0] == new_user.user_texts.expert_text_only
        env.single.assert_not_awaited()
        env.count.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("mode,used,phone,gated", [
    (helpers.LAST_USED_EXPERT, 0, None, False),
    (helpers.LAST_USED_EXPERT, 4, None, False),
    (helpers.LAST_USED_EXPERT, 5, None, True),
    (helpers.LAST_USED_PROFESSOR, 0, None, True),
    (helpers.LAST_USED_PROFESSOR, 50, "+14155552671", False),
])
def test_onboarding_guard_uses_phone_policy_without_requiring_answer_credit(env, monkeypatch, mode, used, phone, gated):
    env.user.last_used, env.user.tg_phone, env.user.premium_requests = mode, phone, 0
    env.count.return_value = used
    monkeypatch.setattr(new_user, "_can_use_professor_mode", lambda user: pytest.fail("Normalization does not require answer credit"))
    progress = {"status": "active", "step": "age", "token": "age-original"}
    mentor_onboarding.save(UID, progress)

    async def run():
        state, original = state_context(), message(text="forty")
        assert await access.check_onboarding_access(original, UID, state, input_message=original) is gated
        assert mentor_onboarding.load(UID) == progress
        assert bool((await state.get_data()).get(RESUME)) is gated
        if gated:
            pending = (await state.get_data())[RESUME]
            assert pending["kind"] == "onboarding"
            assert pending["messages"][0]["text"] == "forty"
        env.single.assert_not_awaited()
        env.write_usage.assert_not_awaited()
        env.update_user.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("filter_name", ["check_blocked", "CHAT_NOT_BANNED_FILTER"])
def test_onboarding_guard_checks_callback_actor_not_bot_card(env, monkeypatch, filter_name):
    blocker = AsyncMock(return_value=False)
    monkeypatch.setattr(new_user, filter_name, blocker)

    async def run():
        state = state_context()
        card = message(from_user={"id": 999, "is_bot": True, "first_name": "Bot"}).as_(env.bot)
        query = CallbackQuery(id="callback", from_user=message().from_user, chat_instance="chat",
            message=card, data="mentor:setup:old:begin").as_(env.bot)
        assert await access.check_onboarding_access(MentorPanel(card, state), UID, state, actor=query)
        blocker.assert_awaited_once_with(query)
        env.get_user.assert_not_awaited()
        assert await state.get_data() == {}
        assert await state.get_state() is None

    asyncio.run(run())


def test_onboarding_begin_requests_phone_without_clearing_existing_gate(env):
    async def run():
        state = state_context()
        original = message(text="My meal")
        await env.send(original, state)
        pending = (await state.get_data())[RESUME]
        card = message(from_user={"id": 999, "is_bot": True, "first_name": "Bot"}).as_(env.bot)
        query = CallbackQuery(id="callback", from_user=original.from_user, chat_instance="chat",
            message=card, data="mentor:setup:old:begin").as_(env.bot)
        assert await access.check_onboarding_access(MentorPanel(card, state), UID, state, actor=query)
        assert (await state.get_data())[RESUME] == pending
        assert await state.get_state() == user_states.Registration.phone.state
        assert "Test User" in env.answer.await_args.args[0]

    asyncio.run(run())


@pytest.mark.parametrize("mismatch", ["actor", "input_user", "input_chat", "bot_card"])
def test_onboarding_guard_rejects_wrong_identity(env, mismatch):
    async def run():
        state = state_context()
        msg, original, actor = message(), message(text="forty"), None
        if mismatch == "actor":
            actor = message(from_user={"id": 456, "is_bot": False, "first_name": "Other"})
        elif mismatch == "input_user":
            original = message(from_user={"id": 456, "is_bot": False, "first_name": "Other"})
        elif mismatch == "input_chat":
            original = message(chat={"id": 456, "type": "private"})
        else:
            msg = message(from_user={"id": 999, "is_bot": True, "first_name": "Bot"})
        assert await access.check_onboarding_access(msg, UID, state, actor=actor, input_message=original)
        env.get_user.assert_not_awaited()
        assert await state.get_data() == {}

    asyncio.run(run())


@pytest.mark.parametrize("media", [False, True])
def test_onboarding_input_resumes_exact_event_and_clears_only_after_acceptance(env, monkeypatch, media):
    original = message(text="forty") if not media else message(voice={
        "file_id": "original-voice", "file_unique_id": "voice-unique", "duration": 4})
    progress = {"status": "active", "step": "age", "token": "original-token"}
    mentor_onboarding.save(UID, progress)

    async def run():
        state = state_context()
        await state.update_data(keep="persistent")
        t.save_opening_question(UID, "How old are you?")
        assert await access.check_onboarding_access(original, UID, state, input_message=original)
        assert json.loads(json.dumps(await state.get_data()))[RESUME]["kind"] == "onboarding"

        async def receive(replayed, resumed_state, **kwargs):
            assert replayed.model_dump() == original.model_dump()
            assert replayed.bot is env.bot
            assert kwargs == {"professor_client": env.professor_client, "professor_bot": env.professor_bot,
                "expert_client": env.expert_client}
            assert mentor_onboarding.load(UID) == progress
            assert t.opening_question(UID) == "How old are you?"
            assert not await access.check_onboarding_access(replayed, UID, resumed_state, input_message=replayed)
            assert RESUME in await resumed_state.get_data()
            return True

        resumed = AsyncMock(side_effect=receive)
        monkeypatch.setattr(mentor_onboarding, "receive", resumed)
        await asyncio.gather(env.register(state), env.register(state, contact(mid=21)))
        resumed.assert_awaited_once()
        assert RESUME not in await state.get_data()
        assert (await state.get_data())["keep"] == "persistent"
        assert await state.get_state() is None
        env.start.assert_not_awaited()
        env.single.assert_not_awaited()
        env.write_usage.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("result", [False, None])
def test_unaccepted_onboarding_replay_is_retained(env, monkeypatch, result):
    async def run():
        state, original = state_context(), message(text="forty")
        await access.check_onboarding_access(original, UID, state, input_message=original)
        pending = (await state.get_data())[RESUME]
        receive = AsyncMock(return_value=result)
        monkeypatch.setattr(mentor_onboarding, "receive", receive)
        await env.register(state)
        assert (await state.get_data())[RESUME] == pending
        assert await state.get_state() == user_states.Registration.phone.state
        receive.return_value = True
        await env.register(state, contact(mid=21))
        assert RESUME not in await state.get_data()
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_onboarding_replay_does_not_loop_or_clear_when_phone_is_still_missing(env, monkeypatch):
    env.professor_bot.create_user.side_effect = None

    async def run():
        state, original = state_context(), message(text="forty")
        await access.check_onboarding_access(original, UID, state, input_message=original)
        pending = (await state.get_data())[RESUME]

        async def gated_receive(msg, state, **kwargs):
            assert await access.check_onboarding_access(msg, UID, state, input_message=msg)
            return True

        receive = AsyncMock(side_effect=gated_receive)
        monkeypatch.setattr(mentor_onboarding, "receive", receive)
        await env.register(state)
        receive.assert_awaited_once()
        assert (await state.get_data())[RESUME] == pending
        assert await state.get_state() == user_states.Registration.phone.state
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_onboarding_input_upgrades_entry_marker_without_losing_original_state(env):
    async def run():
        state = state_context()
        await state.set_state("Onboarding:answer")
        await access.check_mentor_phone(message(text="/mentor"), UID, state)
        original = message(text="forty")
        await access.check_onboarding_access(original, UID, state, input_message=original)
        pending = (await state.get_data())[RESUME]
        assert pending["kind"] == "onboarding"
        assert pending["state"] == "Onboarding:answer"
        assert pending["messages"][0]["text"] == "forty"
        await access.check_onboarding_access(message(11, text="second answer"), UID, state,
            input_message=message(11, text="second answer"))
        assert (await state.get_data())[RESUME] == pending

    asyncio.run(run())


def test_onboarding_replay_exception_preserves_input(env, monkeypatch):
    async def run():
        state, original = state_context(), message(text="forty")
        await access.check_onboarding_access(original, UID, state, input_message=original)
        pending = (await state.get_data())[RESUME]
        monkeypatch.setattr(mentor_onboarding, "receive", AsyncMock(side_effect=RuntimeError("Unavailable")))
        with pytest.raises(RuntimeError, match="Unavailable"):
            await env.register(state)
        assert (await state.get_data())[RESUME] == pending
        assert await state.get_state() == user_states.Registration.phone.state
        env.start.assert_not_awaited()

    asyncio.run(run())


def test_onboarding_capture_does_not_restore_registration_as_resume_state(env, monkeypatch):
    async def run():
        state, original = state_context(), message(text="forty")
        await state.set_state(user_states.Registration.phone)
        await access.check_onboarding_access(original, UID, state, input_message=original)
        assert (await state.get_data())[RESUME]["state"] is None
        monkeypatch.setattr(mentor_onboarding, "receive", AsyncMock(return_value=True))
        await env.register(state)
        assert await state.get_state() is None
        assert RESUME not in await state.get_data()
        env.start.assert_not_awaited()

    asyncio.run(run())
