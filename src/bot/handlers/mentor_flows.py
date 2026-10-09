"""Human-confirmed Telegram forms; model prose never stands in for persistence."""
import asyncio
import json
import re
import secrets
from datetime import date, datetime, time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
import config

from src.ai.telegram_mentor import api, BridgeError, mentor_enabled
from src.ai.mentor_input import MODELS, input_lock, parse_step, family, canonical, followup
from src.ai.mentor_copy import QUESTIONS, edit_question
from .mentor_format import day, period, timezone_label, DAYS, GOALS, ACTIVITY
from .mentor_panel import MentorPanel, ReplyCards, clear_input, complete_card

router = Router(name="mentor_forms")


class Input(StatesGroup):
    value = State()


def keyboard(*rows):
    from .mentor import button
    return InlineKeyboardMarkup(inline_keyboard=[*[ [button(label, action) for label, action in row] for row in rows],
        [button("← Меню наставника", "menu")]])


def fmt(value):
    return f"{float(value):,.2f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",") if value is not None else "нет данных"


def number(value):
    return float(value.strip().replace(",", "."))


def request_key(message):
    return f"tg:{message.chat.id}:{message.message_id}"


def specialist_button():
    value = (config.env("TELEGRAM_MENTOR_SPECIALIST_URL", "") or config.env("TELEGRAM_MENTOR_SUPPORT_URL", "") or "https://t.me/ShostakovIV").strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    return InlineKeyboardButton(text="Связаться со специалистом" if config.env("TELEGRAM_MENTOR_SPECIALIST_URL", "") else "Открыть поддержку", url=value)


def reminder_payload(settings):
    old = settings.get("reminders", {})
    return {"timezone": settings.get("timezone", "Europe/Moscow"), "morning": old.get("morning"),
        "evening": old.get("evening", settings.get("daily_time")), "weekly": old.get("weekly"),
        "weekday": old.get("weekday", 6), "inactivity_days": old.get("inactivity_days"), "course": old.get("course", False),
        "detailed_reports": old.get("detailed_reports", False)}


async def reminders_view(message, uid):
    settings = (await api("/dashboard", {"telegram_user_id": uid}))["settings"]
    values = reminder_payload(settings)
    text = "Напоминания\n\n" + "\n".join([
        f"Утренний вес: {values['morning'] or 'выключено'}",
        f"Вечерний итог: {values['evening'] or 'выключено'}",
        f"За неделю: {values['weekly'] or 'выключено'} ({DAYS[values['weekday']]})",
        f"После паузы: {str(values['inactivity_days'])+' дн.' if values['inactivity_days'] else 'выключено'}",
        f"Курс по расписанию: {'включено' if values['course'] else 'выключено'}",
        f"Подробные отчёты в сообщениях: {'включено' if values['detailed_reports'] else 'выключено'}",
        f"Часовой пояс: {timezone_label(values['timezone'])}"])
    await message.answer(text, parse_mode=None, reply_markup=keyboard(
        [("Утренний вес", "reminder:morning"), ("Вечерний итог", "reminder:evening")],
        [("За неделю", "reminder:weekly"), ("После паузы", "reminder:inactivity")],
        [("Выключить курс" if values["course"] else "Включить курс", "reminder:course")],
        [("Выключить подробные отчёты" if values["detailed_reports"] else "Включить подробные отчёты", "reminder:reports")],
        [("Выключить все", "reminder:off"), ("Часовой пояс", "timezone")]))


async def form(message, state, kind, prompt, *, rows=None, silent=False, **data):
    previous = await state.get_data()
    same = family(previous.get("form_kind")) == family(kind)
    known = previous.get("form_known", {}) if same else {}
    known = data.pop("known", known)
    await state.set_state(Input.value)
    await state.update_data(form_kind=kind, form_prompt=prompt, form_question=prompt,
        form_answers=previous.get("form_answers", []) if same else [], form_known=known,
        form_intent=None, form_paused=False, form_token=secrets.token_hex(8),
        form_profile_version=previous.get("form_profile_version") if same else None,
        pending_input=None, **data)
    if not silent:
        if isinstance(message, MentorPanel):
            message.kind = "form"
        await message.answer(prompt, parse_mode=None, reply_markup=keyboard(*(rows or [[("Отмена", "menu")]])))


async def next_step(message, state, kind, prompt, client=None, **data):
    known = data.get("known", (await state.get_data()).get("form_known", {}))
    try:
        ready = canonical(kind, known, False, data)
    except (ValueError, KeyError):
        ready = None
    await form(message, state, kind, prompt, silent=ready is not None, **data)
    if ready is not None:
        await receive_value(message, state, client, normalized=ready)


async def converse(message, state, bot, professor_client, expert_client):
    from .new_user import handle_single_ai_message
    from src.ai.telegram_mentor import save_opening_question
    values = await state.get_data()
    save_opening_question(message.from_user.id, "Незавершённый шаг: "+values.get("form_question", "")+
        "\nУже выяснено: "+json.dumps(values.get("form_known", {}), ensure_ascii=False)+
        "\nСейчас человек задал вопрос. Ответь на него как наставник, без заполнения анкеты, без записи предположений. Шаг можно продолжить позже.")
    return await handle_single_ai_message(message, state, bot, professor_client, expert_client, mentor_followup=True)


async def remember_response(state, response):
    entries = [response[k] for k in ("meal_draft", "target_draft", "program_draft") if response.get(k)]
    entries += response.get("record_drafts") or []
    if not entries:
        await state.update_data(text_confirmation_ready=False)
        return
    previous = (await state.get_data()).get("pending_reviews", [])
    replaced = response.get("replaced_review_ids", [])
    merged = {e["id"]: e for e in previous if e.get("status", "draft") == "draft" and e["id"] not in replaced}
    for entry in entries:
        merged[entry["id"]] = entry
    await state.update_data(pending_reviews=list(merged.values())[-10:], text_confirmation_ready=True)
    if response.get("record_drafts"):
        response["text"] = response.get("text", "")+"\n\nПроверьте запись перед сохранением:\n"+"\n\n".join(
            draft_text(e) for e in response["record_drafts"])


async def pending_text(message, state, client=None, *, professor_bot=None, expert_client=None):
    """Only a literal user confirmation can execute an existing, owned preview."""
    async with input_lock(message.from_user.id):
        from .mentor_panel import reply_panel
        return await pending_text_locked(await reply_panel(message, state), state, client,
            professor_bot=professor_bot, expert_client=expert_client)


async def pending_text_locked(message, state, client=None, *, professor_bot=None, expert_client=None):
    text = re.sub(r"\s+", " ", (message.text or "").casefold()).strip().rstrip(".! ")
    yes = text in {"да", "да, сохрани", "да сохрани", "сохрани", "сохранить", "верно", "всё верно", "все верно", "подтверждаю"}
    no = text in {"нет", "отмена", "не сохраняй", "не сохранять", "отмени"}
    pause = text in {"позже", "не сейчас", "пока не знаю"}
    correction = bool(re.match(r"^(?:нет[, ]|исправ|поправ|вместо|на самом деле)", text)) and not no
    if not (yes or no or pause or correction):
        await state.update_data(text_confirmation_ready=False)
        return False
    values = await state.get_data()
    if not values.get("text_confirmation_ready", True): return False
    reviews = values.get("pending_reviews", [])
    count = bool(values.get("pending_input")) + bool(values.get("pending_set")) + len(reviews)
    if not count: return False
    if pause:
        await message.answer("Хорошо, запись пока не сохраняю. К подтверждению можно вернуться позже.")
        return True
    if count != 1:
        await message.answer("Какую запись вы имеете в виду? Подтвердите или исправьте её кнопкой под ней.")
        return True
    uid = message.from_user.id
    if values.get("pending_input"):
        pending = values["pending_input"]
        if no:
            await clear_input(state)
            await message.answer("Не сохраняю.", reply_markup=keyboard())
        elif yes:
            await confirm_input_locked(message, uid, state, "input_save:"+pending["token"])
        else:
            await form(message, state, values["form_kind"], edit_question(values["form_kind"]), editing=True, silent=True)
            await receive_value(message, state, client, professor_bot=professor_bot, expert_client=expert_client)
        return True
    if values.get("pending_set"):
        if no:
            await state.update_data(pending_set=None, set_receipt=None)
            await message.answer("Этот подход не записываю.", reply_markup=keyboard([("Следующий подход", "workout_start")]))
        elif yes:
            await api("/workspace/workout/set", {"telegram_user_id":uid, "entry_id":values["workout_id"],
                "request_key":values["set_request_key"], "exercise_set":values["pending_set"]})
            await state.update_data(pending_set=None, set_receipt=None)
            await message.answer("Подход записан.", reply_markup=keyboard([("Следующий подход", "workout_start")], [("Завершить тренировку", f"workout_finish:{values['workout_id']}")]))
        else:
            await form(message, state, "workout_set", edit_question("workout_set"), known=values["pending_set"], editing=True, silent=True, pending_set=None)
            await receive_value(message, state, client, professor_bot=professor_bot, expert_client=expert_client)
        return True
    entry = reviews[0]
    if correction:
        if entry.get("kind") in {"program", "workout", "activity_log", "measurement", "wellbeing"}:
            from src.ai.telegram_mentor import save_opening_question
            save_opening_question(uid, f"Человек исправляет черновик {entry['kind']} №{entry['id']}. "
                "Прочитай его через get_mentor_record, сохрани неизменённые данные и подготовь новый черновик с replaces_id. Нужно новое подтверждение.")
            await state.update_data(text_confirmation_ready=False)
            return False
        if await edit_record(message, uid, state, entry["id"], entry.get("kind", "meal"), silent=True):
            await receive_value(message, state, client, professor_bot=professor_bot, expert_client=expert_client)
        else:
            return False
        return True
    path = "/journal/action" if entry.get("kind", "meal") == "meal" else "/workspace/action"
    result = await api(path, {"telegram_user_id":uid, "entry_id":entry["id"], "action":"confirm" if yes else "cancel"})
    await state.update_data(pending_reviews=[])
    if entry.get("kind") in {"workout", "activity_log", "measurement", "wellbeing"}:
        saved = result["entry"]
        await message.answer("Запись сохранена.\n\n"+draft_text(saved) if saved["status"] == "confirmed" else "Черновик отменён.",
            parse_mode=None, reply_markup=keyboard([("Открыть результаты", "workouts" if entry["kind"] in {"workout", "activity_log"} else "progress")]))
        return True
    await message.answer("Записано: "+entry.get("name", {"measurement":"замеры", "wellbeing":"самочувствие", "target":"норма питания", "program":"программа"}.get(entry.get("kind"), "запись"))+"." if yes else "Не сохраняю.", reply_markup=keyboard())
    return True


