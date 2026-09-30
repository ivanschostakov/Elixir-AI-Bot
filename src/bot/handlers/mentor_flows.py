"""Human-confirmed Telegram forms; model prose never stands in for persistence."""
from datetime import date, datetime, time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
import config

from src.ai.telegram_mentor import api, BridgeError, mentor_enabled

router = Router(name="mentor_forms")


class Input(StatesGroup):
    value = State()


def keyboard(*rows):
    from .mentor import button
    return InlineKeyboardMarkup(inline_keyboard=[*[ [button(label, action) for label, action in row] for row in rows],
        [button("← Меню наставника", "menu")]])


def fmt(value):
    return f"{float(value):g}" if value is not None else "нет данных"


def number(value):
    return float(value.strip().replace(",", "."))


def request_key(message):
    return f"tg:{message.chat.id}:{message.message_id}"


def specialist_button():
    value = config.env("TELEGRAM_MENTOR_SPECIALIST_URL", "").strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    return InlineKeyboardButton(text="Связаться со специалистом", url=value)


def reminder_payload(settings):
    old = settings.get("reminders", {})
    return {"timezone": settings.get("timezone", "Europe/Moscow"), "morning": old.get("morning"),
        "evening": old.get("evening", settings.get("daily_time")), "weekly": old.get("weekly"),
        "weekday": old.get("weekday", 6), "inactivity_days": old.get("inactivity_days"), "course": old.get("course", False)}


async def reminders_view(message, uid):
    settings = (await api("/dashboard", {"telegram_user_id": uid}))["settings"]
    values = reminder_payload(settings)
    text = "Напоминания\n\n" + "\n".join([
        f"Утренний вес: {values['morning'] or 'выключено'}",
        f"Вечерний итог: {values['evening'] or 'выключено'}",
        f"За неделю: {values['weekly'] or 'выключено'} (день {values['weekday']+1})",
        f"После паузы: {str(values['inactivity_days'])+' дн.' if values['inactivity_days'] else 'выключено'}",
        f"Курс по расписанию: {'включено' if values['course'] else 'выключено'}",
        f"Часовой пояс: {values['timezone']}"])
    await message.answer(text, parse_mode=None, reply_markup=keyboard(
        [("Утренний вес", "reminder:morning"), ("Вечерний итог", "reminder:evening")],
        [("За неделю", "reminder:weekly"), ("После паузы", "reminder:inactivity")],
        [("Выключить курс" if values["course"] else "Включить курс", "reminder:course")],
        [("Выключить все", "reminder:off"), ("Часовой пояс", "timezone")]))


async def form(message, state, kind, prompt, **data):
    await state.set_state(Input.value)
    await state.update_data(form_kind=kind, **data)
    await message.answer(prompt, parse_mode=None, reply_markup=keyboard([("Отмена", "menu")]))


def draft_text(entry):
    kind = entry["kind"]
    if kind == "course":
        lines = ["Проверьте существующую схему", entry["name"], "Дозировка: "+entry["dose_text"],
            "Источник: "+("специалист" if entry["source"] == "specialist" else "ваша схема"),
            f"{entry['start_date']} - {entry['end_date']}",
            (f"Каждые {entry['interval_days']} дн. от даты начала" if entry.get("interval_days") else
             "Дни недели (пн=1): "+", ".join(str(d+1) for d in entry["weekdays"])),
            "Время: "+", ".join(entry["times"])+" · "+entry["timezone"],
            "Это запись вашей схемы, а не назначение. Дозировки бот не подбирает."]
        if entry.get("supply_amount") is not None:
            lines.append(f"Запас {fmt(entry['supply_amount'])} {entry['supply_unit']}; на приём {fmt(entry['amount_per_intake'])} {entry['supply_unit']}")
        return "\n".join(lines)
    if kind == "program":
        return "Программа на неделю\n"+"\n".join(f"День {e['weekday']+1}: {e['name']} · {e['sets']} × {e['reps']}" for e in entry["exercises"])
    if kind == "target":
        return "Сохранить вашу дневную норму?\n"+"\n".join(f"{label}: {fmt(entry.get(key))}" for key, label in [("kcal", "Ккал"), ("protein", "Белки, г"), ("fat", "Жиры, г"), ("carbs", "Углеводы, г")])
    if kind == "wellbeing":
        return f"Самочувствие: {entry['score']}/5\nЭнергия: {fmt(entry.get('energy_score'))}\n{entry.get('note', '')}"
    if kind == "measurement":
        return "Замеры\n"+"\n".join(f"{label}: {fmt(entry.get(key))} см" for key, label in [("waist_cm", "Талия"), ("chest_cm", "Грудь"), ("hips_cm", "Бёдра")])+ ("\nЛичное фото приложено." if entry.get("photo_file_id") else "")
    return "Проверьте запись перед сохранением."


