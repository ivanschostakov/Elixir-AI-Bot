import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai import telegram_mentor as t
from src.bot.handlers import mentor, mentor_flows as f


@pytest.fixture(autouse=True)
def private_store(tmp_path, monkeypatch):
    monkeypatch.setattr(t.config, "DATA_DIR", tmp_path)


def dashboard():
    return {"profile": {"goal": "weight_gain", "current_weight_kg": 90, "target_weight_kg": 100},
        "version": 2, "date": "2030-01-01", "now": "2030-01-01T12:00:00+00:00",
        "meals": [], "totals": {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0},
        "settings": {"timezone": "UTC", "daily_time": None},
        "workspace": {"today_exercises": [], "today_course": [], "today_workouts": [], "today_wellbeing": [], "sections": {}}}


class State:
    def __init__(self, **values):
        self.values = values
        self.state = None

    async def update_data(self, **values): self.values.update(values)
    async def get_data(self): return self.values
    async def set_state(self, value): self.state = value
    async def clear(self): self.values = {}; self.state = None


def test_reports_do_not_expose_details_without_opt_in(monkeypatch):
    data = dashboard()
    data["profile"]["current_weight_kg"] = 91
    monkeypatch.setattr(mentor, "api", AsyncMock(return_value=data))
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    asyncio.run(mentor.send_reminder(bot, {"telegram_user_id":123, "kind":"weekly", "text":"Пора посмотреть итоги."}))
    assert bot.send_message.await_args.args[1] == "Пора посмотреть итоги."
    bot.send_photo.assert_not_awaited()


def test_opt_in_weekly_report_sends_chart_and_saved_totals(monkeypatch):
    data = dashboard()
    data["settings"]["reminders"] = {"detailed_reports":True}
    data["workspace"]["weekly"] = {"from":"2030-01-01", "to":"2030-01-07", "average_kcal":1860,
        "workouts":3, "planned_workouts":3, "course_done":1, "course_due":1, "nutrition_days":5}
    monkeypatch.setattr(mentor, "api", AsyncMock(return_value=data))
    monkeypatch.setattr(f, "weight_chart_png", lambda _: b"png-test")
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    asyncio.run(mentor.send_reminder(bot, {"telegram_user_id":123, "kind":"weekly", "text":"Итоги"}))
    text = bot.send_photo.await_args.kwargs["caption"]
    assert "1 860" in text and "дней с записями: 5" in text
    assert len(text) < 1024
    assert bot.send_photo.await_args.kwargs["protect_content"]
    bot.send_message.assert_not_awaited()


def test_profile_update_keeps_diary_and_existing_profile(monkeypatch):
    call = AsyncMock(return_value={"ok": True, "profile": {"goal": "weight_gain"}, "version": 3})
    monkeypatch.setattr(t, "api", call)
    context = {"saved": {**dashboard(), "meals": [{"name": "rice"}]}, "telegram_user_id": 123,
        "source_text": "хочу набрать вес", "request_key": "real-message-1"}
    asyncio.run(t.execute_tool(context, "update_mentor_profile", {"patch": {"goal": "weight_gain"}, "evidence": "хочу набрать вес"}))
    assert context["saved"]["meals"] == [{"name": "rice"}]
    assert context["saved"]["profile"]["current_weight_kg"] == 90
    assert context["saved"]["version"] == 3


def test_photo_identifiers_and_delivery_receipts_never_enter_model_context():
    saved = {"workspace": {"measurements": [{"photo_file_id": "private-photo-handle", "waist_cm": 80}], "workouts": [{"set_receipts": {"request": "value"}}]}}
    text = t.memory_instructions({"saved": saved})
    assert "private-photo-handle" not in text and "set_receipts" not in text
    assert "waist_cm" in text


def test_today_does_not_invent_daily_course_or_nutrition_target():
    text = f.today_view(dashboard())
    assert "сегодня нет приёмов" in text and "Норма КБЖУ не задана" in text
    assert "Выполнено 0 из 2" in text
    data = dashboard()
    data["workspace"]["target"] = {"kcal": 2200}
    assert "из 2 200" in f.today_view(data)


