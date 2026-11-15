# BARS KB Ingestion Pipeline

Пайплайн загрузки документации БАРС в Qdrant. Превращаем PDF/DOCX/MD/TXT в чистый,
структурированный датасет: чанки с текстом, изображениями, таблицами, FAQ-слоем и
векторами для гибридного поиска.

Это **только ingestion + Qdrant**. Мы не делаем HTTP API, генерацию ответов и
Telegram-интеграцию — это зона следующего разработчика. Результат передаётся пакетом
`dist/<build_id>/` и подробно описан в [HANDOFF.md](HANDOFF.md).

## Что умеет

- **PDF** — нативный парсинг через `PyMuPDF` (без `pdftotext`, `pdfimages` и `poppler-utils`);
- **DOCX** — заголовки, списки, таблицы, изображения через `python-docx`;
- **MD/TXT** — простые текстовые источники;
- очистка мусора Word/PDF (колонтитулы, оглавление, `Ошибка! Используйте вкладку...`);
- чанкинг parent/child с breadcrumb и типами `text`, `faq`, `table`, `diagram`, `screenshot`;
- фильтрация изображений через `Pillow`: отсеиваются картинки меньше 50×50, системные
  иконки и квадратные заглушки/скрепки, чтобы не тратить токены VLM;
- обогащение текста, таблиц и изображений через **Qwen3.8-Flash** (`qwen/qwen3.8-flash`);
- параллельные запросы к LLM/VLM через `ThreadPoolExecutor` (`ENRICHMENT_WORKERS`);
- эмбеддинги Qwen `qwen/qwen3-embedding-8b` (dim 1024) или hash-lexical без ключа;
- Qdrant: dense + sparse BM25, alias `bars_kb_current`, предыдущая версия `bars_kb_prev`;
- экспорт `chunks.jsonl.gz` с векторами, `documents.jsonl`, `manifest.json`, `images/`;
- offline-eval по golden set и переэмбеддинг готового экспорта другой моделью.

## Требования

- **Python 3.10+**;
- больше **никаких системных пакетов**: `poppler-utils`, `pdftotext` и `pdfimages`
  не нужны. `PyMuPDF` и `Pillow` ставятся как обычные Python-зависимости и работают
  на Linux, macOS и Windows, в том числе внутри Docker;
- для реального обогащения — ключ Qwen API (OpenRouter или DashScope).

## Установка

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

Опционально, только если нужны локальные эмбеддинги без API:

```bash
.venv/bin/pip install -e ".[local-embeddings]"
```

