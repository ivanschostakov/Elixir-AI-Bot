import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.ai.mentor_copy import QUESTIONS
from src.bot.handlers import mentor_flows as f, mentor_format
from test_telegram_flows import State, dashboard


@pytest.fixture(autouse=True)
def local_settings(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MENTOR_CLOSED_SECTIONS", raising=False)
    monkeypatch.delenv("TELEGRAM_MENTOR_SPECIALIST_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_MENTOR_SUPPORT_URL", raising=False)


def message(text=""):
    return SimpleNamespace(from_user=SimpleNamespace(id=123), text=text, answer=AsyncMock())


def dispatch(action, msg, state=None):
    query = SimpleNamespace(id="flow-fixes", from_user=msg.from_user, message=msg)
    return asyncio.run(f.dispatch(query, state or State(), action, None, None, None))


@pytest.mark.parametrize("percent", [0, 40])
def test_populated_progress_formats_the_baseline_date(monkeypatch, percent):
    saved = dashboard()
    saved["workspace"].update(weekly={}, goal_progress={"percent": percent, "since": "2030-01-01T12:00:00+00:00"})
    async def api(path, payload):
        return {"weight_change_kg": 0} if path == "/workspace/report" else saved
    monkeypatch.setattr(f, "api", AsyncMock(side_effect=api))
    msg = message()
    assert dispatch("progress", msg)
    text = msg.answer.await_args.args[0]
    assert f"{percent}%" in text and "1 января" in text


def test_populated_measurements_format_the_record_date(monkeypatch):
    saved = dashboard()
    saved["workspace"]["measurements"] = [{"id": 8, "kind": "measurement", "waist_cm": 82,
        "occurred_at": "2030-01-01T12:00:00+00:00"}]
    monkeypatch.setattr(f, "api", AsyncMock(return_value=saved))
    msg = message()
    assert dispatch("measurements", msg)
    assert "1 января" in msg.answer.await_args.args[0]
    assert "82 см" in msg.answer.await_args.args[0]


@pytest.mark.parametrize("kind,clocks", [("morning", ["07:00", "08:00", "09:00"]),
    ("evening", ["19:00", "20:00", "21:00"])])
def test_reminder_question_and_presets_render_once(monkeypatch, kind, clocks):
    monkeypatch.setattr(f, "api", AsyncMock(return_value=dashboard()))
    msg, state = message(), State()
    dispatch("reminder:"+kind, msg, state)
    msg.answer.assert_awaited_once()
    assert msg.answer.await_args.args[0].count(QUESTIONS["reminder_clock"]) == 1
    actions = [b.callback_data for row in msg.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert all("mentor:reminder_clock:"+clock in actions for clock in clocks)
    assert "mentor:reminder_clock:off" in actions
    assert state.state == f.Input.value and state.values["reminder_kind"] == kind


RECORD_ROUTES = [
    ("workout", "workout_results", "workouts"),
    ("activity_log", "workout_results", "workouts"),
    ("measurement", "measurements", "measurements"),
    ("wellbeing", "today", "today"),
    ("target", "nutrition", "food"),
    ("program", "workout_plan", "workouts"),
    ("course", "course", "course"),
]


@pytest.mark.parametrize("kind,confirmed_route,cancelled_route", RECORD_ROUTES)
@pytest.mark.parametrize("outcome", ["confirm", "cancel"])
@pytest.mark.parametrize("transport", ["button", "text"])
def test_record_followup_matches_kind_and_authoritative_status(monkeypatch, kind, confirmed_route, cancelled_route, outcome, transport):
    entry = {"id": 7, "kind": kind, "status": "draft", "name": "Test", "sets": [],
        "duration_minutes": 30, "score": 4, "waist_cm": 82, "kcal": 2000}
    status = "confirmed" if outcome == "confirm" else "cancelled"
    async def api(path, payload):
        return dashboard() if path == "/dashboard" else {"entry": {**entry, "status": status}}
    monkeypatch.setattr(f, "api", AsyncMock(side_effect=api))
    msg, state = message("да" if outcome == "confirm" else "отмена"), State(pending_reviews=[entry])
    if transport == "button":
        dispatch("record:"+outcome+":7", msg, state)
    else:
        assert asyncio.run(f.pending_text_locked(msg, state))
    buttons = [b for row in msg.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    expected = confirmed_route if outcome == "confirm" else cancelled_route
    assert "mentor:"+expected in [b.callback_data for b in buttons]
    assert all(b.text != "Открыть результаты" for b in buttons)
    if outcome == "cancel":
        assert all("результат" not in b.text.lower() for b in buttons)
        assert "сохранена" not in msg.answer.await_args.args[0].lower()
    assert state.values["pending_reviews"] == []


def test_panel_cancellation_also_returns_to_its_section(monkeypatch):
    class Panel(SimpleNamespace):
        pass
    monkeypatch.setattr(f, "MentorPanel", Panel)
    async def api(path, payload):
        return dashboard() if path == "/dashboard" else {"entry": {"id": 7, "kind": "workout", "status": "cancelled"}}
    monkeypatch.setattr(f, "api", AsyncMock(side_effect=api))
    msg = Panel(from_user=SimpleNamespace(id=123), complete=AsyncMock())
    dispatch("record:cancel:7", msg)
    buttons = [b for row in msg.complete.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(b.callback_data == "mentor:workouts" for b in buttons)
    assert all("результат" not in b.text.lower() for b in buttons)


@pytest.mark.parametrize("text,status,route", [("да", "cancelled", "food"), ("отмена", "confirmed", "nutrition")])
def test_text_receipt_uses_returned_status_not_requested_action(monkeypatch, text, status, route):
    entry = {"id": 7, "kind": "meal", "name": "Обед", "status": status}
    monkeypatch.setattr(f, "api", AsyncMock(return_value={"entry": entry}))
    msg = message(text)
    assert asyncio.run(f.pending_text_locked(msg, State(pending_reviews=[{**entry, "status": "draft"}])))
    response = msg.answer.await_args
    assert response.args[0].startswith("Записано:" if status == "confirmed" else "Не сохраняю.")
    assert any(b.callback_data == "mentor:"+route for row in response.kwargs["reply_markup"].inline_keyboard for b in row)


def test_empty_home_has_one_empty_state_without_unset_sections():
    text = f.home_view({**dashboard(), "profile": {}})
    assert "пока нет записей" in text
    assert len(text.splitlines()) <= 3
    assert all(word not in text for word in ("не задана", "не запланирован", "0 из 2", "Мои нормы КБЖУ"))


def test_populated_home_keeps_real_values_and_summarizes_activity():
    saved = dashboard()
    saved["totals"]["kcal"] = 1240
    saved["workspace"].update(target={"kcal": 1900}, today_workouts=[{}], today_activities=[
        {"name": "Very long activity name "*50, "duration_minutes": 30.0},
        {"name": "Walk", "duration_minutes": 15.5}])
    text = f.home_view(saved)
    assert "1 240 из 1 900" in text and "90 кг" in text
    assert "45,5 мин" in text and "Very long" not in text
    assert "не запланирован" not in text and len(text) < 500


def test_home_keeps_scheduled_course_and_recorded_wellbeing():
    saved = {**dashboard(), "profile": {}}
    saved["workspace"].update(today_wellbeing=[{"score": 4}], courses=[{"calendar": [
        {"status": "pending", "occurred_at": "2030-01-01T14:00:00+00:00"}]}])
    text = f.home_view(saved)
    assert "Самочувствие: 4/5" in text and "01.01 в 14:00" in text
    assert "пока нет записей" not in text


@pytest.mark.parametrize("value,expected", [(100, "100"), (100.0, "100"), ("100.0", "100"),
    (Decimal("99.50"), "99,5"), (2000, "2 000"), (None, "нет данных")])
def test_shared_number_formatting(value, expected):
    assert mentor_format.number_text(value) == expected
    assert f.fmt(value) == expected


@pytest.mark.parametrize("field,value,expected", [
    ("current_weight_kg", "100.0", "100"), ("height_cm", Decimal("180.0"), "180"),
    ("age", 35.0, "35"), ("activity", "moderate", mentor_format.ACTIVITY["moderate"]),
    ("activity", "Хожу пешком", "Хожу пешком"), ("preferences", "100.0", "100.0"),
])
def test_profile_values_format_only_numeric_fields_and_known_activity(field, value, expected):
    assert mentor_format.profile_value(field, value) == expected


@pytest.mark.parametrize("url", ["", "https://t.me/ShostakovIV", "https://t.me/ShostakovIV/123",
    "https://example.test/specialist", "javascript:alert(1)", "https://t.me/doctors", "https://t.me/c/123",
    "https://name:password@t.me/doctors/12", "https://t.me.evil.test/doctors/12", "https://[bad"])
def test_specialist_link_requires_a_configured_doctors_topic(monkeypatch, url):
    monkeypatch.setenv("TELEGRAM_MENTOR_SPECIALIST_URL", url)
    assert f.specialist_button() is None


@pytest.mark.parametrize("setting", ["TELEGRAM_MENTOR_SPECIALIST_URL", "TELEGRAM_MENTOR_SUPPORT_URL"])
@pytest.mark.parametrize("url", ["https://t.me/doctors/123", "https://t.me/c/1234567890/123"])
def test_configured_doctors_topic_is_preserved(monkeypatch, setting, url):
    monkeypatch.setenv(setting, url)
    assert f.specialist_button().url == url


def test_missing_specialist_topic_has_safe_explicit_unavailable_state(monkeypatch):
    monkeypatch.setattr(f, "api", AsyncMock(return_value=dashboard()))
    msg = message()
    dispatch("specialist", msg)
    text = msg.answer.await_args.args[0]
    assert "ShostakovIV" not in text and "не настроена" in text
    assert not any(b.url for row in msg.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row)


@pytest.mark.parametrize("backend_succeeds", [True, False])
def test_privacy_erases_onboarding_metadata_only_after_backend_success(monkeypatch, backend_succeeds):
    from src.ai import telegram_mentor as bridge
    from src.bot.handlers import mentor_onboarding
    events = []
    async def api(path, payload):
        if path == "/dashboard":
            return dashboard()
        assert path == "/workspace/privacy/erase"
        events.append("backend")
        if not backend_succeeds:
            raise bridge.BridgeError("Unavailable")
        return {"ok": True}
    monkeypatch.setattr(f, "api", AsyncMock(side_effect=api))
    erase = Mock(side_effect=lambda uid: events.append("onboarding"))
    monkeypatch.setattr(mentor_onboarding, "erase", erase)
    for name in ("reset_mentor_conversations", "clear_opening_question", "opening_question", "forget_all_deliveries"):
        monkeypatch.setattr(bridge, name, Mock())
    state = State(erase_token="current")
    if backend_succeeds:
        dispatch("privacy_erase_confirm:current", message(), state)
        erase.assert_called_once_with(123)
        assert events == ["backend", "onboarding"]
        assert not state.values
    else:
        with pytest.raises(bridge.BridgeError):
            dispatch("privacy_erase_confirm:current", message(), state)
        erase.assert_not_called()
        assert state.values["erase_token"] == "current"
