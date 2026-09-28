"""Four-section mentor menu with a free-text conversation behind each action."""
import asyncio
import hashlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from aiogram import F, Router, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError
import config
from src.ai.telegram_mentor import (BridgeError, configured, api, set_mentor_enabled,
    mentor_enabled, save_opening_question, clear_opening_question, opening_question)

router = Router(name="telegram_mentor")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
log = logging.getLogger("telegram_mentor")


class SettingsInput(StatesGroup):
    timezone = State()


def button(label, action):
    return InlineKeyboardButton(text=label, callback_data="mentor:"+action)


def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("🍽 Питание","food"),button("⚖️ Мой прогресс","progress")],
        [button("🎯 Мой план","plan"),button("👤 Мой профиль","profile")],
        [button("← Обычный ИИ","leave")]])


SECTIONS = {
    "food": ("🍽 Питание",[("Добавить еду","meal"),("Итоги за сегодня","today"),("Что поесть?","suggest")]),
    "progress": ("⚖️ Мой прогресс",[("Записать вес","weight"),("Посмотреть динамику","history")]),
    "plan": ("🎯 Мой план",[("План на сегодня","daily_plan"),("Скорректировать план","adjust")]),
    "profile": ("👤 Мой профиль",[("Мои данные и цель","data"),("Напоминания","reminders")])}


def section_keyboard(section):
    return InlineKeyboardMarkup(inline_keyboard=[*[ [button(label,action)] for label,action in SECTIONS[section][1]],
        [button("← Меню наставника","menu")]])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[button("← Меню наставника","menu")]])


def response_keyboard(response):
    keyboard=back_keyboard()
    draft=response.get("meal_draft")
    if draft:
        keyboard.inline_keyboard.insert(0,[button("Записать в дневник",f"meal_confirm:{draft['id']}"),button("Не записывать",f"meal_cancel:{draft['id']}")])
    return keyboard


async def shop_button(bot):
    try: value=await bot.get_chat_menu_button()
    except Exception: value=None
    url=getattr(getattr(value,"web_app",None),"url",None) or config.env("TELEGRAM_SHOP_WEBAPP_URL","")
    if not url.startswith("https://"): raise BridgeError("WebApp магазина пока не настроен")
    return InlineKeyboardButton(text="🛒 Открыть магазин",web_app=WebAppInfo(url=url))


class MentorNavigationMiddleware(BaseMiddleware):
    async def __call__(self,handler,event,data):
        callback=getattr(event,"data",None) or ""
        message=getattr(event,"text",None) or ""
        command=message.split(maxsplit=1)[0].split("@")[0] if message else ""
        if callback in {"user:main_menu","user:main_menuu","user:ai:start","user:ai:free","user:ai:premium"} or command=="/start":
            set_mentor_enabled(event.from_user.id,False)
            clear_opening_question(event.from_user.id,opening_question(event.from_user.id))
            state=data.get("state")
            if state and await state.get_state()==SettingsInput.timezone.state: await state.clear()
        return await handler(event,data)


def fmt(value):
    return f"{float(value):g}" if value is not None else "не указан"


async def enter(message,user_id,state,*,onboarding=True):
    if not configured(): return await message.answer("Наставник пока недоступен. Попробуйте позже.")
    await state.clear();set_mentor_enabled(user_id,True)
    clear_opening_question(user_id,opening_question(user_id))
    try:
        saved=await api("/dashboard",{"telegram_user_id":user_id});p=saved.get("profile",{})
        text="🌿 Наставник по похудению\n\n"
        if p.get("current_weight_kg") is not None: text+=f"Последний вес: {fmt(p['current_weight_kg'])} кг.\n"
        if p.get("target_weight_kg") is not None: text+=f"Цель: {fmt(p['target_weight_kg'])} кг.\n"
        questions=[("goal","Какого результата по весу хотите достичь?"),("current_weight_kg","Сколько вы сейчас весите?"),
            ("height_cm","Какой у вас рост?"),("age","Сколько вам лет?"),("target_weight_kg","Какого веса хотите достичь?"),
            ("activity","Какая у вас обычно физическая активность?")]
        question=next((q for field,q in questions if not p.get(field)),None) if onboarding else None
        if question:
            text+="\n"+question;save_opening_question(user_id,question)
        else: text+="\nВыберите действие или просто напишите сообщение."
    except BridgeError:
        text="🌿 Наставник по похудению\n\nПрофиль сейчас не загрузился. Можно продолжить разговор или попробовать открыть меню позже."
    await message.answer(text,reply_markup=menu(),parse_mode=None)


@router.message(Command("mentor"))
async def mentor_command(message:Message,state:FSMContext):
    await enter(message,message.from_user.id,state)


async def ask(message,uid,text):
    save_opening_question(uid,text)
    await message.answer(text,reply_markup=back_keyboard(),parse_mode=None)


