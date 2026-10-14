import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ai import mentor_input as parser, telegram_mentor as bridge
from src.bot.handlers import mentor, mentor_flows as flows, mentor_format as formatting, mentor_instruction_cache as cache
from src.bot.handlers.ai_helpers import safe_ai_response
from test_telegram_flows import State, dashboard
from test_mentor_navigation import message, query, actions


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.config, 'DATA_DIR', tmp_path)
    bridge.set_mentor_enabled(123, True)


def run_action(action, saved, monkeypatch):
    mock = AsyncMock(return_value=saved)
    monkeypatch.setattr(flows, "api", mock)
    msg, state = message(), State()
    asyncio.run(flows.dispatch(query(action, msg), state, action, None, None, None))
    return msg, state, mock


def test_stock_button_starts_actual_recount_not_same_course_screen(monkeypatch):
    saved = dashboard()
    saved['workspace']['courses'] = [{'id': 7, 'dose_text': '2 mg weekly', 'amount_per_intake': 2,
        'supply_unit': 'мг', 'supply': {'available': True}}]
    msg, state, api = run_action('course_supply:7', saved, monkeypatch)
    assert state.values['form_kind'] == 'course_supply' and state.values['course_supply_edit'] is True
    assert 'осталось' in msg.answer.await_args.args[0]
    assert state.values['form_known']['amount_per_intake'] == 2
    assert 'mentor:menu' in actions(msg.answer.await_args.kwargs['reply_markup'])
    assert all(call.args[0] == '/dashboard' for call in api.await_args_list)


def test_unknown_old_stock_is_not_seeded_into_recount(monkeypatch):
    saved = dashboard()
    saved['workspace']['courses'] = [{'id': 7, 'dose_text': '2 mg weekly', 'amount_per_intake': 2,
        'supply_unit': 'флакон 10 мг', 'supply': {'available': False}}]
    _, state, _ = run_action('course_supply:7', saved, monkeypatch)
    assert state.values['form_known'] == {'dose_text': '2 mg weekly'}


def test_skipping_recount_does_not_create_another_course(monkeypatch):
    api = AsyncMock()
    monkeypatch.setattr(flows, 'api', api)
    msg, state = message(text='-'), State(course_data={'id': 7}, course_supply_edit=True, form_kind='course_supply')
    asyncio.run(flows.receive_value(msg, state, None, normalized='-'))
    api.assert_not_awaited()
    assert 'Остаток не меняю' in msg.answer.await_args.args[0]


def test_new_course_resets_old_recount_state():
    msg, state = message(), State(course_supply_edit=True, course_id=7)
    asyncio.run(flows.start_form(msg, 123, state, 'course'))
    assert state.values['course_supply_edit'] is False and state.values['course_id'] is None


def test_stock_saved_receipt_survives_unavailable_refresh(monkeypatch):
    async def api(path, payload):
        if path == '/dashboard':
            raise bridge.BridgeError('Temporary read failure')
        return {'ok': True}
    monkeypatch.setattr(flows, 'api', AsyncMock(side_effect=api))
    msg, state = message(), State(pending_input={'kind': 'course_supply', 'token': 'receipt',
        'payload': {'entry_id': 7, 'request_key': 'same-key', 'supply_amount': 10,
            'amount_per_intake': 2, 'supply_unit': 'мг'}})
    asyncio.run(flows.confirm_input(msg, 123, state, 'input_save:receipt'))
    assert 'остаток сохранён' in msg.answer.await_args.args[0]


def test_course_card_has_one_specialist_link_and_an_intake_button(monkeypatch):
    monkeypatch.setenv('TELEGRAM_MENTOR_SPECIALIST_URL', 'https://t.me/peptide_rus/107318')
    saved = dashboard()
    saved['workspace']['courses'] = [{'id': 7, 'name': 'Existing scheme', 'dose_text': 'User-reported',
        'start_date': '2030-01-01', 'end_date': '2030-02-01', 'timezone': 'Asia/Almaty',
        'done': 0, 'due': 0, 'calendar': [], 'supply': {'available': True, 'remaining': 10,
            'unit': 'мг', 'intakes_available': 5, 'runs_out_at': '2030-01-19T04:00:00+00:00'}}]
    msg, _, _ = run_action('course', saved, monkeypatch)
    text = msg.answer.await_args.args[0]
    markup = msg.answer.await_args.kwargs['reply_markup']
    assert '0 из 0' not in text and '19 января, 09:00' in text
    assert 'mentor:course_mark:7' in actions(markup)
    links = [b for row in markup.inline_keyboard for b in row if b.url]
    assert len(links) == 1 and 'mentor:specialist' not in actions(markup)


