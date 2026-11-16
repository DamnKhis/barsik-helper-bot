# BARS KB — пакет передачи

Собран пайплайном `bars-kb` версии `0.1.0`.

| Параметр | Значение |
|---|---|
| build_id | `20261116T005001_cc4db7fc` |
| коллекция | `bars_kb_20261116T005001_cc4db7fc` |
| alias | `bars_kb_current` |
| предыдущая версия | `bars_kb_prev` |
| source digest | `cc4db7fcb5702ab4` |
| создан | 2026-11-16T00:50:31.616820+00:00 |
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
| `images/` | 36 скриншотов и 2 схема |
| `collection_config.json` | имя коллекции, alias, named vectors, модель эмбеддингов |
| `manifest.json` | полный манифест сборки, counts, документы, Qdrant |
| `build_info.json` | краткая информация о сборке |
| `sparse_vocab.json` | словарь BM25; обязателен для sparse-поиска |
| `golden_retrieval.jsonl` | контрольные вопросы для eval |
| `support_golden.jsonl` | контрольные вопросы из решённых обращений |
| `eval_report.json` | метрики hit@1/3/5, MRR и примеры выдачи (после `bars-kb eval`) |
| `checksums.sha256` | SHA256 всех файлов пакета |

## Данные

- документов: **6** (из них решённых обращений поддержки:
  **1**)
- чанков: **133**
- text: 77, faq: 13, table: 4,
  screenshot: 36, diagram: 2,
  support: 1 (пары «вопрос/ответ» из решённых обращений)

Подробная схема payload — в `HANDOFF.md` проекта. Кратко: поиск идёт по `enriched_text`,
ответ и цитата — по `raw_text`; `parent_text` даёт контекст раздела; `section_path` и
`page` — для ссылки на источник. Поле `doc_kind` отличает документацию (`doc`) от
решённых обращений поддержки (`support`), `source_path` ведёт к исходному файлу.

## Восстановление

Из корня проекта (выбирается сборка из `dist/LATEST`):

```bash
# Qdrant-сервер
python examples/restore_from_export.py \
  --export dist \
  --qdrant-url http://127.0.0.1:6333 \
  --alias bars_kb_current

# локальный embedded Qdrant
python examples/restore_from_export.py \
  --export dist --qdrant-path data/qdrant-restored
```

## Проверка целостности

```bash
cd 20261116T005001_cc4db7fc && sha256sum -c checksums.sha256
```

## Поиск

```bash
python examples/query.py "Почему не работает фильтр !> 200?" --top-k 5
```

## Модель эмбеддингов

Вектор запроса обязан считаться той же моделью: **`qwen/qwen3-embedding-8b`**, dim **1024**.
Если модель другая — `examples/reembed_from_export.py` пересчитает только dense-векторы.
