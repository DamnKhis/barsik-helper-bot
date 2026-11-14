# HANDOFF — что получает следующий разработчик

Конечный артефакт — **Qdrant-датасет** и инструменты его восстановления/пересборки.
Никакого HTTP API для бота в поставку не входит.

## 1. Состав пакета

```text
dist/<build_id>/
├── manifest.json              # версия, counts, модель эмбеддингов, документы, checksums
├── collection_config.json     # имя коллекции, alias, named vectors, payload-индексы
├── chunks.jsonl.gz            # id + dense_vector + sparse_vector + payload
├── documents.jsonl            # документы и их версии
├── images.jsonl               # соответствие chunk_id → изображение
├── images/                    # скриншоты и схемы
├── golden_retrieval.jsonl     # 13 контрольных вопросов
├── eval_report.json           # hit@1/3/5, MRR
├── sparse_vocab.json          # словарь BM25 (нужен для query-time sparse)
├── build_info.json
└── README_HANDOFF.md          # этот файл
```

## 2. Коллекция Qdrant

```text
collection: bars_kb_<build_id>
alias:      bars_kb_current
previous:   bars_kb_prev

dense:  1024, Cosine   (Qwen text-embedding-v4 в финальной сборке)
bm25:   sparse, IDF    (точные строки: !>, 2334-26, ссылки)
```

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
"embedding": {"provider": "qwen", "model": "text-embedding-v4", "dim": 1024}
```

Если у вас другая модель/провайдер, пересоберите только dense-векторы готовым скриптом:

```bash
# OpenAI-совместимый endpoint (Qwen/DashScope, OpenAI, vLLM, Ollama...)
python examples/reembed_from_export.py \
  --export dist/<build_id> --provider openai \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --api-key "$QWEN_API_KEY" --model text-embedding-v4 --dim 1024 --send-dimensions

# Локальная модель (в окружении, где установлен sentence-transformers)
python examples/reembed_from_export.py \
  --export dist/<build_id> --provider local --model BAAI/bge-m3 --dim 1024
```

Скрипт сохраняет `chunk_id`, payload и sparse-векторы, меняет только `dense_vector`,
создаёт новый `collection_config.json`/`manifest.json` и готовую папку для restore.
Не смешивайте векторы разных моделей в одной коллекции.

Это согласованная точка: следующий разработчик эмбеддит запросы Qwen `text-embedding-v4`
(dim 1024), как и коллекция. Если его модель изменится — используется скрипт выше.

## 6. Обновление документации

### Локально

```bash
# положить новые PDF/DOCX в docs/source
bars-kb sync
```

### GitHub

```bash
# .env: GITHUB_REPO=owner/repo, GITHUB_TOKEN=..., GITHUB_DOCS_PATH=docs
bars-kb sync --github
```

Полный пересбор создаёт новую коллекцию `bars_kb_<build_id>`, переключает
`bars_kb_current`, оставляет `bars_kb_prev`. Ошибка пересбора не ломает текущую версию.
Для регулярного обновления — cron/GitHub Actions, без webhook-сервера:

```cron
*/15 * * * * cd /opt/bars-kb && .venv/bin/bars-kb sync --github >> sync.log 2>&1
```

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
