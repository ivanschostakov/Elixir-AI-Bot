import base64
import asyncio
import httpx
import logging
import re

from datetime import datetime, timedelta
from urllib.parse import parse_qs
from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, Message, CallbackQuery, ReplyKeyboardRemove, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from config import OWNER_TG_IDS, UFA_TZ, DATA_DIR, WEBAPP_BASE_DOMAIN
from src.bot.handlers.new_user_helpers import _get_unverified_requests_count, _request_phone, _ensure_user, _should_spend_premium_request, _can_use_professor_mode, _resolve_mode_client, _resolve_last_used, LAST_USED_EXPERT, LAST_USED_PROFESSOR, UNVERIFIED_REQUEST_LIMIT
from src.calc import generate_drug_graphs, plot_filled_scale
from src.ai.helpers import CHAT_NOT_BANNED_FILTER, _notify_user, with_action, _fmt, check_blocked
from src.ai.webapp_client import WebappBotApiError, webapp_client
from src.tg_methods import normalize_phone
from src.bot.texts import user_texts
from src.bot.keyboards import user_keyboards
from src.bot.states import user_states
from .ai_helpers import (
    MediaGroupFilter,
    media_group_handler,
    has_supported_media,
    send_message_v2_from_media_group,
    send_message_v2_from_telegram,
    safe_ai_response,
    safe_webapp_call,
    schedule_webapp_call,
)

async def _(x: Message):
    await asyncio.sleep(15)
    await x.delete()

professor_user_router = Router(name="shop_professor")
graph_request_logger = logging.getLogger("aiogram.graph_lifecycle")
user_flow_logger = logging.getLogger("aiogram.shop_user")
graph_generation_lock = asyncio.Lock()

def _normalize_order_code_input(value: str | int | None) -> str:
    code = str(value or "").strip()
    code = re.sub(r"^\s*заказ\s*", "", code, flags=re.IGNORECASE)
    code = re.sub(r"^\s*[№#]\s*", "", code)
    return code.strip()

professor_user_router.message.filter(lambda message: message.from_user.id not in OWNER_TG_IDS and message.chat.type == ChatType.PRIVATE, check_blocked, CHAT_NOT_BANNED_FILTER)
professor_user_router.callback_query.filter(lambda call: call.data.startswith("user") and call.from_user.id not in OWNER_TG_IDS and call.message.chat.type == ChatType.PRIVATE, check_blocked, CHAT_NOT_BANNED_FILTER)

UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_creative")


def _clean_query_value(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _decode_start_payload(payload: str | None) -> str:
    raw = str(payload or "").strip()
    if not raw:
        return ""
    if "=" in raw or "&" in raw:
        return raw

    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        if decoded:
            return decoded
    except Exception:
        pass
    return raw


def _parse_start_payload(payload: str | None) -> tuple[str, dict[str, str | None]]:
    decoded_payload = _decode_start_payload(payload)
    parsed = parse_qs(decoded_payload, keep_blank_values=False)
    return decoded_payload, {key: _clean_query_value(values[0] if values else None) for key, values in parsed.items()}


def _build_utm_payload(params: dict[str, str | None], raw_payload: str) -> dict[str, str | None]:
    payload = {field: params.get(field) for field in UTM_FIELDS}
    payload["utm_payload_raw"] = _clean_query_value(raw_payload)
    return payload


def _has_utm_markers(payload: dict[str, str | None]) -> bool:
    return any(payload.get(field) for field in UTM_FIELDS)


async def _capture_first_touch_utm(message: Message, params: dict[str, str | None], raw_payload: str):
    utm_payload = _build_utm_payload(params, raw_payload)
    if not _has_utm_markers(utm_payload):
        return None

    existing_user = await webapp_client.get_user("tg_id", message.from_user.id)
    if existing_user:
        return existing_user

    return await webapp_client.upsert_user(
        {
            "tg_id": message.from_user.id,
            "name": message.from_user.first_name,
            "surname": message.from_user.last_name,
            **utm_payload,
        }
    )


async def _send_product_deep_link_preview(message: Message, product_id: str) -> None:
    product = await webapp_client.get_product_with_features(product_id)
    async with httpx.AsyncClient(timeout=20.0) as client:
        result = await client.get(f"{WEBAPP_BASE_DOMAIN}/static/images/{product_id}.png")
        result.raise_for_status()
        image_bytes = result.content

    url = f"{WEBAPP_BASE_DOMAIN}/#/product/{product_id}"
    await message.answer_photo(
        photo=BufferedInputFile(file=image_bytes, filename=f"{product_id}.png"),
        caption=f"<b>{product.name}</b>\nАртикул: {product.code}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Подробнее", web_app=WebAppInfo(url=url))]]
        ),
    )


