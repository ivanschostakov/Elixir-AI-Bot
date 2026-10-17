"""Resumable introduction; profile facts and nutrition remain backend-owned."""
import json
import re
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.types import InlineKeyboardMarkup

from src.ai import telegram_mentor as bridge
from src.ai.mentor_input import OnboardingProfile, extract_answer, input_lock
from src.ai.webapp_client import WebappBotApiError
from .mentor_format import ACTIVITY, GOALS, timezone_label
from .mentor_onboarding_ui import FIELDS, LABELS, NUTRITION_FIELDS, PROMPTS, display, question, summary
import config



def load(uid):
    with bridge._state_db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS onboarding (user_id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        row = db.execute("SELECT data FROM onboarding WHERE user_id=?", (uid,)).fetchone()
    return json.loads(row[0]) if row else {}


def save(uid, values):
    with bridge._state_db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS onboarding (user_id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        db.execute("INSERT INTO onboarding VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
                   (uid, json.dumps(values, ensure_ascii=False)))


def erase(uid):
    load(uid)
    with bridge._state_db() as db:
        db.execute("DELETE FROM onboarding WHERE user_id=?", (uid,))


def active(uid):
    return bridge.mentor_enabled(uid) and load(uid).get("status") == "active"


def pause(uid):
    value = load(uid)
    if value.get("status") == "active":
        save(uid, {**value, "status": "paused", "token": secrets.token_hex(4)})


def current(uid, token):
    value = load(uid)
    return value.get("status") == "active" and value.get("token") == token and bridge.mentor_enabled(uid)


def record_timezone_choice(uid, zone):
    value = load(uid)
    if value.pop("timezone_only", False):
        value.update(status="complete", step=None)
    elif value.get("step") in {"timezone", "timezone_confirm"}:
        value.update(status="paused", step=None)
    save(uid, {**value, "timezone": zone, "timezone_done": True, "token": secrets.token_hex(4)})


async def section_access(message, uid, step):
    data = await bridge.api("/dashboard", {"telegram_user_id": uid})
    closed = {item.strip() for item in config.env("TELEGRAM_MENTOR_CLOSED_SECTIONS", "").split(",")}
    section = "settings" if step in {"timezone", "timezone_confirm"} else "food" if step in {"target", "eligibility"} else "profile"
    if section in closed or not data.get("workspace", {}).get("sections", {}).get(section, True):
        pause(uid)
        from .mentor import back_keyboard
        await message.answer("Этот раздел сейчас недоступен. Остальные возможности находятся в меню наставника.",
                             reply_markup=back_keyboard(), parse_mode=None)
        return False
    return True


def needs_profile(data):
    profile = data.get("profile", {})
    return any(profile.get(k) is None for k in FIELDS) or profile.get("activity") not in ACTIVITY


def markup(token, choices=(), *, skippable=False, back=False):
    from .mentor import button
    rows = [[button(label, f"setup:{token}:{value}")] for label, value in choices]
    if skippable:
        rows.append([button("Пропустить этот шаг", f"setup:{token}:skip")])
    if back:
        rows.append([button("Назад", f"setup:{token}:back")])
    rows.append([button("Продолжить позже", f"setup:{token}:later")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def offer(message, uid):
    from .mentor import button
    value = load(uid)
    token = secrets.token_hex(4)
    save(uid, {**value, "token": token, "status": "paused"})
    await message.answer(
        "Наставник поможет вести питание, следить за активностью и замечать изменения. "
        "Знакомство займёт примерно 3–5 минут: я задам вопросы по одному, затем предложу ориентир калорий, белков, жиров и углеводов.\n\n"
        "Можно отвечать текстом или голосом, а где есть варианты — кнопками. "
        "Ответы сохраняются: уже известное повторять не нужно. Можно вернуться назад, исправить ответ или продолжить позже.",
        parse_mode=None, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [button("Продолжить знакомство" if value.get("step") else "Начать знакомство", f"setup:{token}:begin")],
            [button("Пока открыть меню", "menu")]]))


async def confirm_existing_timezone(message, uid, data):
    value = load(uid)
    zone = data.get("settings", {}).get("timezone")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        zone = None
    save(uid, {**value, "timezone_only": True, "timezone": zone,
               "step": "timezone_confirm" if zone else "timezone"})
    await resume(message, uid)


async def advance(message, uid, *, data=None):
    from .mentor_panel import MentorPanel, ReplyCards
    if isinstance(message, (MentorPanel, ReplyCards)):
        message.kind = "form"
    value = load(uid)
    data = data or await bridge.api("/dashboard", {"telegram_user_id": uid})
    if not current(uid, value.get("token")):
        return
    profile = data.get("profile", {})
    if value.get("editing"):
        return await review(message, uid, data=data)
    if value.get("timezone_only"):
        if value.get("timezone_done"):
            return await finish(message, uid, "Часовой пояс подтверждён: "+timezone_label(value["timezone"])+
                                ". Профиль и настройки напоминаний сохранены.")
        step = "timezone"
    else:
        step = next((key for key in FIELDS if profile.get(key) is None or key == "activity" and profile.get(key) not in ACTIVITY), None)
        if not step and not value.get("preferences_done") and not profile.get("preferences"):
            step = "preferences"
    if not step and not value.get("timezone_done"):
        step = "timezone"
    if not step:
        return await review(message, uid, data=data)
    pending = [key for key in FIELDS if profile.get(key) is None or key == "activity" and profile.get(key) not in ACTIVITY]
    if not value.get("preferences_done") and not profile.get("preferences"):
        pending.append("preferences")
    if not value.get("timezone_done"):
        pending.append("timezone")
    visited = value.get("visited", [])
    route = [key for key in value.get("route", []) if key != "review" and (key in visited or key in pending)]
    route.extend(key for key in pending if key not in route)
    route.append("review")
    save(uid, {**value, "route": route})
    await show_question(message, uid, step, data=data)


async def show_question(message, uid, step, *, data=None):
    value = load(uid)
    data = data or await bridge.api("/dashboard", {"telegram_user_id": uid})
    if load(uid).get("token") != value.get("token"):
        return
    if step != value.get("step"):
        value.update(answers=[], answers_step=step, clarification=None)
    token = secrets.token_hex(4)
    value = {**value, "status": "active", "step": step, "token": token,
             "visited": list(dict.fromkeys([*value.get("visited", []), step]))}
    save(uid, value)
    if not await section_access(message, uid, step):
        return
    if not current(uid, token):
        return
    choices = []
    if step == "goal":
        choices = [(label.capitalize(), key) for key, label in GOALS.items() if key != "custom"]
    elif step == "sex":
        choices = [("Мужчина", "male"), ("Женщина", "female")]
    elif step == "activity":
        choices = [(label, key) for key, label in ACTIVITY.items()]
    elif step == "eligibility":
        choices = [("Да, рассчитать норму", "eligible"), ("Нет / не уверен", "ineligible")]
    saved_zone = data.get("settings", {}).get("timezone") if value.get("timezone_done") else None
    if step in LABELS and display(step, data.get("profile", {}), saved_zone) != "не указано":
        choices.append(("Оставить как есть", "keep"))
    text = PROMPTS[step] if step == "eligibility" else question(step, {**value, "timezone": saved_zone}, data.get("profile", {}), clarification=value.get("clarification"))
    await message.answer(text, parse_mode=None,
                         reply_markup=markup(token, choices, skippable=step == "preferences", back=True))


async def review(message, uid, *, data=None):
    from .mentor import button
    from .mentor_panel import MentorPanel, ReplyCards
    if isinstance(message, (MentorPanel, ReplyCards)):
        message.kind = "form"
    value = load(uid)
    data = data or await bridge.api("/dashboard", {"telegram_user_id": uid})
    if load(uid).get("token") != value.get("token"):
        return
    value = {**value, "status": "active", "step": "review", "editing": False,
             "token": secrets.token_hex(4), "answers": [], "clarification": None}
    save(uid, value)
    if not await section_access(message, uid, "review") or not current(uid, value["token"]):
        return
    rows = []
    for field in (*FIELDS, "preferences", "restrictions", "timezone"):
        shown = display(field, data.get("profile", {}), data.get("settings", {}).get("timezone") if value.get("timezone_done") else None)
        label = f"{LABELS[field]}: {shown}"
        rows.append([button(label[:55] + ("…" if len(label) > 55 else ""), f"setup:{value['token']}:edit_{field}")])
    ready = not needs_profile(data) and value.get("timezone_done")
    label = "Всё верно, продолжить" if ready else "Продолжить заполнение"
    rows.append([button(label, f"setup:{value['token']}:review_continue")])
    if ready and data.get("workspace", {}).get("target"):
        rows.append([button("Рассчитать новый ориентир", f"setup:{value['token']}:recalculate")])
    rows.append([button("Продолжить позже", f"setup:{value['token']}:later")])
    await message.answer(summary(data, value), parse_mode=None, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def discard_draft(uid, value):
    if value.get("step") != "target" or not value.get("draft_id"):
        return
    result = await bridge.api("/workspace/record", {"telegram_user_id": uid, "entry_id": value["draft_id"]})
    if current(uid, value["token"]) and result["entry"].get("status") == "draft":
        await bridge.api("/workspace/action", {"telegram_user_id": uid, "entry_id": value["draft_id"], "action": "cancel"})


async def resume(message, uid):
    from .mentor_panel import MentorPanel, ReplyCards
    if isinstance(message, (MentorPanel, ReplyCards)):
        message.kind = "form"
    value = load(uid)
    value = {**value, "status": "active", "token": secrets.token_hex(4)}
    save(uid, value)
    if value.get("step") in {"timezone_confirm", "eligibility"}:
        if await section_access(message, uid, value["step"]) and current(uid, value["token"]):
            await repeat_confirmation(message, uid, value)
        return
    if value.get("step") == "review":
        return await review(message, uid)
    if value.get("step") in LABELS and value.get("step") in PROMPTS:
        if not value.get("route") and not value.get("editing"):
            return await advance(message, uid)
        return await show_question(message, uid, value["step"])
    if value.get("step") == "target" and value.get("draft_id"):
        if not await section_access(message, uid, "target"):
            return
        result = await bridge.api("/workspace/record", {"telegram_user_id": uid, "entry_id": value["draft_id"]})
        if not current(uid, value["token"]):
            return
        if result["entry"].get("status") == "draft":
            from .mentor_flows import draft_text
            return await message.answer(draft_text(result["entry"]), parse_mode=None,
                reply_markup=markup(value["token"], [("Сохранить мою норму", "save_target"), ("Пока без нормы", "cancel_target")], back=True))
    await advance(message, uid)


async def repeat_confirmation(message, uid, value):
    step = value["step"]
    if step == "eligibility":
        text = PROMPTS[step]
        choices = [("Да, рассчитать норму", "eligible"), ("Нет / не уверен", "ineligible")]
    elif step == "timezone_confirm":
        clock = datetime.now(ZoneInfo(value["timezone"])).strftime("%H:%M, %d.%m")
        text = f"Ваш часовой пояс: {timezone_label(value['timezone'])}. Сейчас там {clock}. Всё верно?"
        if value.get("timezone_only"):
            text = "Уточним местное время для дневника и напоминаний. Профиль заново заполнять не нужно.\n\n"+text
        text += "\nНапоминания сами не включатся."
        choices = [("Да, верно", "confirm_timezone"), ("Указать другой город", "change_timezone")]
    else:
        result = await bridge.api("/workspace/record", {"telegram_user_id": uid, "entry_id": value["draft_id"]})
        if not current(uid, value["token"]):
            return
        if result["entry"].get("status") != "draft":
            return await finish(message, uid, "Эта оценка уже обработана. Актуальную норму можно посмотреть в разделе питания.")
        from .mentor_flows import draft_text
        text = draft_text(result["entry"])
        choices = [("Сохранить мою норму", "save_target"), ("Пока без нормы", "cancel_target")]
    await message.answer(text, parse_mode=None, reply_markup=markup(value["token"], choices, back=True))


async def finish(message, uid, text):
    from .mentor import button
    from .mentor_panel import MentorPanel, ReplyCards
    if isinstance(message, (MentorPanel, ReplyCards)):
        message.kind = "navigation"
    value = load(uid)
    timezone_only = value.pop("timezone_only", False)
    save(uid, {**value, "status": "complete", "step": None, "token": secrets.token_hex(4)})
    next_step = ("Можно продолжить дневник или задать вопрос наставнику. Остальные разделы доступны в меню."
                 if timezone_only else "С чего начнём? Можно записать первый приём пищи или задать вопрос о питании и привычках. Тренировки, курс и прогресс доступны в меню.")
    await message.answer(text+"\n\n"+next_step,
                         parse_mode=None, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [button("Добавить еду", "meal")], [button("План на сегодня", "daily_plan")],
                             [button("Спросить наставника", "question")],
                             [button("Меню наставника", "menu")]]))


async def calculate(message, uid, token):
    data = await bridge.api("/dashboard", {"telegram_user_id": uid})
    if not current(uid, token):
        return
    activity = data.get("profile", {}).get("activity")
    await bridge.api("/workspace/nutrition/eligibility", {"telegram_user_id": uid, "confirmed": True})
    result = await bridge.api("/workspace/nutrition/preview", {
        "telegram_user_id": uid, "eligibility_confirmed": True, "activity": activity})
    if not current(uid, token):
        return
    if not result.get("available"):
        from .mentor import button
        save(uid, {**load(uid), "status": "complete", "step": None})
        return await message.answer(result.get("reason", "Автоматический расчёт сейчас недоступен.")+
                                    "\n\nМожно продолжать вести дневник или сохранить норму, согласованную со специалистом.",
                                    parse_mode=None, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                        [button("Норма специалиста", "target_manual")], [button("Добавить еду", "meal")],
                                        [button("Меню наставника", "menu")]]))
    result_draft = await bridge.api("/workspace/draft", {"telegram_user_id": uid, "kind": "target",
        "request_key": f"onboarding:{uid}:{token}", "expected_version": data["version"],
        "data": {**result["nutrition"], "source": "user"}})
    if not current(uid, token):
        return
    entry = result_draft["entry"]
    from .mentor_flows import draft_text
    value = {**load(uid), "step": "target", "draft_id": entry["id"], "token": secrets.token_hex(4),
             "calculation_profile": {k: data.get("profile", {}).get(k) for k in NUTRITION_FIELDS}}
    save(uid, value)
    await message.answer(draft_text(entry)+"\n\n"+result.get("note", "Это ориентир, а не медицинское назначение.")+
                         "\n\nПосле сохранения я смогу сравнивать дневник с этой нормой.", parse_mode=None,
                         reply_markup=markup(value["token"], [("Сохранить мою норму", "save_target"), ("Пока без нормы", "cancel_target")], back=True))


