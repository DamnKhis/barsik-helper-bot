"""Парсинг PDF в плоский список блоков. Роль 1. Основная библиотека — PyMuPDF (fitz).

Что PyMuPDF даёт и почему он основной:
- текст с координатами и шрифтами -> заголовки определяются по размеру шрифта
  и нумерации разделов («2.3 Аванс»), а не угадываются по пустым строкам;
- page.find_tables() -> таблицы превращаются в markdown и не разваливаются по
  чанкам (вопросы про статусы мебели работают без ручной разметки);
- картинки со страниц сохраняются на диск, подпись делает VLM (задайте
  VLM_MODEL в .env) либо вставляется плейсхолдер — см. make_vlm_captioner();
- сканированные PDF: страница без текста прогоняется через OCR
  (pytesseract + tesseract в системе), иначе пропускается с предупреждением.

Тяжёлая альтернатива для сложной вёрстки — docling (IBM): layout-модель даёт
лучший markdown, но тянет torch (гигабайты). Ставить только если PyMuPDF
не справится с реальными документами: pip install docling, затем заменить
парсер по образцу этого модуля — контракт (list[Block]) не меняется.
"""
from __future__ import annotations

import base64
import logging
import re
import shutil
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

from indexer.blocks import Block

logger = logging.getLogger(__name__)

# «2.3 Аванс», «1. Общие положения» — нумерация надёжнее шрифтовых эвристик
NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+\S")

try:  # OCR нужен только для сканированных PDF; tesseract ставится в систему отдельно
    import pytesseract
    from PIL import Image

    _OCR_AVAILABLE = shutil.which("tesseract") is not None
except ImportError:  # pragma: no cover — опциональная зависимость
    _OCR_AVAILABLE = False

try:  # запасной детектор таблиц: видит линии там, где find_tables не справляется
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None


class _PlumberTable:
    """Адаптер таблицы pdfplumber под интерфейс таблицы PyMuPDF (.bbox/.extract)."""

    def __init__(self, table):
        self.bbox = fitz.Rect(table.bbox)
        self._table = table

    def extract(self) -> list[list[str]]:
        return self._table.extract()


def _rows_to_markdown(rows: list[list[str]]) -> str:
    """Ячейки таблицы -> markdown-таблица (едет в чанк одним блоком)."""
    cleaned = [[" ".join((cell or "").split()) for cell in row] for row in rows]
    width = max(len(row) for row in cleaned)
    for row in cleaned:
        row += [""] * (width - len(row))
    lines = ["| " + " | ".join(row) + " |" for row in cleaned]
    if len(lines) >= 2:
        lines.insert(1, "|" + "|".join([" --- "] * width) + "|")
    return "\n".join(lines)


def _heading_level(text: str, size: float, body_size: float, size_rank: dict[float, int]) -> int | None:
    """Уровень заголовка или None, если строка — обычный абзац."""
    match = NUMBERED_RE.match(text)
    if match:
        return min(3, match.group(1).count(".") + 1)  # «2.3» -> уровень 2
    if size >= body_size * 1.15 and len(text) <= 80:
        return size_rank.get(size, 1)  # крупный шрифт без нумерации — титул/раздел
    return None


def _ocr_page(page) -> str | None:
    """OCR для сканированной страницы; None, если OCR недоступен."""
    if not _OCR_AVAILABLE:
        return None
    pixmap = page.get_pixmap(dpi=200)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    try:
        return pytesseract.image_to_string(image, lang="rus+eng")
    except Exception as exc:  # noqa: BLE001 — нет языкового пакета и т.п.
        logger.warning("OCR не сработал: %s", exc)
        return None


