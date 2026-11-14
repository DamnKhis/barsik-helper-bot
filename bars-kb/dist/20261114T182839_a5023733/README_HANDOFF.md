# BARS KB — пакет передачи

Собран пайплайном `bars-kb` версии `0.1.0`.

| Параметр | Значение |
|---|---|
| build_id | `20261114T182839_a5023733` |
| коллекция | `bars_kb_20261114T182839_a5023733` |
| alias | `bars_kb_current` |
| предыдущая версия | `bars_kb_prev` |
| создан | 2026-11-14T18:29:19.780050+00:00 |
| эмбеддинги | `qwen` / `qwen/qwen3-embedding-8b`, dim `1024` |
| текст и JSON | `qwen/qwen3.8-flash` |
| скриншоты и схемы | `qwen/qwen3.8-flash` |
| sparse | BM25, IDF |
| snapshot | `нет (embedded Qdrant)` |

## Состав

| Файл | Что внутри |
|---|---|
| `chunks.jsonl.gz` | по одному JSON на чанк: `id`, `dense_vector`, `sparse_vector`, `payload` |
| `documents.jsonl` | документы, версии, sha256, число разделов и чанков |
| `images.jsonl` | связь `chunk_id` → файл изображения |
| `images/` | 41 скриншотов и 1 схема |
| `collection_config.json` | имя коллекции, alias, named vectors, модель эмбеддингов |
| `manifest.json` | полный манифест сборки, counts, документы, Qdrant |
| `build_info.json` | краткая информация о сборке |
| `sparse_vocab.json` | словарь BM25; обязателен для sparse-поиска |
| `golden_retrieval.jsonl` | контрольные вопросы для eval |
| `eval_report.json` | метрики hit@1/3/5, MRR и примеры выдачи (после `bars-kb eval`) |
| `checksums.sha256` | SHA256 всех файлов пакета |

## Данные

- документов: **3**
- чанков: **108**
- text: 55, faq: 11, table: 0,
  screenshot: 41, diagram: 1

Подробная схема payload — в `HANDOFF.md` проекта. Кратко: поиск идёт по `enriched_text`,
ответ и цитата — по `raw_text`; `parent_text` даёт контекст раздела; `section_path` и
`page` — для ссылки на источник.

## Восстановление

```bash
# Qdrant-сервер
python examples/restore_from_export.py \
  --export 20261114T182839_a5023733 \
  --qdrant-url http://127.0.0.1:6333 \
  --alias bars_kb_current

# локальный embedded Qdrant
python examples/restore_from_export.py \
  --export 20261114T182839_a5023733 --qdrant-path data/qdrant-restored
```

## Проверка целостности

```bash
cd 20261114T182839_a5023733 && sha256sum -c checksums.sha256
```

## Поиск

```bash
python examples/query.py "Почему не работает фильтр !> 200?" --top-k 5
```

## Модель эмбеддингов

Вектор запроса обязан считаться той же моделью: **`qwen/qwen3-embedding-8b`**, dim **1024**.
Если модель другая — `examples/reembed_from_export.py` пересчитает только dense-векторы.