def today_text(data):
    if not data.get("meals"): return "Сегодня в дневнике ещё нет записанной еды. Нажмите «Добавить еду» и пришлите описание или фото."
    t=data["totals"]
    lines=[f"🍽 Сегодня · {data['date']}","",*[f"• {m['name']} — ≈ {fmt(m['kcal'])} ккал" for m in data["meals"][-12:]],"",
        f"Итого: ≈ {fmt(t['kcal'])} ккал",f"Б {fmt(t['protein'])} г · Ж {fmt(t['fat'])} г · У {fmt(t['carbs'])} г",
        "Учитываются только записанные приёмы пищи. КБЖУ — приблизительная оценка."]
    return "\n".join(lines)


def history_text(data):
    weights=data.get("weights",[])[-10:]
    if not weights:
        current=data.get("profile",{}).get("current_weight_kg")
        return (f"В профиле: {fmt(current)} кг.\n" if current is not None else "")+"Истории измерений пока нет. Запишите вес — начнём отслеживать динамику."
    zone=ZoneInfo(data["settings"]["timezone"])
    lines=["⚖️ Динамика веса",""]
    for w in weights[-10:]:
        date=datetime.fromisoformat(w['occurred_at']).astimezone(zone).strftime('%d.%m %H:%M')
        lines.append(f"{date} — {fmt(w['weight_kg'])} кг")
    if len(weights)>1:
        diff=weights[-1]['weight_kg']-weights[0]['weight_kg']
        lines.append(f"\nИзменение за показанную историю: {diff:+g} кг")
    else: lines.append("\nДля сравнения нужно ещё одно измерение.")
    return "\n".join(lines)


def profile_text(p):
    labels={"goal":"Цель","age":"Возраст","sex":"Пол","height_cm":"Рост, см","current_weight_kg":"Вес, кг",
        "target_weight_kg":"Целевой вес, кг","activity":"Активность","preferences":"Предпочтения","restrictions":"Ограничения"}
    values={"weight_loss":"снижение веса","maintain":"поддержание","weight_gain":"набор веса","male":"мужской","female":"женский"}
    lines=[f"{label}: {values.get(str(p[key]),p[key])}" for key,label in labels.items() if p.get(key) is not None]
    return "👤 Мои данные и цель\n\n"+("\n".join(lines)[:3400] if lines else "Профиль пока не заполнен.")+"\n\nЧто хотите изменить? Напишите новые данные обычным сообщением."


async def show_reminders(message,uid):
    data=await api("/dashboard",{"telegram_user_id":uid});settings=data['settings']
    status=f"Каждый день в {settings['daily_time']}" if settings['daily_time'] else "Выключены"
    text=f"🔔 Вечерний итог\n\n{status}\nЧасовой пояс: {settings['timezone']}\n\nВыберите время напоминания:"
    kb=InlineKeyboardMarkup(inline_keyboard=[[button("19:00","remind:19:00"),button("20:00","remind:20:00"),button("21:00","remind:21:00")],
        [button("Изменить часовой пояс","timezone")],[button("Выключить","remind:off")],[button("← Мой профиль","profile")]])
    await message.answer(text,reply_markup=kb,parse_mode=None)


async def run_ai_action(query,state,professor_bot,professor_client,expert_client,text):
    from src.ai.helpers import check_blocked, CHAT_NOT_BANNED_FILTER
    from src.bot.handlers.new_user import handle_single_ai_message
    if not await check_blocked(query) or not await CHAT_NOT_BANNED_FILTER(query): return
    # Same model/phone/quota/usage path as typed messages; only the chosen prompt is synthetic.
    message=query.message.model_copy(update={"from_user":query.from_user,"text":text,"caption":None,
        "photo":None,"document":None,"video":None,"voice":None,"video_note":None,
        "message_id":1_000_000_000+int(hashlib.sha256(query.id.encode()).hexdigest()[:8],16)%1_000_000_000})
    message.as_(query.bot)
    await handle_single_ai_message(message,state,professor_bot,professor_client,expert_client)


