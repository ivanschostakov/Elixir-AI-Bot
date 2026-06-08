import logging
import os
import uuid
import httpx
import pandas as pd

from typing import Literal, get_args
from datetime import date, datetime, timedelta
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineQuery, InlineQueryResultArticle, InputTextMessageContent

from aiogram.utils.deep_linking import create_start_link

from config import UFA_TZ, ELIXIR_CHAT_ID
from src.bot.handlers.new_admin_helpers import (
    _cart_search_results,
    _display_user_name,
    _format_dt_local,
    _format_get_user_ai_usage,
    _format_user_attribution,
    _parse_cart_search_date,
    _user_search_results,
)
from src.bot.texts import admin_texts
from .admin import professor_admin_router
from src.bot.keyboards import admin_keyboards
from src.bot.states import admin_states
from src.ai.helpers import make_excel_safe
from src.ai.webapp_client import webapp_client
from src.tg_methods import get_user_id_by_phone, normalize_phone, get_user_id_by_username

admin_logger = logging.getLogger("aiogram.admin")
_UTM_FUNNEL_MONEY_COLUMNS = {
    "Выручка товаров, ₽",
    "Выручка доставки, ₽",
    "Общая выручка, ₽",
    "Стоимость ИИ, $",
}


def _parse_date_range_input(text: str) -> tuple[date, date] | None:
    parts = [part.strip() for part in str(text or "").split() if part.strip()]
    if len(parts) != 2:
        return None
    try:
        start_date = datetime.strptime(parts[0], "%d.%m.%Y").date()
        end_date = datetime.strptime(parts[1], "%d.%m.%Y").date()
    except ValueError:
        return None
    if end_date < start_date:
        return None
    return start_date, end_date


def _period_range_from_preset(preset: str) -> tuple[date, date]:
    today = datetime.now(tz=UFA_TZ).date()
    if preset == "0":
        return date(1970, 1, 1), today
    try:
        days = max(1, int(preset))
    except ValueError:
        days = 1
    return today - timedelta(days=days - 1), today


def _write_excel_workbook(
    path: str,
    sheets: list[tuple[str, pd.DataFrame]],
    *,
    money_cols: set[str] | None = None,
    percent_cols: set[str] | None = None,
) -> None:
    money_cols = money_cols or set()
    percent_cols = percent_cols or set()
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, df in sheets:
            make_excel_safe(df).to_excel(writer, index=False, sheet_name=sheet_name)

        wb = writer.book
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            ws.freeze_panes = "A2"
            if ws.max_row >= 1:
                for cell in ws[1]:
                    cell.font = cell.font.copy(bold=True)

            for col in ws.columns:
                max_len = 0
                col_letter = col[0].column_letter
                for cell in col:
                    value = cell.value
                    if value is None:
                        continue
                    max_len = max(max_len, len(str(value)))
                ws.column_dimensions[col_letter].width = min(max(10, max_len + 2), 55)

            header_map = {ws.cell(row=1, column=j).value: j for j in range(1, ws.max_column + 1)}
            for name in money_cols:
                col_idx = header_map.get(name)
                if not col_idx:
                    continue
                for row_idx in range(2, ws.max_row + 1):
                    ws.cell(row=row_idx, column=col_idx).number_format = "#,##0.00"

            for name in percent_cols:
                col_idx = header_map.get(name)
                if not col_idx:
                    continue
                for row_idx in range(2, ws.max_row + 1):
                    ws.cell(row=row_idx, column=col_idx).number_format = "0.00"


