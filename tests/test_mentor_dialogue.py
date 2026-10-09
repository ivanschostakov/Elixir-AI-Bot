import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai import telegram_mentor as t
from src.ai.mentor_records import RECORDS
from src.bot.handlers import mentor, mentor_flows as f
from src.bot.handlers.mentor_dialogue import TASKS
from test_mentor_input import message
from test_telegram_flows import State, dashboard


@pytest.mark.parametrize("action", list(TASKS))
def test_buttons_use_the_existing_ai_conversation_not_an_input_fsm(monkeypatch, action):
    send = AsyncMock()
    monkeypatch.setattr(mentor, "run_ai_action", send)
    monkeypatch.setattr(f, "api", AsyncMock(return_value=dashboard()))
    state = State(form_kind="program_reps", form_known={"name": "old exercise"})
    state.state = f.Input.value
    msg = message("")
    query = SimpleNamespace(from_user=msg.from_user, message=msg, data="mentor:"+action, id="task-1")
    assert asyncio.run(f.dispatch(query, state, action, None, None, None))
    assert send.await_args.args[-1] == TASKS[action]
    assert state.state is None and not state.values.get("form_kind")


def test_closed_sections_still_block_conversational_buttons(monkeypatch):
    saved = dashboard()
    saved["workspace"]["sections"]["workouts"] = False
    monkeypatch.setattr(f, "api", AsyncMock(return_value=saved))
    send = AsyncMock()
    monkeypatch.setattr(mentor, "run_ai_action", send)
    msg = message("")
    query = SimpleNamespace(from_user=msg.from_user, message=msg, id="task-1")
    asyncio.run(f.dispatch(query, State(), "workout_start", None, None, None))
    send.assert_not_awaited()
    assert "отключён" in msg.answer.await_args.args[0]


@pytest.mark.parametrize("name", RECORDS)
def test_buttons_are_not_evidence_of_performed_activity_or_measurements(monkeypatch, name):
    write = AsyncMock()
    monkeypatch.setattr(t, "api", write)
    context = {"telegram_user_id": 123, "button_action": "workout_start"}
    result = asyncio.run(t.execute_tool(context, name, {"data": {}}))
    assert result["error"] == "user_facts_required"
    write.assert_not_awaited()


@pytest.mark.parametrize("name,data", [
    ("draft_mentor_activity", {"name": "Прогулка", "duration_minutes": 30}),
    ("draft_mentor_workout", {"sets": [{"exercise": "Присед", "weight_kg": 40, "reps": 10}], "duration_minutes": 30}),
    ("draft_mentor_measurement", {"waist_cm": 90}),
    ("draft_mentor_wellbeing", {"score": 4, "note": "Немного устал"}),
])
def test_record_tools_only_create_owned_drafts_and_replace_the_review(monkeypatch, name, data):
    kind = RECORDS[name][0]
    write = AsyncMock(return_value={"entry": {"id": 88, "kind": kind, "status": "draft", **data}})
    monkeypatch.setattr(t, "api", write)
    context = {"telegram_user_id": 123, "request_key": "real-message", "saved": {"version": 2}}
    asyncio.run(t.execute_tool(context, name, {"data": data, "replaces_id": 77, "telegram_user_id": 999, "status": "confirmed"}))
    path, body = write.await_args.args
    assert path == "/workspace/draft" and body["kind"] == kind
    assert body["telegram_user_id"] == 123 and body["expected_version"] == 2
    assert body["replaces_id"] == 77 and "status" not in body
    assert context["record_drafts"][0]["id"] == 88
    assert context["replaced_review_ids"] == [77]


def test_confirmed_retry_does_not_offer_save_again(monkeypatch):
    monkeypatch.setattr(t, "api", AsyncMock(return_value={"entry": {"id": 88, "kind": "activity_log", "status": "confirmed"}}))
    context = {"telegram_user_id": 123, "request_key": "same-message"}
    asyncio.run(t.execute_tool(context, "draft_mentor_activity", {"data": {"name": "Walk", "duration_minutes": 30}}))
    assert not context.get("record_drafts")


@pytest.mark.parametrize("active", [None, {"id": 77}])
def test_legacy_workout_identity_comes_from_backend_not_model(monkeypatch, active):
    write = AsyncMock(return_value={"entry": {"id": 88, "kind": "workout", "status": "draft"}})
    monkeypatch.setattr(t, "api", write)
    context = {"telegram_user_id": 123, "request_key": "finish", "saved": {"workspace": {"active_workout": active}}}
    data = {"sets": [{"exercise": "Squat", "weight_kg": 40, "reps": 10}], "duration_minutes": 30, "active_entry_id": 999}
    asyncio.run(t.execute_tool(context, "draft_mentor_workout", {"data": data}))
    stored = write.await_args.args[1]["data"]
    assert stored.get("active_entry_id") == (77 if active else None)
    assert data["active_entry_id"] == 999


