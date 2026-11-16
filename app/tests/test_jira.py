from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import db as db_module
from app.bot.services import jira_poller as jira_poller_module
from app.bot.services.jira import (
    JiraClient,
    JiraComment,
    JiraIssueState,
    format_transcript,
    is_done_state,
)


def test_create_issue_and_add_comment() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/rest/api/2/myself":
            return httpx.Response(200, json={"accountId": "acc-1", "displayName": "Бот"})
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "SUP-1", "self": "https://jira/SUP-1"})
        if request.url.path == "/rest/api/2/issue/SUP-1/comment":
            return httpx.Response(201, json={"id": "10"})
        return httpx.Response(404)

    client = JiraClient(
        base_url="https://jira.example.com",
        email="bot@example.com",
        token="secret",
        transport=httpx.MockTransport(handler),
    )

    key, url = asyncio.run(client.create_issue("SUP", "Task", "БАРС-Офис: тест", "Описание"))
    comment_id = asyncio.run(client.add_comment(key, "Новое сообщение"))

    assert key == "SUP-1"
    assert url == "https://jira.example.com/browse/SUP-1"
    assert comment_id == 10
    assert all(req.headers["authorization"].startswith("Basic ") for req in seen)
    create_request = next(req for req in seen if req.url.path == "/rest/api/2/issue")
    payload = json.loads(create_request.content)
    assert payload["fields"]["project"] == {"key": "SUP"}
    assert payload["fields"]["summary"] == "БАРС-Офис: тест"
    assert payload["fields"]["reporter"] == {"id": "acc-1"}


def test_get_state_and_comments_use_adf_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/2/issue/SUP-2":
            return httpx.Response(
                200,
                json={
                    "key": "SUP-2",
                    "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}},
                },
            )
        if request.url.path == "/rest/api/2/issue/SUP-2/comment":
            return httpx.Response(
                200,
                json={
                    "total": 2,
                    "comments": [
                        {
                            "id": "7",
                            "author": {"displayName": "Иван", "emailAddress": "ivan@example.com"},
                            "body": "Простой текст",
                        },
                        {
                            "id": "8",
                            "author": {"displayName": "Пётр"},
                            "body": {
                                "type": "doc",
                                "content": [
                                    {"type": "paragraph", "content": [{"type": "text", "text": "ADF текст"}]}
                                ],
                            },
                        },
                    ],
                },
            )
        return httpx.Response(404)

    client = JiraClient(
        base_url="https://jira.example.com",
        token="pat",
        transport=httpx.MockTransport(handler),
    )

    state = asyncio.run(client.get_issue_state("SUP-2"))
    comments = asyncio.run(client.list_comments("SUP-2"))

    assert state.status_name == "Done"
    assert is_done_state(state) is True
    assert [c.id for c in comments] == [7, 8]
    assert comments[0].body == "Простой текст"
    assert comments[1].body == "ADF текст"


def test_transcript_contains_dialog() -> None:
    context = [
        {"role": "user", "content": "Как оформить пропуск?"},
        {"role": "assistant", "content": "Нужен паспорт."},
    ]
    user = SimpleNamespace(telegram_id=42, full_name="Иван", username="ivan")
    text = format_transcript(context, user)
    assert "Иван (@ivan), Telegram ID 42" in text
    assert "Пользователь: Как оформить пропуск?" in text
    assert "ИИ: Нужен паспорт." in text


@pytest.fixture()
def session_maker(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'jira.db'}",
        poolclass=NullPool,
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def create_tables() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(db_module.Base.metadata.create_all)

    asyncio.run(create_tables())
    monkeypatch.setattr(db_module, "async_session_maker", maker)
    monkeypatch.setattr(jira_poller_module, "async_session_maker", maker)
    monkeypatch.setattr(jira_poller_module.settings, "jira_email", "bot@example.com")

    yield maker
    asyncio.run(engine.dispose())


