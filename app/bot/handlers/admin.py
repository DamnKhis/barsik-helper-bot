from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import kb_autosync
from app.bot.filters import RoleFilter
from app.bot.keyboards.inline import approve_manager_keyboard
from app.bot.keyboards.reply import BTN_ADD_DOC, BTN_APPROVE_MANAGER, admin_menu
from app.bot.states import AdminStates
from app.config import settings
from app.db import approve_manager as approve_manager_db
from app.db import get_user, list_pending_managers

logger = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Админ-панель «БАРС-Офис».", reply_markup=admin_menu())


@router.message(F.text == BTN_ADD_DOC)
async def ask_for_document(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_doc)
    await message.answer("Пришлите документ файлом — сохраню его локально.")


@router.message(F.text == BTN_APPROVE_MANAGER)
async def show_pending_managers(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()
    pending = await list_pending_managers(session)
    if not pending:
        await message.answer("Заявок на права менеджера нет.", reply_markup=admin_menu())
        return

    await message.answer(f"Заявок на рассмотрении: {len(pending)}")
    for applicant in pending:
        name = applicant.full_name or applicant.username or str(applicant.telegram_id)
        await message.answer(
            f"Пользователь: {name}\n"
            f"ID: {applicant.telegram_id}\n"
            f"Username: @{applicant.username or '—'}",
            reply_markup=approve_manager_keyboard(applicant.telegram_id),
        )


@router.message(AdminStates.waiting_for_doc)
async def save_document(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.document is None:
        await message.answer("Нужно отправить именно файл (документ).")
        return

    original_name = message.document.file_name or f"document_{message.document.file_id}"
    safe_name = Path(original_name.replace("\\", "/")).name
    if safe_name in {"", ".", ".."}:
        safe_name = f"document_{message.document.file_id}"

    if not kb_autosync.is_supported_document(safe_name):
        await message.answer(
            "Поддерживаются PDF, DOCX, MD и TXT.\n"
            "Пришлите документ в одном из этих форматов."
        )
        return

    target_dir = Path(settings.uploaded_docs_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    if target_path.exists():
        target_path = target_dir / f"{target_path.stem}_{uuid4().hex[:8]}{target_path.suffix}"

    await bot.download(message.document, destination=target_path)

    try:
        kb_path = kb_autosync.stage_document(target_path, safe_name)
    except Exception as exc:
        logger.exception("Не удалось добавить документ %s в базу знаний", safe_name)
        await state.clear()
        await message.answer(
            f"Файл сохранён локально: {target_path.name}\n"
            f"Но добавить его в базу знаний не удалось: {exc}",
            reply_markup=admin_menu(),
        )
        return

    await state.clear()
    if kb_autosync.enabled():
        rebuild = (
            "Пересборка базы знаний запустится автоматически "
            f"в течение {kb_autosync.interval()} с."
        )
    else:
        rebuild = "Автообновление выключено (KB_AUTOSYNC=0): запустите bars-kb sync вручную."
    await message.answer(
        f"Документ добавлен в базу знаний: {kb_path.name}\n{rebuild}",
        reply_markup=admin_menu(),
    )


@router.callback_query(F.data.startswith("approve_manager:"))
async def approve_manager_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
) -> None:
    raw_id = (callback.data or "").removeprefix("approve_manager:")
    try:
        target_id = int(raw_id)
    except ValueError:
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return

    target = await get_user(session, target_id)
    if target is None:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    if target.role == "admin":
        await callback.answer("Этот пользователь уже администратор.", show_alert=True)
        return

    await approve_manager_db(session, target_id)
    try:
        await bot.send_message(
            target_id,
            "Администратор одобрил вашу заявку. Теперь вы менеджер: /start",
        )
    except TelegramBadRequest as exc:
        logger.warning("Не удалось уведомить пользователя %s: %s", target_id, exc)

    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            f"Пользователь {target_id} назначен менеджером.",
            reply_markup=admin_menu(),
        )
    await callback.answer("Готово")


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Используйте кнопки админ-панели.", reply_markup=admin_menu())
