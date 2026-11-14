from aiogram.fsm.state import State, StatesGroup


class ManagerStates(StatesGroup):
    waiting_for_answer = State()
