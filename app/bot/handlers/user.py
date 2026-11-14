from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.keyboards.reply import (
    BTN_ASK,
    BTN_EXIT_DIALOG,
    BTN_REQUEST_MANAGER,
    user_dialog_menu,
    user_main_menu,
)
from app.bot.services.ai_stub import get_ai_answer
from app.bot.services.notifications import notify_admins_manager_request, notify_managers_new_ticket
from app.bot.services.redis_context import RedisContextService
from app.bot.states import UserStates
from app.db import User, create_dialog_entry

router = Router(name="user")
router.message.filter(RoleFilter("user"))


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    text = "Здравствуйте! Я бот поддержки «БАРС-Офис». Задайте вопрос — постараюсь ответить."
    if user.is_pending_manager:
        text += "\n\nЗаявка на права менеджера уже отправлена администратору."
    await message.answer(text, reply_markup=user_main_menu())


@router.message(Command("clear"))
async def clear_history(
    message: Message,
    user: User,
    redis_context: RedisContextService,
) -> None:
    await redis_context.clear_context(user.telegram_id)
    await message.answer("История диалога очищена.")


@router.message(F.text == BTN_ASK)
async def start_dialog(message: Message, state: FSMContext) -> None:
    await state.set_state(UserStates.in_dialog)
    await message.answer(
        "Напишите ваш вопрос одним сообщением.",
        reply_markup=user_dialog_menu(),
    )


@router.message(F.text == BTN_EXIT_DIALOG)
async def exit_dialog(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Вы вышли из диалога.", reply_markup=user_main_menu())


@router.message(F.text == BTN_REQUEST_MANAGER)
async def request_manager(
    message: Message,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    if user.is_pending_manager:
        await message.answer("Заявка уже отправлена. Дождитесь решения администратора.")
        return

    user.is_pending_manager = True
    await session.flush()
    await message.answer("Заявка на права менеджера отправлена администратору.")
    await notify_admins_manager_request(bot, session, user)


@router.message(UserStates.in_dialog)
async def handle_question(
    message: Message,
    session: AsyncSession,
    user: User,
    bot: Bot,
    redis_context: RedisContextService,
) -> None:
    question = (message.text or "").strip()
    if not question:
        await message.answer("Пожалуйста, отправьте вопрос текстом.")
        return

    context = await redis_context.get_context(user.telegram_id)
    known, answer = await get_ai_answer(user.telegram_id, question, context)

    if known:
        await message.answer(answer)
        await redis_context.append_pair(user.telegram_id, question, answer)
        await create_dialog_entry(
            session,
            user_id=user.telegram_id,
            question=question,
            answer=answer,
            answered_by="ai",
            ticket_status="none",
        )
        return

    system_answer = "Я не знаю ответ на этот вопрос. Передаю его менеджеру."
    await message.answer(f"{system_answer}\n\nМожете задать следующий вопрос.")
    await redis_context.append_pair(user.telegram_id, question, system_answer)
    ticket = await create_dialog_entry(
        session,
        user_id=user.telegram_id,
        question=question,
        answer=None,
        answered_by="none",
        ticket_status="open",
    )
    await notify_managers_new_ticket(bot, session, ticket, user)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Нажмите «Задать вопрос», чтобы начать диалог.",
        reply_markup=user_main_menu(),
    )