async def edit_record(message, uid, state, entry_id, kind, *, silent=False):
    entry = (await api("/workspace/record", {"telegram_user_id":uid, "entry_id":int(entry_id)}))["entry"]
    if entry["status"] != "draft": raise BridgeError("Эта запись уже сохранена или отменена. Откройте её актуальные данные в разделе.")
    edit_kind = {"wellbeing":"wellbeing_record", "course":"course_record", "progress_photo":"measurement"}.get(kind, kind)
    if edit_kind == "meal":
        from .mentor import ask
        await ask(message, uid, QUESTIONS["meal_edit"], context=f"Исправь черновик еды №{entry_id}; сохрани неизменённые продукты. Нужен новый черновик и подтверждение.")
        return False
    if edit_kind not in MODELS: raise BridgeError("Для этой записи откройте её раздел.")
    if kind == "progress_photo":
        await form(message, state, "progress_photo", edit_question("progress_photo"),
            editing=True, original_record=entry, replace_kind="measurement", replace_id=int(entry_id), silent=silent)
        return True
    await form(message, state, edit_kind, edit_question(edit_kind), known={k:v for k,v in entry.items() if k in MODELS[edit_kind].model_fields},
        editing=True, original_record=entry, course_data=entry if kind == "course" else {}, replace_kind=kind, replace_id=int(entry_id), silent=silent)
    return True


def draft_text(entry):
    kind = entry["kind"]
    if kind == "course":
        lines = ["Проверьте существующую схему", entry["name"], "Дозировка: "+entry["dose_text"],
            "Источник: "+("специалист" if entry["source"] == "specialist" else "ваша схема"),
            period(entry['start_date'], entry['end_date']),
            (f"Каждые {entry['interval_days']} дн. от даты начала" if entry.get("interval_days") else
             "Дни недели: "+", ".join(DAYS[d] for d in entry["weekdays"])),
            "Время: "+", ".join(entry["times"])+" · "+timezone_label(entry["timezone"]),
            "Это запись вашей схемы, а не назначение. Дозировки бот не подбирает."]
        if entry.get("supply_amount") is not None:
            lines.append(f"Запас {fmt(entry['supply_amount'])} {entry['supply_unit']}; на приём {fmt(entry['amount_per_intake'])} {entry['supply_unit']}")
        return "\n".join(lines)
    if kind == "program":
        return "Программа на неделю\n"+"\n".join(f"{DAYS[e['weekday']]}: {e['name']} · {e['sets']} × {e['reps']}" for e in entry["exercises"])
    if kind == "target":
        return "Сохранить вашу дневную норму?\n"+"\n".join(f"{label}: {fmt(entry.get(key))}" for key, label in [("kcal", "Ккал"), ("protein", "Белки, г"), ("fat", "Жиры, г"), ("carbs", "Углеводы, г")])
    if kind == "wellbeing":
        return f"Самочувствие: {entry['score']}/5"+(f"\nЭнергия: {entry['energy_score']}/5" if entry.get('energy_score') is not None else "")+f"\n{entry.get('note', '')}"
    if kind == "measurement":
        return "Замеры\n"+"\n".join(f"{label}: {fmt(entry[key])} см" for key, label in [("waist_cm", "Талия"), ("chest_cm", "Грудь"), ("hips_cm", "Бёдра")] if entry.get(key) is not None)+ ("\nЛичное фото приложено." if entry.get("photo_file_id") else "")
    if kind == "workout":
        return "Выполненная силовая тренировка\n"+"\n".join(
            f"{s['exercise']}: {fmt(s['weight_kg'])} кг × {s['reps']}" for s in entry["sets"])+f"\nДлительность: {fmt(entry['duration_minutes'])} мин"
    if kind == "activity_log":
        return f"Физическая активность\n{entry['name']}\nДлительность: {fmt(entry['duration_minutes'])} мин\nОтдельная запись, не изменение программы тренировок."
    return "Проверьте запись перед сохранением."


async def show_draft(message, entry, *, note=None):
    text = (note+"\n\n" if note else "")+draft_text(entry)
    edit_kind = "progress_photo" if entry.get("photo_file_id") else entry["kind"]
    for start in range(0, len(text), 3500):
        await message.answer(text[start:start+3500], parse_mode=None,
            reply_markup=keyboard([("Сохранить", f"record:confirm:{entry['id']}"), ("Отмена", f"record:cancel:{entry['id']}")],
                [("Исправить", f"record_edit:{edit_kind}:{entry['id']}")]) if start+3500 >= len(text) else None)


async def make_draft(message, uid, state, kind, data, *, key=None):
    saved = await state.get_data()
    payload = {"telegram_user_id": uid, "request_key": key or request_key(message), "kind": kind, "data": data}
    if saved.get("form_profile_version") is not None:
        payload["expected_version"] = saved["form_profile_version"]
    if saved.get("replace_kind") == kind:
        payload["replaces_id"] = saved.get("replace_id")
    result = await api("/workspace/draft", payload)
    await clear_input(state)
    await state.update_data(pending_reviews=[result["entry"]], text_confirmation_ready=True)
    await show_draft(message, result["entry"])


async def review_input(message, state, kind, payload, text):
    token = secrets.token_hex(8)
    await state.set_state(None)
    await state.update_data(pending_input={"kind": kind, "payload": payload, "token": token}, text_confirmation_ready=True)
    await message.answer(text, parse_mode=None, reply_markup=keyboard(
        [("Да, сохранить", "input_save:"+token)], [("Исправить", "input_edit:"+token)]))


async def confirm_input(message, uid, state, action):
    async with input_lock(uid):
        await confirm_input_locked(message, uid, state, action)


async def confirm_input_locked(message, uid, state, action):
    root, token = action.split(":", 1)
    values = dict(await state.get_data())
    pending = values.get("pending_input")
    if not pending or pending["token"] != token:
        raise BridgeError("Эта запись уже обработана или заменена. Откройте нужный раздел заново.")
    if root == "input_edit":
        await form(message, state, values["form_kind"], edit_question(values["form_kind"]), editing=True)
        return
    payload = {"telegram_user_id": uid, **pending["payload"]}
    kind = pending["kind"]
    if kind == "weight":
        weight = payload.pop("weight")
        await api("/profile/update", {**payload, "patch": {"current_weight_kg": weight}})
        await clear_input(state)
        await complete_card(message, f"Вес {fmt(weight)} кг записан.", parse_mode=None,
            reply_markup=keyboard([("Последние измерения", "history")]))
    elif kind in {"reminder", "timezone"}:
        saved = await api("/dashboard", {"telegram_user_id": uid})
        await api("/reminder/options", {"telegram_user_id": uid,
            **reminder_payload(saved["settings"]), **pending["payload"]})
        await clear_input(state)
        if isinstance(message, MentorPanel):
            await complete_card(message, "Настройка сохранена.", reply_markup=keyboard([("Настройки напоминаний", "reminders")]))
            return
        await reminders_view(message, uid)
    elif kind == "workout_duration":
        result = await api("/workspace/workout/finish", payload)
        await clear_input(state)
        await workout_result(message, result["entry"], completed=True)
    elif kind == "course_reminder":
        await api("/workspace/course/reminder", payload)
        await clear_input(state)
        await complete_card(message, "Время напоминания сохранено. Схема и дозировка не менялись.", reply_markup=keyboard([("🧬 Мой курс", "course")], [("⏰ Включить напоминания", "reminders")]))


def wellbeing_prompt(values):
    text = QUESTIONS["wellbeing_note"]
    if values.get("score", 5) <= 2 or values.get("energy_score", 5) is not None and values.get("energy_score", 5) <= 1:
        text += " Если состояние резко ухудшилось или симптомы сильные, обратитесь за медицинской помощью."
    return text



def today_view(data):
    w = data.get("workspace", {})
    p = data.get("profile", {})
    lines = ["Сегодня · "+day(data["date"]), "", f"Питание: ≈ {fmt(data['totals']['kcal'])} ккал"]
    target = w.get("target")
    if target:
        lines[-1] += " из "+fmt(target["kcal"])
    else:
        lines.append("Норма КБЖУ не задана.")
    if p.get("current_weight_kg") is not None:
        lines.append(f"Вес: {fmt(p['current_weight_kg'])} кг" + (f" · цель {fmt(p['target_weight_kg'])} кг" if p.get("target_weight_kg") else ""))
    exercises = w.get("today_exercises", [])
    lines.append("\nТренировка: "+(", ".join(dict.fromkeys(e["name"] for e in exercises)) if exercises else "в программе на сегодня нет"))
    lines.append("Записанных тренировок: "+str(len(w.get("today_workouts", []))))
    if w.get("today_activities"):
        lines.append("Другая активность: "+"; ".join(f"{e['name']} — {fmt(e['duration_minutes'])} мин" for e in w["today_activities"]))
    events = w.get("today_course", [])
    lines.append("\nКурс: "+("сегодня нет приёмов по сохранённому расписанию" if not events else ""))
    zone = ZoneInfo(data["settings"]["timezone"])
    for event in events:
        clock = datetime.fromisoformat(event["occurred_at"]).astimezone(zone).strftime("%H:%M")
        status = {"done": "выполнено", "skipped": "пропущено", "pending": "ожидается"}.get(event["status"], event["status"])
        lines.append(f"{clock} · {event['name']} · {status}")
    checks = [bool(data.get("meals")), bool(w.get("today_wellbeing"))]
    if exercises:
        checks.append(bool(w.get("today_workouts")))
    checks.extend(e["status"] == "done" for e in events)
    lines.append(f"\nВыполнено {sum(checks)} из {len(checks)}: питание, самочувствие" + (", тренировка" if exercises else "") + (", пункты курса" if events else ""))
    return "\n".join(lines)


