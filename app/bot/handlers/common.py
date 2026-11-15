from pathlib import Path
import fitz  # PyMuPDF
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, Message
from app.bot.keyboards.reply import menu_for_role

router = Router(name="common")

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


def _extract_pdf_pages(pdf_path: Path, page_nums: list[int]) -> tuple[bytes, list[int]]:
    with fitz.open(pdf_path) as src, fitz.open() as out:
        total_pages = len(src)
        actual_pages: list[int] = []
        for p in page_nums:
            clamped = max(1, min(p, total_pages))
            if clamped not in actual_pages:
                actual_pages.append(clamped)

        if not actual_pages:
            actual_pages = [1]
        else:
            actual_pages.sort()

        for p in actual_pages:
            idx = p - 1
            out.insert_pdf(src, from_page=idx, to_page=idx)

        return out.tobytes(), actual_pages


@router.message(CommandStart(deep_link=True))
async def handle_pdf_deep_link(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    if not args.startswith("src_"):
        await message.answer(
            "Здравствуйте! Я бот поддержки «БАРС-Офис». Задайте вопрос — постараюсь ответить."
        )
        return

    # Поддержка ссылок на одну или несколько страниц: src_<code>_<p1>_<p2>...
    parts = args.split("_")
    if len(parts) < 3:
        await message.answer("Некорректная ссылка на источник документа.")
        return

    code = parts[1]
    page_strs = parts[2:]
    if not all(p.isdigit() for p in page_strs):
        await message.answer("Некорректная ссылка на источник документа.")
        return

    page_nums = [int(p) for p in page_strs]
    doc_entry = PDF_MAP.get(code)
    if not doc_entry:
        await message.answer("Документ не найден в списке источников.")
        return

    raw_filename, clean_prefix = doc_entry
    pdf_path = _resolve_pdf_path(raw_filename)
    if not pdf_path:
        await message.answer(f"Файл документа «{raw_filename}» не найден на сервере.")
        return

    pdf_bytes, actual_pages = _extract_pdf_pages(pdf_path, page_nums)
    pages_file_suffix = "_".join(str(p) for p in actual_pages)
    pages_caption = ", ".join(str(p) for p in actual_pages)
    out_filename = f"{clean_prefix}_стр_{pages_file_suffix}.pdf"

    await message.answer_document(
        BufferedInputFile(pdf_bytes, filename=out_filename),
        caption=f"📄 Выдержка из документа «{clean_prefix.replace('_', ' ')}» (стр. {pages_caption})",
    )


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
