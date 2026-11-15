from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from app.bot.keyboards.reply import menu_for_role
from pathlib import Path
import fitz  # PyMuPDF
from aiogram import F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, BufferedInputFile

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DOCS_DIR = PROJECT_ROOT / "bars-kb" / "docs" / "source"
FALLBACK_DOCS_DIR = PROJECT_ROOT / "data" / "docs"

PDF_MAP = {
    "mebel": ("Реестр мебели.pdf", "Реестр_мебели"),
    "bp": ("БП Командировка.pdf", "БП_Командировка"),
    "func": (
        "Функциональные_возможности_работы_с_БАРС_Офисом.pdf",
        "Функциональные_возможности_БО",
    ),
    "ext": ("bars_office_extended_kb.pdf", "Расширенная_БЗ_БАРС_Офис"),
}


def _resolve_pdf_path(filename: str) -> Path | None:
    for folder in (SOURCE_DOCS_DIR, FALLBACK_DOCS_DIR, PROJECT_ROOT):
        candidate = folder / filename
        if candidate.exists():
            return candidate
    return None


def _extract_pdf_page(pdf_path: Path, page_num: int) -> tuple[bytes, int]:
    with fitz.open(pdf_path) as src, fitz.open() as out:
        actual_page = max(1, min(page_num, len(src)))
        idx = actual_page - 1
        out.insert_pdf(src, from_page=idx, to_page=idx)
        return out.tobytes(), actual_page


@router.message(CommandStart(deep_link=True, magic=F.args.startswith("src_")))
async def handle_pdf_deep_link(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    parts = args.split("_", 2)  # src_<code>_<page>
    if len(parts) != 3 or not parts[2].isdigit():
        await message.answer("Некорректная ссылка на источник документа.")
        return

    _, code, page_str = parts
    page_num = int(page_str)
    doc_entry = PDF_MAP.get(code)
    if not doc_entry:
        await message.answer("Документ не найден в списке источников.")
        return

    raw_filename, clean_prefix = doc_entry
    pdf_path = _resolve_pdf_path(raw_filename)
    if not pdf_path:
        await message.answer(f"Файл документа «{raw_filename}» не найден на сервере.")
        return

    pdf_bytes, actual_page = _extract_pdf_page(pdf_path, page_num)
    out_filename = f"{clean_prefix}_стр_{actual_page}.pdf"

    await message.answer_document(
        BufferedInputFile(pdf_bytes, filename=out_filename),
        caption=f"📄 Выдержка из документа «{clean_prefix.replace('_', ' ')}» (стр. {actual_page})",
    )


router = Router(name="common")


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, role: str) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=menu_for_role(role))


@router.message(Command("help"))
async def help_command(message: Message, role: str) -> None:
    if role == "admin":
        text = "Админ-панель: добавьте документ или одобрите заявку менеджера."
    elif role == "manager":
        text = "Меню менеджера: откройте «Неразобранные тикеты» и примите вопрос в работу."
    else:
        text = "Нажмите «Задать вопрос», чтобы поговорить с ботом. /cancel — выйти из диалога."
    await message.answer(text, reply_markup=menu_for_role(role))


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(F.text == "")
async def empty_message(message: Message, role: str) -> None:
    await message.answer("Похоже, сообщение пустое.", reply_markup=menu_for_role(role))
