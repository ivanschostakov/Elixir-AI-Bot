"""Buttons select a task in the existing conversation, not a separate input form."""
from src.ai.mentor_copy import QUESTIONS


TASKS = {
    "program_ai": "Помоги составить недельную программу тренировок. Прочитай мой профиль и текущий план. Уточни только неизвестные опыт, оборудование и ограничения. Предложение программы нужно оформить черновиком для подтверждения.",
    "program_text": "Хочу добавить или изменить свою программу тренировок. Посмотри текущий план. Попроси рассказать, как я тренируюсь и что хочу изменить; я могу описать несколько дней и упражнений сразу. Не создавай программу из предположений до моего ответа.",
    "workout_start": "Покажи тренировку на сегодня из моей сохранённой программы, если она есть. Я хочу обсудить или записать занятие: попроси рассказать, что я уже выполнил или собираюсь делать. Не считай план выполненным и не запускай таймер. Фактическую длительность и выполненные подходы я сообщу сам.",
    "workout_free": "Хочу записать занятие вне программы. Спроси, чем я занимался и как долго. Если это силовая тренировка, собери реальные упражнения, подходы, вес и повторения; если другая активность, достаточно её названия и времени. Ничего не придумывай и не записывай до моего ответа и подтверждения.",
    "reason:workout": "Помоги скорректировать мою тренировочную программу. Сначала прочитай мой текущий недельный план, последние фактические тренировки, цель и ограничения. Уточни, что не подходит, только если этого ещё нет в диалоге. Затем предложи конкретное изменение полного плана с сохранением остальных упражнений. Изменённая программа пока только черновик для моего подтверждения.",
    "measurement": "Хочу добавить замеры тела. Спроси, какие замеры я сделал, и поясни, что можно сообщить, например, талию или бёдра в сантиметрах. Не требуй всех замеров и не оценивай их сам. Запись только черновиком через draft_mentor_measurement.",
    "wellbeing": "Хочу отметить самочувствие. Спроси, как я себя чувствую сегодня. Для дневника нужна моя оценка от 1 до 5, где 1 — плохо, 5 — хорошо; можно добавить рассказ о состоянии. Не назначай оценку по описанию симптомов самостоятельно. Запись только черновиком через draft_mentor_wellbeing.",
}


async def dispatch_dialogue(query, state, action, message, professor_bot, professor_client, expert_client):
    from .mentor import ask, run_ai_action
    if action in TASKS:
        from .mentor_panel import clear_input
        await clear_input(state)
        await run_ai_action(query, state, professor_bot, professor_client, expert_client, TASKS[action])
        return True
    if action == "weight":
        from .mentor_flows import form
        await form(message, state, "weight", QUESTIONS["weight"])
        return True
    if action == "custom_goal":
        await ask(message, query.from_user.id, QUESTIONS[action],
            context="Продолжи общий диалог. Используй актуальный профиль и сохраняй только явно сообщённые факты через update_mentor_profile. Не начинай отдельную анкету.")
        return True
    if action.startswith("program_day:"):
        from .mentor_format import DAYS
        day = int(action.split(":")[1])
        if day not in range(7):
            from src.ai.telegram_mentor import BridgeError
            raise BridgeError("Выберите день недели.")
        await run_ai_action(query, state, professor_bot, professor_client, expert_client,
            "Хочу составить или изменить упражнения на "+DAYS[day]+". Прочитай текущую программу. "
            "Спроси об упражнениях на этот день; можно сообщить весь день сразу. Сохрани остальные дни при подготовке черновика. "
            "Недостающие подходы и повторения уточни в разговоре, не требуй формата.")
        return True
    if action.startswith("record_edit:"):
        _, kind, entry_id = action.split(":")
        if kind in {"program", "workout", "activity_log", "measurement", "wellbeing"}:
            from src.ai.telegram_mentor import api, BridgeError
            entry = (await api("/workspace/record", {"telegram_user_id": query.from_user.id, "entry_id": int(entry_id)}))["entry"]
            if entry.get("kind") != kind or entry.get("status") != "draft":
                raise BridgeError("Этот черновик уже обработан. Откройте актуальную запись.")
            await ask(message, query.from_user.id,
                "Что поправим в этой записи? Можно уточнить время, отдельную цифру или рассказать, что было иначе.",
                context=f"Исправляется черновик {kind} №{entry_id}. Сначала прочитай его через get_mentor_record. Сохрани неизменённое, создай новый черновик с replaces_id={entry_id}; нужно новое подтверждение.")
            return True
    return False
