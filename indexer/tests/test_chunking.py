"""Проверка чанкинга: путь раздела, таблицы, нарезка длинных разделов (роль 1)."""
from indexer.chunking import MAX_CHUNK_CHARS, chunk_document
from indexer.parse_docx import Block


def test_chunks_keep_section_path():
    blocks = [
        Block("heading", "Командировки", level=1),
        Block("paragraph", "Заявка на командировку оформляется в модуле «Командировки»."),
        Block("heading", "Расчёт аванса", level=2),
        Block("paragraph", "Аванс считается по формуле: (Дата по − Дата с) × 700."),
    ]
    chunks = chunk_document("БП Командировка.docx", blocks)

    assert len(chunks) == 2
    assert chunks[0].section_path == "Командировки"
    assert chunks[1].section_path == "Командировки / Расчёт аванса"
    assert "× 700" in chunks[1].text
    assert chunks[1].text.startswith("Документ: БП Командировка.docx | Раздел: ")


def test_table_stays_with_its_section():
    blocks = [
        Block("heading", "Реестр мебели", level=1),
        Block("paragraph", "Статусы мебели:"),
        Block("table", "| Статус | Описание |\n| --- | --- |\n| занят | выдана сотруднику |"),
    ]
    chunks = chunk_document("Реестр мебели.docx", blocks)

    assert len(chunks) == 1
    assert "| занят |" in chunks[0].text


def test_long_section_is_split_with_header_on_each_part():
    long_text = "\n".join(f"Строка {i}: " + "текст " * 30 for i in range(120))
    blocks = [Block("heading", "Фильтры", level=1), Block("paragraph", long_text)]
    chunks = chunk_document("Функциональные возможности.docx", blocks)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= MAX_CHUNK_CHARS + 220 for chunk in chunks)
    assert all(chunk.section_path == "Фильтры" for chunk in chunks)
    assert all(chunk.text.startswith("Документ:") for chunk in chunks)