def test_today_completed_workout_uses_results_action(monkeypatch):
    saved = dashboard()
    saved['workspace']['today_workouts'] = [{}]
    msg, _, _ = run_action('today', saved, monkeypatch)
    callbacks = actions(msg.answer.await_args.kwargs['reply_markup'])
    assert 'mentor:workout_results' in callbacks and 'mentor:workout_start' not in callbacks


def test_current_target_is_visible_and_month_average_matches_period(monkeypatch):
    saved = dashboard()
    saved['workspace']['target'] = {'kcal': 3200, 'protein': 180, 'fat': 90, 'carbs': 420}
    msg, _, _ = run_action('target', saved, monkeypatch)
    assert '3 200' in msg.answer.await_args.args[0] and '180' in msg.answer.await_args.args[0]
    saved['workspace']['weekly'] = {'days': 30, 'weight_mean': 84, 'weight_samples': 3,
        'weight_mean_7d': 81, 'course_due': 0, 'course_done': 0, 'nutrition_days': 1}
    text = flows.weekly_view(saved)
    assert 'Средний вес за 30 дней: 84 кг' in text
    assert 'Курс:' not in text and 'за 1 день' in text and 'дн.' not in text


def test_formatting_localizes_headings_numbers_and_city():
    assert formatting.html_text('### **Название**\nhttps://example.test/a_b/') == '<b>Название</b>\nhttps://example.test/a_b/'
    assert formatting.timezone_label('Asia/Almaty') == 'Алматы (UTC+5)'
    assert formatting.local_datetime('2026-10-19T04:00:00+00:00', 'Asia/Almaty') == '19 октября, 09:00'
    saved = dashboard()
    saved['settings']['timezone'] = 'Asia/Almaty'
    saved['weights'] = [{'occurred_at': '2026-10-14T10:02:00+00:00', 'weight_kg': 101}]
    assert '14.10 15:02' in mentor.history_text(saved)


def test_package_extraction_keeps_contents_and_dose_units_separate():
    data = {'package_count': 1, 'package_amount': 10, 'package_unit': 'мг',
        'amount_per_intake': 2, 'intake_unit': 'мг'}
    import json
    assert json.loads(parser.canonical('course_supply', data, False, {})) == data
    assert 'содержится' in parser.followup('course_supply', {'package_count': 1}, {})
    changed = parser.merge_known('course_supply', data, {'supply_amount': 8, 'supply_unit': 'мг'})
    assert changed['package_count'] is None


def test_callback_owned_error_sends_no_second_message():
    async def fail():
        raise RuntimeError('upstream unavailable')
    msg = message()
    assert asyncio.run(safe_ai_response(msg, fail(), notify_errors=False)) is None
    msg.answer.assert_not_awaited()


def test_empty_mentor_response_is_a_single_clear_failure():
    async def empty():
        return {'mentor': True, 'text': '', 'files': []}
    msg = message()
    assert asyncio.run(safe_ai_response(msg, empty())) is None
    msg.answer.assert_awaited_once()
    assert 'наставника' in msg.answer.await_args.args[0]


def test_instruction_cache_is_user_scoped_expires_and_never_caches_drafts(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(cache.time, 'time', lambda: 10000)
    response = {'mentor': True, 'text': 'Existing reference', 'files': []}
    key = cache.cache_key('reference', {'version': 1})
    cache.store(123, key, response, 'professor')
    assert cache.load(123, key)['text'] == 'Existing reference'
    assert cache.load(124, key) is None
    cache.store(123, 'unsafe', {**response, 'meal_draft': {'id': 7}}, 'professor')
    assert cache.load(123, 'unsafe') is None
    cache.store(123, 'unavailable', {**response, 'text': 'Не удалось получить инструкцию. Попробуйте позже.'}, 'professor')
    assert cache.load(123, 'unavailable') is None
    assert cache.cache_key('reference', {'version': 2}) != key
    bridge.reset_mentor_conversations(123)
    assert cache.load(123, key) is None
    cache.store(123, key, response, 'professor')
    monkeypatch.setattr(cache.time, 'time', lambda: 10000+cache.TTL+1)
    assert cache.load(123, key) is None


def test_repeated_ai_button_does_not_queue_another_request(monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []
    async def handle(*args, **kwargs):
        calls.append(args)
        started.set()
        await finish.wait()
        return True
    monkeypatch.setattr(mentor, '_run_ai_action', handle)
    msg, state = message(), State()
    callback = query('suggest', msg)
    async def run():
        first = asyncio.create_task(mentor.run_ai_action(callback, state, None, None, None, 'suggest'))
        await started.wait()
        await mentor.run_ai_action(callback, state, None, None, None, 'suggest')
        assert len(calls) == 1
        finish.set()
        await first
    asyncio.run(run())