def _build_utm_funnel_dataframe(report: object) -> pd.DataFrame:
    rows = [
        {
            "UTM source": row.utm_source,
            "UTM medium": row.utm_medium,
            "UTM campaign": row.utm_campaign,
            "UTM content": row.utm_content,
            "UTM creative": row.utm_creative,
            "Регистрации": int(row.registrations or 0),
            "Верифицированы": int(row.verified_users or 0),
            "Платящих пользователей": int(row.paid_users or 0),
            "Оплаченных заказов": int(row.paid_orders or 0),
            "Выручка товаров, ₽": float(row.goods_revenue or 0),
            "Выручка доставки, ₽": float(row.delivery_revenue or 0),
            "Общая выручка, ₽": float(row.total_revenue or 0),
            "Запросов ИИ": int(row.ai_total_requests or 0),
            "Input tokens": int(row.input_tokens or 0),
            "Cached input tokens": int(row.cached_input_tokens or 0),
            "Output tokens": int(row.output_tokens or 0),
            "Стоимость ИИ, $": float(row.ai_total_cost_usd or 0),
        }
        for row in getattr(report, "rows", [])
    ]
    columns = [
        "UTM source",
        "UTM medium",
        "UTM campaign",
        "UTM content",
        "UTM creative",
        "Регистрации",
        "Верифицированы",
        "Платящих пользователей",
        "Оплаченных заказов",
        "Выручка товаров, ₽",
        "Выручка доставки, ₽",
        "Общая выручка, ₽",
        "Запросов ИИ",
        "Input tokens",
        "Cached input tokens",
        "Output tokens",
        "Стоимость ИИ, $",
    ]
    df = pd.DataFrame(rows, columns=columns)
    totals = {
        "UTM source": "ИТОГО",
        "UTM medium": None,
        "UTM campaign": None,
        "UTM content": None,
        "UTM creative": None,
        "Регистрации": int(df["Регистрации"].sum()) if not df.empty else 0,
        "Верифицированы": int(df["Верифицированы"].sum()) if not df.empty else 0,
        "Платящих пользователей": int(df["Платящих пользователей"].sum()) if not df.empty else 0,
        "Оплаченных заказов": int(df["Оплаченных заказов"].sum()) if not df.empty else 0,
        "Выручка товаров, ₽": round(float(df["Выручка товаров, ₽"].sum()) if not df.empty else 0, 2),
        "Выручка доставки, ₽": round(float(df["Выручка доставки, ₽"].sum()) if not df.empty else 0, 2),
        "Общая выручка, ₽": round(float(df["Общая выручка, ₽"].sum()) if not df.empty else 0, 2),
        "Запросов ИИ": int(df["Запросов ИИ"].sum()) if not df.empty else 0,
        "Input tokens": int(df["Input tokens"].sum()) if not df.empty else 0,
        "Cached input tokens": int(df["Cached input tokens"].sum()) if not df.empty else 0,
        "Output tokens": int(df["Output tokens"].sum()) if not df.empty else 0,
        "Стоимость ИИ, $": round(float(df["Стоимость ИИ, $"].sum()) if not df.empty else 0, 6),
    }
    return pd.concat([df, pd.DataFrame([totals], columns=columns)], ignore_index=True)


def _build_utm_users_dataframe(report: object) -> pd.DataFrame:
    rows = [
        {
            "Telegram ID": user.tg_id,
            "Номер Telegram": user.tg_phone,
            "Создан": _format_dt_local(user.created_at),
            "Обновлен": _format_dt_local(user.updated_at),
            "UTM source": user.utm_source,
            "UTM medium": user.utm_medium,
            "UTM campaign": user.utm_campaign,
            "UTM content": user.utm_content,
            "UTM creative": user.utm_creative,
            "Payload": user.utm_payload_raw,
            "Верифицирован": bool(user.verified),
            "Платящий пользователь": bool((user.paid_orders or 0) > 0),
            "Оплаченных заказов": int(user.paid_orders or 0),
            "Выручка товаров, ₽": float(user.goods_revenue or 0),
            "Выручка доставки, ₽": float(user.delivery_revenue or 0),
            "Общая выручка, ₽": float(user.total_revenue or 0),
            "Запросов ИИ": int(user.ai_total_requests or 0),
            "Input tokens": int(user.input_tokens or 0),
            "Cached input tokens": int(user.cached_input_tokens or 0),
            "Output tokens": int(user.output_tokens or 0),
            "Стоимость ИИ, $": float(user.ai_total_cost_usd or 0),
        }
        for user in getattr(report, "users", [])
    ]
    columns = [
        "Telegram ID",
        "Номер Telegram",
        "Создан",
        "Обновлен",
        "UTM source",
        "UTM medium",
        "UTM campaign",
        "UTM content",
        "UTM creative",
        "Payload",
        "Верифицирован",
        "Платящий пользователь",
        "Оплаченных заказов",
        "Выручка товаров, ₽",
        "Выручка доставки, ₽",
        "Общая выручка, ₽",
        "Запросов ИИ",
        "Input tokens",
        "Cached input tokens",
        "Output tokens",
        "Стоимость ИИ, $",
    ]
    return pd.DataFrame(rows, columns=columns)


