import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from src.bot.handlers.mentor_panel import MentorPanel, navigate_page, edit_panel, reply_panel
from src.bot.handlers.mentor import button
from src.bot.handlers.mentor_flows import keyboard


class State:
    def __init__(self): self.values = {"mentor_panel": {"message_id": 4, "chat_id": 123}}
    async def update_data(self, **kwargs): self.values.update(kwargs)
    async def get_data(self): return self.values


def message():
    return SimpleNamespace(message_id=4, chat=SimpleNamespace(id=123), photo=None, edit_text=AsyncMock(), edit_caption=AsyncMock(), answer=AsyncMock())


def test_callback_renders_one_edit_and_no_send_even_for_multi_block_workout():
    async def run():
        msg, state = message(), State()
        panel = MentorPanel(msg, state)
        await panel.answer("Тренировка на сегодня")
        await panel.answer("Какой рабочий вес?", reply_markup=keyboard([("Закончить", "workout_finish:1")]))
        await panel.flush()
        msg.answer.assert_not_awaited()
        msg.edit_text.assert_awaited_once()
        assert "Какой рабочий вес?" in msg.edit_text.await_args.args[0]
        assert "Тренировка на сегодня" in msg.edit_text.await_args.args[0]
    asyncio.run(run())


def test_long_text_is_paginated_without_truncating_or_sending():
    async def run():
        msg, state = message(), State()
        panel = MentorPanel(msg, state)
        original = "запись " * 1200
        await panel.answer(original)
        await panel.flush()
        saved = state.values["mentor_panel"]
        assert "".join(p["text"] for p in saved["pages"]) == original
        query = SimpleNamespace(data=f"mentor:page:{saved['token']}:1", message=msg, answer=AsyncMock())
        await navigate_page(query, state)
        assert msg.edit_text.await_count == 2
        assert "2 /" in msg.edit_text.await_args.args[0]
        msg.answer.assert_not_awaited()
    asyncio.run(run())


def test_stale_page_cannot_replace_current_screen():
    async def run():
        msg, state = message(), State()
        query = SimpleNamespace(data="mentor:page:old:0", message=msg, answer=AsyncMock())
        await navigate_page(query, state)
        msg.edit_text.assert_not_awaited()
        query.answer.assert_awaited_once()
    asyncio.run(run())


def test_unchanged_message_is_ignored_but_other_telegram_errors_propagate():
    async def run():
        msg = message()
        msg.edit_text.side_effect = TelegramBadRequest(method=EditMessageText(text="x"), message="message is not modified")
        await edit_panel(msg, "x", keyboard())
        msg.answer.assert_not_awaited()
    asyncio.run(run())


def test_photo_caption_navigation_stays_under_telegram_limit():
    async def run():
        msg, state = message(), State()
        msg.photo = [object()]
        panel = MentorPanel(msg, state)
        await panel.answer("x"*3000)
        await panel.flush()
        assert len(msg.edit_caption.await_args.kwargs["caption"]) < 1024
        msg.edit_text.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["open", "menu", "today", "food", "workouts", "course", "settings", "meal", "nutrition", "target", "food_lookup", "privacy"])
def test_real_menu_handler_edits_instead_of_sending(monkeypatch, tmp_path, action):
    from src.bot.handlers import mentor, mentor_flows
    from src.ai import telegram_mentor
    from test_telegram_flows import State as FormState, dashboard
    monkeypatch.setattr(telegram_mentor.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mentor, "configured", lambda: True)
    telegram_mentor.set_mentor_enabled(123, True)
    monkeypatch.setattr(mentor, "api", AsyncMock(return_value=dashboard()))
    monkeypatch.setattr(mentor_flows, "api", AsyncMock(return_value=dashboard()))
    msg = message()
    query = SimpleNamespace(id="q1", data="mentor:"+action, from_user=SimpleNamespace(id=123), message=msg, answer=AsyncMock())
    asyncio.run(mentor.mentor_action(query, FormState(mentor_panel={"message_id":4, "chat_id":123})))
    msg.answer.assert_not_awaited()
    msg.edit_text.assert_awaited_once()


def test_typed_workout_steps_send_below_user_and_keep_request_identity(monkeypatch, tmp_path):
    from src.bot.handlers import mentor_flows as f
    from src.ai import telegram_mentor
    from test_telegram_flows import State as FormState
    monkeypatch.setattr(telegram_mentor.config, "DATA_DIR", tmp_path)
    telegram_mentor.set_mentor_enabled(123, True)
    async def parsed(message, state, kind, client): return message.text
    monkeypatch.setattr(f, "parse_step", parsed)
    async def run():
        bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
        msg = SimpleNamespace(message_id=900, from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123),
            text="20", bot=bot, answer=AsyncMock())
        state = FormState(form_kind="set_weight", planned_exercise="Press", workout_id=10,
            mentor_panel={"message_id":4, "chat_id":123, "photo":False})
        await f.receive(msg, state)
        assert state.values["form_kind"] == "set_reps"
        bot.edit_message_text.assert_not_awaited()
        assert "повторений" in msg.answer.await_args.args[0]
        msg.message_id, msg.text = 901, "10"
        await f.receive(msg, state)
        assert state.values["pending_set"]["weight_kg"] == 20
        assert state.values["set_request_key"] == "tg:123:901"
        bot.edit_message_text.assert_not_awaited()
        assert msg.answer.await_count == 2
        bot.send_message.assert_not_awaited()
    asyncio.run(run())


def test_other_chat_cannot_reuse_card():
    msg = message()
    state = State()
    state.values = {"mentor_panel": {"message_id":4, "chat_id":999}}
    async def run():
        reply = await reply_panel(msg, state)
        await reply.answer("Следующий вопрос")
        msg.answer.assert_awaited_once()
        msg.edit_text.assert_not_awaited()
        msg.edit_caption.assert_not_awaited()
    asyncio.run(run())


def test_non_editable_message_error_is_not_silently_resent():
    msg = message()
    msg.edit_text.side_effect = TelegramBadRequest(method=EditMessageText(text="x"), message="message to edit not found")
    with pytest.raises(TelegramBadRequest):
        asyncio.run(edit_panel(msg, "x", keyboard()))
    msg.answer.assert_not_awaited()
