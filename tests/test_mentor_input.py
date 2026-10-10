import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

from src.ai import mentor_input as n, telegram_mentor as t
from src.bot.handlers import mentor, mentor_flows as f
from test_telegram_flows import State, dashboard


def message(text):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=123),
        chat=SimpleNamespace(id=123), message_id=900, answer=AsyncMock())


def client_for(data, clarification=None, skip=False):
    response = SimpleNamespace(status="completed", usage=None,
        output_parsed={"data": data, "clarification": clarification, "skip": skip})
    return SimpleNamespace(responses=SimpleNamespace(parse=AsyncMock(return_value=response)))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(t.config, "DATA_DIR", tmp_path)
    t.set_mentor_enabled(123, True)
    monkeypatch.setattr(n, "api", AsyncMock(return_value=dashboard()))


@pytest.mark.parametrize("kind,text,data,context,expected", [
    ("weight", "сейчас вешу семьдесят пять с половиной", {"weight":75.5}, {}, "75.5"),
    ("target", "моя норма две тысячи калорий", {"kcal":2000}, {}, "2000.0"),
    ("target", "2000 калорий, белка сто, жиров семьдесят, углеводов 240", {"kcal":2000,"protein":100,"fat":70,"carbs":240}, {}, "2000.0 100.0 70.0 240.0"),
    ("measurement", "талия восемьдесят", {"waist_cm":80}, {}, "80.0 - -"),
    ("program_sets", "три подхода", {"value":3}, {}, "3"),
    ("program_reps", "по десять повторов", {"value":10}, {}, "10"),
    ("set_weight", "с гантелями двадцать кило", {"value":20}, {}, "20.0"),
    ("set_reps", "сделал десять", {"value":10}, {}, "10"),
    ("workout_duration", "тренировался полтора часа", {"value":90}, {}, "90.0"),
    ("wellbeing_energy", "энергия четыре из пяти", {"value":4}, {}, "4"),
    ("reminder", "каждое воскресенье в семь вечера", {"weekday":6,"clock":"19:00"}, {"reminder_kind":"weekly"}, "7 19:00"),
    ("reminder", "в восемь утра", {"clock":"08:00"}, {"reminder_kind":"morning"}, "08:00"),
    ("reminder", "после трёх дней без записей", {"days":3}, {"reminder_kind":"inactivity"}, "3"),
    ("course_reminder", "в девять вечера", {"clock":"21:00"}, {}, "21:00"),
    ("timezone", "я живу в Москве", {"timezone":"Europe/Moscow"}, {}, "Europe/Moscow"),
    ("program", "в понедельник присед три по десять", {"exercises":[{"weekday":0,"name":"Присед","sets":3,"reps":10}]}, {}, "1 | Присед | 3 | 10"),
    ("workout_set", "присед 40 кило на десять", {"exercise":"Присед","weight_kg":40,"reps":10}, {}, "Присед | 40.0 | 10"),
    ("course_supply", "двадцать таблеток, расход одна таблетка за приём", {"supply_amount":20,"amount_per_intake":1,"supply_unit":"таблетка"}, {}, "20.0 | 1.0 | таблетка"),
    ("course_schedule", "с 8 по 31 октября, пн и чт, в девять утра", {"start_date":"2026-10-08","end_date":"2026-10-31","weekdays":[0,3],"times":["09:00"]}, {}, "2026-10-08 | 2026-10-31 | 1,4 | 09:00"),
])
def test_typed_extraction_passes_only_normalized_data_to_existing_contract(kind,text,data,context,expected):
    client = client_for(data)
    result = asyncio.run(n.extract_answer(client, kind, [{"question":"Вопрос", "answer":text}], {"uid":123, **context}))
    assert n.canonical(kind, result.data.model_dump(), result.skip, context) == expected
    request = client.responses.parse.await_args.kwargs
    assert request["store"] is False and request["model"] == "gpt-5-mini"
    assert "tools" not in request and "conversation" not in request
    assert "uid" not in json.loads(request["input"])


@pytest.mark.parametrize("kind", list(n.ENVELOPES))
def test_every_step_has_strict_schema_without_arbitrary_properties(kind):
    schema = to_strict_json_schema(n.ENVELOPES[kind])
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"data", "clarification", "skip", "intent"}


@pytest.mark.parametrize("kind,data", [
    ("weight", {"weight":float("nan")}), ("weight", {"weight":5001}),
    ("weight", {"weight":-10}), ("program_sets", {"value":3.5}),
    ("set_reps", {"value":0}), ("target", {"kcal":float("inf")}),
    ("measurement", {"waist_cm":500}), ("reminder", {"weekday":9}),
])
def test_invalid_model_values_cannot_reach_persistence(kind,data):
    with pytest.raises(ValidationError): n.ENVELOPES[kind](data=data)


