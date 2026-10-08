"""Telegram mentor navigation; AI media stays on the existing gated transport."""
import asyncio
import hashlib
import logging
import secrets
import time
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
    mentor_enabled, save_opening_question, clear_opening_question, opening_question,
    reminder_was_delivered, remember_delivery, forget_delivery)

router = Router(name="telegram_mentor")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
log = logging.getLogger("telegram_mentor")


class SettingsInput(StatesGroup):
    timezone = State()


class WeightInput(StatesGroup):
    value = State()


def button(label, action):
    return InlineKeyboardButton(text=label, callback_data="mentor:"+action)


def menu(gates=None):
    gates = gates or {}
    closed = {x.strip() for x in config.env("TELEGRAM_MENTOR_CLOSED_SECTIONS", "").split(",")}
    items = [("📅 Сегодня", "today"), ("🍽 Питание", "food"), ("🏋️ Тренировки", "workouts"),
             ("🧬 Мой курс", "course"), ("📊 Прогресс", "progress"), ("💬 Спросить наставника", "ask"),
             ("👤 Профиль", "profile"), ("⚙️ Настройки", "settings")]
    visible = [button(label, action) for label, action in items if gates.get(action, True) and action not in closed]
    rows = [[button("➕ Добавить еду", "meal")]] if gates.get("food", True) and "food" not in closed else []
    rows += [visible[i:i+2] for i in range(0, len(visible), 2)]
    return InlineKeyboardMarkup(inline_keyboard=[*rows, [button("🏠 Главное меню", "leave")]])


SECTIONS = {
    "food": ("🍽 Питание",[("➕ Добавить еду","meal"),
        ("♻️ Повторить прошлый приём","repeat_previous"),("⭐ Любимые блюда","favorites"),("🔎 Найти продукт","food_lookup"),("📊 Итоги за сегодня","nutrition"),("🍲 Что мне поесть?","suggest"),("📅 История питания","meals:0"),("🎯 Мои нормы КБЖУ","target")]),
    "progress": ("📊 Прогресс",[("⚖️ Добавить вес","weight"),("📏 Добавить замеры","measurement"),("📷 Добавить фото","progress_photo"),("📈 График веса","weight_chart"),("Последние измерения веса","history"),("Фото и замеры","measurements"),("🏋️ Прогресс тренировок","workout_results"),("📊 Отчёт за неделю","weekly"),("За месяц","monthly")]),
    "plan": ("🎯 Мой план",[("План на сегодня","daily_plan"),("Скорректировать план","adjust")]),
    "profile": ("Профиль",[("Мои данные и цель","data"),("Изменить цель","goals")]),
    "settings": ("Настройки",[("Напоминания","reminders"),("Часовой пояс","timezone"),("Приватность","privacy")]),
    "ask": ("💬 Спросить наставника",[("🍲 Что поесть?","suggest"),("📊 Проанализировать мой день","analyze_day"),("🏋️ Скорректировать тренировку","reason:workout"),("⚖️ Почему вес стоит?","reason:plateau"),("🧬 Вопрос по моему курсу","specialist"),("💬 Задать свой вопрос","question"),("✏️ Скорректировать план","adjust")])}


def section_keyboard(section, profile=None):
    items = [("⚖️ Почему вес не растёт?" if action == "reason:plateau" and (profile or {}).get("goal") == "weight_gain" else label, action)
        for label, action in SECTIONS[section][1]]
    return InlineKeyboardMarkup(inline_keyboard=[*[ [button(label,action)] for label,action in items],
        [button("← Меню наставника","menu")]])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[button("← Меню наставника","menu")]])