def test_record_read_uses_transport_identity_and_strips_receipts(monkeypatch):
    call = AsyncMock(return_value={"entry": {"id": 77, "status": "cancelled", "request_key": "private", "set_receipts": {}}})
    monkeypatch.setattr(t, "api", call)
    result = asyncio.run(t.execute_tool({"telegram_user_id": 123}, "get_mentor_record", {"entry_id": 77, "telegram_user_id": 999}))
    assert call.await_args.args[1] == {"telegram_user_id": 123, "entry_id": 77}
    assert result["entry"] == {"id": 77, "status": "cancelled"}


def test_review_always_shows_the_actual_server_draft_not_only_model_prose():
    entry = {"id": 88, "kind": "activity_log", "status": "draft", "name": "Прогулка", "duration_minutes": 30}
    response = {"text": "Готово к проверке.", "record_drafts": [entry]}
    state = State()
    asyncio.run(f.remember_response(state, response))
    assert "30 мин" in response["text"] and "не изменение программы" in response["text"]
    assert state.values["pending_reviews"] == [entry]
    callbacks = [b.callback_data for row in mentor.response_keyboard(response).inline_keyboard for b in row]
    assert "mentor:record:confirm:88" in callbacks and "mentor:record_edit:activity_log:88" in callbacks


def test_natural_correction_returns_to_same_conversation_without_a_form(monkeypatch, tmp_path):
    monkeypatch.setattr(t.config, "DATA_DIR", tmp_path)
    state = State(pending_reviews=[{"id": 88, "kind": "activity_log"}], text_confirmation_ready=True)
    write = AsyncMock()
    monkeypatch.setattr(f, "api", write)
    assert not asyncio.run(f.pending_text_locked(message("нет, было полчаса"), state))
    assert "replaces_id" in t.opening_question(123) and "88" in t.opening_question(123)
    assert state.state is None
    write.assert_not_awaited()


def test_legacy_finish_asks_for_duration_without_finishing_or_running_a_timer(monkeypatch):
    call = AsyncMock(return_value=dashboard())
    monkeypatch.setattr(f, "api", call)
    state, msg = State(), message("")
    query = SimpleNamespace(from_user=msg.from_user, message=msg, id="task-1")
    asyncio.run(f.dispatch(query, state, "workout_finish:77", None, None, None))
    assert state.values["form_kind"] == "workout_duration"
    assert all(c.args[0] != "/workspace/workout/finish" for c in call.await_args_list)


def test_cancelled_activity_does_not_turn_into_a_saved_workout(monkeypatch):
    call = AsyncMock(return_value={"entry": {"id": 88, "kind": "activity_log", "status": "cancelled"}})
    monkeypatch.setattr(f, "api", call)
    state = State(pending_reviews=[{"id": 88, "kind": "activity_log"}], text_confirmation_ready=True)
    msg = message("нет")
    assert asyncio.run(f.pending_text_locked(msg, state))
    assert "отменён" in msg.answer.await_args.args[0]
    assert not state.values["pending_reviews"]


def test_overlapping_messages_continue_one_conversation_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(t.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(t, "api", AsyncMock(return_value={"version": 2, "profile": {}}))
    seen = []
    async def send(**kwargs):
        seen.append((kwargs["input_text"], kwargs["conversation_id"]))
        await asyncio.sleep(0)
        return {"conversation_id": "mentor-one", "text": "Уточнение"}
    client = t.TelegramAIClient(SimpleNamespace(send_message_v2=send), 123, "professor")
    async def run():
        await asyncio.gather(client.send_message_v2(input_text="Первый ответ", conversation_id="ordinary"),
            client.send_message_v2(input_text="Уточнение", conversation_id="ordinary"))
    asyncio.run(run())
    assert seen == [("Первый ответ", None), ("Уточнение", "mentor-one")]


def test_editing_a_saved_program_does_not_try_to_replace_a_confirmed_row(monkeypatch):
    call = AsyncMock(return_value={"entry": {"id": 88, "kind": "program", "status": "draft"}})
    monkeypatch.setattr(t, "api", call)
    context = {"telegram_user_id": 123, "request_key": "change-monday", "saved": {
        "workspace": {"program": {"id": 11, "status": "confirmed"}}}}
    asyncio.run(t.execute_tool(context, "draft_mentor_program", {"exercises": [], "replaces_id": 11}))
    assert "replaces_id" not in call.await_args.args[1]
    assert context["program_draft"]["id"] == 88 and not context.get("replaced_review_ids")


@pytest.mark.parametrize("photo,limit", [(False, 4096), (True, 1024)])
def test_long_saved_receipt_is_paginated_without_losing_any_text(photo, limit):
    from src.bot.handlers.mentor_panel import MentorPanel
    from test_mentor_navigation import message as card
    msg, state = card(), State()
    if photo:
        msg.photo = [object()]
    text = "Тренировка сохранена.\n" + "Присед: 40 кг, 10 повторений 🏋️\n"*200
    asyncio.run(MentorPanel(msg, state).complete(text, replace=True))
    pages = state.values["mentor_panel"]["pages"]
    assert "".join(page["text"] for page in pages) == text
    assert all(len((page["text"]+"\n\n999 / 999").encode("utf-16-le")) // 2 <= limit for page in pages)
    msg.answer.assert_not_awaited()