@pytest.mark.parametrize("kind,data,context", [
    ("reminder", {"clock":"25:99"}, {}),
    ("timezone", {"timezone":"Not/AZone"}, {}),
    ("program", {"exercises":[{"weekday":0,"name":"Присед"}]}, {}),
    ("target", {"kcal":2000,"protein":100}, {}),
    ("measurement", {}, {}), ("course_supply", {"supply_amount":20}, {}),
    ("course_schedule", {"start_date":"2026-10-08","end_date":"2026-10-31","weekdays":[0],"interval_days":3,"times":["09:00"]}, {}),
])
def test_missing_or_ambiguous_values_need_another_question(kind,data,context):
    parsed = n.ENVELOPES[kind](data=data)
    with pytest.raises((ValueError,KeyError)): n.canonical(kind, parsed.data.model_dump(), False, context)


def test_partial_answer_keeps_context_and_asks_one_question_before_preview(monkeypatch):
    api = AsyncMock(return_value=dashboard())
    monkeypatch.setattr(f, "api", api)
    client = client_for({"weekday":6}, "Во сколько в воскресенье присылать итоги?")
    msg, state = message("по воскресеньям"), State(form_kind="reminder", reminder_kind="weekly", form_token="test", form_prompt="Когда присылать итоги?")
    async def run():
        await f.receive(msg,state,client)
        api.assert_not_awaited()
        assert "Во сколько" in msg.answer.await_args.args[0]
        msg.text = "в семь вечера"
        client.responses.parse.return_value.output_parsed = {"data":{"weekday":6,"clock":"19:00"},"clarification":None,"skip":False}
        await f.receive(msg,state,client)
        history = json.loads(client.responses.parse.await_args.kwargs["input"])["answers"]
        assert [a["answer"] for a in history] == ["по воскресеньям","в семь вечера"]
        assert state.values["pending_input"]["payload"] == {"weekday":6,"weekly":"19:00"}
        assert all(c.args[0] == "/dashboard" for c in api.await_args_list)
    asyncio.run(run())


def test_natural_weight_is_not_saved_until_real_confirmation_and_old_button_is_rejected(monkeypatch):
    api = AsyncMock(return_value={"ok":True})
    monkeypatch.setattr(f,"api",api)
    msg,state = message("вешу семьдесят пять с половиной"),State(form_kind="weight",form_token="test")
    async def run():
        await f.receive(msg,state,client_for({"weight":75.5}))
        api.assert_not_awaited()
        token=state.values["pending_input"]["token"]
        assert "75,5" in msg.answer.await_args.args[0]
        await f.confirm_input(msg,123,state,"input_save:"+token)
        payload=api.await_args.args[1]
        assert payload["patch"] == {"current_weight_kg":75.5} and payload["expected_version"] == 2
        assert "75.5" in payload["evidence"] and payload["evidence"] in payload["source_text"]
        with pytest.raises(t.BridgeError): await f.confirm_input(msg,123,state,"input_save:"+token)
        assert api.await_count == 1
    asyncio.run(run())


def test_cancelled_step_discards_in_flight_model_result(monkeypatch):
    state=State(form_kind="measurement",form_token="old")
    msg=message("талия 80")
    client=client_for({"waist_cm":80})
    async def respond(**kwargs):
        await state.clear()
        return SimpleNamespace(status="completed",usage=None,output_parsed={"data":{"waist_cm":80}})
    client.responses.parse.side_effect=respond
    assert asyncio.run(n.parse_step(msg,state,"measurement",client)) is None
    msg.answer.assert_not_awaited()
    assert state.values == {}


def test_privacy_reset_discards_model_result_even_when_step_token_did_not_change():
    state=State(form_kind="weight",form_token="old")
    msg=message("Сейчас мой вес семьдесят пять килограммов")
    client=client_for({"weight":75})
    async def respond(**kwargs):
        t.reset_mentor_conversations(123)
        return SimpleNamespace(status="completed",usage=None,output_parsed={"data":{"weight":75}})
    client.responses.parse.side_effect=respond
    assert asyncio.run(n.parse_step(msg,state,"weight",client)) is None
    msg.answer.assert_not_awaited()


def test_plain_weight_uses_validated_numeric_path_without_ai():
    state=State(form_kind="weight",form_token="current")
    client=client_for({})
    assert asyncio.run(n.parse_step(message("75,5 кг"),state,"weight",client)) == "75.5"
    client.responses.parse.assert_not_awaited()


