import asyncio
import csv
import os
import pandas as pd

from dataclasses import dataclass
from datetime import datetime, date, timedelta
from tempfile import TemporaryDirectory
from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, FSInputFile

from config import ADMIN_TG_IDS, SPENDS_DIR, EXPERT_BOT_TOKEN, DOSE_BOT_TOKEN, UFA_TZ
from src.bot.keyboards import admin_keyboards
from src.bot.states import admin_states
from src.ai.webapp_client import webapp_client
from src.tg_methods import get_user_id_by_phone, normalize_phone

expert_admin_router = Router(name="admin_expert")
professor_admin_router = Router(name="admin_professor")
professor_admin_router.inline_query.filter(lambda query: query.from_user.id in ADMIN_TG_IDS)
dose_admin_router = Router(name="admin_dose")

expert_admin_router.message.filter(lambda message: message.from_user.id in ADMIN_TG_IDS and message.chat.type == ChatType.PRIVATE)
expert_admin_router.callback_query.filter(lambda call: call.data.startswith("admin") and call.from_user.id in ADMIN_TG_IDS and call.message.chat.type == ChatType.PRIVATE)
professor_admin_router.message.filter(lambda message: message.from_user.id in ADMIN_TG_IDS and message.chat.type == ChatType.PRIVATE)
professor_admin_router.callback_query.filter(lambda call: call.data.startswith("admin") and call.from_user.id in ADMIN_TG_IDS and call.message.chat.type == ChatType.PRIVATE)
dose_admin_router.message.filter(lambda message: message.from_user.id in ADMIN_TG_IDS and message.chat.type == ChatType.PRIVATE)
dose_admin_router.callback_query.filter(lambda call: call.data.startswith("admin") and call.from_user.id in ADMIN_TG_IDS and call.message.chat.type == ChatType.PRIVATE)

SEND_BROADCAST_DELAY_SEC = 0.2
active_send_broadcasts: dict[int, asyncio.Event] = {}
pending_send_confirmations: dict[int, "PendingSendConfirmation"] = {}


@dataclass
class PendingSendConfirmation:
    admin_id: int
    text: str
    step: int = 1
    message_id: int | None = None

async def _send_broadcast_result_files(
    message: Message,
    status_text: str,
    success_rows: list[tuple[int, int]],
    error_rows: list[tuple[int, str]],
):
    with TemporaryDirectory(prefix=f"send_{message.bot.id}_") as tmp_dir:
        success_path = os.path.join(tmp_dir, "success.csv")
        error_path = os.path.join(tmp_dir, "error.csv")

        with open(success_path, "w", encoding="utf-8", newline="") as success_file:
            writer = csv.writer(success_file)
            writer.writerow(["tg_id", "message_id"])
            writer.writerows(success_rows)

        with open(error_path, "w", encoding="utf-8", newline="") as error_file:
            writer = csv.writer(error_file)
            writer.writerow(["tg_id", "reason"])
            writer.writerows(error_rows)

        await message.answer(status_text)
        await message.answer_document(FSInputFile(success_path), caption="success.csv")
        await message.answer_document(FSInputFile(error_path), caption="error.csv")


