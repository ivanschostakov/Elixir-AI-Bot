import asyncio
from unittest.mock import AsyncMock

import pytest

from src.ai import mentor_input as parser, telegram_mentor as bridge
from src.bot.handlers import mentor, mentor_flows as flows
from test_mentor_input import client_for
from test_mentor_navigation import actions, message, query
from test_telegram_flows import State, dashboard


@pytest.fixture
def backend(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.config, "DATA_DIR", tmp_path)
    bridge.set_mentor_enabled(123, True)
    entries = []

    async def api(path, payload):
        if path == "/dashboard":
            return dashboard()
        if path == "/workspace/draft":
            assert payload["telegram_user_id"] == 123
            assert payload["kind"] == "wellbeing"
            entry = {"id": 71, "kind": "wellbeing", "status": "draft", **payload["data"]}
            entries.append(entry)
            return {"entry": entry}
        if path == "/workspace/action":
            assert payload == {"telegram_user_id": 123, "entry_id": 71, "action": "confirm"}
            entries[-1] = {**entries[-1], "status": "confirmed"}
            return {"entry": entries[-1]}
        raise AssertionError(f"Unexpected backend call: {path}")

    call = AsyncMock(side_effect=api)
    monkeypatch.setattr(flows, "api", call)
    monkeypatch.setattr(parser, "api", call)
    monkeypatch.setattr(mentor, "run_ai_action", AsyncMock())
    return entries, call


async def dispatch(action, state):
    msg = message()
    assert await flows.dispatch(query(action, msg), state, action, None, None, None)
    return msg


def callbacks(msg):
    return set(actions(msg.answer.await_args.kwargs["reply_markup"]))


@pytest.mark.parametrize("energy", [None, 1, 2, 3, 4, 5])
def test_normal_entry_rating_energy_skip_and_confirmation_share_existing_funnel(backend, energy):
    entries, api = backend

    async def run():
        state = State(form_kind="wellbeing_note", score=1, energy_score=1,
            form_known={"score": 1, "note": "Previous note"},
            form_answers=[{"question": "Previous question", "answer": "Previous answer"}])
        first = await dispatch("wellbeing", state)
        assert {f"mentor:wellbeing:{i}" for i in range(1, 6)} <= callbacks(first)
        assert state.state == flows.Input.value
        assert state.values["form_kind"] == "wellbeing_score"
        assert state.values["form_known"] == {} and state.values["form_answers"] == []
        assert "score" not in state.values and "energy_score" not in state.values

        rating = await dispatch("wellbeing:4", state)
        assert {f"mentor:energy:{i}" for i in range(1, 6)} | {"mentor:energy:skip"} <= callbacks(rating)
        assert state.values["score"] == 4
        assert state.values["form_kind"] == "wellbeing_energy"

        note = await dispatch("energy:skip" if energy is None else f"energy:{energy}", state)
        assert "mentor:wellbeing_skip" in callbacks(note)
        assert state.values["form_kind"] == "wellbeing_note"
        assert not entries
        assert all(call.args[0] == "/dashboard" for call in api.await_args_list)

        draft = await dispatch("wellbeing_skip", state)
        assert entries == [{"id": 71, "kind": "wellbeing", "status": "draft",
            "score": 4, "energy_score": energy, "note": ""}]
        assert {"mentor:record:confirm:71", "mentor:record:cancel:71"} <= callbacks(draft)
        assert "4/5" in draft.answer.await_args.args[0]
        assert state.state is None and state.values["pending_reviews"] == entries

        await dispatch("record:confirm:71", state)
        assert entries[0]["status"] == "confirmed"
        assert state.values["pending_reviews"] == []
        mentor.run_ai_action.assert_not_awaited()

    asyncio.run(run())


def test_free_text_rating_energy_and_note_use_receive_and_the_same_draft(backend):
    entries, _ = backend

    async def run():
        state = State()
        await dispatch("wellbeing", state)
        await flows.receive(message(text="Самочувствие четыре из пяти"), state,
            client_for({"score": 4}))
        assert state.values["form_kind"] == "wellbeing_energy"
        await flows.receive(message(text="Энергия три из пяти"), state,
            client_for({"energy_score": 3}))
        assert state.values["form_kind"] == "wellbeing_note"
        await flows.receive(message(text="Хорошо спал"), state,
            client_for({"note": "Хорошо спал"}))
        assert entries == [{"id": 71, "kind": "wellbeing", "status": "draft",
            "score": 4, "energy_score": 3, "note": "Хорошо спал"}]
        assert state.values["pending_reviews"] == entries

    asyncio.run(run())


@pytest.mark.parametrize("step", ["wellbeing_score", "wellbeing_energy", "wellbeing_note"])
def test_free_text_question_returns_to_ai_without_saving_or_losing_step(backend, monkeypatch, step):
    from src.bot.handlers import new_user

    entries, api = backend
    answer = AsyncMock()
    monkeypatch.setattr(new_user, "handle_single_ai_message", answer)

    async def run():
        state = State()
        await dispatch("wellbeing", state)
        if step != "wellbeing_score":
            await dispatch("wellbeing:4", state)
        if step == "wellbeing_note":
            await dispatch("energy:skip", state)
        known = dict(state.values["form_known"])
        client = client_for({})
        client.responses.parse.return_value.output_parsed["intent"] = "question"
        msg = message(text="Как отличить усталость от плохого самочувствия?")
        bot, expert = object(), object()
        await flows.receive(msg, state, client, professor_bot=bot, expert_client=expert)
        answer.assert_awaited_once_with(msg, state, bot, client, expert, mentor_followup=True)
        assert state.values["form_kind"] == step and state.values["form_known"] == known
        assert not entries
        assert all(call.args[0] == "/dashboard" for call in api.await_args_list)

    asyncio.run(run())
