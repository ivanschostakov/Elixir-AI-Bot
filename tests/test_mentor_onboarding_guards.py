import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai.mentor_input import ENVELOPES
from src.ai.webapp_client import WebappBotApiError
from src.bot.handlers import mentor_access, mentor_onboarding as setup
from test_mentor_navigation import message
from test_mentor_onboarding import backend
from test_telegram_flows import State


@pytest.fixture
def guarded_backend(backend, monkeypatch):
    data, api = backend
    data["workspace"]["sections"].update(profile=True, settings=True, food=True)
    monkeypatch.setenv("TELEGRAM_MENTOR_CLOSED_SECTIONS", "")
    monkeypatch.setattr(mentor_access, "check_onboarding_access", AsyncMock(return_value=False))
    return data, api


def callback(choice, msg):
    return SimpleNamespace(id="guard-" + choice, from_user=msg.from_user, message=msg,
        data=f"mentor:setup:{setup.load(123)['token']}:{choice}", answer=AsyncMock())


@pytest.mark.parametrize("failure", [WebappBotApiError("unavailable"), setup.bridge.BridgeError("unavailable")])
def test_access_failure_preserves_step_and_returns_retry(guarded_backend, monkeypatch, failure):
    async def run():
        setup.save(123, {"status": "active", "step": "age", "token": "retry-token"})
        before = setup.load(123)
        monkeypatch.setattr(mentor_access, "check_onboarding_access", AsyncMock(side_effect=failure))
        msg = message(mid=20, text="35")
        assert await setup.receive(msg, State()) is False
        assert setup.load(123) == before
        msg.answer.assert_awaited_once()
        assert "повторите ответ" in msg.answer.await_args.args[0]
    asyncio.run(run())


@pytest.mark.parametrize("step,choices", [
    ("target", {"save_target", "cancel_target"}),
    ("timezone_confirm", {"confirm_timezone", "change_timezone"}),
    ("eligibility", {"eligible", "ineligible"}),
])
def test_typed_yes_repeats_actionable_confirmation_without_writing(guarded_backend, step, choices):
    data, api = guarded_backend

    async def run():
        value = {"status": "active", "step": step, "token": "guard-token", "timezone": "Asia/Yerevan"}
        if step == "target":
            result = await api("/workspace/draft", {"telegram_user_id": 123, "kind": "target",
                "data": {"kcal": 2000, "protein": 100, "fat": 60, "carbs": 265}})
            value["draft_id"] = result["entry"]["id"]
        setup.save(123, value)
        before = deepcopy(data)
        api.reset_mock()
        state = State(mentor_panel={"message_id": 10, "chat_id": 123, "kind": "form"})
        msg = message(mid=20, text="\u0434\u0430")
        await setup.receive(msg, state)

        msg.answer.assert_awaited_once()
        buttons = msg.answer.await_args.kwargs.get("reply_markup")
        assert buttons is not None
        callbacks = {b.callback_data for row in buttons.inline_keyboard for b in row}
        current = setup.load(123)
        assert {f"mentor:setup:{current['token']}:{choice}" for choice in choices | {"later"}} <= callbacks
        assert current["status"] == "active" and current["step"] == step
        assert state.values["mentor_panel"]["message_id"] == msg.sent.message_id
        assert data == before
        assert {call.args[0] for call in api.await_args_list} <= {"/dashboard", "/workspace/record"}
        if step == "target":
            result = await api("/workspace/record", {"telegram_user_id": 123, "entry_id": current["draft_id"]})
            assert result["entry"]["status"] == "draft"
    asyncio.run(run())


@pytest.mark.parametrize("route", ["typed", "callback"])
def test_disabled_profile_section_blocks_profile_update(guarded_backend, route):
    data, api = guarded_backend
    data["workspace"]["sections"]["profile"] = False
    data["profile"] = {"preferences": "Vegetarian"}
    before = deepcopy(data["profile"])
    setup.save(123, {"status": "active", "step": "age" if route == "typed" else "goal", "token": "guard-token"})

    async def run():
        msg = message(mid=20, text="35")
        if route == "typed":
            await setup.receive(msg, State())
        else:
            await setup.callback(callback("maintain", msg), State(), msg)
        assert data["profile"] == before
        assert not any(call.args[0] == "/profile/update" for call in api.await_args_list)
        assert setup.load(123)["status"] == "paused"
        msg.answer.assert_awaited_once()
        assert msg.answer.await_args.kwargs.get("reply_markup") is not None
    asyncio.run(run())