def test_poller_delivers_support_comments_and_closes_session(session_maker) -> None:
    class FakeClient:
        async def get_issue_state(self, issue_key: str) -> JiraIssueState:
            return JiraIssueState(key=issue_key, status_name="Done", status_category="done")

        async def list_comments(self, issue_key: str, since_id: int = 0) -> list[JiraComment]:
            if since_id:
                return []
            return [
                # Поддержка отвечает с того же Jira-аккаунта, что и бот: доставляем.
                JiraComment(
                    id=1,
                    author="Тест Бот",
                    author_email="bot@example.com",
                    body="Здравствуйте, помогу.",
                ),
                JiraComment(
                    id=2,
                    author="Тест Бот",
                    author_email="bot@example.com",
                    body="[BARS-USER:42]\nЭто наш комментарий",
                ),
            ]

    class FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def send_message(self, user_id: int, text: str, reply_markup=None) -> None:
            self.sent.append((user_id, text))

    class FakeRedisContext:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str, str]] = []

        async def append_message(self, user_id: int, role: str, content: str) -> None:
            self.messages.append((user_id, role, content))

        async def pop_wait_message(self, user_id: int):
            return None

    async def prepare() -> None:
        async with db_module.async_session_maker() as session:
            session.add(
                db_module.User(telegram_id=42, role="user", full_name="Иван", username="ivan")
            )
            await session.flush()
            await db_module.create_jira_session(session, 42, "SUP-5", "https://jira/browse/SUP-5")
            await session.commit()

    asyncio.run(prepare())

    bot = FakeBot()
    redis = FakeRedisContext()
    poller = jira_poller_module.JiraPoller(
        bot=bot,
        redis_context=redis,
        client=FakeClient(),
        interval=1,
    )
    asyncio.run(poller.poll_once())

    assert any("Здравствуйте, помогу." in text for _, text in bot.sent)
    assert not any("Это наш комментарий" in text for _, text in bot.sent)
    assert any("закрыто" in text for _, text in bot.sent)
    assert redis.messages == [(42, "assistant", "Поддержка: Здравствуйте, помогу.")]

    async def state() -> tuple[str, int]:
        async with db_module.async_session_maker() as session:
            row = await db_module.get_active_jira_session(session, 42)
            assert row is None
            closed = await session.scalar(
                db_module.select(db_module.JiraSession).where(db_module.JiraSession.issue_key == "SUP-5")
            )
            return closed.status, closed.last_comment_id

    assert asyncio.run(state()) == ("closed", 2)