@professor_user_router.message(Command('shop'))
async def app(message: Message):
    await message.answer(user_texts.offer, reply_markup=user_keyboards.open_app)

@professor_user_router.message(Command('about'))
async def about(message: Message):
    x = await message.answer(user_texts.about)
    asyncio.create_task(_(x))

@professor_user_router.message(Command('offer'))
async def offer(message: Message):
    x = await message.answer_document(FSInputFile(DATA_DIR / 'offer.pdf'), caption=user_texts.offer)
    asyncio.create_task(_(x))

@professor_user_router.message(Command('clicks'))
async def clicks(message: Message, state: FSMContext):
    await message.answer(user_texts.cartridge_volume, reply_markup=user_keyboards.cartridge_volume)
    await state.set_state(user_states.CalculateClicks.cartridge_volume)
    await message.delete()

@professor_user_router.message(Command('divisions'))
async def divisions(message: Message, state: FSMContext):
    await message.answer(user_texts.vial_amount, reply_markup=user_keyboards.back)
    await state.set_state(user_states.CalculateDivisions.vial_amount)
    await message.delete()

@professor_user_router.message(Command('graph'))
async def graph(message: Message):
    await message.answer(user_texts.choose_peptide, reply_markup=user_keyboards.peptides_keyboard)
    await message.delete()

@professor_user_router.message(CommandStart(deep_link=True, deep_link_encoded=True))
async def handle_encoded_deep_start(message: Message, command: CommandObject, state: FSMContext):
    raw_payload, params = _parse_start_payload(command.args)
    await _capture_first_touch_utm(message, params, raw_payload)
    product_id = params.get("product_id")
    if product_id:
        return await _send_product_deep_link_preview(message, product_id)
    return await handle_user_start(message, state)

@professor_user_router.message(CommandStart(deep_link=True))
async def handle_deep_start(message: Message, command: CommandObject, state: FSMContext):
    raw_payload, params = _parse_start_payload(command.args)
    await _capture_first_touch_utm(message, params, raw_payload)
    product_id = params.get("product_id")
    if product_id:
        return await _send_product_deep_link_preview(message, product_id)
    return await handle_user_start(message, state)


@professor_user_router.message(CommandStart())
async def handle_user_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    user = await webapp_client.get_user("tg_id", user_id)
    if user and user.tg_phone: schedule_webapp_call(safe_webapp_call(webapp_client.update_user_name(user_id, message.from_user.first_name, message.from_user.last_name), operation="update_user_name"), operation="update_user_name")
    return await message.answer(user_texts.greetings.replace('full_name', message.from_user.full_name), reply_markup=user_keyboards.main_menu)