def test_refused_or_incomplete_provider_response_does_not_save(monkeypatch):
    api=AsyncMock()
    monkeypatch.setattr(f,"api",api)
    client=client_for({})
    client.responses.parse.return_value.status="incomplete"
    msg,state=message("мой вес"),State(form_kind="weight",form_token="old")
    asyncio.run(f.receive(msg,state,client))
    api.assert_not_awaited()
    assert "ещё раз" in msg.answer.await_args.args[0]
    assert state.values["form_kind"] == "weight"


def test_usage_is_recorded_even_for_refusal(monkeypatch):
    from src.ai.webapp_client import webapp_client
    usage=SimpleNamespace(input_tokens=100,output_tokens=20,input_tokens_details=SimpleNamespace(cached_tokens=10))
    client=client_for({})
    client.responses.parse.return_value.usage=usage
    client.responses.parse.return_value.output_parsed=None
    writes,tokens=AsyncMock(),AsyncMock()
    monkeypatch.setattr(webapp_client,"write_usage",writes)
    monkeypatch.setattr(webapp_client,"increment_tokens",tokens)
    with pytest.raises(t.BridgeError): asyncio.run(n.extract_answer(client,"weight",[],{"uid":123}))
    writes.assert_awaited_once_with(123,100,20,"new",cached_input_tokens=10)
    tokens.assert_awaited_once_with(123,100,20)


def test_input_lock_is_stable_while_held_and_serializes_calls():
    async def run():
        first=n.input_lock(123)
        assert n.input_lock(123) is first
        order=[]
        async def task(i):
            async with n.input_lock(123):
                order.append(i)
                await asyncio.sleep(0.01)
                order.append(i)
        await asyncio.gather(task(1),task(2))
        assert order == [1,1,2,2]
    asyncio.run(run())


def test_reminder_confirmation_merges_only_its_fields_into_latest_settings(monkeypatch):
    saved=dashboard()
    saved["settings"]["reminders"]={"morning":"09:00","course":True}
    api=AsyncMock(return_value=saved)
    monkeypatch.setattr(f,"api",api)
    state=State(pending_input={"token":"test","kind":"reminder","payload":{"weekly":"19:00","weekday":6}})
    asyncio.run(f.confirm_input(message(""),123,state,"input_save:test"))
    body=api.await_args_list[1].args[1]
    assert body["morning"] == "09:00" and body["course"] is True and body["weekly"] == "19:00"


def test_form_prompts_do_not_require_delimiters_or_technical_time_format(monkeypatch):
    async def run():
        msg=message("")
        for action in ("target","measurement","course","program_text"):
            state=State()
            if action == "program_text":
                from src.bot.handlers import mentor
                dialogue=AsyncMock()
                monkeypatch.setattr(mentor,"run_ai_action",dialogue)
                monkeypatch.setattr(f,"api",AsyncMock(return_value=dashboard()))
                query=SimpleNamespace(from_user=msg.from_user,message=msg,id="test")
                await f.dispatch(query,state,action,None,None,None)
                text=dialogue.await_args.args[-1]
            else: await f.start_form(msg,123,state,action)
            if action != "program_text": text=msg.answer.await_args.args[0]
            assert "|" not in text and "HH:MM" not in text and "пн=1" not in text
    asyncio.run(run())


@pytest.mark.parametrize("question", ["Укажите время HH:MM", "Введите ЧЧ:ММ", "Пришлите в формате JSON", "День | время", "Укажите IANA"])
def test_model_cannot_reintroduce_technical_format_in_clarification(question):
    rendered=n.human_question("reminder",{"weekday":6},{"reminder_kind":"weekly"},question)
    from src.ai.mentor_copy import QUESTIONS
    assert rendered == QUESTIONS["reminder_clock"]


def test_empty_photographic_answer_never_invokes_text_parser():
    client=client_for({})
    msg,state=message(""),State(form_kind="measurement",form_token="test")
    assert asyncio.run(n.parse_step(msg,state,"measurement",client)) is None
    client.responses.parse.assert_not_awaited()
    assert "талия 82 см" in msg.answer.await_args.args[0]
    assert "текстом" not in msg.answer.await_args.args[0]


def test_mutation_is_not_confirmed_when_backend_rejects_it(monkeypatch):
    monkeypatch.setattr(f,"api",AsyncMock(side_effect=t.BridgeError("Данные изменились",409)))
    state=State(pending_input={"token":"test","kind":"weight","payload":{"weight":75,"expected_version":2}})
    msg=message("")
    with pytest.raises(t.BridgeError): asyncio.run(f.confirm_input(msg,123,state,"input_save:test"))
    msg.answer.assert_not_awaited()
    assert state.values["pending_input"]["token"] == "test"
