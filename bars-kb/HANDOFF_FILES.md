# Что именно мы отдаём

Итог нашей части — **один переносимый пакет** в `dist/<build_id>/` плюс инструменты
рядом с ним. Никакого API, бота или сгенерированных ответов в поставке нет.

Актуальная сборка: последняя папка `dist/<build_id>/` (около 3 МБ).

Модели: **Qwen3.8-Flash** (`qwen/qwen3.8-flash`) — текст, JSON, таблицы и скриншоты;
**Qwen `qwen/qwen3-embedding-8b`** (dim 1024) — dense-векторы. Один `QWEN_API_KEY`
OpenRouter (для DashScope — `qwen3.8-flash` и `text-embedding-v4`).

## Пакет `dist/<build_id>/`

| Файл | Формат | Кому / зачем |
|---|---|---|
| `chunks.jsonl.gz` | gzip + JSONL | **главный файл**: чанки, dense + sparse векторы, payload |
| `images/` | 42 PNG | скриншоты и схема для ответов бота |
| `images.jsonl` | JSONL | связь `chunk_id` → файл изображения |
| `documents.jsonl` | JSONL | документы, версии, sha256, counts |
| `collection_config.json` | JSON | схема коллекции: имя, alias, named vectors, модель |
| `manifest.json` | JSON | полный манифест: документы, counts, Qdrant, экспорт |
| `sparse_vocab.json` | JSON | словарь BM25, нужен для запросов |
| `golden_retrieval.jsonl` | JSONL | 13 контрольных вопросов |
| `eval_report.json` | JSON | hit@1/3/5, MRR, примеры выдачи |
| `README_HANDOFF.md` | Markdown | инструкция внутри пакета |
| `checksums.sha256` | текст | SHA256 всех файлов, проверка целостности |
| `build_info.json` | JSON | краткая информация о сборке |

## Инструменты рядом

| Файл | Зачем |
|---|---|
| `examples/restore_from_export.py` | залить пакет в Qdrant (сервер или embedded) |
| `examples/query.py` | пример hybrid-поиска |
| `examples/kb_client.py` | клиент: эмбеддинг запроса + dense/sparse + RRF |
| `examples/reembed_from_export.py` | пересчитать dense-векторы под другую модель |
| `HANDOFF.md` | контракт: схема payload, восстановление, обновление |
| `PROJECT_FILES.md` | полная карта модулей: кто за что отвечает и что делает каждый файл |
| `README.md` | запуск пайплайна и пересборка |
| `docs/source/` | исходные PDF, вход пайплайна |
| `presentation/slides.md` | слайды защиты (Marp) |

## Быстрая проверка пакета

```bash
cd dist/<build_id>
sha256sum -c checksums.sha256

cd ../..
python examples/restore_from_export.py \
  --export dist/<build_id> --qdrant-path /tmp/bars-kb-check
QDRANT_PATH=/tmp/bars-kb-check PYTHONPATH=examples python examples/query.py \
  "Почему не работает фильтр !> 200?" --top-k 3
```

## Что НЕ входит в поставку

- HTTP API, Telegram-бот, генерация ответов, эскалация, аналитика;
- `data/qdrant/`, `data/cache/`, `data/images/` — генерируются заново;
- ключи и `.env` — только `.env.example`.

## Где смотреть на GitHub

Ветка: `feature/bars-kb`

```text
https://github.com/DamnKhis/barsik-helper-bot/tree/feature/bars-kb/bars-kb
https://github.com/DamnKhis/barsik-helper-bot/tree/feature/bars-kb/bars-kb/dist/<build_id>
```

GitHub откроет Markdown и JSON, PNG — в просмотрщике. `chunks.jsonl.gz` скачивается
и распаковывается локально.
