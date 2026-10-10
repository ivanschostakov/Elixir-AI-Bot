import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai import telegram_mentor as bridge
from src.ai.mentor_input import ENVELOPES
from src.bot.handlers import mentor, mentor_onboarding as setup
from test_telegram_flows import State, dashboard
from test_mentor_regressions import message


@pytest.fixture
def backend(monkeypatch, tmp_path):
    from src.bot.handlers import mentor_access
    monkeypatch.setattr(mentor_access, "check_onboarding_access", AsyncMock(return_value=False))
    monkeypatch.setattr(bridge.config, "DATA_DIR", tmp_path)
    bridge.set_mentor_enabled(123, True)
    data = dashboard()
    data.update(profile={}, version=0)
    records = {}
    async def call(path, payload):
        assert payload["telegram_user_id"] == 123
        if path in {"/dashboard", "/profile/context"}:
            return deepcopy(data)
        if path == "/profile/update":
            assert payload["expected_version"] == data["version"]
            data["profile"].update(payload["patch"])
            data["version"] += 1
            return {"ok": True}
        if path == "/reminder/options":
            data["settings"]["timezone"] = payload["timezone"]
            return {"ok": True}
        if path == "/workspace/nutrition/eligibility":
            data["workspace"]["nutrition_eligibility_confirmed"] = payload["confirmed"]
            return {"ok": True}
        if path == "/workspace/nutrition/preview":
            assert payload["eligibility_confirmed"]
            return {"available": True, "nutrition": {"kcal": 2000, "protein": 100, "fat": 60, "carbs": 265}, "note": "Тестовый ориентир."}
        if path == "/workspace/draft":
            entry = {"id": 7, "kind": "target", "status": "draft", **payload["data"]}
            records[7] = entry
            return {"entry": deepcopy(entry)}
        if path == "/workspace/record":
            return {"entry": deepcopy(records[payload["entry_id"]])}
        if path == "/workspace/action":
            entry = records[payload["entry_id"]]
            entry["status"] = "confirmed" if payload["action"] == "confirm" else "cancelled"
            if entry["status"] == "confirmed":
                data["workspace"]["target"] = deepcopy(entry)
            return {"entry": deepcopy(entry)}
        raise AssertionError(path)
    api = AsyncMock(side_effect=call)
    monkeypatch.setattr(bridge, "api", api)
    return data, api


async def click(choice, state=None, token=None):
    msg = message("")
    query = SimpleNamespace(id="query-"+str(setup.load(123).get("token")), from_user=msg.from_user,
        data=f"mentor:setup:{token or setup.load(123)['token']}:{choice}")
    await setup.callback(query, state or State(), msg)
    return msg


def test_full_onboarding_saves_each_fact_and_requires_target_confirmation(backend, monkeypatch):
    data, api = backend
    extraction = AsyncMock(return_value=ENVELOPES["timezone"](data={"timezone": "Asia/Yerevan"}))
    monkeypatch.setattr(setup, "extract_answer", extraction)
    async def run():
        await setup.offer(message(), 123)
        await click("begin")
        assert setup.load(123)["step"] == "goal"
        await click("weight_loss")
        await setup.receive(message("35", 100), State())
        assert setup.load(123)["step"] == "sex"
        await click("male")
        await setup.receive(message("180 см", 101), State())
        await setup.receive(message("90", 102), State())
        await click("light")
        assert setup.load(123)["step"] == "preferences"
        await click("skip")
        await setup.receive(message("Ереван", 103), State())
        assert data["settings"]["timezone"] == "UTC"
        await click("confirm_timezone")
        assert data["settings"]["timezone"] == "Asia/Yerevan"
        assert setup.load(123)["step"] == "eligibility"
        assert not any(c.args[0] == "/workspace/nutrition/preview" for c in api.await_args_list)
        await click("eligible")
        assert "target" not in data["workspace"]
        assert setup.load(123)["step"] == "target"
        msg = await click("save_target")
        assert data["workspace"]["target"]["status"] == "confirmed"
        assert setup.load(123)["status"] == "complete"
        assert "Добавить еду" in str(msg.answer.await_args.kwargs["reply_markup"])
    asyncio.run(run())
    assert extraction.await_count == 1
    assert data["profile"] == {"goal": "weight_loss", "age": 35, "sex": "male", "height_cm": 180, "current_weight_kg": 90, "activity": "light"}


