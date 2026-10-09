import asyncio
import html
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai import telegram_mentor as t
from src.bot.handlers import mentor, mentor_flows as f
from src.bot.handlers.mentor_delivery import send_mentor_reply
from src.bot.handlers.mentor_panel import MentorPanel
from test_telegram_flows import State, dashboard


@pytest.fixture(autouse=True)
def private_store(tmp_path, monkeypatch):
    monkeypatch.setattr(t.config, "DATA_DIR", tmp_path)
    t.set_mentor_enabled(123, True)


def message(text="", mid=900):
    return SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123),
        message_id=mid, text=text, photo=None, answer=AsyncMock(), edit_text=AsyncMock(),
        edit_caption=AsyncMock(), edit_reply_markup=AsyncMock(), answer_photo=AsyncMock())


def query(action, msg=None):
    return SimpleNamespace(id="callback-123", data="mentor:"+action,
        from_user=SimpleNamespace(id=123, full_name="Test"), message=msg or message(), answer=AsyncMock())


def dispatch(action, state=None, msg=None):
    msg = msg or message()
    asyncio.run(f.dispatch(query(action, msg), state or State(), action, None, None, None))
    return msg


def test_answers_use_safe_html_without_replying_to_a_synthetic_id():
    msg = message()
    msg.reply = AsyncMock(side_effect=AssertionError("must not reply to fake message"))
    text = "**Важно** <not-html> https://example.test/some_product/\n" + "&😀текст "*2000
    markup = mentor.back_keyboard()
    asyncio.run(send_mentor_reply(msg, text, markup))
    calls = msg.answer.await_args_list
    assert len(calls) > 1
    assert "<b>Важно</b>" in calls[0].args[0] and "&lt;not-html&gt;" in calls[0].args[0]
    assert "some_product" in calls[0].args[0]
    restored = "".join(html.unescape(re.sub(r"</?b>", "", c.args[0])) for c in calls)
    assert restored == text.replace("**Важно**", "Важно")
    assert all(len(c.args[0].encode("utf-16-le"))//2 <= 3900 for c in calls)
    assert all(c.kwargs["parse_mode"] == "HTML" and c.kwargs["protect_content"] for c in calls)
    assert all(c.kwargs["reply_markup"] is None for c in calls[:-1])
    assert calls[-1].kwargs["reply_markup"] is markup
    msg.reply.assert_not_awaited()


def test_navigation_from_an_ai_answer_never_overwrites_that_answer():
    async def run():
        old, new = message(mid=12), message(mid=13)
        old.answer.return_value = new
        state = State(mentor_panel={"message_id":4, "chat_id":123})
        panel = MentorPanel(old, state)
        await panel.answer("Новое меню")
        await panel.flush()
        old.edit_text.assert_not_awaited()
        old.answer.assert_awaited_once()
        assert old.answer.await_args.args[0] == "Новое меню"
        new.edit_text.assert_not_awaited()
        assert state.values["mentor_panel"]["message_id"] == 13
    asyncio.run(run())


@pytest.mark.parametrize("text,expected", [("100",100), ("75,5",75.5), ("75.5 кг",75.5)])
def test_weight_requires_review_and_confirmation_is_specific(monkeypatch, text, expected):
    api = AsyncMock(return_value={"ok":True})
    monkeypatch.setattr(f, "api", api)
    monkeypatch.setattr(f, "parse_step", AsyncMock(return_value=str(expected)))
    msg, state = message(text), State()
    state.values["form_profile_version"] = 2
    asyncio.run(mentor.save_weight(msg, state))
    api.assert_not_awaited()
    token = state.values["pending_input"]["token"]
    asyncio.run(f.confirm_input(msg, 123, state, "input_save:"+token))
    payload = api.await_args.args[1]
    assert payload["patch"] == {"current_weight_kg":expected}
    assert text in payload["source_text"] and payload["expected_version"] == 2
    assert "кг записан" in msg.answer.await_args.args[0]
    assert any(b.callback_data == "mentor:history" for r in msg.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in r)


@pytest.mark.parametrize("text", ["не знаю", "5000", "nan", "-12"])
def test_bad_weight_keeps_the_current_step(monkeypatch, text):
    from src.ai import mentor_input as inputs
    api = AsyncMock()
    monkeypatch.setattr(f, "api", api)
    monkeypatch.setattr(inputs, "api", AsyncMock(return_value=dashboard()))
    client = SimpleNamespace(responses=SimpleNamespace(parse=AsyncMock(return_value=SimpleNamespace(
        status="completed", output_parsed={"data": {"weight": None}, "clarification": "Сколько вы сейчас весите?", "skip": False}))))
    state, msg = State(), message(text)
    state.state = mentor.WeightInput.value
    asyncio.run(mentor.save_weight(msg, state, client))
    api.assert_not_awaited()
    assert state.state == mentor.WeightInput.value
    assert "Сколько вы сейчас весите" in msg.answer.await_args.args[0]


def test_target_preflight_happens_before_eligibility_and_activity(monkeypatch):
    data = dashboard()
    data["profile"] = {"age":19}
    api = AsyncMock(return_value=data)
    monkeypatch.setattr(f, "api", api)
    msg = dispatch("target_auto")
    text = msg.answer.await_args.args[0]
    assert text.startswith("Какого результата хотите достичь:")
    assert "снизить вес" in text and "ориентир питания" in text
    assert text.count("?") == 1
    buttons = [b.callback_data for r in msg.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in r]
    assert "mentor:sex:male" in buttons and "mentor:target_activity" not in buttons
    assert api.await_count == 1


def test_guardrail_refusal_does_not_suggest_filling_an_already_complete_profile(monkeypatch):
    data = dashboard()
    data["workspace"]["nutrition_eligibility_confirmed"] = True
    monkeypatch.setattr(f, "api", AsyncMock(side_effect=[data, {}, {"available":False, "reason":"Вес вне диапазона."}]))
    msg = dispatch("target_activity:moderate")
    assert msg.answer.await_args.args[0] == "Вес вне диапазона."
    actions = [b.callback_data for r in msg.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in r]
    assert "mentor:data" not in actions and "mentor:target_manual" in actions


def test_ai_target_uses_the_same_server_preview_and_returns_an_unconfirmed_draft(monkeypatch):
    call = AsyncMock(side_effect=[{"available":True, "nutrition":{"kcal":2200,"protein":110,"fat":70,"carbs":280}}, {"entry":{"id":71,"status":"draft"}}])
    monkeypatch.setattr(t, "api", call)
    context = {"saved":{**dashboard(), "workspace":{"nutrition_eligibility_confirmed":True}}, "telegram_user_id":123, "request_key":"test-question"}
    asyncio.run(t.execute_tool(context, "preview_mentor_nutrition", {}))
    assert call.await_args_list[0].args[0] == "/workspace/nutrition/preview"
    assert call.await_args_list[0].args[1]["eligibility_confirmed"] is True
    assert call.await_args_list[1].args[0] == "/workspace/draft"
    assert context["target_draft"]["status"] == "draft"
    actions = [b.callback_data for r in mentor.response_keyboard(context).inline_keyboard for b in r]
    assert "mentor:record:confirm:71" in actions


def test_program_day_uses_conversation_and_rejects_stale_repetition_button(monkeypatch):
    from src.bot.handlers import mentor
    dialogue=AsyncMock()
    monkeypatch.setattr(mentor, "run_ai_action", dialogue)
    monkeypatch.setattr(f, "api", AsyncMock(return_value=dashboard()))
    monkeypatch.setattr(f, "parse_step", AsyncMock(return_value="Присед"))
    async def run():
        state, msg = State(program_exercises=[]), message("Присед")
        await f.dispatch(query("program_day:0",msg),state,"program_day:0",None,None,None)
        assert "Понедельник" in dialogue.await_args.args[-1]
        assert "Сохрани остальные дни" in dialogue.await_args.args[-1]
        assert not state.values.get("form_kind")
        with pytest.raises(t.BridgeError):
            await f.dispatch(query("program_reps:10",msg),state,"program_reps:10",None,None,None)
        assert state.values["program_exercises"] == []
    asyncio.run(run())


def test_empty_workout_is_not_automatically_started(monkeypatch):
    from src.bot.handlers import mentor
    dialogue=AsyncMock()
    monkeypatch.setattr(mentor, "run_ai_action", dialogue)
    api = AsyncMock(return_value=dashboard())
    monkeypatch.setattr(f, "api", api)
    msg = dispatch("workout_start")
    assert api.await_count == 1
    assert "Не считай план выполненным" in dialogue.await_args.args[-1]
    assert "не запускай таймер" in dialogue.await_args.args[-1]


def test_energy_buttons_and_optional_note(monkeypatch):
    api = AsyncMock(return_value=dashboard())
    monkeypatch.setattr(f,"api",api)
    state = State()
    msg = dispatch("wellbeing:4",state)
    actions = [b.callback_data for r in msg.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in r]
    assert {"mentor:energy:1","mentor:energy:5","mentor:energy:skip"} <= set(actions)
    dispatch("energy:3",state)
    assert state.values["energy_score"] == 3 and state.values["form_kind"] == "wellbeing_note"
    assert "4/5" in f.draft_text({"kind":"wellbeing","score":4,"energy_score":3})


@pytest.mark.parametrize("action,section", [("energy:3","today"),("program_sets:3","workouts"),("reminder_clock:08:00","settings"),("sex:male","profile"),("target_ineligible","food")])
def test_new_callbacks_respect_disabled_sections(monkeypatch, action, section):
    data = dashboard()
    data["workspace"]["sections"][section] = False
    api = AsyncMock(return_value=data)
    monkeypatch.setattr(f,"api",api)
    msg=dispatch(action)
    assert msg.answer.await_args.args[0] == "Раздел временно отключён."
    assert api.await_count == 1


def test_erase_needs_a_current_confirmation_token(monkeypatch):
    api = AsyncMock(return_value=dashboard())
    monkeypatch.setattr(f,"api",api)
    with pytest.raises(t.BridgeError):
        dispatch("privacy_erase_confirm:old",State(erase_token="new"))
    assert api.await_count == 1


def test_profile_gain_goal_and_missing_metric_formatting():
    assert "Пол: не указан" in mentor.profile_text({})
    assert "100.0" not in mentor.profile_text({"current_weight_kg":100.0})
    labels=[b.text for r in mentor.section_keyboard("ask",{"goal":"weight_gain"}).inline_keyboard for b in r]
    assert "⚖️ Почему вес не растёт?" in labels
    assert "нет данных см" not in f.draft_text({"kind":"measurement","waist_cm":80})
    assert "нет данных кг" not in f.home_view({**dashboard(),"profile":{}})


def test_reset_during_an_ai_turn_does_not_restore_the_old_conversation(monkeypatch):
    monkeypatch.setattr(t,"api",AsyncMock(return_value=dashboard()))
    async def send(**kwargs):
        t.reset_mentor_conversations(123)
        return {"conversation_id":"old-in-flight-conversation","text":"old answer"}
    with pytest.raises(t.BridgeError):
        asyncio.run(t.TelegramAIClient(SimpleNamespace(send_message_v2=send),123,"new").send_message_v2(input_text="test"))
    assert t.mentor_conversation(123,"new") is None


@pytest.mark.parametrize("kind", ["photo","voice","video_note"])
def test_meal_media_reaches_existing_transport(monkeypatch,kind):
    from src.bot.handlers import ai_helpers as h
    msg = message()
    for field in ("photo","voice","video_note","video","document","caption"):
        setattr(msg,field,None)
    media=SimpleNamespace(file_id="test-file",mime_type="audio/ogg")
    setattr(msg,kind,[media] if kind=="photo" else media)
    monkeypatch.setattr(h,"_download_telegram_file_bytes",AsyncMock(return_value=b"test-only-media"))
    client=SimpleNamespace(transcribe_audio_bytes=AsyncMock(return_value="гречка 150 граммов"))
    text,files,images,video=asyncio.run(h.collect_message_payload(msg,client))
    if kind=="photo":
        assert images == [("photo_900.jpg",b"test-only-media")]
        client.transcribe_audio_bytes.assert_not_awaited()
    else:
        assert "гречка 150" in text
        client.transcribe_audio_bytes.assert_awaited_once()
        assert video == (kind=="video_note")


def test_food_search_history_and_favorites_are_owner_scoped(monkeypatch):
    call=AsyncMock(return_value={"items":[],"has_more":False})
    monkeypatch.setattr(f,"api",call)
    msg,state=message(),State()
    asyncio.run(f.library(msg,123,state,query="суп",favorites=True))
    assert call.await_args.args[1]["telegram_user_id"] == 123
    assert call.await_args.args[1]["query"] == "суп" and call.await_args.args[1]["favorites_only"]
    assert "избранн" in msg.answer.await_args.args[0].lower()


def test_progress_photo_is_a_draft_until_confirmed(monkeypatch):
    call=AsyncMock(return_value={"entry":{"id":44,"kind":"measurement","photo_file_id":"private-photo","status":"draft"}})
    monkeypatch.setattr(f,"api",call)
    msg=message()
    msg.photo=[SimpleNamespace(file_id="private-photo")]
    msg.caption="До начала"
    asyncio.run(f.receive_value(msg,State(form_kind="progress_photo")))
    assert call.await_args.args[0] == "/workspace/draft"
    assert call.await_args.args[1]["data"]["photo_file_id"] == "private-photo"
    assert "Сохранить" in str(msg.answer.await_args.kwargs["reply_markup"])


def test_opening_course_and_leaving_mentor_do_not_start_a_scheme(monkeypatch):
    call=AsyncMock(return_value=dashboard())
    monkeypatch.setattr(f,"api",call)
    msg=dispatch("course")
    assert "ещё не сохранена" in msg.answer.await_args.args[0]
    assert call.await_count == 1
    monkeypatch.setattr(mentor,"api",call)
    msg=message()
    asyncio.run(mentor.perform_action(query("leave",msg),State(),msg))
    assert not t.mentor_enabled(123)
    assert call.await_args.args[0] == "/workspace/workout/discard-empty"
    assert "ИИ Ассистенты" in str(msg.answer.await_args.kwargs["reply_markup"])


def test_real_bot_response_method_uses_html_and_draft_buttons():
    import ast
    import logging
    from pathlib import Path
    import src.bot
    # Load the actual class/helpers, excluding module-level bot/client startup.
    path=Path(src.bot.__file__).with_name("main.py")
    tree=ast.parse(path.read_text())
    boundary=next(i for i,node in enumerate(tree.body) if isinstance(node,ast.Assign)
        and any(isinstance(target,ast.Name) and target.id=="expert_bot" for target in node.targets))
    namespace={"__name__":"mentor_transport_test"}
    exec(compile(ast.Module(body=tree.body[:boundary],type_ignores=[]),str(path),"exec"),namespace)
    bot=SimpleNamespace(_ProfessorBot__logger=logging.getLogger("test-mentor"))
    msg=message()
    msg.chat.type="private"
    msg.reply=AsyncMock(side_effect=AssertionError("Synthetic reply target must not be used"))
    asyncio.run(namespace["ProfessorBot"].parse_response(bot,
        {"mentor":True,"text":"**Норма** https://example.test/some_product/","target_draft":{"id":77}},msg,back_menu=True))
    assert "<b>Норма</b>" in msg.answer.await_args.args[0]
    assert msg.answer.await_args.kwargs["parse_mode"] == "HTML"
    assert "record:confirm:77" in str(msg.answer.await_args.kwargs["reply_markup"])
    msg.reply.assert_not_awaited()


def test_callback_timeout_keeps_a_retry_button(monkeypatch):
    from datetime import datetime, timezone
    from aiogram import Bot
    from aiogram.methods import SendMessage, EditMessageText, DeleteMessage
    from aiogram.types import Message, Chat, User, CallbackQuery
    from src.ai import helpers
    from src.bot.handlers import new_user
    monkeypatch.setattr(helpers,"check_blocked",AsyncMock(return_value=True))
    monkeypatch.setattr(helpers,"CHAT_NOT_BANNED_FILTER",AsyncMock(return_value=True))
    monkeypatch.setattr(new_user,"handle_single_ai_message",AsyncMock(return_value=None))
    async def run():
        bot=Bot("111:testtoken")
        calls=[]
        async def request(_bot,method,**kwargs):
            calls.append(method)
            if isinstance(method,SendMessage):
                return Message(message_id=21,date=datetime.now(timezone.utc),chat=Chat(id=123,type="private"),text=method.text).as_(bot)
            return True
        monkeypatch.setattr(bot.session,"make_request",request)
        msg=Message(message_id=20,date=datetime.now(timezone.utc),chat=Chat(id=123,type="private"),text="menu").as_(bot)
        q=CallbackQuery(id="retry-test",chat_instance="test",message=msg,data="mentor:suggest",from_user=User(id=123,is_bot=False,first_name="Test")).as_(bot)
        await mentor.run_ai_action(q,State(),None,None,None,"Что поесть?")
        edit=next(c for c in calls if isinstance(c,EditMessageText))
        assert "mentor:suggest" in str(edit.reply_markup)
        assert not any(isinstance(c,DeleteMessage) for c in calls)
        await bot.session.close()
    asyncio.run(run())