@professor_user_router.message(user_states.Ai.activate_code)
async def handle_activate_code(message: Message, state: FSMContext):
    code = _normalize_order_code_input(message.text)
    if not code: return await message.answer("Введите номер заказа.")
    used_code = await webapp_client.get_used_code_by_code(code)

    if used_code:
        await message.answer(f"Номер заказа {code} уже использован")
        return await handle_user_start(message, state)

    try: verification = await webapp_client.verify_order_code(code)
    except WebappBotApiError as exc:
        if exc.status_code and exc.status_code >= 500: await message.answer("Не удалось проверить заказ (ошибка сервера). Попробуйте позже.")
        else: await message.answer("Не удалось проверить заказ (ошибка сети). Попробуйте позже.")
        user_flow_logger.warning("Order verification failed for user_id=%s code=%s err=%s", message.from_user.id, code, exc)
        return await handle_user_start(message, state)

    price = verification.price
    email = verification.email
    verification_code = verification.verification_code

    if verification.status == "not_found" or price == "not_found":
        await message.answer(f"Заказ не был найден по номеру {code}")
        return await handle_user_start(message, state)

    if verification.status == "smtp_failed":
        await message.answer("Заказ найден, но не удалось отправить код подтверждения на почту. Попробуйте позже или обратитесь в поддержку.")
        return await handle_user_start(message, state)

    if verification.status == "no_email":
        await message.answer("Заказ найден, но в контакте не указана корректная почта. Обратитесь в поддержку.")
        return await handle_user_start(message, state)

    if price == "old":
        await message.answer("Для активации заказов со старого сайта, пожалуйста, обратитесь к администрации.")
        return await handle_user_start(message, state)

    elif price == "low":
        await message.answer("Сожалеем, считываются заказы от 5000р. Каждые 5000р = 1 месяц.")
        return await handle_user_start(message, state)

    if not email or not verification_code:
        await message.answer("Заказ найден, но не удалось отправить код подтверждения на почту. Обратитесь в поддержку")
        return await handle_user_start(message, state)

    try: price = float(price)
    except ValueError: pass
    if not isinstance(price, (int, float)):
        await message.answer("Ошибка: сумма заказа некорректна.")
        return await handle_user_start(message, state)

    add_months = int(price) // 5000
    if add_months <= 0:
        await message.answer("Сожалеем, считываются заказы от 5000р. Каждые 5000р = 1 месяц.")
        return await handle_user_start(message, state)

    await state.update_data(verification_code=verification_code, add_months=add_months, failed=0, order_code=code, price=int(price), email=email)
    await state.set_state(user_states.Ai.verification_code)
    return await message.answer(
        f"На вашу почту {email} был отправлен код подтверждения.\n\n"
        "У вас есть 3 попытки, чтобы ввести его правильно. "
        "Иначе вы будете заблокированы на один день за попытку активации чужого заказа."
    )

@professor_user_router.message(user_states.Ai.verification_code)
async def handle_verification_code(message: Message, state: FSMContext):
    state_data = await state.get_data()
    entered_code = message.text.strip()
    verification_code = state_data.get("verification_code", None)
    add_months = state_data.get("add_months", None)
    order_code = state_data.get("order_code", None)
    price = state_data.get("price", None)
    failed = state_data.get("failed", 0)
    if not (verification_code and add_months and order_code and price):
        await message.answer("Ошибка, начните заново")
        await handle_user_start(message, state)

    elif entered_code == f"{verification_code}":
        user = await webapp_client.get_user("tg_id", message.from_user.id)
        premium_until = user.premium_until
        if not user.premium_until or user.premium_until <= datetime.now(tz=UFA_TZ): premium_until = datetime.now(tz=UFA_TZ) + timedelta(days=add_months * 30)
        else: premium_until += timedelta(days=add_months * 30)
        await webapp_client.update_user(message.from_user.id, {"premium_until": premium_until})
        await webapp_client.create_used_code({"user_id": message.from_user.id, "code": order_code, "price": price})
        await state.clear()
        await message.answer(f'Вам успешно начислено {add_months} месяцев безлимита, он теперь действителен до {premium_until.date()}')
        await handle_user_start(message, state)

    else:
        failed += 1
        if failed >= 3:
            await message.answer("Некорректных попыток: 3. Вы были заблокированы на 1 день")
            await webapp_client.update_user(message.from_user.id, {"blocked_until": datetime.now(tz=UFA_TZ) + timedelta(days=1)})
            await state.clear()
            return await handle_user_start(message, state)
        await message.answer(f"Код подтверждения некорректный, осталось {3-failed} попыток")
        await state.update_data(failed=failed)

    return None


