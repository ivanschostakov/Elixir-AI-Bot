import asyncio

import pytest

from src.bot.handlers import mentor
from src.bot.handlers.mentor_panel import (
    MentorPanel, clear_input, complete_card, navigate_page, navigation_mode,
    remember_card, remember_message,
)
from test_mentor_navigation import actions, message, query
from test_telegram_flows import State


@pytest.mark.parametrize("kind", ["user", "answer"])
@pytest.mark.parametrize("photo", [False, True])
def test_old_menu_after_conversation_opens_one_new_card_below_it(kind, photo):
    async def run():
        old = message(mid=4, text="Old menu", markup=mentor.menu())
        if photo:
            old.photo, old.caption = [object()], old.text
        later = message(mid=20, text="Keep this conversation message")
        fresh = message(mid=21)
        old.answer.side_effect = None
        old.answer.return_value = fresh
        state = State()
        await remember_card(state, old, kind="navigation")
        panel = MentorPanel(old, state, saved_card=state.values["mentor_panel"], action="menu")
        await remember_message(state, later, kind=kind)
        await panel.answer("Fresh menu", reply_markup=mentor.menu())
        await panel.flush()
        await panel.flush()
        old.answer.assert_awaited_once()
        old.edit_text.assert_not_awaited()
        old.edit_caption.assert_not_awaited()
        later.edit_text.assert_not_awaited()
        later.edit_reply_markup.assert_not_awaited()
        fresh.edit_text.assert_not_awaited()
        assert state.values["mentor_panel"]["message_id"] == 21
        assert state.values["mentor_latest_message"]["message_id"] == 21

        repeated = MentorPanel(old, state, action="menu")
        await repeated.answer("Fresh menu", reply_markup=mentor.menu())
        await repeated.flush()
        old.answer.assert_awaited_once()
        fresh.edit_text.assert_not_awaited()

        current = MentorPanel(fresh, state, action="food")
        await current.answer("Food")
        await current.flush()
        fresh.edit_text.assert_awaited_once()
        fresh.answer.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["navigation", "form"])
def test_only_latest_service_card_is_editable(kind):
    async def run():
        old, current = message(mid=4), message(mid=20)
        state = State()
        await remember_card(state, old, kind=kind)
        await remember_card(state, current, kind=kind)
        before = dict(state.values)
        assert navigation_mode(old, state.values, action="food") == "ignore"
        panel = MentorPanel(old, state, target=current, action="food")
        await panel.answer("Must not be delivered")
        await panel.flush()
        old.edit_text.assert_not_awaited()
        old.answer.assert_not_awaited()
        current.edit_text.assert_not_awaited()
        current.answer.assert_not_awaited()
        assert state.values == before
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["user", "answer"])
def test_conversation_watermark_overrides_menu_shaped_keyboard(kind):
    async def run():
        msg = message(mid=20, text="Conversation", markup=mentor.menu())
        state = State()
        await remember_message(state, msg, kind=kind)
        panel = MentorPanel(msg, state, action="menu")
        await panel.answer("Menu")
        await panel.flush()
        msg.edit_text.assert_not_awaited()
        msg.answer.assert_awaited_once()
    asyncio.run(run())


def test_watermark_is_monotonic_and_does_not_promote_conversation_to_panel():
    async def run():
        state = State()
        await remember_card(state, message(mid=4), kind="navigation")
        await remember_message(state, message(mid=30), kind="answer")
        await remember_message(state, message(mid=25), kind="user")
        await remember_card(state, message(mid=4), kind="receipt")
        assert state.values["mentor_latest_message"] == {"message_id": 30, "chat_id": 123, "kind": "answer"}
        assert state.values["mentor_panel"]["message_id"] == 4
        await clear_input(state)
        assert state.values["mentor_latest_message"]["message_id"] == 30
    asyncio.run(run())


def test_other_chat_watermark_does_not_make_current_card_stale():
    async def run():
        msg, state = message(mid=4), State()
        await remember_card(state, msg, kind="navigation")
        other = message(mid=90)
        other.chat.id = 999
        await remember_message(state, other)
        panel = MentorPanel(msg, state)
        await panel.answer("Menu")
        await panel.flush()
        msg.edit_text.assert_awaited_once()
        msg.answer.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["user", "answer"])
def test_page_callback_after_later_message_does_not_edit_history(kind):
    async def run():
        msg, state = message(mid=4), State()
        await remember_card(state, msg, kind="navigation", token="page-token",
            pages=[{"text": "First", "rows": []}, {"text": "Second", "rows": []}])
        await remember_message(state, message(mid=20), kind=kind)
        callback = query("page:page-token:1", msg)
        await navigate_page(callback, state)
        callback.answer.assert_awaited_once()
        msg.edit_text.assert_not_awaited()
        msg.answer.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("status", ["Saved.", "Cancelled."])
