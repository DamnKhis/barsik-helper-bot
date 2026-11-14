# HANDOFF — что получает следующий разработчик

Конечный артефакт — **Qdrant-датасет** и инструменты его восстановления/пересборки.
Никакого HTTP API для бота в поставку не входит.

## 1. Состав пакета

```text
dist/
├── LATEST                     # build_id актуальной сборки (стабильная точка входа)
└── <build_id>/
    ├── manifest.json              # версия, counts, модели, документы, source digest
    ├── collection_config.json     # имя коллекции, alias, named vectors, payload-индексы
    ├── chunks.jsonl.gz            # id + dense_vector + sparse_vector + payload
    ├── documents.jsonl            # документы и их версии
    ├── images.jsonl               # соответствие chunk_id → изображение
    ├── images/                    # скриншоты и схемы
    ├── golden_retrieval.jsonl     # 13 контрольных вопросов
    ├── eval_report.json           # hit@1/3/5, MRR
    ├── sparse_vocab.json          # словарь BM25 (нужен для query-time sparse)
    ├── checksums.sha256           # SHA256 всех файлов пакета
    ├── build_info.json
    └── README_HANDOFF.md          # инструкция внутри пакета
```

Не ссылайтесь на `dist/<build_id>/` в коде: при пересборке имя меняется.
Передавайте базовую папку `dist` — клиенты читают `dist/LATEST` сами.

## 2. Коллекция Qdrant

```text
collection: bars_kb_<build_id>
alias:      bars_kb_current
previous:   bars_kb_prev

dense:  1024, Cosine   (Qwen qwen/qwen3-embedding-8b в финальной сборке)
bm25:   sparse, IDF    (точные строки: !>, 2334-26, ссылки)
```

Модели (один ключ `QWEN_API_KEY`, OpenRouter):

| Задача | Модель |
|---|---|
| текст, JSON, таблицы | `qwen/qwen3.8-flash` |
| скриншоты и схемы | `qwen/qwen3.8-flash` (мультимодальный) |
| dense-векторы | `qwen/qwen3-embedding-8b`, dim 1024 |

`qwen/qwen3.8-flash` — чат/мультимодальная модель, у неё нет embeddings-эндпоинта,
поэтому векторы считает отдельная embedding-модель на том же ключе OpenRouter.
Для DashScope используйте `qwen3.8-flash` и `text-embedding-v4` (см. `.env.example`).

Payload-поля:

| Поле | Назначение |
|---|---|
| `chunk_id` | стабильный UUID |
| `doc_id`, `doc_title` | документ |
| `section_path` | путь раздела, для цитаты |
| `page` | страница PDF (0 для DOCX) |
| `chunk_type` | `text`, `faq`, `table`, `diagram`, `screenshot` |
| `raw_text` | оригинальный текст — **только для цитаты** |
| `enriched_text` | текст, по которому ищут; содержит breadcrumb/вопросы |
| `summary` | краткое описание |
| `parent_id`, `parent_text` | контекст раздела для LLM |
| `source_url` | ссылка на документ и версию |
| `image_path`, `image_url` | изображение, если есть |
| `table_json` | строки/заголовки таблицы |
| `diagram_mermaid` | схема процесса, если удалось построить |
| `entities` | термины |
| `version`, `content_hash`, `pipeline_version` | версионирование |
| `faq_tag`, `derived` | происхождение FAQ-чанка |

## 3. Восстановление

### Вариант A: из JSONL (работает всегда)

```bash
python examples/restore_from_export.py \
  --export dist/<build_id> \
  --qdrant-url http://127.0.0.1:6333 \
  --alias bars_kb_current
```

Скрипт создаёт коллекцию с `dense` + `bm25`, заливает точки и ставит alias.

### Вариант B: из snapshot

Если в `manifest.json` есть `qdrant.snapshot`, восстановите snapshot средствами Qdrant
и создайте alias `bars_kb_current` на восстановленную коллекцию. Snapshot’ы привязаны
к версии Qdrant-сервера.

## 4. Поиск

```bash
# dense + sparse + RRF
python examples/query.py "Почему не работает фильтр !> 200?" --top-k 5

# только dense / только sparse
python examples/query.py "график офис-менеджера" --mode dense
python examples/query.py "2334-26" --mode sparse
```

Пример кода — `examples/kb_client.py`.

## 5. Критично: модель эмбеддингов запроса

Вектор запроса **обязан** считаться той же моделью, что и векторы документов.
В `collection_config.json` и `manifest.json` зафиксированы:

```json
"embedding": {"provider": "qwen", "model": "qwen/qwen3-embedding-8b", "dim": 1024}
```