> Windows: интерпретатор и pip лежат в `.venv\Scripts\`, поэтому вместо
> `.venv/bin/python` используйте `.venv\Scripts\python`. Системные пакеты
> (Visual C++ Build Tools, poppler) не нужны: PyMuPDF и Pillow ставятся колёсами.

## Настройка окружения

```bash
cp .env.example .env
```

В `.env` достаточно заполнить один ключ:

```dotenv
QWEN_API_KEY=...
```

Шаблон [`.env.example`](.env.example) уже содержит настройки OpenRouter:

```dotenv
QWEN_BASE_URL=https://openrouter.ai/api/v1
QWEN_LLM_MODEL=qwen/qwen3.8-flash
QWEN_VISION_MODEL=qwen/qwen3.8-flash
QWEN_EMBEDDING_MODEL=qwen/qwen3-embedding-8b
```

Для DashScope переключите закомментированные строки в `.env.example` на
`qwen3.8-flash` и `text-embedding-v4`.

Полезные параметры:

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `QWEN_API_KEY` | пусто | без ключа включается mock/hash-режим |
| `ENRICHMENT_WORKERS` | `4` | число параллельных запросов к LLM/VLM |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `QDRANT_PATH` | `data/qdrant` | локальный embedded Qdrant |
| `QDRANT_URL` | пусто | если нужен Qdrant-сервер |

## Запуск

Через установленную команду:

```bash
.venv/bin/bars-kb ingest
.venv/bin/bars-kb eval --top-k 8
.venv/bin/bars-kb status
```

Или напрямую из исходников, без установки console script:

```bash
.venv/bin/python src/bars_kb/cli.py ingest
.venv/bin/python src/bars_kb/cli.py eval --top-k 8
.venv/bin/python src/bars_kb/cli.py status
```

Логи пишутся в формате:

```text
[2026-11-14 23:38:08] INFO - bars_kb.pipeline - [1/3] parse БП Командировка.pdf
```

### Команды

| Команда | Что делает |
|---|---|
| `bars-kb ingest` | собрать базу из `docs/source` |
| `bars-kb ingest --source data/fixtures` | собрать из тестовых DOCX |
| `bars-kb sync` | локальная синхронизация + пересбор, если документы изменились |
| `bars-kb sync --github` | скачать `GITHUB_REPO` и пересобрать |
| `bars-kb sync --publish` | пересобрать и закоммитить/запушить `dist/` |
| `bars-kb builds` | показать сборки в `dist/`, `*` — актуальная |
| `bars-kb rollback [build_id]` | переключить `dist/LATEST` на предыдущую сборку |
| `bars-kb eval --top-k 8` | прогнать golden set, записать `data/eval_report.json` |
| `bars-kb fixtures` | сгенерировать DOCX-фикстуры |
| `bars-kb status` | показать последний manifest |

Глобальный флаг `--log-level` работает перед подкомандой:

```bash
.venv/bin/bars-kb --log-level DEBUG ingest
```

То же через `make`: `make install`, `make ingest`, `make sync`, `make publish`,
`make builds`, `make rollback`, `make eval`, `make test`, `make restore`,
`make query`.

## Обновление документации и `dist/`

`dist/` хранится в git намеренно: это готовый пакет передачи. Чтобы не менять
код следующего разработчика при каждой пересборке, актуальная сборка выбирается
через стабильный указатель `dist/LATEST`, а не через имя папки.

```text
dist/LATEST                     # содержит build_id
dist/<build_id>/                # текущая сборка
dist/<build_id-1>/              # предыдущая сборка
```

В коде и конфигурации используйте базовую папку:

```dotenv
KB_EXPORT_DIR=bars-kb/dist
```

`examples/kb_client.py`, `examples/restore_from_export.py` и `Retriever` сами
читают `LATEST`. Захардкоженный `dist/2026...` не нужен. В `dist/` лежат текущая
и предыдущая сборки; `bars-kb builds` покажет список, `bars-kb rollback` —
переключит указатель.

### Локально

```bash
# новые PDF/DOCX уже лежат в docs/source
bars-kb sync

# то же + commit/push обновлённого dist
bars-kb sync --publish

