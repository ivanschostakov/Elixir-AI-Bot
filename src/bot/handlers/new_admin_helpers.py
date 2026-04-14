import uuid
from datetime import datetime

from aiogram.types import InputTextMessageContent, InlineQueryResultArticle

from config import UFA_TZ

_GET_USER_BOT_USAGE_ORDER = (("new", "new"), ("professor", "professor"), ("dose", "dose"))


def _format_get_user_ai_usage(token_usages: object) -> str:
    totals = getattr(token_usages, "totals", None) or {}
    by_bot_items = getattr(token_usages, "by_bot", None) or []
    by_bot = {str(item.get("bot")): item for item in by_bot_items if isinstance(item, dict)}

    lines = [f"🤖 <b>Запросов ИИ: {totals.get('total_requests', 0)} на сумму {totals.get('total_cost_usd', 0)}$</b>"]
    for bot_key, label in _GET_USER_BOT_USAGE_ORDER:
        bot_totals = by_bot.get(bot_key) or {}
        lines.append(
            f"• {label}: <i>{bot_totals.get('total_requests', 0)} запросов на сумму {bot_totals.get('total_cost_usd', 0)}$</i>"
        )

    lines.append(f"💲 Стоимость запроса в среднем: <i>{totals.get('avg_cost_per_request', 0)}</i>")
    return "\n".join(lines)


def _display_user_name(user: object) -> str:
    full_name = str(getattr(user, "full_name", "") or "").strip()
    if full_name: return full_name
    tg_phone = str(getattr(user, "tg_phone", "") or "").strip()
    if tg_phone: return tg_phone
    tg_id = getattr(user, "tg_id", None)
    if tg_id is not None: return f"Пользователь {tg_id}"
    return "Пользователь без имени"


def _format_dt_local(value: object) -> str:
    if not isinstance(value, datetime):
        return "не указан"
    dt = value.replace(tzinfo=UFA_TZ) if value.tzinfo is None else value.astimezone(UFA_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


def _format_user_attribution(user: object) -> str:
    fields = (
        ("source", getattr(user, "utm_source", None)),
        ("medium", getattr(user, "utm_medium", None)),
        ("campaign", getattr(user, "utm_campaign", None)),
        ("content", getattr(user, "utm_content", None)),
        ("creative", getattr(user, "utm_creative", None)),
    )
    lines = [f"• {label}: <code>{str(value).strip()}</code>" for label, value in fields if str(value or "").strip()]
    raw_payload = str(getattr(user, "utm_payload_raw", "") or "").strip()
    if raw_payload:
        lines.append(f"• payload: <code>{raw_payload}</code>")
    return "\n".join(lines) if lines else "не указана"


def _display_user_contact(user: object) -> str:
    contact_info = str(getattr(user, "contact_info", "") or "").strip()
    if contact_info: return contact_info
    tg_id = getattr(user, "tg_id", "не указан")
    tg_phone = str(getattr(user, "tg_phone", "") or "").strip() or "не указан"
    contact_id = getattr(user, "contact_id", None)
    contact_suffix = f", amoCRM contact_id: {contact_id}" if contact_id else ""
    return f"ID ТГ: {tg_id}, Номер ТГ: {tg_phone}{contact_suffix}"


def _display_cart_owner(cart: object) -> str:
    user = getattr(cart, "user", None)
    if user:
        return _display_user_name(user)
    cart_phone = str(getattr(cart, "phone", "") or "").strip()
    if cart_phone:
        return cart_phone
    user_id = getattr(cart, "user_id", None)
    if user_id is not None:
        return f"TG {user_id}"
    return "неизвестный пользователь"


def _parse_cart_search_date(value: str) -> datetime | None:
    parts = [part.strip() for part in str(value).split(".") if part.strip()]
    if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
        return None

    day = int(parts[0])
    month = int(parts[1])
    if len(parts) == 2:
        year = datetime.now(tz=UFA_TZ).year
    else:
        year = int(parts[2])
        if len(parts[2]) == 2:
            year += 2000

    try:
        return datetime(year=year, month=month, day=day, tzinfo=UFA_TZ)
    except ValueError:
        return None


def _user_search_results(rows: list[object]) -> list[InlineQueryResultArticle]:
    return [
        InlineQueryResultArticle(
            thumbnail_url=row.photo_url,
            id=str(uuid.uuid4()),
            title=_display_user_name(row),
            description=_display_user_contact(row),
            input_message_content=InputTextMessageContent(message_text=f"/get_user {row.tg_id}", parse_mode=None),
        )
        for row in rows
    ]


def _cart_search_results(carts: list[object]) -> list[InlineQueryResultArticle]:
    return [
        InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=f"{cart.name} от {_display_cart_owner(cart)}",
            description=f"Статус: {cart.status}, Обновлено: {cart.updated_at.hour}:{cart.updated_at.minute}, {cart.updated_at.date()}",
            input_message_content=InputTextMessageContent(message_text=f"/get_cart {cart.id}"),
        )
        for cart in carts
    ]
