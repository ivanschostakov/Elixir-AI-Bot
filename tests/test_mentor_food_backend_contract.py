"""Exercise the sibling backend in isolation; no real DB or network is opened."""
import os
from pathlib import Path
import subprocess

import pytest


def test_food_backend_ownership_and_repeat_contract():
    backend = Path(os.environ.get("ELIXIR_APP_BACKEND", str(
        Path.home() / "Projects" / "Elixir-Shop-Application" / "backend")))
    python = backend.parent / ".venv" / "bin" / "python"
    if not python.exists() or not (backend / "tests" / "conftest.py").exists():
        pytest.skip("Sibling backend and its test environment are required")
    script = '''
import asyncio
import runpy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

runpy.run_path("tests/conftest.py")
from fastapi import HTTPException
from src.app.modules.telegram_ai import mentor as m

m.lock = AsyncMock()
m.require_section = Mock()
estimate = SimpleNamespace(id=99, telegram_user_id=999, kind="food_estimate", status="recorded")
meal = SimpleNamespace(id=100, telegram_user_id=999, kind="meal", status="confirmed",
    occurred_at=m.now(), data={"name":"Food", "kcal":242, "protein":34, "fat":10, "carbs":6})

async def execute(statement):
    params = statement.compile().params
    assert params["telegram_user_id_1"] in {123, 999}
    if "id_1" in params:
        assert params["id_1"] in {99, 100}
        row = estimate if params["id_1"] == 99 else meal
        row = row if params["telegram_user_id_1"] == row.telegram_user_id else None
    else:
        assert params["request_key_1"] == "food-estimate-record:99"
        row = meal
    return SimpleNamespace(scalar_one_or_none=lambda: row)

db = SimpleNamespace(execute=AsyncMock(side_effect=execute), commit=AsyncMock(), add=Mock())

async def run():
    assert await m.owned(db, 999, 99, "food_estimate") is estimate
    for endpoint, entry_id in ((m.record_estimate,99), (m.delete_meal,100)):
        try:
            await endpoint(m.EstimateRecord(telegram_user_id=123, entry_id=entry_id), db)
        except HTTPException as error:
            assert error.status_code == 404
        else:
            raise AssertionError("Backend accepted another user's record")
    db.commit.assert_not_awaited()
    db.add.assert_not_called()
    payload = m.EstimateRecord(telegram_user_id=999, entry_id=99)
    first = await m.record_estimate(payload, db)
    second = await m.record_estimate(payload, db)
    assert first["entry"]["id"] == second["entry"]["id"] == 100
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    payload = m.EstimateRecord(telegram_user_id=999, entry_id=100)
    await m.delete_meal(payload, db)
    await m.delete_meal(payload, db)
    assert meal.status == "deleted"
    db.commit.assert_awaited_once()
    try:
        await m.record_estimate(m.EstimateRecord(telegram_user_id=999, entry_id=99), db)
    except HTTPException as error:
        assert error.status_code == 409
    else:
        raise AssertionError("Deleted meal was resurrected by a duplicate record click")

asyncio.run(run())
print("ownership and idempotency verified without a database connection")
'''
    environment = {**os.environ, "PYTHONPATH": str(backend)}
    result = subprocess.run([str(python), "-c", script], cwd=backend, env=environment,
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
