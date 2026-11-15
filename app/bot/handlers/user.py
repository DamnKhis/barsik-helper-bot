import logging
import time
from pathlib import Path
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
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
from app.bot.services.image_store import is_image_message, save_query_image
from app.bot.services.notifications import notify_admins_manager_request, notify_managers_new_ticket
from app.bot.services.redis_context import RedisContextService
from app.bot.states import UserStates
from app.db import User, create_dialog_entry, log_request

logger = logging.getLogger(__name__)

router = Router(name="user")
router.message.filter(RoleFilter("user"))
router.callback_query.filter(RoleFilter("user"))

# Лимит Telegram 4096 символов; оставляем запас под источник и подпись.
TELEGRAM_CHUNK = 3500
IMAGE_PLACEHOLDER = "«скриншот»"


async def _send_long(message: Message, text: str, **kwargs) -> Message:
    """Отправить ответ, разбив его по абзацам: Telegram режет сообщения > 4096.

    Возвращает последнее отправленное сообщение — к нему привязывается оценка.
    """
    blocks: list[str] = []
    for block in (text or "…").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        while len(block) > TELEGRAM_CHUNK:  # один абзац длиннее лимита — режем жёстко
            blocks.append(block[:TELEGRAM_CHUNK])
            block = block[TELEGRAM_CHUNK:]
        if block:
            blocks.append(block)

    chunks: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) + 2 > TELEGRAM_CHUNK:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)

    sent: Message | None = None
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        sent = await message.answer(chunk, **(kwargs if last else {}))
    assert sent is not None
    return sent


async def _send_kb_images(message: Message, images: list[str]) -> None:
    """Скриншоты из базы знаний к ответу (включается SEND_KB_IMAGES=1)."""
    for path in images:
        try:
            await message.answer_photo(FSInputFile(path))
        except Exception as exc:  # файла нет в контейнере / формат не поддержан
            logger.warning("Не удалось отправить скриншот из БЗ %s: %s", path, exc)


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
        "Здравствуйте! Я бот поддержки «БАРС-Офис». Задайте вопрос текстом или пришлите\n"
        "скриншот — постараюсь ответить.",
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
        "Напишите ваш вопрос текстом или пришлите скриншот (можно с подписью).",
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
    bot: Bot,
) -> None:
    question = (message.text or message.caption or "").strip()

    # Ветка со скриншотом: картинку сохраняем (она понадобится оператору),
    # vision-модель опишет её, и дальше запрос идёт обычным путём.
    image_path: Path | None = None
    if is_image_message(message):
        try:
            image_path = await save_query_image(bot, message, user.telegram_id)
        except Exception as exc:
            logger.warning("Не удалось получить изображение от %s: %s", user.telegram_id, exc)
            await message.answer(
                "Не получилось принять изображение. Отправьте его ещё раз или опишите вопрос текстом."
            )
            return

    if not question and image_path is None:
        await message.answer("Пожалуйста, отправьте вопрос текстом или скриншотом.")
        return

    stored_question = question or IMAGE_PLACEHOLDER
    if image_path is not None:
        await bot.send_chat_action(message.chat.id, "typing")

    context = await redis_context.get_context(user.telegram_id)
    started = time.perf_counter()
    result = await ai_service.answer(
        user.telegram_id,
        stored_question,
        context,
        image_path=image_path,
    )
    response_ms = int((time.perf_counter() - started) * 1000)
    await redis_context.append_pair(user.telegram_id, stored_question, result.answer)

    if result.need_operator:
        # Уверенность ниже порога: ядро решило эскалировать.
        # Метрику пишем после отправки ответа; тикет создаст колбэк ниже.
        # Путь к скриншоту уходит в состояние: оператор увидит его в тикете.
        token = uuid4().hex[:12]
        await state.update_data(
            operator_token=token,
            operator_question=stored_question,
            operator_images=[str(image_path)] if image_path else [],
        )
        sent = await _send_long(
            message,
            result.answer,
            reply_markup=call_operator_keyboard(token),
        )
        await _save_metrics(user, stored_question, result, response_ms, sent.message_id)
        return

    sent = await _send_long(message, result.answer)  # id сообщения — по нему привяжется 👍/👎
    await _save_metrics(user, stored_question, result, response_ms, sent.message_id)
    if result.images:
        await _send_kb_images(message, result.images)
    await create_dialog_entry(
        session,
        user_id=user.telegram_id,
        question=stored_question,
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
    # Присланные скриншоты лежат в data["operator_images"]: их отдаст оператору
    # тикет-часть (следующий шаг), здесь только передаём запрос дальше.
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
        "Нажмите «Задать вопрос», чтобы начать диалог (текст или скриншот).",
        reply_markup=user_main_menu(),
    )
