from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

main_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text='💸 Расходы Ассистента', callback_data='admin:spends')],
])



open_test = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Магазин", url="t.me/elixirpeptidebot/test")],
])

admin_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text='💸 Расходы Ассистента', callback_data='admin:spends')],
    [InlineKeyboardButton(text='📈 UTM статистика', callback_data='admin:utm_stats')],
    [InlineKeyboardButton(text="👥 Пользователи", callback_data='admin:users:search'),
     InlineKeyboardButton(text="🛍️ Заказы", switch_inline_query_current_chat='search_cart ')]
])

search_users_choice = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Полное имя", switch_inline_query_current_chat='search_user full_name '),
     InlineKeyboardButton(text="Email", switch_inline_query_current_chat='search_user email ')],
    [InlineKeyboardButton(text="Телеграм ID", switch_inline_query_current_chat='search_user tg_id '),
     InlineKeyboardButton(text="Телеграм username", switch_inline_query_current_chat='search_user username ')],
    [InlineKeyboardButton(text="Номер Telegram", switch_inline_query_current_chat='search_user phone ')]
])

back_button = InlineKeyboardButton(text="🔙 Главное меню", callback_data='admin:main_menu')
back = InlineKeyboardMarkup(inline_keyboard=[[back_button]])

backk_button = InlineKeyboardButton(text="🔙 Главное меню", callback_data='admin:main_menuu')
backk = InlineKeyboardMarkup(inline_keyboard=[[backk_button]])

spend_times = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text='1️⃣ Этот день', callback_data='admin:spends:1'),
     InlineKeyboardButton(text='Неделя 7️⃣', callback_data='admin:spends:7')],
    [InlineKeyboardButton(text='🗓 Месяц️', callback_data='admin:spends:30'),
     InlineKeyboardButton(text='Все время ♾️', callback_data='admin:spends:0')],
    [back_button]
])

utm_statistics_times = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text='1️⃣ Этот день', callback_data='admin:utm_stats:1'),
     InlineKeyboardButton(text='Неделя 7️⃣', callback_data='admin:utm_stats:7')],
    [InlineKeyboardButton(text='🗓 Месяц️', callback_data='admin:utm_stats:30'),
     InlineKeyboardButton(text='Все время ♾️', callback_data='admin:utm_stats:0')],
    [back_button]
])


def send_confirm(step: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Подтвердить ({step}/2)", callback_data=f"admin:send:confirm:{step}")],
        [InlineKeyboardButton(text="✖️ Отменить запуск", callback_data="admin:send:confirm:cancel")],
    ])


def send_buttons_builder(*, has_buttons: bool):
    action_button = (
        InlineKeyboardButton(text="➡️ Дальше", callback_data="admin:send:buttons:done")
        if has_buttons
        else InlineKeyboardButton(text="Пропустить кнопки", callback_data="admin:send:buttons:skip")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [action_button],
        [InlineKeyboardButton(text="✖️ Отменить запуск", callback_data="admin:send:confirm:cancel")],
    ])


def send_broadcast_buttons(buttons: list[tuple[str, str]]):
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, url=url)]
        for text, url in buttons[:100]
    ])


send_cancel = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⛔️ Остановить рассылку", callback_data="admin:send:cancel")]
])


def back_to_user(user_id: int): return InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👤 К пользователю", switch_inline_query_current_chat=f'search_user tg_id {user_id}')],
    [back_button]
])

def view_user_menu(user_id: int, carts_len: int, blocked: bool):
    if not blocked: block_button = InlineKeyboardButton(text="🔐 Заблокировать", callback_data=f'admin:users:{user_id}:block')
    else: block_button = InlineKeyboardButton(text="🔓 Разблокировать", callback_data=f'admin:users:{user_id}:unblock')
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🛍️ Заказы ({carts_len})", callback_data=f"admin:users:{user_id}:carts")],
        [block_button], [back_button]
    ])
