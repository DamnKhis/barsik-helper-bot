# BARS KB — ingestion & Qdrant dataset

Наша часть проекта: превращаем документацию БАРС (PDF/DOCX/MD) в чистый, структурированный
датасет в Qdrant и отдаём его следующему разработчику. Мы **не делаем** API для бота,
генерацию ответов и Telegram-интеграцию.

## Что уже работает

- парсинг DOCX (заголовки, списки, таблицы, изображения) и PDF (текст, изображения, чистка футера);
- очистка мусора Word/PDF (`Ошибка! Используйте вкладку...`, колонтитулы, оглавление);
- чанкинг parent/child с breadcrumb;
- типы чанков: `text`, `faq`, `table`, `diagram`, `screenshot`;
- FAQ-слой по ключевым тест-кейсам (только из текста документов);
- обогащение текста, таблиц и скриншотов — **Qwen3.8-Flash** (`qwen3.8-flash`) по API;
- эмбеддинги: hash-lexical для работы без ключей, Qwen `text-embedding-v4` при наличии ключа
  (**согласованная модель для финальной сборки: dim 1024**);
- Qdrant: dense + sparse BM25, alias `bars_kb_current`;
- экспорт `chunks.jsonl.gz` с векторами, `documents.jsonl`, `manifest.json`, `images/`;
- переэмбеддинг готового экспорта чужой моделью (`examples/reembed_from_export.py`);
- offline-eval по golden set;
- локальная синхронизация и заготовка GitHub-синхронизации.

## Быстрый старт

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env

# без ключей — mock/hash режим
.venv/bin/bars-kb ingest
.venv/bin/bars-kb eval
.venv/bin/bars-kb status

# когда появится ключ
# .env: QWEN_API_KEY=...
# Qwen3.8-Flash — текст/JSON/скриншоты; text-embedding-v4 — векторы
.venv/bin/bars-kb ingest
```

То же через `make`: `make install`, `make ingest`, `make eval`, `make test`,
`make restore` (нужен `QDRANT_URL`), `make query`.

## Qdrant-сервер

Локально по умолчанию используется embedded Qdrant (`data/qdrant`). Для сервера:

```bash
docker compose up -d
# .env: QDRANT_URL=http://127.0.0.1:6333
```

## Команды

| Команда | Что делает |
|---|---|
| `bars-kb ingest` | собрать базу из `docs/source` |
| `bars-kb ingest --source data/fixtures` | собрать из тестовых DOCX |
| `bars-kb sync` | локальная синхронизация + пересбор |
| `bars-kb sync --github` | скачать `GITHUB_REPO` и пересобрать |
| `bars-kb eval --top-k 8` | прогнать golden set, записать `data/eval_report.json` |
| `bars-kb fixtures` | сгенерировать DOCX-фикстуры |
| `bars-kb status` | показать последний manifest |

## Структура

```text
src/bars_kb/
  parsers/      DOCX/PDF/MD → AST
  enrich/       текст, таблицы, изображения, FAQ
  embeddings/   hash + Qwen
  store/        Qdrant
  pipeline.py   сборка и экспорт
  eval/         offline retrieval eval
data/
  fixtures/     сгенерированные DOCX
  images/       извлечённые изображения
  qdrant/       локальный embedded Qdrant
docs/
  source/       исходные документы для базы знаний
  context/      бриф и критерии (не в базе знаний)
dist/<build>/   handoff-пакет
HANDOFF_FILES.md  что именно отдаём: список файлов и их назначение
PROJECT_FILES.md  карта модулей: кто за что отвечает и что делает
examples/       restore + query + reembed примеры
presentation/   слайды защиты (Marp)
tests/          unit-тесты (unittest)
Makefile        короткие команды
docker-compose.yml  Qdrant-сервер
```

## Режимы без ключей

`LLM_PROVIDER=qwen`, `VISION_PROVIDER=qwen`, `EMBEDDING_PROVIDER=qwen`, но `QWEN_API_KEY` пустой —
пайплайн автоматически падает в mock/hash и работает полностью. Описания изображений и
обогащение текста будут заглушками — это нормально до появления ключа. После ключа достаточно
заполнить один `QWEN_API_KEY` в `.env` и выполнить `bars-kb ingest`.

Для жёсткого mock-режима можно явно поставить `LLM_PROVIDER=mock`, `VISION_PROVIDER=mock`,
`EMBEDDING_PROVIDER=hash`.

## Ограничения текущего прогона

- PDF-таблицы не парсятся структурно (Word-экспорт теряет границы колонок); DOCX-таблицы парсятся.
- Без vision-ключа скриншоты получают только контекстное описание, а не OCR/разметку выносок.
- Snapshot Qdrant в embedded-режиме недоступен; основной формат передачи — `chunks.jsonl.gz` + restore-скрипт.

Подробности передачи — в [HANDOFF.md](HANDOFF.md), полная карта файлов и зон
ответственности — в [PROJECT_FILES.md](PROJECT_FILES.md).