async def _run_send_broadcast(message: Message, text: str):
    if message.bot.id in active_send_broadcasts: return await message.answer("Рассылка уже выполняется. Для остановки отправьте <code>/stop_send</code>")

    users = await webapp_client.get_users()
    stop_event = asyncio.Event()
    active_send_broadcasts[message.bot.id] = stop_event
    await message.answer("рассылка успешно запущена", reply_markup=admin_keyboards.send_cancel)
    success_rows: list[tuple[int, int]] = []
    error_rows: list[tuple[int, str]] = []
    stopped = False
    try:
        for n, user in enumerate(users):
            if stop_event.is_set():
                stopped = True
                break

            try:
                await message.bot.get_chat(user.tg_id)
                sent_message = await message.bot.send_message(user.tg_id, text)
                success_rows.append((user.tg_id, sent_message.message_id))
            except Exception as exc:
                reason = str(exc).strip() or exc.__class__.__name__
                error_rows.append((user.tg_id, reason))

            if n < len(users) - 1:
                try: await asyncio.wait_for(stop_event.wait(), timeout=SEND_BROADCAST_DELAY_SEC)
                except asyncio.TimeoutError: pass

        status_text = (
            f"Рассылка остановлена. Всего пользователей: {len(users)}. Успешно: {len(success_rows)}. Ошибок: {len(error_rows)}."
            if stopped or stop_event.is_set()
            else f"Рассылка завершена. Всего пользователей: {len(users)}. Успешно: {len(success_rows)}. Ошибок: {len(error_rows)}."
        )
        await _send_broadcast_result_files(message, status_text, success_rows, error_rows)
    finally:
        if active_send_broadcasts.get(message.bot.id) is stop_event: active_send_broadcasts.pop(message.bot.id, None)


async def _handle_send_confirm_callback(call: CallbackQuery):
    payload = (call.data or "").split(":")
    if len(payload) < 4: return await call.answer("Некорректное подтверждение")
    action = payload[3]
    bot_id = call.message.bot.id
    pending = pending_send_confirmations.get(bot_id)
    if not pending: return await call.answer("Нет ожидающей рассылки", show_alert=True)
    if pending.admin_id != call.from_user.id: return await call.answer("Подтвердить запуск может только администратор, который отправил /send", show_alert=True)
    if pending.message_id is not None and call.message.message_id != pending.message_id: return await call.answer("Это устаревшее подтверждение", show_alert=True)

    if action == "cancel":
        pending_send_confirmations.pop(bot_id, None)
        await call.answer("Запуск рассылки отменен")
        try: await call.message.edit_text("Запуск рассылки отменен")
        except Exception: pass
        return

    if action == "1":
        pending.step = 2
        await call.answer("Подтверждение 1/2 принято")
        return await call.message.edit_text("Подтверждение рассылки 2/2. Нажмите кнопку еще раз для запуска.", reply_markup=admin_keyboards.send_confirm(2))

    if action == "2":
        if pending.step != 2: return await call.answer("Сначала нажмите подтверждение 1/2", show_alert=True)
        pending_send_confirmations.pop(bot_id, None)
        await call.answer("Запускаю рассылку")
        try: await call.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
        return await _run_send_broadcast(call.message, pending.text)

    return await call.answer("Некорректное подтверждение")


async def _handle_send_cancel_callback(call: CallbackQuery):
    stop_event = active_send_broadcasts.get(call.message.bot.id)
    if not stop_event: return await call.answer("Сейчас нет активной рассылки", show_alert=True)
    stop_event.set()
    await call.answer("Останавливаю рассылку...")
    try: await call.message.edit_reply_markup(reply_markup=None)
    except Exception: pass

@professor_admin_router.message(Command("stop_send"))
@dose_admin_router.message(Command("stop_send"))
@expert_admin_router.message(Command("stop_send"))
async def handle_stop_send(message: Message):
    stop_event = active_send_broadcasts.get(message.bot.id)
    if not stop_event: return await message.answer("Сейчас нет активной рассылки")
    stop_event.set()
    await message.answer("Останавливаю рассылку...")

@professor_admin_router.message(Command("fix"))
@dose_admin_router.message(Command("fix"))
@expert_admin_router.message(Command("fix"))
async def handle_fix(message: Message):
    users = await webapp_client.get_users()
    for user in users:
        if user.tg_id == 896376335:
            print("break")
            break

        else:
            try: await message.bot.send_message(user.tg_id, """Также в новом обновлении используется набор ИИ: Grok, Gemini, Midjourney, Claude.

Мы расширяем возможности ассистента""")
            except Exception as e: print(e)
        await asyncio.sleep(1)