def test_home_summary_uses_saved_values_and_never_invents_a_course():
    data = dashboard()
    data["totals"]["kcal"] = 1240
    data["workspace"]["target"] = {"kcal": 1900}
    result = f.home_view(data)
    assert "🌿 Наставник ElixirPeptide" in result
    assert "1 240 из 1 900" in result and "90 кг" in result
    assert "ближайший приём не запланирован" in result
    assert "20:00" not in result


def test_daily_checklist_only_counts_real_scheduled_course_events():
    data = dashboard()
    data["meals"] = [{"name": "Завтрак"}, {"name": "Обед"}]
    data["workspace"]["today_exercises"] = [{"name": "Присед"}]
    data["workspace"]["today_course"] = [{"name": "Курс", "status": "pending"}]
    tasks = f.daily_tasks(data)
    assert len(tasks) == 5 and sum(done for _, done in tasks) == 2
    data["workspace"]["today_course"] = []
    assert len(f.daily_tasks(data)) == 4


def test_meal_add_button_refers_to_existing_draft():
    kb = mentor.response_keyboard({"meal_draft": {"id": 91}})
    actions = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "mentor:meal_add:91" in actions
    assert "mentor:meal_confirm:91" in actions
    assert "mentor:meal_cancel:91" in actions


def test_nutrition_shows_actual_macro_targets():
    data = dashboard()
    data["totals"].update(kcal=1240, protein=96)
    data["workspace"]["target"] = {"kcal": 1900, "protein": 145}
    result = mentor.today_text(data)
    assert "1 240 / 1 900" in result and "96 / 145" in result
    assert "Жиры: ≈ 0 / не указан" in result


def test_closed_food_blocks_new_media_and_add_buttons(monkeypatch):
    data = dashboard()
    data["workspace"]["sections"] = {"food": False}
    monkeypatch.setattr(f, "api", AsyncMock(return_value=data))
    message = SimpleNamespace(answer=AsyncMock())
    query = SimpleNamespace(from_user=SimpleNamespace(id=123), message=message)
    for action in ["meal:video", "meal:photo", "meal_add:91"]:
        assert asyncio.run(f.dispatch(query, State(), action, None, None, None))
        assert "отключён" in message.answer.await_args.args[0]


def test_weight_chart_requires_real_history():
    message = SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock())
    asyncio.run(f.weight_chart(message, dashboard()))
    message.answer_photo.assert_not_awaited()
    data = dashboard()
    data["weights"] = [{"occurred_at": "2029-12-30T12:00:00+00:00", "weight_kg": 91},
                       {"occurred_at": "2029-12-31T12:00:00+00:00", "weight_kg": 90}]
    asyncio.run(f.weight_chart(message, data))
    assert message.answer_photo.await_args.args[0].data.startswith(b"\x89PNG")
    assert message.answer_photo.await_args.kwargs["protect_content"]


def test_menu_gates_default_open_and_hide_individual_sections(monkeypatch):
    actions = [b.callback_data for row in mentor.menu({"course": False}).inline_keyboard for b in row]
    assert "mentor:course" not in actions and "mentor:workouts" in actions
    monkeypatch.setenv("TELEGRAM_MENTOR_CLOSED_SECTIONS", "food")
    actions = [b.callback_data for row in mentor.menu().inline_keyboard for b in row]
    assert "mentor:meal" not in actions and "mentor:food" not in actions


def test_repeat_meal_is_a_server_draft_not_a_fake_saved_message(monkeypatch):
    call = AsyncMock(return_value={"entry": {"id": 9, "name": "rice", "kcal": 100, "status": "draft"}})
    monkeypatch.setattr(f, "api", call)
    message = SimpleNamespace(answer=AsyncMock())
    asyncio.run(f.repeat_meal(message, 123, "callback-1", 5, mentor.response_keyboard))
    assert call.await_args.args[0] == "/workspace/meals/repeat"
    text = message.answer.await_args.args[0]
    assert "Повторить" in text and "сохран" not in text.lower()
    kb = message.answer.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "mentor:meal_confirm:9"


