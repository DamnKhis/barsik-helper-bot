from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_ASK = "Задать вопрос"
BTN_REQUEST_MANAGER = "Запросить права менеджера"
BTN_EXIT_DIALOG = "Выйти из диалога"

BTN_OPEN_TICKETS = "Неразобранные тикеты"
BTN_ADD_DOC = "Добавить доку"
BTN_APPROVE_MANAGER = "Одобрить менеджера"


def _keyboard(*rows: tuple[str, ...]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
    )


def user_main_menu() -> ReplyKeyboardMarkup:
    return _keyboard((BTN_ASK,), (BTN_REQUEST_MANAGER,))


def user_dialog_menu() -> ReplyKeyboardMarkup:
    return _keyboard((BTN_EXIT_DIALOG,))


def manager_menu() -> ReplyKeyboardMarkup:
    return _keyboard((BTN_OPEN_TICKETS,))


def admin_menu() -> ReplyKeyboardMarkup:
    return _keyboard((BTN_ADD_DOC,), (BTN_APPROVE_MANAGER,))


def menu_for_role(role: str) -> ReplyKeyboardMarkup:
    if role == "admin":
        return admin_menu()
    if role == "manager":
        return manager_menu()
    return user_main_menu()
