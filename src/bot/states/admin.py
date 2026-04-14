from aiogram.fsm.state import State, StatesGroup


class MainMenu(StatesGroup):
    spends_time = State()
    utm_statistics_time = State()

class ViewUser(StatesGroup):
    block_days = State()
