import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.ai import telegram_mentor as transport
from src.bot.handlers import mentor_food as food


MEAL = {"name": "Творог 5%, 200 г", "kcal": 242, "protein": 34, "fat": 10, "carbs": 6}
ESTIMATE = {"id": 12, "kind": "food_estimate", "status": "preview", **MEAL}
ENTRY = {"id": 20, "kind": "meal", "status": "confirmed", **MEAL}


class State:
    def __init__(self):
        self.data = {}

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)


def query(action, uid=123, chat_id=123, chat_type="private"):
    message = SimpleNamespace(text="Творог: примерно 242 ккал, Б 34 / Ж 10 / У 6.",
        caption=None, chat=SimpleNamespace(id=chat_id, type=chat_type), message_id=7,
        edit_text=AsyncMock(), edit_caption=AsyncMock(), edit_reply_markup=AsyncMock())
    message.answer = AsyncMock(return_value=SimpleNamespace(
        message_id=8, chat=message.chat, photo=None))
    message.reply_markup = InlineKeyboardMarkup(inline_keyboard=[
        *food.food_response_rows({"food_estimate": ESTIMATE}),
        [InlineKeyboardButton(text="Меню", callback_data="mentor:menu")]])
    return SimpleNamespace(message=message, from_user=SimpleNamespace(id=uid),
        answer=AsyncMock(), data="mentor:" + action)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(transport.config, "DATA_DIR", tmp_path)


@pytest.mark.parametrize("use_context_var", [False, True])
def test_lookup_never_creates_meal_draft_or_replaces_consumed_meal(monkeypatch, use_context_var):
    api = AsyncMock(return_value={"estimate": ESTIMATE, "recorded": False})
    monkeypatch.setattr(transport, "api", api)
    context = {"telegram_user_id": 123, "request_key": "real-turn", "saved": {"version": 2}}
    if not use_context_var:
        context["food_lookup_mode"] = True

    async def run():
        token = transport.food_lookup_mode.set(use_context_var)
        try:
            result = await transport.execute_tool(context, "draft_mentor_meal", {
                "meal": MEAL, "replaces_id": 999, "occurred_at": "2026-10-14T00:00:00Z",
                "telegram_user_id": 888, "request_key": "spoofed", "status": "confirmed"})
        finally:
            transport.food_lookup_mode.reset(token)
        assert result["recorded"] is False
    asyncio.run(run())
    api.assert_awaited_once_with("/workspace/food/estimate", {
        "telegram_user_id": 123, "request_key": "real-turn", "meal": MEAL, "expected_version": 2})
    assert context["food_estimate"] == ESTIMATE
    assert "meal_draft" not in context and "replaced_review_ids" not in context


def test_lookup_backend_error_has_no_diary_fallback(monkeypatch):
    api = AsyncMock(side_effect=transport.BridgeError("unavailable"))
    monkeypatch.setattr(transport, "api", api)
    with pytest.raises(transport.BridgeError):
        asyncio.run(transport.execute_tool({"telegram_user_id": 123, "request_key": "turn",
            "food_lookup_mode": True}, "draft_mentor_meal", {"meal": MEAL}))
    assert api.await_count == 1 and api.await_args.args[0] == "/workspace/food/estimate"


def test_consumed_meal_still_requires_journal_confirmation(monkeypatch):
    draft = {**ENTRY, "status": "draft"}
    api = AsyncMock(return_value={"entry": draft})
    monkeypatch.setattr(transport, "api", api)
    context = {"telegram_user_id": 123, "request_key": "turn"}
    asyncio.run(transport.execute_tool(context, "draft_mentor_meal", {"meal": MEAL}))
    assert api.await_args.args[0] == "/journal/draft"
    assert context["meal_draft"] == draft and "food_estimate" not in context


