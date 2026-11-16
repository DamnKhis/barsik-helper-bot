# БАРС хелпер — Telegram-бот поддержки «БАРС-Офис»

Виртуальный помощник по документации «БАРС-Офис» на базе RAG. Отвечает на
вопросы по внутренним регламентам и инструкциям, принимает скриншоты, а если не
уверен — передаёт обращение живому оператору в Jira. Всё, что нужно, работает в
одном процессе: Telegram-бот (aiogram), HTTP API и дашборд (FastAPI), поиск по
базе знаний (Qdrant) и автообновление базы при изменении документов.

## Что умеет

### Для пользователя

- **Вопросы текстом.** Отвечает по базе знаний с указанием источника.
- **Вопросы скриншотом.** Присланную картинку описывает vision-модель, и запрос
  идёт тем же путём, что и текстовый (можно добавить подпись).
- **Вызов оператора.** Слова вида «оператор», «человек», «поддержка» (список
  настраивается) показывают кнопку, которая создаёт задачу в Jira и пересылает
  туда всю историю диалога и скриншоты. Дальше идёт двусторонняя переписка.
- **Оценка ответа.** Кнопки 👍/👎 под каждым ответом ИИ — попадают в аналитику.
- **Закрытие обращения.** Пользователь сам может закрыть свой тикет.
- **`/clear`** — очистить историю диалога, **`/help`**, **`/cancel`**, **`/id`**.

### Для администратора

- **Добавление документов.** «Добавить доку» → пришлите файл (PDF, DOCX, MD,
  TXT). Файл попадает в базу знаний, бот сам пересобирает пакет и сообщает
  «✅ документ в базе» либо «⚠️ пересборка не прошла» с причиной.
- Роль `admin` выдаётся автоматически ID из `ADMIN_IDS`.

### Для поддержки (Jira)

- Обращения живут в задачах Jira: бот создаёт задачу, дописывает сообщения
  пользователя комментариями и забирает ответы оператора обратно в Telegram.
- Закрытие задачи в Jira → завершение диалога в боте.
- Тикет со статусом «Полезно»/`useful` выгружается в базу знаний как Markdown
  (с вложениями и скриншотами) — так ответы поддержки улучшают будущие ответы ИИ.
  Комментарии с персональными данными (ФИО, телефон, e-mail, № записи) в БЗ не
  попадают.

### Аналитика

- Дашборд `/dashboard` и JSON `/stats` (`?period=today|week|month`): количество
  запросов, доля авт. ответов, уверенность, медиана времени ответа, оценки,
  топ-темы и «слепые зоны» (вопросы с низкой уверенностью). Время — по Москве.

## Как это работает

```text
Пользователь ─► aiogram ─► RAG (Qdrant: dense + BM25, RRF)
                  │            │
                  │            └─ уверенно → ответ + источник + оценка
                  └─ неуверенно/«оператор» ─► задача Jira ─► оператор
                                                   │
                    ответ оператора ◄── Jira poller ┘

Админ ─► «Добавить доку» ─► uploaded_docs ─► bars-kb/docs/source
      └─ watcher видит изменения ─► пересборка bars-kb/dist ─► бот читает LATEST
```

Ядро RAG (`app/rag/`) читает пакет `bars-kb/dist` (указатель `LATEST`) на каждом
поиске, поэтому пересборка не требует перезапуска бота. Если Qdrant/ключ
недоступны — бот деградирует на заглушку и предлагает оператора, а не падает.

## Быстрый старт

### Docker (рекомендуется)

```bash
cp .env.example .env      # заполните BOT_TOKEN и ключ LLM
make up                   # docker compose up -d --build
make logs                 # смотреть логи API/бота
```

Поднимаются Postgres (опционально), Qdrant и Redis; API с ботом — на `:8000`.

