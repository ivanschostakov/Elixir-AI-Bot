"""Telegram mentor navigation; AI media stays on the existing gated transport."""
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
    mentor_enabled, save_opening_question, clear_opening_question, opening_question,
    reminder_was_delivered, remember_delivery, forget_delivery)

router = Router(name="telegram_mentor")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
log = logging.getLogger("telegram_mentor")


class SettingsInput(StatesGroup):
    timezone = State()


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
    return InlineKeyboardMarkup(inline_keyboard=[*rows, [button("← Обычный ИИ", "leave")]])


SECTIONS = {
    "food": ("🍽 Питание",[("📷 Добавить по фото","meal:photo"),("🎙 Добавить голосом","meal:voice"),("✍️ Добавить текстом","meal:text"),("⭕ Отправить кружок","meal:video"),
        ("♻️ Повторить прошлый приём","repeat_previous"),("⭐ Любимые блюда","favorites"),("🔎 Найти продукт","food_lookup"),("📊 Итоги за сегодня","nutrition"),("🍲 Что мне поесть?","suggest"),("📅 История питания","meals:0"),("Мои нормы КБЖУ","target")]),
    "progress": ("📊 Прогресс",[("⚖️ Добавить вес","weight"),("📏 Добавить замеры","measurement"),("📷 Добавить фото","progress_photo"),("📈 График веса","weight_chart"),("Последние измерения веса","history"),("Фото и замеры","measurements"),("🏋️ Прогресс тренировок","workout_results"),("📊 Отчёт за неделю","weekly"),("За месяц","monthly")]),
    "plan": ("🎯 Мой план",[("План на сегодня","daily_plan"),("Скорректировать план","adjust")]),
    "profile": ("Профиль",[("Мои данные и цель","data"),("Изменить цель","goals")]),
    "settings": ("Настройки",[("Напоминания","reminders"),("Часовой пояс","timezone"),("Приватность","privacy")]),
    "ask": ("💬 Спросить наставника",[("🍲 Что поесть?","suggest"),("📊 Проанализировать мой день","analyze_day"),("🏋️ Скорректировать тренировку","reason:workout"),("⚖️ Почему вес стоит?","reason:plateau"),("🧬 Вопрос по моему курсу","specialist"),("💬 Задать свой вопрос","question"),("✏️ Скорректировать план","adjust")])}


def section_keyboard(section):
    return InlineKeyboardMarkup(inline_keyboard=[*[ [button(label,action)] for label,action in SECTIONS[section][1]],
        [button("← Меню наставника","menu")]])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[button("← Меню наставника","menu")]])


def response_keyboard(response):
    keyboard=back_keyboard()
    draft=response.get("meal_draft")
    if draft:
        keyboard.inline_keyboard.insert(0,[button("✅ Всё верно",f"meal_confirm:{draft['id']}"),button("❌ Отменить",f"meal_cancel:{draft['id']}")])
        keyboard.inline_keyboard.insert(1,[button("✏️ Изменить",f"meal_edit:{draft['id']}"),button("➕ Добавить продукт",f"meal_add:{draft['id']}")])
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
            ("height_cm","Какой у вас рост?"),("age","Сколько вам лет?"),("sex","Укажите пол для расчёта КБЖУ: мужской или женский."),("target_weight_kg","Какого веса хотите достичь?"),
            ("activity","Какая у вас обычно физическая активность?")]
        questions = [(field, q) for field, q in questions if field != "target_weight_kg" or p.get("goal") in {"weight_loss", "weight_gain"}]
        question=next((q for field,q in questions if p.get(field) is None),None) if onboarding else None
        if question:
            text+="\n\nДозаполните профиль: "+question;save_opening_question(user_id,question)
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
    t=data.get("totals", {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0})
    target=data.get("workspace", {}).get("target") or {}
    lines=[f"📊 Ваши итоги за сегодня · {data.get('date', '')}", ""]
    for key,label,unit in [("kcal","Калории","ккал"),("protein","Белки","г"),("fat","Жиры","г"),("carbs","Углеводы","г")]:
        lines.append(f"{label}: ≈ {fmt(t[key])} / {fmt(target.get(key))} {unit}")
    lines += ["", "Учитываются только записанные приёмы пищи. КБЖУ — приблизительная оценка."]
    if not data.get("meals"):
        lines.insert(2, "Сегодня в дневнике ещё нет записанной еды.")
    remaining = data.get("workspace", {}).get("remaining")
    if remaining:
        labels = {"kcal": "ккал", "protein": "белка, г", "fat": "жиров, г", "carbs": "углеводов, г"}
        lines.append("\nДо сохранённой нормы: " + "; ".join(f"{fmt(v)} {labels[k]}" for k, v in remaining.items()))
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
    lines=[f"{label}: {values.get(str(p[key]),p[key])}" for key,label in labels.items() if p.get(key) is not None]
    return "👤 Мои данные и цель\n\n"+("\n".join(lines)[:3400] if lines else "Профиль пока не заполнен.")+"\n\nЧто хотите изменить? Напишите новые данные обычным сообщением."


