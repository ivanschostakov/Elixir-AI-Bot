import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageMedia

from src.ai import telegram_mentor as t
from src.bot.handlers import mentor, mentor_flows as f
from src.bot.handlers.mentor_panel import MentorPanel, clear_input, reply_panel
from src.bot.keyboards import user_keyboards
from test_telegram_flows import State, dashboard


@pytest.fixture(autouse=True)
def private_store(tmp_path, monkeypatch):
    monkeypatch.setattr(t.config, "DATA_DIR", tmp_path)
    t.set_mentor_enabled(123, True)
    monkeypatch.setattr(mentor, "configured", lambda: True)


def bot():
    return SimpleNamespace(edit_message_text=AsyncMock(), edit_message_media=AsyncMock(),
        edit_message_caption=AsyncMock(), edit_message_reply_markup=AsyncMock())


def message(mid=10, text="", markup=None, client=None):
    msg = SimpleNamespace(message_id=mid, text=text, caption=None, photo=None, reply_markup=markup,
        from_user=SimpleNamespace(id=123, full_name="Test"), chat=SimpleNamespace(id=123), bot=client or bot(),
        edit_text=AsyncMock(), edit_caption=AsyncMock(), edit_reply_markup=AsyncMock(), answer_photo=AsyncMock())

    async def send(text, **kwargs):
        child = message(mid+1, text, kwargs.get("reply_markup"), msg.bot)
        msg.sent = child
        return child

    msg.answer = AsyncMock(side_effect=send)
    return msg


def query(action, msg):
    return SimpleNamespace(id="query-123", data="mentor:"+action, message=msg,
        from_user=msg.from_user, answer=AsyncMock())


def apis(monkeypatch, entries=None):
    async def call(path, payload):
        if path == "/dashboard": return dashboard()
        return (entries or {}).get(path, {})
    mock = AsyncMock(side_effect=call)
    monkeypatch.setattr(mentor, "api", mock)
    monkeypatch.setattr(f, "api", mock)
    return mock


