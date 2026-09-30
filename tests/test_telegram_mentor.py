import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from src.ai import telegram_mentor as t


@pytest.fixture(autouse=True)
def isolated_mode_store(tmp_path, monkeypatch):
    monkeypatch.setattr(t.config, 'DATA_DIR', tmp_path)


def test_original_chat_and_model_are_preserved(monkeypatch):
    seen = {}
    async def send(**kwargs):
        seen.update(kwargs)
        c = t.catalog_context.get()
        assert c['saved']['profile']['age'] == 19
        return {'text': 'Какая обычно активность?', 'conversation_id': 'same-conversation', 'input_tokens': 17}
    api = AsyncMock(return_value={'profile': {'age': 19}, 'version': 1})
    monkeypatch.setattr(t, 'api', api)
    async def run():
        result = await t.TelegramAIClient(SimpleNamespace(send_message_v2=send), 123, 'new').send_message_v2(input_text='Привет', conversation_id='same-conversation', trace_id='tg-123-5')
        assert result['conversation_id'] == 'same-conversation'
        assert result['input_tokens'] == 17
        assert 'mentor_messages' not in result
        assert seen['conversation_id'] is None
        assert t.catalog_context.get() is None
        assert api.await_args.args[0] == '/dashboard'
    asyncio.run(run())


def test_profile_identity_and_evidence_come_from_transport(monkeypatch):
    api = AsyncMock(return_value={'ok': True, 'profile': {'age': 19}, 'version': 2})
    monkeypatch.setattr(t, 'api', api)
    c = {'saved': {'profile': {}, 'version': 1}, 'telegram_user_id': 123, 'source_text': 'Мне 19', 'request_key': 'telegram-message-1'}
    asyncio.run(t.execute_tool(c, 'update_mentor_profile', {'patch': {'age': 19}, 'evidence': 'Мне 19', 'telegram_user_id': 999, 'source_text': 'Мне 50', 'expected_version': 30}))
    sent = api.await_args.args[1]
    assert sent['telegram_user_id'] == 123 and sent['source_text'] == 'Мне 19'
    assert sent['expected_version'] == 1 and c['saved']['version'] == 2


def test_memory_outage_keeps_chat_working(monkeypatch):
    monkeypatch.setattr(t, 'api', AsyncMock(side_effect=t.BridgeError('unavailable')))
    async def send(**kwargs):
        assert t.catalog_context.get()['saved'] is None
        return {'text': 'Давайте обсудим питание'}
    result = asyncio.run(t.TelegramAIClient(SimpleNamespace(send_message_v2=send), 123, 'new').send_message_v2(input_text='Привет'))
    assert result['text'] == 'Давайте обсудим питание'


def test_purchase_button_marker_removed(monkeypatch):
    monkeypatch.setattr(t, 'api', AsyncMock(return_value={'profile': {}, 'version': 0}))
    delegate = SimpleNamespace(send_message_v2=AsyncMock(return_value={'text': 'Откройте магазин [[OPEN_SHOP]]'}))
    result = asyncio.run(t.TelegramAIClient(delegate, 123, 'professor').send_message_v2(input_text='где купить?', conversation_id=None))
    assert result['open_shop'] and '[[OPEN_SHOP]]' not in result['text']


def test_bad_tool_arguments_do_not_escape_validation():
    with pytest.raises(ValueError):
        asyncio.run(t.execute_tool({'saved': {'profile': {}, 'version': 0}}, 'update_mentor_profile', []))


def test_original_assistant_is_default_and_has_no_added_tools(monkeypatch):
    monkeypatch.setattr(t, 'configured', lambda: True)
    delegate=object()
    assert t.wrap_client(delegate, 456, 'new') is delegate
    t.set_mentor_enabled(456, True)
    assert isinstance(t.wrap_client(delegate, 456, 'new'), t.TelegramAIClient)
    assert t.wrap_client(delegate, 789, 'new') is delegate
    t.set_mentor_enabled(456, False)
    assert t.wrap_client(delegate, 456, 'new') is delegate
    assert t.catalog_context.get() is None


