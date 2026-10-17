import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from src.ai.mentor_input import ENVELOPES
from src.bot.handlers import mentor, mentor_access, mentor_flows, mentor_onboarding as setup
from src.bot.handlers.mentor_format import timezone_label
from test_mentor_navigation import message, query
from test_mentor_onboarding import backend
from test_telegram_flows import State


@pytest.fixture
def legacy(backend, monkeypatch):
    data, api = backend
    data['profile'] = {'goal': 'maintain', 'age': 40, 'sex': 'female', 'height_cm': 170,
                       'current_weight_kg': 70, 'activity': 'light'}
    data['settings'] = {'timezone': 'Europe/Moscow', 'daily_time': None, 'reminders': {}}
    monkeypatch.setattr(mentor, 'configured', lambda: True)
    monkeypatch.setattr(mentor, 'api', api)
    monkeypatch.setattr(mentor_flows, 'api', api)
    monkeypatch.setattr(mentor_access, 'check_mentor_phone', AsyncMock(return_value=False))
    monkeypatch.setenv('TELEGRAM_MENTOR_CLOSED_SECTIONS', '')
    return data, api


async def choose(choice, state, *, token=None):
    msg = message(mid=100)
    action = f"setup:{token or setup.load(123)['token']}:{choice}"
    await setup.callback(query(action, msg), state, msg)
    return msg


def callbacks(msg):
    return {button.callback_data for row in msg.answer.await_args.kwargs['reply_markup'].inline_keyboard
            for button in row}


@pytest.mark.parametrize('progress', [{}, {'status': 'complete'}, {'status': 'paused'}])
@pytest.mark.parametrize('zone', ['Europe/Moscow', 'Asia/Almaty'])
def test_complete_legacy_profile_gets_only_timezone_confirmation(legacy, progress, zone):
    data, api = legacy
    data['settings']['timezone'] = zone
    data['workspace']['target'] = {'kcal': 2000}
    setup.save(123, progress)
    before = deepcopy(data)
    msg, state = message(), State()

    asyncio.run(mentor.enter(msg, 123, state))

    current = setup.load(123)
    assert current['status'] == 'active'
    assert current['step'] == 'timezone_confirm' and current['timezone_only']
    assert current['timezone'] == zone
    assert not current.get('timezone_done')
    assert timezone_label(zone) in msg.answer.await_args.args[0]
    assert zone not in msg.answer.await_args.args[0]
    assert callbacks(msg) == {f"mentor:setup:{current['token']}:{choice}"
                              for choice in ('confirm_timezone', 'change_timezone', 'later', 'back')}
    assert state.values['mentor_panel']['kind'] == 'form'
    assert data == before
    assert {call.args[0] for call in api.await_args_list} == {'/dashboard'}


@pytest.mark.parametrize('has_target', [False, True])
def test_confirmation_is_one_time_without_profile_or_reminder_writes(legacy, has_target):
    data, api = legacy
    if has_target:
        data['workspace']['target'] = {'kcal': 2000}
    data['settings']['daily_time'] = '20:15'
    before = deepcopy(data)

    async def run():
        state = State()
        await mentor.enter(message(), 123, state)
        old_token = setup.load(123)['token']
        await choose('confirm_timezone', state)
        current = setup.load(123)
        assert current['timezone_done'] and current['status'] == 'complete'
        assert not current.get('timezone_only') and current['step'] is None
        await choose('change_timezone', state, token=old_token)
        assert setup.load(123) == current
        msg = message()
        await mentor.enter(msg, 123, State())
        assert 'mentor:meal' in callbacks(msg)
        assert not any(value.startswith('mentor:setup:') for value in callbacks(msg))

    asyncio.run(run())
    assert data == before
    assert {call.args[0] for call in api.await_args_list} == {'/dashboard'}


def test_confirmed_choice_is_not_reasked_or_replaced_by_old_metadata(legacy):
    data, api = legacy
    data['settings']['timezone'] = 'Asia/Almaty'
    progress = {'status': 'complete', 'timezone_done': True, 'timezone': 'Europe/Moscow'}
    setup.save(123, progress)
    msg = message()
    asyncio.run(mentor.enter(msg, 123, State()))
    assert 'mentor:meal' in callbacks(msg)
    assert setup.load(123) == progress
    assert data['settings']['timezone'] == 'Asia/Almaty'
    assert {call.args[0] for call in api.await_args_list} == {'/dashboard'}


