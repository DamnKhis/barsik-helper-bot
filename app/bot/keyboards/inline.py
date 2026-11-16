from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def call_operator_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Вызвать оператора", callback_data=f"call_operator:{token}")]
        ]
    )


def answer_rating_keyboard() -> InlineKeyboardMarkup:
    """👍/👎 под ответом ИИ; id сообщения берётся из callback, не кодируется."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Полезно", callback_data="rate:up"),
                InlineKeyboardButton(text="👎 Не помогло", callback_data="rate:down"),
            ]
        ]
    )


def close_ticket_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение закрытия обращения самим пользователем."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, закрыть", callback_data="close_ticket:confirm"),
                InlineKeyboardButton(text="Отмена", callback_data="close_ticket:cancel"),
            ]
        ]
    )