async def _send_utm_statistics_report(message: Message, start_date: date, end_date: date) -> None:
    report = await webapp_client.get_utm_funnel_report(start_date, end_date)
    funnel_df = _build_utm_funnel_dataframe(report)
    users_df = _build_utm_users_dataframe(report)
    ts = datetime.now(tz=UFA_TZ).strftime("%Y-%m-%d_%H-%M-%S")
    path = f"/tmp/utm_statistics_{ts}.xlsx"

    _write_excel_workbook(
        path,
        [("UTM funnel", funnel_df), ("Users", users_df)],
        money_cols=_UTM_FUNNEL_MONEY_COLUMNS,
    )
    await message.answer_document(
        FSInputFile(path),
        caption=f"📈 UTM статистика\nПериод: <b>{report.period_label}</b>",
        parse_mode="HTML",
    )
    try:
        os.remove(path)
    except Exception:
        pass


@professor_admin_router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(admin_texts.greeting, reply_markup=admin_keyboards.admin_menu)
    await message.delete()

@professor_admin_router.message(Command('deeplink'))
async def handle_link(message: Message, state: FSMContext):
    await message.answer(await create_start_link(message.bot, message.text.removeprefix('/deeplink '), encode=True))


@professor_admin_router.message(Command('edit_and_pin'), lambda message: message.reply_to_message)
async def handle_pin(message: Message):
    forwarded_message = message.reply_to_message
    c_id = forwarded_message.forward_from_chat.id
    m_id = forwarded_message.forward_from_message_id
    await message.bot.edit_message_reply_markup(message_id=m_id,  chat_id=c_id, reply_markup=admin_keyboards.open_test)

@professor_admin_router.message(Command('set_premium'))
async def add_premium(message: Message):
    phone = message.text.removeprefix("/set_premium ").strip()
    if phone:
        phone = normalize_phone(phone)
        user = await webapp_client.get_user("tg_phone", phone)
        user_id = await get_user_id_by_phone(phone) if not (user and user.tg_id) else user.tg_id
        if not user_id: return await message.answer('Пользователь не найден по номеру в ТГ')

    else: return await message.answer('Ошибка команды: <code>/set_premium номер_в_тг</code>')
    user = await webapp_client.update_user(int(user_id), {"premium_until": datetime.now(tz=UFA_TZ) + timedelta(weeks=1044)})
    if user: return await message.answer(f'Пользователю с номером {user.tg_phone} выдан премиум доступ')
    else:
        user = await webapp_client.upsert_user({"tg_phone": phone, "tg_id": user_id, "premium_until": datetime.now(tz=UFA_TZ) + timedelta(weeks=1044)})
        if user: await message.answer(f'Пользователю с номером {user.tg_phone} выдан премиум доступ')
        else: await message.answer("Ошибка команды: пользователь не пользовался ботом или не был найден в базе")
        return None

