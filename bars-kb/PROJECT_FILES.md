# Карта проекта: кто за что отвечает и что делает каждый файл

Наша часть — **ingestion + Qdrant-датасет**. Ниже — зоны ответственности и полное
описание файлов. «Владелец» — зона внутри нашей части, а не человек: если распределять
по команде, эти зоны можно закрепить за конкретными людьми.

## 1. Зоны ответственности

| Зона | Модули | Что делает | Результат |
|---|---|---|---|
| **Parsing / Ingestion** | `parsers/`, `models.py`, `pipeline.py` | читает PDF/DOCX/MD/TXT, чистит мусор, строит дерево разделов | `Document` с секциями, таблицами, картинками |
| **Chunking** | `chunker.py` | режет разделы на parent/child-чанки, добавляет breadcrumb, типы, ID | список `Chunk` |
| **Enrichment** | `enrich/`, `providers/qwen.py` | Qwen3.8-Flash: summary, вопросы, сущности, OCR скриншотов, схема процесса | обогащённый текст и payload |
| **Embeddings / Search** | `embeddings/`, `store/` | dense `qwen/qwen3-embedding-8b` + sparse BM25, запись и поиск в Qdrant | коллекция Qdrant |
| **Export / Handoff** | `pipeline.py` (export), `checksums.py`, `examples/` | собирает `dist/`, README пакета, checksums, restore/query | переносимый пакет |
| **Evaluation / QA** | `eval/`, `tests/`, `data/golden_retrieval.jsonl` | offline-метрики hit@k/MRR, unit-тесты | `eval_report.json`, отчёт тестов |
| **Config / CLI / Infra** | `config.py`, `cli.py`, `Makefile`, `docker-compose.yml` | `.env`, команды, Qdrant-сервер, запуск | воспроизводимый запуск |
| **Docs / Presentation** | `docs/`, `README.md`, `HANDOFF*.md`, `presentation/` | документация, контракт передачи, слайды | понятный handoff |

## 2. Поток данных

```text
docs/source/*.pdf
   │  parsers/ + clean.py
   ▼
Document (models.py: Section, Block)
   │  chunker.py + enrich/ + providers/qwen.py
   ▼
Chunk (text / table / image / faq)
   │  embeddings/ + store/qdrant_store.py
   ▼
Qdrant: bars_kb_<build_id> → bars_kb_current
   │  pipeline.export_build + checksums.py
   ▼
dist/<build_id>/  → следующий разработчик
```

## 3. Корневые файлы

| Файл | Что делает | Владелец | Кому |
|---|---|---|---|
| `.env.example` | шаблон конфигурации: один `QWEN_API_KEY`, модели, Qdrant, GitHub | Config | все |
| `.gitignore` | исключает `.venv`, `.env`, `data/qdrant`, `data/cache`, `data/images`, `data/builds`; `dist/` хранится намеренно | Infra | все |
| `pyproject.toml` | пакет `bars-kb`, зависимости `qdrant-client` и `python-docx`, entry point CLI | Infra | все |
| `Makefile` | короткие команды: `install`, `ingest`, `eval`, `test`, `restore`, `query`, `clean` | Infra | все |
| `docker-compose.yml` | Qdrant-сервер на портах 6333/6334 с volume | Infra | следующий разработчик |
| `README.md` | вход в проект: запуск, команды, структура, ограничения | Docs | все |
| `HANDOFF.md` | контракт передачи: схема payload, restore, обновление, модели | Docs / Handoff | следующий разработчик |
| `HANDOFF_FILES.md` | краткий список того, что отдаём, и зачем | Docs / Handoff | следующий разработчик |
| `PROJECT_FILES.md` | этот файл: полная карта модулей и файлов | Docs | команда |

## 4. `src/bars_kb/` — код пайплайна

