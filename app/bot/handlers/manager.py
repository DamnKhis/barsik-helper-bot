from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.keyboards.inline import take_ticket_keyboard
from app.bot.keyboards.reply import BTN_OPEN_TICKETS, manager_menu
from app.bot.services.redis_context import RedisContextService
from app.bot.states import ManagerStates
from app.db import User, close_ticket, get_ticket, list_open_tickets, take_ticket

logger = logging.getLogger(__name__)

router = Router(name="manager")
router.message.filter(RoleFilter("manager"))
router.callback_query.filter(RoleFilter("manager"))


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Меню менеджера. Здесь можно принимать вопросы, на которые не ответил бот.",
        reply_markup=manager_menu(),
    )


@router.message(F.text == BTN_OPEN_TICKETS)
async def show_open_tickets(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    tickets = await list_open_tickets(session)
    if not tickets:
        await message.answer("Неразобранных тикетов нет.", reply_markup=manager_menu())
        return

    await message.answer(f"Неразобранных тикетов: {len(tickets)}")
    for ticket in tickets:
        await message.answer(
            f"Тикет #{ticket.id}\n"
            f"Пользователь: {ticket.user_id}\n"
            f"Вопрос:\n{ticket.question}",
            reply_markup=take_ticket_keyboard(ticket.id),
        )


@router.callback_query(F.data.startswith("take_ticket:"))
async def take_ticket_callback(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User,
) -> None:
    raw_id = (callback.data or "").removeprefix("take_ticket:")
    try:
        ticket_id = int(raw_id)
    except ValueError:
        await callback.answer("Некорректный тикет.", show_alert=True)
        return

    ticket = await get_ticket(session, ticket_id)
    if ticket is None:
        await callback.answer("Тикет не найден.", show_alert=True)
        return
    if ticket.ticket_status != "open":
        await callback.answer("Этот тикет уже принят или закрыт.", show_alert=True)
        return

    taken = await take_ticket(session, ticket_id, user.telegram_id)
    if not taken:
        await callback.answer("Тикет уже забрал другой менеджер.", show_alert=True)
        return

    await state.set_state(ManagerStates.waiting_for_answer)
    await state.update_data(ticket_id=ticket_id)

    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            f"Тикет #{ticket.id} в работе.\n\n"
            f"Вопрос пользователя:\n{ticket.question}\n\n"
            "Напишите ответ одним сообщением."
        )
    await callback.answer("Тикет принят")


@router.message(ManagerStates.waiting_for_answer)
async def send_answer(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    bot: Bot,
    redis_context: RedisContextService,
) -> None:
    answer = (message.text or "").strip()
    if not answer:
        await message.answer("Пожалуйста, отправьте ответ текстом.")
        return

    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    if not ticket_id:
        await state.clear()
        await message.answer("Тикет потерялся. Откройте список заново.", reply_markup=manager_menu())
        return

    ticket = await get_ticket(session, int(ticket_id))
    if ticket is None:
        await state.clear()
        await message.answer("Тикет не найден.", reply_markup=manager_menu())
        return
    if ticket.ticket_status != "in_progress" or ticket.manager_id != user.telegram_id:
        await state.clear()
        await message.answer("Тикет больше не закреплён за вами.", reply_markup=manager_menu())
        return

    closed = await close_ticket(session, ticket.id, user.telegram_id, answer)
    if not closed:
        await state.clear()
        await message.answer("Не удалось закрыть тикет.", reply_markup=manager_menu())
        return

    try:
        await bot.send_message(ticket.user_id, f"Ответ менеджера:\n{answer}")
    except TelegramBadRequest as exc:
        logger.warning("Не удалось отправить ответ пользователю %s: %s", ticket.user_id, exc)

    await redis_context.append_answer(ticket.user_id, answer)
    await state.clear()
    await message.answer(
        f"Ответ отправлен пользователю {ticket.user_id}. Тикет #{ticket.id} закрыт.",
        reply_markup=manager_menu(),
    )