async def show_draft(message, entry):
    text = draft_text(entry)
    edit_kind = "progress_photo" if entry.get("photo_file_id") else entry["kind"]
    for start in range(0, len(text), 3500):
        await message.answer(text[start:start+3500], parse_mode=None,
            reply_markup=keyboard([("Сохранить", f"record:confirm:{entry['id']}"), ("Отмена", f"record:cancel:{entry['id']}")],
                [("Исправить", f"record_edit:{edit_kind}:{entry['id']}")]) if start+3500 >= len(text) else None)


async def make_draft(message, uid, state, kind, data):
    saved = await state.get_data()
    payload = {"telegram_user_id": uid, "request_key": request_key(message), "kind": kind, "data": data}
    if saved.get("replace_kind") == kind:
        payload["replaces_id"] = saved.get("replace_id")
    result = await api("/workspace/draft", payload)
    await state.clear()
    await show_draft(message, result["entry"])


def today_view(data):
    w = data.get("workspace", {})
    p = data.get("profile", {})
    lines = ["Сегодня · "+data["date"], "", f"Питание: ≈ {fmt(data['totals']['kcal'])} ккал"]
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


def weekly_view(data):
    w = data["workspace"]["weekly"]
    diff = w["weight_change_kg"]
    return "\n".join([f"Прогресс за {w['days']} дней · {w.get('from', '')} - {w.get('to', '')}", "",
        f"Вес: {diff:+g} кг" if diff is not None else "Вес: недостаточно измерений за период",
        f"Средний вес за 7 дней: {fmt(w.get('weight_mean_7d'))} кг · измерений {w.get('weight_samples_7d', 0)}",
        f"Среднее самочувствие: {fmt(w.get('wellbeing_mean'))}/5 · записей {w.get('wellbeing_samples', 0)}",
        f"Средняя энергия: {fmt(w.get('energy_mean'))}/5 · записей {w.get('energy_samples', 0)}",
        f"Записано приёмов пищи: {w['meals']}", f"Калории в записях: ≈ {fmt(w['nutrition']['kcal'])} ккал",
        f"Тренировок: {w['workouts']}", f"Длительность: {fmt(w['duration_minutes'])} мин",
        f"Объём (вес × повторы): {fmt(w['volume_kg'])} кг",
        f"Курс: выполнено {w['course_done']} из {w['course_due']} запланированных приёмов",
        "", "Показаны только сохранённые записи. Отсутствие записи не означает ноль или пропуск питания."])


async def library(message, uid, state, *, offset=0, favorites=False, query=""):
    result = await api("/workspace/meals", {"telegram_user_id": uid, "offset": offset, "query": query, "favorites_only": favorites})
    await state.update_data(library_query=query, library_favorites=favorites)
    if not result["items"]:
        await message.answer("Подходящих записей нет.", reply_markup=keyboard([("Добавить еду", "meal")]))
        return
    for meal in result["items"]:
        await message.answer(f"{meal['name']}\n≈ {fmt(meal['kcal'])} ккал · Б {fmt(meal['protein'])} / Ж {fmt(meal['fat'])} / У {fmt(meal['carbs'])}\n{meal['occurred_at'][:10]}",
            parse_mode=None, reply_markup=keyboard([("Повторить", f"repeat:{meal['id']}"),
                ("Убрать из избранного" if meal.get("favorite") else "В избранное", f"favorite:{meal['id']}:{0 if meal.get('favorite') else 1}")]))
    if result.get("has_more"):
        await message.answer("История еды", reply_markup=keyboard([("Далее", f"meals:{offset+10}")]))