def test_progress_survives_new_fsm_and_does_not_reask_known_fields(backend):
    data, _ = backend
    data["profile"] = {"goal": "maintain", "age": 40, "sex": "female", "height_cm": 170}
    asyncio.run(setup.resume(message(), 123))
    assert setup.load(123)["step"] == "current_weight_kg"
    asyncio.run(setup.receive(message("70,5", 104), State()))
    setup.pause(123)
    asyncio.run(setup.resume(message(), 123))
    assert setup.load(123)["step"] == "activity"
    assert data["profile"]["age"] == 40


def test_stale_button_cannot_overwrite_new_profile(backend):
    data, api = backend
    asyncio.run(setup.resume(message(), 123))
    token = setup.load(123)["token"]
    asyncio.run(click("maintain"))
    asyncio.run(click("weight_loss", token=token))
    assert data["profile"]["goal"] == "maintain"
    assert len([c for c in api.await_args_list if c.args[0] == "/profile/update"]) == 1


def test_unknown_or_question_is_not_profile_evidence(backend, monkeypatch):
    data, api = backend
    asyncio.run(setup.resume(message(), 123))
    monkeypatch.setattr(setup, "extract_answer", AsyncMock(return_value=ENVELOPES["onboarding_profile"](
        data={}, intent="unknown", clarification="Какая цель сейчас важнее для вас?")))
    asyncio.run(setup.receive(message("не знаю"), State()))
    assert data["profile"] == {}
    assert not any(c.args[0] == "/profile/update" for c in api.await_args_list)


@pytest.mark.parametrize("step,text", [("age", "150"), ("age", "25.5"), ("height_cm", "25"), ("current_weight_kg", "0")])
def test_invalid_numeric_values_not_written(backend, step, text):
    _, api = backend
    setup.save(123, {"status": "active", "step": step, "token": "test"})
    asyncio.run(setup.receive(message(text), State()))
    assert setup.load(123)["step"] == step
    assert not any(c.args[0] == "/profile/update" for c in api.await_args_list)


def test_free_text_extracts_several_explicit_facts_without_reasking(backend, monkeypatch):
    data, _ = backend
    asyncio.run(setup.resume(message(), 123))
    monkeypatch.setattr(setup, "extract_answer", AsyncMock(return_value=ENVELOPES["onboarding_profile"](
        data={"goal": "maintain", "age": 38, "height_cm": 180, "sex": "male", "current_weight_kg": 85})))
    asyncio.run(setup.receive(message("Хочу поддерживать вес: мужчина, 38 лет, 180 см, 85 кг"), State()))
    assert data["profile"]["age"] == 38
    assert setup.load(123)["step"] == "activity"


def test_no_automatic_nutrition_when_user_is_unsure(backend):
    _, api = backend
    setup.save(123, {"status": "active", "step": "eligibility", "token": "test"})
    asyncio.run(click("ineligible"))
    assert setup.load(123)["status"] == "complete"
    assert not any(c.args[0] in {"/workspace/nutrition/preview", "/workspace/draft"} for c in api.await_args_list)


def test_resuming_pending_target_does_not_recreate_draft(backend):
    data, api = backend
    data["profile"] = {"goal": "maintain", "age": 40, "sex": "female", "height_cm": 170,
                       "current_weight_kg": 70, "activity": "light"}
    setup.save(123, {"status": "active", "step": "eligibility", "token": "test", "timezone_done": True})
    asyncio.run(click("eligible"))
    setup.pause(123)
    asyncio.run(setup.resume(message(), 123))
    assert setup.load(123)["step"] == "target"
    assert len([c for c in api.await_args_list if c.args[0] == "/workspace/draft"]) == 1


def test_cancelled_target_never_becomes_daily_norm(backend):
    data, _ = backend
    data["profile"]["activity"] = "light"
    setup.save(123, {"status": "active", "step": "eligibility", "token": "test"})
    asyncio.run(click("eligible"))
    asyncio.run(click("cancel_target"))
    assert "target" not in data["workspace"]


def test_erase_removes_onboarding_metadata(backend):
    setup.save(123, {"status": "complete", "timezone_done": True})
    setup.erase(123)
    assert setup.load(123) == {}
