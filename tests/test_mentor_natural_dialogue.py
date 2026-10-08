import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai import mentor_input as n, telegram_mentor as t
from src.bot.handlers import mentor, mentor_flows as f
from src.bot.handlers.mentor_insights import daily_insight, weekly_insight
from test_mentor_input import client_for, message
from test_telegram_flows import State, dashboard


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(t.config, "DATA_DIR", tmp_path)
    t.set_mentor_enabled(123, True)
    monkeypatch.setattr(n, "api", AsyncMock(return_value=dashboard()))


def test_program_free_sentence_collects_all_details_without_reasking():
    msg = message("давай жим лёжа, три по десять")
    state = State(form_kind="program_name", form_token="one", program_weekday=0, program_exercises=[])
    client = client_for({"name":"Жим лёжа", "sets":3, "reps":10})
    asyncio.run(f.receive(msg, state, client))
    assert state.values["program_exercises"] == [{"weekday":0,"name":"Жим лёжа","sets":3,"reps":10}]
    assert client.responses.parse.await_count == 1
    assert msg.answer.await_count == 1


def test_workout_answer_contains_weight_and_reps_without_extra_question():
    state = State(form_kind="set_weight", form_token="one", planned_exercise="Присед", workout_id=7)
    msg = message("сорок килограмм, сделал десять раз")
    asyncio.run(f.receive(msg, state, client_for({"weight_kg":40,"reps":10})))
    assert state.values["pending_set"] == {"exercise":"Присед","weight_kg":40,"reps":10}
    assert msg.answer.await_count == 1


@pytest.mark.parametrize("intent", ["question", "pause", "unknown", "cancel"])
@pytest.mark.parametrize("kind", ["program_name", "meal_search", "progress_photo", "wellbeing_note"])
def test_intents_do_not_turn_into_names_or_records(monkeypatch, intent, kind):
    client = client_for({})
    client.responses.parse.return_value.output_parsed["intent"] = intent
    state = State(form_kind=kind, form_token="one", form_known={"sets":3})
    msg = message("а почему именно так?" if intent == "question" else "не знаю")
    writes, converse = AsyncMock(), AsyncMock()
    monkeypatch.setattr(f, "api", writes)
    monkeypatch.setattr(f, "converse", converse)
    asyncio.run(f.receive(msg, state, client))
    writes.assert_not_awaited()
    if intent == "question":
        converse.assert_awaited_once()
        assert state.values["form_known"]["sets"] == 3
    elif intent == "cancel": assert state.values == {}
    else: assert state.values["form_paused"] and state.values["form_known"] == {"sets":3}


def test_technical_outage_preserves_latest_answer_and_known_fields():
    client = client_for({})
    client.responses.parse.return_value.status = "incomplete"
    state = State(form_kind="program_sets", form_token="one", form_known={"name":"Присед"})
    msg = message("три")
    asyncio.run(f.receive(msg, state, client))
    assert state.values["form_answers"][-1]["answer"] == "три"
    assert state.values["form_known"] == {"name":"Присед"}
    assert "сервиса" in msg.answer.await_args.args[0]
    assert "Не совсем понял" not in msg.answer.await_args.args[0]


def test_long_conversation_compacts_history_without_restarting():
    state = State(form_kind="program_reps", form_token="one", form_known={"name":"Присед","sets":3},
        form_answers=[{"question":"Q", "answer":str(i)} for i in range(10)])
    msg = message("десять")
    result = asyncio.run(n.parse_step(msg, state, "program_reps", client_for({"reps":10})))
    assert result == "10" and len(state.values["form_answers"]) == 10
    assert state.values["form_known"]["name"] == "Присед"
    msg.answer.assert_not_awaited()


def test_plain_time_is_not_a_forbidden_technical_format():
    text = "В 08:00 удобно?"
    assert n.human_question("reminder", {}, {}, text) == text


def test_input_edit_keeps_other_collected_fields():
    state = State(form_kind="reminder", reminder_kind="weekly", form_known={"weekday":6,"clock":"19:00"},
        pending_input={"token":"one","kind":"reminder","payload":{"weekday":6,"weekly":"19:00"}})
    asyncio.run(f.confirm_input(message(""), 123, state, "input_edit:one"))
    assert state.values["form_known"] == {"weekday":6,"clock":"19:00"}
    assert state.values["editing"]