async def start_form(message, uid, state, action, *, editing=False):
    prompts = {
        "target": "Введите вашу согласованную дневную норму: ккал белки жиры углеводы. Например: 2000 100 70 240. Можно указать только калории. Бот не назначает эту норму.",
        "program": "Введите упражнения недельной программы, каждое с новой строки:\nдень недели (пн=1, вс=7) | упражнение | подходы | повторы\nНапример: 1 | Присед | 3 | 10",
        "measurement": "Введите замеры в сантиметрах: талия грудь бёдра. Для неизвестного значения укажите -.",
        "progress_photo": "Пришлите личное фото прогресса. Оно сохранится только после вашего подтверждения; публичная ссылка не создаётся.",
        "meal_search": "Введите название блюда для поиска в вашем дневнике.",
        "custom_goal": "Какова ваша цель?",
        "course": "Введите название средства из вашей существующей схемы. Бот не назначает и не меняет лечение или дозировки.",
    }
    if action not in prompts:
        return False
    if not editing:
        await state.update_data(replace_kind=None, replace_id=None)
    await form(message, state, action, prompts[action])
    return True


async def dispatch(query, state, action, professor_bot, professor_client, expert_client):
    from .mentor import ask, run_ai_action, response_keyboard, section_keyboard, profile_text
    uid = query.from_user.id
    message = query.message
    # Apply server-side gates to stale keyboards as well as the current menu.
    data = await api("/dashboard", {"telegram_user_id": uid})
    w = data.get("workspace", {})
    gates = w.get("sections", {})
    root = action.split(":", 1)[0]
    section = {"meal": "food", "nutrition": "food", "suggest": "food", "target": "food", "meal_search": "food", "favorites": "food", "meals": "food", "repeat": "food", "repeat_previous": "food", "favorite": "food", "meal_edit": "food", "meal_confirm": "food", "meal_cancel": "food",
        "program": "workouts", "workout_start": "workouts", "workout_set": "workouts", "workout_finish": "workouts", "workout_duration": "workouts",
        "course_event": "course", "course_stop": "course", "course_new": "course", "course_source": "course",
        "weight": "progress", "history": "progress", "weekly": "progress", "monthly": "progress", "measurement": "progress", "measurements": "progress", "progress_photo": "progress", "photo": "progress",
        "reminder": "settings", "reminders": "settings", "privacy": "settings", "timezone": "settings",
        "data": "profile", "goals": "profile", "goal": "profile", "custom_goal": "profile",
        "question": "ask", "adjust": "ask", "reason": "ask", "daily_plan": "ask", "wellbeing": "today"}.get(root, root)
    closed = {x.strip() for x in config.env("TELEGRAM_MENTOR_CLOSED_SECTIONS", "").split(",")}
    if not gates.get(section, True) or section in closed:
        await message.answer("Раздел временно отключён.", reply_markup=keyboard())
        return True
    if action == "today":
        await message.answer(today_view(data)[:3900], parse_mode=None, reply_markup=keyboard(
            [("Добавить еду", "meal")], [("Начать тренировку", "workout_start"), ("Мой курс", "course")],
            [("Самочувствие", "wellbeing"), ("Прогресс", "progress")], [("Скорректировать план", "adjust")]))
    elif action in {"weekly", "monthly", "progress"}:
        if action == "monthly":
            data["workspace"]["weekly"] = await api("/workspace/report", {"telegram_user_id": uid, "days": 30})
        await message.answer(weekly_view(data), parse_mode=None, reply_markup=section_keyboard("progress"))
    elif action == "workouts":
        program = w.get("program")
        text = draft_text(program) if program else "Программа на неделю пока не сохранена."
        if w.get("active_workout"):
            text += f"\n\nАктивная тренировка: {len(w['active_workout']['sets'])} подходов."
        await message.answer(text[:3900], parse_mode=None, reply_markup=keyboard(
            [("Начать / продолжить", "workout_start")], [("Изменить программу", "program"), ("За неделю", "weekly")]))
    elif action == "workout_start":
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
        prompt = f"\nСледующее упражнение: {next_name}. Введите вес в кг | повторы." if next_name else "\nВведите упражнение | вес в кг | повторы."
        await form(message, state, "workout_set", (f"Тренировка · {len(entry['sets'])} подходов записано.\n"+plan_text+prompt+"\nДля другого упражнения укажите его название первым полем.")[:3900], workout_id=entry["id"], planned_exercise=next_name, program_day=day)
        await message.answer("Тренировка", reply_markup=keyboard([("Завершить", f"workout_finish:{entry['id']}"), ("Указать длительность", f"workout_duration:{entry['id']}")]))
    elif root == "workout_finish":
        result = await api("/workspace/workout/finish", {"telegram_user_id": uid, "entry_id": int(action.split(":")[1])})
        await workout_result(message, result["entry"])
    elif root == "workout_duration":
        await form(message, state, "workout_duration", "Сколько минут длилась тренировка?", workout_id=int(action.split(":")[1]))
    elif action == "course":
        courses = w.get("courses", [])
        if not courses:
            await message.answer("Существующая схема ещё не сохранена. Добавьте только назначения специалиста или вашу уже существующую схему; бот не подбирает дозировки.", reply_markup=keyboard([("Добавить существующую схему", "course_new")]))
        for course in courses:
            lines = [draft_text(course), f"\nВыполнено {course['done']} из {course['due']} наступивших приёмов."]
            supply = course.get("supply")
            if supply:
                lines.append(f"Запас по отметкам: {fmt(supply['remaining'])} {supply['unit']} · на {supply['intakes_available']} приёмов.")
                if supply["runs_out_at"]:
                    lines.append("Первый приём без достаточного запаса: "+supply["runs_out_at"])
            else:
                lines.append("Запас не рассчитан: количество и расход в одинаковых единицах не указаны.")
            for event in course.get("calendar", [])[:14]:
                clock = datetime.fromisoformat(event["occurred_at"]).astimezone(ZoneInfo(course["timezone"])).strftime("%d.%m %H:%M")
                lines.append(f"{clock} · "+{"done": "выполнено", "skipped": "пропущено", "pending": "по расписанию", "cancelled": "отменено"}.get(event["status"], event["status"]))
            kb = keyboard([("Остановить расписание", f"course_stop:{course['id']}")], [("Добавить схему", "course_new"), ("Самочувствие", "wellbeing")])
            link = specialist_button()
            if link:
                kb.inline_keyboard.insert(0, [link])
            await message.answer("\n".join(lines)[:3900], parse_mode=None, reply_markup=kb)
        instant = datetime.fromisoformat(data["now"])
        for event in w.get("today_course", []):
            if event["status"] == "pending" and datetime.fromisoformat(event["occurred_at"]) <= instant:
                await message.answer(event["name"]+" · "+event["dose_text"], parse_mode=None, reply_markup=keyboard(
                    [("Выполнено", f"course_event:done:{event['id']}"), ("Пропущено", f"course_event:skipped:{event['id']}")]))
    elif root == "course_event":
        _, outcome, entry_id = action.split(":")
        await api("/workspace/course/action", {"telegram_user_id": uid, "entry_id": int(entry_id), "action": outcome})
        await message.answer("Отметка сохранена.", reply_markup=keyboard([("Мой курс", "course")]))
    elif root == "course_stop":
        entry_id = int(action.split(":")[1])
        await message.answer("Остановить напоминания и будущие пункты этого расписания? Это не рекомендация прекращать лечение.", reply_markup=keyboard([("Остановить расписание", f"record:stop:{entry_id}"), ("Отмена", "course")]))
    elif root == "record":
        _, outcome, entry_id = action.split(":")
        result = await api("/workspace/action", {"telegram_user_id": uid, "entry_id": int(entry_id), "action": outcome})
        await message.edit_reply_markup(reply_markup=keyboard())
        await message.answer({"confirmed": "Запись сохранена.", "cancelled": "Черновик отменён.", "stopped": "Расписание остановлено. История сохранена."}[result["entry"]["status"]], reply_markup=keyboard())
    elif root == "record_edit":
        _, kind, entry_id = action.split(":")
        await state.update_data(replace_kind="measurement" if kind == "progress_photo" else kind, replace_id=int(entry_id))
        if kind == "wellbeing":
            await message.answer("Самочувствие: 1 — очень плохо, 5 — хорошо.", reply_markup=keyboard([(str(i), f"wellbeing:{i}") for i in range(1, 6)]))
        elif not await start_form(message, uid, state, kind, editing=True):
            raise BridgeError("Этот тип записи нельзя исправить из старой кнопки.")
    elif action == "course_new":
        await start_form(message, uid, state, "course")
    elif root == "course_source":
        current = await state.get_data()
        course = current.get("course_data", {})
        if not course.get("dose_text"):
            raise BridgeError("Начните добавление схемы заново.")
        course["source"] = action.split(":")[1]
        course["timezone"] = data["settings"]["timezone"]
        await form(message, state, "course_schedule", "Введите: дата начала | дата конца | дни недели (пн=1) или интервал | время\nНапример: 2026-10-01 | 2026-10-31 | 1,4 | 09:00\nДля интервала замените 1,4 на интервал=10 (каждые 10 дней от начала). Несколько времён через запятую. Другие средства можно добавить отдельными пунктами курса.", course_data=course)
    elif action == "wellbeing":
        await state.update_data(replace_kind=None, replace_id=None)
        await message.answer("Как ваше самочувствие? 1 — очень плохо, 5 — хорошо.", reply_markup=keyboard([(str(i), f"wellbeing:{i}") for i in range(1, 6)]))
    elif root == "wellbeing":
        await form(message, state, "wellbeing_energy", "Уровень энергии от 1 (очень мало) до 5 (много)? Отправьте - без оценки.", score=int(action.split(":")[1]))
    elif action == "measurements":
        entries = w.get("measurements", [])
        if not entries:
            await message.answer("Сохранённых замеров и фото пока нет.", reply_markup=section_keyboard("progress"))
        for entry in entries[-10:]:
            kb = keyboard([("Показать фото", f"photo:{entry['id']}")]) if entry.get("photo_file_id") else keyboard()
            await message.answer(entry["occurred_at"][:10]+"\n"+draft_text(entry), parse_mode=None, reply_markup=kb)
    elif root == "photo":
        entry = next((m for m in w.get("measurements", []) if m["id"] == int(action.split(":")[1])), None)
        if not entry or not entry.get("photo_file_id"):
            raise BridgeError("Фото не найдено.")
        await message.answer_photo(entry["photo_file_id"], caption="Личное фото прогресса", protect_content=True)
    elif action == "goals":
        await message.answer("Ваша цель", reply_markup=keyboard([("Снижение веса", "goal:weight_loss"), ("Набор веса", "goal:weight_gain")], [("Поддержание", "goal:maintain"), ("Своя цель", "custom_goal")]))
    elif root == "goal":
        goal = action.split(":")[1]
        evidence = {"weight_loss": "Хочу снизить вес", "weight_gain": "Хочу набрать вес", "maintain": "Хочу поддерживать вес"}[goal]
        result = await api("/profile/update", {"telegram_user_id": uid, "expected_version": data["version"], "request_key": "goal:"+query.id, "source_text": evidence, "evidence": evidence, "patch": {"goal": goal}})
        await message.answer(profile_text(result["profile"]), parse_mode=None, reply_markup=section_keyboard("profile"))
    elif root == "meal_edit":
        await ask(message, uid, f"Что исправить в оценке еды №{int(action.split(':')[1])}? Укажите состав или порцию. Исправление будет новым черновиком, пока вы его не подтвердите.")
    elif action == "repeat_previous":
        result = await api("/workspace/meals", {"telegram_user_id": uid})
        if not result["items"]:
            await message.answer("Записанной еды пока нет.", reply_markup=section_keyboard("food"))
        else:
            await repeat_meal(message, uid, query.id, result["items"][0]["id"], response_keyboard)
    elif root == "repeat":
        await repeat_meal(message, uid, query.id, int(action.split(":")[1]), response_keyboard)
    elif root == "favorite":
        _, entry_id, enabled = action.split(":")
        await api("/workspace/meals/favorite", {"telegram_user_id": uid, "entry_id": int(entry_id), "enabled": enabled == "1"})
        await message.answer("Избранное обновлено.", reply_markup=section_keyboard("food"))
    elif action == "favorites":
        await library(message, uid, state, favorites=True)
    elif root == "meals":
        offset = int(action.split(":")[1])
        previous = await state.get_data() if offset else {}
        await library(message, uid, state, offset=offset, query=previous.get("library_query", ""), favorites=previous.get("library_favorites", False))
    elif action == "privacy":
        await message.answer("Профиль и дневник привязаны к вашему Telegram ID и не публикуются. Фото хранится как приватный идентификатор Telegram. В уведомлениях нет веса, дозировок и названий средств. Выход из наставника сохраняет записи; напоминания отключаются отдельно.", reply_markup=keyboard([("Выключить все напоминания", "reminder:off")], [("Выйти из наставника", "leave")]))
    elif root == "reminder":
        kind = action.split(":")[1]
        values = reminder_payload(data["settings"])
        if kind in {"off", "course"}:
            if kind == "off":
                values.update(morning=None, evening=None, weekly=None, inactivity_days=None, course=False)
            else:
                values["course"] = not values["course"]
            await api("/reminder/options", {"telegram_user_id": uid, **values})
            await reminders_view(message, uid)
        else:
            prompt = "Введите время HH:MM или - для выключения."
            if kind == "weekly": prompt = "Введите день недели (пн=1, вс=7) и время, например 7 19:00; - для выключения."
            if kind == "inactivity": prompt = "Через сколько дней без записей напомнить (1-30)? Отправьте - для выключения."
            await form(message, state, "reminder", prompt, reminder_kind=kind)
    elif action == "adjust":
        await message.answer("Что нужно скорректировать?", reply_markup=keyboard(
            [("Голод", "reason:hunger"), ("Усталость", "reason:fatigue")], [("Нет времени", "reason:time"), ("Нет прогресса", "reason:plateau")],
            [("Самочувствие / побочные эффекты", "reason:symptoms")], [("Другая причина", "question")]))
    elif root == "reason":
        reason = {"hunger": "часто чувствую голод", "fatigue": "чувствую усталость", "time": "не хватает времени", "plateau": "не вижу прогресса", "symptoms": "хочу обсудить самочувствие или побочные эффекты"}[action.split(":")[1]]
        await run_ai_action(query, state, professor_bot, professor_client, expert_client,
            "Помоги скорректировать мой план: "+reason+". Используй сохранённые данные, не начинай анкету заново. Не меняй дозировки и медицинские назначения. Не утверждай, что изменения сохранены.")
    elif await start_form(message, uid, state, action):
        pass
    else:
        return False
    return True