def test_defer_keeps_menu_and_explicit_resume_keeps_selected_city(legacy):
    data, api = legacy
    data['settings']['timezone'] = 'Asia/Almaty'

    async def run():
        state = State()
        await mentor.enter(message(), 123, state)
        old_token = setup.load(123)['token']
        msg = await choose('later', state)
        paused = setup.load(123)
        assert paused['status'] == 'paused' and paused['timezone_only']
        assert 'mentor:setup_start' in callbacks(msg) and 'mentor:ask' in callbacks(msg)
        await mentor.enter(message(), 123, state)
        assert setup.load(123) == paused
        msg = message()
        await mentor.perform_action(query('setup_start', msg), state, msg)
        current = setup.load(123)
        assert current['step'] == 'timezone_confirm' and current['token'] != old_token
        assert current['timezone'] == 'Asia/Almaty'
        assert timezone_label('Asia/Almaty') in msg.answer.await_args.args[0]
        await choose('confirm_timezone', state, token=old_token)
        assert setup.load(123) == current

    asyncio.run(run())
    assert {call.args[0] for call in api.await_args_list} == {'/dashboard'}


def test_change_city_accepts_free_text_and_preserves_all_reminder_options(legacy, monkeypatch):
    data, api = legacy
    data['settings']['reminders'] = {'morning': '08:15', 'evening': '20:30', 'weekly': '12:00',
        'weekday': 2, 'inactivity_days': 4, 'course': True, 'detailed_reports': True}
    before = deepcopy(data)
    extraction = AsyncMock(return_value=ENVELOPES['timezone'](data={'timezone': 'Asia/Almaty'}))
    monkeypatch.setattr(setup, 'extract_answer', extraction)

    async def run():
        state = State()
        await mentor.enter(message(), 123, state)
        await choose('change_timezone', state)
        assert setup.load(123)['step'] == 'timezone'
        await setup.receive(message(mid=120, text='I live in Almaty, Kazakhstan'), state)
        assert setup.load(123)['step'] == 'timezone_confirm'
        assert data == before
        setup.pause(123)
        msg = message()
        await setup.resume(msg, 123)
        assert timezone_label('Asia/Almaty') in msg.answer.await_args.args[0]
        assert setup.load(123)['step'] == 'timezone_confirm'
        await choose('confirm_timezone', state)
        assert setup.load(123)['status'] == 'complete'

    asyncio.run(run())
    extraction.assert_awaited_once()
    assert extraction.await_args.args[1] == 'timezone'
    writes = [call for call in api.await_args_list if call.args[0] != '/dashboard']
    assert len(writes) == 1 and writes[0].args[0] == '/reminder/options'
    assert writes[0].args[1] == {'telegram_user_id': 123,
        **mentor_flows.reminder_payload(before['settings']), 'timezone': 'Asia/Almaty'}
    before['settings']['timezone'] = 'Asia/Almaty'
    assert data == before


def test_new_incomplete_profile_keeps_original_funnel(legacy):
    data, api = legacy
    data['profile'] = {}

    async def run():
        state = State()
        await mentor.enter(message(), 123, state)
        assert setup.load(123)['status'] == 'paused'
        assert not setup.load(123).get('timezone_only')
        await choose('begin', state)
        assert setup.load(123)['step'] == 'goal'

    asyncio.run(run())
    assert {call.args[0] for call in api.await_args_list} == {'/dashboard'}


@pytest.mark.parametrize('zone', [None, 'Not/AZone'])
def test_missing_or_invalid_timezone_asks_city_without_guessing(legacy, zone):
    data, api = legacy
    data['settings']['timezone'] = zone
    msg = message()
    asyncio.run(mentor.enter(msg, 123, State()))
    assert setup.load(123)['step'] == 'timezone'
    assert setup.PROMPTS['timezone'] in msg.answer.await_args.args[0]
    assert {call.args[0] for call in api.await_args_list} == {'/dashboard'}


