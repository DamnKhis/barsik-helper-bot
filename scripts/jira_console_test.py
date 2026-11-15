"""Консольный тест Jira-поддержки: создаёт задачу и позволяет общаться с оператором.

Запуск из корня репозитория:
    python scripts/jira_console_test.py
    python scripts/jira_console_test.py --issue KAN-123   # подключиться к существующей
    python scripts/jira_console_test.py --interval 10     # чаще опрашивать Jira

Скрипт ничего не знает про Telegram: только Jira. Токен и URL берутся из .env.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot.services.jira import (  # noqa: E402
    JiraClient,
    JiraComment,
    JiraNotFound,
    is_done_state,
)
from app.config import settings  # noqa: E402

USER_LABEL = "Пользователь (консольный тест)"
CONSOLE_PREFIX = "[BARS-CONSOLE]"


def _print_comment(comment: JiraComment, own_email: str) -> None:
    if comment.body.startswith(CONSOLE_PREFIX) or comment.body.startswith("[BARS-"):
        return
    print(f"\n[оператор: {comment.author}]\n{comment.body}\n", flush=True)


def _print_rules(issue_key: str, issue_url: str) -> None:
    print(
        "\n"
        "=== Jira console test ===\n"
        f"Задача: {issue_key}\n"
        f"Ссылка: {issue_url}\n"
        "Пиши сообщение и Enter — оно уйдёт комментарием в Jira.\n"
        "Ответы оператора печатаются автоматически.\n"
        "Команды: :q — выход, :url — ссылка, :status — статус, :help — подсказка.\n"
    )


async def _create_issue(client: JiraClient, summary: str, description: str) -> tuple[str, str]:
    return await client.create_issue(
        project_key=settings.jira_project_key,
        issue_type=settings.jira_issue_type,
        summary=summary,
        description=description,
    )


async def _main_async(args: argparse.Namespace) -> int:
    if not settings.jira_enabled:
        print(
            "Jira не настроена. Нужны JIRA_BASE_URL и JIRA_API_TOKEN/JIRO_API_KEY в .env",
            file=sys.stderr,
        )
        return 2

    client = JiraClient()
    own_email = settings.jira_email.strip().lower()

    if args.issue:
        issue_key = args.issue
        try:
            state = await client.get_issue_state(issue_key)
        except JiraNotFound:
            print(f"Задача {issue_key} не найдена в Jira", file=sys.stderr)
            return 2
        issue_url = f"{client.base_url}/browse/{issue_key}"
        print(f"Подключаюсь к существующей задаче: {issue_key} (статус: {state.status_name})")
    else:
        if not settings.jira_create_ready:
            print("Для создания задачи нужен JIRA_PROJECT_KEY в .env", file=sys.stderr)
            return 2
        issue_key, issue_url = await _create_issue(
            client,
            summary=args.summary,
            description=args.description,
        )
        print(f"Создана задача: {issue_key}")

    _print_rules(issue_key, issue_url)

    existing = await client.list_comments(issue_key, since_id=0)
    last_id = 0
    for comment in existing:
        last_id = max(last_id, comment.id)
        _print_comment(comment, own_email)

    stop = asyncio.Event()
    last_id_box = [last_id]

    async def poll_loop() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1, args.interval))
                return
            except asyncio.TimeoutError:
                pass

            try:
                state = await client.get_issue_state(issue_key)
                comments = await client.list_comments(issue_key, since_id=last_id_box[0])
                for comment in comments:
                    last_id_box[0] = max(last_id_box[0], comment.id)
                    _print_comment(comment, own_email)
                if is_done_state(state):
                    print(f"\nЗадача {issue_key} закрыта. Завершаю.\n", flush=True)
                    stop.set()
                    return
            except Exception as exc:  # noqa: BLE001
                print(f"\n[ошибка опроса Jira] {exc}\n", flush=True)

    poll_task = asyncio.create_task(poll_loop())
    print(f"Опрос Jira каждые {max(1, args.interval)} с. Жду сообщений...\n")

    try:
        while not stop.is_set():
            line = await asyncio.to_thread(input, "> ")
            line = line.strip()
            if not line:
                continue
            if line == ":q":
                break
            if line == ":url":
                print(issue_url)
                continue
            if line == ":help":
                print(":q — выход, :url — ссылка, :status — статус задачи")
                continue
            if line == ":status":
                state = await client.get_issue_state(issue_key)
                print(f"{issue_key}: {state.status_name} ({state.status_category})")
                continue

            comment_id = await client.add_comment(issue_key, f"{CONSOLE_PREFIX}\n{USER_LABEL}:\n{line}")
            last_id_box[0] = max(last_id_box[0], comment_id)
            print(f"[отправлено в {issue_key}]")
    except (KeyboardInterrupt, EOFError):
        print("\nПрерываю...")
    finally:
        stop.set()
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass

    print(f"Готово. Задача: {issue_url}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Консольный тест диалога с поддержкой через Jira")
    parser.add_argument("--issue", help="не создавать задачу, а подключиться к существующей (например KAN-123)")
    parser.add_argument(
        "--interval",
        type=int,
        default=1,
        help="интервал опроса Jira в секундах (по умолчанию 1)",
    )
    parser.add_argument("--summary", default="[BARS] Консольный тест поддержки", help="заголовок новой задачи")
    parser.add_argument(
        "--description",
        default="Тестовая задача из scripts/jira_console_test.py. Можно спокойно удалить.",
        help="описание новой задачи",
    )
    return parser.parse_args(argv)


def main() -> int:
    return asyncio.run(_main_async(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