def daily_tasks(data):
    w = data.get("workspace", {})
    meals = data.get("meals", [])
    tasks = [("Приём пищи: "+m["name"], True) for m in meals]
    if not meals:
        tasks.append(("Добавить еду", False))
    if w.get("today_exercises"):
        tasks.append(("Тренировка", bool(w.get("today_workouts"))))
    tasks.extend(("Курс: "+e["name"], e["status"] == "done") for e in w.get("today_course", []) if e["status"] != "cancelled")
    tasks.append(("Вечерняя оценка самочувствия", bool(w.get("today_wellbeing"))))
    return tasks


def home_view(data):
    w = data.get("workspace", {})
    p = data.get("profile", {})
    tasks = daily_tasks(data)
    target = w.get("target") or {}
    lines = ["🌿 Наставник ElixirPeptide", f"Сегодня выполнено {sum(done for _, done in tasks)} из {len(tasks)} задач."]
    calories = fmt(data.get("totals", {}).get("kcal", 0))
    lines.append(f"Питание: ≈ {calories}" + (f" из {fmt(target['kcal'])} ккал" if target.get("kcal") else " ккал; норма не задана"))
    lines.append("Тренировка: "+("завершена" if w.get("today_workouts") else "запланирована на сегодня" if w.get("today_exercises") else "на сегодня не запланирована"))
    if w.get("today_activities"):
        lines.append("Другая активность: "+"; ".join(f"{e['name']} — {fmt(e['duration_minutes'])} мин" for e in w["today_activities"]))
    now = datetime.fromisoformat(data["now"]) if data.get("now") else None
    events = [e for c in w.get("courses", []) for e in c.get("calendar", [])
        if e["status"] == "pending" and now and datetime.fromisoformat(e["occurred_at"]) >= now]
    zone = ZoneInfo(data.get("settings", {}).get("timezone", "Europe/Moscow"))
    if events:
        next_event = min(events, key=lambda e: datetime.fromisoformat(e["occurred_at"]))
        clock = datetime.fromisoformat(next_event["occurred_at"]).astimezone(zone).strftime("%d.%m в %H:%M")
        lines.append("Курс: следующий приём по расписанию "+clock)
    else:
        lines.append("Курс: ближайший приём не запланирован")
    if p.get('current_weight_kg') is not None:
        lines.append(f"Вес: {fmt(p['current_weight_kg'])} кг"+(f", цель: {fmt(p['target_weight_kg'])} кг" if p.get('target_weight_kg') is not None else ""))
    if not target:
        lines.append("Для расчёта остатка задайте норму в разделе «Питание → Мои нормы КБЖУ».")
    return "\n".join(lines)


def weight_chart_png(data):
    from io import BytesIO
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    weights = sorted(data.get("weights", []), key=lambda x: x["occurred_at"])
    if len(weights) < 2:
        return None
    zone = ZoneInfo(data["settings"]["timezone"])
    figure = Figure(figsize=(8, 4), layout="constrained")
    axis = figure.subplots()
    axis.plot([datetime.fromisoformat(w["occurred_at"]).astimezone(zone) for w in weights], [w["weight_kg"] for w in weights], marker="o", color="#19754b")
    axis.set_ylabel("кг")
    axis.set_title("Динамика записанного веса")
    axis.grid(alpha=.2)
    figure.autofmt_xdate()
    image = BytesIO()
    FigureCanvasAgg(figure).print_png(image)
    return image.getvalue()


async def weight_chart(message, data):
    from aiogram.types import BufferedInputFile
    image = await asyncio.to_thread(weight_chart_png, data)
    if image is None:
        await message.answer("Для графика нужны хотя бы два измерения веса.", reply_markup=keyboard([("⚖️ Добавить вес", "weight")]))
        return
    await message.answer_photo(BufferedInputFile(image, filename="weight.png"), caption="Только ваши сохранённые измерения.", protect_content=True,
        reply_markup=keyboard([("⚖️ Добавить вес", "weight")], [("📊 Прогресс", "progress")]))


def weekly_view(data):
    w = data["workspace"]["weekly"]
    diff = w.get("weight_change_kg")
    lines = [f"Прогресс за {w.get('days', 7)} дней · {period(w.get('from'), w.get('to'))}", "",
        f"Вес: {diff:+g} кг" if diff is not None else "Вес: недостаточно измерений за период",
        f"Средний вес за 7 дней: {fmt(w.get('weight_mean_7d'))} кг · измерений {w.get('weight_samples_7d', 0)}",
        f"Среднее самочувствие: {fmt(w.get('wellbeing_mean'))}/5 · записей {w.get('wellbeing_samples', 0)}",
        f"Средняя энергия: {fmt(w.get('energy_mean'))}/5 · записей {w.get('energy_samples', 0)}",
        f"Записано приёмов пищи: {w.get('meals', 0)}", f"Средняя калорийность: ≈ {fmt(w.get('average_kcal'))} ккал (дней с записями: {w.get('nutrition_days', 0)})",
        f"Белок к сохранённой норме: {fmt(w.get('protein_target_percent'))}% (дней: {w.get('protein_target_days', 0)})",
        f"Тренировок: {w.get('workouts', 0)} · по программе {w.get('planned_workouts', 0)}", f"Длительность: {fmt(w.get('duration_minutes'))} мин",
        f"Объём (вес × повторы): {fmt(w.get('volume_kg'))} кг",
        f"Курс: выполнено {w.get('course_done', 0)} из {w.get('course_due', 0)} запланированных приёмов",
        "", "Показаны только сохранённые записи. Отсутствие записи не означает ноль или пропуск питания.",
        ]
    from .mentor_insights import weekly_insight
    lines.append(weekly_insight(data))
    return "\n".join(line for line in lines if "нет данных" not in line)


async def library(message, uid, state, *, offset=0, favorites=False, query=""):
    result = await api("/workspace/meals", {"telegram_user_id": uid, "offset": offset, "query": query, "favorites_only": favorites})
    await state.update_data(library_query=query, library_favorites=favorites, library_offset=offset)
    if not result["items"]:
        text = "Вы ещё не добавили любимые блюда. Нажмите «В избранное» в истории питания." if favorites else "Подходящих записей нет. Попробуйте другое название или добавьте еду."
        await message.answer(text, reply_markup=keyboard([("➕ Добавить еду", "meal")], [("📅 История питания", "meals:0")]))
        return
    for meal in result["items"]:
        await message.answer(f"{meal['name']}\n≈ {fmt(meal['kcal'])} ккал · Б {fmt(meal['protein'])} / Ж {fmt(meal['fat'])} / У {fmt(meal['carbs'])}\n{day(meal['occurred_at'])}",
            parse_mode=None, reply_markup=keyboard([("Повторить", f"repeat:{meal['id']}"),
                ("Убрать из избранного" if meal.get("favorite") else "В избранное", f"favorite:{meal['id']}:{0 if meal.get('favorite') else 1}")]))
    if result.get("has_more"):
        await message.answer("История еды", reply_markup=keyboard([("Далее", f"meals:{offset+10}")]))


async def start_form(message, uid, state, action, *, editing=False):
    if action == "program":
        if not editing:
            await state.update_data(replace_kind=None, replace_id=None)
        await state.update_data(program_exercises=[], form_kind=None, form_known={})
        await message.answer("Как вам удобнее составить программу тренировок? Можно собрать её по одному упражнению, "
            "попросить наставника предложить вариант или прислать уже готовую программу.", reply_markup=keyboard(
            [("📅 Собрать по шагам", "program_day")], [("✨ Собрать программу за меня", "program_ai")],
            [("✍️ Ввести готовую программу", "program_text")]))
        return True
    if action not in {"target", "program", "measurement", "progress_photo", "meal_search", "custom_goal", "course"}:
        return False
    if not editing:
        await state.update_data(replace_kind=None, replace_id=None, form_kind=None, form_known={}, editing=False)
    await form(message, state, action, QUESTIONS[action])
    return True