def test_wrapper_keeps_lookup_through_portion_clarification_and_returns_preview(monkeypatch):
    async def api(path, payload):
        return {"estimate": ESTIMATE, "recorded": False} if path.endswith("/food/estimate") else {"version": 1}
    monkeypatch.setattr(transport, "api", AsyncMock(side_effect=api))
    turns = []

    async def send(**kwargs):
        context = transport.catalog_context.get()
        turns.append(context["food_lookup_mode"])
        assert "справочная оценка" in transport.memory_instructions(context)
        if len(turns) == 1:
            return {"text": "Какая порция?"}
        await transport.execute_tool(context, "draft_mentor_meal", {"meal": MEAL})
        return {"text": "Примерно 242 ккал"}

    async def run():
        client = transport.TelegramAIClient(SimpleNamespace(send_message_v2=send), 123, "new")
        for text in ("Творог 5%", "200 г"):
            token = transport.food_lookup_mode.set(True)
            try:
                result = await client.send_message_v2(input_text=text)
            finally:
                transport.food_lookup_mode.reset(token)
            assert transport.catalog_context.get() is None
        assert result["food_estimate"] == ESTIMATE and result["meal_draft"] is None
        assert not transport.food_lookup_mode.get()
    asyncio.run(run())
    assert turns == [True, True]


def test_lookup_tasks_do_not_leak_mode_into_consumed_meals(monkeypatch):
    api = AsyncMock(side_effect=lambda path, payload: {"estimate": ESTIMATE} if path.endswith("/estimate")
        else {"entry": {**ENTRY, "status": "draft"}})
    monkeypatch.setattr(transport, "api", api)

    async def turn(lookup):
        token = transport.food_lookup_mode.set(lookup)
        try:
            await asyncio.sleep(0)
            context = {"telegram_user_id": 123 if lookup else 456, "request_key": "turn"}
            await transport.execute_tool(context, "draft_mentor_meal", {"meal": MEAL})
            return context
        finally:
            transport.food_lookup_mode.reset(token)

    async def run():
        preview, meal = await asyncio.gather(turn(True), turn(False))
        assert "food_estimate" in preview and "meal_draft" not in preview
        assert "meal_draft" in meal and "food_estimate" not in meal
    asyncio.run(run())


def test_preview_rows_do_not_offer_draft_or_recorded_estimates():
    rows = food.food_response_rows({"food_estimate": ESTIMATE})
    assert [b.callback_data for b in rows[0]] == ["mentor:estimate_record:12", "mentor:estimate_cancel:12"]
    assert food.food_response_rows({"food_estimate": {**ESTIMATE, "status": "recorded"}}) == []
    assert food.food_response_rows({"meal_draft": ENTRY}) == []


@pytest.mark.parametrize("action", ["estimate_record:12", "estimate_cancel:12"])
def test_estimate_actions_preserve_ai_text_and_send_new_receipt(monkeypatch, action):
    api = AsyncMock(return_value={"entry": ENTRY})
    monkeypatch.setattr(food, "api", api)
    q = query(action)
    original = q.message.text
    panel = SimpleNamespace(answer=AsyncMock(), complete=AsyncMock())
    asyncio.run(food.dispatch_food(q, State(), action, panel))
    assert q.message.text == original
    q.message.edit_text.assert_not_awaited()
    q.message.edit_caption.assert_not_awaited()
    panel.answer.assert_not_awaited()
    panel.complete.assert_not_awaited()
    q.message.answer.assert_awaited_once()
    markup = q.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["mentor:menu"]
    if action.startswith("estimate_record"):
        api.assert_awaited_once_with("/workspace/food/record", {"telegram_user_id": 123, "entry_id": 12})
        assert "meal_delete:20" in str(q.message.answer.await_args.kwargs["reply_markup"])
    else:
        api.assert_not_awaited()


def test_duplicate_estimate_record_uses_same_backend_idempotency_identity(monkeypatch):
    api = AsyncMock(return_value={"entry": ENTRY})
    monkeypatch.setattr(food, "api", api)
    q = query("estimate_record:12")

    async def run():
        state = State()
        await food.dispatch_food(q, state, "estimate_record:12")
        await food.dispatch_food(q, state, "estimate_record:12")
    asyncio.run(run())
    assert api.await_count == 2
    assert api.await_args_list[0] == api.await_args_list[1]
    assert all(call.args[0] == "/workspace/food/record" for call in api.await_args_list)


def test_backend_ownership_denial_never_reports_saved_or_changes_text(monkeypatch):
    api = AsyncMock(side_effect=transport.BridgeError("Запись не найдена", 404))
    monkeypatch.setattr(food, "api", api)
    q = query("estimate_record:99")
    with pytest.raises(transport.BridgeError):
        asyncio.run(food.dispatch_food(q, State(), "estimate_record:99"))
    api.assert_awaited_once_with("/workspace/food/record", {"telegram_user_id": 123, "entry_id": 99})
    q.message.answer.assert_not_awaited()
    q.message.edit_reply_markup.assert_not_awaited()


