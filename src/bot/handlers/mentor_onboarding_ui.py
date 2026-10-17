"""Presentation for the guided profile; no model output controls navigation."""
from .mentor_format import ACTIVITY, GOALS, count_text, timezone_label


LABELS = {
    "goal": "Цель", "age": "Возраст", "sex": "Пол", "height_cm": "Рост",
    "current_weight_kg": "Вес", "activity": "Активность",
    "preferences": "Привычки и питание", "restrictions": "Ограничения",
    "timezone": "Местное время", "review": "Проверка профиля",
}
FIELDS = ("goal", "age", "sex", "height_cm", "current_weight_kg", "activity")
NUTRITION_FIELDS = (*FIELDS, "goal_detail", "preferences", "restrictions")
PROMPTS = {
    "goal": "Какого результата вы хотите достичь? Выберите цель или расскажите, что для вас сейчас важно.",
    "age": "Сколько вам полных лет? Возраст нужен для расчёта и проверки, подходит ли вам автоматическая норма питания.",
    "sex": "Для расчёта нормы питания уточню: вы мужчина или женщина? Можно выбрать кнопку ниже.",
    "height_cm": "Какой у вас рост? Он нужен для расчёта нормы питания. Например, 170 см.",
    "current_weight_kg": "Сколько вы сейчас весите? Это поможет рассчитать ориентир питания и отслеживать изменения. Можно написать примерно, например: «около 85 кг».",
    "activity": "Как проходит ваша обычная неделя? Учитывайте работу, прогулки и тренировки.\n\nМало движения: в основном сидите, почти не гуляете и не тренируетесь.\nЛёгкая активность: регулярно гуляете или занимаетесь 1–3 раза в неделю.\nСредняя: регулярно занимаетесь 3–5 раз в неделю и двигаетесь в течение дня.\nВысокая: физическая работа или интенсивные занятия почти ежедневно.\n\nВыберите ближайший вариант или расскажите о своём режиме. Ориентируйтесь на обычную неделю, не на самый активный день.",
    "preferences": "Расскажите немного о своём обычном дне и питании: что любите есть, как обычно питаетесь и есть ли продукты, которые вам не подходят. Это поможет учитывать ваши привычки. Этот шаг можно пропустить.",
    "restrictions": "Есть ли особенности здоровья или ограничения в питании, которые важно учитывать? Расскажите только то, чем готовы поделиться. Если ограничений нет, так и напишите.",
    "timezone": "В каком городе вы сейчас живёте? Это нужно, чтобы дневник и напоминания совпадали с вашим местным временем. Напоминания сами не включатся.",
    "eligibility": "Перед расчётом уточню важный момент. Автоматическая норма предназначена для взрослых без беременности, грудного вскармливания, расстройств пищевого поведения и необходимости лечебного питания.\n\nЭти ограничения к вам не относятся? Если сомневаетесь, расчёт пропустим: дневник останется доступен.",
}


def display(field, profile, zone=None):
    value = zone if field == "timezone" else profile.get(field)
    if value is None or value == "":
        return "не указано"
    if field == "timezone":
        return timezone_label(value)
    if field == "goal":
        return profile.get("goal_detail") if value == "custom" and profile.get("goal_detail") else GOALS.get(value, value)
    if field == "activity":
        return ACTIVITY.get(value, value)
    if field == "sex":
        return {"male": "мужчина", "female": "женщина"}.get(value, value)
    if field == "age":
        return count_text(value, ("год", "года", "лет"))
    if isinstance(value, (int, float)):
        value = f"{value:g}".replace(".", ",")
    suffix = {"height_cm": " см", "current_weight_kg": " кг", "age": " лет"}.get(field, "")
    return str(value) + suffix


def question(step, value, profile, *, clarification=None):
    route = value.get("route", [])
    progress = f"Шаг {route.index(step)+1} из {len(route)} · " if step in route and not value.get("editing") else ""
    title = progress + LABELS.get(step, "Знакомство")
    saved = display(step, profile, value.get("timezone") if value.get("timezone_done") else None)
    current = f"\nСейчас сохранено: {saved[:500]}" if saved != "не указано" else ""
    return title + current + "\n\n" + (clarification or PROMPTS[step])


def summary(data, value):
    profile = data.get("profile", {})
    lines = ["Проверим ваш профиль", "", "Проверьте ответы. Любой пункт можно изменить кнопкой ниже.", ""]
    route = value.get("route", [])
    if route and "review" in route and not value.get("editing"):
        lines[0] = f"Шаг {route.index('review')+1} из {len(route)} · Проверим ваш профиль"
    for field in (*FIELDS, "preferences", "restrictions", "timezone"):
        shown = display(field, profile, data.get("settings", {}).get("timezone") if value.get("timezone_done") else None)
        if field in {"preferences", "restrictions"} and shown == "не указано":
            continue
        lines.append(f"{LABELS[field]}: {shown[:450]}")
    if data.get("workspace", {}).get("target"):
        lines += ["", "Сохранённую норму питания не меняю. После изменения данных можно отдельно рассчитать новый ориентир и подтвердить его."]
    else:
        lines += ["", "После проверки предложу рассчитать ориентир питания. Норма сохранится только после вашего подтверждения."]
    return "\n".join(lines)
