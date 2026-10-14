"""Explicit food-preview recording and state-bound meal deletion callbacks."""
import re
import secrets

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.ai.mentor_input import input_lock
from src.ai.telegram_mentor import api, BridgeError
from .mentor_panel import remember_card, missing_card


def _button(text, action):
    return InlineKeyboardButton(text=text, callback_data="mentor:" + action)


def food_response_rows(response):
    estimate = response.get("food_estimate") or {}
    if estimate.get("kind") != "food_estimate" or estimate.get("status") != "preview":
        return []
    entry_id = estimate["id"]
    return [[_button("Записать в дневник", f"estimate_record:{entry_id}"),
        _button("Отмена", f"estimate_cancel:{entry_id}")]]


def meal_delete_row(entry_id, label="Удалить"):
    return [_button(label, f"meal_delete:{entry_id}")]


def _menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button("К питанию", "food")], [_button("Меню наставника", "menu")]])


async def _receipt(message, state, text, markup=None):
    sent = await message.answer(text, parse_mode=None, reply_markup=markup or _menu())
    await remember_card(state, sent, kind="receipt")
    return sent


async def _remove_buttons(message, actions):
    markup = getattr(message, "reply_markup", None)
    if markup is None:
        return
    rows = [[b for b in row if b.callback_data not in actions] for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    try:
        await message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest as error:
        if not missing_card(error) and "message is not modified" not in str(error).lower():
            raise


async def dispatch_food(query, state, action, message=None, data=None):
    """Call after existing phone/access/food gates; always preserve clicked text.

    `message` may be a MentorPanel. Receipts deliberately use query.message's
    real transport so its flush cannot replace an estimate or AI response.
    """
    root = action.split(":", 1)[0]
    if root not in {"estimate_record", "estimate_cancel", "meal_delete",
            "meal_delete_confirm", "meal_delete_cancel"}:
        return False
    source = query.message
    uid = query.from_user.id
    if (getattr(getattr(source, "chat", None), "type", None) != "private"
            or source.chat.id != uid):
        await query.answer("Откройте наставника в личном чате.", show_alert=True)
        return True
    match = re.fullmatch(r"(estimate_record|estimate_cancel|meal_delete):([1-9][0-9]*)", action)
    confirmation = re.fullmatch(r"(meal_delete_confirm|meal_delete_cancel):([1-9][0-9]*):([a-f0-9]{16})", action)
    if not match and not confirmation:
        await query.answer("Эта кнопка устарела. Откройте питание заново.")
        return True
    entry_id = int((match or confirmation)[2])
    async with input_lock(uid):
        saved = await state.get_data()
        if root == "estimate_record":
            # Backend checks ownership and creates a meal from its stored preview
            # exactly once. Never trust nutrition data from the callback/model.
            result = await api("/workspace/food/record", {"telegram_user_id": uid, "entry_id": entry_id})
            entry = result.get("entry") or {}
            if entry.get("kind") != "meal" or entry.get("status") != "confirmed":
                raise BridgeError("Запись не подтверждена. Попробуйте ещё раз.")
            await state.update_data(mentor_food_lookup=False)
            await _remove_buttons(source, {f"mentor:estimate_record:{entry_id}", f"mentor:estimate_cancel:{entry_id}"})
            await _receipt(source, state, "Еда записана в дневник.", InlineKeyboardMarkup(inline_keyboard=[
                meal_delete_row(entry["id"]), *_menu().inline_keyboard]))
        elif root == "estimate_cancel":
            await state.update_data(mentor_food_lookup=False)
            await _remove_buttons(source, {f"mentor:estimate_record:{entry_id}", f"mentor:estimate_cancel:{entry_id}"})
            await _receipt(source, state, "Оценка осталась в переписке. Данные дневника не меняю.")
        elif root == "meal_delete":
            result = await api("/workspace/record", {"telegram_user_id": uid, "entry_id": entry_id})
            entry = result.get("entry") or {}
            if entry.get("kind") != "meal" or entry.get("status") != "confirmed":
                raise BridgeError("Удалить можно только сохранённую еду.")
            token = secrets.token_hex(8)
            await state.update_data(mentor_meal_delete={"entry_id": entry_id, "user_id": uid, "token": token})
            await _receipt(source, state, "Удалить эту еду из дневника?", InlineKeyboardMarkup(inline_keyboard=[
                [_button("Да, удалить", f"meal_delete_confirm:{entry_id}:{token}"),
                 _button("Отмена", f"meal_delete_cancel:{entry_id}:{token}")]]))
        else:
            pending = saved.get("mentor_meal_delete") or {}
            if pending != {"entry_id": entry_id, "user_id": uid, "token": confirmation[3]}:
                await query.answer("Эта запись уже обработана или подтверждение устарело.")
                return True
            if root == "meal_delete_confirm":
                result = await api("/workspace/meals/delete", {"telegram_user_id": uid, "entry_id": entry_id})
                if not result.get("ok"):
                    raise BridgeError("Не удалось удалить запись. Попробуйте ещё раз.")
            await state.update_data(mentor_meal_delete=None)
            await _remove_buttons(source, {
                f"mentor:meal_delete_confirm:{entry_id}:{confirmation[3]}",
                f"mentor:meal_delete_cancel:{entry_id}:{confirmation[3]}"})
            await _receipt(source, state, "Еда удалена из дневника." if root == "meal_delete_confirm"
                else "Удаление отменено. Еда осталась в дневнике.")
    return True