async def show_reminders(message,uid):
    from .mentor_flows import reminders_view
    return await reminders_view(message, uid)


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
    from .mentor_panel import MentorPanel, navigate_page
    if query.data.startswith("mentor:page:"):
        return await navigate_page(query, state)
    panel = MentorPanel(query.message, state)
    try:
        await perform_action(query, state, panel, professor_bot, professor_client, expert_client)
    finally:
        await panel.flush()


async def perform_action(query,state,message,professor_bot=None,professor_client=None,expert_client=None):
    uid=query.from_user.id;action=query.data.removeprefix("mentor:")
    await query.answer()
    if action=="leave":
        from src.bot.keyboards import user_keyboards
        set_mentor_enabled(uid,False);await state.clear()
        clear_opening_question(uid,opening_question(uid))
        return await message.answer("Вы вернулись к обычному ИИ-ассистенту. Задайте свой вопрос.",reply_markup=user_keyboards.backk)
    if action in {"open","menu","start"}:
        return await enter(message,uid,state,onboarding=action!="menu")
    if not mentor_enabled(uid):
        return await message.answer("Откройте наставника, чтобы продолжить.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button("🌿 Наставник ElixirPeptide","open")]]))
    await state.set_state(None);clear_opening_question(uid,opening_question(uid))
    try:
        from .mentor_flows import dispatch
        if await dispatch(query, state, action, professor_bot, professor_client, expert_client, panel=message): return
        if action in SECTIONS:
            return await message.answer(SECTIONS[action][0],reply_markup=section_keyboard(action),parse_mode=None)
        if action=="meal" or action.startswith("meal:"):
            return await ask(message,uid,"Отправьте фотографию еды, голосовое сообщение, видеокружок или напишите, что вы съели. Например: «гречка 150 г, куриная грудка 200 г и овощной салат».\nДля кружка используйте кнопку видео в Telegram. Медиа доступны в режиме ИИ-профессора.")
        if action=="weight": return await ask(message,uid,"Сколько вы сейчас весите, в килограммах?")
        if action=="question": return await ask(message,uid,"Что хотите обсудить?")
        if action in {"nutrition","history","data"}:
            data=await api("/dashboard",{"telegram_user_id":uid})
            section={"nutrition":"food","history":"progress","data":"profile"}[action]
            text={"nutrition":today_text,"history":history_text,"data":lambda d:profile_text(d['profile'])}[action](data)
            if action=="data": save_opening_question(uid,"Какие данные профиля или цель хотите изменить?")
            kb=section_keyboard(section)
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
            await state.set_state(SettingsInput.timezone)
            return await message.answer("Введите часовой пояс, например Europe/Moscow, Asia/Yekaterinburg или America/Chicago.",reply_markup=back_keyboard())
        await message.answer("Эта старая кнопка больше не используется. Выберите действие в меню.",reply_markup=menu())
    except (BridgeError,ValueError) as error:
        text=str(error) if isinstance(error,BridgeError) else "Не удалось обработать действие. Откройте меню и попробуйте снова."
        await message.answer(text,reply_markup=back_keyboard(),parse_mode=None)


@router.message(SettingsInput.timezone)
async def timezone_input(message:Message,state:FSMContext):
    from .mentor_panel import reply_panel
    panel = await reply_panel(message, state)
    try:
        await save_timezone(panel if panel is not None else message, state)
    finally:
        if panel is not None: await panel.flush()


async def save_timezone(message,state):
    if not mentor_enabled(message.from_user.id):
        await state.clear()
        return
    value=(message.text or "").strip()
    try:
        ZoneInfo(value)
        settings=(await api("/dashboard",{"telegram_user_id":message.from_user.id}))['settings']
        from .mentor_flows import reminder_payload
        await api("/reminder/options",{**reminder_payload(settings),"telegram_user_id":message.from_user.id,"timezone":value})
    except (ValueError,KeyError,BridgeError):
        return await message.answer("Не удалось сохранить часовой пояс. Проверьте название, например Europe/Moscow, и попробуйте снова.",reply_markup=back_keyboard())
    await state.clear();await show_reminders(message,message.from_user.id)


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
                report=saved['workspace']['weekly']
                text="📊 Отчёт за неделю\n"+report['from']+" — "+report['to']+"\n"+"\n".join([
                    f"Изменение веса: {number(report.get('weight_change_kg'))} кг",
                    f"Средний вес: {number(report.get('weight_mean_7d'))} кг",
                    f"Средняя калорийность: {number(report.get('average_kcal'))} ккал · дней с записями: {report.get('nutrition_days', 0)}",
                    f"Белок к норме: {number(report.get('protein_target_percent'))}%",
                    f"Тренировки: {report['workouts']} · в плане {report.get('planned_workouts', 0)}",
                    f"Курс: {report['course_done']} / {report['course_due']}",
                    f"Энергия: {number(report.get('energy_mean'))} / 5",
                    "Продолжайте дневник и сравнивайте недельные средние. Пропущенные записи не считаются нулём."])
                image=await asyncio.to_thread(weight_chart_png, saved)
    if image is not None:
        await bot.send_photo(item['telegram_user_id'], BufferedInputFile(image, filename="weekly-weight.png"), caption=text, parse_mode=None, protect_content=True, reply_markup=markup)
    else:
        await bot.send_message(item['telegram_user_id'], text, parse_mode=None, protect_content=True, reply_markup=markup)


from .mentor_flows import router as flows_router
router.include_router(flows_router)