@professor_admin_router.message(Command("send"))
@dose_admin_router.message(Command("send"))
@expert_admin_router.message(Command("send"))
async def handle_send(message: Message):
    text = (message.html_text or message.text or "").strip()
    args = text.removeprefix("/send ").strip().split(maxsplit=1)
    if len(args) < 2: return await message.answer("Ошибка команды: <code>/send тг_айди/all текст</code>\nОстановить рассылку: <code>/stop_send</code>")
    who, text = args[0], args[1]

    if who.isdigit():
        user_id = int(who)
        user = await webapp_client.get_user("tg_id", user_id)
        if user:
            try:
                await message.bot.get_chat(user_id)
                try:
                    sent_message = await message.bot.send_message(user_id, text)
                    await message.answer(
                        f"Сообщение успешно отправлено пользователю с номером {user.tg_phone}\n"
                        f"message_id: <code>{sent_message.message_id}</code>"
                    )
                except Exception as e: await message.answer(str(e))
            except: await message.answer(f"Чат у пользователя с айди {user.tg_id} не был найден")
        else: await message.answer(f"Пользователь с айди {user_id} не был найден")

    elif who == "all":
        if message.bot.id in active_send_broadcasts: return await message.answer("Рассылка уже выполняется. Для остановки отправьте <code>/stop_send</code>")
        pending = pending_send_confirmations.get(message.bot.id)
        if pending and pending.admin_id != message.from_user.id: return await message.answer("Другой администратор уже подтверждает запуск рассылки. Дождитесь завершения подтверждения.")

        pending_send_confirmations[message.bot.id] = PendingSendConfirmation(admin_id=message.from_user.id, text=text, step=1)
        confirmation_message = await message.answer(
            "Подтверждение рассылки 1/2. Нажмите кнопку ниже, чтобы продолжить запуск.",
            reply_markup=admin_keyboards.send_confirm(1)
        )
        pending_send_confirmations[message.bot.id].message_id = confirmation_message.message_id
    else: await message.answer("Ошибка команды: <code>/send тг_айди/all текст</code>")


@expert_admin_router.message(CommandStart())
@dose_admin_router.message(CommandStart())
async def handle_admin_start(message: Message):
    await message.answer(f'{message.from_user.full_name}, Добро пожаловать в <b>админ панель</b>\n\nВыберите действие кнопками ниже', reply_markup=admin_keyboards.main_menu, parse_mode="html")
    await message.delete()

@expert_admin_router.message(Command('block'))
@professor_admin_router.message(Command('block'))
@dose_admin_router.message(Command('block'))
async def handle_block(message: Message):
    text = (message.text or "").strip()
    args = text.removeprefix("/block ").split()
    if len(args) != 2: return await message.answer("<b>Ошибка команды</b>\n<code>/block phone номер_телефона</code>\n<code>/block id айди_телеграм</code>")
    mode, value = args[0], args[1]
    user_update = {"blocked_until": datetime.max.replace(tzinfo=UFA_TZ)}
    full_name = "Unknown"

    if mode == "id":
        if not value.isdigit(): return await message.answer("<b>Ошибка команды:</b> айди должен быть числом\n<code>/block id 123456789</code>")
        user_id = int(value)
        user = await webapp_client.get_user("tg_id", user_id)
        if not user: return await message.answer(f"<b>Ошибка команды: пользователь с айди {user_id} не найден</b>")
        await webapp_client.update_user(user.tg_id, user_update)

        try:
            chat = await message.bot.get_chat(user_id)
            if chat: full_name = chat.full_name
        except Exception: full_name = str(user_id)
        return await message.answer(f"Пользователь {full_name} успешно <b>заблокирован</b>\nКоманда для разблокировки: <code>/unblock id {user_id}</code>")

    elif mode == "phone":
        phone = normalize_phone(value)
        full_name = phone
        user = await webapp_client.get_user("tg_phone", phone)
        if not user and not phone.startswith("+"): user = await webapp_client.get_user("tg_phone", f"+{phone}")

        if not user:
            user_id = await get_user_id_by_phone(phone)
            if not user_id:return await message.answer(f"<b>Ошибка команды: пользователь с номером +{phone.removeprefix('+')} не найден</b>")
            user = await webapp_client.get_user("tg_id", user_id)
            if not user: return await message.answer(f"<b>Ошибка команды: пользователь с номером +{phone.removeprefix('+')} не найден</b>")
            await webapp_client.update_user(user.tg_id, user_update)
        else: await webapp_client.update_user(user.tg_id, user_update)

        try:
            chat = await message.bot.get_chat(user.tg_id)
            if chat: full_name = chat.full_name
        except Exception: pass
        return await message.answer(f"Пользователь {full_name} успешно <b>заблокирован</b>\nКоманда для разблокировки: <code>/unblock phone +{phone.removeprefix('+')}</code>")

    else: return await message.answer("<b>Ошибка команды</b>\n<code>/block phone номер_телефона</code>\n<code>/block id айди_телеграм</code>")