@professor_admin_router.message(Command("statistics"))
async def handle_statistics(message: Message):
    promos = await webapp_client.list_promos()
    carts = await webapp_client.get_carts()

    promos_rows = [{
        "ID": getattr(p, "id", None),
        "Промокод": getattr(p, "code", None),
        "Скидка, %": float(getattr(p, "discount_pct", 0) or 0),
        "Владелец": getattr(p, "owner_name", None),
        "Процент владельца, %": float(getattr(p, "owner_pct", 0) or 0),
        "Начислено владельцу, ₽": float(getattr(p, "owner_amount_gained", 0) or 0),
        "Уровень 1 (имя)": getattr(p, "lvl1_name", None),
        "Уровень 1 (процент), %": float(getattr(p, "lvl1_pct", 0) or 0),
        "Уровень 1 (начислено), ₽": float(getattr(p, "lvl1_amount_gained", 0) or 0),
        "Уровень 2 (имя)": getattr(p, "lvl2_name", None),
        "Уровень 2 (процент), %": float(getattr(p, "lvl2_pct", 0) or 0),
        "Уровень 2 (начислено), ₽": float(getattr(p, "lvl2_amount_gained", 0) or 0),
        "Использований": int(getattr(p, "times_used", 0) or 0),
        "Создано": getattr(p, "created_at", None),
        "Обновлено": getattr(p, "updated_at", None),
    } for p in promos]

    carts_rows = [{
        "Заказ ID": getattr(c, "id", None),
        "Пользователь ID": getattr(c, "user_id", None),
        "Название": getattr(c, "name", None),
        "Сумма товаров, ₽": float(getattr(c, "sum", 0) or 0),
        "Доставка, ₽": float(getattr(c, "delivery_sum", 0) or 0),
        "Доставка (текст)": getattr(c, "delivery_string", None),
        "Комментарий": getattr(c, "commentary", None),
        "Промокод": getattr(c, "promo_code", None),
        "Статус": getattr(c, "status", None),
        "Оплачен": False if not bool(getattr(c, "is_paid", False)) else True,
        "Создано": getattr(c, "created_at", None),
        "Обновлено": getattr(c, "updated_at", None),
    } for c in carts]

    promos_df = pd.DataFrame(promos_rows)
    carts_df = pd.DataFrame(carts_rows)
    if not carts_df.empty and "Промокод" in carts_df.columns: applied = carts_df[carts_df["Промокод"].notna() & (carts_df["Промокод"].astype(str).str.strip() != "")].copy()
    else: applied = pd.DataFrame(columns=carts_df.columns if not carts_df.empty else ["Промокод"])

    if applied.empty: summary_df = pd.DataFrame(columns=["Промокод", "Заказов", "Неоплаченных заказов", "Сумма товаров итого, ₽", "Средняя сумма, ₽", "Доставка итого, ₽"])
    else:
        applied["Сумма товаров, ₽"] = pd.to_numeric(applied["Сумма товаров, ₽"], errors="coerce").fillna(0.0)
        applied["Доставка, ₽"] = pd.to_numeric(applied["Доставка, ₽"], errors="coerce").fillna(0.0)
        g = applied.groupby("Промокод", as_index=False)
        summary_df = g.agg(
            **{
                "Заказов": ("Заказ ID", "count"),
                "Оплаченных заказов": ("Оплачен", "sum"),
                "Сумма товаров итого, ₽": ("Сумма товаров, ₽", "sum"),
                "Средняя сумма, ₽": ("Сумма товаров, ₽", "mean"),
                "Доставка итого, ₽": ("Доставка, ₽", "sum"),
            }
        )
        summary_df["Сумма товаров итого, ₽"] = summary_df["Сумма товаров итого, ₽"].round(2)
        summary_df["Средняя сумма, ₽"] = summary_df["Средняя сумма, ₽"].round(2)
        summary_df["Доставка итого, ₽"] = summary_df["Доставка итого, ₽"].round(2)
        summary_df = summary_df.sort_values(by=["Заказов", "Промокод"], ascending=[False, True])

    promos_df = make_excel_safe(promos_df)
    carts_df = make_excel_safe(carts_df)
    summary_df = make_excel_safe(summary_df)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = f"/tmp/statistics_{ts}.xlsx"

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, index=False, sheet_name="Сводка по промокодам")
        promos_df.to_excel(writer, index=False, sheet_name="Промокоды")
        carts_df.to_excel(writer, index=False, sheet_name="Заказы")

        wb = writer.book
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            ws.freeze_panes = "A2"
            if ws.max_row >= 1:
                for cell in ws[1]: cell.font = cell.font.copy(bold=True)

            for col in ws.columns:
                max_len = 0
                col_letter = col[0].column_letter
                for cell in col:
                    v = cell.value
                    if v is None: continue
                    s = str(v)
                    if len(s) > max_len: max_len = len(s)
                ws.column_dimensions[col_letter].width = min(max(10, max_len + 2), 55)

            money_cols = {
                "Сумма товаров, ₽", "Доставка, ₽", "Начислено владельцу, ₽",
                "Уровень 1 (начислено), ₽", "Уровень 2 (начислено), ₽",
                "Сумма товаров итого, ₽", "Средняя сумма, ₽", "Доставка итого, ₽",
            }

            header_map = {}
            for j in range(1, ws.max_column + 1): header_map[ws.cell(row=1, column=j).value] = j
            for name in money_cols:
                j = header_map.get(name)
                if not j: continue
                for i in range(2, ws.max_row + 1): ws.cell(row=i, column=j).number_format = "#,##0.00"

            pct_cols = {
                "Скидка, %", "Процент владельца, %", "Уровень 1 (процент), %", "Уровень 2 (процент), %",
            }
            for name in pct_cols:
                j = header_map.get(name)
                if not j: continue
                for i in range(2, ws.max_row + 1): ws.cell(row=i, column=j).number_format = "0.00"

    await message.answer_document(FSInputFile(path), caption=f"📊 Статистика (Excel)\nСформировано: {ts.replace('_', ' ')}")
    try: os.remove(path)
    except Exception: pass