Если у вас другая модель/провайдер, пересоберите только dense-векторы готовым скриптом:

```bash
# OpenAI-совместимый endpoint (OpenRouter, DashScope, OpenAI, vLLM, Ollama...)
python examples/reembed_from_export.py \
  --export dist/<build_id> --provider openai \
  --base-url https://openrouter.ai/api/v1 \
  --api-key "$QWEN_API_KEY" --model qwen/qwen3-embedding-8b --dim 1024 --send-dimensions

# Локальная модель (в окружении, где установлен sentence-transformers)
python examples/reembed_from_export.py \
  --export dist/<build_id> --provider local --model BAAI/bge-m3 --dim 1024
```

Скрипт сохраняет `chunk_id`, payload и sparse-векторы, меняет только `dense_vector`,
создаёт новый `collection_config.json`/`manifest.json` и готовую папку для restore.
Не смешивайте векторы разных моделей в одной коллекции.

Это согласованная точка: следующий разработчик эмбеддит запросы Qwen
`qwen/qwen3-embedding-8b` (dim 1024), как и коллекция, и использует
`qwen/qwen3.8-flash` для генерации ответов. Оба вызова идут через один
`QWEN_API_KEY` OpenRouter (можно разные ключи, но провайдер один).
Если embedding-модель изменится — используется скрипт выше.

## 6. Обновление документации

Актуальная сборка выбирается через `dist/LATEST`, а не через имя папки. Имя
`dist/<build_id>/` меняется при каждой пересборке; код и конфигурация должны
ссылаться на базовую папку:

```dotenv
KB_EXPORT_DIR=bars-kb/dist
```

`examples/kb_client.py`, `examples/restore_from_export.py` и `Retriever` читают
`LATEST` сами. В `dist/` по умолчанию хранятся две сборки: текущая и предыдущая.

### Локально

```bash
# положить новые PDF/DOCX в docs/source
bars-kb sync              # пересоберёт, только если документы изменились
bars-kb sync --force      # пересобрать принудительно
bars-kb sync --eval       # + прогнать golden set и записать eval_report.json
bars-kb sync --publish    # пересобрать + commit/push dist/
```

Сравнение идёт по digest содержимого документов (`manifest.json → source.digest`),
поэтому лишние сборки при неизменных файлах не появляются.

### GitHub

```bash
# .env: GITHUB_REPO=owner/repo, GITHUB_TOKEN=..., GITHUB_DOCS_PATH=docs/source
bars-kb sync --github
```

Для автоматического обновления `dist/` при изменениях документации на GitHub
используется workflow `.github/workflows/update-kb.yml`. Он запускается на push
в `docs/source/**`, выполняет `bars-kb sync --source docs/source --publish --eval`
и коммитит изменившийся `dist/` + `dist/LATEST` обратно в ту же ветку. Нужен
секрет `QWEN_API_KEY`; без него workflow падает, чтобы не опубликовать
mock/hash-сборку.

Если Actions недоступны, тот же эффект даёт cron:

```cron
*/15 * * * * cd /opt/bars-kb && .venv/bin/bars-kb sync --github --publish >> sync.log 2>&1
```

### Откат

```bash
bars-kb builds                    # * — актуальная сборка
bars-kb rollback                  # LATEST -> предыдущая
bars-kb rollback <build_id>       # LATEST -> конкретная
bars-kb rollback --publish        # закоммитить и запушить откат

# вернуть выбранную сборку в Qdrant
python examples/restore_from_export.py --export dist --qdrant-url http://127.0.0.1:6333
```

Полный пересбор создаёт новую коллекцию `bars_kb_<build_id>`, переключает
`bars_kb_current`, оставляет `bars_kb_prev`. Ошибка пересбора не ломает текущую
версию. Старые сборки удаляются при `--keep N` (по умолчанию 2, минимум 1).

## 7. Изображения

`images/` содержит PNG/JPG, имена — sha256-префиксы. `images.jsonl` связывает
`chunk_id` с файлом. Если бот должен отдавать картинку, следующий разработчик
поднимает статику/object storage и подставляет `image_url`; мы отдаём файлы и
относительные пути.

## 8. Ограничения

- Без Qwen vision скриншоты описаны контекстно, без OCR/выносок.
- PDF-таблицы не структурированы; для качества нужны исходные DOCX.
- Qdrant embedded не умеет snapshot; JSONL — основной переносимый формат.
- Alias и payload-индексы полностью работают на Qdrant-сервере; в embedded alias
  поддерживается, индексы — нет (предупреждение при сборке).
