import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai import mentor_input as n
from src.ai.mentor_copy import QUESTIONS
from src.bot.handlers import mentor_flows as f
from test_mentor_input import client_for, message
from test_telegram_flows import State, dashboard


@pytest.mark.parametrize("kind,data,context,expected", [
    ("program", {}, {}, "В какой день недели тренируетесь? Начнём с одного дня, остальные можно добавить дальше."),
    ("program", {"exercises": [{"weekday": 0}]}, {}, "Какое упражнение делаете в этот день? Например, приседания или жим лёжа."),
    ("program", {"exercises": [{"weekday": 0, "name": "Присед"}]}, {}, "Сколько подходов в упражнении «Присед»? Например, три подхода за тренировку."),
    ("program", {"exercises": [{"weekday": 0, "name": "Присед", "sets": 3}]}, {}, "Сколько повторений в каждом подходе «Присед»? Например, по десять раз."),
    ("program", {"exercises": [{"weekday": 0, "name": "Присед", "sets": 3, "reps": 10}, {"name": "Жим"}]}, {}, "В какой день выполняете «Жим»? Назовите день недели, например понедельник."),
    ("workout_set", {}, {}, QUESTIONS["set_name"]),
    ("workout_set", {}, {"planned_exercise": "Присед"}, QUESTIONS["set_weight"]),
    ("workout_set", {"exercise": "Присед", "weight_kg": 0}, {}, QUESTIONS["set_reps"]),
    ("course_schedule", {}, {}, QUESTIONS["course_schedule"]),
    ("course_schedule", {"start_date": "2026-10-08"}, {}, "Когда заканчивается курс по вашей схеме? Назовите дату окончания, например 31 октября."),
    ("target", {"kcal": 2000, "protein": 100}, {}, "Сколько граммов жиров в вашей норме?"),
    ("course_supply", {"supply_amount": 0, "supply_unit": "таблетка"}, {}, "Сколько из этого запаса уходит на один приём по вашей схеме? Нужен расход в тех же единицах, что и запас; дозировку не пересчитываем."),
    ("reminder", {}, {"reminder_kind": "weekly"}, QUESTIONS["reminder_weekly"]),
])
def test_fallback_asks_only_the_next_missing_detail(kind, data, context, expected):
    question = n.followup(kind, data, context)
    assert question == expected
    assert question.count("?") == 1
    assert len(question) <= 350


@pytest.mark.parametrize("question", [
    "В какой день? Сколько подходов? Сколько повторений?",
    "Расскажите о тренировке. " * 12,
    "Укажите число подходов.",
    "Введите название упражнения.",
    "В какой день?\nСколько подходов?",
])
def test_model_clarifications_cannot_become_another_form(question):
    assert n.human_question("program", {"exercises": [{"weekday": 0, "name": "Присед"}]}, {}, question) == "Сколько подходов в упражнении «Присед»? Например, три подхода за тренировку."


def test_short_explanation_and_question_are_not_removed():
    question = "Время нужно для напоминания. Во сколько вам удобно?"
    assert n.human_question("reminder", {}, {}, question) == question


def test_partial_course_answers_keep_the_same_form_and_do_not_save(monkeypatch, tmp_path):
    from src.ai import telegram_mentor as t
    monkeypatch.setattr(t.config, "DATA_DIR", tmp_path)
    t.set_mentor_enabled(123, True)
    monkeypatch.setattr(n, "api", AsyncMock(return_value=dashboard()))
    writes = AsyncMock()
    monkeypatch.setattr(f, "api", writes)
    state = State(form_kind="course_schedule", form_token="same-step", course_data={"name": "Моя схема"})
    msg = message("с сегодняшнего дня")
    client = client_for({"start_date": "2026-10-08"}, "Когда заканчивается курс по вашей схеме?")
    async def run():
        await f.receive(msg, state, client)
        assert state.values["form_kind"] == "course_schedule"
        assert state.values["form_token"] == "same-step"
        assert state.values["course_data"] == {"name": "Моя схема"}
        assert msg.answer.await_args.args[0] == "Когда заканчивается курс по вашей схеме?"
        writes.assert_not_awaited()
    asyncio.run(run())


def test_partial_model_result_without_clarification_still_asks_instead_of_saving(monkeypatch, tmp_path):
    from src.ai import telegram_mentor as t
    monkeypatch.setattr(t.config, "DATA_DIR", tmp_path)
    t.set_mentor_enabled(123, True)
    monkeypatch.setattr(n, "api", AsyncMock(return_value=dashboard()))
    writes = AsyncMock()
    monkeypatch.setattr(f, "api", writes)
    state = State(form_kind="reminder", reminder_kind="weekly", form_token="same-step")
    msg = message("по воскресеньям")
    asyncio.run(f.receive(msg, state, client_for({"weekday": 6})))
    assert "Во сколько" in msg.answer.await_args.args[0]
    assert state.values["form_token"] == "same-step"
    writes.assert_not_awaited()


@pytest.mark.parametrize("action,prompt", [
    ("target", QUESTIONS["target"]),
    ("measurement", QUESTIONS["measurement"]),
    ("course", QUESTIONS["course"]),
    ("custom_goal", QUESTIONS["custom_goal"]),
])
def test_initial_questions_explain_the_input_without_changing_the_step(action, prompt):
    msg, state = message(""), State()
    asyncio.run(f.start_form(msg, 123, state, action))
    assert msg.answer.await_args.args[0] == prompt
    assert state.values["form_kind"] == action
    assert msg.answer.await_args.kwargs["reply_markup"] == f.keyboard([("Отмена", "menu")])


def test_program_entry_keeps_all_three_existing_buttons():
    msg, state = message(""), State()
    asyncio.run(f.start_form(msg, 123, state, "program"))
    markup = msg.answer.await_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert callbacks[:3] == ["mentor:program_day", "mentor:program_ai", "mentor:program_text"]


def test_weekly_reminder_still_uses_the_same_step_and_buttons(monkeypatch):
    monkeypatch.setattr(f, "api", AsyncMock(return_value=dashboard()))
    msg, state = message(""), State()
    query = SimpleNamespace(from_user=msg.from_user, message=msg, id="test")
    asyncio.run(f.dispatch(query, state, "reminder:weekly", None, None, None))
    assert state.values["form_kind"] == "reminder"
    assert state.values["reminder_kind"] == "weekly"
    assert msg.answer.await_args.args[0].startswith("В какой день недели присылать итоги?")
    assert msg.answer.await_args.kwargs["reply_markup"] == f.keyboard([("Отмена", "menu")])
