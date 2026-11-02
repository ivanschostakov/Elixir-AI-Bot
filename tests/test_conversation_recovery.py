import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

import config
from src.ai import telegram_mentor as state
from src.ai.client import ProfessorClient
from src.bot.middleware import ContextMiddleware


def test_conversations_persist_separately_for_each_bot(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    async def run():
        for keyword in ["new", "dose", "professor"]:
            client = ProfessorClient("test-key", keyword=keyword)
            client.conversations.create = AsyncMock(return_value=SimpleNamespace(id="conv_" + keyword))
            await client.create_conversation(user_id=123)
            await client.close()
        for keyword in ["new", "dose", "professor"]:
            assert state.mentor_conversation(123, "assistant:" + keyword) == "conv_" + keyword
        assert state.mentor_conversation(123, "new") is None
    asyncio.run(run())


def test_rollover_preserves_text_order_without_replaying_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    async def run():
        client = ProfessorClient("test-key", keyword="new")
        client.conversations.retrieve = AsyncMock(return_value=SimpleNamespace(metadata={"user_id": "123", "bot_keyword": "new"}))
        def message(role, text):
            return SimpleNamespace(type="message", role=role, content=[SimpleNamespace(type="output_text", text=text)])
        client.conversations.items.list = AsyncMock(return_value=SimpleNamespace(data=[
            message("assistant", "Answer"), SimpleNamespace(type="function_call"), message("user", "Question"),
        ]))
        client.conversations.create = AsyncMock(return_value=SimpleNamespace(id="conv_next"))
        await client._continue_in_new_conversation("conv_old", 123)
        assert client.conversations.create.await_args.kwargs["items"] == [
            {"role": "user", "content": "Question"}, {"role": "assistant", "content": "Answer"}]
        await client.close()
    asyncio.run(run())


def test_other_users_history_is_never_copied(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    async def run():
        client = ProfessorClient("test-key", keyword="new")
        client.conversations.retrieve = AsyncMock(return_value=SimpleNamespace(metadata={"user_id": "999"}))
        client.conversations.items.list = AsyncMock()
        client.conversations.create = AsyncMock(return_value=SimpleNamespace(id="conv_safe"))
        await client._continue_in_new_conversation("conv_other", 123)
        client.conversations.items.list.assert_not_awaited()
        assert "items" not in client.conversations.create.await_args.kwargs
        await client.close()
    asyncio.run(run())


def test_repeated_menu_tap_is_a_noop_but_other_errors_propagate():
    event = Mock(spec=CallbackQuery)
    event.answer = AsyncMock()
    middleware = ContextMiddleware(None, None)
    handler = AsyncMock(side_effect=TelegramBadRequest(method=Mock(), message="message is not modified"))
    asyncio.run(middleware(handler, event, {}))
    event.answer.assert_awaited_once()
    handler.side_effect = TelegramBadRequest(method=Mock(), message="message to edit not found")
    with pytest.raises(TelegramBadRequest):
        asyncio.run(middleware(handler, event, {}))