@pytest.mark.parametrize("choice", ["confirm_timezone", "change_timezone"])
def test_pause_during_timezone_dashboard_read_does_not_write_or_resume(guarded_backend, choice):
    data, api = guarded_backend
    data["profile"] = {"goal": "maintain", "age": 35, "sex": "male", "height_cm": 180,
        "current_weight_kg": 80, "activity": "light", "preferences": "Vegetarian"}
    setup.save(123, {"status": "active", "step": "timezone_confirm", "token": "guard-token",
        "timezone": "Asia/Yerevan", "preferences_done": True})

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        original = api.side_effect
        dashboard_reads = 0

        async def blocked_read(path, payload):
            nonlocal dashboard_reads
            result = await original(path, payload)
            if path == "/dashboard":
                dashboard_reads += 1
                # The first read is the section guard; hold the action's own read.
                if dashboard_reads == 2:
                    started.set()
                    await release.wait()
            return result

        api.side_effect = blocked_read
        msg = message(mid=20)
        task = asyncio.create_task(setup.callback(callback(choice, msg), State(), msg))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            setup.pause(123)
            paused = setup.load(123)
            release.set()
            await asyncio.wait_for(task, timeout=2)
            assert setup.load(123) == paused
            assert paused["status"] == "paused"
            assert data["settings"]["timezone"] == "UTC"
            assert not any(call.args[0] in {"/reminder/options", "/profile/update",
                "/workspace/nutrition/eligibility", "/workspace/draft"} for call in api.await_args_list)
            msg.answer.assert_not_awaited()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_partial_extraction_with_clarification_preserves_explicit_facts(guarded_backend, monkeypatch):
    data, _ = guarded_backend
    data["profile"] = {"preferences": "Vegetarian"}
    setup.save(123, {"status": "active", "step": "goal", "token": "guard-token"})
    extraction = AsyncMock(side_effect=[
        ENVELOPES["onboarding_profile"](data={"age": 35, "sex": "male"},
            clarification="Which goal matters most to you?"),
        ENVELOPES["onboarding_profile"](data={"goal": "maintain"}),
    ])
    monkeypatch.setattr(setup, "extract_answer", extraction)

    async def run():
        state = State()
        await setup.receive(message(mid=20, text="I am a man, 35 years old. I am still choosing a goal."), state)
        assert data["profile"]["age"] == 35
        assert data["profile"]["sex"] == "male"
        assert setup.load(123)["step"] == "goal"
        await setup.receive(message(mid=22, text="Maintain my current weight."), state)
        extraction.assert_awaited()
        assert extraction.await_count == 2
        context = extraction.await_args_list[1].args[3]
        known = {**context.get("profile", {}), **context.get("known", {})}
        assert known["age"] == 35 and known["sex"] == "male"
        assert data["profile"] == {"preferences": "Vegetarian", "age": 35, "sex": "male", "goal": "maintain"}
        assert setup.load(123)["step"] == "height_cm"
    asyncio.run(run())


def test_extraction_continues_with_the_actual_clarification_and_answer_history(guarded_backend, monkeypatch):
    _, api = guarded_backend
    setup.save(123, {"status": "active", "step": "goal", "token": "guard-token"})
    question = "Do you want to maintain your current weight or change it?"
    first_answer, second_answer = "I want to feel better.", "Maintain my current weight."
    extraction = AsyncMock(side_effect=[
        ENVELOPES["onboarding_profile"](data={}, clarification=question),
        ENVELOPES["onboarding_profile"](data={"goal": "maintain"}),
    ])
    monkeypatch.setattr(setup, "extract_answer", extraction)

    async def run():
        state, first = State(), message(mid=20, text=first_answer)
        await setup.receive(first, state)
        assert first.answer.await_args.args[0] == question
        assert not any(call.args[0] == "/profile/update" for call in api.await_args_list)
        await setup.receive(message(mid=22, text=second_answer), state)
        assert extraction.await_count == 2
        assert extraction.await_args_list[0].args[2] == [
            {"question": setup.PROMPTS["goal"], "answer": first_answer}]
        assert extraction.await_args_list[1].args[2] == [
            {"question": setup.PROMPTS["goal"], "answer": first_answer},
            {"question": question, "answer": second_answer},
        ]
    asyncio.run(run())
