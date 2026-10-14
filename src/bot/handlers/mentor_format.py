"""Human-readable diary values and escaped Telegram HTML."""
from datetime import date
from decimal import Decimal
from html import escape
import re

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
GOALS = {"weight_loss": "снижение веса", "weight_gain": "набор веса", "maintain": "поддержание веса", "custom": "своя цель"}
ACTIVITY = {"low": "Низкая — преимущественно сидячий день", "light": "Лёгкая — 1–3 тренировки в неделю",
    "moderate": "Умеренная — 3–4 тренировки в неделю", "high": "Высокая — 5–7 тренировок в неделю"}


def number_text(value, missing="нет данных"):
    if value is None:
        return missing
    return f"{Decimal(str(value)):,.2f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")


def profile_value(field, value):
    if field in {"age", "height_cm", "current_weight_kg", "target_weight_kg"}:
        return number_text(value, missing="не указан")
    labels = {"goal": GOALS, "activity": ACTIVITY, "sex": {"male": "мужской", "female": "женский"}}
    return labels.get(field, {}).get(value, value) if value is not None else "не указан"


def day(value):
    try:
        parsed = date.fromisoformat(str(value)[:10])
        return f"{parsed.day} {MONTHS[parsed.month-1]}"
    except (ValueError, TypeError):
        return str(value or "")


def period(start, end):
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
        if a.year == b.year and a.month == b.month:
            return day(end) if a == b else f"{a.day}–{b.day} {MONTHS[b.month-1]}"
        return f"{day(start)} — {day(end)}" + (f" {a.year}–{b.year}" if a.year != b.year else "")
    except (ValueError, TypeError):
        return ""


def timezone_label(value):
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    names = {"Europe/Moscow": "Москва", "Asia/Yekaterinburg": "Екатеринбург", "Asia/Almaty": "Алматы",
        "Asia/Yerevan": "Ереван", "Asia/Tbilisi": "Тбилиси", "Europe/Kaliningrad": "Калининград",
        "Asia/Novosibirsk": "Новосибирск", "Asia/Vladivostok": "Владивосток", "UTC": "UTC"}
    try:
        offset = datetime.now(ZoneInfo(value)).utcoffset().total_seconds() / 3600
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return value
    return f"{names.get(value, value.rsplit('/', 1)[-1].replace('_', ' '))} (UTC{offset:+g})"


def local_datetime(value, zone):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    parsed = datetime.fromisoformat(value).astimezone(ZoneInfo(zone))
    return f"{day(parsed.date().isoformat())}, {parsed:%H:%M}"


def count_text(value, forms):
    count = abs(int(value))
    word = forms[2] if 11 <= count % 100 <= 14 else forms[0] if count % 10 == 1 else forms[1] if 2 <= count % 10 <= 4 else forms[2]
    return f"{value} {word}"


def html_text(text):
    # Escape first: model HTML is never trusted; underscores in URLs stay literal.
    text = escape(text, quote=False)
    text = re.sub(r"(?m)^\s*#{1,6}\s+(.+?)\s*$", lambda m: "<b>"+m[1].strip("* ")+"</b>", text)
    return re.sub(r"(?<!\w)(\*\*|\*)([^*\n]+?)\1(?!\w)", lambda m: "<b>"+m[2]+"</b>", text)