def test_separate_conversations_resume_without_overwriting_regular_chat(monkeypatch):
    monkeypatch.setattr(t, 'api', AsyncMock(return_value={'profile':{},'version':0}))
    delegate=SimpleNamespace(send_message_v2=AsyncMock(side_effect=[
        {'text':'Один вопрос?', 'conversation_id':'mentor-first'},
        {'text':'Следующий вопрос?', 'conversation_id':'mentor-next'},
    ]))
    client=t.TelegramAIClient(delegate, 456, 'new')
    async def run():
        first=await client.send_message_v2(input_text='Привет',conversation_id='regular-old')
        assert delegate.send_message_v2.await_args.kwargs['conversation_id'] is None
        assert first['conversation_id']=='regular-old'
        second=await client.send_message_v2(input_text='19',conversation_id='regular-old')
        assert delegate.send_message_v2.await_args.kwargs['conversation_id']=='mentor-first'
        assert second['conversation_id']=='regular-old'
        assert t.mentor_conversation(456,'new')=='mentor-next'
        assert t.mentor_conversation(456,'professor') is None
        t.reset_mentor_conversations(456)
        assert t.mentor_conversation(456,'new') is None
    asyncio.run(run())


def test_opening_question_is_available_for_short_answer(monkeypatch):
    t.save_opening_question(456,'Сколько вам лет?')
    monkeypatch.setattr(t,'api',AsyncMock(return_value={'profile':{},'version':0}))
    async def send(**kwargs):
        c=t.catalog_context.get()
        assert c['source_text']=='19'
        assert 'Сколько вам лет?' in t.memory_instructions(c)
        return {'text':'Какой рост?', 'conversation_id':'mentor-age'}
    asyncio.run(t.TelegramAIClient(SimpleNamespace(send_message_v2=send),456,'new').send_message_v2(input_text='19',conversation_id='regular'))
    assert t.opening_question(456) is None


def test_navigation_leaves_mentor_but_keeps_saved_conversation():
    from src.bot.handlers.mentor import MentorNavigationMiddleware
    t.set_mentor_enabled(456,True)
    t.save_mentor_conversation(456,'new','mentor-saved')
    event=SimpleNamespace(data='user:ai:free',text=None,from_user=SimpleNamespace(id=456))
    handler=AsyncMock(return_value='ordinary')
    assert asyncio.run(MentorNavigationMiddleware()(handler,event,{}))=='ordinary'
    assert not t.mentor_enabled(456)
    assert t.mentor_conversation(456,'new')=='mentor-saved'


def test_regular_provider_request_has_original_prompt_and_tools(monkeypatch, tmp_path):
    from src.ai.client import ProfessorClient
    from pathlib import Path
    import src.ai.client as module
    (tmp_path/'new.txt').write_text('Original assistant instructions.')
    monkeypatch.setattr(module,'INSTRUCTIONS_DIR',tmp_path)
    async def run():
        client=ProfessorClient(api_key='test',keyword='new')
        call=AsyncMock(return_value=SimpleNamespace(output=[]))
        monkeypatch.setattr(client.responses,'create',call)
        await client._create_v2_response('ordinary-conversation','Обычный вопрос',None)
        sent=call.await_args.kwargs
        assert sent['instructions']==client.instructions
        assert sent['tools']==client._build_tools()
        assert 'update_mentor_profile' not in str(sent['tools'])
        assert 'Наставник по похудению' not in sent['instructions']
        await client.close()
    asyncio.run(run())


def test_mentor_entry_uses_saved_profile_and_asks_one_question(monkeypatch):
    from src.bot.handlers import mentor
    monkeypatch.setattr(mentor,'configured',lambda:True)
    monkeypatch.setattr(mentor,'api',AsyncMock(return_value={'profile':{'goal':'weight_loss','age':19,'height_cm':183,'current_weight_kg':110,'target_weight_kg':90},'version':1}))
    msg=SimpleNamespace(answer=AsyncMock())
    asyncio.run(mentor.enter(msg,456,SimpleNamespace(clear=AsyncMock())))
    assert t.mentor_enabled(456)
    text=msg.answer.await_args.args[0]
    assert 'пол для расчёта КБЖУ' in text
    assert 'Сколько вы сейчас весите?' not in text
    assert t.opening_question(456)=='Укажите пол для расчёта КБЖУ: мужской или женский.'
    buttons=msg.answer.await_args.kwargs['reply_markup'].inline_keyboard
    assert [b.callback_data for row in buttons for b in row] == ['mentor:meal','mentor:today','mentor:food','mentor:workouts','mentor:course','mentor:progress','mentor:ask','mentor:profile','mentor:settings','mentor:leave']