| Файл | Что делает | Владелец |
|---|---|---|
| `__init__.py` | версия пакета | Infra |
| `config.py` | `Settings`, загрузка `.env`, дефолты, флаги mock/hash, один Qwen-ключ | Config |
| `models.py` | dataclass-модели `Block`, `Section`, `Document`, `Chunk`; `payload()`; `table_to_markdown()` | Parsing |
| `parsers/__init__.py` | выбор парсера по расширению, `SUPPORTED` | Parsing |
| `parsers/clean.py` | удаление Word/PDF-футеров, колонтитулов, строк оглавления | Parsing |
| `parsers/pdf_parser.py` | `pdftotext -layout`, страницы, заголовки, списки, извлечение картинок через `pdfimages`; PDF-таблицы намеренно не структурируются | Parsing |
| `parsers/docx_parser.py` | `python-docx`: заголовки/стили, списки, таблицы, изображения, сборка секций | Parsing |
| `parsers/markdown_parser.py` | заголовки, таблицы, списки, изображения из Markdown | Parsing |
| `parsers/text_parser.py` | простой текст → абзацы | Parsing |
| `chunker.py` | parent/child-чанки; text/table/image/FAQ; breadcrumb; стабильные UUID; parent_text; image_path | Chunking |
| `enrich/__init__.py` | пакет обогащения | Enrichment |
| `enrich/cache.py` | дисковый JSON-кэш по sha256-ключу, чтобы не платить за повторные вызовы | Enrichment |
| `enrich/text.py` | промпт к Qwen3.8-Flash: summary, вопросы, сущности, шаги; fallback без ключа | Enrichment |
| `enrich/tables.py` | описание таблицы и вопросы к ней | Enrichment |
| `enrich/images.py` | промпты для скриншотов и схем: kind, description, OCR, UI-элементы, nodes/edges, Mermaid; fallback | Enrichment |
| `enrich/faq.py` | FAQ-чанки по сложным тест-кейсам, строго из текста документов | Enrichment |
| `providers/__init__.py` | фабрики `build_llm()` и `build_vision()`: Qwen или `None` | Enrichment |
| `providers/base.py` | протоколы `LLMProvider`, `VisionProvider`, `EmbeddingProvider` | Enrichment |
| `providers/http.py` | HTTP POST с ретраями и извлечением JSON из ответа модели | Enrichment |
| `providers/qwen.py` | `QwenChat`: `qwen/qwen3.8-flash` для текста/JSON/картинок; `QwenEmbeddings`: `qwen/qwen3-embedding-8b` | Enrichment / Search |
| `providers/mock.py` | пустые mock-реализации для тестов | Enrichment |
| `embeddings/__init__.py` | фабрика embedder: Qwen или hash-fallback | Search |
| `embeddings/hash_embedder.py` | детерминированные лексические векторы для работы без ключа | Search |
| `embeddings/sparse.py` | словарь BM25 и sparse-кодирование текста | Search |
| `store/__init__.py` | пакет хранилища | Search |
| `store/qdrant_store.py` | embedded/server Qdrant, создание коллекции, upsert, dense/sparse/hybrid RRF, alias `current`/`prev`, snapshot, scroll | Search |
| `sources/__init__.py` | пакет источников | Ingestion |
| `sources/github.py` | скачивание документов из GitHub через API (`bars-kb sync --github`) | Ingestion |
| `eval/__init__.py` | пакет оценки | QA |
| `eval/runner.py` | golden set → hybrid-поиск → hit@1/3/5, MRR; пишет `eval_report.json` и копию в `dist/` | QA |
| `checksums.py` | SHA256 всех файлов пакета | Handoff |
| `pipeline.py` | оркестратор: источники, build_id, парсинг, чанкинг, эмбеддинги, Qdrant, alias, manifest, export, README пакета, checksums | Ingestion / Handoff |
| `cli.py` | команды `ingest`, `sync`, `status`, `eval`, `fixtures` | Config |
| `fixtures.py` | генерация 4 DOCX-фикстур для тестирования парсера | QA |

## 5. `examples/` — то, чем пользуется следующий разработчик

| Файл | Что делает | Кому |
|---|---|---|
| `restore_from_export.py` | создаёт коллекцию, заливает точки из `chunks.jsonl.gz`, ставит alias | следующий разработчик |
| `query.py` | CLI-пример поиска: hybrid/dense/sparse, вывод цитаты и источника | следующий разработчик |
| `kb_client.py` | переиспользуемый клиент: `.env`, эмбеддинг запроса, sparse, RRF | следующий разработчик |
| `reembed_from_export.py` | пересчёт только dense-векторов под другую модель (OpenAI-совместимый API или local sentence-transformers) | следующий разработчик |