async def dispatch(query, state, action, professor_bot, professor_client, expert_client, *, panel=None):
    from .mentor import ask, run_ai_action, response_keyboard, section_keyboard, profile_text
    uid = query.from_user.id
    message = panel if panel is not None else query.message
    # Apply server-side gates to stale keyboards as well as the current menu.
    data = await api("/dashboard", {"telegram_user_id": uid})
    w = data.get("workspace", {})
    gates = w.get("sections", {})
    root = action.split(":", 1)[0]
    section = {"meal": "food", "nutrition": "food", "suggest": "food", "target": "food", "meal_search": "food", "favorites": "food", "meals": "food", "repeat": "food", "repeat_previous": "food", "favorite": "food", "meal_edit": "food", "meal_confirm": "food", "meal_cancel": "food",
        "program": "workouts", "program_ai": "workouts", "program_day": "workouts", "program_sets": "workouts", "program_reps": "workouts", "program_done": "workouts", "program_text": "workouts", "workout_free": "workouts", "set_confirm": "workouts", "set_edit": "workouts", "workout_plan": "workouts", "workout_results": "workouts", "workout_start": "workouts", "workout_set": "workouts", "workout_finish": "workouts", "workout_duration": "workouts",
        "course_event": "course", "course_instruction": "course", "course_calendar": "course", "course_supply": "course", "course_stop": "course", "course_new": "course", "course_source": "course",
        "weight": "progress", "history": "progress", "weekly": "progress", "monthly": "progress", "measurement": "progress", "measurements": "progress", "progress_photo": "progress", "photo": "progress",
        "reminder": "settings", "reminder_clock": "settings", "reminders": "settings", "privacy": "settings", "privacy_erase": "settings", "privacy_erase_confirm": "settings", "timezone": "settings",
        "data": "profile", "sex": "profile", "goals": "profile", "goal": "profile", "custom_goal": "profile",
        "meal_add": "food", "food_lookup": "food", "target_auto": "food", "target_activity": "food", "target_ineligible": "food", "target_manual": "food",
        "course_reminder": "course", "course_reminder_save": "course", "reminder_reports_yes": "settings",
        "weight_chart": "progress", "specialist": "course", "analyze_day": "ask",
        "question": "ask", "adjust": "ask", "reason": "ask", "daily_plan": "ask", "wellbeing": "today", "energy": "today", "wellbeing_skip": "today"}.get(root, root)
    if root in {"input_save", "input_edit"}:
        pending = (await state.get_data()).get("pending_input") or {}
        section = {"weight": "progress", "workout_duration": "workouts"}.get(pending.get("kind"), "settings")
    closed = {x.strip() for x in config.env("TELEGRAM_MENTOR_CLOSED_SECTIONS", "").split(",")}
    if not gates.get(section, True) or section in closed:
        await message.answer("Раздел временно отключён.", reply_markup=keyboard())
        return True
    from .mentor_dialogue import dispatch_dialogue
    if await dispatch_dialogue(query, state, action, message, professor_bot, professor_client, expert_client):
        return True
    if root in {"input_save", "input_edit"}:
        await confirm_input(message, uid, state, action)
    elif action == "food_lookup":
        from src.ai.telegram_mentor import save_opening_question
        save_opening_question(uid, "Справочный поиск продукта: дай приблизительную оценку КБЖУ, уточнив порцию при необходимости. Не создавай запись или черновик съеденной еды, пока человек явно не сообщил, что уже съел её.")
        await message.answer("О каком продукте или блюде хотите узнать? Назовите его и, если знаете, размер порции. "
            "Помогу примерно оценить калории, белки, жиры и углеводы; в дневник этот поиск ничего не запишет.",
            reply_markup=keyboard([("Найти среди моих записей", "meal_search")]))
    elif action == "target":
        await message.answer("Дневная норма КБЖУ\nМожно рассчитать стартовый ориентир по профилю или ввести свою согласованную норму.",
            reply_markup=keyboard([("Рассчитать по профилю", "target_auto")], [("Ввести вручную", "target_manual")]))
    elif action == "target_manual":
        await start_form(message, uid, state, "target")
    elif action == "target_auto":
        labels = {"goal": "цель", "age": "возраст", "sex": "пол", "height_cm": "рост", "current_weight_kg": "вес"}
        missing = [label for key, label in labels.items() if data.get("profile", {}).get(key) is None]
        if missing:
            rows = [[("👤 Заполнить профиль", "data")]]
            if data.get("profile", {}).get("sex") is None:
                rows.insert(0, [("Мужской", "sex:male"), ("Женский", "sex:female")])
            questions = {"цель":"Какого результата хотите достичь: снизить вес, набрать его или поддерживать нынешний? Это поможет подобрать ориентир питания.",
                "возраст":"Сколько вам полных лет? Возраст нужен для расчёта ориентировочной нормы питания.",
                "пол":"Для расчёта нормы уточню: вы мужчина или женщина? Можно ответить сообщением или выбрать кнопку ниже.",
                "рост":"Какой у вас рост в сантиметрах? Например, 170 см. Это нужно для расчёта нормы питания.", "вес":QUESTIONS["weight"]}
            from src.ai.telegram_mentor import save_opening_question
            question = questions[missing[0]]
            save_opening_question(uid, question+"\nЧеловек собирает профиль для расчёта нормы. Сохрани только явно названные факты текущего ответа и уточни следующую недостающую деталь, если нужна. Для расчёта используй preview_mentor_nutrition с существующими ограничениями.")
            await message.answer(question, reply_markup=keyboard(*rows, [("Продолжить расчёт", "target_auto")]))
        else:
            await message.answer("Расчёт предназначен для взрослых без беременности, грудного вскармливания, РПП и необходимости лечебного питания. Это стартовый ориентир, не медицинское назначение. Подтверждаете, что эти ограничения к вам не относятся?",
                reply_markup=keyboard([("Да, выбрать активность", "target_activity")], [("Нет / не уверен: ввести норму специалиста", "target_ineligible")]))
    elif action == "target_ineligible":
        await api("/workspace/nutrition/eligibility", {"telegram_user_id": uid, "confirmed": False})
        await start_form(message, uid, state, "target")
    elif action == "target_activity":
        await api("/workspace/nutrition/eligibility", {"telegram_user_id": uid, "confirmed": True})
        await message.answer("Насколько активен ваш обычный день?", reply_markup=keyboard(
            *[[(label, "target_activity:"+key)] for key, label in ACTIVITY.items()]))
    elif root == "target_activity":
        activity = action.split(":")[1]
        if activity not in ACTIVITY: raise BridgeError("Выберите активность кнопкой.")
        evidence = ACTIVITY[activity]
        await api("/profile/update", {"telegram_user_id":uid, "expected_version":data["version"], "request_key":"activity:"+query.id,
            "source_text":evidence, "evidence":evidence, "patch":{"activity":activity}})
        result=await api("/workspace/nutrition/preview", {"telegram_user_id": uid, "eligibility_confirmed": bool(w.get("nutrition_eligibility_confirmed")), "activity": activity})
        if not result.get("available"):
            labels={"goal":"цель", "age":"возраст", "sex":"пол", "height_cm":"рост", "current_weight_kg":"вес", "activity":"активность"}
            missing=", ".join(labels[k] for k in result.get("missing", []))
            rows = [[("👤 Дозаполнить профиль", "data")]] if missing else []
            await message.answer(result["reason"]+("\nНе хватает: "+missing if missing else ""), reply_markup=keyboard(*rows, [("Ввести норму вручную", "target_manual")]))
        else:
            result_draft=await api("/workspace/draft", {"telegram_user_id": uid, "request_key": "nutrition:"+query.id,
                "kind": "target", "data": {**{k:float(v) for k,v in result["nutrition"].items()}, "source": "user"}})
            await remember_response(state, {"target_draft":result_draft["entry"]})
            await show_draft(message, result_draft["entry"], note=result["note"])
    elif root == "course_reminder":
        course=next((c for c in w.get("courses", []) if c["id"] == int(action.split(":")[1])), None)
        if not course: raise BridgeError("Курс не найден.")
        await form(message, state, "course_reminder", QUESTIONS["course_reminder"]+" Часовой пояс: "+timezone_label(course["timezone"]), course_id=course["id"])
    elif action == "course_reminder_save":
        values=await state.get_data()
        if "pending_reminder_time" not in values: raise BridgeError("Откройте настройку времени заново.")
        await api("/workspace/course/reminder", {"telegram_user_id": uid, "entry_id": values["course_id"], "reminder_time": values["pending_reminder_time"]})
        await clear_input(state)
        await complete_card(message, "Время напоминания сохранено. Схема и дозировка не менялись.", reply_markup=keyboard([("🧬 Мой курс", "course")], [("⏰ Включить напоминания", "reminders")]))
    elif action == "reminder_reports_yes":
        values=reminder_payload(data["settings"])
        await api("/reminder/options", {"telegram_user_id": uid, **values, "detailed_reports": True})
        await reminders_view(message, uid)
    elif action == "today":
        tasks = daily_tasks(data)
        text = "🎯 План на сегодня\n\n"+"\n".join(("✅ " if done else "⬜ ")+name for name, done in tasks)
        text += f"\n\nВыполнено: {sum(done for _, done in tasks)} из {len(tasks)}"
        await message.answer(text, parse_mode=None, reply_markup=keyboard(
            [("➕ Добавить еду", "meal")], [("🏋️ Начать тренировку", "workout_start")], [("🧬 Открыть мой курс", "course")],
            [("❤️ Отметить самочувствие", "wellbeing")], [("✏️ Скорректировать план", "adjust")]))
    elif action in {"weekly", "monthly", "progress"}:
        if action == "monthly":
            data["workspace"]["weekly"] = await api("/workspace/report", {"telegram_user_id": uid, "days": 30})
        text=weekly_view(data)
        if action == "progress":
            month=await api("/workspace/report", {"telegram_user_id": uid, "days": 30})
            diff=month.get("weight_change_kg")
            text="📊 Прогресс\nТекущий вес: "+(fmt(data["profile"]["current_weight_kg"])+" кг" if data.get("profile", {}).get("current_weight_kg") is not None else "ещё не записан")+"\nИзменение за месяц: "+(f"{diff:+g} кг" if diff is not None else "недостаточно измерений")+"\n\n"+text
            progress=w.get("goal_progress")
            if progress and progress.get("percent") is not None:
                text+=f"\nПуть к текущей цели: {fmt(progress['percent'])}% от первого измерения {day(progress['since'])}."
        await message.answer(text, parse_mode=None, reply_markup=section_keyboard("progress"))
    elif action == "weight_chart":
        await weight_chart(message, data)
    elif action == "specialist":
        kb = keyboard()
        link = specialist_button()
        if link: kb.inline_keyboard.insert(0, [link])
        await message.answer("Медицинские вопросы и изменение схемы курса обсудите со специалистом. Бот не меняет дозировки. Нажмите кнопку ниже для связи." if link else "Напишите в поддержку @ShostakovIV — вам помогут связаться со специалистом.", reply_markup=kb)
    elif root == "course_instruction":
        course=next((c for c in w.get("courses", []) if c["id"] == int(action.split(":")[1])), None)
        if not course: raise BridgeError("Курс не найден.")
        await run_ai_action(query, state, professor_bot, professor_client, expert_client,
            "Найди проверенное описание продукта из моего курса: "+course["name"]+". Используй доступный каталог/базу знаний и укажи источник. Если инструкции нет, прямо скажи об этом. Не назначай и не меняй дозировки; медицинские вопросы направь специалисту.")
    elif action == "analyze_day":
        await run_ai_action(query, state, professor_bot, professor_client, expert_client,
            "Проанализируй мой сегодняшний день по сохранённым питанию, весу, тренировкам и самочувствию. Учитывай остаток КБЖУ и любимые блюда. Не придумывай отсутствующие записи. Дозировки не меняй.")
    elif action == "workout_results":
        week = w.get("weekly", {})
        lines = ["Результаты тренировок за неделю", f"Записано тренировок: {week.get('workouts', 0)}",
            f"По программе: {week.get('planned_workouts', 0)}", f"Длительность: {fmt(week.get('duration_minutes'))} мин",
            f"Объём: {fmt(week.get('volume_kg'))} кг",
            f"Другая активность: {week.get('activities', 0)} записей, {fmt(week.get('activity_duration_minutes', 0))} мин", "",
            "Для сравнения прогресса записывайте вес и повторы в тех же упражнениях. Общий объём сам по себе не показывает рост силы."
            if week.get("workouts") else "Пока нет записанных тренировок. После следующей тренировки сохраните подходы — будет с чем сравнивать."]
        await message.answer("\n".join(lines), parse_mode=None, reply_markup=keyboard([("🏋️ Тренировки", "workouts")]))
    elif action == "program_ai":
        await run_ai_action(query, state, professor_bot, professor_client, expert_client,
            "Помоги составить недельную программу тренировок по моему профилю. Уточни ограничения и опыт, если они неизвестны. "
            "Покажи упражнения, дни недели, подходы и повторы. Используй draft_mentor_program для черновика; не утверждай, что программа сохранена без подтверждения кнопкой.")
    elif action == "program_text":
        await form(message, state, "program", QUESTIONS["program"])
    elif action == "program_day":
        await message.answer("В какой день недели запланируем упражнение? Выберите день ниже, затем добавим само упражнение, подходы и повторения.", reply_markup=keyboard(*[[(label, f"program_day:{i}")] for i, label in enumerate(DAYS)]))
    elif root == "program_day":
        weekday = int(action.split(":")[1])
        if weekday not in range(7): raise BridgeError("Выберите день недели.")
        await state.update_data(form_kind=None, form_known={})
        await form(message, state, "program_name", f"День тренировки: {DAYS[weekday]}. "+QUESTIONS["program_name"], program_weekday=weekday, known={"weekday":weekday})
    elif root == "program_sets":
        if (await state.get_data()).get("form_kind") != "program_sets":
            raise BridgeError("Эта кнопка устарела. Продолжите текущий шаг конструктора.")
        sets = int(action.split(":")[1])
        if not 1 <= sets <= 30: raise BridgeError("Число подходов: от 1 до 30.")
        await state.update_data(program_sets=sets, form_known={**(await state.get_data()).get("form_known", {}), "sets":sets})
        await next_step(message, state, "program_reps", QUESTIONS["program_reps"], professor_client,
            rows=[[(str(i), f"program_reps:{i}") for i in (6, 8, 10, 12, 15)]])
    elif root == "program_reps":
        await append_program_exercise(message, state, int(action.split(":")[1]))
    elif action == "program_done":
        values = await state.get_data()
        if not values.get("program_exercises"): raise BridgeError("Сначала добавьте упражнение.")
        await make_draft(message, uid, state, "program", {"exercises": values["program_exercises"]}, key="program:"+query.id)
    elif action in {"workouts", "workout_plan"}:
        program = w.get("program")
        text = draft_text(program) if program else "Программа на неделю пока не сохранена."
        if w.get("active_workout"):
            text += f"\n\nАктивная тренировка: {len(w['active_workout']['sets'])} подходов."
        await message.answer(text, parse_mode=None, reply_markup=keyboard(
            [("🏋️ Тренировка на сегодня", "workout_start")], [("📅 План на неделю", "workout_plan")],
            [("📈 Мои результаты", "workout_results")], [("⚙️ Изменить программу" if program else "➕ Создать программу", "program")]))
    elif action in {"workout_start", "workout_free"}:
        if action == "workout_start" and not w.get("active_workout") and not w.get("today_exercises"):
            await message.answer("На сегодня нет программы. Создайте её или выберите свободную тренировку.", reply_markup=keyboard(
                [("➕ Создать программу", "program")], [("Записать свободную тренировку", "workout_free")]))
            return True
        result = await api("/workspace/workout/start", {"telegram_user_id": uid, "request_key": "start:"+query.id})
        entry = result["entry"]
        if entry["status"] == "confirmed":
            await workout_result(message, entry)
            return True
        await state.update_data(workout_id=entry["id"])
        day = datetime.fromisoformat(data["now"]).astimezone(ZoneInfo(data["settings"]["timezone"])).weekday()
        planned = [e for e in entry.get("program_exercises", []) if e["weekday"] == day]
        plan_text = "\n".join(f"{e['name']}: {e['sets']} × {e['reps']}" for e in planned)
        next_name = next_exercise(entry, day)
        await message.answer(("🏋️ Тренировка на сегодня\n"+plan_text) if plan_text else "Сегодня в программе нет упражнений. Можно записать свою тренировку.", parse_mode=None)
        await begin_set(message, state, entry, day, next_name)
        await message.answer("Когда закончите подходы:", reply_markup=keyboard([("Завершить", f"workout_finish:{entry['id']}"), ("Указать длительность", f"workout_duration:{entry['id']}")]))
    elif root == "set_confirm":
        values = await state.get_data()
        if action.split(":", 1)[1] != values.get("set_receipt") or not values.get("pending_set"):
            raise BridgeError("Этот подход уже обработан или заменён. Продолжите тренировку.")
        result = await api("/workspace/workout/set", {"telegram_user_id": uid, "entry_id": values["workout_id"],
            "request_key": values["set_request_key"], "exercise_set": values["pending_set"]})
        await state.update_data(pending_set=None, set_receipt=None)
        await complete_card(message, f"✅ Подход сохранён. Всего: {len(result['entry']['sets'])}.", reply_markup=keyboard(
            [("➡️ Следующий подход / упражнение", "workout_start")], [("✅ Завершить тренировку", f"workout_finish:{values['workout_id']}")]))
    elif root == "set_edit":
        values = await state.get_data()
        if action.split(":", 1)[1] != values.get("set_receipt"):
            raise BridgeError("Откройте текущую тренировку.")
        await form(message, state, "workout_set", edit_question("workout_set"), known=values.get("pending_set") or {}, editing=True, pending_set=None)
    elif root == "workout_finish":
        await form(message, state, "workout_duration", QUESTIONS["workout_duration"], workout_id=int(action.split(":")[1]))
    elif root == "workout_duration":
        await form(message, state, "workout_duration", QUESTIONS["workout_duration"], workout_id=int(action.split(":")[1]))
    elif action == "course" or root in {"course_calendar", "course_supply"}:
        courses = w.get("courses", [])
        if root in {"course_calendar", "course_supply"}:
            courses = [c for c in courses if c["id"] == int(action.split(":")[1])]
            if not courses:
                raise BridgeError("Курс не найден. Откройте актуальное меню.")
        if not courses:
            await message.answer("Существующая схема ещё не сохранена. Добавьте только назначения специалиста или вашу уже существующую схему; бот не подбирает дозировки.", reply_markup=keyboard([("Добавить существующую схему", "course_new")]))
        for course in courses:
            start=date.fromisoformat(course["start_date"])
            end=date.fromisoformat(course["end_date"])
            local_day=datetime.fromisoformat(data["now"]).astimezone(ZoneInfo(course["timezone"])).date()
            days=(end-start).days+1
            course_day=max(0,min(days,(local_day-start).days+1))
            lines = ["🧬 Мой курс", course["name"], f"День курса: {course_day} из {days}", "Сохранённая дозировка: "+course["dose_text"],
                f"Выполнено {course['done']} из {course['due']} наступивших приёмов."]
            if course['due']:
                lines.append(f"Выполнение плана: {course['done']/course['due']*100:.0f}%")
            future=[e for e in course.get("calendar", []) if e["status"]=="pending" and datetime.fromisoformat(e["occurred_at"])>=datetime.fromisoformat(data["now"])]
            if future:
                clock=datetime.fromisoformat(future[0]["occurred_at"]).astimezone(ZoneInfo(course["timezone"])).strftime("%d.%m в %H:%M")
                lines.append("Следующий приём по расписанию: "+clock)
            if not data.get("settings", {}).get("reminders", {}).get("course"):
                lines.append("Напоминания о курсе выключены. Включите их в настройках.")
            elif course.get("reminder_time"):
                lines.append("Уведомление в дни курса: "+course["reminder_time"]+" · "+course["timezone"])
            else:
                lines.append("Уведомления: по времени сохранённой схемы.")
            supply = course.get("supply")
            if supply:
                lines.append(f"Запас по отметкам: {fmt(supply['remaining'])} {supply['unit']} · на {supply['intakes_available']} приёмов.")
                if supply["runs_out_at"]:
                    lines.append("Первый приём без достаточного запаса: "+supply["runs_out_at"])
            else:
                lines.append("Запас не рассчитан: количество и расход в одинаковых единицах не указаны.")
            for event in (course.get("calendar", [])[:14] if root == "course_calendar" else []):
                clock = datetime.fromisoformat(event["occurred_at"]).astimezone(ZoneInfo(course["timezone"])).strftime("%d.%m %H:%M")
                lines.append(f"{clock} · "+{"done": "выполнено", "skipped": "пропущено", "pending": "по расписанию", "cancelled": "отменено"}.get(event["status"], event["status"]))
            kb = keyboard([("📅 Календарь курса", f"course_calendar:{course['id']}")],
                [("📦 Рассчитать остаток", f"course_supply:{course['id']}")], [("⏰ Изменить время напоминания", f"course_reminder:{course['id']}")],
                [("❤️ Дневник самочувствия", "wellbeing")], [("👨‍⚕️ Вопрос специалисту", "specialist")],
                [("📖 Инструкция по продукту", f"course_instruction:{course['id']}")],
                [("Остановить расписание", f"course_stop:{course['id']}")], [("Добавить схему", "course_new")])
            link = specialist_button()
            if link:
                kb.inline_keyboard.insert(0, [link])
            await message.answer("\n".join(lines), parse_mode=None, reply_markup=kb)
        instant = datetime.fromisoformat(data["now"])
        for event in w.get("today_course", []):
            if event["status"] == "pending" and datetime.fromisoformat(event["occurred_at"]) <= instant:
                await message.answer(event["name"]+" · "+event["dose_text"], parse_mode=None, reply_markup=keyboard(
                    [("Выполнено", f"course_event:done:{event['id']}"), ("Пропущено", f"course_event:skipped:{event['id']}")]))
    elif root == "course_event":
        _, outcome, entry_id = action.split(":")
        await api("/workspace/course/action", {"telegram_user_id": uid, "entry_id": int(entry_id), "action": outcome})
        await dispatch(query, state, "course", professor_bot, professor_client, expert_client, panel=message)
    elif root == "course_stop":
        entry_id = int(action.split(":")[1])
        await message.answer("Остановить напоминания и будущие пункты этого расписания? Это не рекомендация прекращать лечение.", reply_markup=keyboard([("Остановить расписание", f"record:stop:{entry_id}"), ("Отмена", "course")]))
    elif root == "record":
        _, outcome, entry_id = action.split(":")
        result = await api("/workspace/action", {"telegram_user_id": uid, "entry_id": int(entry_id), "action": outcome})
        entry = result["entry"]
        await state.update_data(pending_reviews=[e for e in (await state.get_data()).get("pending_reviews", []) if e["id"] != entry["id"]])
        kind = entry.get("kind")
        text = {"confirmed": "Запись сохранена.", "cancelled": "Черновик отменён.", "stopped": "Расписание остановлено. История сохранена."}[entry["status"]]
        if entry["status"] == "confirmed":
            text = {"target": f"Норма {fmt(entry.get('kcal'))} ккал сохранена.", "program": "Программа тренировок сохранена.",
                "measurement": "Фото прогресса сохранено." if entry.get("photo_file_id") else "Замеры сохранены.", "wellbeing": "Самочувствие сохранено.", "course": "Существующая схема курса сохранена.",
                "workout": "Тренировка записана.", "activity_log": "Активность записана отдельно от тренировочной программы."}.get(kind, text)
            if kind in {"workout", "activity_log", "measurement", "wellbeing"}:
                text += "\n\n"+draft_text(entry)
        if kind in {"workout", "activity_log", "measurement", "wellbeing"} and isinstance(message, MentorPanel):
            await message.complete(text, replace=True, reply_markup=keyboard([("Открыть результаты", "workouts" if kind in {"workout", "activity_log"} else "progress")]))
        else:
            await complete_card(message, text, reply_markup=keyboard([("Открыть результаты", {"target":"nutrition", "program":"workouts", "course":"course"}.get(kind, "progress"))]))
    elif root == "record_edit":
        _, kind, entry_id = action.split(":")
        await state.update_data(replace_kind="measurement" if kind == "progress_photo" else kind, replace_id=int(entry_id))
        await edit_record(message, uid, state, entry_id, kind)
    elif action == "course_new":
        await start_form(message, uid, state, "course")
    elif root == "course_source":
        current = await state.get_data()
        course = current.get("course_data", {})
        if not course.get("dose_text"):
            raise BridgeError("Начните добавление схемы заново.")
        course["source"] = action.split(":")[1]
        course["timezone"] = data["settings"]["timezone"]
        await next_step(message, state, "course_schedule", QUESTIONS["course_schedule"], professor_client, course_data=course)
    elif action == "wellbeing":
        await state.update_data(replace_kind=None, replace_id=None)
        await form(message, state, "wellbeing_score", QUESTIONS["wellbeing_score"],
            known={}, rows=[[(str(i), f"wellbeing:{i}") for i in range(1, 6)]])
    elif root == "wellbeing":
        await form(message, state, "wellbeing_energy", QUESTIONS["wellbeing_energy"], score=int(action.split(":")[1]),
            known={"score":int(action.split(":")[1])},
            rows=[[(str(i), f"energy:{i}") for i in range(1, 6)], [("Пропустить", "energy:skip")]])
    elif root == "energy":
        if (await state.get_data()).get("form_kind") != "wellbeing_energy":
            raise BridgeError("Выберите самочувствие заново.")
        energy = None if action.endswith(":skip") else int(action.split(":")[1])
        if energy is not None and energy not in range(1, 6): raise BridgeError("Оценка должна быть от 1 до 5.")
        await next_step(message, state, "wellbeing_note", wellbeing_prompt({**(await state.get_data()), "energy_score":energy}), professor_client, energy_score=energy,
            known={**(await state.get_data()).get("form_known", {}), "energy_score":energy},
            rows=[[("Пропустить", "wellbeing_skip")]])
    elif action == "wellbeing_skip":
        values = await state.get_data()
        if values.get("form_kind") != "wellbeing_note" or "score" not in values: raise BridgeError("Начните оценку самочувствия заново.")
        await make_draft(message, uid, state, "wellbeing", {"score": values["score"], "energy_score": values.get("energy_score"), "note": ""}, key="wellbeing:"+query.id)
    elif action == "measurements":
        entries = w.get("measurements", [])
        if not entries:
            await message.answer("Сохранённых замеров и фото пока нет.", reply_markup=section_keyboard("progress"))
        for entry in entries[-10:]:
            kb = keyboard([("Показать фото", f"photo:{entry['id']}")]) if entry.get("photo_file_id") else keyboard()
            await message.answer(day(entry["occurred_at"])+"\n"+draft_text(entry), parse_mode=None, reply_markup=kb)
    elif root == "photo":
        entry = next((m for m in w.get("measurements", []) if m["id"] == int(action.split(":")[1])), None)
        if not entry or not entry.get("photo_file_id"):
            raise BridgeError("Фото не найдено.")
        await message.answer_photo(entry["photo_file_id"], caption="Личное фото прогресса", protect_content=True,
            reply_markup=keyboard([("← Фото и замеры", "measurements")]))
    elif action == "goals":
        goal = data.get("profile", {}).get("goal")
        await message.answer("Ваша цель\nСейчас: "+GOALS.get(goal, "не указана"), reply_markup=keyboard([("Снижение веса", "goal:weight_loss"), ("Набор веса", "goal:weight_gain")], [("Поддержание", "goal:maintain"), ("Своя цель", "custom_goal")], [("← Назад", "profile")]))
    elif root == "sex":
        sex = action.split(":")[1]
        evidence = {"male":"Мужской", "female":"Женский"}[sex]
        await api("/profile/update", {"telegram_user_id":uid, "expected_version":data["version"], "request_key":"sex:"+query.id,
            "source_text":evidence, "evidence":evidence, "patch":{"sex":sex}})
        await message.answer("Пол сохранён: "+evidence.lower()+".", reply_markup=keyboard([("Продолжить расчёт", "target_auto")], [("👤 Профиль", "data")]))
    elif root == "goal":
        goal = action.split(":")[1]
        evidence = {"weight_loss": "Хочу снизить вес", "weight_gain": "Хочу набрать вес", "maintain": "Хочу поддерживать вес"}[goal]
        result = await api("/profile/update", {"telegram_user_id": uid, "expected_version": data["version"], "request_key": "goal:"+query.id, "source_text": evidence, "evidence": evidence, "patch": {"goal": goal}})
        await message.answer(profile_text(result["profile"]), parse_mode=None, reply_markup=section_keyboard("profile"))
    elif root in {"meal_edit", "meal_add"}:
        await ask(message, uid, QUESTIONS[root], context=f"Исправляется черновик еды №{int(action.split(':')[1])}. Сохрани остальные продукты и замени этот черновик новым. Запись только после подтверждения.")
    elif action == "repeat_previous":
        result = await api("/workspace/meals", {"telegram_user_id": uid})
        if not result["items"]:
            await message.answer("Записанной еды пока нет.", reply_markup=section_keyboard("food"))
        else:
            await repeat_meal(message, uid, query.id, result["items"][0]["id"], response_keyboard, state=state)
    elif root == "repeat":
        await repeat_meal(message, uid, query.id, int(action.split(":")[1]), response_keyboard, state=state)
    elif root == "favorite":
        _, entry_id, enabled = action.split(":")
        await api("/workspace/meals/favorite", {"telegram_user_id": uid, "entry_id": int(entry_id), "enabled": enabled == "1"})
        values = await state.get_data()
        await library(message, uid, state, offset=values.get("library_offset", 0),
            query=values.get("library_query", ""), favorites=values.get("library_favorites", False))
    elif action == "favorites":
        await library(message, uid, state, favorites=True)
    elif root == "meals":
        offset = int(action.split(":")[1])
        previous = await state.get_data() if offset else {}
        await library(message, uid, state, offset=offset, query=previous.get("library_query", ""), favorites=previous.get("library_favorites", False))
    elif action == "privacy":
        await message.answer("Профиль и дневник привязаны к вашему Telegram ID и не публикуются. Фото хранится как приватный идентификатор Telegram. По умолчанию уведомления не содержат веса, дозировок и названий средств. Подробные отчёты включаются отдельно. Выход сохраняет записи. Удаление данных наставника не удаляет заказы, профиль мобильного приложения или сообщения из Telegram.", reply_markup=keyboard([("Выключить все напоминания", "reminder:off")], [("Удалить мои данные наставника", "privacy_erase")], [("Выйти из наставника", "leave")]))
    elif action == "privacy_erase":
        import secrets
        token = secrets.token_hex(8)
        await state.update_data(erase_token=token)
        await message.answer("Удалить профиль, вес, питание, тренировки, курс, замеры, ссылки на фото и напоминания Telegram-наставника? Восстановить их в наставнике нельзя. Заказы, данные приложения и сообщения в самом Telegram останутся.", reply_markup=keyboard([("Да, удалить мои данные", "privacy_erase_confirm:"+token)], [("Отмена", "privacy")]))
    elif root == "privacy_erase_confirm":
        from src.ai.telegram_mentor import reset_mentor_conversations, clear_opening_question, opening_question, forget_all_deliveries
        values = await state.get_data()
        if not values.get("erase_token") or action.split(":")[1] != values["erase_token"]:
            raise BridgeError("Подтвердите удаление заново в разделе «Приватность».")
        reset_mentor_conversations(uid)
        await api("/workspace/privacy/erase", {"telegram_user_id":uid, "confirmed":True})
        clear_opening_question(uid, opening_question(uid))
        forget_all_deliveries(uid)
        await state.clear()
        await message.answer("Данные Telegram-наставника удалены, напоминания отключены. Заказы, данные приложения и сообщения Telegram не удалялись.", reply_markup=keyboard())
    elif root == "reminder":
        kind = action.split(":")[1]
        values = reminder_payload(data["settings"])
        if kind == "reports":
            if values["detailed_reports"]:
                await api("/reminder/options", {"telegram_user_id": uid, **values, "detailed_reports": False})
                await reminders_view(message, uid)
            else:
                await message.answer("В вечерних и недельных сообщениях будут ваши вес и данные дневника. Они могут быть видны в уведомлениях Telegram. Включить подробные отчёты?",
                    reply_markup=keyboard([("Да, включить", "reminder_reports_yes")], [("Нет", "reminders")]))
        elif kind in {"off", "course"}:
            if kind == "off":
                values.update(morning=None, evening=None, weekly=None, inactivity_days=None, course=False)
            else:
                values["course"] = not values["course"]
            await api("/reminder/options", {"telegram_user_id": uid, **values})
            await reminders_view(message, uid)
        else:
            prompt = QUESTIONS["reminder_clock"]
            if kind == "weekly": prompt = QUESTIONS["reminder_weekly"]
            if kind == "inactivity": prompt = QUESTIONS["reminder_inactivity"]
            await form(message, state, "reminder", prompt, reminder_kind=kind)
            if kind in {"morning", "evening"}:
                clocks = ("07:00", "08:00", "09:00") if kind == "morning" else ("19:00", "20:00", "21:00")
                await message.answer(prompt+"\n\nМожно выбрать подходящее время ниже.", reply_markup=keyboard(
                    [(clock, "reminder_clock:"+clock) for clock in clocks], [("Выключить", "reminder_clock:off")]))
    elif root == "reminder_clock":
        values = await state.get_data()
        if values.get("form_kind") != "reminder" or values.get("reminder_kind") not in {"morning", "evening"}: raise BridgeError("Выберите напоминание заново.")
        settings = reminder_payload(data["settings"])
        settings[values["reminder_kind"]] = None if action.endswith(":off") else action.removeprefix("reminder_clock:")
        await api("/reminder/options", {"telegram_user_id":uid, **settings})
        await clear_input(state)
        await reminders_view(message, uid)
    elif action == "adjust":
        await message.answer("Что нужно скорректировать?", reply_markup=keyboard(
            [("🍽 Не подходит питание", "reason:food")], [("🔥 Слишком мало калорий", "reason:hunger")],
            [("🏋️ Не подходит тренировка", "reason:workout")], [("📅 Неудобное расписание", "reason:time")],
            [("🧬 Вопрос по курсу", "specialist")], [("😓 Сложно соблюдать план", "reason:adherence")], [("✍️ Другая причина", "question")]))
    elif root == "reason":
        reason = {"food": "не подходит питание", "workout": "не подходит тренировка", "adherence": "сложно соблюдать план", "hunger": "часто чувствую голод", "fatigue": "чувствую усталость", "time": "не подходит расписание", "plateau": "не вижу прогресса", "symptoms": "хочу обсудить самочувствие или побочные эффекты"}[action.split(":")[1]]
        await run_ai_action(query, state, professor_bot, professor_client, expert_client,
            "Помоги скорректировать мой план: "+reason+". Используй сохранённые данные, не начинай анкету заново. Не меняй дозировки и медицинские назначения. Не утверждай, что изменения сохранены.")
    elif await start_form(message, uid, state, action):
        pass
    else:
        return False
    return True


