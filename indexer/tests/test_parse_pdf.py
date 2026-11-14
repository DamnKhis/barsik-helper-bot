"""Проверка PDF-парсера (роль 1): заголовки, абзацы, таблицы, диспетчер, чанки.

Тестовые PDF генерируются PyMuPDF и LibreOffice (реалистичный docx->pdf) —
внешних файлов не нужно.
"""
import shutil
import subprocess
from pathlib import Path

import fitz
import pytest

from indexer.blocks import parse_file
from indexer.chunking import chunk_document
from indexer.parse_pdf import _rows_to_markdown, parse_pdf

pytest.importorskip("fitz")

# base-14 шрифты PDF не умеют кириллицу — берём системный Noto (есть в Docker-образе и CI)
FONT_PATH = "/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf"


def _insert(page, x: float, y: float, text: str, size: float) -> None:
    if Path(FONT_PATH).exists():
        page.insert_font(fontname="noto", fontfile=FONT_PATH)
        page.insert_text((x, y), text, fontsize=size, fontname="noto")
    else:  # чуть меньше покрытия, но структуру проверяет
        page.insert_text((x, y), text.encode("ascii", "ignore").decode() or "x",
                         fontsize=size, fontname="helv")


def build_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    _insert(page, 72, 90, "БП Командировка", 18)
    _insert(page, 72, 130, "1. Общие положения", 12)
    _insert(page, 72, 150, "Командировка оформляется заявкой в модуле.", 10)
    _insert(page, 72, 190, "1.1 Расчёт аванса", 12)
    _insert(page, 72, 210, "Аванс считается как (Дата по - Дата с) * 700.", 10)
    doc.save(str(path))


def test_parse_pdf_finds_numbered_headings(tmp_path):
    pdf = tmp_path / "bp_komandirovka.pdf"
    build_pdf(pdf)
    blocks = parse_pdf(pdf)

    headings = [(b.text, b.level) for b in blocks if b.type == "heading"]
    assert ("1. Общие положения", 1) in headings
    assert ("1.1 Расчёт аванса", 2) in headings
    paragraphs = [b.text for b in blocks if b.type == "paragraph"]
    assert any("(Дата по" in p for p in paragraphs)


def test_parse_file_dispatches_pdf(tmp_path):
    pdf = tmp_path / "doc.pdf"
    build_pdf(pdf)
    blocks = parse_file(pdf)
    assert blocks and blocks[0].type in {"heading", "paragraph"}


def test_pdf_chunks_have_section_paths(tmp_path):
    pdf = tmp_path / "doc.pdf"
    build_pdf(pdf)
    chunks = chunk_document("doc.pdf", parse_pdf(pdf))
    paths = [c.section_path for c in chunks]
    assert "1. Общие положения" in paths
    assert "1. Общие положения / 1.1 Расчёт аванса" in paths
    assert all(c.text.startswith("Документ: doc.pdf | Раздел: ") for c in chunks)


def test_table_from_drawn_grid_becomes_table_block(tmp_path):
    """Таблица из сетки линий с текстом (base-14, как в реальных экспортах) -> блок type='table'."""
    pdf = tmp_path / "table.pdf"
    doc = fitz.open()
    page = doc.new_page()
    x0, y0, width, row_h = 72, 100, 150, 20
    for i in range(3):  # горизонтальные линии сетки
        y = y0 + i * row_h
        page.draw_line(fitz.Point(x0, y), fitz.Point(x0 + 2 * width, y))
    for i in range(3):  # вертикальные
        x = x0 + i * width
        page.draw_line(fitz.Point(x, y0), fitz.Point(x, y0 + 2 * row_h))
    cells = [["Status", "Opisanie"], ["zanyat", "vydana"]]
    for r, row in enumerate(cells):
        for c, text in enumerate(row):
            page.insert_text((x0 + c * width + 4, y0 + r * row_h + 14), text,
                             fontsize=9, fontname="helv")
    doc.save(str(pdf))

    blocks = parse_pdf(pdf)
    tables = [b for b in blocks if b.type == "table"]
    assert tables and "zanyat" in tables[0].text and "vydana" in tables[0].text


def test_real_docx_exported_pdf(tmp_path):
    """Реалистичный сценарий: .docx -> PDF (LibreOffice) -> парсер: заголовок+таблица+текст."""
    try:
        import docx as docx_lib
    except ImportError:
        pytest.skip("python-docx не установлен")
    if shutil.which("soffice") is None:
        pytest.skip("soffice недоступен")

    document = docx_lib.Document()
    document.add_heading("Реестр мебели", level=1)
    document.add_paragraph("Реестр хранит статусы мебели на складе.")
    table = document.add_table(rows=2, cols=2)
    table.style = "Table Grid"
    table.cell(0, 0).text = "Статус"
    table.cell(0, 1).text = "Описание"
    table.cell(1, 0).text = "занят"
    table.cell(1, 1).text = "выдана сотруднику"
    docx_path = tmp_path / "reestr.docx"
    document.save(str(docx_path))

    subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(docx_path)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    pdf_path = tmp_path / "reestr.pdf"
    assert pdf_path.exists()

    blocks = parse_pdf(pdf_path)
    headings = [b.text for b in blocks if b.type == "heading"]
    assert any("Реестр мебели" in h for h in headings)
    assert any("Реестр хранит статусы" in b.text for b in blocks if b.type == "paragraph")
    tables = [b for b in blocks if b.type == "table"]
    assert tables and "занят" in tables[0].text and "выдана сотруднику" in tables[0].text

    chunks = chunk_document("reestr.pdf", blocks)
    assert chunks and any("занят" in c.text for c in chunks)


def test_rows_to_markdown_normalizes_cells():
    md = _rows_to_markdown([["Статус", "Описание"], ["занят", "  выдана   сотруднику "], ["списан", None]])
    lines = md.splitlines()
    assert lines[1] == "| --- | --- |"
    assert "| занят | выдана сотруднику |" in lines
    assert "| списан |  |" in lines  # пустая ячейка не рушит таблицу