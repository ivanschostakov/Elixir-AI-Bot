from aiogram.fsm.state import State, StatesGroup

class Registration(StatesGroup):
    phone = State()

class CalculateClicks(StatesGroup):
    cartridge_volume = State()
    cartridge_amount = State()
    desired_dosage = State()

class CalculateDivisions(StatesGroup):
    vial_amount = State()
    desired_dosage = State()
    water_volume = State()

class Graph(StatesGroup):
    dosage = State()
    course_length_weeks = State()
    course_interval_days = State()

class Ai(StatesGroup):
    activate_code = State()
    verification_code = State()