# пересборка с прогоном golden set и eval_report.json
bars-kb sync --eval
```

`sync` сравнивает digest содержимого документов с текущей сборкой. Если
документы не изменились, пересборка пропускается; `--force` пересобирает
принудительно. После успешной сборки обновляются `dist/LATEST`, манифест и
checksums, а старые сборки удаляются (`--keep 2` по умолчанию).

### GitHub Actions

Workflow [`.github/workflows/update-kb.yml`](../.github/workflows/update-kb.yml)
запускается при изменении `docs/source/**` (и вручную через `workflow_dispatch`):

1. ставит пакет и запускает `bars-kb sync --source docs/source --publish --eval`;
2. если документы изменились — пересобирает KB, считает hit@k/MRR и коммитит
   `dist/` + `dist/LATEST` обратно в ту же ветку;
3. если не изменились — ничего не коммитит.

Нужен repository secret `QWEN_API_KEY`. Без него workflow падает, чтобы не
опубликовать mock/hash-сборку поверх реальной. Публикация mock-сборки вручную
требует явного `--allow-mock`.

В Settings → Actions → General должно быть разрешено выполнение Actions
(«Allow all actions and reusable workflows»). Если Actions выключены, GitHub
молча не создаёт запуски, и коммит в `docs/source/` остаётся без пересборки.

`workflow_dispatch` (кнопка Run workflow) доступен только когда workflow есть в
ветке по умолчанию. До мержа он срабатывает на push в `feature/bars-kb`; правка
самого файла workflow тоже запускает прогон — это безопасный smoke-тест: при
неизменных документах сборка пропускается и коммит не создаётся.

### Сервер: обновление без перезапуска

Если проект крутится на сервере, пересборка не должна требовать релиза бота.
Есть два рабочих сценария; оба сводятся к одному: меняется `dist/LATEST`, а
клиент подхватывает новый пакет на следующем запросе.

**Документы приходят через git (GitHub Actions).** Сервер только забирает
готовый пакет:

```bash
# каждые 10 минут: git pull + sync (пересборка пропускается, digest совпал)
bars-kb/deploy/update-kb.sh --git-pull
```

**Документы лежат на сервере** (`docs/source`). Тогда `sync` сам пересоберёт KB,
когда изменится содержимое:

```bash
bars-kb/deploy/update-kb.sh
```

Скрипт запускается из cron или systemd-таймера; готовые юниты лежат в
[`deploy/`](deploy/):

```bash
sudo cp bars-kb/deploy/bars-kb-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bars-kb-sync.timer
```

Пример cron:

```cron
*/10 * * * * cd /opt/barsik-helper-bot/bars-kb && PATH=.venv/bin:$PATH deploy/update-kb.sh --git-pull >> /var/log/bars-kb-sync.log 2>&1
```

Qdrant. Если CI собирал пакет с embedded Qdrant, коллекции на сервере нет:
добавьте к скрипту `--restore` (нужен `QDRANT_URL`), и он загрузит сборку из
`dist/LATEST` в серверный Qdrant. Если в secrets Actions заданы `QDRANT_URL` и
`QDRANT_API_KEY`, CI пишет сразу в общий Qdrant и `--restore` не нужен.

Клиенты. `examples/kb_client.py` (и `Retriever`, который им пользуется) на
каждом поиске перечитывает `dist/LATEST` и сам переключается на новую коллекцию.
Перезапускать бота после пересборки не нужно; откат через `bars-kb rollback`
подхватывается так же. Чтобы не терять ответы на время пересборки, клиент
продолжает работать со старой сборкой, пока новая не станет доступна.

### Откат

```bash
bars-kb builds                 # список; * — актуальная
bars-kb rollback               # LATEST -> предыдущая сборка
bars-kb rollback --publish     # закоммитить и запушить откат
python examples/restore_from_export.py --export dist
```

`--keep 2` гарантирует, что предыдущая сборка остаётся в репозитории. Откат
меняет указатель, а Qdrant переключается восстановлением выбранного пакета
(скрипт создаёт коллекцию и переключает alias `bars_kb_current`).

## Что получается на выходе

- коллекция Qdrant `bars_kb_<build_id>` и alias `bars_kb_current`;
- пакет `dist/<build_id>/` с `chunks.jsonl.gz`, `sparse_vocab.json`, `images/`,
  `manifest.json`, `checksums.sha256`;
- `dist/LATEST` — стабильный указатель на актуальную сборку;
- `data/manifest.json` с последней сборкой и `data/eval_report.json` с метриками.

Пакет самодостаточен: его можно восстановить в чистый Qdrant скриптом
`examples/restore_from_export.py` и искать через `examples/kb_client.py`.

## Qdrant-сервер

Локально по умолчанию используется embedded Qdrant (`data/qdrant`). Для сервера:

```bash
docker compose up -d
# .env: QDRANT_URL=http://127.0.0.1:6333
```

## Структура

```text
src/bars_kb/
  parsers/      PDF (PyMuPDF), DOCX, MD/TXT → AST; фильтрация изображений
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
dist/LATEST     указатель на актуальную сборку
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

## Ограничения

- PDF-таблицы не парсятся структурно: Word-экспорт теряет границы колонок. DOCX-таблицы
  парсятся структурно.
- Без vision-ключа скриншоты получают только контекстное описание, а не OCR/разметку выносок.
- Snapshot Qdrant в embedded-режиме недоступен; основной формат передачи — `chunks.jsonl.gz`
  + restore-скрипт.

Подробности передачи — в [HANDOFF.md](HANDOFF.md), полная карта файлов и зон
ответственности — в [PROJECT_FILES.md](PROJECT_FILES.md).