def test_workout_set_message_persists_before_success(monkeypatch):
    t.set_mentor_enabled(123, True)
    call = AsyncMock(return_value={"entry": {"sets": [{"exercise": "Присед", "weight_kg": 40, "reps": 10}]}})
    monkeypatch.setattr(f, "api", call)
    message = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123), message_id=7,
        text="Присед | 40 | 10", answer=AsyncMock())
    state = State(form_kind="workout_set", workout_id=9)
    asyncio.run(f.receive(message, state))
    path, body = call.await_args.args
    assert path == "/workspace/workout/set" and body["exercise_set"]["weight_kg"] == 40
    assert body["telegram_user_id"] == 123 and body["entry_id"] == 9
    assert "сохранён" in message.answer.await_args.args[0]


def test_workout_dialogue_requires_weight_reps_and_confirmation(monkeypatch):
    t.set_mentor_enabled(123, True)
    call = AsyncMock(return_value=dashboard())
    monkeypatch.setattr(f, "api", call)
    message = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123), message_id=7,
        text="40", answer=AsyncMock(), edit_reply_markup=AsyncMock())
    state = State(form_kind="set_weight", planned_exercise="Присед", workout_id=9)
    asyncio.run(f.receive(message, state))
    assert state.values["form_kind"] == "set_reps"
    call.assert_not_awaited()
    message.text = "10"
    asyncio.run(f.receive(message, state))
    assert state.values["pending_set"] == {"exercise": "Присед", "weight_kg": 40, "reps": 10}
    call.assert_not_awaited()
    async def respond(path, body):
        if path == "/dashboard": return dashboard()
        assert path == "/workspace/workout/set" and body["request_key"] == "tg:123:7"
        return {"entry": {"sets": [body["exercise_set"]]}}
    call.side_effect = respond
    query = SimpleNamespace(from_user=message.from_user, message=message)
    asyncio.run(f.dispatch(query, state, "set_confirm:7", None, None, None))
    assert state.values["pending_set"] is None
    with pytest.raises(t.BridgeError):
        asyncio.run(f.dispatch(query, state, "set_confirm:7", None, None, None))


def test_home_keyboard_matches_requested_rows():
    rows = mentor.menu().inline_keyboard
    assert rows[0][0].text == "➕ Добавить еду"
    assert [[b.text for b in row] for row in rows[1:5]] == [
        ["📅 Сегодня", "🍽 Питание"], ["🏋️ Тренировки", "🧬 Мой курс"],
        ["📊 Прогресс", "💬 Спросить наставника"], ["👤 Профиль", "⚙️ Настройки"]]


def test_failed_form_does_not_claim_save(monkeypatch):
    t.set_mentor_enabled(123, True)
    monkeypatch.setattr(f, "api", AsyncMock(side_effect=t.BridgeError("Сервис недоступен")))
    message = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123), message_id=7,
        text="2000 100 70 240", answer=AsyncMock())
    state = State(form_kind="target")
    asyncio.run(f.receive(message, state))
    assert message.answer.await_args.args[0] == "Сервис недоступен"
    assert state.values["form_kind"] == "target"


