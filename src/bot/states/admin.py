from aiogram.fsm.state import State, StatesGroup


class MainMenu(StatesGroup):
    spends_time = State()
    utm_statistics_time = State()

class ViewUser(StatesGroup):
    block_days = State()


class EditChannelMarkup(StatesGroup):
    waiting_for_forward = State()
    waiting_for_url = State()
    waiting_for_button_text = State()