def response_keyboard(response):
    keyboard=back_keyboard()
    draft=response.get("meal_draft")
    if draft:
        keyboard.inline_keyboard.insert(0,[button("✅ Записать в дневник",f"meal_confirm:{draft['id']}"),button("❌ Отменить",f"meal_cancel:{draft['id']}")])
        keyboard.inline_keyboard.insert(1,[button("✏️ Изменить",f"meal_edit:{draft['id']}"),button("➕ Добавить продукт",f"meal_add:{draft['id']}")])
    for key, label in (("target_draft", "Сохранить как мою норму"), ("program_draft", "Сохранить программу")):
        if response.get(key):
            entry = response[key]
            keyboard.inline_keyboard.insert(0, [button(label, f"record:confirm:{entry['id']}"), button("Отмена", f"record:cancel:{entry['id']}")])
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
            if mentor_enabled(event.from_user.id) and configured():
                try:
                    await api("/workspace/workout/discard-empty", {"telegram_user_id": event.from_user.id})
                except BridgeError:
                    log.warning("Could not discard empty mentor workout on exit")
            set_mentor_enabled(event.from_user.id,False)
            clear_opening_question(event.from_user.id,opening_question(event.from_user.id))
            state=data.get("state")
            if state: await state.clear()
        return await handler(event,data)


def fmt(value):
    return f"{float(value):,.2f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",") if value is not None else "не указан"


async def enter(message,user_id,state,*,onboarding=True):
    if not configured(): return await message.answer("Наставник пока недоступен. Попробуйте позже.")
    await state.clear();set_mentor_enabled(user_id,True)
    clear_opening_question(user_id,opening_question(user_id))
    try:
        saved=await api("/dashboard",{"telegram_user_id":user_id});p=saved.get("profile",{})
        from .mentor_flows import home_view
        text=home_view(saved)
        questions=[("goal","Какого результата по весу хотите достичь?"),("current_weight_kg","Сколько вы сейчас весите?"),
            ("height_cm","Какой у вас рост?"),("age","Сколько вам лет?"),("sex","Для расчёта КБЖУ уточню: вы мужчина или женщина?"),("target_weight_kg","Какого веса хотите достичь?"),
            ("activity","Какая у вас обычно физическая активность?")]
        questions = [(field, q) for field, q in questions if field != "target_weight_kg" or p.get("goal") in {"weight_loss", "weight_gain"}]
        question=next((q for field,q in questions if p.get(field) is None),None) if onboarding else None
        if question:
            text+="\n\n"+question;save_opening_question(user_id,question)
        else: text+="\nВыберите действие или просто напишите сообщение."
    except BridgeError:
        saved = {}
        text="Наставник ElixirPeptide\n\nПрофиль сейчас не загрузился. Можно продолжить разговор или попробовать открыть меню позже."
    await message.answer(text,reply_markup=menu(saved.get("workspace", {}).get("sections")),parse_mode=None)


@router.message(Command("mentor"))
async def mentor_command(message:Message,state:FSMContext):
    await enter(message,message.from_user.id,state)


async def ask(message,uid,text):
    save_opening_question(uid,text)
    await message.answer(text,reply_markup=back_keyboard(),parse_mode=None)


def today_text(data):
    from .mentor_format import day
    t=data.get("totals", {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0})
    target=data.get("workspace", {}).get("target") or {}
    lines=[f"📊 Ваши итоги за сегодня · {day(data.get('date', ''))}", ""]
    for key,label,unit in [("kcal","Калории","ккал"),("protein","Белки","г"),("fat","Жиры","г"),("carbs","Углеводы","г")]:
        value = f"{fmt(t[key])} / {fmt(target[key])} {unit}" if target.get(key) is not None else f"{fmt(t[key])} {unit} (норма не задана)"
        lines.append(f"{label}: ≈ {value}")
    lines += ["", "Учитываются только записанные приёмы пищи. КБЖУ — приблизительная оценка."]
    if not data.get("meals"):
        lines.insert(2, "Сегодня в дневнике ещё нет записанной еды.")
    remaining = data.get("workspace", {}).get("remaining")
    if remaining:
        labels = {"kcal": ("калории", "ккал"), "protein": ("белки", "г"), "fat": ("жиры", "г"), "carbs": ("углеводы", "г")}
        lines.append("\nДо сохранённой нормы: " + "; ".join(f"{labels[k][0]} {fmt(v)} {labels[k][1]}" for k, v in remaining.items()))
    else:
        lines.append("\nЧисловая норма не сохранена. Остаток не рассчитан.")
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
    labels={"goal":"Цель","goal_detail":"Подробности цели","age":"Возраст","sex":"Пол","height_cm":"Рост, см","current_weight_kg":"Вес, кг",
        "target_weight_kg":"Целевой вес, кг","activity":"Активность","preferences":"Предпочтения","restrictions":"Ограничения"}
    values={"weight_loss":"снижение веса","maintain":"поддержание","weight_gain":"набор веса","custom":"своя цель","male":"мужской","female":"женский"}
    lines=[f"{label}: {fmt(p[key]) if isinstance(p[key], (int, float)) else values.get(str(p[key]),p[key])}" for key,label in labels.items() if p.get(key) is not None]
    if p.get("sex") is None: lines.append("Пол: не указан")
    return "👤 Мои данные и цель\n\n"+("\n".join(lines)[:3400] if lines else "Профиль пока не заполнен.")+"\n\nЧто хотите изменить? Напишите новые данные обычным сообщением."


