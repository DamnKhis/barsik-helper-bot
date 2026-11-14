# Бот первой линии поддержки «БАРС-Офис» (RAG MVP)

Хакатон-проект: Telegram-бот отвечает на вопросы сотрудников по документации
БАРС-Офис через RAG (поиск по базе знаний + генерация LLM со ссылкой на
источник). При низкой уверенности бот честно говорит «не нашёл» и предлагает
оператора; эскалация уходит в чат дежурной смены вместе с историей диалога.
Для администратора — веб-дашборд аналитики. База знаний обновляется из
Git-репозитория (GitHub/GitLab).

## Архитектура

```
 Telegram                      FastAPI (RAG-ядро, app/)
    │  POST /ask ────────────►  retriever ──► Qdrant (чанки + вектора)
    │                               │
    │                               └── generator ──► LLM (OpenAI-совместимый API)
    │
    └── эскалация ──► чат дежурных операторов (история из Redis)

 indexer/ (роль 1): .pdf ──► блоки ──► чанки по разделам ──► эмбеддинги ──► Qdrant
 app/analytics/ (роль 4): SQLite логи ──► GET /stats ──► дашборд (Chart.js)
 app/integrations/ (роль 4): Git polling/webhooks ──► авто-реиндексация
```

## Структура репозитория и роли

| Каталог | Роль | Что делать |
|---|---|---|
| `indexer/` | Роль 1 — данные | Дочистить парсинг PDF (скриншоты через VLM), тюнить чанкинг |
| `app/rag/` | Роль 2 — RAG-ядро | Гибридный поиск + реранкер, калибровка уверенности, промпт |
| `bot/` | Роль 3 — бот/UX | Тексты, кнопки, эскалация, сценарии диалога |
| `app/analytics/`, `app/static/`, `qa/`, `app/integrations/` | Роль 4 — QA/DevOps | Дашборд, тест-кейсы, CI, README, демо |

Контракты и правила работы — в `AGENTS.md`; план работ — в `PLAN.md`.

## Стек: для чего какая библиотека

| Библиотека | Зачем | Где |
|---|---|---|
| `pymupdf` (fitz) | Парсинг PDF: текст с координатами и шрифтами → заголовки; `find_tables()` → таблицы; картинки → скриншоты | `indexer/parse_pdf.py` |
| `pdfplumber` | Запасной детектор таблиц, если у fitz не получилось | `indexer/parse_pdf.py` |
| `pytesseract` + tesseract | OCR сканированных страниц | `indexer/parse_pdf.py` |
| `python-docx` | Запасной формат .docx | `indexer/parse_docx.py` |
| `openai` | Единый клиент LLM / эмбеддингов / VLM (любой OpenAI-совместимый провайдер) | `indexer/embeddings.py`, `app/rag/generator.py` |
| `qdrant-client` | Векторная база: чанки + эмбеддинги, поиск по сходству | `indexer/qdrant_client.py`, `app/rag/retriever.py` |
| `redis` | История диалогов для эскалации | `bot/history.py` |
| `fastapi` + `uvicorn` | RAG-ядро: `/ask`, `/stats`, `/dashboard`, вебхуки | `app/main.py` |
| `aiogram` | Telegram-бот | `bot/main.py` |
| `pydantic-settings` | Все настройки из .env в одном месте | `settings.py` |
| `apscheduler` | Поллинг Git-репозитория документации | `app/integrations/reindex.py` |
| `httpx` | HTTP к RAG-ядру из бота и к GitHub/GitLab API | `bot/api_client.py`, `app/integrations/git_provider.py` |
| `sqlite3` (stdlib) | Логи запросов и реиндексаций для дашборда | `app/analytics/` |
| `pytest`, `ruff` | Тесты и линтер | CI, `indexer/tests/`, `app/tests/` |

Тяжёлая альтернатива для сложной вёрстки — `docling`: лучше понимает layout,
но тянет torch. Ставить только если PyMuPDF не справится с реальными файлами.

## Быстрый старт

```bash
# 1. Настроить окружение
cp .env.example .env
# заполнить BOT_TOKEN (от @BotFather), LLM_API_KEY, OPERATOR_CHAT_ID

# 2. Поднять инфраструктуру
docker compose up -d --build

# 3. Положить PDF документации в ./docs/ и проиндексировать
docker compose exec api python -m indexer.run_index --docs-dir /app/docs

# 4. Проверить API и открыть дашборд
curl -X POST http://localhost:8000/ask -H 'Content-Type: application/json' \
     -d '{"text": "Как считается аванс за командировку?", "user_id": "test"}'
# дашборд: http://localhost:8000/dashboard
```

Без Docker (для разработки): `pip install -r requirements.txt`, поднять только
`qdrant` и `redis` (например, в docker), затем `uvicorn app.main:app --reload`
и `python -m bot.main`.

## Команды (Makefile)

```bash
make up      # поднять весь стек
make index   # переиндексировать ./docs
make seed    # наполнить дашборд демо-данными (перед показом жюри)
make lint    # ruff
make test    # pytest
make logs    # логи api и бота
```

## Сценарий демо (3–5 минут)

1. `/start` — приветствие и кнопки тем.
2. Базовый вопрос («Как считается аванс за командировку?») — ответ с цитатой
   и «Источник: …, раздел …».
3. Подвох («Можно ли фильтровать по !> 200?») — бот корректно отрицает.
4. Вопрос вне базы — честное «не нашёл», предложение оператора.
5. Эскалация — в чате дежурных появляется тикет с историей диалога.
6. Дашборд `/dashboard` — живые графики (перед демо запустить `make seed`).
7. Пуш в репозиторий документации → бот уже отвечает по новому разделу.

## Чек-лист соответствия брифу

- RAG по документации БАРС-Офис с источником и разделом в ответе — `app/rag/`.
- Честное «не знаю» при низкой уверенности — `app/rag/confidence.py`.
- Эскалация с историей диалога — `bot/escalation.py`, история в Redis.
- Аналитика для администратора — `app/analytics/`, дашборд `/dashboard`.
- Интеграция с GitLab/GitHub — `app/integrations/` (поллинг + вебхуки).
- Docker — `docker-compose.yml`, чистый код — `ruff`, тесты — `pytest`.