def parse_pdf(
    path: Path,
    vlm_caption=None,
    screenshots_dir: Path | None = None,
) -> list[Block]:
    """PDF -> блоки. vlm_caption: функция bytes->str (подпись скриншота)."""
    doc = fitz.open(str(path))

    # 1) Статистика шрифтов по документу: самый частый размер = «тело» текста
    sizes: Counter[float] = Counter()
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip():
                        sizes[round(span["size"], 1)] += len(span["text"].strip())
    if not sizes:
        logger.warning("%s: нет текстового слоя — сканированный PDF", path.name)
        return []

    body_size = sizes.most_common(1)[0][0]
    big_sizes = sorted((s for s in sizes if s >= body_size * 1.15), reverse=True)[:3]
    size_rank = {size: level + 1 for level, size in enumerate(big_sizes)}

    blocks: list[Block] = []
    ocr_warned = False
    plumber_doc = None  # pdfplumber открываем лениво — только если fitz не нашёл таблицы

    def _plumber_tables(page_index: int) -> list[_PlumberTable]:
        nonlocal plumber_doc
        if pdfplumber is None:
            return []
        if plumber_doc is None:
            plumber_doc = pdfplumber.open(str(path))
        return [_PlumberTable(t) for t in plumber_doc.pages[page_index].find_tables()]

    for page_no, page in enumerate(doc, start=1):
        page_dict = page.get_text("dict")
        tables = list(page.find_tables().tables)
        if not tables:  # фолбэк: у pdfplumber свой детектор линий, он надёжнее на редкой вёрстке
            tables = _plumber_tables(page_no - 1)
        table_boxes = [fitz.Rect(table.bbox) for table in tables]
        items: list[tuple[float, float, int, object]] = []  # (y0, x0, тип, данные)

        for block in page_dict["blocks"]:
            bbox = fitz.Rect(block["bbox"])
            if block["type"] == 0:
                for line in block["lines"]:
                    text = " ".join(
                        span["text"] for span in line["spans"] if span["text"].strip()
                    ).strip()
                    if not text:
                        continue
                    line_box = fitz.Rect(line["bbox"])
                    if any(line_box.intersects(tb) for tb in table_boxes):
                        continue  # строка таблицы — попадёт в markdown таблицы
                    size = max((span["size"] for span in line["spans"]), default=body_size)
                    items.append((line_box.y0, line_box.x0, 0, (text, round(size, 1))))
            elif block["type"] == 1:  # скриншот интерфейса
                if any(bbox.intersects(tb) for tb in table_boxes):
                    continue
                items.append((bbox.y0, bbox.x0, 1, block))

        for table, box in zip(tables, table_boxes):
            items.append((box.y0, box.x0, 2, table))
        items.sort(key=lambda item: (round(item[0]), item[1]))

        page_text_chars = sum(len(item[3][0]) for item in items if item[2] == 0)
        # OCR-ветка — только для действительно пустых страниц: если есть таблица,
        # её текст живёт в ячейках, и считать страницу «сканированной» нельзя
        if page_text_chars < 10 and not tables and not any(item[2] == 1 for item in items):
            ocr_text = _ocr_page(page)
            if ocr_text is None and not ocr_warned:
                logger.warning(
                    "%s, стр. %s: нет текста и OCR недоступен "
                    "(поставьте tesseract + pytesseract) — страница пропущена",
                    path.name,
                    page_no,
                )
                ocr_warned = True
            for chunk in filter(None, (s.strip() for s in (ocr_text or "").split("\n\n"))):
                blocks.append(Block("paragraph", " ".join(chunk.split())))
            continue

        for _, _, kind, payload in items:
            if kind == 0:  # текстовая строка
                text = " ".join(payload[0].split())  # \xa0 и кратные пробелы -> обычные
                size = payload[1]
                level = _heading_level(text, size, body_size, size_rank)
                if level:
                    blocks.append(Block("heading", text, level=level))
                else:
                    blocks.append(Block("paragraph", text))
            elif kind == 1:  # картинка-скриншот
                image_block = payload
                caption = ""
                try:
                    if vlm_caption:
                        caption = vlm_caption(image_block["image"]) or ""
                except Exception as exc:  # noqa: BLE001 — VLM не критичен для индексации
                    logger.warning("VLM-подпись не удалась: %s", exc)
                label = f"[Скриншот интерфейса, страница {page_no}"
                label += f": {caption}]" if caption else " — подпись не распознана]"
                if screenshots_dir:
                    stem = Path(path).stem
                    folder = screenshots_dir / stem
                    folder.mkdir(parents=True, exist_ok=True)
                    ext = image_block.get("ext", "png")
                    image_file = folder / f"page{page_no}_{len(blocks)}.{ext}"
                    image_file.write_bytes(image_block["image"])
                    label += f" Файл: {image_file}."
                blocks.append(Block("paragraph", label))
            else:  # таблица
                rows = payload.extract()
                markdown = _rows_to_markdown(rows)
                if markdown.strip("| -\n"):
                    blocks.append(Block("table", markdown))

    doc.close()
    if plumber_doc is not None:
        plumber_doc.close()
    return blocks


def make_vlm_captioner(base_url: str, api_key: str, model: str):
    """Фабрика подписчика скриншотов через OpenAI-совместимый VLM.

    Модель должна поддерживать картинки (GLM-4V, gpt-4o-mini, qwen-vl…).
    Возвращает функцию bytes->str; при ошибке вернёт пустую строку.
    """
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key or "not-set")
    prompt = (
        "Одной короткой фразой по-русски опиши, что на скриншоте интерфейса "
        "БАРС-Офис: названия кнопок, полей, разделов, важные надписи."
    )

    def caption(image_bytes: bytes) -> str:
        b64 = base64.b64encode(image_bytes).decode()
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            },
                        ],
                    }
                ],
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("VLM-подпись не удалась: %s", exc)
            return ""

    return caption