def actions(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_main_menu_enters_mentor_without_sending_and_has_no_self_back(monkeypatch):
    apis(monkeypatch)
    msg, state = message(markup=user_keyboards.main_menu), State()
    asyncio.run(mentor.mentor_action(query("open", msg), state))
    msg.answer.assert_not_awaited()
    msg.edit_text.assert_awaited_once()
    markup = msg.edit_text.await_args.kwargs["reply_markup"]
    assert "mentor:leave" in actions(markup)
    assert "mentor:menu" not in actions(markup)


def test_leave_does_not_add_mentor_back_to_the_main_menu(monkeypatch):
    apis(monkeypatch)
    msg, state = message(), State(mentor_panel={"message_id":10, "chat_id":123})
    asyncio.run(mentor.mentor_action(query("leave", msg), state))
    msg.answer.assert_not_awaited()
    markup = msg.edit_text.await_args.kwargs["reply_markup"]
    assert "mentor:open" in actions(markup)
    assert "mentor:menu" not in actions(markup)


def test_existing_mentor_root_remains_editable_after_fsm_restart(monkeypatch):
    apis(monkeypatch)
    msg = message(markup=mentor.menu())
    asyncio.run(mentor.mentor_action(query("food", msg), State()))
    msg.answer.assert_not_awaited()
    msg.edit_text.assert_awaited_once()


def test_mentor_command_registers_its_new_navigation_card(monkeypatch):
    apis(monkeypatch)
    msg, state = message(), State()
    asyncio.run(mentor.mentor_command(msg, state))
    assert state.values["mentor_panel"]["message_id"] == msg.sent.message_id
    assert state.values["mentor_panel"]["kind"] == "navigation"
    asyncio.run(mentor.mentor_action(query("food", msg.sent), state))
    msg.sent.answer.assert_not_awaited()
    msg.sent.edit_text.assert_awaited_once()


def test_text_answer_creates_a_card_below_user_then_buttons_edit_it(monkeypatch):
    apis(monkeypatch)
    monkeypatch.setattr(f, "parse_step", AsyncMock(return_value="Присед"))
    msg, state = message(text="Присед"), State(form_kind="program_name", program_weekday=0)
    asyncio.run(f.receive(msg, state))
    assert state.values["mentor_panel"]["message_id"] == msg.sent.message_id
    asyncio.run(mentor.mentor_action(query("program_sets:3", msg.sent), state))
    msg.sent.answer.assert_not_awaited()
    msg.sent.edit_text.assert_awaited_once()
    assert "повторений" in msg.sent.edit_text.await_args.args[0]


def test_weight_confirmation_keeps_values_and_consumes_review_buttons(monkeypatch):
    apis(monkeypatch)
    monkeypatch.setattr(f, "parse_step", AsyncMock(return_value="75.5"))
    msg, state = message(text="75,5 кг"), State(form_kind="weight")
    asyncio.run(f.receive(msg, state))
    review = msg.sent
    token = state.values["pending_input"]["token"]
    asyncio.run(mentor.mentor_action(query("input_save:"+token, review), state))
    review.answer.assert_not_awaited()
    text = review.edit_text.await_args.args[0]
    assert text.startswith(review.text) and "75,5 кг записан" in text
    markup = review.edit_text.await_args.kwargs["reply_markup"]
    assert "mentor:receipt" in actions(markup)
    assert not any(a.startswith(("mentor:input_save:", "mentor:input_edit:")) for a in actions(markup))
    assert "pending_input" not in state.values


@pytest.mark.parametrize("outcome,status", [("confirm", "confirmed"), ("cancel", "cancelled")])
def test_ai_meal_review_only_changes_keyboard_not_the_answer(monkeypatch, outcome, status):
    entry = {"id":42, "status":status, "name":"Рис", "kcal":150, "protein":3, "fat":1, "carbs":30}
    apis(monkeypatch, {"/journal/action":{"entry":entry}})
    msg = message(text="Полный ответ ИИ с объяснением и оценкой еды.", markup=mentor.response_keyboard({"meal_draft":entry}))
    state = State(pending_reviews=[entry])
    asyncio.run(mentor.mentor_action(query("meal_"+outcome+":42", msg), state))
    msg.answer.assert_not_awaited()
    msg.edit_text.assert_not_awaited()
    msg.edit_reply_markup.assert_awaited_once()
    markup = msg.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert "mentor:receipt" in actions(markup)
    assert not any(a.startswith(("mentor:meal_confirm:", "mentor:meal_cancel:")) for a in actions(markup))
    assert state.values["pending_reviews"] == []


def test_structured_draft_confirmation_preserves_full_preview(monkeypatch):
    entry = {"id":42, "status":"confirmed", "kind":"program"}
    apis(monkeypatch, {"/workspace/action":{"entry":entry}})
    msg = message(text="Полная программа\nПрисед: 3 × 10\nЖим: 4 × 8")
    state = State(mentor_panel={"message_id":10, "chat_id":123, "kind":"form"}, pending_reviews=[entry])
    asyncio.run(mentor.mentor_action(query("record:confirm:42", msg), state))
    msg.answer.assert_not_awaited()
    assert msg.edit_text.await_args.args[0].startswith(msg.text)
    assert "Программа тренировок сохранена" in msg.edit_text.await_args.args[0]


def test_long_preview_is_not_trimmed_to_append_a_status(monkeypatch):
    entry = {"id":42, "status":"confirmed", "kind":"program"}
    apis(monkeypatch, {"/workspace/action":{"entry":entry}})
    msg = message(text="я"*3990)
    state = State(mentor_panel={"message_id":10, "chat_id":123, "kind":"form"})
    asyncio.run(mentor.mentor_action(query("record:confirm:42", msg), state))
    msg.edit_text.assert_not_awaited()
    msg.answer.assert_not_awaited()
    msg.edit_reply_markup.assert_awaited_once()


def test_confirmation_retains_all_preview_pages():
    async def run():
        msg = message()
        saved = {"message_id":10, "chat_id":123, "token":"old", "index":1,
            "pages":[{"text":"Первая часть", "rows":[]}, {"text":"Вторая часть", "rows":[]}]}
        state = State(mentor_panel=saved)
        panel = MentorPanel(msg, state, saved_card=saved)
        await panel.complete("Программа сохранена.", mentor.back_keyboard())
        assert [p["text"] for p in state.values["mentor_panel"]["pages"]] == ["Первая часть", "Вторая часть"]
        assert "Вторая часть" in msg.edit_text.await_args.args[0]
        assert state.values["mentor_panel"]["index"] == 1
        assert not any("record:confirm" in str(p["rows"]) for p in state.values["mentor_panel"]["pages"])
        msg.answer.assert_not_awaited()
    asyncio.run(run())


def test_receipt_button_does_not_invalidate_another_pending_input(monkeypatch):
    api = apis(monkeypatch)
    state = State(pending_input={"token":"new"}, form_token="active")
    msg = message()
    asyncio.run(mentor.mentor_action(query("receipt", msg), state))
    assert state.values == {"pending_input":{"token":"new"}, "form_token":"active"}
    api.assert_not_awaited()
    msg.answer.assert_not_awaited()


def test_navigation_from_ai_answer_reuses_a_separate_menu():
    async def run():
        msg = message(text="Do not overwrite this answer")
        state = State(mentor_cards=[{"message_id":4, "chat_id":123, "kind":"navigation"}])
        panel = MentorPanel(msg, state, saved_card={}, cards=state.values["mentor_cards"])
        await panel.answer("Питание")
        await panel.flush()
        msg.answer.assert_not_awaited()
        msg.edit_text.assert_not_awaited()
        assert msg.bot.edit_message_text.await_args.kwargs["message_id"] == 4
    asyncio.run(run())


def test_form_answer_retires_old_question_buttons_but_does_not_edit_its_text():
    async def run():
        msg = message(text="20 кг")
        state = State(mentor_panel={"message_id":4, "chat_id":123, "kind":"form"})
        replies = await reply_panel(msg, state)
        await replies.answer("Сколько повторений?")
        msg.edit_text.assert_not_awaited()
        assert msg.bot.edit_message_reply_markup.await_args.kwargs == {"chat_id":123,"message_id":4,"reply_markup":None}
        assert state.values["mentor_panel"]["message_id"] == msg.sent.message_id
    asyncio.run(run())


def test_form_reset_preserves_ui_but_discards_pending_write():
    state = State(pending_input={"token":"secret"}, mentor_panel={"message_id":4},
        mentor_cards=[{"message_id":4}], mentor_media={"message_id":5})
    asyncio.run(clear_input(state))
    assert set(state.values) == {"mentor_panel", "mentor_cards", "mentor_media"}


def test_graph_and_photos_share_one_reusable_protected_viewer():
    async def run():
        msg, state = message(), State()
        photo = message(mid=30)
        photo.photo = [object()]
        msg.answer_photo.return_value = photo
        panel = MentorPanel(msg, state)
        await panel.answer_photo("graph-file", caption="График", protect_content=True, reply_markup=mentor.back_keyboard())
        await panel.answer_photo("progress-file", caption="Фото", protect_content=True, reply_markup=mentor.back_keyboard())
        msg.answer.assert_not_awaited()
        msg.answer_photo.assert_awaited_once()
        assert msg.answer_photo.await_args.kwargs["protect_content"]
        assert msg.bot.edit_message_media.await_args.kwargs["message_id"] == 30
        assert msg.bot.edit_message_media.await_args.kwargs["media"].media == "progress-file"
    asyncio.run(run())


@pytest.mark.parametrize("error,new_send", [("message to edit not found", True), ("message is not modified", False)])
def test_media_recovery_only_resends_a_missing_viewer(error, new_send):
    async def run():
        msg = message()
        msg.answer_photo.return_value = message(mid=30)
        msg.bot.edit_message_media.side_effect = TelegramBadRequest(method=EditMessageMedia(chat_id=123,
            message_id=20, media={"type":"photo", "media":"file"}), message=error)
        state = State(mentor_media={"message_id":20,"chat_id":123})
        await MentorPanel(msg, state).answer_photo("file", caption="Фото", protect_content=True)
        assert bool(msg.answer_photo.await_count) is new_send
    asyncio.run(run())


def test_requisites_button_edits_the_menu_instead_of_sending():
    from src.bot.handlers.new_user import handle_user_call
    msg = message()
    call = query("unused", msg)
    call.data = "user:about"
    asyncio.run(handle_user_call(call, State()))
    msg.answer.assert_not_awaited()
    msg.edit_text.assert_awaited_once()


def test_favorite_updates_the_same_list_and_keeps_search_context(monkeypatch):
    data = dashboard()
    item = {"id":7,"name":"Суп","kcal":150,"protein":5,"fat":5,"carbs":20,
        "occurred_at":"2030-01-01T12:00:00+00:00","favorite":True}
    async def call(path, payload):
        if path == "/dashboard": return data
        if path == "/workspace/meals": return {"items":[item]}
        return {}
    api = AsyncMock(side_effect=call)
    monkeypatch.setattr(f, "api", api)
    monkeypatch.setattr(mentor, "api", api)
    msg, state = message(), State(mentor_panel={"message_id":10,"chat_id":123},
        library_query="Суп", library_favorites=False, library_offset=10)
    asyncio.run(mentor.mentor_action(query("favorite:7:1", msg), state))
    msg.answer.assert_not_awaited()
    assert "Суп" in msg.edit_text.await_args.args[0]
    assert "Избранное обновлено" not in msg.edit_text.await_args.args[0]
    assert "mentor:favorite:7:0" in actions(msg.edit_text.await_args.kwargs["reply_markup"])
    assert api.await_args.args[1] == {"telegram_user_id":123,"offset":10,"query":"Суп","favorites_only":False}


def test_course_mark_refreshes_course_not_a_separate_confirmation(monkeypatch):
    data = dashboard()
    data["workspace"]["courses"] = [{"id":7,"name":"Existing course","dose_text":"Existing dose",
        "start_date":"2030-01-01","end_date":"2030-01-31","timezone":"UTC","calendar":[],"done":1,"due":1}]
    async def call(path, payload):
        return data if path == "/dashboard" else {}
    api = AsyncMock(side_effect=call)
    monkeypatch.setattr(f, "api", api)
    monkeypatch.setattr(mentor, "api", api)
    msg, state = message(), State(mentor_panel={"message_id":10,"chat_id":123})
    asyncio.run(mentor.mentor_action(query("course_event:done:70", msg), state))
    msg.answer.assert_not_awaited()
    assert "Выполнено 1 из 1" in msg.edit_text.await_args.args[0]
    assert "Отметка сохранена" not in msg.edit_text.await_args.args[0]