async def repeat_meal(message, uid, callback_id, entry_id, response_keyboard):
    result = await api("/workspace/meals/repeat", {"telegram_user_id": uid, "entry_id": entry_id, "request_key": "repeat:"+callback_id})
    entry = result["entry"]
    await message.answer(f"Повторить: {entry['name']} · ≈ {fmt(entry['kcal'])} ккал?", parse_mode=None, reply_markup=response_keyboard({"meal_draft": entry}))


async def workout_result(message, entry):
    volume = sum(s["weight_kg"]*s["reps"] for s in entry["sets"])
    await message.answer(f"Тренировка сохранена.\nПодходов: {len(entry['sets'])}\nОбъём: {fmt(volume)} кг\nДлительность: {fmt(entry['duration_minutes'])} мин", reply_markup=keyboard([("Тренировки", "workouts")]))


def next_exercise(entry, weekday):
    for exercise in entry.get("program_exercises", []):
        if exercise["weekday"] == weekday and sum(s["exercise"] == exercise["name"] for s in entry["sets"]) < exercise["sets"]:
            return exercise["name"]
    return None


@router.message(Input.value)
async def receive(message: Message, state):
    uid = message.from_user.id
    if not mentor_enabled(uid):
        await state.clear()
        return
    values = await state.get_data()
    kind = values.get("form_kind")
    text = (message.text or "").strip()
    try:
        if kind == "target":
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
            await make_draft(message, uid, state, "measurement", {key: number(value) for key, value in zip(("waist_cm", "chest_cm", "hips_cm"), parts) if value != "-"})
        elif kind == "progress_photo":
            if not message.photo:
                raise ValueError()
            await make_draft(message, uid, state, "measurement", {"photo_file_id": message.photo[-1].file_id, "note": message.caption or ""})
        elif kind == "wellbeing_energy":
            energy = None if text == "-" else int(text)
            if energy is not None and not 1 <= energy <= 5: raise ValueError()
            await form(message, state, "wellbeing_note", "Добавьте заметку о самочувствии или отправьте - без заметки. При выраженном ухудшении обратитесь за медицинской помощью; бот не меняет дозировки.", energy_score=energy)
        elif kind == "wellbeing_note":
            await make_draft(message, uid, state, "wellbeing", {"score": values["score"], "energy_score": values.get("energy_score"), "note": "" if text == "-" else text})
        elif kind == "meal_search":
            if not text: raise ValueError()
            await state.set_state(None)
            await library(message, uid, state, query=text)
        elif kind == "custom_goal":
            if not text: raise ValueError()
            data = await api("/dashboard", {"telegram_user_id": uid})
            await api("/profile/update", {"telegram_user_id": uid, "expected_version": data["version"], "request_key": request_key(message), "source_text": text, "evidence": text, "patch": {"goal": "custom", "goal_detail": text}})
            await state.clear()
            await message.answer("Ваша цель сохранена.", reply_markup=keyboard([("Профиль", "profile")]))
        elif kind == "course":
            if not text or len(text) > 120: raise ValueError()
            await form(message, state, "course_dose", "Укажите дозировку дословно из существующей схемы. Не указывайте новую дозу, подобранную ботом.", course_data={"name": text})
        elif kind == "course_dose":
            if not text or len(text) > 240: raise ValueError()
            await state.update_data(course_data={**values["course_data"], "dose_text": text})
            await state.set_state(None)
            await message.answer("Источник существующей схемы", reply_markup=keyboard([("Назначил специалист", "course_source:specialist"), ("Моя существующая схема", "course_source:user")]))
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
            await form(message, state, "course_supply", "Запас: количество | расход на один приём | единица (одна и та же). Например: 20 | 1 | таблетка. Если неизвестно, отправьте -. Бот не переводит единицы и не рассчитывает дозу.", course_data=course)
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
            result = await api("/workspace/workout/set", {"telegram_user_id": uid, "entry_id": values["workout_id"], "request_key": request_key(message), "exercise_set": {"exercise": name, "weight_kg": number(weight), "reps": int(reps)}})
            next_name = next_exercise(result["entry"], values.get("program_day", 0))
            await state.update_data(planned_exercise=next_name)
            prompt = f"Следующее: {next_name}. Вес | повторы" if next_name else "Следующий подход: упражнение | вес | повторы"
            await message.answer(f"Подход сохранён. Всего: {len(result['entry']['sets'])}.\n{prompt}", reply_markup=keyboard([("Завершить", f"workout_finish:{values['workout_id']}"), ("Длительность", f"workout_duration:{values['workout_id']}")]))
        elif kind == "workout_duration":
            result = await api("/workspace/workout/finish", {"telegram_user_id": uid, "entry_id": values["workout_id"], "duration_minutes": number(text)})
            await state.clear()
            await workout_result(message, result["entry"])
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
            await api("/reminder/options", {"telegram_user_id": uid, **settings})
            await state.clear()
            await reminders_view(message, uid)
        else:
            await state.clear()
            await message.answer("Откройте раздел заново.", reply_markup=keyboard())
    except (ValueError, KeyError, BridgeError) as error:
        detail = str(error) if isinstance(error, BridgeError) else "Проверьте формат и значения и попробуйте ещё раз."
        await message.answer(detail, parse_mode=None, reply_markup=keyboard([("Отмена", "menu")]))