@professor_user_router.message(user_states.Registration.phone)
async def handle_user_registration(message: Message, state: FSMContext, professor_bot, professor_client):
    if not message.contact: return await message.answer(user_texts.verify_phone.replace('*', message.from_user.full_name), reply_markup=user_keyboards.phone)
    phone = message.contact.phone_number
    await state.clear()
    await professor_bot.create_user(message.from_user.id, normalize_phone(phone), message.from_user.first_name, message.from_user.last_name)
    await message.answer('Проверка пройдена успешно ✅', reply_markup=ReplyKeyboardRemove())
    return await handle_user_start(message, state)

@professor_user_router.message(user_states.CalculateClicks.cartridge_volume, lambda message: message.text and message.text.strip())
async def handle_cartridge_volume(message: Message, state: FSMContext):
    try: amount = float(message.text.strip().replace(',', '.'))
    except: return await message.answer(user_texts.num_format_error, reply_markup=user_keyboards.back)
    await state.update_data(cartridge_volume=amount)
    await state.set_state(user_states.CalculateClicks.cartridge_amount)
    return await message.answer(user_texts.cartridge_amount, reply_markup=user_keyboards.back)


@professor_user_router.message(user_states.CalculateClicks.cartridge_amount, lambda message: message.text and message.text.strip())
async def handle_cartridge_amount(message: Message, state: FSMContext):
    try: amount = float(message.text.strip().replace(',', '.'))
    except: return await message.answer(user_texts.num_format_error, reply_markup=user_keyboards.back)
    await state.update_data(cartridge_amount_mg=amount)
    await state.set_state(user_states.CalculateClicks.desired_dosage)
    return await message.answer(user_texts.desired_dosage, reply_markup=user_keyboards.back)


@professor_user_router.message(user_states.CalculateClicks.desired_dosage, lambda message: message.text and message.text.strip())
async def handle_desired_dosage_clicks(message: Message, state: FSMContext):
    try: dosage_mg = float(message.text.strip().replace(',', '.'))
    except: return await message.answer(user_texts.num_format_error, reply_markup=user_keyboards.back)
    state_data = await state.get_data()
    cartridge_amount_mg = state_data['cartridge_amount_mg']
    cartridge_volume = state_data['cartridge_volume']

    mg_per_click = (cartridge_amount_mg / cartridge_volume) * 0.01
    click_amount_exact = dosage_mg / mg_per_click

    response_text = (f"<b>Входные данные</b>\n"
                     f"Объем картриджа (мл): <i>{_fmt(cartridge_volume)}</i>\n"
                     f"Количество вещества в картридже (мг): <i>{_fmt(cartridge_amount_mg)}</i>\n"
                     f"Желаемая дозировка вещества (мг): <i>{_fmt(dosage_mg)}</i>\n\n"
                     f"<b>Результаты</b>\n"
                     f"Количество вводимого вещества на 1 щелчок: ({_fmt(cartridge_amount_mg)}мг ÷ {_fmt(cartridge_volume)}мл) • 0.01мл = {_fmt(mg_per_click)}мг\n\n"
                     f"<b>ИТОГО КОЛИЧЕСТВО ЩЕЛЧКОВ: {_fmt(dosage_mg)}мг ÷ {_fmt(mg_per_click)}мг = {_fmt(click_amount_exact)}</b>")

    await message.answer(response_text, reply_markup=user_keyboards.backk)
    return await state.clear()


@professor_user_router.message(user_states.CalculateDivisions.vial_amount, lambda message: message.text and message.text.strip())
async def handle_vial_amount(message: Message, state: FSMContext):
    try: amount = float(message.text.strip().replace(',', '.'))
    except: return await message.answer(user_texts.num_format_error, reply_markup=user_keyboards.back)
    await state.update_data(vial_amount_mg=amount)
    await state.set_state(user_states.CalculateDivisions.water_volume)
    return await message.answer(user_texts.water_volume, reply_markup=user_keyboards.back)


@professor_user_router.message(user_states.CalculateDivisions.water_volume, lambda message: message.text and message.text.strip())
async def handle_water_volume(message: Message, state: FSMContext):
    try: amount = float(message.text.strip().replace(',', '.'))
    except: return await message.answer(user_texts.num_format_error, reply_markup=user_keyboards.back)
    await state.update_data(water_volume=amount)
    await state.set_state(user_states.CalculateDivisions.desired_dosage)
    return await message.answer(user_texts.desired_dosage, reply_markup=user_keyboards.back)


