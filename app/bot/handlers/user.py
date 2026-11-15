import asyncio
import logging
import time
from io import BytesIO
from pathlib import Path
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
from app.bot.services.jira import (
    BOT_TRANSCRIPT_PREFIX,
    JiraClient,
    format_transcript,
    format_user_comment,
    format_user_label,
)
from app.bot.services.redis_context import RedisContextService
from app.bot.states import UserStates
from app.config import settings
from app.db import (
    JiraSession,
    User,
    create_dialog_entry,
    create_jira_session,
    get_active_jira_session,
    log_request,
    set_jira_last_comment,
)

logger = logging.getLogger(__name__)

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


async def _get_active_session(session: AsyncSession | None, user: User) -> JiraSession | None:
    if session is None:
        return None
    return await get_active_jira_session(session, user.telegram_id)


MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


async def _forward_text_to_jira(
    message: Message,
    session: AsyncSession,
    user: User,
    redis_context: RedisContextService,
    jira_client: JiraClient,
    jira_session: JiraSession,
    question: str,
) -> None:
    try:
        comment_id = await jira_client.add_comment(
            jira_session.issue_key,
            format_user_comment(question, user),
        )
    except Exception:
        logger.exception("Не удалось отправить сообщение в Jira %s", jira_session.issue_key)
        await message.answer("Не удалось передать сообщение в поддержку. Попробуйте ещё раз.")
        return

    await set_jira_last_comment(session, jira_session, comment_id)
    await redis_context.append_message(user.telegram_id, "user", question)
    await message.answer("Сообщение передано в поддержку.")


def _image_payload(message: Message) -> tuple[object | None, str, str]:
    """Вернуть (Telegram-файл, имя файла, MIME) для картинки в сообщении."""
    if message.photo:
        return message.photo[-1], f"photo_{message.message_id}.jpg", "image/jpeg"
    document = message.document
    if document is not None and (document.mime_type or "").startswith("image/"):
        name = Path((document.file_name or f"image_{message.message_id}").replace("\\", "/")).name
        if name in {"", ".", ".."}:
            name = f"image_{message.message_id}"
        return document, name, document.mime_type or "application/octet-stream"
    return None, "", ""


async def _download_telegram_file(bot: Bot, file_obj: object) -> bytes:
    """Скачать файл из Telegram с ретраями: сеть до api.telegram.org бывает нестабильной."""
    file_id = getattr(file_obj, "file_id", None)
    if not file_id:
        raise ValueError("У вложения нет file_id")

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            file = await bot.get_file(file_id, request_timeout=60)
            if not file.file_path:
                raise RuntimeError("Telegram не вернул путь к файлу")
            buffer = BytesIO()
            await bot.download_file(file.file_path, destination=buffer, timeout=60)
            return buffer.getvalue()
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Не удалось скачать файл из Telegram (попытка %s/3): %s", attempt + 1, exc
            )
            if attempt < 2:
                await asyncio.sleep(1 + attempt)
    raise last_exc or RuntimeError("Не удалось скачать файл из Telegram")


async def _forward_attachment_to_jira(
    message: Message,
    session: AsyncSession,
    user: User,
    redis_context: RedisContextService,
    jira_client: JiraClient,
    jira_session: JiraSession,
    bot: Bot | None,
) -> None:
    file_obj, filename, content_type = _image_payload(message)
    if file_obj is None:
        await message.answer("Поддерживаются текст и изображения.")
        return
    if bot is None:
        await message.answer("Не удалось обработать изображение. Попробуйте отправить текстом.")
        return

    file_size = getattr(file_obj, "file_size", None) or 0
    if file_size > MAX_ATTACHMENT_BYTES:
        await message.answer(
            f"Изображение больше {MAX_ATTACHMENT_BYTES // (1024 * 1024)} МБ. Отправьте поменьше."
        )
        return

    try:
        content = await _download_telegram_file(bot, file_obj)
    except Exception:
        logger.exception("Не удалось скачать изображение из Telegram")
        await message.answer("Не удалось скачать изображение. Попробуйте ещё раз.")
        return
    if not content:
        await message.answer("Изображение не загрузилось, попробуйте ещё раз.")
        return

    try:
        await jira_client.add_attachment(jira_session.issue_key, filename, content, content_type)
    except Exception:
        logger.exception("Не удалось прикрепить файл к Jira %s", jira_session.issue_key)
        await message.answer("Не удалось прикрепить изображение в поддержку. Попробуйте ещё раз.")
        return

    caption = (message.caption or "").strip()
    label = f"{caption}\n\n(вложение: {filename})" if caption else f"(вложение: {filename})"
    try:
        comment_id = await jira_client.add_comment(
            jira_session.issue_key,
            format_user_comment(label, user),
        )
    except Exception:
        logger.exception("Не удалось оставить комментарий к вложению %s", jira_session.issue_key)
        await message.answer("Изображение загружено, но комментарий не добавился.")
        return

    await set_jira_last_comment(session, jira_session, comment_id)
    context_line = f"[изображение: {filename}] {caption}".strip()
    await redis_context.append_message(user.telegram_id, "user", context_line)
    await message.answer("Изображение передано в поддержку.")