async def repeat_meal(message, uid, callback_id, entry_id, response_keyboard, *, state=None):
    result = await api("/workspace/meals/repeat", {"telegram_user_id": uid, "entry_id": entry_id, "request_key": "repeat:"+callback_id})
    entry = result["entry"]
    if state is not None:
        await remember_response(state, {"meal_draft":entry})
    await message.answer(f"Повторить: {entry['name']} · ≈ {fmt(entry['kcal'])} ккал?", parse_mode=None, reply_markup=response_keyboard({"meal_draft": entry}))


async def workout_result(message, entry, *, completed=False):
    volume = sum(s["weight_kg"]*s["reps"] for s in entry["sets"])
    from .mentor_insights import workout_insight
    duration = f"\nДлительность: {fmt(entry['duration_minutes'])} мин" if entry.get("duration_minutes") is not None else ""
    text = f"✅ Тренировка завершена\nВыполнено {len(entry['sets'])} подходов\nОбщий объём: {fmt(volume)} кг"+duration+"\n\n"+workout_insight(entry)
    if completed:
        await complete_card(message, text, reply_markup=keyboard([("🏋️ Тренировки", "workouts")]))
    else:
        await message.answer(text, reply_markup=keyboard([("🏋️ Тренировки", "workouts")]))


async def begin_set(message, state, entry, day, next_name):
    await state.update_data(workout_id=entry["id"], program_day=day, planned_exercise=next_name, pending_set=None, set_receipt=None,
        form_kind=None, form_known={"exercise": next_name} if next_name else {})
    if next_name:
        await form(message, state, "set_weight", f"Упражнение: {next_name}. "+QUESTIONS["set_weight"], known={"exercise":next_name})
    else:
        await form(message, state, "set_name", QUESTIONS["set_name"])


