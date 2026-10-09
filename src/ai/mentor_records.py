"""Draft-only tools for facts reported in the ongoing mentor conversation."""


def number(low, high):
    return {"type": "number", "minimum": low, "maximum": high}


SET = {"type": "object", "properties": {
    "exercise": {"type": "string", "minLength": 1, "maxLength": 120},
    "weight_kg": number(0, 1000), "reps": {"type": "integer", "minimum": 1, "maximum": 1000}},
    "required": ["exercise", "weight_kg", "reps"], "additionalProperties": False}
DURATION = {"type": "number", "exclusiveMinimum": 0, "maximum": 1440,
    "description": "Actual duration explicitly reported by the user, in MINUTES. Never elapsed chat time."}

RECORDS = {
    "draft_mentor_workout": ("workout", {
        "sets": {"type": "array", "items": SET, "minItems": 1, "maxItems": 200},
        "duration_minutes": DURATION}, ["sets", "duration_minutes"],
        "Draft a completed strength workout from actual user-reported sets and duration. "
        "One item per performed set. Never turn a plan or arbitrary activity into performed sets, or invent reps/weight. "
        "Ask for missing duration, weight or repetitions. Bodyweight is 0 kg only when explicitly clear. "
        "For walking, cardio or other physical activities use draft_mentor_activity instead."),
    "draft_mentor_activity": ("activity_log", {
        "name": {"type": "string", "minLength": 1, "maxLength": 120}, "duration_minutes": DURATION},
        ["name", "duration_minutes"],
        "Draft a physical activity actually reported by the user, separately from strength workouts and programs. "
        "Use a neutral factual name and explicitly reported duration; do not invent weights, reps or calorie expenditure. "
        "Sexual activity, if the adult user explicitly wants to log it, is an activity, never a strength program."),
    "draft_mentor_measurement": ("measurement", {
        "waist_cm": number(0.01, 300), "chest_cm": number(0.01, 300), "hips_cm": number(0.01, 300),
        "note": {"type": "string", "maxLength": 1000}}, [],
        "Draft user-reported body measurements in centimeters. At least one measurement is required. "
        "Do not estimate measurements from photos or fill unspecified values."),
    "draft_mentor_wellbeing": ("wellbeing", {
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "energy_score": {"type": "integer", "minimum": 1, "maximum": 5},
        "note": {"type": "string", "maxLength": 2000}}, ["score"],
        "Draft the user's wellbeing note and their own rating from 1 to 5. "
        "Ask for the rating conversationally if missing; never assign it yourself from symptoms. "
        "Energy rating is optional. Do not diagnose or prescribe."),
}

RECORD_TOOLS = [{"type": "function", "name": name, "strict": False,
    "description": description + " Creates only a draft; the user must review and confirm it. For a correction supply replaces_id.",
    "parameters": {"type": "object", "properties": {
        "data": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        "replaces_id": {"type": "integer", "minimum": 1}},
        "required": ["data"], "additionalProperties": False}}
    for name, (_, properties, required, description) in RECORDS.items()]
RECORD_TOOLS.append({"type": "function", "name": "get_mentor_record", "strict": False,
    "description": "Read the current owned record by ID before editing or discussing its saved/draft/cancelled status. "
        "Current backend status overrides old conversation messages. Never read another user's record.",
    "parameters": {"type": "object", "properties": {"entry_id": {"type": "integer", "minimum": 1}},
        "required": ["entry_id"], "additionalProperties": False}})


async def execute_record(context, name, arguments, api):
    if name == "get_mentor_record":
        result = await api("/workspace/record", {"telegram_user_id": context["telegram_user_id"],
            "entry_id": arguments.get("entry_id")})
        from .telegram_mentor import public_memory
        return public_memory(result)
    if context.get("button_action"):
        return {"ok": False, "error": "user_facts_required",
            "message": "Кнопка задаёт задачу, но не подтверждает, что человек выполнил активность или сообщил измерение. Уточни фактические данные."}
    kind = RECORDS[name][0]
    data = arguments.get("data")
    if not isinstance(data, dict):
        raise ValueError("Record data must be an object")
    data = dict(data)
    if kind == "workout":
        data.pop("active_entry_id", None)
        active = (context.get("saved") or {}).get("workspace", {}).get("active_workout")
        if active:
            data["active_entry_id"] = active["id"]
    payload = {"telegram_user_id": context["telegram_user_id"], "kind": kind, "data": data,
        "request_key": kind+":"+context["request_key"],
        "expected_version": (context.get("saved") or {}).get("version")}
    if arguments.get("replaces_id") is not None:
        payload["replaces_id"] = arguments["replaces_id"]
    result = await api("/workspace/draft", payload)
    if result.get("entry", {}).get("status") == "draft":
        context.setdefault("record_drafts", []).append(result["entry"])
        if payload.get("replaces_id"):
            context.setdefault("replaced_review_ids", []).append(payload["replaces_id"])
    return result