@expert_admin_router.message(Command('unblock'))
@professor_admin_router.message(Command('unblock'))
@dose_admin_router.message(Command('unblock'))
async def handle_unblock(message: Message):
    text = (message.text or "").strip()
    args = text.removeprefix("/unblock ").split()
    if len(args) != 2: return await message.answer("<b>Ошибка команды</b>\n<code>/unblock phone номер_телефона</code>\n<code>/unblock id айди_телеграм</code>")
    mode, value = args[0], args[1]
    user_update = {"blocked_until": None}
    full_name = "Unknown"

    if mode == "id":
        if not value.isdigit(): return await message.answer("<b>Ошибка команды:</b> айди должен быть числом\n<code>/unblock id 123456789</code>")
        user_id = int(value)
        user = await webapp_client.get_user("tg_id", user_id)
        if not user: return await message.answer(f"<b>Ошибка команды: пользователь с айди {user_id} не найден</b>")
        await webapp_client.update_user(user.tg_id, user_update)

        try:
            chat = await message.bot.get_chat(user_id)
            if chat: full_name = chat.full_name
        except Exception: full_name = str(user_id)
        return await message.answer(f"Пользователь {full_name} успешно <b>разблокирован</b>\nКоманда для блокировки: <code>/block id {user_id}</code>")

    elif mode == "phone":
        phone = normalize_phone(value)
        full_name = phone
        user = await webapp_client.get_user("tg_phone", phone)
        if not user and not phone.startswith("+"): user = await webapp_client.get_user("tg_phone", f"+{phone}")

        if not user:
            user_id = await get_user_id_by_phone(phone)
            if not user_id:return await message.answer(f"<b>Ошибка команды: пользователь с номером +{phone.removeprefix('+')} не найден</b>")
            user = await webapp_client.get_user("tg_id", user_id)
            if not user: return await message.answer(f"<b>Ошибка команды: пользователь с номером +{phone.removeprefix('+')} не найден</b>")
            await webapp_client.update_user(user.tg_id, user_update)
        else: await webapp_client.update_user(user.tg_id, user_update)
        try:
            chat = await message.bot.get_chat(user.tg_id)
            if chat: full_name = chat.full_name
        except Exception: pass
        return await message.answer(f"Пользователь {full_name} успешно <b>разблокирован</b>\nКоманда для блокировки: <code>/block phone +{phone.removeprefix('+')}</code>")

    else: return await message.answer("<b>Ошибка команды</b>\n<code>/unblock phone номер_телефона</code>\n<code>/unblock id айди_телеграм</code>")