def test_text_confirmation_is_idempotent_and_only_uses_existing_preview(monkeypatch):
    api = AsyncMock(return_value={"ok":True})
    monkeypatch.setattr(f, "api", api)
    state = State(pending_reviews=[{"id":7,"kind":"meal","name":"Обед"}])
    msg = message("да, сохрани")
    async def run():
        assert await f.pending_text(msg,state)
        assert not await f.pending_text(msg,state)
    asyncio.run(run())
    api.assert_awaited_once_with("/journal/action", {"telegram_user_id":123,"entry_id":7,"action":"confirm"})


def test_ambiguous_confirmation_never_guesses_which_record(monkeypatch):
    api = AsyncMock()
    monkeypatch.setattr(f, "api", api)
    state = State(pending_reviews=[{"id":7,"kind":"meal"},{"id":8,"kind":"program"}])
    msg = message("да")
    assert asyncio.run(f.pending_text(msg,state))
    api.assert_not_awaited()
    assert "Какую запись" in msg.answer.await_args.args[0]


def test_failed_text_confirmation_retains_preview(monkeypatch):
    monkeypatch.setattr(f, "api", AsyncMock(side_effect=t.BridgeError("Не загрузилось")))
    state = State(pending_reviews=[{"id":7,"kind":"meal"}])
    with pytest.raises(t.BridgeError): asyncio.run(f.pending_text(message("сохрани"),state))
    assert state.values["pending_reviews"][0]["id"] == 7


def test_meal_edit_does_not_show_model_instructions(monkeypatch):
    monkeypatch.setattr(f, "api", AsyncMock(return_value=dashboard()))
    msg = message("")
    query = SimpleNamespace(from_user=msg.from_user, message=msg, id="one")
    asyncio.run(f.dispatch(query, State(), "meal_edit:7", None, None, None))
    shown = msg.answer.await_args.args[0]
    assert shown == "Что исправить в этом приёме пищи?"
    assert "Сохрани остальные" not in shown
    assert "черновик еды №7" in t.opening_question(123)


def test_wellbeing_free_answer_creates_one_preview(monkeypatch):
    entry = {"id":8,"kind":"wellbeing","status":"draft","score":4,"energy_score":3,"note":"плохо спал"}
    api = AsyncMock(return_value={"entry":entry})
    monkeypatch.setattr(f, "api", api)
    state = State(form_kind="wellbeing_score", form_token="one")
    msg = message("самочувствие четыре, энергии три, плохо спал")
    asyncio.run(f.receive(msg,state,client_for({"score":4,"energy_score":3,"note":"плохо спал"})))
    assert api.await_args.args[1]["data"] == {"score":4,"energy_score":3,"note":"плохо спал"}
    assert msg.answer.await_count == 1


def test_medical_warning_is_contextual_not_on_every_positive_checkin():
    assert "медицинской" not in f.wellbeing_prompt({"score":5,"energy_score":5})
    assert "медицинской" in f.wellbeing_prompt({"score":1,"energy_score":1})


