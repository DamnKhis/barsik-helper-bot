from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def call_operator_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Вызвать оператора", callback_data=f"call_operator:{token}")]
        ]
    )
