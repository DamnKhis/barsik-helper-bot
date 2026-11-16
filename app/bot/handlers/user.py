import logging
import mimetypes
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


async def _get_active_session(session: AsyncSession | None, user: User) -> JiraSession | None:
    if session is None:
        return None
    return await get_active_jira_session(session, user.telegram_id)


def _image_note(image_path: Path | None) -> str:
    """Строка про присланный скриншот для комментария в Jira.

    ``!файл.jpg!`` — wiki-разметка Jira: картинка рендерится прямо в комментарии,
    потому что файл уже приложен к задаче через ``_attach_saved_image``.
    """
    if image_path is None:
        return ""
    return f"\n\n📎 Скриншот от пользователя: !{image_path.name}!"


async def _attach_saved_image(jira_client: JiraClient, issue_key: str, image_path: Path) -> None:
    """Приложить сохранённый скриншот к задаче, если клиент это умеет."""
    if not image_path.is_file() or not hasattr(jira_client, "add_attachment"):
        return
    try:
        content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        await jira_client.add_attachment(
            issue_key,
            image_path.name,
            image_path.read_bytes(),
            content_type,
        )
    except Exception:
        logger.exception("Не удалось прикрепить скриншот %s к Jira %s", image_path.name, issue_key)


async def _forward_to_jira(
    message: Message,
    session: AsyncSession,
    user: User,
    redis_context: RedisContextService,
    jira_client: JiraClient,
    jira_session: JiraSession,
    question: str,
    image_path: Path | None = None,
) -> None:
    if image_path is not None:
        await _attach_saved_image(jira_client, jira_session.issue_key, image_path)

    try:
        comment_id = await jira_client.add_comment(
            jira_session.issue_key,
            format_user_comment(question, user) + _image_note(image_path),
        )
    except Exception:
        logger.exception("Не удалось отправить сообщение в Jira %s", jira_session.issue_key)
        await message.answer("Не удалось передать сообщение в поддержку. Попробуйте ещё раз.")
        return

    await set_jira_last_comment(session, jira_session, comment_id)
    await redis_context.append_message(user.telegram_id, "user", question)
    await message.answer("Сообщение передано в поддержку.")


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
    bot: Bot,
    jira_client: JiraClient | None = None,
) -> None:
    question = (message.text or message.caption or "").strip()

    # Ветка со скриншотом: картинку сохраняем (она нужна поддержке), vision-модель
    # опишет её, и дальше запрос идёт обычным путём — как текстовый.
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

    active = await _get_active_session(session, user)
    if active is not None and session is not None:
        if jira_client is None:
            await message.answer("Поддержка временно недоступна. Попробуйте позже.")
            return
        await _forward_to_jira(
            message, session, user, redis_context, jira_client, active, stored_question, image_path
        )
        await log_request(
            str(user.telegram_id),
            stored_question,
            0.0,
            "escalated",
            0,
            f"jira:{active.issue_key}",
            None,
        )
        return

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
        # Метрику пишем после отправки ответа; Jira-задачу создаст колбэк ниже.
        # Путь к скриншоту уходит в состояние: он попадёт в обращение поддержки.
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
    if session is not None:
        await create_dialog_entry(
            session,
            user_id=user.telegram_id,
            question=stored_question,
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
    # Присланные скриншоты лежат в data["operator_images"]: они уходят в задачу Jira
    # вместе с историей диалога (вложения в Jira — следующий шаг).
    images: list[str] = data.get("operator_images") or []
    if not question or data.get("operator_token") != token:
        await callback.answer("Этот запрос уже неактуален.", show_alert=True)
        return

    active = await get_active_jira_session(session, user.telegram_id)
    if active is not None:
        await state.update_data(operator_token=None, operator_question=None, operator_images=[])
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
    if images:
        transcript += "\n\n" + "\n".join(_image_note(Path(path)).strip() for path in images)
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
    for raw_path in images:
        await _attach_saved_image(jira_client, issue_key, Path(raw_path))

    try:
        transcript_comment_id = await jira_client.add_comment(
            issue_key,
            f"{BOT_TRANSCRIPT_PREFIX}\n{transcript}",
        )
    except Exception:
        logger.exception("Не удалось приложить историю диалога к задаче %s", issue_key)
    else:
        await set_jira_last_comment(session, jira_session, transcript_comment_id)
    await state.update_data(operator_token=None, operator_question=None, operator_images=[])
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
    bot: Bot,
    jira_client: JiraClient | None = None,
) -> None:
    question = (message.text or message.caption or "").strip()

    image_path: Path | None = None
    if is_image_message(message):
        try:
            image_path = await save_query_image(bot, message, user.telegram_id)
        except Exception as exc:
            logger.warning("Не удалось получить изображение от %s: %s", user.telegram_id, exc)

    active = await _get_active_session(session, user)
    if active is not None and (question or image_path is not None):
        if jira_client is None:
            await message.answer("Поддержка временно недоступна. Попробуйте позже.")
            return
        await _forward_to_jira(
            message,
            session,
            user,
            redis_context,
            jira_client,
            active,
            question or IMAGE_PLACEHOLDER,
            image_path,
        )
        return
    await message.answer(
        "Нажмите «Задать вопрос», чтобы начать диалог (текст или скриншот).",
        reply_markup=user_main_menu(),
    )