async def show_reminders(message,uid):
    from .mentor_flows import reminders_view
    return await reminders_view(message, uid)


async def run_ai_action(query,state,professor_bot,professor_client,expert_client,text):
    from src.ai.helpers import check_blocked, CHAT_NOT_BANNED_FILTER
    from src.bot.handlers.new_user import handle_single_ai_message
    if not await check_blocked(query) or not await CHAT_NOT_BANNED_FILTER(query): return
    # Same model/phone/quota/usage path as typed messages; only the chosen prompt is synthetic.
    started = time.monotonic()
    status = await query.message.answer("⏳ Подбираю…" if query.data == "mentor:suggest" else "⏳ Считаю…", parse_mode=None)
    message=query.message.model_copy(update={"from_user":query.from_user,"text":text,"caption":None,
        "photo":None,"document":None,"video":None,"voice":None,"video_note":None,
        "message_id":status.message_id})
    message.as_(query.bot)
    retry = InlineKeyboardMarkup(inline_keyboard=[[button("Повторить", query.data.removeprefix("mentor:"))], [button("← Меню наставника", "menu")]])
    try:
        result = await handle_single_ai_message(message,state,professor_bot,professor_client,expert_client)
    except Exception:
        log.exception("Mentor AI callback failed | action=%s", query.data.split(":")[1])
        await status.edit_text("Не удалось получить ответ. Попробуйте ещё раз.", parse_mode=None,
            reply_markup=retry)
    else:
        if result is None:
            await status.edit_text("Ответ не получен. Попробуйте ещё раз.", parse_mode=None, reply_markup=retry)
        else:
            try: await status.delete()
            except Exception: log.warning("Mentor progress message could not be removed")
    finally:
        log.info("Mentor AI callback timing | action=%s | elapsed_ms=%d", query.data.split(":")[1], (time.monotonic()-started)*1000)


@router.callback_query(F.data.startswith("mentor:"))
async def mentor_action(query:CallbackQuery,state:FSMContext,professor_bot=None,professor_client=None,expert_client=None):
    from .mentor_panel import MentorPanel, navigate_page
    if query.data.startswith("mentor:page:"):
        return await navigate_page(query, state)
    panel = MentorPanel(query.message, state, saved_card=(await state.get_data()).get("mentor_panel", {}))
    try:
        await perform_action(query, state, panel, professor_bot, professor_client, expert_client)
    finally:
        await panel.flush()


