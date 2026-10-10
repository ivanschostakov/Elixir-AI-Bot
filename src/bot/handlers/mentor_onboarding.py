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
import config

FIELDS = ("goal", "age", "sex", "height_cm", "current_weight_kg", "activity")
PROMPTS = {
    "goal": "Какого результата вы хотите достичь? Выберите подходящую цель или расскажите, что для вас сейчас важно.",
    "age": "Сколько вам полных лет? Возраст нужен, чтобы проверить, подходит ли автоматический расчёт нормы питания.",
    "sex": "Для расчёта нормы питания уточню: вы мужчина или женщина? Можно выбрать кнопку ниже.",
    "height_cm": "Какой у вас рост в сантиметрах? Например, 170 см.",
    "current_weight_kg": "Сколько вы сейчас весите? Напишите вес в килограммах, например 78,5. Если давно не взвешивались, можно указать примерно.",
    "activity": "Насколько активен ваш обычный день? Учитывайте работу, прогулки и тренировки. Выберите самый близкий вариант или расскажите о своём режиме.",
    "preferences": "Расскажите немного о своём обычном дне и питании: как много двигаетесь, что любите есть и есть ли продукты, которые вам не подходят. Это поможет учитывать ваши привычки. Этот шаг можно пропустить.",
    "timezone": "В каком городе вы сейчас живёте? Это нужно, чтобы дневник и напоминания совпадали с вашим местным временем. Напоминания сами не включатся.",
    "eligibility": "Можно рассчитать стартовую норму питания по вашему профилю. Расчёт предназначен для взрослых без беременности, грудного вскармливания, РПП и необходимости лечебного питания. Эти ограничения к вам не относятся? Если сомневаетесь, расчёт пропустим: дневник останется доступен.",
}


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


def markup(token, choices=(), *, skippable=False):
    from .mentor import button
    rows = [[button(label, f"setup:{token}:{value}")] for label, value in choices]
    if skippable:
        rows.append([button("Пропустить этот шаг", f"setup:{token}:skip")])
    rows.append([button("Продолжить позже", f"setup:{token}:later")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def offer(message, uid):
    from .mentor import button
    value = load(uid)
    token = secrets.token_hex(4)
    save(uid, {**value, "token": token, "status": "paused"})
    await message.answer(
        "Наставник поможет вести питание, следить за активностью и замечать изменения. "
        "Давайте сначала познакомимся: я задам несколько вопросов по одному, затем предложу ориентир КБЖУ.\n\n"
        "Ответы сохраняются в вашем профиле. То, что вы уже рассказали, повторять не нужно; настройку можно отложить.",
        parse_mode=None, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [button("Продолжить знакомство" if value.get("step") else "Начать знакомство", f"setup:{token}:begin")],
            [button("Пока открыть меню", "menu")]]))


async def advance(message, uid, *, data=None):
    from .mentor_panel import MentorPanel
    if isinstance(message, MentorPanel):
        message.kind = "form"
    value = load(uid)
    data = data or await bridge.api("/dashboard", {"telegram_user_id": uid})
    if not current(uid, value.get("token")):
        return
    profile = data.get("profile", {})
    step = next((key for key in FIELDS if profile.get(key) is None or key == "activity" and profile.get(key) not in ACTIVITY), None)
    if not step and not value.get("preferences_done") and not profile.get("preferences"):
        step = "preferences"
    if not step and not value.get("timezone_done"):
        step = "timezone"
    if not step:
        if data.get("workspace", {}).get("target"):
            return await finish(message, uid, "Ваш профиль готов. Сохранённая норма питания остаётся без изменений.")
        step = "eligibility"
    token = secrets.token_hex(4)
    save(uid, {**value, "status": "active", "step": step, "token": token})
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
    await message.answer(PROMPTS[step], parse_mode=None,
                         reply_markup=markup(token, choices, skippable=step == "preferences"))


async def resume(message, uid):
    value = load(uid)
    value = {**value, "status": "active", "token": secrets.token_hex(4)}
    save(uid, value)
    if value.get("step") == "target" and value.get("draft_id"):
        if not await section_access(message, uid, "target"):
            return
        result = await bridge.api("/workspace/record", {"telegram_user_id": uid, "entry_id": value["draft_id"]})
        if not current(uid, value["token"]):
            return
        if result["entry"].get("status") == "draft":
            from .mentor_flows import draft_text
            return await message.answer(draft_text(result["entry"]), parse_mode=None,
                reply_markup=markup(value["token"], [("Сохранить мою норму", "save_target"), ("Пока без нормы", "cancel_target")]))
    await advance(message, uid)


async def repeat_confirmation(message, uid, value):
    step = value["step"]
    if step == "eligibility":
        text = PROMPTS[step]
        choices = [("Да, рассчитать норму", "eligible"), ("Нет / не уверен", "ineligible")]
    elif step == "timezone_confirm":
        clock = datetime.now(ZoneInfo(value["timezone"])).strftime("%H:%M, %d.%m")
        text = f"Ваш часовой пояс: {timezone_label(value['timezone'])}. Сейчас там {clock}. Всё верно?"
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
    await message.answer(text, parse_mode=None, reply_markup=markup(value["token"], choices))


