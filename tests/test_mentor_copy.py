import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai.mentor_copy import QUESTIONS, EDIT_QUESTIONS, edit_question
from src.ai import mentor_input, telegram_mentor
from src.bot.handlers import mentor, mentor_flows, new_user_helpers
from test_mentor_input import message
from test_telegram_flows import State


@pytest.mark.parametrize("text", [*QUESTIONS.values(), *EDIT_QUESTIONS.values()])
def test_questions_are_explanatory_bounded_and_do_not_require_a_format(text):
    assert 70 <= len(text) <= 350
    assert text.count("?") <= 1
    assert not re.search(r"HH:MM|ЧЧ:ММ|YYYY|IANA|JSON|\||обычным сообщением|^Введите|^Укажите|^Заполните", text)
    assert "голос" not in text and "видео" not in text


@pytest.mark.parametrize("mode,phone,credits,media", [
    ("new", "+70000000000", 1, True),
    ("professor", "+70000000000", 1, False),
    ("new", None, 1, False),
    ("new", "+70000000000", 0, False),
])
def test_food_input_options_match_existing_media_gates(monkeypatch, mode, phone, credits, media):
    user = SimpleNamespace(last_used=mode, tg_phone=phone, premium_requests=credits, premium_until=None)
    get_user = AsyncMock(return_value=user)
    monkeypatch.setattr(new_user_helpers.webapp_client, "get_user", get_user)
    text = asyncio.run(mentor.meal_question(123))
    assert text.startswith(QUESTIONS["meal"])
    assert "примерно" in text and "необязательно" in text
    assert ("голосовое" in text) == media
    assert ("фото еды" in text) == media
    assert ("кружочек" in text) == media
    get_user.assert_awaited_once_with("tg_id", 123)


def test_food_question_remains_usable_if_mode_lookup_is_unavailable(monkeypatch):
    monkeypatch.setattr(new_user_helpers.webapp_client, "get_user", AsyncMock(side_effect=RuntimeError()))
    assert asyncio.run(mentor.meal_question(123)) == QUESTIONS["meal"]


@pytest.mark.parametrize("kind", ["weight", "measurement", "target", "program", "workout_set", "reminder"])
def test_edit_question_names_the_relevant_values_and_preserves_known_data(kind):
    msg, state = message(""), State(form_kind=kind, form_known={"kept":"value"},
        pending_input={"token":"one", "kind":kind, "payload":{}})
    asyncio.run(mentor_flows.confirm_input(msg, 123, state, "input_edit:one"))
    assert msg.answer.await_args.args[0] == edit_question(kind)
    assert state.values["form_known"] == {"kept":"value"}
    assert state.values["editing"] and state.values["form_kind"] == kind


def test_model_instructions_keep_clarity_without_changing_saving_rules():
    assert "2-3 предложений" in mentor_input.INSTRUCTIONS
    assert "2-3 предложения" in telegram_mentor.CATALOG_INSTRUCTIONS
    assert "Не записывай примеры" in mentor_input.INSTRUCTIONS
    assert "Нет инструментов и права сохранять данные" in mentor_input.INSTRUCTIONS
    assert "не изображай врача" in telegram_mentor.CATALOG_INSTRUCTIONS.lower()


def test_usable_clarification_keeps_its_example():
    text = "Какой примерно был размер порции? Например, небольшая тарелка или два кусочка."
    assert mentor_input.human_question("weight", {}, {}, text) == text