async def update_profile(uid, patch, text, key, *, token=None):
    patch = OnboardingProfile.model_validate(patch).model_dump(exclude_none=True)
    if not patch:
        return
    saved = await bridge.api("/profile/context", {"telegram_user_id": uid})
    if token is not None and (load(uid).get("token") != token or not active(uid)):
        return False
    await bridge.api("/profile/update", {"telegram_user_id": uid, "expected_version": saved["version"],
        "request_key": key, "source_text": text, "evidence": text, "patch": patch})
    return True


async def callback(query, state, message):
    action = query.data.removeprefix("mentor:")
    if not action.startswith("setup:"):
        return False
    uid = query.from_user.id
    from .mentor_access import check_onboarding_access
    if await check_onboarding_access(message, uid, state, actor=query, full_name=getattr(query.from_user, "full_name", None)):
        return True
    async with input_lock(uid):
        value = load(uid)
        parts = action.split(":", 2)
        if len(parts) != 3 or value.get("token") != parts[1]:
            await message.answer("Этот шаг уже пройден. Актуальный вопрос находится ниже в переписке.", parse_mode=None)
            return True
        choice = parts[2]
        if not await section_access(message, uid, value.get("step")):
            return True
        if load(uid).get("token") != value.get("token"):
            return True
        if choice == "later":
            pause(uid)
            from .mentor import enter
            await enter(message, uid, state, onboarding=False)
            return True
        if choice == "begin":
            from .mentor_panel import clear_input
            await clear_input(state)
            bridge.set_mentor_enabled(uid, True)
            bridge.clear_opening_question(uid, bridge.opening_question(uid))
            await resume(message, uid)
            return True
        if value.get("status") != "active":
            return True
        step = value.get("step")
        if choice == "back":
            if value.get("editing") or step in {"target", "eligibility"}:
                await discard_draft(uid, value)
                if current(uid, value["token"]):
                    await review(message, uid)
                return True
            if step == "timezone_confirm":
                await show_question(message, uid, "timezone")
                return True
            route = value.get("route", [])
            index = route.index(step) if step in route else 0
            if index:
                await show_question(message, uid, route[index-1])
            else:
                await offer(message, uid)
            return True
        if step == "review":
            if choice.startswith("edit_") and choice[5:] in LABELS and choice[5:] != "review":
                save(uid, {**value, "editing": True})
                await show_question(message, uid, choice[5:])
            elif choice in {"review_continue", "recalculate"}:
                data = await bridge.api("/dashboard", {"telegram_user_id": uid})
                if not current(uid, value["token"]):
                    return True
                if needs_profile(data) or not value.get("timezone_done"):
                    await advance(message, uid, data=data)
                elif data.get("workspace", {}).get("target") and choice != "recalculate":
                    await finish(message, uid, "Профиль проверен. Сохранённая норма питания остаётся без изменений.")
                else:
                    await show_question(message, uid, "eligibility", data=data)
            return True
        if choice == "keep":
            data = await bridge.api("/dashboard", {"telegram_user_id": uid})
            if not current(uid, value["token"]):
                return True
            if display(step, data.get("profile", {}), value.get("timezone") if value.get("timezone_done") else None) != "не указано":
                await advance(message, uid, data=data)
            return True
        if step in {"goal", "sex", "activity"}:
            labels = GOALS if step == "goal" else ACTIVITY if step == "activity" else {"male": "Мужчина", "female": "Женщина"}
            if choice not in labels:
                return True
            if not await update_profile(uid, {step: choice}, labels[choice], "onboarding:"+query.id, token=value["token"]):
                return True
        elif step == "preferences" and choice == "skip":
            save(uid, {**value, "preferences_done": True})
        elif step == "timezone_confirm" and choice == "confirm_timezone":
            from .mentor_flows import reminder_payload
            data = await bridge.api("/dashboard", {"telegram_user_id": uid})
            if not current(uid, value["token"]):
                return True
            if data["settings"].get("timezone") != value["timezone"]:
                await bridge.api("/reminder/options", {"telegram_user_id": uid,
                    **reminder_payload(data["settings"]), "timezone": value["timezone"]})
            if not current(uid, value["token"]):
                return True
            save(uid, {**value, "timezone_done": True})
        elif step == "timezone_confirm" and choice == "change_timezone":
            await show_question(message, uid, "timezone")
            return True
        elif step == "eligibility" and choice == "eligible":
            await calculate(message, uid, value["token"])
            return True
        elif step == "eligibility" and choice == "ineligible":
            await bridge.api("/workspace/nutrition/eligibility", {"telegram_user_id": uid, "confirmed": False})
            if not current(uid, value["token"]):
                return True
            await finish(message, uid, "Автоматическую норму не рассчитываю. Для индивидуальной нормы лучше обратиться к специалисту. Дневник и остальные разделы доступны.")
            return True
        elif step == "target" and choice in {"save_target", "cancel_target"}:
            if choice == "save_target" and "calculation_profile" in value:
                data = await bridge.api("/dashboard", {"telegram_user_id": uid})
                if not current(uid, value["token"]):
                    return True
                if value["calculation_profile"] != {k: data.get("profile", {}).get(k) for k in NUTRITION_FIELDS}:
                    await discard_draft(uid, value)
                    if current(uid, value["token"]):
                        await message.answer("Данные профиля изменились после расчёта. Проверим их перед новым расчётом; прежнюю норму не меняю.", parse_mode=None)
                        await review(message, uid, data=data)
                    return True
            result = await bridge.api("/workspace/action", {"telegram_user_id": uid, "entry_id": value["draft_id"],
                "action": "confirm" if choice == "save_target" else "cancel"})
            if not current(uid, value["token"]):
                return True
            saved = result["entry"].get("status") == "confirmed"
            if choice == "save_target" and not saved:
                raise bridge.BridgeError("Эта норма уже отменена. Откройте расчёт заново.")
            from .mentor import fmt
            summary = "Ваша дневная норма сохранена.\n"+"\n".join(
                f"{label}: {fmt(result['entry'].get(key))} {unit}" for key, label, unit in
                [("kcal", "Калории", "ккал"), ("protein", "Белки", "г"), ("fat", "Жиры", "г"), ("carbs", "Углеводы", "г")]) if saved else "Норму пока не сохраняю. Настройка профиля завершена."
            await finish(message, uid, summary)
            return True
        else:
            return True
        await advance(message, uid)
    return True


