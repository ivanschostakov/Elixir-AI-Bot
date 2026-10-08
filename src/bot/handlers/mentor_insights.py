"""Short, factual coaching beside Telegram reports; no invented records or prescriptions."""


def daily_insight(data):
    if not data.get("meals"):
        return "Начнём с одного приёма пищи: добавьте то, что уже съели. Так следующий совет будет учитывать ваш день."
    remaining = data.get("workspace", {}).get("remaining") or {}
    if remaining.get("protein", 0) > 0:
        return "Для следующего приёма пищи можно подобрать вариант с источником белка через «Что поесть?». Учту уже записанные блюда и остаток вашей нормы."
    if not data.get("workspace", {}).get("target"):
        return "Еда уже записана. Следующий полезный шаг — сохранить вашу норму питания, чтобы видеть остаток, а не только сумму."
    return "Сверьте, всё ли съеденное внесено. Для следующего приёма пищи можно подобрать вариант с учётом ваших записей, не меняя норму автоматически."


def weekly_insight(data):
    report = data.get("workspace", {}).get("weekly", {})
    days = report.get("nutrition_days", 0)
    if days < 3:
        return f"За период питание записано за {days} дн. По этому нельзя судить о всём рационе. Начните с регулярной записи одного привычного приёма пищи."
    protein = report.get("protein_target_percent")
    if protein is not None and protein < 90:
        return "В записанные дни белка в среднем меньше вашей сохранённой нормы. Разберём один обычный день и подберём подходящее блюдо, не меняя норму автоматически."
    planned, done = report.get("planned_workouts", 0), report.get("workouts", 0)
    if planned and done < planned:
        return "Тренировок записано меньше, чем было в программе. Если расписание неудобно, скорректируем дни; отсутствие записи само по себе не означает пропуск."
    diff = report.get("weight_change_kg")
    goal = data.get("profile", {}).get("goal")
    if diff is not None and goal in {"weight_loss", "weight_gain"}:
        toward = diff < 0 if goal == "weight_loss" else diff > 0
        return ("Изменение внесённого веса направлено к вашей цели. Сравним следующие недельные средние, чтобы отличить тенденцию от отдельных колебаний." if toward else
            "Внесённый вес пока не движется в сторону цели. Один период не объясняет причину: сначала вместе проверим полноту питания, измерений и удобство плана.")
    return "Записи уже позволяют сравнивать периоды. Выберите один удобный следующий шаг: питание, тренировку или регулярное измерение веса."


def workout_insight(entry):
    sets = entry.get("sets", [])
    if not sets: return "Подходов не записано, поэтому оценивать нагрузку пока не по чему. В следующей тренировке начнём с одного выполненного подхода."
    names = list(dict.fromkeys(s["exercise"] for s in sets))
    return "Для следующей тренировки есть ориентир по упражнениям: "+", ".join(names[:3])+". Сравнивайте вес и повторы в одном и том же упражнении; увеличение общего объёма само по себе не доказывает рост силы."