def test_failed_timezone_write_does_not_mark_confirmation_done(legacy, monkeypatch):
    data, api = legacy
    extraction = AsyncMock(return_value=ENVELOPES['timezone'](data={'timezone': 'Asia/Almaty'}))
    monkeypatch.setattr(setup, 'extract_answer', extraction)
    original = api.side_effect

    async def fail_write(path, payload):
        if path == '/reminder/options':
            raise setup.bridge.BridgeError('unavailable')
        return await original(path, payload)

    api.side_effect = fail_write

    async def run():
        state = State()
        await mentor.enter(message(), 123, state)
        await choose('change_timezone', state)
        await setup.receive(message(mid=120, text='Almaty'), state)
        with pytest.raises(setup.bridge.BridgeError):
            await choose('confirm_timezone', state)
        assert not setup.load(123).get('timezone_done')
        assert setup.load(123)['step'] == 'timezone_confirm'
        assert data['settings']['timezone'] == 'Europe/Moscow'
        api.side_effect = original
        await choose('confirm_timezone', state)
        assert setup.load(123)['timezone_done']

    asyncio.run(run())


@pytest.mark.parametrize('zone', ['Europe/Moscow', 'Asia/Almaty'])
def test_manual_settings_choice_marks_confirmation_and_prevents_repeat(legacy, zone):
    data, api = legacy

    async def run():
        state = State(pending_input={'kind': 'timezone', 'token': 'manual', 'payload': {'timezone': zone}})
        await mentor_flows.confirm_input(message(), 123, state, 'input_save:manual')
        current = setup.load(123)
        assert current['timezone_done'] and current['timezone'] == zone
        msg = message()
        await mentor.enter(msg, 123, state)
        assert 'mentor:meal' in callbacks(msg)
        assert not any(value.startswith('mentor:setup:') for value in callbacks(msg))

    asyncio.run(run())
    assert data['settings']['timezone'] == zone
    assert len([call for call in api.await_args_list if call.args[0] == '/reminder/options']) == 1


@pytest.mark.parametrize('timezone_only', [False, True])
def test_old_onboarding_button_cannot_overwrite_new_manual_timezone(legacy, timezone_only):
    data, api = legacy
    progress = {'status': 'active', 'step': 'timezone_confirm', 'token': 'old-choice',
                'timezone': 'Europe/Moscow', 'preferences_done': True}
    if timezone_only:
        progress['timezone_only'] = True
    setup.save(123, progress)

    async def run():
        state = State(pending_input={'kind': 'timezone', 'token': 'manual',
                                     'payload': {'timezone': 'Asia/Almaty'}})
        await mentor_flows.confirm_input(message(), 123, state, 'input_save:manual')
        current = setup.load(123)
        assert current['timezone_done'] and current['timezone'] == 'Asia/Almaty'
        assert current['token'] != 'old-choice' and current['preferences_done']
        assert current['status'] == ('complete' if timezone_only else 'paused')
        assert not current.get('timezone_only')
        api.reset_mock()
        await choose('confirm_timezone', state, token='old-choice')
        assert setup.load(123) == current
        assert data['settings']['timezone'] == 'Asia/Almaty'
        assert not any(call.args[0] == '/reminder/options' for call in api.await_args_list)

    asyncio.run(run())


@pytest.mark.parametrize('kind,fail', [('timezone', True), ('reminder', False)])
def test_failed_save_or_reminder_only_change_does_not_confirm_timezone(legacy, kind, fail):
    _, api = legacy
    original = api.side_effect

    async def call(path, payload):
        if fail and path == '/reminder/options':
            raise setup.bridge.BridgeError('unavailable')
        return await original(path, payload)

    api.side_effect = call

    async def run():
        payload = {'timezone': 'Asia/Almaty'} if kind == 'timezone' else {'evening': '20:00'}
        state = State(pending_input={'kind': kind, 'token': 'manual', 'payload': payload})
        if fail:
            with pytest.raises(setup.bridge.BridgeError):
                await mentor_flows.confirm_input(message(), 123, state, 'input_save:manual')
        else:
            await mentor_flows.confirm_input(message(), 123, state, 'input_save:manual')
        assert setup.load(123) == {}

    asyncio.run(run())


def test_course_reminder_uses_human_timezone_label(legacy):
    data, _ = legacy
    data['settings']['reminders'] = {'course': True}
    data['workspace']['courses'] = [{'id': 7, 'name': 'Saved course', 'dose_text': 'Saved dose',
        'start_date': '2030-01-01', 'end_date': '2030-01-31', 'timezone': 'Asia/Almaty',
        'reminder_time': '09:00', 'calendar': [], 'due': 0, 'done': 0}]
    msg = message()
    asyncio.run(mentor_flows.dispatch(query('course', msg), State(), 'course', None, None, None))
    text = '\n'.join(call.args[0] for call in msg.answer.await_args_list)
    assert '09:00' in text and timezone_label('Asia/Almaty') in text
    assert 'Asia/Almaty' not in text