### Локально

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload     # API + бот + дашборд
```

Отдельно только бот (без HTTP):

```bash
.venv/bin/python -m app.bot.main
```

## Настройка

Все параметры — через `.env`. Полный шаблон с комментариями — в
[`.env.example`](.env.example):

```bash
cp .env.example .env
```

Обязательный минимум:

| Переменная | Зачем |
|---|---|
| `BOT_TOKEN` | токен бота от @BotFather. **Без него бот не запускается**, API работает |
| `QWEN_API_KEY` | ключ LLM. Алиасы: `LLM_API_KEY`, `OPENROUTER_API_KEY` (хватит одного). Обслуживает эмбеддинги, ответ и vision. Пустой → mock-режим |
| `ADMIN_IDS` | ID админов через запятую — им выдаётся роль `admin` |

Остальное (значения по умолчанию указаны в `.env.example`):

| Переменная | Зачем |
|---|---|
| `BOT_USERNAME` | юзернейм без `@`, нужен для deep-link на страницы PDF |
| `LLM_BASE_URL` / `LLM_MODEL` | endpoint и модель (по умолчанию OpenRouter + `qwen/qwen3.8-flash`). Для DashScope — блок в `.env.example` |
| `LLM_PROVIDER` / `VISION_PROVIDER` / `EMBEDDING_PROVIDER` | провайдеры (`qwen`/`mock`, эмбеддинги `qwen`/`hash`) |
| `EMBEDDING_DIM`, `QWEN_EMBEDDING_MODEL` | размерность и модель эмбеддингов (1024, `qwen/qwen3-embedding-8b`) |
| `DATABASE_URL` | SQLite по умолчанию, можно Postgres (`postgresql+asyncpg://...`) |
| `REDIS_URL` | контекст диалога для ИИ |
| `QDRANT_URL` / `QDRANT_PATH` | сервер Qdrant (в compose — `http://qdrant:6333`) или локальный embedded |
| `KB_EXPORT_DIR` | папка пакета знаний (по умолчанию `bars-kb/dist`) |
| `TOP_K`, `CONFIDENCE_THRESHOLD` | сколько чанков брать и порог уверенности; ниже порога — предложение оператора |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | HTTP Basic для дашборда; пустой пароль — открыт всем |
| `JIRA_BASE_URL` / `JIRA_API_TOKEN` / `JIRA_PROJECT_KEY` | подключение к Jira (без них кнопка оператора скажет, что поддержка недоступна) |
| `SUPPORT_HANDLE` | кого упоминать в сообщении «запрос в обработке» |
| `VISION_ENABLED`, `IMAGE_QUERY_DIR`, `SEND_KB_IMAGES` | обработка скриншотов |
| `KB_AUTOSYNC`, `KB_AUTOSYNC_INTERVAL` | автообновление базы знаний (выключить: `KB_AUTOSYNC=0`) |

Скриншоты без подписи: если vision не ответил, бот попросит описать вопрос
текстом. Скриншоты сохраняются в `IMAGE_QUERY_DIR/<telegram_id>/` — они нужны
оператору в тикете.

## Команды (`make`)

| Команда | Что делает |
|---|---|
| `make up` / `make down` | поднять/остановить docker compose |
| `make logs` | логи API |
| `make test` | `pytest -q` |
| `make lint` | `ruff check .` |
| `make format` | `ruff check --fix .` |
| `make seed` | заполнить дашборд тестовыми метриками |
| `make index` | переиндексировать документы (внутри контейнера) |

## HTTP API

| Эндпоинт | Назначение |
|---|---|
| `GET /health` | статус API, Qdrant и Redis |
| `GET /dashboard` | HTML-дашборд аналитики (Basic auth) |
| `GET /stats?period=today\|week\|month` | агрегированные метрики (Basic auth) |
| `POST /webhooks/github`, `POST /webhooks/gitlab` | приём вебхуков Git-провайдеров |

## Обновление базы знаний

Документы кладутся в `bars-kb/docs/source` (документация) и
`bars-kb/docs/support_answers` (решённые обращения). Watcher запускается вместе
с приложением, считает digest, пересобирает `bars-kb/dist` только при реальном
изменении и переключает указатель `LATEST`. Детали и ручные команды
(`bars-kb sync`, `builds`, `rollback` и т.д.) — в
[bars-kb/README.md](bars-kb/README.md) и [docs/kb-updates.md](docs/kb-updates.md).

## Тестирование и линт

```bash
make test        # pytest -q
make lint        # ruff check .
make format      # ruff check --fix .
```

Тесты: `app/tests/` (бот, Jira, скриншоты, автообновление KB, метрики),
`bars-kb/tests/` (пайплайн базы знаний).

## Структура проекта

```text
app/
  main.py            единая точка входа: FastAPI + бот + автообновление KB
  config.py          настройки из .env
  db.py              users / dialog_history / jira_sessions
  kb_autosync.py     watcher пересборки bars-kb/dist
  api/               health, dashboard, webhooks
  bot/
    handlers/        common (start/help/id), admin (загрузка доков), user (диалог)
    services/        ai_service, vision, jira, jira_poller, support_answers, redis_context
    middlewares/     БД и роли
    keyboards/, states/, filters.py
  rag/               мост к RAG-ядру (Retriever)
  integrators/       Git-провайдеры и поллер
bars-kb/             пайплайн базы знаний (парсинг, обогащение, Qdrant) + dist/
docs/                image-queries.md, kb-updates.md
data/                SQLite, кэши vision/fastembed, картинки запросов
```

## Документация

- [bars-kb/README.md](bars-kb/README.md) — сборка базы знаний и Qdrant.
- [docs/kb-updates.md](docs/kb-updates.md) — путь «документ → ответ» и диагностика.
- [docs/image-queries.md](docs/image-queries.md) — обработка скриншотов.