async def _forward_message_to_jira(
    message: Message,
    session: AsyncSession,
    user: User,
    redis_context: RedisContextService,
    jira_client: JiraClient,
    jira_session: JiraSession,
    bot: Bot | None = None,
) -> None:
    if message.photo or message.document:
        await _forward_attachment_to_jira(
            message, session, user, redis_context, jira_client, jira_session, bot
        )
        return
    question = (message.text or "").strip()
    if not question:
        await message.answer("Пришлите текст или изображение.")
        return
    await _forward_text_to_jira(message, session, user, redis_context, jira_client, jira_session, question)


async def _remove_operator_button(callback: CallbackQuery) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.message(CommandStart())
async def start(
    message: Message,
    state: FSMContext,
    session: AsyncSession | None = None,
    user: User | None = None,
) -> None:
    active = await _get_active_session(session, user) if user is not None else None
    if active is not None:
        await message.answer(
            f"У вас открыто обращение {active.issue_key} в поддержке. "
            "Сообщения из этого чата уходят в Jira.",
            reply_markup=user_dialog_menu(),
        )
        return
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
async def exit_dialog(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
) -> None:
    active = await _get_active_session(session, user)
    if active is not None:
        await message.answer(
            f"Обращение {active.issue_key} ещё открыто в Jira. "
            "Диалог завершится, когда поддержка закроет задачу.",
            reply_markup=user_dialog_menu(),
        )
        return
    await state.clear()
    await message.answer("Вы вышли из диалога.", reply_markup=user_main_menu())


@router.message(UserStates.in_dialog)
async def handle_question(
    message: Message,
    state: FSMContext,
    session: AsyncSession | None,
    user: User,
    redis_context: RedisContextService,
    ai_service: AIService,
    jira_client: JiraClient | None = None,
    bot: Bot | None = None,
) -> None:
    question = (message.text or "").strip()

    active = await _get_active_session(session, user)
    if active is not None and session is not None:
        if jira_client is None:
            await message.answer("Поддержка временно недоступна. Попробуйте позже.")
            return
        await _forward_message_to_jira(
            message, session, user, redis_context, jira_client, active, bot
        )
        if question:
            await log_request(
                str(user.telegram_id),
                question,
                0.0,
                "escalated",
                0,
                f"jira:{active.issue_key}",
                None,
            )
        return

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
        # Метрику пишем после отправки ответа; Jira-задачу создаст колбэк ниже.
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
    if session is not None:
        await create_dialog_entry(
            session,
            user_id=user.telegram_id,
            question=question,
            answer=result.answer,
            answered_by="ai",
        )


@router.callback_query(F.data.startswith("call_operator:"))
async def call_operator(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    redis_context: RedisContextService,
    jira_client: JiraClient,
) -> None:
    token = (callback.data or "").removeprefix("call_operator:")
    data = await state.get_data()
    question = data.get("operator_question")
    if not question or data.get("operator_token") != token:
        await callback.answer("Этот запрос уже неактуален.", show_alert=True)
        return

    active = await get_active_jira_session(session, user.telegram_id)
    if active is not None:
        await state.update_data(operator_token=None, operator_question=None)
        await _remove_operator_button(callback)
        await callback.answer("Обращение уже открыто в поддержке.", show_alert=True)
        return

    if not settings.jira_create_ready:
        logger.warning("Вызов оператора без настроенной Jira")
        await callback.answer(
            "Поддержка временно недоступна: Jira не настроена.",
            show_alert=True,
        )
        return

    context = await redis_context.get_context(user.telegram_id)
    transcript = format_transcript(context, user)
    summary = f"БАРС-Офис: {question[:200]}"
    description = (
        "Обращение из бота «БАРС-Офис».\n"
        f"{format_user_label(user)}\n"
        "Полная история диалога с ИИ — в первом комментарии к задаче."
    )
    try:
        issue_key, issue_url = await jira_client.create_issue(
            project_key=settings.jira_project_key,
            issue_type=settings.jira_issue_type,
            summary=summary,
            description=description,
        )
    except Exception:
        logger.exception("Не удалось создать задачу Jira для пользователя %s", user.telegram_id)
        await callback.answer(
            "Не удалось создать обращение в Jira. Попробуйте ещё раз.",
            show_alert=True,
        )
        return

    jira_session = await create_jira_session(session, user.telegram_id, issue_key, issue_url)
    try:
        transcript_comment_id = await jira_client.add_comment(
            issue_key,
            f"{BOT_TRANSCRIPT_PREFIX}\n{transcript}",
        )
    except Exception:
        logger.exception("Не удалось приложить историю диалога к задаче %s", issue_key)
    else:
        await set_jira_last_comment(session, jira_session, transcript_comment_id)
    await state.update_data(operator_token=None, operator_question=None)
    await _remove_operator_button(callback)

    if callback.message is not None:
        await callback.message.answer(
            f"Запрос передан в поддержку.\nЗадача Jira: {issue_key}\n{issue_url}",
            reply_markup=user_dialog_menu(),
        )
    await callback.answer("Обращение создано")


@router.message()
async def fallback(
    message: Message,
    session: AsyncSession,
    user: User,
    redis_context: RedisContextService,
    jira_client: JiraClient,
    bot: Bot | None = None,
) -> None:
    has_payload = bool(message.text or message.photo or message.document)
    active = await _get_active_session(session, user)
    if active is not None and has_payload:
        await _forward_message_to_jira(
            message, session, user, redis_context, jira_client, active, bot
        )
        return
    await message.answer(
        "Нажмите «Задать вопрос», чтобы начать диалог.",
        reply_markup=user_main_menu(),
    )