@professor_user_router.message(user_states.CalculateDivisions.desired_dosage, lambda message: message.text and message.text.strip())
async def handle_desired_dosage_divisions(message: Message, state: FSMContext):
    try: desired_dosage_mg = float(message.text.strip().replace(",", "."))
    except: return await message.answer(user_texts.num_format_error, reply_markup=user_keyboards.back)

    state_data = await state.get_data()
    vial_amount_mg = state_data["vial_amount_mg"]
    water_volume = state_data["water_volume"]

    vial_mcg = vial_amount_mg * 1000.0
    dosage_mcg = desired_dosage_mg * 1000.0

    mcg_per_ml = vial_mcg / water_volume
    mcg_per_division = mcg_per_ml * 0.01                          
    divisions = dosage_mcg / mcg_per_division                                  

    total_units = int(round(divisions))                
    full_syringes = total_units // 100
    remainder_units = total_units % 100

    def ru_plural(n: int, one: str, two_four: str, five: str) -> str:
        n = abs(n) % 100
        n1 = n % 10
        if 11 <= n <= 19: return five
        if n1 == 1: return one
        if 2 <= n1 <= 4: return two_four
        return five

    caption = "Визуализация делений на шприце"

    if full_syringes > 0 and remainder_units > 0: caption += f"\n<i>{full_syringes} {ru_plural(full_syringes, 'полный шприц', 'полных шприца', 'полных шприцев')}" f" + 1 на {remainder_units} {ru_plural(remainder_units, 'единицу', 'единицы', 'единиц')}</i>"
    elif full_syringes > 0 and remainder_units == 0: caption += f"\n<i>{full_syringes} {ru_plural(full_syringes, 'полный шприц', 'полных шприца', 'полных шприцев')}</i>"

    if full_syringes > 0: value_for_plot = remainder_units if remainder_units > 0 else 100
    else: value_for_plot = total_units

    fpath = plot_filled_scale(value_for_plot)
    await message.answer_photo(FSInputFile(fpath), caption=caption)
    fpath.unlink()

    response_text = (
        f"<b>Входные данные</b>\n"
        f"Количество вещества во флаконе (мг): <i>{_fmt(vial_amount_mg)}</i>\n"
        f"Объем воды (мл): <i>{_fmt(water_volume)}</i>\n"
        f"Желаемая дозировка вещества (мг): <i>{_fmt(desired_dosage_mg)}</i>\n\n"
        f"<b>Результаты</b>\n"
        f"Концентрация после разведения: {_fmt(vial_amount_mg)}мг ÷ {_fmt(water_volume)}мл = {_fmt(vial_amount_mg / water_volume)}мг/мл\n"
        f"Количество вещества на 1 единицу (0.01мл): {_fmt(vial_amount_mg / water_volume)}мг/мл • 0.01мл = {_fmt((vial_amount_mg / water_volume) * 0.01)}мг\n\n"
        f"<b>ИТОГО НУЖНО НАБРАТЬ ЕДИНИЦ: {_fmt(desired_dosage_mg)}мг ÷ {_fmt((vial_amount_mg / water_volume) * 0.01)}мг = {total_units}</b>\n"
    )

    await message.answer(response_text, reply_markup=user_keyboards.backk)
    return await state.clear()


@professor_user_router.message(user_states.Graph.dosage, lambda message: message.text and message.text.strip())
async def handle_dosage_graph(message: Message, state: FSMContext):
    try: amount = float(message.text.strip().replace(',', '.'))
    except: return await message.answer(user_texts.num_format_error, reply_markup=user_keyboards.back)
    await state.update_data(dose_mg=amount)
    await state.set_state(user_states.Graph.course_length_weeks)
    return await message.answer(user_texts.course_length_weeks, reply_markup=user_keyboards.back)