def next_exercise(entry, weekday):
    for exercise in entry.get("program_exercises", []):
        if exercise["weekday"] == weekday and sum(s["exercise"] == exercise["name"] for s in entry["sets"]) < exercise["sets"]:
            return exercise["name"]
    return None


async def append_program_exercise(message, state, reps):
    values = await state.get_data()
    if values.get("form_kind") != "program_reps":
        raise BridgeError("Это упражнение уже добавлено. Добавьте следующее или сохраните программу.")
    if not 1 <= reps <= 200: raise ValueError()
    exercise = {"weekday": values["program_weekday"], "name": values["program_name"], "sets": values["program_sets"], "reps": reps}
    exercises = [*values.get("program_exercises", []), exercise]
    if len(exercises) > 100: raise ValueError()
    await state.set_state(None)
    await state.update_data(program_exercises=exercises, form_kind=None)
    await message.answer(draft_text({"kind":"program", "exercises":exercises}), parse_mode=None,
        reply_markup=keyboard([("➕ Добавить упражнение", "program_day")], [("Проверить и сохранить", "program_done")]))


@router.message(Input.value)
async def receive(message: Message, state, professor_client=None, professor_bot=None, expert_client=None):
    if getattr(message, "voice", None) or getattr(message, "video_note", None):
        from .mentor_messages import spoken_message
        try:
            message = await spoken_message(message, state, professor_client)
        except Exception:
            await message.answer("Сейчас не получилось обработать запись. Уже собранное осталось; попробуйте позже или ответьте текстом.")
            return
        if message is None: return
    from .mentor_panel import reply_panel
    panel = await reply_panel(message, state)
    try:
        async with input_lock(message.from_user.id):
            await receive_value(panel if panel is not None else message, state, professor_client,
                professor_bot=professor_bot, expert_client=expert_client, original=message)
    finally:
        if panel is not None:
            await panel.flush()


