from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app import kb_autosync
from app.bot.filters import RoleFilter
from app.bot.keyboards.reply import BTN_ADD_DOC, admin_menu
from app.bot.services import kb_status
from app.bot.states import AdminStates
from app.config import settings

logger = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(RoleFilter("admin"))

# Сколько ждать, пока загруженный документ появится в сборке (сек).
def confirm_timeout_sec() -> int:
    try:
        return max(5, int(os.getenv("KB_CONFIRM_TIMEOUT_SEC", "900") or 900))
    except ValueError:
        return 900


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Админ-панель «БАРС-Офис».", reply_markup=admin_menu())


@router.message(F.text == BTN_ADD_DOC)
async def ask_for_document(message: Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_doc)
    await message.answer("Пришлите документ файлом — добавлю его в базу знаний.")


async def _confirm_indexed(bot: Bot, chat_id: int, name: str) -> None:
    """Дождаться, пока загруженный документ появится в сборке, и отчитаться админу.

    Раньше админ получал «пересборка запустится в течение N с» и больше ничего:
    если пересборка падала (нет ключа, лежит Qdrant, watcher не стартовал), про
    это никто не узнавал, а вопросы по документу оставались без ответа.
    """
    interval = max(5, kb_autosync.interval())
    deadline = time.monotonic() + confirm_timeout_sec()
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        status = await asyncio.to_thread(kb_status.snapshot)
        if status is not None and status.has_document(name):
            try:
                await bot.send_message(
                    chat_id,
                    f"✅ Документ в базе знаний: {name}\n{status.short()}.\n"
                    f"Регламент можно спрашивать прямо сейчас.",
                    reply_markup=admin_menu(),
                )
            except Exception as exc:
                logger.warning("Не удалось сообщить админу об успешной сборке: %s", exc)
            return

    status = await asyncio.to_thread(kb_status.snapshot)
    if status is None:
        tail = "Пакета знаний ещё нет: проверьте, что bars-kb собран (bars-kb builds)."
    elif status.stale:
        tail = (
            f"Похоже, пересборка не прошла: папка с документами новее сборки {status.build_id}.\n"
            "Проверьте лог приложения (KB autosync) и что Qdrant поднят, "
            "либо запустите bars-kb sync вручную."
        )
    else:
        tail = (
            f"В сборке {status.build_id} этого документа нет, хотя исходники не менялись.\n"
            "Проверьте формат файла и запустите bars-kb sync вручную."
        )
    try:
        await bot.send_message(
            chat_id,
            f"⚠️ Документ «{name}» пока не попал в базу знаний.\n{tail}",
            reply_markup=admin_menu(),
        )
    except Exception as exc:
        logger.warning("Не удалось сообщить админу о неудаче пересборки: %s", exc)


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

    status = await asyncio.to_thread(kb_status.snapshot)
    lines = [f"Документ добавлен в базу знаний: {kb_path.name}"]
    if status is not None:
        lines.append(f"Текущая сборка — {status.short()}.")
        if status.has_document(kb_path.name):
            lines.append("Этот документ в сборке уже есть.")
        else:
            lines.append("Этого документа в сборке пока нет.")
    else:
        lines.append("Сборка ещё не найдена (bars-kb builds покажет, что есть).")

    if kb_autosync.enabled():
        lines.append(
            "Пересборка запустится автоматически в течение "
            f"{kb_autosync.interval()} с — сообщу, когда документ окажется в базе."
        )
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if chat_id is not None and (status is None or not status.has_document(kb_path.name)):
            asyncio.create_task(_confirm_indexed(bot, chat_id, kb_path.name))
    else:
        lines.append("Автообновление выключено (KB_AUTOSYNC=0): запустите bars-kb sync вручную.")

    await message.answer("\n".join(lines), reply_markup=admin_menu())


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Используйте кнопки админ-панели.", reply_markup=admin_menu())