@professor_user_router.message(user_states.Graph.course_length_weeks, lambda message: message.text and message.text.strip())
async def handle_course_length_weeks(message: Message, state: FSMContext):
    try: amount = float(message.text.strip().replace(',', '.'))
    except: return await message.answer(user_texts.num_format_error, reply_markup=user_keyboards.back)
    await state.update_data(weeks=amount)
    await state.set_state(user_states.Graph.course_interval_days)
    return await message.answer(user_texts.course_interval_days, reply_markup=user_keyboards.back)


@professor_user_router.message(user_states.Graph.course_interval_days, lambda message: message.text and message.text.strip())
async def handle_course_interval_days(message: Message, state: FSMContext):
    try: interval_days = float(message.text.strip().replace(',', '.'))
    except: return await message.answer(user_texts.num_format_error, reply_markup=user_keyboards.back)

    state_data = await state.get_data()
    drug_key = state_data['drug_key']
    weeks = state_data['weeks']
    dose_mg = state_data['dose_mg']
    started_at = asyncio.get_running_loop().time()
    graph_request_logger.info("Graph request start | user_id=%s | drug=%s | weeks=%s | dose_mg=%s | interval_days=%s", message.from_user.id, drug_key, weeks, dose_mg, interval_days)
    fpath = None
    try:
        async with graph_generation_lock: filename = await asyncio.to_thread(generate_drug_graphs, drug_key, weeks, dose_mg, interval_days)
        fpath = DATA_DIR / filename
        caption = (
            f'График <b>содержания пептида в крови</b> на протяжении курса по параметрам\n'
            f'Пептид: <i>{drug_key.capitalize()}</i>\n'
            f'Длительность курса (в неделях): <i>{_fmt(weeks)}</i>\n'
            f'Интервал между уколами (в днях): <i>{_fmt(interval_days)}</i>\n'
            f'Дозировка пептида (мг): <i>{_fmt(dose_mg)}</i>\n'
        )
        await message.answer_photo(FSInputFile(fpath), caption=caption)
        graph_request_logger.info("Graph request done | user_id=%s | drug=%s | elapsed_ms=%s", message.from_user.id, drug_key, int((asyncio.get_running_loop().time() - started_at) * 1000))
        return await handle_user_start(message, state)
    except Exception:
        graph_request_logger.exception("Graph request failed | user_id=%s | drug=%s | weeks=%s | dose_mg=%s | interval_days=%s", message.from_user.id, drug_key, weeks, dose_mg, interval_days)
        await state.clear()
        return await message.answer("Не удалось построить график концентрации. Попробуйте позже.", reply_markup=user_keyboards.calc_back)
    finally:
        if fpath is not None:
            try: fpath.unlink(missing_ok=True)
            except Exception: graph_request_logger.exception("Failed to remove graph file: %s", fpath)

@professor_user_router.message(Command('new_chat'))
async def handle_new_chat(message: Message, state: FSMContext, professor_client, expert_client=None):
    user = await webapp_client.get_user("tg_id", message.from_user.id)
    last_used, has_unknown_last_used = _resolve_last_used(user)
    if user and has_unknown_last_used: schedule_webapp_call(safe_webapp_call(webapp_client.update_user(message.from_user.id, {"last_used": last_used}), operation="update_last_used"), operation="update_last_used")
    active_client = _resolve_mode_client(last_used, professor_client, expert_client)
    conversation_id = await active_client.create_conversation(user_id=message.from_user.id)
    await safe_webapp_call(webapp_client.upsert_user({"tg_id": message.from_user.id, "name": message.from_user.first_name, "surname": message.from_user.last_name, "conversation_id": conversation_id, "last_used": last_used}), operation="upsert_conversation_id")
    return await message.answer(user_texts.new_chat)