def test_call_operator_posts_ai_history_to_jira(monkeypatch) -> None:
    from app.bot.handlers import user as user_module

    monkeypatch.setattr(user_module.settings, "jira_base_url", "https://jira.example.com")
    monkeypatch.setattr(user_module.settings, "jira_api_token", "tok")
    monkeypatch.setattr(user_module.settings, "jira_project_key", "SUP")
    monkeypatch.setattr(user_module.settings, "jira_issue_type", "Задача")

    class FakeClient:
        def __init__(self) -> None:
            self.created: dict = {}
            self.comments: list[tuple[str, str]] = []

        async def create_issue(self, project_key, issue_type, summary, description):
            self.created = {
                "project_key": project_key,
                "issue_type": issue_type,
                "summary": summary,
                "description": description,
            }
            return "SUP-9", "https://jira.example.com/browse/SUP-9"

        async def add_comment(self, issue_key, body):
            self.comments.append((issue_key, body))
            return 555

    class FakeMessage:
        def __init__(self) -> None:
            self.answers: list[str] = []

        async def edit_reply_markup(self, reply_markup=None):
            return None

        async def answer(self, text, reply_markup=None):
            self.answers.append(text)
            return SimpleNamespace(message_id=999)

    class FakeCallback:
        data = "call_operator:tok"

        def __init__(self) -> None:
            self.message = FakeMessage()
            self.alerts: list[str] = []

        async def answer(self, text, show_alert: bool = False):
            self.alerts.append(text)

    class FakeState:
        async def get_data(self) -> dict:
            return {"operator_token": "tok", "operator_question": "Как оформить пропуск?"}

        async def update_data(self, **kwargs) -> None:
            return None

    class FakeRedis:
        async def get_context(self, user_id: int) -> list:
            return [
                {"role": "user", "content": "Как оформить пропуск?"},
                {"role": "assistant", "content": "Нужен паспорт."},
            ]

        async def get_wait_message(self, user_id: int):
            return None

        async def set_wait_message(self, user_id: int, message_id: int) -> None:
            return None

    session = SimpleNamespace()
    row = SimpleNamespace(user_id=42, issue_key="SUP-9", last_comment_id=0)
    monkeypatch.setattr(user_module, "get_active_jira_session", AsyncMock(return_value=None))
    monkeypatch.setattr(
        user_module, "create_jira_session", AsyncMock(return_value=row)
    )
    watermark = AsyncMock()
    monkeypatch.setattr(user_module, "set_jira_last_comment", watermark)

    client = FakeClient()
    callback = FakeCallback()
    user = SimpleNamespace(telegram_id=42, full_name="Иван", username="ivan")

    asyncio.run(
        user_module.call_operator(
            callback, FakeState(), session, user, FakeRedis(), client
        )
    )

    assert client.created["summary"] == "БАРС-Офис: Как оформить пропуск?"
    assert "Полная история диалога" in client.created["description"]
    assert len(client.comments) == 1
    body = client.comments[0][1]
    assert body.startswith("[BARS-TRANSCRIPT]")
    assert "Как оформить пропуск?" in body
    assert "Нужен паспорт." in body
    watermark.assert_awaited_once_with(session, row, 555)


def test_add_attachment_posts_multipart() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/2/issue/SUP-3/attachments":
            assert request.headers["x-atlassian-token"] == "no-check"
            assert request.headers["content-type"].startswith("multipart/form-data")
            assert b"jpeg-bytes" in request.content
            return httpx.Response(200, json=[{"id": "900", "filename": "photo.jpg"}])
        return httpx.Response(404)

    client = JiraClient(
        base_url="https://jira.example.com",
        email="bot@example.com",
        token="secret",
        transport=httpx.MockTransport(handler),
    )
    attachment_id = asyncio.run(
        client.add_attachment("SUP-3", "photo.jpg", b"jpeg-bytes", "image/jpeg")
    )
    assert attachment_id == 900




def test_handle_question_attaches_photo_to_jira(tmp_path, monkeypatch) -> None:
    from app.bot.handlers import user as user_module
    from app.bot.services import image_store as image_store_module

    monkeypatch.setattr(image_store_module.settings, "image_query_dir", str(tmp_path))
    monkeypatch.setattr(
        user_module,
        "get_active_jira_session",
        AsyncMock(return_value=SimpleNamespace(issue_key="SUP-1", last_comment_id=0)),
    )
    watermark = AsyncMock()
    monkeypatch.setattr(user_module, "set_jira_last_comment", watermark)

    class FakeBot:
        async def download(self, file, destination) -> None:
            Path(destination).write_bytes(b"img-bytes")

        async def send_chat_action(self, *args, **kwargs) -> None:
            return None

    class FakeClient:
        def __init__(self) -> None:
            self.attachments: list[tuple] = []
            self.comments: list[tuple[str, str]] = []

        async def add_attachment(self, issue_key, filename, content, content_type):
            self.attachments.append((issue_key, filename, content, content_type))
            return 1

        async def add_comment(self, issue_key, body):
            self.comments.append((issue_key, body))
            return 2

    message = SimpleNamespace(
        text=None,
        caption="вот скрин",
        photo=[SimpleNamespace(file_id="f1", file_size=9)],
        document=None,
        message_id=7,
        chat=SimpleNamespace(id=1),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=5)),
        answer_photo=AsyncMock(),
    )
    user = SimpleNamespace(telegram_id=42, full_name="Иван", username="ivan")
    redis = SimpleNamespace(
        get_context=AsyncMock(return_value=[]),
        append_pair=AsyncMock(),
        append_message=AsyncMock(),
        get_wait_message=AsyncMock(return_value=None),
        set_wait_message=AsyncMock(),
    )
    state = SimpleNamespace(
        clear=AsyncMock(),
        update_data=AsyncMock(),
        get_data=AsyncMock(return_value={}),
    )
    client = FakeClient()

    asyncio.run(
        user_module.handle_question(
            message,
            state,
            SimpleNamespace(),
            user,
            redis,
            SimpleNamespace(),
            bot=FakeBot(),
            jira_client=client,
        )
    )

    assert len(client.attachments) == 1
    issue_key, filename, content, content_type = client.attachments[0]
    assert issue_key == "SUP-1"
    assert filename.endswith(".jpg")
    assert content == b"img-bytes"
    assert content_type == "image/jpeg"
    assert client.comments and "вот скрин" in client.comments[0][1]
    watermark.assert_awaited_once()