@professor_admin_router.message(Command("utm_statistics"))
async def handle_utm_statistics(message: Message, state: FSMContext):
    await state.set_state(admin_states.MainMenu.utm_statistics_time)
    await message.answer(admin_texts.utm_statistics_period, reply_markup=admin_keyboards.utm_statistics_times)


@professor_admin_router.message(admin_states.MainMenu.utm_statistics_time)
async def handle_utm_statistics_time(message: Message, state: FSMContext):
    period = _parse_date_range_input(message.text or "")
    if not period:
        return await message.answer(
            "Неверный формат диапазона.\n"
            "Отправьте период в формате <code>22.09.2025 12.10.2025</code>.",
            parse_mode="HTML",
            reply_markup=admin_keyboards.utm_statistics_times,
        )

    start_date, end_date = period
    await _send_utm_statistics_report(message, start_date, end_date)
    await state.clear()
    await message.answer(admin_texts.greeting, reply_markup=admin_keyboards.admin_menu)

@professor_admin_router.message(Command('get_user'))
async def handle_get_user(message: Message):
    raw_value = message.text.removeprefix("/get_user ").strip()
    if not raw_value: await message.answer("Ошибка команды: <code>/get_user телефон_или_айди_тг</code>", reply_markup=admin_keyboards.back)
    else:
        phone = normalize_phone(raw_value)
        user = None
        if phone:
            phone_candidates = [phone, phone.removeprefix("+")]
            for candidate in dict.fromkeys(phone_candidates):
                user = await webapp_client.get_user("tg_phone", candidate)
                if user: break
        if not user and raw_value.isdigit():
            user = await webapp_client.get_user("tg_id", int(raw_value))
        if not user: return await message.answer(f"Пользователь с телефоном или айди {raw_value} не найден", reply_markup=admin_keyboards.back)
        token_usages = await webapp_client.get_user_usage_totals(user.tg_id)
        user_carts = [cart for cart in await webapp_client.get_user_carts(user.tg_id)]

        paid: list[object] = []
        unpaid: list[object] = []
        for cart in user_carts: paid.append(cart) if cart.is_paid else unpaid.append(cart)
        totals = token_usages.totals
        total_rub = sum([(cart.sum or 0) for cart in user_carts])
        paid_rub = sum([(cart.sum or 0) for cart in paid])
        unpaid_rub = sum([(cart.sum or 0) for cart in unpaid])
        is_member = False
        try:
            member = await message.bot.get_chat_member(ELIXIR_CHAT_ID, user.tg_id)
            is_member = getattr(member, "status", None) not in {"left", "kicked"}
        except Exception as exc:
            admin_logger.warning("Failed to get chat membership for tg_id=%s: %s", user.tg_id, exc)
        full_name = user.full_name or "не указан"
        tg_phone = user.tg_phone or "не указан"
        last_phone = (str(user.phone).strip() if user.phone else "")
        last_email = (str(user.email).strip() if user.email else "")
        if last_phone and last_email: last_contact_info = f"Телефон: {last_phone}, Email: {last_email}"
        elif last_phone: last_contact_info = f"Телефон: {last_phone}"
        elif last_email: last_contact_info = f"Email: {last_email}"
        else: last_contact_info = "не указан"
        user_attribution = _format_user_attribution(user)
        user_text = (f"👤 <b>{full_name}</b>\n"
                     f"📞 Номер ТГ: <i>{tg_phone}</i>\n"
                     f"🆔 Айди ТГ: <i>{user.tg_id}</i>\n"
                     f"📲 Последняя контактная информация в заказах:\n"
                     f"{last_contact_info}\n"
                     f"📣 Состоит в чате: <i>{'❌ Нет' if not is_member else '✅ Да'}</i>\n"
                     f"🗓️ Зарегистрирован: <i>{_format_dt_local(user.created_at)}</i>\n"
                     f"🛠️ Обновлен: <i>{_format_dt_local(user.updated_at)}</i>\n"
                     f"🏷️ UTM атрибуция:\n{user_attribution}\n\n"
                     f"🛍️ <b>Заказов: {len(user_carts)} на сумму {total_rub}₽\n</b>"
                     f" — Оплаченных: <i>{len(paid)} на сумму {paid_rub}₽</i>\n"
                     f" — Неоплаченных: <i>{len(unpaid)} на сумму {unpaid_rub}₽</i>\n\n"
                     f"{_format_get_user_ai_usage(token_usages)}")

        if user.blocked_until and user.blocked_until > datetime.now(UFA_TZ): user_text += f"\n\n‼️ <b>ЗАБЛОКИРОВАН ДО {user.blocked_until.date()} {user.blocked_until.hour}:{user.blocked_until.minute} по МСК ‼️</b>"
        await message.answer(user_text, reply_markup=admin_keyboards.view_user_menu(user.tg_id, len(user_carts), bool(user.blocked_until and user.blocked_until > datetime.now(UFA_TZ))))