@professor_user_router.callback_query()
async def handle_user_call(call: CallbackQuery, state: FSMContext):
    data = call.data.removeprefix("user:").split(":")
    if data[0] == "about": return await about(call.message)
    elif data[0] == "offer": return await offer(call.message)
    elif data[0] == "ai":
        if data[1] == "start": await call.message.edit_text(user_texts.pick_ai, reply_markup=user_keyboards.pick_ai)

        elif data[1] == "free":
            await safe_webapp_call(
                webapp_client.upsert_user(
                    {
                        "tg_id": call.from_user.id,
                        "name": call.from_user.first_name,
                        "surname": call.from_user.last_name,
                        "last_used": LAST_USED_EXPERT,
                    }
                ),
                operation="set_last_used_free",
            )
            await call.message.edit_text(user_texts.pick_free, reply_markup=user_keyboards.back)

        elif data[1] == "premium":
            user = await webapp_client.get_user("tg_id", call.from_user.id)
            if not user or not user.tg_phone: return await _request_phone(call.message, state, call.from_user.full_name)
            await safe_webapp_call(
                webapp_client.update_user(call.from_user.id, {"last_used": LAST_USED_PROFESSOR}),
                operation="set_last_used_premium",
            )
            await call.message.edit_text(user_texts.pick_premium, reply_markup=user_keyboards.back)

        elif data[1] == "activate_code":
            await state.set_state(user_states.Ai.activate_code)
            await call.message.edit_text('Отправьте код <u>оплаченного</u> заказа, чтобы засчитать его сумму', reply_markup=user_keyboards.back)

    elif data[0] == "calculators": await call.message.edit_text(user_texts.calculators_start, reply_markup=user_keyboards.calculators_menu)
    elif data[0] == "clicks":
        if data[1] == "start":
            await call.message.edit_text(user_texts.cartridge_volume, reply_markup=user_keyboards.cartridge_volume)
            await state.set_state(user_states.CalculateClicks.cartridge_volume)

        elif data[1] == 'cartridge_volume':
            await state.clear()
            await state.update_data(cartridge_volume=3)
            await state.set_state(user_states.CalculateClicks.cartridge_amount)
            await call.message.edit_text(user_texts.cartridge_amount, reply_markup=user_keyboards.back)

    elif data[0] == "divisions":
        if data[1] == 'start':
            await call.message.edit_text(user_texts.vial_amount, reply_markup=user_keyboards.back)
            await state.set_state(user_states.CalculateDivisions.vial_amount)

    elif data[0] == "graph":
        if data[1] == 'start': await call.message.edit_text(user_texts.choose_peptide, reply_markup=user_keyboards.peptides_keyboard)
        elif data[1] == "drug":
            drug_key = data[2]
            await state.update_data(drug_key=drug_key)
            await state.set_state(user_states.Graph.dosage)
            await call.message.edit_text(user_texts.dosage, reply_markup=user_keyboards.back)

    elif data[0] == "main_menu": await call.message.edit_text(user_texts.greetings.replace('full_name', call.from_user.full_name), reply_markup=user_keyboards.main_menu)
    elif data[0] == "main_menuu":
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(user_texts.greetings.replace('full_name', call.from_user.full_name), reply_markup=user_keyboards.main_menu)

    return None


@professor_user_router.message(MediaGroupFilter())
@media_group_handler()
async def handle_media_group(messages: list[Message], state: FSMContext, professor_bot, professor_client, expert_client=None):
    message = messages[0]
    user_id = message.from_user.id

    existing_user = await webapp_client.get_user("tg_id", user_id)
    last_used, has_unknown_last_used = _resolve_last_used(existing_user)
    active_client = _resolve_mode_client(last_used, professor_client, expert_client)
    user = await _ensure_user(message, active_client)
    if has_unknown_last_used:
        schedule_webapp_call(safe_webapp_call(webapp_client.update_user(user_id, {"last_used": last_used}), operation="update_last_used"), operation="update_last_used")
        await _notify_user(message, user_texts.pick_fallback_free, 10)

    elif last_used != LAST_USED_PROFESSOR: await _notify_user(message, user_texts.premium_mode_hint, 10)

    if last_used != LAST_USED_PROFESSOR: return await message.answer(user_texts.expert_text_only, reply_markup=user_keyboards.upgrade_to_professor)
    if user.tg_phone: schedule_webapp_call(safe_webapp_call(webapp_client.update_user_name(user_id, message.from_user.first_name, message.from_user.last_name), operation="update_user_name"), operation="update_user_name")

    used_requests = 0 if user.tg_phone else await _get_unverified_requests_count(user_id)
    if not user.tg_phone and (last_used == LAST_USED_PROFESSOR or used_requests >= UNVERIFIED_REQUEST_LIMIT): return await _request_phone(message, state)
    if last_used == LAST_USED_PROFESSOR and not _can_use_professor_mode(user): return await message.answer(user_texts.premium_limit_0, reply_markup=user_keyboards.only_free)

    response = await safe_ai_response(message, send_message_v2_from_media_group(messages=messages, professor_client=active_client, user_id=user_id, conversation_id=user.conversation_id))
    if response is None: return None
    schedule_webapp_call(
        safe_webapp_call(
            webapp_client.write_usage(
                message.from_user.id,
                response['input_tokens'],
                response['output_tokens'],
                last_used,
                cached_input_tokens=response.get("cached_input_tokens"),
            ),
            operation="write_usage",
        ),
        operation="write_usage",
    )
    if last_used == LAST_USED_PROFESSOR and _should_spend_premium_request(user):
        next_requests = max(int(getattr(user, "premium_requests", 0) or 0) - 1, 0)
        schedule_webapp_call(safe_webapp_call(webapp_client.update_user(message.from_user.id, {"premium_requests": next_requests}), operation="decrement_premium_requests"), operation="decrement_premium_requests")

    return await professor_bot.parse_response(response, message, back_menu=True)