@router.callback_query(F.data.startswith("mentor:"))
async def mentor_action(query:CallbackQuery,state:FSMContext,professor_bot=None,professor_client=None,expert_client=None):
    uid=query.from_user.id;action=query.data.removeprefix("mentor:")
    await query.answer()
    if action=="leave":
        from src.bot.keyboards import user_keyboards
        set_mentor_enabled(uid,False);await state.clear()
        clear_opening_question(uid,opening_question(uid))
        return await query.message.answer("Вы вернулись к обычному ИИ-ассистенту. Задайте свой вопрос.",reply_markup=user_keyboards.backk)
    if action in {"open","menu","start"}:
        return await enter(query.message,uid,state,onboarding=action!="menu")
    if not mentor_enabled(uid):
        return await query.message.answer("Откройте наставника, чтобы продолжить.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button("🌿 Наставник по похудению","open")]]))
    await state.clear();clear_opening_question(uid,opening_question(uid))
    try:
        if action in SECTIONS:
            return await query.message.answer(SECTIONS[action][0],reply_markup=section_keyboard(action),parse_mode=None)
        if action=="meal": return await ask(query.message,uid,"Что вы ели? Пришлите описание, фото или голосовое. Для оценки укажите примерную порцию. Фото и голос доступны в режиме ИИ-профессора.")
        if action=="weight": return await ask(query.message,uid,"Сколько вы сейчас весите, в килограммах?")
        if action=="adjust": return await ask(query.message,uid,"Что в текущем плане неудобно или не получается?")
        if action in {"today","history","data"}:
            data=await api("/dashboard",{"telegram_user_id":uid})
            section={"today":"food","history":"progress","data":"profile"}[action]
            text={"today":today_text,"history":history_text,"data":lambda d:profile_text(d['profile'])}[action](data)
            if action=="data": save_opening_question(uid,"Какие данные профиля или цель хотите изменить?")
            return await query.message.answer(text,reply_markup=section_keyboard(section),parse_mode=None)
        if action in {"daily_plan","suggest"}:
            text={"daily_plan":"Составь мой короткий практический план на сегодня по сохранённому профилю и дневнику. Используй имеющиеся данные, не начинай анкету заново.",
                  "suggest":"Что мне поесть сегодня с учётом моего профиля, ограничений и записанной еды? Предложи несколько простых вариантов, не записывая их в дневник."}[action]
            return await run_ai_action(query,state,professor_bot,professor_client,expert_client,text)
        if action.startswith(("meal_confirm:","meal_cancel:")):
            kind,value=action.split(":",1)
            result=await api("/journal/action",{"telegram_user_id":uid,"entry_id":int(value),"action":"confirm" if kind=="meal_confirm" else "cancel"})
            text="Добавлено в дневник. Итоги за сегодня обновлены." if result['entry']['status']=='confirmed' else "Оценка не записана в дневник."
            await query.message.edit_reply_markup(reply_markup=back_keyboard())
            return await query.message.answer(text,reply_markup=section_keyboard('food'))
        if action=="reminders": return await show_reminders(query.message,uid)
        if action.startswith("remind:"):
            value=action.removeprefix("remind:")
            settings=(await api("/dashboard",{"telegram_user_id":uid}))['settings']
            await api("/reminder/settings",{"telegram_user_id":uid,"timezone":settings['timezone'],"daily_time":None if value=='off' else value})
            return await show_reminders(query.message,uid)
        if action=="timezone":
            await state.set_state(SettingsInput.timezone)
            return await query.message.answer("Введите часовой пояс, например Europe/Moscow, Asia/Yekaterinburg или America/Chicago.",reply_markup=back_keyboard())
        await query.message.answer("Эта старая кнопка больше не используется. Выберите действие в меню.",reply_markup=menu())
    except (BridgeError,ValueError) as error:
        text=str(error) if isinstance(error,BridgeError) else "Не удалось обработать действие. Откройте меню и попробуйте снова."
        await query.message.answer(text,reply_markup=back_keyboard(),parse_mode=None)


@router.message(SettingsInput.timezone)
async def timezone_input(message:Message,state:FSMContext):
    if not mentor_enabled(message.from_user.id):
        await state.clear()
        return
    value=(message.text or "").strip()
    try:
        ZoneInfo(value)
        settings=(await api("/dashboard",{"telegram_user_id":message.from_user.id}))['settings']
        await api("/reminder/settings",{"telegram_user_id":message.from_user.id,"timezone":value,"daily_time":settings['daily_time']})
    except (ValueError,KeyError,BridgeError):
        return await message.answer("Не удалось сохранить часовой пояс. Проверьте название, например Europe/Moscow, и попробуйте снова.",reply_markup=back_keyboard())
    await state.clear();await show_reminders(message,message.from_user.id)


async def reminder_loop(bot):
    while True:
        try:
            if configured():
                result=await api("/reminder/due",{})
                for item in result.get("items",[]):
                    try:
                        await bot.send_message(item['telegram_user_id'],"🌿 Как прошёл день? Можно подвести итог питания или обсудить, что получилось.",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button("Открыть наставника","open")]]))
                    except TelegramForbiddenError:
                        settings=(await api("/dashboard",item))['settings']
                        await api("/reminder/settings",{**item,"timezone":settings['timezone'],"daily_time":None})
                    except Exception:
                        log.warning("Mentor reminder delivery failed")
        except Exception:
            log.warning("Mentor reminder poll failed")
        await asyncio.sleep(60)
