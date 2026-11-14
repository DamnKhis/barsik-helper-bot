"""Уведомления менеджерам и админам (без сложной аналитики)."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.inline import approve_manager_keyboard, take_ticket_keyboard
from app.db import DialogHistory, User, list_by_role

logger = logging.getLogger(__name__)


def _display_name(user: User) -> str:
    if user.full_name:
        return user.full_name
    if user.username:
        return f"@{user.username}"
    return str(user.telegram_id)


async def notify_managers_new_ticket(
    bot: Bot,
    session: AsyncSession,
    ticket: DialogHistory,
    user: User,
) -> None:
    managers = await list_by_role(session, "manager")
    text = (
        f"Новый тикет #{ticket.id}\n"
        f"Пользователь: {_display_name(user)} ({user.telegram_id})\n\n"
        f"Вопрос:\n{ticket.question}"
    )
    for manager in managers:
        try:
            await bot.send_message(
                manager.telegram_id,
                text,
                reply_markup=take_ticket_keyboard(ticket.id),
            )
        except TelegramBadRequest as exc:
            logger.warning("Не удалось уведомить менеджера %s: %s", manager.telegram_id, exc)


async def notify_admins_manager_request(
    bot: Bot,
    session: AsyncSession,
    applicant: User,
) -> None:
    admins = await list_by_role(session, "admin")
    text = (
        "Заявка на права менеджера\n\n"
        f"Пользователь: {_display_name(applicant)} ({applicant.telegram_id})\n"
        f"Username: @{applicant.username or '—'}"
    )
    for admin in admins:
        try:
            await bot.send_message(
                admin.telegram_id,
                text,
                reply_markup=approve_manager_keyboard(applicant.telegram_id),
            )
        except TelegramBadRequest as exc:
            logger.warning("Не удалось уведомить админа %s: %s", admin.telegram_id, exc)