def test_specialist_link_is_never_invented(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MENTOR_SPECIALIST_URL", raising=False)
    assert f.specialist_button() is None
    monkeypatch.setenv("TELEGRAM_MENTOR_SPECIALIST_URL", "javascript:alert(1)")
    assert f.specialist_button() is None
    monkeypatch.setenv("TELEGRAM_MENTOR_SPECIALIST_URL", "https://example.test/specialist")
    assert f.specialist_button().url == "https://example.test/specialist"


def test_course_interval_is_visible_and_dose_is_verbatim():
    entry = {"kind": "course", "name": "Existing", "source": "user", "dose_text": "exact user dose", "start_date": "2030-01-01",
        "end_date": "2030-01-31", "interval_days": 10, "weekdays": [], "times": ["09:00"], "timezone": "UTC"}
    result = f.draft_text(entry)
    assert "Каждые 10" in result and "exact user dose" in result


def test_next_exercise_uses_frozen_plan_and_completed_set_counts():
    entry = {"program_exercises": [{"weekday": 1, "name": "Squat", "sets": 2, "reps": 10}, {"weekday": 1, "name": "Press", "sets": 1, "reps": 8}], "sets": []}
    assert f.next_exercise(entry, 1) == "Squat"
    entry["sets"] = [{"exercise": "Squat"}, {"exercise": "Squat"}]
    assert f.next_exercise(entry, 1) == "Press"
    assert f.next_exercise(entry, 2) is None


def test_report_labels_include_period_and_real_sample_counts():
    report = {"days": 30, "from": "2030-01-01", "to": "2030-01-30", "weight_change_kg": -2,
        "weight_mean_7d": 81, "weight_samples_7d": 2, "wellbeing_mean": 4, "wellbeing_samples": 3,
        "energy_mean": None, "energy_samples": 0, "meals": 2, "nutrition": {"kcal": 1000},
        "workouts": 1, "duration_minutes": 30, "volume_kg": 100, "course_done": 0, "course_due": 0}
    result = f.weekly_view({"workspace": {"weekly": report}})
    assert "за 30 дней" in result and "81 кг · измерений 2" in result
    assert "Средняя энергия: нет данных" in result


def test_reminder_local_receipts_survive_ack_failure():
    assert not t.reminder_was_delivered(123, 99)
    t.remember_delivery(123, 99)
    assert t.reminder_was_delivered(123, 99)
    assert not t.reminder_was_delivered(124, 99)
    t.forget_delivery(123, 99)
    assert not t.reminder_was_delivered(123, 99)


def test_loop_sends_then_acks_and_failed_send_requests_retry(monkeypatch):
    item = {"telegram_user_id": 123, "delivery_id": 99, "token": "a"*32, "kind": "morning", "text": "Reminder"}
    seen = []
    async def call(path, body):
        seen.append((path, body))
        if path == "/reminder/due": return {"items": [item]}
        if path == "/reminder/check": return {"deliver": True}
        return {"ok": True}
    async def stop(_): raise asyncio.CancelledError()
    monkeypatch.setattr(mentor, "configured", lambda: True)
    monkeypatch.setattr(mentor, "api", call)
    monkeypatch.setattr(mentor.asyncio, "sleep", stop)
    bot = SimpleNamespace(send_message=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mentor.reminder_loop(bot))
    bot.send_message.assert_awaited_once()
    assert seen[-1][0] == "/reminder/ack" and seen[-1][1]["outcome"] == "sent"
    bot.send_message = AsyncMock(side_effect=RuntimeError("network unavailable"))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mentor.reminder_loop(bot))
    assert seen[-1][1]["outcome"] == "retry"


def test_loop_ack_retry_does_not_resend_successful_delivery(monkeypatch):
    item = {"telegram_user_id": 123, "delivery_id": 99, "token": "a"*32, "kind": "morning", "text": "Reminder"}
    async def call(path, body):
        if path == "/reminder/due": return {"items": [item]}
        if path == "/reminder/check": return {"deliver": True}
        raise t.BridgeError("Ack unavailable")
    async def stop(_): raise asyncio.CancelledError()
    monkeypatch.setattr(mentor, "configured", lambda: True)
    monkeypatch.setattr(mentor, "api", call)
    monkeypatch.setattr(mentor.asyncio, "sleep", stop)
    bot = SimpleNamespace(send_message=AsyncMock())
    for _ in range(2):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(mentor.reminder_loop(bot))
    bot.send_message.assert_awaited_once()
    assert t.reminder_was_delivered(123, 99)