async def receive_value(message, state, professor_client=None, *, normalized=None, professor_bot=None, expert_client=None, original=None):
    uid = message.from_user.id
    if not mentor_enabled(uid):
        await state.clear()
        return
    values = await state.get_data()
    kind = values.get("form_kind")
    text = (message.text or "").strip()
    try:
        if normalized is not None:
            text = normalized
        elif kind in MODELS and not (kind == "progress_photo" and getattr(message, "photo", None)):
            text = await parse_step(message, state, kind, professor_client)
            if text is None:
                current = await state.get_data()
                intent = current.get("form_intent")
                if intent == "cancel":
                    await clear_input(state)
                    await message.answer("Отменено. Ничего не записывал.", reply_markup=keyboard())
                elif intent in {"pause", "unknown"}:
                    await state.update_data(form_paused=True, form_intent=None)
                    await message.answer("Хорошо, оставим это пока. Уже собранное осталось — вернёмся, когда будет удобно.", reply_markup=keyboard([("Отмена", "menu")]))
                elif intent == "question":
                    await state.update_data(form_intent=None)
                    await converse(original or (message.message if isinstance(message, ReplyCards) else message),
                        state, professor_bot, professor_client, expert_client)
                return
            values = await state.get_data()
        if kind == "weight":
            weight = number(text)
            statement = f"Подтверждаю мой вес: {weight:g} кг."
            await review_input(message, state, "weight", {"weight": weight,
                "expected_version": values.get("form_profile_version"),
                "source_text": (message.text or "")+"\n"+statement, "evidence": statement,
                "request_key": request_key(message)}, f"Правильно понял: ваш текущий вес {fmt(weight)} кг?")
        elif kind == "timezone":
            await review_input(message, state, "timezone", {"timezone": text}, "Ваш часовой пояс: "+timezone_label(text)+". Сохранить?")
        elif kind == "program_name":
            if not text or len(text) > 120: raise ValueError()
            if values.get("form_known", {}).get("weekday") is not None:
                await state.update_data(program_weekday=values["form_known"]["weekday"])
            await next_step(message, state, "program_sets", QUESTIONS["program_sets"], professor_client, program_name=text,
                rows=[[(str(i), f"program_sets:{i}") for i in range(1, 6)]])
        elif kind == "program_sets":
            sets = int(text)
            if not 1 <= sets <= 30: raise ValueError()
            await next_step(message, state, "program_reps", QUESTIONS["program_reps"], professor_client, program_sets=sets,
                rows=[[(str(i), f"program_reps:{i}") for i in (6, 8, 10, 12, 15)]])
        elif kind == "program_reps":
            await append_program_exercise(message, state, int(text))
        elif kind == "set_name":
            if not text or len(text)>120: raise ValueError()
            await next_step(message, state, "set_weight", QUESTIONS["set_weight"], professor_client, planned_exercise=text)
        elif kind == "set_weight":
            weight=number(text)
            if not 0 <= weight <= 1000: raise ValueError()
            await next_step(message, state, "set_reps", QUESTIONS["set_reps"], professor_client, set_weight=weight)
        elif kind == "set_reps":
            reps=int(text)
            if not 1 <= reps <= 1000: raise ValueError()
            pending={"exercise": values["planned_exercise"], "weight_kg": values["set_weight"], "reps": reps}
            receipt=str(message.message_id)
            await state.set_state(None)
            await state.update_data(pending_set=pending, set_receipt=receipt, set_request_key=request_key(message), text_confirmation_ready=True)
            await message.answer(f"{pending['exercise']}: {fmt(pending['weight_kg'])} кг × {reps}\nПодход завершён?", parse_mode=None,
                reply_markup=keyboard([("✅ Да, записать", "set_confirm:"+receipt)], [("✏️ Изменить", "set_edit:"+receipt)]))
        elif kind == "target":
            parts = text.split()
            if len(parts) not in {1, 4}: raise ValueError()
            data = dict(zip(("kcal", "protein", "fat", "carbs"), map(number, parts)))
            await make_draft(message, uid, state, "target", {**data, "source": "user"})
        elif kind == "program":
            exercises = []
            for line in text.splitlines():
                day, name, sets, reps = [p.strip() for p in line.split("|")]
                exercises.append({"weekday": int(day)-1, "name": name, "sets": int(sets), "reps": int(reps)})
            await make_draft(message, uid, state, "program", {"exercises": exercises})
        elif kind == "measurement":
            parts = text.split()
            if len(parts) != 3: raise ValueError()
            retained = {k:v for k,v in values.get("original_record", {}).items() if k in {"photo_file_id", "note"}}
            await make_draft(message, uid, state, "measurement", {**retained, **{key: number(value) for key, value in zip(("waist_cm", "chest_cm", "hips_cm"), parts) if value != "-"}})
        elif kind == "progress_photo":
            if not message.photo:
                raise ValueError()
            retained = {k:v for k,v in values.get("original_record", {}).items() if k in {"waist_cm", "chest_cm", "hips_cm", "note"}}
            await make_draft(message, uid, state, "measurement", {**retained, "photo_file_id": message.photo[-1].file_id, "note": message.caption or retained.get("note", "")})
        elif kind == "wellbeing_score":
            score = int(text)
            if score not in range(1, 6): raise ValueError()
            await next_step(message, state, "wellbeing_energy", QUESTIONS["wellbeing_energy"], professor_client,
                score=score, rows=[[(str(i), f"energy:{i}") for i in range(1, 6)], [("Пропустить", "energy:skip")]])
        elif kind == "wellbeing_energy":
            energy = None if text == "-" else int(text)
            if energy is not None and not 1 <= energy <= 5: raise ValueError()
            await next_step(message, state, "wellbeing_note", wellbeing_prompt({**values, "energy_score":energy}), professor_client,
                energy_score=energy, rows=[[("Пропустить", "wellbeing_skip")]])
        elif kind == "wellbeing_note":
            await make_draft(message, uid, state, "wellbeing", {"score": values["score"], "energy_score": values.get("energy_score"), "note": "" if text == "-" else text})
        elif kind == "wellbeing_record":
            await make_draft(message, uid, state, "wellbeing", json.loads(text))
        elif kind == "meal_search":
            if not text: raise ValueError()
            await state.set_state(None)
            await library(message, uid, state, query=text)
        elif kind == "custom_goal":
            if not text: raise ValueError()
            data = await api("/dashboard", {"telegram_user_id": uid})
            await api("/profile/update", {"telegram_user_id": uid, "expected_version": data["version"], "request_key": request_key(message), "source_text": text, "evidence": text, "patch": {"goal": "custom", "goal_detail": text}})
            await clear_input(state)
            await message.answer("Ваша цель сохранена.", reply_markup=keyboard([("Профиль", "profile")]))
        elif kind == "course":
            if not text or len(text) > 120: raise ValueError()
            await next_step(message, state, "course_dose", QUESTIONS["course_dose"], professor_client, course_data={"name": text})
        elif kind == "course_reminder":
            clock=None if text=="-" else text
            if clock is not None and (len(clock)!=5 or time.fromisoformat(clock).tzinfo is not None): raise ValueError()
            await state.set_state(None)
            await state.update_data(pending_reminder_time=clock, text_confirmation_ready=True,
                pending_input={"kind":"course_reminder", "token":secrets.token_hex(8),
                    "payload":{"entry_id":values["course_id"], "reminder_time":clock}})
            await message.answer("Новое время уведомления: "+(clock or "по сохранённой схеме")+". Сохранить?", reply_markup=keyboard([("✅ Сохранить", "course_reminder_save")], [("Отмена", "course")]))
        elif kind == "course_dose":
            if not text or len(text) > 240: raise ValueError()
            await state.update_data(course_data={**values["course_data"], "dose_text": text})
            await state.set_state(None)
            await message.answer("Эту схему назначил специалист?", reply_markup=keyboard([("Назначил специалист", "course_source:specialist"), ("Моя существующая схема", "course_source:user")]))
        elif kind == "course_schedule":
            start, end, days, clocks = [p.strip() for p in text.split("|")]
            schedule = {"interval_days": int(days.split("=", 1)[1])} if days.lower().startswith("интервал=") else {"weekdays": [int(d.strip())-1 for d in days.split(",")]}
            if not 0 <= (date.fromisoformat(end)-date.fromisoformat(start)).days <= 365:
                raise ValueError()
            if "interval_days" in schedule and not 1 <= schedule["interval_days"] <= 365:
                raise ValueError()
            weekdays = schedule.get("weekdays", [])
            if len(set(weekdays)) != len(weekdays) or any(day not in range(7) for day in weekdays):
                raise ValueError()
            times = [c.strip() for c in clocks.split(",")]
            if not 1 <= len(times) <= 8 or len(set(times)) != len(times): raise ValueError()
            for clock in times:
                if len(clock) != 5 or time.fromisoformat(clock).tzinfo is not None: raise ValueError()
            course = {**values["course_data"], "start_date": start, "end_date": end, **schedule, "times": times}
            await next_step(message, state, "course_supply", QUESTIONS["course_supply"], professor_client, course_data=course)
        elif kind == "course_record":
            merged = {**values["course_data"], **json.loads(text)}
            allowed = {"name","dose_text","source","timezone","start_date","end_date","weekdays","interval_days","times","supply_amount","amount_per_intake","supply_unit"}
            await make_draft(message, uid, state, "course", {k:v for k,v in merged.items() if k in allowed and v is not None})
        elif kind == "course_supply":
            course = values["course_data"]
            if text != "-":
                stock, amount, unit = [p.strip() for p in text.split("|")]
                course = {**course, "supply_amount": number(stock), "amount_per_intake": number(amount), "supply_unit": unit}
            await make_draft(message, uid, state, "course", course)
        elif kind == "workout_set":
            parts = [p.strip() for p in text.split("|")]
            if len(parts) == 2 and values.get("planned_exercise"):
                name = values["planned_exercise"]
                weight, reps = parts
            else:
                name, weight, reps = parts
            pending = {"exercise": name, "weight_kg": number(weight), "reps": int(reps)}
            receipt = str(message.message_id)
            await state.set_state(None)
            await state.update_data(pending_set=pending, set_receipt=receipt, set_request_key=request_key(message), text_confirmation_ready=True)
            await message.answer(f"{name}: {fmt(pending['weight_kg'])} кг × {pending['reps']}. Записать подход?", parse_mode=None,
                reply_markup=keyboard([("Да, записать", "set_confirm:"+receipt)], [("Изменить", "set_edit:"+receipt)]))
        elif kind == "workout_duration":
            await review_input(message, state, "workout_duration", {"entry_id": values["workout_id"], "duration_minutes": number(text)},
                f"Тренировка длилась {fmt(number(text))} минут. Завершить и сохранить?")
        elif kind == "reminder":
            data = await api("/dashboard", {"telegram_user_id": uid})
            settings = reminder_payload(data["settings"])
            reminder = values["reminder_kind"]
            if reminder == "inactivity":
                settings["inactivity_days"] = None if text == "-" else int(text)
            elif reminder == "weekly" and text != "-":
                day, clock = text.split()
                settings.update(weekday=int(day)-1, weekly=clock)
            else:
                settings[reminder] = None if text == "-" else text
            change = {k: settings[k] for k in ("weekday", "weekly")} if reminder == "weekly" else {"inactivity_days" if reminder == "inactivity" else reminder: settings["inactivity_days" if reminder == "inactivity" else reminder]}
            label = "выключить" if text == "-" else (f"{DAYS[settings['weekday']]} в {settings['weekly']}" if reminder == "weekly" else f"через {settings['inactivity_days']} дн." if reminder == "inactivity" else settings[reminder])
            await review_input(message, state, "reminder", change, "Напоминание: "+label+". Сохранить?")
        else:
            await clear_input(state)
            await message.answer("Откройте раздел заново.", reply_markup=keyboard())
    except (ValueError, KeyError, BridgeError) as error:
        detail = str(error) if isinstance(error, BridgeError) and error.status != 422 else "Не получилось сохранить запись. Уже собранные данные остались."
        await message.answer(detail+"\n\n"+values.get("form_question", values.get("form_prompt", "")), parse_mode=None, reply_markup=keyboard([("Отмена", "menu")]))