def test_explicit_completion_changes_only_its_draft_and_preserves_active_panel(replace, status):
    async def run():
        draft, current = message(mid=4, text="Draft details"), message(mid=20)
        state = State()
        await remember_card(state, draft, kind="form")
        await remember_card(state, current, kind="navigation")
        active = state.values["mentor_panel"]
        panel = MentorPanel(draft, state, saved_card=active, target=current)
        await complete_card(panel, status, mentor.back_keyboard(), replace=replace)
        await panel.flush()
        draft.edit_text.assert_awaited_once()
        assert draft.edit_text.await_args.args[0] == (status if replace else "Draft details\n\n" + status)
        assert "mentor:receipt" not in actions(draft.edit_text.await_args.kwargs["reply_markup"])
        draft.answer.assert_not_awaited()
        current.edit_text.assert_not_awaited()
        assert state.values["mentor_panel"] == active
        assert state.values["mentor_latest_message"]["message_id"] == 20
        assert next(c for c in state.values["mentor_cards"] if c["message_id"] == 4)["kind"] == "receipt"
    asyncio.run(run())


def test_older_paginated_draft_keeps_all_pages_without_promoting_it():
    async def run():
        draft, current, state = message(mid=4), message(mid=20), State()
        await remember_card(state, draft, kind="form", token="old", index=0,
            pages=[{"text": "First preview", "rows": []}, {"text": "Second preview", "rows": []}])
        await remember_card(state, current, kind="navigation")
        await MentorPanel(draft, state).complete("Saved.", mentor.back_keyboard())
        receipt = next(c for c in state.values["mentor_cards"] if c["message_id"] == 4)
        assert [p["text"] for p in receipt["pages"]] == ["First preview", "Second preview\n\nSaved."]
        assert state.values["mentor_panel"]["message_id"] == 20
        assert "Saved." in draft.edit_text.await_args.args[0]
        await navigate_page(query("page:" + receipt["token"] + ":0", draft), state)
        assert "First preview" in draft.edit_text.await_args.args[0]
        assert state.values["mentor_panel"]["message_id"] == 20
        assert state.values["mentor_latest_message"]["message_id"] == 20
        draft.answer.assert_not_awaited()
        current.edit_text.assert_not_awaited()
    asyncio.run(run())


def test_repeated_confirmation_does_not_duplicate_the_status():
    async def run():
        draft, state = message(mid=4, text="Preview"), State()
        await remember_card(state, draft, kind="form")
        await MentorPanel(draft, state).complete("Saved.", mentor.back_keyboard())
        await MentorPanel(draft, state).complete("Saved.", mentor.back_keyboard())
        draft.edit_text.assert_awaited_once()
        draft.answer.assert_not_awaited()
        assert state.values["mentor_panel"]["pages"][0]["text"] == "Preview\n\nSaved."
    asyncio.run(run())


def test_freshness_is_rechecked_after_panel_construction():
    async def run():
        old, current, state = message(mid=4), message(mid=20), State()
        await remember_card(state, old, kind="navigation")
        panel = MentorPanel(old, state, saved_card=state.values["mentor_panel"])
        await panel.answer("Outdated result")
        await remember_card(state, current, kind="navigation")
        await panel.flush()
        old.edit_text.assert_not_awaited()
        old.answer.assert_not_awaited()
        assert state.values["mentor_panel"]["message_id"] == 20
    asyncio.run(run())


def test_concurrent_stale_clicks_send_only_one_fresh_menu():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    async def run():
        old, current = message(mid=4), message(mid=21)
        storage = MemoryStorage()
        key = StorageKey(bot_id=1, chat_id=123, user_id=123)
        first_state, second_state = FSMContext(storage, key), FSMContext(storage, key)
        await remember_card(first_state, old, kind="navigation")
        await remember_message(first_state, message(mid=20), kind="answer")
        sending, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            sending.set()
            await release.wait()
            return current

        old.answer.side_effect = send
        first, second = MentorPanel(old, first_state), MentorPanel(old, second_state)
        await first.answer("Fresh menu")
        await second.answer("Fresh menu")
        task = asyncio.create_task(first.flush())
        await sending.wait()
        duplicate = asyncio.create_task(second.flush())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task, duplicate)
        old.answer.assert_awaited_once()
        old.edit_text.assert_not_awaited()
        current.edit_text.assert_not_awaited()
        assert (await first_state.get_data())["mentor_panel"]["message_id"] == 21
        await storage.close()
    asyncio.run(run())


def test_media_after_new_conversation_does_not_edit_old_viewer():
    async def run():
        msg, state = message(mid=4), State(mentor_media={"message_id": 10, "chat_id": 123})
        msg.answer_photo.return_value = message(mid=21)
        await remember_message(state, message(mid=20), kind="answer")
        await MentorPanel(msg, state).answer_photo("new-photo", caption="Progress", protect_content=True)
        msg.bot.edit_message_media.assert_not_awaited()
        msg.answer_photo.assert_awaited_once()
        assert state.values["mentor_media"]["message_id"] == 21
        assert state.values["mentor_latest_message"]["message_id"] == 21
    asyncio.run(run())