@expert_admin_router.message(admin_states.MainMenu.spends_time)
@professor_admin_router.message(admin_states.MainMenu.spends_time)
@dose_admin_router.message(admin_states.MainMenu.spends_time)
async def handle_spends_time(message: Message):
    text = message.text.strip()
    dates = text.split()
    if len(dates) != 2:return await message.answer("<b>Неверное количество дат.</b>\nПожалуйста, укажите <b>ровно две даты</b> через пробел.\nПример: <code>22.09.2025 12.10.2025</code>", reply_markup=admin_keyboards.main_menu, parse_mode="HTML")
    try:
        start_date = datetime.strptime(dates[0], "%d.%m.%Y").date()
        end_date = datetime.strptime(dates[1], "%d.%m.%Y").date()
        if end_date < start_date: raise ValueError("End date is before start date")
    except Exception: return await message.answer("<b>Ошибка формата промежутка.</b>\n" "Пожалуйста, следуйте примеру:\n" "<code>22.09.2025 12.10.2025</code>\n" "(можно скопировать по нажатию)", reply_markup=admin_keyboards.main_menu, parse_mode="HTML")

    bot_id = str(message.bot.id)
    if bot_id == EXPERT_BOT_TOKEN.split(':')[0]: bot = "professor"
    elif bot_id == DOSE_BOT_TOKEN.split(':')[0]: bot = "dose"
    else: bot = "new"

    period_label, usages = await webapp_client.get_usages(start_date, end_date, bot=bot)
    if not usages: return await message.answer(f"📭 Нет данных за период {period_label}.", reply_markup=admin_keyboards.main_menu, parse_mode="HTML")

    df = pd.DataFrame(usages)
    safe_label = period_label.replace(":", "-").replace("/", "-")
    file_path = os.path.join(SPENDS_DIR, f"Расходы {safe_label}.xlsx")
    df.to_excel(file_path, index=False)
    await message.answer_document(FSInputFile(file_path), caption=f"📊 Файл со статистикой расходов <b>{period_label}</b>", parse_mode="HTML", reply_markup=admin_keyboards.main_menu)
    return os.remove(file_path)

@expert_admin_router.callback_query()
@dose_admin_router.callback_query()
async def handle_admin_callback(call: CallbackQuery, state: FSMContext):
    data = (call.data or "").split(":")[1:]
    if not data: return
    if data[0] == "send":
        if len(data) < 2: return
        if data[1] == "cancel": return await _handle_send_cancel_callback(call)
        if data[1] == "confirm": return await _handle_send_confirm_callback(call)
        return
    if data[0] != "spends": return
    try: await call.answer()
    except Exception: pass
    if len(data) == 1:
        await state.set_state(admin_states.MainMenu.spends_time)
        await call.message.edit_text('Выберите <b>временной промежуток</b> за который будете смотреть расходы\n\nТакже можете отправить <i>количество дней цифрой</i> или <i>промежуток</i> вида <code>22.09.2025 12.10.2025</code>.', parse_mode="HTML", reply_markup=admin_keyboards.spend_times)
        return

    preset = data[1]
    today = date.today()

    if preset == "0": start_date, end_date = date(1970, 1, 1), today
    else:
        try: days = max(1, int(preset))          
        except ValueError: days = 1
        end_date = today
        start_date = end_date - timedelta(days=days - 1)

    bot_id = str(call.bot.id)
    if bot_id == EXPERT_BOT_TOKEN.split(":")[0]: bot = "professor"
    elif bot_id == DOSE_BOT_TOKEN.split(":")[0]: bot = "dose"
    else: bot = "new"

    period_label, usages = await webapp_client.get_usages(start_date, end_date, bot=bot)
    df = pd.DataFrame(usages)
    safe_label = (period_label or "").replace(":", "-").replace("/", "-")
    file_path = os.path.join(SPENDS_DIR, f"Расходы {safe_label}.xlsx")
    df.to_excel(file_path, index=False)

    await call.message.answer_document(FSInputFile(file_path), caption=f"📊 Файл со статистикой расходов всех пользователей <b>{period_label}</b>", parse_mode="HTML")
    try: os.remove(file_path)
    except Exception: pass

    await state.clear()
    await call.message.answer(f'{call.from_user.full_name}, Добро пожаловать в <b>админ панель</b>\n\nВыберите действие кнопками ниже', reply_markup=admin_keyboards.main_menu, parse_mode="HTML")

    try: await call.message.delete()
    except Exception: pass
