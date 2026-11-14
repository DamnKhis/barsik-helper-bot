from aiogram.fsm.state import State, StatesGroup


class UserStates(StatesGroup):
    in_dialog = State()
