import time
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.keyboards.inline import call_operator_keyboard
from app.bot.keyboards.reply import (
    BTN_ASK,
    BTN_EXIT_DIALOG,
    user_dialog_menu,
    user_main_menu,
)
from app.bot.services.ai_service import AIResult, AIService
from app.bot.services.notifications import notify_admins_manager_request, notify_managers_new_ticket
from app.bot.services.redis_context import RedisContextService
from app.bot.states import UserStates
from app.db import User, create_dialog_entry, log_request

router = Router(name="user")
router.message.filter(RoleFilter("user"))
router.callback_query.filter(RoleFilter("user"))


def _metric_outcome(result: AIResult) -> str:
    if result.need_operator or result.flag == "ESCALATE":
        return "escalated"
    if result.flag == "ANSWER":
        return "answered"
    return "not_found"


async def _save_metrics(
    user: User,
    question: str,
    result: AIResult,
    response_ms: int,
    msg_id: int | None,
) -> None:
    await log_request(
        str(user.telegram_id),
        question,
        result.confidence,
        _metric_outcome(result),
        response_ms,
        result.source or "",
        msg_id,
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Здравствуйте! Я бот поддержки «БАРС-Офис». Задайте вопрос — постараюсь ответить.",
        reply_markup=user_main_menu(),
    )


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


@router.message(Command("request_access"))
async def request_access(
    message: Message,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    if user.is_pending_manager:
        return

    user.is_pending_manager = True
    await session.flush()
    await notify_admins_manager_request(bot, session, user)


@router.message(UserStates.in_dialog)
async def handle_question(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    redis_context: RedisContextService,
    ai_service: AIService,
) -> None:
    question = (message.text or "").strip()
    if not question:
        await message.answer("Пожалуйста, отправьте вопрос текстом.")
        return

    context = await redis_context.get_context(user.telegram_id)
    started = time.perf_counter()
    result = await ai_service.answer(user.telegram_id, question, context)
    response_ms = int((time.perf_counter() - started) * 1000)
    await redis_context.append_pair(user.telegram_id, question, result.answer)

    if result.need_operator:
        # Уверенность ниже порога: ядро решило эскалировать.
        # Метрику пишем после отправки ответа; тикет создаст колбэк ниже.
        token = uuid4().hex[:12]
        await state.update_data(operator_token=token, operator_question=question)
        sent = await message.answer(
            result.answer,
            reply_markup=call_operator_keyboard(token),
        )
        await _save_metrics(user, question, result, response_ms, sent.message_id)
        return

    sent = await message.answer(result.answer)  # id сообщения — по нему привяжется 👍/👎
    await _save_metrics(user, question, result, response_ms, sent.message_id)
    await create_dialog_entry(
        session,
        user_id=user.telegram_id,
        question=question,
        answer=result.answer,
        answered_by="ai",
        ticket_status="none",
    )


@router.callback_query(F.data.startswith("call_operator:"))
async def call_operator(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    token = (callback.data or "").removeprefix("call_operator:")
    data = await state.get_data()
    question = data.get("operator_question")
    if not question or data.get("operator_token") != token:
        await callback.answer("Этот запрос уже неактуален.", show_alert=True)
        return

    ticket = await create_dialog_entry(
        session,
        user_id=user.telegram_id,
        question=question,
        answer=None,
        answered_by="none",
        ticket_status="open",
    )
    await notify_managers_new_ticket(bot, session, ticket, user)
    await state.update_data(operator_token=None, operator_question=None)

    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "Запрос передан оператору. Ответ придёт в этот чат.",
            reply_markup=user_dialog_menu(),
        )
    await callback.answer("Оператор вызван")


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Нажмите «Задать вопрос», чтобы начать диалог.",
        reply_markup=user_main_menu(),
    )
