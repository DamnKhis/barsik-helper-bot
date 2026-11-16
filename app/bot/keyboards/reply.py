from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_ASK = "Задать вопрос"
BTN_EXIT_DIALOG = "Выйти из диалога"

BTN_ADD_DOC = "Добавить доку"
BTN_DELETE_DOC = "Удалить доку из БЗ"


def _keyboard(*rows: tuple[str, ...]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
    )


def user_main_menu() -> ReplyKeyboardMarkup:
    return _keyboard((BTN_ASK,))


def user_dialog_menu() -> ReplyKeyboardMarkup:
    return _keyboard((BTN_EXIT_DIALOG,))


def admin_menu() -> ReplyKeyboardMarkup:
    # Кнопка удаления пока без обработчика: добавим позже.
    return _keyboard((BTN_ADD_DOC,), (BTN_DELETE_DOC,))


def menu_for_role(role: str) -> ReplyKeyboardMarkup:
    if role == "admin":
        return admin_menu()
    return user_main_menu()
