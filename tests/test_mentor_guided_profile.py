import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

from src.ai.mentor_input import ENVELOPES
from src.bot.handlers import mentor_onboarding as setup
from test_mentor_onboarding import backend, click
from test_mentor_regressions import message
from test_telegram_flows import State


PROFILE = dict(goal="maintain", age=40, sex="female", height_cm=170,
               current_weight_kg=70, activity="light", preferences="Обычное питание")


def ready(data):
    data["profile"] = dict(PROFILE)
    setup.save(123, {"status": "active", "token": "ready", "timezone_done": True})


def test_progress_counts_only_missing_questions_and_skips_multi_field_answer(backend, monkeypatch):
    data, _ = backend
    data["profile"] = {"goal": "maintain", "age": 40, "sex": "female"}
    monkeypatch.setattr(setup, "extract_answer", AsyncMock(return_value=ENVELOPES["onboarding_profile"](
        data={"height_cm": 170, "current_weight_kg": 70, "activity": "light"})))
    async def run():
        first = message()
        await setup.resume(first, 123)
        assert "Шаг 1 из 6" in first.answer.await_args.args[0]
        await setup.receive(message("170 см, 70 кг, лёгкая активность", 301), State())
        value = setup.load(123)
        assert value["step"] == "preferences"
        assert value["route"] == ["height_cm", "preferences", "timezone", "review"]
    asyncio.run(run())


def test_review_is_required_before_nutrition_calculation(backend):
    data, api = backend
    ready(data)
    async def run():
        await setup.resume(message(), 123)
        assert setup.load(123)["step"] == "review"
        await click("eligible")
        assert not any(c.args[0] == "/workspace/nutrition/preview" for c in api.await_args_list)
        await click("review_continue")
        assert setup.load(123)["step"] == "eligibility"
        await click("eligible")
        assert setup.load(123)["step"] == "target"
        await click("save_target")
        assert data["workspace"]["target"]["status"] == "confirmed"
    asyncio.run(run())


def test_back_edits_saved_value_without_erasing_other_fields(backend):
    data, _ = backend
    data["profile"] = {"goal": "maintain", "age": 40, "sex": "female"}
    async def run():
        await setup.resume(message(), 123)
        await setup.receive(message("170", 302), State())
        previous = await click("back")
        assert setup.load(123)["step"] == "height_cm"
        assert "Сейчас сохранено: 170 см" in previous.answer.await_args.args[0]
        await setup.receive(message("171", 303), State())
        assert setup.load(123)["step"] == "current_weight_kg"
        assert data["profile"] == {"goal": "maintain", "age": 40, "sex": "female", "height_cm": 171}
    asyncio.run(run())


def test_summary_edit_and_keep_existing_norm(backend):
    data, api = backend
    ready(data)
    old = {"id": 80, "status": "confirmed", "kcal": 1900}
    data["workspace"]["target"] = deepcopy(old)
    async def run():
        await setup.review(message(), 123)
        await click("edit_age")
        await setup.receive(message("41", 304), State())
        assert setup.load(123)["step"] == "review"
        assert data["profile"]["age"] == 41
        await click("review_continue")
        assert setup.load(123)["status"] == "complete"
        assert data["workspace"]["target"] == old
        assert not any(c.args[0] == "/workspace/nutrition/preview" for c in api.await_args_list)
    asyncio.run(run())


def test_recalculate_cancel_preserves_old_norm(backend):
    data, _ = backend
    ready(data)
    old = {"id": 80, "status": "confirmed", "kcal": 1900}
    data["workspace"]["target"] = deepcopy(old)
    async def run():
        await setup.review(message(), 123)
        await click("recalculate")
        await click("eligible")
        assert data["workspace"]["target"] == old
        await click("cancel_target")
        assert data["workspace"]["target"] == old
    asyncio.run(run())


def test_profile_changed_after_calculation_cannot_confirm_stale_norm(backend):
    data, api = backend
    ready(data)
    async def run():
        setup.save(123, {**setup.load(123), "step": "eligibility"})
        await click("eligible")
        data["profile"]["current_weight_kg"] = 90
        await click("save_target")
        assert setup.load(123)["step"] == "review"
        assert not data["workspace"].get("target")
        actions = [c.args[1]["action"] for c in api.await_args_list if c.args[0] == "/workspace/action"]
        assert actions == ["cancel"]
    asyncio.run(run())