async def perform_action(query,state,message,professor_bot=None,professor_client=None,expert_client=None):
    uid=query.from_user.id;action=query.data.removeprefix("mentor:")
    await query.answer()
    # Navigation invalidates in-flight extraction; only its own review buttons keep a pending write.
    await state.update_data(form_token=secrets.token_hex(8),
        **({} if action.startswith(("input_save:", "input_edit:")) else {"pending_input": None}))
    if action=="leave":
        from src.bot.keyboards import user_keyboards
        from src.bot.texts import user_texts
        await api("/workspace/workout/discard-empty", {"telegram_user_id": uid})
        set_mentor_enabled(uid,False);await state.clear()
        clear_opening_question(uid,opening_question(uid))
        return await message.answer(user_texts.greetings.replace('full_name', query.from_user.full_name),reply_markup=user_keyboards.main_menu)
    if action in {"open","menu","start"}:
        await api("/workspace/workout/discard-empty", {"telegram_user_id": uid})
        return await enter(message,uid,state,onboarding=action!="menu")
    if not mentor_enabled(uid):
        return await message.answer("Откройте наставника, чтобы продолжить.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button("🌿 Наставник ElixirPeptide","open")]]))
    await state.set_state(None);clear_opening_question(uid,opening_question(uid))
    try:
        from .mentor_flows import dispatch
        if await dispatch(query, state, action, professor_bot, professor_client, expert_client, panel=message): return
        if action in SECTIONS:
            saved = await api("/dashboard", {"telegram_user_id": uid}) if action == "ask" else {}
            return await message.answer(SECTIONS[action][0],reply_markup=section_keyboard(action, saved.get("profile")),parse_mode=None)
        if action=="meal" or action.startswith("meal:"):
            return await ask(message,uid,"Что вы съели? Расскажите или пришлите фото, голосовое сообщение либо видеокружок. Медиа доступны в режиме ИИ-профессора.")
        if action=="weight":
            from .mentor_flows import form
            return await form(message, state, "weight", "Сколько вы сейчас весите?")
        if action=="question": return await ask(message,uid,"Что хотите обсудить?")
        if action in {"nutrition","history","data"}:
            data=await api("/dashboard",{"telegram_user_id":uid})
            section={"nutrition":"food","history":"progress","data":"profile"}[action]
            text={"nutrition":today_text,"history":history_text,"data":lambda d:profile_text(d['profile'])}[action](data)
            if action=="data": save_opening_question(uid,"Какие данные профиля или цель хотите изменить?")
            kb=section_keyboard(section)
            if action == "data":
                kb.inline_keyboard.insert(0, [button("Мужской", "sex:male"), button("Женский", "sex:female")])
            if action=="nutrition":
                from .mentor_flows import keyboard
                kb=keyboard([("🍽 Что поесть?","suggest")],[("📋 Все приёмы пищи","meals:0")],[("📷 Добавить еду","meal")])
            return await message.answer(text,reply_markup=kb,parse_mode=None)
        if action in {"daily_plan","suggest"}:
            text={"daily_plan":"Составь мой короткий практический план на сегодня по сохранённому профилю и дневнику. Используй имеющиеся данные, не начинай анкету заново.",
                  "suggest":"Что мне поесть сегодня с учётом моего профиля, ограничений и записанной еды? Предложи несколько простых вариантов, не записывая их в дневник."}[action]
            return await run_ai_action(query,state,professor_bot,professor_client,expert_client,text)
        if action.startswith(("meal_confirm:","meal_cancel:")):
            kind,value=action.split(":",1)
            result=await api("/journal/action",{"telegram_user_id":uid,"entry_id":int(value),"action":"confirm" if kind=="meal_confirm" else "cancel"})
            text="Добавлено в дневник. Итоги за сегодня обновлены." if result['entry']['status']=='confirmed' else "Оценка не записана в дневник."
            if result['entry']['status']=='confirmed':
                entry=result['entry']
                text=f"Добавлено: {entry['name']}\n≈ {fmt(entry['kcal'])} ккал\nБелки: {fmt(entry['protein'])} г\nЖиры: {fmt(entry['fat'])} г\nУглеводы: {fmt(entry['carbs'])} г"
                saved=await api("/dashboard", {"telegram_user_id":uid})
                remaining=saved.get("workspace", {}).get("remaining") or {}
                if remaining:
                    text+="\n\nОсталось: "+"; ".join(f"{fmt(remaining[k])} {unit}" for k,unit in [("kcal","ккал"),("protein","г белка")] if k in remaining)
            return await message.answer(text,reply_markup=section_keyboard('food'))
        if action=="reminders": return await show_reminders(message,uid)
        if action.startswith("remind:"):
            value=action.removeprefix("remind:")
            settings=(await api("/dashboard",{"telegram_user_id":uid}))['settings']
            await api("/reminder/settings",{"telegram_user_id":uid,"timezone":settings['timezone'],"daily_time":None if value=='off' else value})
            return await show_reminders(message,uid)
        if action=="timezone":
            from .mentor_flows import form
            return await form(message, state, "timezone", "В каком городе вы живёте? Подстрою время напоминаний.")
        await message.answer("Эта старая кнопка больше не используется. Выберите действие в меню.",reply_markup=menu())
    except (BridgeError,ValueError) as error:
        text=str(error) if isinstance(error,BridgeError) else "Не удалось обработать действие. Откройте меню и попробуйте снова."
        await message.answer(text,reply_markup=back_keyboard(),parse_mode=None)


@router.message(WeightInput.value)
async def save_weight(message: Message, state: FSMContext, professor_client=None):
    from .mentor_flows import receive
    await state.update_data(form_kind="weight", form_prompt="Сколько вы сейчас весите?")
    await receive(message, state, professor_client)


@router.message(SettingsInput.timezone)
async def timezone_input(message:Message,state:FSMContext,professor_client=None):
    await save_timezone(message, state, professor_client)


async def save_timezone(message,state,professor_client=None):
    from .mentor_flows import receive
    await state.update_data(form_kind="timezone", form_prompt="В каком городе вы живёте? Подстрою время напоминаний.")
    await receive(message, state, professor_client)


async def reminder_loop(bot):
    while True:
        try:
            if configured():
                result=await api("/reminder/due",{})
                for item in result.get("items",[]):
                    receipt = {k: item[k] for k in ("telegram_user_id", "delivery_id", "token")}
                    try:
                        if not (await api("/reminder/check", {**receipt, "outcome": "sent"})).get("deliver"):
                            continue
                        if not reminder_was_delivered(item['telegram_user_id'], item['delivery_id']):
                            await send_reminder(bot, item)
                            remember_delivery(item['telegram_user_id'], item['delivery_id'])
                        await api("/reminder/ack", {**receipt, "outcome": "sent"})
                        forget_delivery(item['telegram_user_id'], item['delivery_id'])
                    except TelegramForbiddenError:
                        await api("/reminder/ack", {**receipt, "outcome": "blocked"})
                    except Exception:
                        log.warning("Mentor reminder delivery failed")
                        try: await api("/reminder/ack", {**receipt, "outcome": "retry"})
                        except Exception: log.warning("Mentor reminder retry ACK failed; lease will expire")
        except Exception:
            log.warning("Mentor reminder poll failed")
        await asyncio.sleep(60)


async def send_reminder(bot, item):
    from aiogram.types import BufferedInputFile
    from .mentor_flows import fmt as number, weight_chart_png
    markup=InlineKeyboardMarkup(inline_keyboard=[[button("Открыть наставника", "open")]])
    text=item['text']
    image=None
    if item['kind'] in {"evening", "weekly"}:
        saved=await api("/dashboard", {"telegram_user_id": item['telegram_user_id']})
        if saved.get("settings", {}).get("reminders", {}).get("detailed_reports"):
            if item['kind']=="evening":
                text=today_text(saved)
            else:
                from .mentor_flows import weekly_view
                text=weekly_view(saved)
                image=await asyncio.to_thread(weight_chart_png, saved)
    if image is not None:
        short = len(text.encode("utf-16-le")) // 2 <= 1000
        await bot.send_photo(item['telegram_user_id'], BufferedInputFile(image, filename="weekly-weight.png"), caption=text if short else "Динамика сохранённых измерений веса.", parse_mode=None, protect_content=True, reply_markup=markup if short else None)
        if not short:
            for start in range(0, len(text), 2000):
                await bot.send_message(item['telegram_user_id'], text[start:start+2000], parse_mode=None, protect_content=True, reply_markup=markup if start+2000 >= len(text) else None)
    else:
        await bot.send_message(item['telegram_user_id'], text, parse_mode=None, protect_content=True, reply_markup=markup)


from .mentor_flows import router as flows_router
router.include_router(flows_router)