def test_poller_saves_useful_answer_to_support_answers(session_maker, tmp_path, monkeypatch) -> None:
    from app.bot.services import support_answers as support_answers_module

    monkeypatch.setattr(
        support_answers_module.settings,
        "support_answers_dir",
        str(tmp_path / "support_answers"),
    )

    class FakeClient:
        base_url = "https://jira.example.com"

        async def get_issue_state(self, issue_key: str) -> JiraIssueState:
            return JiraIssueState(
                key=issue_key,
                status_name="Полезно",
                status_category="indeterminate",
                summary="Как оформить пропуск?",
            )

        async def list_comments(self, issue_key: str, since_id: int = 0) -> list[JiraComment]:
            return [
                JiraComment(
                    id=1,
                    author="Иван",
                    author_email="ivan@example.com",
                    body="[BARS-USER:42]\nИван (@ivan), Telegram ID 42:\nКак оформить пропуск?",
                ),
                JiraComment(
                    id=2,
                    author="Поддержка",
                    author_email="support@example.com",
                    body="Нужен паспорт и заявка от руководителя.",
                ),
            ]

    class FakeBot:
        async def send_message(self, user_id: int, text: str, reply_markup=None) -> None:
            return None

    class FakeRedisContext:
        async def append_message(self, user_id: int, role: str, content: str) -> None:
            return None

        async def pop_wait_message(self, user_id: int):
            return None

    async def prepare() -> None:
        async with db_module.async_session_maker() as session:
            session.add(db_module.User(telegram_id=42, role="user", full_name="Иван"))
            await session.flush()
            await db_module.create_jira_session(
                session, 42, "KAN-77", "https://jira.example.com/browse/KAN-77"
            )
            await session.commit()

    asyncio.run(prepare())

    poller = jira_poller_module.JiraPoller(
        bot=FakeBot(),
        redis_context=FakeRedisContext(),
        client=FakeClient(),
        interval=1,
    )
    asyncio.run(poller.poll_once())

    saved = tmp_path / "support_answers" / "KAN-77.md"
    assert saved.is_file()
    content = saved.read_text(encoding="utf-8")
    assert "Как оформить пропуск?" in content
    assert "Нужен паспорт и заявка от руководителя." in content


def test_poller_deletes_wait_message_after_support_reply() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.deleted: list[tuple[int, int]] = []

        async def delete_message(self, chat_id: int, message_id: int) -> None:
            self.deleted.append((chat_id, message_id))

    class FakeRedis:
        async def pop_wait_message(self, user_id: int):
            return 77

    bot = FakeBot()
    poller = jira_poller_module.JiraPoller(
        bot=bot,
        redis_context=FakeRedis(),
        client=None,
        interval=1,
    )

    asyncio.run(poller._delete_wait_message(42))

    assert bot.deleted == [(42, 77)]
