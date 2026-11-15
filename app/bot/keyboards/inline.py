from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def take_ticket_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Принять тикет", callback_data=f"take_ticket:{ticket_id}")]
        ]
    )


def approve_manager_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Одобрить", callback_data=f"approve_manager:{user_id}")]
        ]
    )


def call_operator_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Вызвать оператора", callback_data=f"call_operator:{token}")]
        ]
    )
