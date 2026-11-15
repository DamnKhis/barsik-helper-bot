from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import kb_autosync
from app.bot.filters import RoleFilter
from app.bot.keyboards.reply import BTN_ADD_DOC, admin_menu
from app.bot.states import AdminStates
from app.config import settings

logger = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(RoleFilter("admin"))


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Админ-панель «БАРС-Офис».", reply_markup=admin_menu())


@router.message(F.text == BTN_ADD_DOC)
async def ask_for_document(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_doc)
    await message.answer("Пришлите документ файлом — добавлю его в базу знаний.")


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


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Используйте кнопки админ-панели.", reply_markup=admin_menu())