@professor_user_router.message(lambda message: not message.media_group_id and ((message.text and message.text.strip()) or (message.caption and message.caption.strip()) or message.photo or message.video or message.video_note or message.document or message.voice))
@with_action()
async def handle_single_ai_message(message: Message, state: FSMContext, professor_bot, professor_client, expert_client=None):
    user_id = message.from_user.id
    existing_user = await webapp_client.get_user("tg_id", user_id)
    last_used, has_unknown_last_used = _resolve_last_used(existing_user)
    active_client = _resolve_mode_client(last_used, professor_client, expert_client)
    user = await _ensure_user(message, active_client)
    if has_unknown_last_used:
        schedule_webapp_call(safe_webapp_call(webapp_client.update_user(user_id, {"last_used": last_used}), operation="update_last_used"), operation="update_last_used")
        await _notify_user(message, user_texts.pick_fallback_free, 10)

    elif last_used != LAST_USED_PROFESSOR: await _notify_user(message, user_texts.premium_mode_hint, 10)

    if last_used != LAST_USED_PROFESSOR and has_supported_media(message): return await message.answer(user_texts.expert_text_only, reply_markup=user_keyboards.upgrade_to_professor)
    if user.tg_phone:schedule_webapp_call(safe_webapp_call(webapp_client.update_user_name(user_id, message.from_user.first_name, message.from_user.last_name), operation="update_user_name"), operation="update_user_name")

    used_requests = 0 if user.tg_phone else await _get_unverified_requests_count(user_id)
    if not user.tg_phone and (last_used == LAST_USED_PROFESSOR or used_requests >= UNVERIFIED_REQUEST_LIMIT): return await _request_phone(message, state)
    if last_used == LAST_USED_PROFESSOR and not _can_use_professor_mode(user): return await message.answer(user_texts.premium_limit_0, reply_markup=user_keyboards.only_free)

    response = await safe_ai_response(message, send_message_v2_from_telegram(message=message, professor_client=active_client, user_id=user_id, conversation_id=user.conversation_id))
    if response is None: return None
    schedule_webapp_call(
        safe_webapp_call(
            webapp_client.write_usage(
                message.from_user.id,
                response['input_tokens'],
                response['output_tokens'],
                last_used,
                cached_input_tokens=response.get("cached_input_tokens"),
            ),
            operation="write_usage",
        ),
        operation="write_usage",
    )
    if last_used == LAST_USED_PROFESSOR and _should_spend_premium_request(user):
        next_requests = max(int(getattr(user, "premium_requests", 0) or 0) - 1, 0)
        schedule_webapp_call(safe_webapp_call(webapp_client.update_user(message.from_user.id, {"premium_requests": next_requests}), operation="decrement_premium_requests"),operation="decrement_premium_requests")

    return await professor_bot.parse_response(response, message, back_menu=True)