@professor_admin_router.message(admin_states.ViewUser.block_days, lambda message: message.text.isdigit())
async def handle_block_days(message: Message, state: FSMContext):
    state_data = await state.get_data()
    user_id = state_data["user_id"]
    days = int(message.text.strip())
    if days == 0: until = datetime.max.replace(tzinfo=UFA_TZ)
    else: until = datetime.now() + timedelta(days=abs(int(days)))
    user = await webapp_client.update_user(user_id, {"blocked_until": until})
    await message.answer(
        f"Пользователь {_display_user_name(user)} {user.tg_phone or ''} <b>успешно заблокирован до {until.date()} {until.hour}:{until.minute} по МСК</b>",
        reply_markup=admin_keyboards.back_to_user(user.tg_id),
    )

@professor_admin_router.message(Command("get_cart"))
async def handle_get_cart(message: Message, state: FSMContext):
    cart_id = message.text.removeprefix("/get_cart").strip()
    if not cart_id.isdigit(): await message.answer("Ошибка команды: <code>/get_cart номер_заказа</code>", reply_markup=admin_keyboards.back)
    else:
        cart = await webapp_client.get_cart_by_id(int(cart_id))
        if cart: await message.answer(await webapp_client.cart_analysis_text(int(cart_id)), reply_markup=admin_keyboards.back_to_user(cart.user_id))
        else:
            await message.answer(f"Заказ по номеру {cart_id} не существует")
            await handle_start(message, state)

@professor_admin_router.callback_query()
async def handle_new_admin_callback(call: CallbackQuery, state: FSMContext):
    data = call.data.removeprefix("admin:").split(':')
    state_data = await state.get_data()
    if data[0] == "users":
        if data[1] == "search": await call.message.edit_text(admin_texts.search_users_choice, reply_markup=admin_keyboards.search_users_choice)
        elif data[1].isdigit():
            user_id = int(data[1])
            user = await webapp_client.get_user("tg_id", user_id)
            if data[2] == "carts":
                analysis_text = await webapp_client.user_carts_analytics_text(user_id)
                await call.message.edit_text(f"{call.message.html_text.splitlines()[0]}\n{analysis_text}")

            elif data[2] == "block":
                await call.message.edit_text(admin_texts.block_days, reply_markup=admin_keyboards.back)
                await state.set_state(admin_states.ViewUser.block_days)
                await state.update_data(user_id=user.tg_id)

            elif data[2] == "unblock":
                user = await webapp_client.update_user(user.tg_id, {"blocked_until": None})
                await call.message.edit_text(
                    f"Пользователь {_display_user_name(user)} {user.tg_phone or ''} успешно <b>разблокирован 🔓</b>",
                    reply_markup=admin_keyboards.back_to_user(user.tg_id),
                )

    elif data[0] in {"spends", "send"}:
        from .admin import handle_admin_callback
        await handle_admin_callback(call, state)
    elif data[0] == "utm_stats":
        try: await call.answer()
        except Exception: pass
        if len(data) == 1:
            await state.set_state(admin_states.MainMenu.utm_statistics_time)
            return await call.message.edit_text(
                admin_texts.utm_statistics_period,
                parse_mode="HTML",
                reply_markup=admin_keyboards.utm_statistics_times,
            )

        start_date, end_date = _period_range_from_preset(data[1])
        await _send_utm_statistics_report(call.message, start_date, end_date)
        await state.clear()
        await call.message.answer(admin_texts.greeting, reply_markup=admin_keyboards.admin_menu)
        try: await call.message.delete()
        except Exception: pass

    elif data[0] == "main_menu": await handle_start(call.message, state)
    elif data[0] == "main_menuu":
        await call.message.answer(admin_texts.greeting, reply_markup=admin_keyboards.admin_menu)
        await state.clear()