@pytest.mark.parametrize("chat_id,chat_type", [(456, "private"), (123, "group")])
def test_callbacks_cannot_write_for_different_chat(monkeypatch, chat_id, chat_type):
    api = AsyncMock()
    monkeypatch.setattr(food, "api", api)
    q = query("estimate_record:12", chat_id=chat_id, chat_type=chat_type)
    asyncio.run(food.dispatch_food(q, State(), "estimate_record:12"))
    api.assert_not_awaited()
    q.answer.assert_awaited_once()


@pytest.mark.parametrize("decision", ["meal_delete_confirm", "meal_delete_cancel"])
def test_delete_needs_owned_meal_and_state_token_and_cannot_repeat(monkeypatch, decision):
    api = AsyncMock(side_effect=[{"entry": ENTRY}, {"ok": True}])
    monkeypatch.setattr(food, "api", api)
    q = query("meal_delete:20")

    async def run():
        state = State()
        await food.dispatch_food(q, state, "meal_delete:20")
        assert api.await_count == 1
        token = state.data["mentor_meal_delete"]["token"]
        before = api.await_count
        await food.dispatch_food(q, state, "meal_delete_confirm:20:0000000000000000")
        assert api.await_count == before
        action = f"{decision}:20:{token}"
        await food.dispatch_food(q, state, action)
        count = api.await_count
        await food.dispatch_food(q, state, action)
        assert api.await_count == count
        assert state.data["mentor_meal_delete"] is None
        assert count == (2 if decision == "meal_delete_confirm" else 1)
    asyncio.run(run())
    if decision == "meal_delete_confirm":
        api.assert_awaited_with("/workspace/meals/delete", {"telegram_user_id": 123, "entry_id": 20})
    q.message.edit_text.assert_not_awaited()


def test_replaced_delete_prompt_invalidates_old_token(monkeypatch):
    monkeypatch.setattr(food, "api", AsyncMock(return_value={"entry": ENTRY}))
    q = query("meal_delete:20")

    async def run():
        state = State()
        await food.dispatch_food(q, state, "meal_delete:20")
        old = state.data["mentor_meal_delete"]["token"]
        await food.dispatch_food(q, state, "meal_delete:20")
        await food.dispatch_food(q, state, f"meal_delete_confirm:20:{old}")
        assert food.api.await_count == 2
    asyncio.run(run())


def test_failed_delete_keeps_confirmation_for_retry_without_claiming_success(monkeypatch):
    api = AsyncMock(side_effect=[{"entry": ENTRY}, transport.BridgeError("unavailable"), {"ok": True}])
    monkeypatch.setattr(food, "api", api)
    q = query("meal_delete:20")

    async def run():
        state = State()
        await food.dispatch_food(q, state, "meal_delete:20")
        pending = state.data["mentor_meal_delete"].copy()
        action = f"meal_delete_confirm:20:{pending['token']}"
        with pytest.raises(transport.BridgeError):
            await food.dispatch_food(q, state, action)
        assert state.data["mentor_meal_delete"] == pending
        assert q.message.answer.await_count == 1
        await food.dispatch_food(q, state, action)
        assert state.data["mentor_meal_delete"] is None
    asyncio.run(run())


@pytest.mark.parametrize("change", [{"entry_id": 99}, {"user_id": 456}])
def test_delete_token_cannot_be_used_for_different_record_or_owner(monkeypatch, change):
    api = AsyncMock()
    monkeypatch.setattr(food, "api", api)
    state = State()
    state.data["mentor_meal_delete"] = {
        "entry_id": 20, "user_id": 123, "token": "0123456789abcdef", **change}
    action = "meal_delete_confirm:20:0123456789abcdef"
    asyncio.run(food.dispatch_food(query(action), state, action))
    api.assert_not_awaited()


@pytest.mark.parametrize("action", ["meal_delete_confirm:20", "meal_delete:-2", "estimate_record:12:spoof"])
def test_malformed_food_callback_never_writes(monkeypatch, action):
    api = AsyncMock()
    monkeypatch.setattr(food, "api", api)
    asyncio.run(food.dispatch_food(query(action), State(), action))
    api.assert_not_awaited()


def test_unrelated_callback_falls_through():
    assert asyncio.run(food.dispatch_food(query("menu"), State(), "menu")) is False