def test_energy_question_and_buttons_are_one_message(monkeypatch):
    monkeypatch.setattr(f,"api",AsyncMock(return_value=dashboard()))
    msg = message("")
    query = SimpleNamespace(from_user=msg.from_user,message=msg,id="one")
    asyncio.run(f.dispatch(query,State(),"wellbeing:4",None,None,None))
    assert msg.answer.await_count == 1
    buttons = [b.callback_data for row in msg.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert "mentor:energy:3" in buttons and "mentor:energy:skip" in buttons


def test_reports_provide_next_step_without_treating_missing_days_as_zero():
    data = dashboard()
    assert "уже съели" in daily_insight(data)
    data["workspace"]["weekly"] = {"nutrition_days":1,"weight_change_kg":-2}
    assert "нельзя судить" in weekly_insight(data)
    data["workspace"]["weekly"] = {"nutrition_days":5,"protein_target_percent":65}
    assert "белка" in weekly_insight(data)


def test_record_correction_seeds_the_owned_draft(monkeypatch):
    entry = {"id":7,"kind":"measurement","status":"draft","waist_cm":80,"hips_cm":95}
    monkeypatch.setattr(f,"api",AsyncMock(return_value={"entry":entry}))
    state = State()
    asyncio.run(f.edit_record(message(""),123,state,7,"measurement"))
    assert state.values["form_known"] == {"waist_cm":80,"hips_cm":95}
    assert state.values["replace_id"] == 7


def test_profile_copy_does_not_explain_how_to_type():
    assert mentor.profile_text({}).endswith("Что изменилось?")
    assert "обычным сообщением" not in mentor.profile_text({})


def test_replaced_meal_is_not_an_ambiguous_pending_review():
    state = State(pending_reviews=[{"id":7,"kind":"meal"}])
    asyncio.run(f.remember_response(state, {"meal_draft":{"id":8,"kind":"meal"},"replaced_review_ids":[7]}))
    assert [e["id"] for e in state.values["pending_reviews"]] == [8]


def test_concurrent_text_confirmations_only_write_once(monkeypatch):
    api = AsyncMock(return_value={"ok":True})
    monkeypatch.setattr(f,"api",api)
    state = State(pending_reviews=[{"id":7,"kind":"meal"}])
    async def run():
        return await asyncio.gather(f.pending_text(message("да"),state), f.pending_text(message("да"),state))
    assert asyncio.run(run()) == [True,False]
    api.assert_awaited_once()


def test_yes_after_an_unrelated_question_does_not_confirm_an_old_draft(monkeypatch):
    api = AsyncMock()
    monkeypatch.setattr(f,"api",api)
    state = State(pending_reviews=[{"id":7,"kind":"meal"}],text_confirmation_ready=True)
    async def run():
        assert not await f.pending_text(message("а что по тренировкам?"),state)
        await f.remember_response(state,{"text":"Хотите обсудить программу?"})
        assert not await f.pending_text(message("да"),state)
    asyncio.run(run())
    api.assert_not_awaited()
    assert state.values["pending_reviews"][0]["id"] == 7


def test_measurement_correction_retains_private_photo_without_sending_it_to_model(monkeypatch):
    entry = {"id":7,"kind":"measurement","status":"draft","waist_cm":81,"photo_file_id":"private-handle","note":"личное"}
    monkeypatch.setattr(f,"api",AsyncMock(return_value={"entry":entry}))
    state = State()
    client = client_for({"waist_cm":81})
    async def run():
        await f.edit_record(message(""),123,state,7,"measurement",silent=True)
        await f.receive(message("нет, талия восемьдесят один"),state,client)
    asyncio.run(run())
    assert f.api.await_args.args[1]["data"]["photo_file_id"] == "private-handle"
    assert "private-handle" not in str(client.responses.parse.await_args)


def test_photo_correction_keeps_measurements(monkeypatch):
    entry = {"id":7,"kind":"measurement","status":"draft","waist_cm":81,"photo_file_id":"old","note":"личное"}
    monkeypatch.setattr(f,"api",AsyncMock(return_value={"entry":entry}))
    state = State()
    msg = message("")
    msg.photo = [SimpleNamespace(file_id="new")]
    msg.caption = None
    async def run():
        await f.edit_record(msg,123,state,7,"progress_photo",silent=True)
        await f.receive(msg,state)
    asyncio.run(run())
    assert f.api.await_args.args[1]["data"] == {"waist_cm":81,"photo_file_id":"new","note":"личное"}
    assert f.api.await_args.args[1]["replaces_id"] == 7


@pytest.mark.parametrize("mode,phone,available,expected", [
    ("free",True,True,False), ("professor",False,True,False),
    ("professor",True,False,False), ("professor",True,True,True)])
def test_voice_steps_keep_existing_access_gates(monkeypatch,mode,phone,available,expected):
    from src.bot.handlers import new_user, ai_helpers, mentor_messages
    from src.ai import helpers
    monkeypatch.setattr(helpers,"check_blocked",AsyncMock(return_value=True))
    monkeypatch.setattr(helpers,"CHAT_NOT_BANNED_FILTER",AsyncMock(return_value=True))
    monkeypatch.setattr(new_user.webapp_client,"get_user",AsyncMock(return_value={}))
    monkeypatch.setattr(new_user,"_resolve_last_used",lambda user:(new_user.LAST_USED_PROFESSOR if mode == "professor" else "free",False))
    monkeypatch.setattr(new_user,"_ensure_user",AsyncMock(return_value=SimpleNamespace(tg_phone=phone)))
    monkeypatch.setattr(new_user,"_can_use_professor_mode",lambda user:available)
    download = AsyncMock(return_value=b"audio")
    monkeypatch.setattr(ai_helpers,"_download_telegram_file_bytes",download)
    msg = message("")
    msg.voice, msg.video_note = SimpleNamespace(file_size=100), None
    clone = message("сорок килограмм")
    clone.as_ = lambda bot:clone
    msg.model_copy = lambda update:clone
    msg.bot = None
    client = SimpleNamespace(transcribe_audio_bytes=AsyncMock(return_value="сорок килограмм"))
    state = State(form_kind="weight",form_known={"weight":39})
    result = asyncio.run(mentor_messages.spoken_message(msg,state,client))
    assert (result is clone) == expected
    assert download.await_count == int(expected)
    assert state.values["form_known"] == {"weight":39}


def test_workout_results_do_not_show_nutrition_or_courses(monkeypatch):
    data = dashboard()
    data["workspace"]["weekly"] = {"workouts":2,"planned_workouts":3,"volume_kg":1000,"duration_minutes":90}
    monkeypatch.setattr(f,"api",AsyncMock(return_value=data))
    msg = message("")
    query = SimpleNamespace(from_user=msg.from_user,message=msg,id="one")
    asyncio.run(f.dispatch(query,State(),"workout_results",None,None,None))
    text = msg.answer.await_args.args[0]
    assert "тренировок: 2" in text and "Ккал" not in text and "Курс" not in text


def test_course_reminder_supports_text_confirmation(monkeypatch):
    api = AsyncMock(return_value={"ok":True})
    monkeypatch.setattr(f,"api",api)
    state = State(form_kind="course_reminder",form_token="one",course_id=8)
    async def run():
        await f.receive_value(message("в восемь вечера"),state,normalized="20:00")
        assert await f.pending_text(message("сохрани"),state)
    asyncio.run(run())
    api.assert_awaited_once_with("/workspace/course/reminder", {"telegram_user_id":123,"entry_id":8,"reminder_time":"20:00"})


def test_counterquestion_during_correction_retains_original_bot_transport(monkeypatch):
    converse = AsyncMock()
    monkeypatch.setattr(f,"converse",converse)
    state = State(form_kind="weight",form_known={"weight":70},
        pending_input={"kind":"weight","token":"one","payload":{"weight":70}})
    client = client_for({})
    client.responses.parse.return_value.output_parsed["intent"] = "question"
    bot, expert = object(), object()
    msg = message("нет, почему этот вес?")
    asyncio.run(f.pending_text(msg,state,client,professor_bot=bot,expert_client=expert))
    converse.assert_awaited_once_with(msg,state,bot,client,expert)
    assert state.values["form_known"]["weight"] == 70


@pytest.mark.parametrize("before,patch,expected", [
    ({"weekdays":[0,2],"interval_days":None},{"weekdays":None,"interval_days":3},{"weekdays":None,"interval_days":3}),
    ({"weekdays":None,"interval_days":3},{"weekdays":[1,4],"interval_days":None},{"weekdays":[1,4],"interval_days":None})])
def test_course_correction_switches_schedule_mode_without_losing_dates(before,patch,expected):
    dates = {"start_date":"2026-10-08","end_date":"2026-10-18","times":["08:00"]}
    merged = n.merge_known("course_record",{**dates,**before},patch)
    assert merged == {**dates,**expected}
    assert n.canonical("course_schedule",merged,False,{})


def test_missing_text_repeats_the_question_not_typing_instructions():
    msg = message("")
    state = State(form_question="Сколько вы сейчас весите?")
    assert asyncio.run(n.parse_step(msg,state,"weight",None)) is None
    assert msg.answer.await_args.args[0] == "Сколько вы сейчас весите?"
