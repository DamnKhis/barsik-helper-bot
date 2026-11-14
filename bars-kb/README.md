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

# когда появятся ключи
# LLM_PROVIDER=deepseek, VISION_PROVIDER=qwen, EMBEDDING_PROVIDER=qwen
# заполнить DEEPSEEK_API_KEY / QWEN_API_KEY
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
examples/       restore + query + reembed примеры
presentation/   слайды защиты (Marp)
tests/          unit-тесты (unittest)
Makefile        короткие команды
docker-compose.yml  Qdrant-сервер
```

## Режимы без ключей

`LLM_PROVIDER=mock`, `VISION_PROVIDER=mock`, `EMBEDDING_PROVIDER=hash`.
Пайплайн, Qdrant, экспорт и eval работают полностью. Описания изображений и обогащение
текста будут заглушками — это нормально до появления ключей. После ключей достаточно
поменять `.env` и выполнить `bars-kb ingest`.

## Ограничения текущего прогона

- PDF-таблицы не парсятся структурно (Word-экспорт теряет границы колонок); DOCX-таблицы парсятся.
- Без vision-ключа скриншоты получают только контекстное описание, а не OCR/разметку выносок.
- Snapshot Qdrant в embedded-режиме недоступен; основной формат передачи — `chunks.jsonl.gz` + restore-скрипт.

Подробности передачи — в [HANDOFF.md](HANDOFF.md).