def test_meal_tool_cannot_confirm_or_spoof_identity(monkeypatch):
    call=AsyncMock(return_value={'ok':True,'entry':{'id':12,'status':'draft'}})
    monkeypatch.setattr(t,'api',call)
    c={'telegram_user_id':456,'request_key':'real-message-1'}
    asyncio.run(t.execute_tool(c,'draft_mentor_meal',{'meal':{'name':'Суп','kcal':200,'protein':10,'fat':5,'carbs':30},'telegram_user_id':999,'status':'confirmed'}))
    assert call.await_args.args[0]=='/journal/draft'
    sent=call.await_args.args[1]
    assert sent['telegram_user_id']==456 and 'status' not in sent
    assert c['meal_draft']['id']==12


def test_menu_meal_confirmation_and_no_fabricated_progress():
    from src.bot.handlers import mentor
    actions=[b.callback_data for row in mentor.menu().inline_keyboard for b in row]
    assert actions==['mentor:meal','mentor:today','mentor:food','mentor:workouts','mentor:course','mentor:progress','mentor:ask','mentor:profile','mentor:settings','mentor:leave']
    reply=mentor.response_keyboard({'meal_draft':{'id':12}})
    assert reply.inline_keyboard[0][0].callback_data=='mentor:meal_confirm:12'
    assert 'Истории измерений пока нет' in mentor.history_text({'profile':{'current_weight_kg':110}})
    assert 'нет записанной еды' in mentor.today_text({'meals':[]})


def test_menu_navigation_does_not_restart_onboarding(monkeypatch):
    from src.bot.handlers import mentor
    monkeypatch.setattr(mentor,'configured',lambda:True)
    monkeypatch.setattr(mentor,'api',AsyncMock(return_value={'profile':{},'version':0}))
    msg=SimpleNamespace(answer=AsyncMock())
    asyncio.run(mentor.enter(msg,456,SimpleNamespace(clear=AsyncMock()),onboarding=False))
    assert 'Выберите действие' in msg.answer.await_args.args[0]
    assert t.opening_question(456) is None


def test_plan_button_uses_existing_gated_ai_handler(monkeypatch):
    from aiogram import Bot
    from aiogram.types import Message, Chat, User, CallbackQuery
    from datetime import datetime,timezone
    from src.bot.handlers import mentor,new_user
    from src.ai import helpers
    monkeypatch.setattr(helpers,'check_blocked',AsyncMock(return_value=True))
    monkeypatch.setattr(helpers,'CHAT_NOT_BANNED_FILTER',AsyncMock(return_value=True))
    handler=AsyncMock();monkeypatch.setattr(new_user,'handle_single_ai_message',handler)
    async def run():
        bot=Bot('111:testtoken')
        user=User(id=456,is_bot=False,first_name='Test')
        msg=Message(message_id=20,date=datetime.now(timezone.utc),chat=Chat(id=456,type='private'),from_user=User(id=111,is_bot=True,first_name='Bot'),text='Menu').as_(bot)
        query=CallbackQuery(id='callback-plan',from_user=user,chat_instance='test',message=msg,data='mentor:daily_plan').as_(bot)
        await mentor.run_ai_action(query,SimpleNamespace(),bot,'premium-client','free-client','Составь план на сегодня')
        sent=handler.await_args.args[0]
        assert sent.from_user.id==456 and sent.text=='Составь план на сегодня'
        assert handler.await_args.args[2:] == (bot,'premium-client','free-client')
        await bot.session.close()
    asyncio.run(run())
