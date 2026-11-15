#!/usr/bin/env python3
"""Оффлайн-проверка «вопрос со скриншотом» на реальном дистрибутиве.

Берём картинки из ``bars-kb/dist/<build>/images/`` (это те же скриншоты, что
проиндексированы), описываем каждую vision-моделью, собираем строку запроса
``build_query`` и ищем её в Qdrant. Считаем hit@1/hit@k: попал ли поиск в чанк,
из которого картинка была взята.

Запуск (из корня репозитория, чтобы подхватился .env):

    PYTHONPATH=bars-kb/src .venv/bin/python tools/check_image_query.py --limit 10

Без Qdrant (только vision + сборка запроса):

    ... tools/check_image_query.py --limit 3 --no-search
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXAMPLES_DIR = PROJECT_ROOT / "bars-kb" / "examples"
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

from export_paths import resolve_export_dir  # noqa: E402

from app.bot.services.vision import build_query, describe_image  # noqa: E402


def load_pairs(export_dir: Path, limit: int) -> list[tuple[str, Path]]:
    """[(chunk_id, путь к картинке)] из images.jsonl текущей сборки."""
    images_jsonl = export_dir / "images.jsonl"
    if not images_jsonl.is_file():
        raise SystemExit(f"нет {images_jsonl}")
    pairs: list[tuple[str, Path]] = []
    for line in images_jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        path = export_dir / str(row.get("path", ""))
        if path.is_file():
            pairs.append((str(row.get("chunk_id", "")), path))
    return pairs[:limit] if limit > 0 else pairs


def chunk_titles(export_dir: Path) -> dict[str, str]:
    """chunk_id → «документ, раздел» для человекочитаемого отчёта."""
    titles: dict[str, str] = {}
    with gzip.open(export_dir / "chunks.jsonl.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            payload = row.get("payload") or {}
            section = payload.get("section_path") or ""
            if isinstance(section, list):
                section = " > ".join(str(s) for s in section)
            titles[str(row.get("id"))] = f"{payload.get('doc_title', '?')}, {section}"
    return titles


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", type=Path, default=PROJECT_ROOT / "bars-kb" / "dist")
    parser.add_argument("--limit", type=int, default=10, help="сколько картинок проверить (0 — все)")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--no-search", action="store_true", help="только vision + сборка запроса")
    parser.add_argument("--print-query", action="store_true")
    parser.add_argument("--caption", default="", help="подпись пользователя к картинке")
    args = parser.parse_args()

    export_dir = resolve_export_dir(args.export)
    print(f"сборка: {export_dir.name}")
    pairs = load_pairs(export_dir, args.limit)
    print(f"картинок к проверке: {len(pairs)}")

    titles = chunk_titles(export_dir) if not args.no_search else {}

    kb = None
    if not args.no_search:
        from kb_client import KB  # noqa: PLC0415

        kb = KB(args.export)
        print(f"коллекция: {kb.collection} (Qdrant: {os.getenv('QDRANT_URL') or os.getenv('QDRANT_PATH') or 'embedded'})")
        print()

    ok1 = okk = 0
    for index, (chunk_id, path) in enumerate(pairs, start=1):
        understanding = describe_image(path, args.caption)
        query = build_query(args.caption, understanding)
        if args.print_query:
            print("─" * 70)
            print(query)
        status = "vision ✗" if not understanding.ok else "vision ✓"
        if kb is None:
            print(f"[{index}/{len(pairs)}] {path.name}: {status}, запрос {len(query)} символов")
            continue

        hits = kb.search(query, args.top_k, "hybrid")
        found = next((rank for rank, hit in enumerate(hits, start=1) if hit["id"] == chunk_id), None)
        if found == 1:
            ok1 += 1
        if found:
            okk += 1
        mark = f"hit@{found}" if found else "мимо"
        print(f"[{index}/{len(pairs)}] {path.name}: {status}, {mark} — ожидался «{titles.get(chunk_id, chunk_id)}»")
        if not found and hits:
            top = hits[0]
            payload = top["payload"] or {}
            print(f"            топ-1: {payload.get('doc_title', '?')} / {payload.get('section_path', '?')}")

    if kb is not None and pairs:
        print()
        print(f"hit@1: {ok1}/{len(pairs)} = {ok1 / len(pairs):.0%}")
        print(f"hit@{args.top_k}: {okk}/{len(pairs)} = {okk / len(pairs):.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