async def finish(message, uid, text):
    from .mentor import button
    from .mentor_panel import MentorPanel
    if isinstance(message, MentorPanel):
        message.kind = "navigation"
    save(uid, {**load(uid), "status": "complete", "step": None, "token": secrets.token_hex(4)})
    await message.answer(text+"\n\nС чего начнём? Можно записать первый приём пищи или задать вопрос о питании и привычках. Тренировки, курс и прогресс доступны в меню.",
                         parse_mode=None, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [button("Добавить еду", "meal")], [button("Спросить наставника", "question")],
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
    value = {**load(uid), "step": "target", "draft_id": entry["id"], "token": secrets.token_hex(4)}
    save(uid, value)
    await message.answer(draft_text(entry)+"\n\n"+result.get("note", "Это ориентир, а не медицинское назначение.")+
                         "\n\nПосле сохранения я смогу сравнивать дневник с этой нормой.", parse_mode=None,
                         reply_markup=markup(value["token"], [("Сохранить мою норму", "save_target"), ("Пока без нормы", "cancel_target")]))


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
            await bridge.api("/reminder/options", {"telegram_user_id": uid,
                **reminder_payload(data["settings"]), "timezone": value["timezone"]})
            if not current(uid, value["token"]):
                return True
            save(uid, {**value, "timezone_done": True})
        elif step == "timezone_confirm" and choice == "change_timezone":
            await advance(message, uid)
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
        raise ValueError("Проверьте число, пожалуйста. "+PROMPTS[step])
    return {step: int(number) if step == "age" else number}


async def receive(message, state, professor_client=None, professor_bot=None, expert_client=None):
    try:
        result = await _receive(message, state, professor_client, professor_bot, expert_client)
    except (WebappBotApiError, bridge.BridgeError):
        from .mentor_panel import ReplyCards
        await ReplyCards(message, state, kind="form").answer(
            "Сейчас не получилось связаться с наставником. Ваш шаг сохранён. Пожалуйста, повторите ответ чуть позже.",
            parse_mode=None, reply_markup=markup(load(message.from_user.id).get("token", "")))
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
                previous = await state.get_data()
                answers = previous.get("onboarding_answers", []) if previous.get("onboarding_step") == step else []
                answers = [*answers[-4:], {"question": previous.get("onboarding_question", PROMPTS[step]) if answers else PROMPTS[step], "answer": text}]
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
                    bridge.save_opening_question(uid, PROMPTS[step]+"\nЧеловек задал встречный вопрос. Ответь на него; анкету можно продолжить кнопкой, не сохраняй предположения.")
                    from .new_user import handle_single_ai_message
                    await handle_single_ai_message(original, state, professor_bot, professor_client, expert_client, mentor_followup=True)
                    return await message.answer("К знакомству можно вернуться, когда будет удобно.",
                        parse_mode=None, reply_markup=markup(load(uid)["token"], [("Продолжить знакомство", "begin")]))
                patch = result.data.model_dump(exclude_none=True)
                if result.intent == "unknown" or not patch:
                    question = result.clarification or PROMPTS[step]
                    await state.update_data(onboarding_question=question)
                    return await message.answer(question, parse_mode=None, reply_markup=markup(token))
            if step == "timezone":
                zone = patch.get("timezone")
                if not zone:
                    return await message.answer(PROMPTS[step], parse_mode=None)
                clock = datetime.now(ZoneInfo(zone)).strftime("%H:%M, %d.%m")
                value = {**value, "timezone": zone, "step": "timezone_confirm", "token": secrets.token_hex(4)}
                save(uid, value)
                return await message.answer(f"Ваш часовой пояс: {timezone_label(zone)}. Сейчас там {clock}. Всё верно?",
                    parse_mode=None, reply_markup=markup(value["token"], [("Да, верно", "confirm_timezone"), ("Указать другой город", "change_timezone")]))
            if result is not None:
                patch = {k: v for k, v in patch.items() if data.get("profile", {}).get(k) != v}
                evidence = "\n".join(item["answer"] for item in answers)
            else:
                evidence = text
            if patch and not await update_profile(uid, patch, evidence, f"onboarding:{uid}:{message.message_id}", token=token):
                return
            if load(uid).get("token") != token or not active(uid):
                return
            if step == "preferences" and patch.get("preferences"):
                save(uid, {**value, "preferences_done": True})
            if result is not None and result.clarification and step not in patch and not data.get("profile", {}).get(step):
                await state.update_data(onboarding_question=result.clarification)
                return await message.answer(result.clarification, parse_mode=None, reply_markup=markup(token))
            await advance(message, uid)
        except (bridge.BridgeError, ValueError, ZoneInfoNotFoundError) as error:
            note = str(error) if isinstance(error, bridge.BridgeError) else "Не получилось уточнить эти данные. "+PROMPTS.get(step, "Попробуйте ещё раз.")
            from .mentor import back_keyboard
            await message.answer(note, parse_mode=None, reply_markup=markup(token) if current(uid, token) else back_keyboard())
