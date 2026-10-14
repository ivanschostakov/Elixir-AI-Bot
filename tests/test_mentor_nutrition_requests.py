import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai import client as module
from src.ai import telegram_mentor as transport


@pytest.mark.parametrize("action,lookup,names", [
    ("suggest", False, set()),
    ("analyze_day", False, set()),
    (None, True, {"draft_mentor_meal"}),
])
def test_nutrition_uses_fresh_memory_without_peptide_tools(monkeypatch, tmp_path, action, lookup, names):
    (tmp_path / "new.txt").write_text("Peptide specialist instructions")
    monkeypatch.setattr(module, "INSTRUCTIONS_DIR", tmp_path)

    async def run():
        client = module.ProfessorClient(api_key="test", keyword="new")
        request = AsyncMock(return_value=SimpleNamespace(output=[]))
        monkeypatch.setattr(client.responses, "create", request)
        token = transport.catalog_context.set({"saved": {"profile": {"restrictions": "орехи"},
            "workspace": {"target": {"kcal": 2100}, "remaining": {"kcal": 700},
                "courses": [{"name": "irrelevant-future-calendar"}],
                "today_workouts": [{"duration_minutes": 30}]}},
            "button_action": action, "food_lookup_mode": lookup, "usage": [0, 0, 0]})
        try:
            await client._create_v2_response("same-mentor-conversation", "Что поесть?", None)
        finally:
            transport.catalog_context.reset(token)
            await client.close()
        sent = request.await_args.kwargs
        assert {tool["name"] for tool in sent["tools"]} == names
        assert sent["reasoning"] == {"effort": "low"}
        assert sent["model"] == "gpt-5.6-terra"
        assert sent["conversation"]["id"] == "same-mentor-conversation"
        assert "орехи" in sent["instructions"] and "700" in sent["instructions"]
        assert "duration_minutes" in sent["instructions"]
        assert "irrelevant-future-calendar" not in sent["instructions"]
        assert "Peptide specialist" not in sent["instructions"]
    asyncio.run(run())


def test_readonly_suggestion_cannot_execute_a_write_tool(monkeypatch, tmp_path):
    (tmp_path / "new.txt").write_text("Original instructions")
    monkeypatch.setattr(module, "INSTRUCTIONS_DIR", tmp_path)
    execute = AsyncMock()
    monkeypatch.setattr(module, "execute_tool", execute)

    async def run():
        client = module.ProfessorClient(api_key="test", keyword="new")
        call = SimpleNamespace(type="function_call", name="draft_mentor_meal", arguments="{}", call_id="1")
        request = AsyncMock(side_effect=[SimpleNamespace(output=[call]), SimpleNamespace(output=[])])
        monkeypatch.setattr(client.responses, "create", request)
        monkeypatch.setattr(client, "_extract_v2_usage", lambda response: (1, 0, 1))
        token = transport.catalog_context.set({"saved": {}, "button_action": "suggest", "usage": [0, 0, 0]})
        try:
            await client._create_v2_response("mentor", "Что поесть?", None)
        finally:
            transport.catalog_context.reset(token)
            await client.close()
        execute.assert_not_awaited()
        assert "tool_failed" in request.await_args.kwargs["input"][0]["output"]
        assert request.await_args.kwargs["reasoning"] == {"effort": "low"}
    asyncio.run(run())