def test_back_from_draft_cancels_only_unconfirmed_draft(backend):
    data, api = backend
    ready(data)
    async def run():
        setup.save(123, {**setup.load(123), "step": "eligibility"})
        await click("eligible")
        old_token = setup.load(123)["token"]
        await click("back")
        await click("save_target", token=old_token)
        assert setup.load(123)["step"] == "review"
        assert not data["workspace"].get("target")
        assert [c.args[1]["action"] for c in api.await_args_list if c.args[0] == "/workspace/action"] == ["cancel"]
    asyncio.run(run())


def test_clarification_survives_pause_and_fresh_fsm(backend, monkeypatch):
    data, _ = backend
    extractor = AsyncMock(side_effect=[
        ENVELOPES["onboarding_profile"](data={}, clarification="Вы хотите сохранить вес или изменить его?"),
        ENVELOPES["onboarding_profile"](data={"goal": "maintain"}),
    ])
    monkeypatch.setattr(setup, "extract_answer", extractor)
    async def run():
        await setup.resume(message(), 123)
        await setup.receive(message("Хочу лучше себя чувствовать", 305), State())
        setup.pause(123)
        resumed = message()
        await setup.resume(resumed, 123)
        assert "сохранить вес или изменить" in resumed.answer.await_args.args[0]
        await setup.receive(message("Сохранить", 306), State())
        assert len(extractor.await_args.args[2]) == 2
        assert extractor.await_args.args[2][-1]["question"] == "Вы хотите сохранить вес или изменить его?"
        assert data["profile"]["goal"] == "maintain"
    asyncio.run(run())


def test_ambiguous_requested_value_not_saved(backend, monkeypatch):
    data, _ = backend
    data["profile"] = {"goal": "maintain"}
    monkeypatch.setattr(setup, "extract_answer", AsyncMock(return_value=ENVELOPES["onboarding_profile"](
        data={"age": 40, "height_cm": 170}, clarification="Сколько вам полных лет сейчас?")))
    async def run():
        await setup.resume(message(), 123)
        await setup.receive(message("Мне скоро сорок, рост 170", 307), State())
        assert "age" not in data["profile"]
        assert data["profile"]["height_cm"] == 170
        assert setup.load(123)["step"] == "age"
    asyncio.run(run())


def test_duplicate_delivery_does_not_become_answer_to_next_question(backend):
    data, api = backend
    data["profile"] = {"goal": "maintain", "age": 40, "sex": "female"}
    async def run():
        await setup.resume(message(), 123)
        await setup.receive(message("170", 308), State())
        await setup.receive(message("170", 308), State())
        assert setup.load(123)["step"] == "current_weight_kg"
        assert "current_weight_kg" not in data["profile"]
        assert len([c for c in api.await_args_list if c.args[0] == "/profile/update"]) == 1
    asyncio.run(run())


def test_timezone_edit_requires_confirmation_and_preserves_other_settings(backend, monkeypatch):
    data, _ = backend
    ready(data)
    before = deepcopy(data["settings"])
    monkeypatch.setattr(setup, "extract_answer", AsyncMock(return_value=ENVELOPES["timezone"](data={"timezone": "Asia/Yerevan"})))
    async def run():
        await setup.review(message(), 123)
        await click("edit_timezone")
        await setup.receive(message("Ереван", 309), State())
        assert data["settings"] == before
        assert setup.load(123)["step"] == "timezone_confirm"
        await click("confirm_timezone")
        assert setup.load(123)["step"] == "review"
        assert data["settings"] == {**before, "timezone": "Asia/Yerevan"}
    asyncio.run(run())


def test_voice_answer_uses_same_validated_path(backend, monkeypatch):
    from src.bot.handlers import mentor_messages
    data, _ = backend
    data["profile"] = {"goal": "maintain", "age": 40, "sex": "female"}
    spoken = AsyncMock(return_value=message("170 см", 310))
    monkeypatch.setattr(mentor_messages, "spoken_message", spoken)
    async def run():
        await setup.resume(message(), 123)
        voice = message("", 310)
        voice.voice = object()
        await setup.receive(voice, State())
        spoken.assert_awaited_once()
        assert data["profile"]["height_cm"] == 170
        assert setup.load(123)["step"] == "current_weight_kg"
    asyncio.run(run())


def test_duplicate_concurrent_button_is_idempotent(backend):
    data, api = backend
    async def run():
        await setup.resume(message(), 123)
        token = setup.load(123)["token"]
        await asyncio.gather(click("maintain", token=token), click("weight_loss", token=token))
        assert data["profile"]["goal"] == "maintain"
        assert len([c for c in api.await_args_list if c.args[0] == "/profile/update"]) == 1
    asyncio.run(run())