def simple_patch(step, text):
    limits = {"age": (1, 120), "height_cm": (50, 260), "current_weight_kg": (0.01, 500)}
    if step not in limits:
        return None
    unit = {"age": r"(?:лет|год|года)?", "height_cm": r"(?:см|cm)?", "current_weight_kg": r"(?:кг|kg)?"}[step]
    match = re.fullmatch(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*"+unit, text, re.I)
    if not match:
        return None
    number = float(match[1].replace(",", "."))
    low, high = limits[step]
    if not low <= number <= high or step == "age" and not number.is_integer():
        units = {"age": "полных лет", "height_cm": "см", "current_weight_kg": "кг"}
        raise AnswerError(f"Проверьте число: для этого поля ожидается значение {'больше 0 и до 500' if step == 'current_weight_kg' else f'от {low:g} до {high:g}'} {units[step]}. "
                          + ("Возраст укажите целым числом. " if step == "age" else "") + PROMPTS[step])
    return {step: int(number) if step == "age" else number}


class AnswerError(ValueError):
    """A user-facing validation hint, unlike internal model/schema errors."""


async def receive(message, state, professor_client=None, professor_bot=None, expert_client=None):
    try:
        result = await _receive(message, state, professor_client, professor_bot, expert_client)
    except (WebappBotApiError, bridge.BridgeError):
        from .mentor_panel import ReplyCards
        await ReplyCards(message, state, kind="form").answer(
            "Сейчас не получилось связаться с наставником. Ваш шаг сохранён. Пожалуйста, повторите ответ чуть позже.",
            parse_mode=None, reply_markup=markup(load(message.from_user.id).get("token", ""), back=True))
        return False
    return result is not False


async def _receive(message, state, professor_client=None, professor_bot=None, expert_client=None):
    uid = message.from_user.id
    from .mentor_access import check_onboarding_access
    if await check_onboarding_access(message, uid, state, input_message=message):
        return False
    if getattr(message, "voice", None) or getattr(message, "video_note", None):
        from .mentor_messages import spoken_message
        message = await spoken_message(message, state, professor_client)
        if message is None:
            return
    from .mentor_panel import ReplyCards
    original = message
    message = ReplyCards(message, state, kind="form")
    async with input_lock(uid):
        value = load(uid)
        if value.get("status") != "active":
            return
        step, token = value.get("step"), value["token"]
        if value.get("last_input_id") == message.message_id:
            return
        if not await section_access(message, uid, step) or not current(uid, token):
            return
        text = (message.text or "").strip()
        if text.casefold() in {"позже", "не сейчас", "отмена"}:
            pause(uid)
            from .mentor import enter
            return await enter(message, uid, state, onboarding=False)
        if step in {"target", "eligibility", "timezone_confirm"}:
            return await repeat_confirmation(message, uid, value)
        if not text or len(text) > 2000:
            return await message.answer("Для этого шага подойдёт короткий текстовый ответ. "+PROMPTS.get(step, ""), parse_mode=None)
        try:
            patch = simple_patch(step, text)
            result = None
            if step == "preferences" and text.casefold() in {"пропустить", "не знаю"}:
                save(uid, {**value, "preferences_done": True})
                return await advance(message, uid)
            if patch is None:
                kind = "timezone" if step == "timezone" else "onboarding_profile"
                data = await bridge.api("/dashboard", {"telegram_user_id": uid})
                prompt = PROMPTS.get(step, "Что хотите исправить в профиле?")
                answers = value.get("answers", []) if value.get("answers_step") == step else []
                answers = [*answers[-4:], {"question": (value.get("clarification") or prompt) if answers else prompt, "answer": text}]
                value = {**value, "answers": answers, "answers_step": step}
                save(uid, value)
                await state.update_data(onboarding_step=step, onboarding_answers=answers)
                result = await extract_answer(professor_client, kind,
                    answers,
                    {"uid": uid, "profile": data.get("profile", {}), "requested_field": step,
                     "local_date": data.get("date"), "known": {}, "timezone": data.get("settings", {}).get("timezone")})
                if load(uid).get("token") != token or not active(uid):
                    return
                if result.intent in {"pause", "cancel"}:
                    pause(uid)
                    from .mentor import enter
                    return await enter(message, uid, state, onboarding=False)
                if result.intent == "question":
                    pause(uid)
                    bridge.save_opening_question(uid, prompt+"\nЧеловек задал встречный вопрос. Ответь на него; анкету можно продолжить кнопкой, не сохраняй предположения.")
                    from .new_user import handle_single_ai_message
                    await handle_single_ai_message(original, state, professor_bot, professor_client, expert_client, mentor_followup=True)
                    return await message.answer("К знакомству можно вернуться, когда будет удобно.",
                        parse_mode=None, reply_markup=markup(load(uid)["token"], [("Продолжить знакомство", "begin")]))
                patch = result.data.model_dump(exclude_none=True)
                if result.intent == "unknown" or not patch:
                    clarification = result.clarification or prompt
                    save(uid, {**load(uid), "clarification": clarification})
                    await state.update_data(onboarding_question=clarification)
                    return await message.answer(clarification, parse_mode=None, reply_markup=markup(token, back=True))
                if result.clarification:
                    # Never persist the uncertain requested value; other explicit facts can survive.
                    patch = {} if step == "review" else {k: v for k, v in patch.items() if k != step}
            if step == "timezone":
                if result is not None and result.clarification:
                    save(uid, {**load(uid), "clarification": result.clarification})
                    return await message.answer(result.clarification, parse_mode=None, reply_markup=markup(token, back=True))
                zone = patch.get("timezone")
                if not zone:
                    return await message.answer(PROMPTS[step], parse_mode=None)
                ZoneInfo(zone)
                value = {**value, "timezone": zone, "step": "timezone_confirm", "token": secrets.token_hex(4)}
                save(uid, value)
                return await repeat_confirmation(message, uid, value)
            if result is not None:
                patch = {k: v for k, v in patch.items() if data.get("profile", {}).get(k) != v}
                evidence = "\n".join(item["answer"] for item in answers)
            else:
                evidence = text
            if patch and not await update_profile(uid, patch, evidence, f"onboarding:{uid}:{message.message_id}", token=token):
                return
            if load(uid).get("token") != token or not active(uid):
                return
            save(uid, {**load(uid), "last_input_id": message.message_id,
                       **({"preferences_done": True} if patch.get("preferences") else {})})
            if result is not None and result.clarification:
                save(uid, {**load(uid), "clarification": result.clarification})
                await state.update_data(onboarding_question=result.clarification)
                return await message.answer(result.clarification, parse_mode=None, reply_markup=markup(token, back=True))
            if step == "review":
                return await review(message, uid)
            await advance(message, uid)
        except (bridge.BridgeError, ValueError, ZoneInfoNotFoundError) as error:
            note = str(error) if isinstance(error, (bridge.BridgeError, AnswerError)) else "Не получилось уточнить эти данные. "+PROMPTS.get(step, "Что хотите исправить в профиле?")
            from .mentor import back_keyboard
            await message.answer(note, parse_mode=None, reply_markup=markup(token, back=True) if current(uid, token) else back_keyboard())