## 6. `tests/`

| Файл | Что проверяет |
|---|---|
| `test_units.py` | markdown-таблицы, детерминизм hash-эмбеддингов, sparse, очистку футеров, DOCX-структуру, FAQ-чанки, правила релевантности eval |

## 7. `docs/`

| Путь | Что это | Попадает в базу? |
|---|---|---|
| `docs/README.md` | индекс документации | нет |
| `docs/source/БП Командировка.pdf` | вход пайплайна | да |
| `docs/source/Реестр мебели.pdf` | вход пайплайна | да |
| `docs/source/Функциональные_возможности_работы_с_БАРС_Офисом.pdf` | вход пайплайна | да |
| `docs/context/Шаблон_Бриф _ хакатон.pdf` | бриф задания | нет |
| `docs/context/Критерии_оценивания_...pdf` | критерии оценки | нет |

## 8. `data/` — вход и последняя сборка

| Файл | Что это | Генерируется? |
|---|---|---|
| `data/fixtures/fixture_core.docx` | DOCX-фикстура: заголовки, списки, таблица, мусорный футер | да, `bars-kb fixtures` |
| `data/fixtures/fixture_tricky.docx` | фикстура со сложными строками (`!>`, URL, 2334-26) | да |
| `data/fixtures/fixture_update_v1.docx` | версия 1 для проверки обновлений | да |
| `data/fixtures/fixture_update_v2.docx` | версия 2 для проверки обновлений | да |
| `data/golden_retrieval.jsonl` | 13 контрольных вопросов с ожидаемыми документами/секциями | вручную |
| `data/manifest.json` | манифест последней сборки | да |
| `data/eval_report.json` | последние метрики и примеры выдачи | да |
| `data/sparse_vocab.json` | словарь BM25 последней сборки | да |
| `data/images/` | извлечённые изображения | да, в git не входит |
| `data/qdrant/` | embedded Qdrant | да, в git не входит |
| `data/cache/` | кэш обогащения | да, в git не входит |
| `data/builds/` | история манифестов | да, в git не входит |

## 9. `dist/<build_id>/` — сам пакет передачи

| Файл | Что внутри | Кому |
|---|---|---|
| `chunks.jsonl.gz` | 108 JSONL: `id`, `dense_vector`, `sparse_vector`, `payload` | следующий разработчик |
| `images/` | 42 PNG: 41 скриншот + 1 схема | бот / следующий разработчик |
| `images.jsonl` | связь `chunk_id` → файл изображения | следующий разработчик |
| `documents.jsonl` | 3 документа: версия, sha256, counts | следующий разработчик |
| `collection_config.json` | имя коллекции, alias, named vectors, модель эмбеддингов | следующий разработчик |
| `manifest.json` | полный манифест: counts, документы, Qdrant, модели | следующий разработчик |
| `sparse_vocab.json` | словарь BM25 для запросов | следующий разработчик |
| `golden_retrieval.jsonl` | 13 контрольных вопросов | QA |
| `eval_report.json` | hit@1/3/5, MRR, топы выдачи | QA |
| `README_HANDOFF.md` | инструкция внутри пакета | следующий разработчик |
| `checksums.sha256` | SHA256 всех файлов | следующий разработчик |
| `build_info.json` | краткая информация о сборке | следующий разработчик |

## 10. `presentation/`

| Файл | Что это |
|---|---|
| `slides.md` | слайды защиты (Marp): задача, архитектура, чанки, метрики, handoff, ограничения |

## 11. Что читать в каком порядке

1. `README.md` — как запустить.
2. `HANDOFF_FILES.md` — что именно отдаём.
3. `HANDOFF.md` — контракт: payload, restore, обновление.
4. `dist/<build>/README_HANDOFF.md` — инструкция внутри пакета.
5. `examples/restore_from_export.py` и `examples/query.py` — быстрый старт.
6. `PROJECT_FILES.md` (этот файл) — если нужно менять пайплайн.