@professor_admin_router.inline_query()
async def handle_inline_query(inline_query: InlineQuery, state: FSMContext):
    data = inline_query.query.strip().split(maxsplit=2)
    start_input_content = InputTextMessageContent(message_text="/start", parse_mode=None)
    if not data:
        return await inline_query.answer([])
    if data[0] == "search_user" and len(data) >= 2:
        column_name = data[1]
        value = data[2] if len(data) == 3 else ""
        allowed_column_names = Literal["full_name", "username", "email", "tg_id", "phone"]
        if column_name not in get_args(allowed_column_names): results = [InlineQueryResultArticle(id=str(uuid.uuid4()), title=f"❌ Неверный поисковой параметр: {column_name}", input_message_content=start_input_content, description=f"Позволено: {', '.join(get_args(allowed_column_names))}", )]
        elif not value.strip(): results = [InlineQueryResultArticle(id=str(uuid.uuid4()), title=f"Введите поисковый запрос", input_message_content=start_input_content, description=f"Не трогайте ничего после двоеточия", )]
        elif column_name == "username":
            value = await get_user_id_by_username(value.removeprefix("@"))
            if value:
                column_name = "tg_id"
                rows, total = await webapp_client.search_users(column_name, value, limit=50)
                if rows: results = _user_search_results(rows)
                else: results = [InlineQueryResultArticle(id=str(uuid.uuid4()), title="В баночке не найдено пользователей по поисковому запросу 🫙", description="Попробуйте другой запрос", input_message_content=start_input_content)]

            else: results = [InlineQueryResultArticle(id=str(uuid.uuid4()), title="Пользователя с таким username не существует", input_message_content=start_input_content)]

        else:
            rows, total = await webapp_client.search_users(column_name, value, limit=50)
            if rows: results = _user_search_results(rows)
            else: results = [InlineQueryResultArticle(id=str(uuid.uuid4()), title="В баночке не найдено пользователей по поисковому запросу 🫙", description="Попробуйте другой запрос", input_message_content=start_input_content)]

    elif data[0] == "search_cart":
        value = data[1] if len(data) >= 2 else ""
        if not value.strip():
            results = [InlineQueryResultArticle(id=str(uuid.uuid4()), title="Введите номер заказа или дату", description="Поиск заказов возможен по номеру, дате дд.мм или дате дд.мм.гггг", input_message_content=start_input_content)]
        elif not value.isdigit():
            dt = _parse_cart_search_date(value)
            if not dt: results = [InlineQueryResultArticle(id=str(uuid.uuid4()), title="Введенный запрос не число и не дата", description="Поиск заказов возможен только по их номерам или дате (дд.мм или дд.мм.гггг)", input_message_content=start_input_content)]
            else:
                carts = await webapp_client.get_carts_by_date(dt)
                if carts: results = _cart_search_results(carts)
                else: results = [InlineQueryResultArticle(id=str(uuid.uuid4()), title="В баночке не найдено заказов по поисковому запросу 🫙", description="Попробуйте другой запрос", input_message_content=start_input_content)]
        else:
            cart_id = int(value)
            carts, total = await webapp_client.search_carts(cart_id, limit=50)
            if carts: results = _cart_search_results(carts)
            else: results = [InlineQueryResultArticle(id=str(uuid.uuid4()), title="В баночке не найдено заказов по поисковому запросу 🫙", description="Попробуйте другой запрос", input_message_content=start_input_content)]

    else: results = []
    await inline_query.answer(results)
