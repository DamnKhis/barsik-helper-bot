"""Минимальный асинхронный клиент Jira REST API v2.

Поддержка нужна для двустороннего диалога: бот создаёт задачу, дописывает
сообщения пользователя комментариями и забирает комментарии/статус поддержки.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Комментарии, которые бот пишет сам, помечаем префиксом: по нему поллер
# отличает «свои» комментарии, даже если поддержка пишет с того же Jira-аккаунта.
BOT_COMMENT_PREFIX = "[BARS-"
BOT_USER_PREFIX = "[BARS-USER:"
BOT_TRANSCRIPT_PREFIX = "[BARS-TRANSCRIPT]"


class JiraError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class JiraNotFound(JiraError):
    pass


@dataclass(slots=True)
class JiraIssueState:
    key: str
    status_name: str
    status_category: str
    summary: str = ""


@dataclass(slots=True)
class JiraComment:
    id: int
    author: str
    author_email: str
    body: str


@dataclass(slots=True)
class JiraAttachment:
    id: str
    filename: str
    mime_type: str
    size: int
    content_url: str


def _comment_id(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _jira_text(value: Any) -> str:
    """Jira Cloud v3 может вернуть ADF-объект вместо строки."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_jira_text(item) for item in value) if part)
    if isinstance(value, dict):
        if value.get("type") == "text":
            return str(value.get("text") or "")
        if value.get("type") == "hardBreak":
            return "\n"
        content = value.get("content")
        if content is not None:
            return _jira_text(content)
        text = value.get("text")
        if text is not None:
            return str(text)
    return ""


def format_user_label(user: Any) -> str:
    name = getattr(user, "full_name", None) or getattr(user, "username", None) or "пользователь"
    username = getattr(user, "username", None)
    suffix = f" (@{username})" if username else ""
    return f"{name}{suffix}, Telegram ID {user.telegram_id}"


def format_transcript(context: list[dict], user: Any) -> str:
    lines = [
        "Обращение пользователя в поддержку «БАРС-Офис».",
        f"Пользователь: {format_user_label(user)}",
        "",
        "Диалог с ИИ до вызова оператора:",
    ]
    for item in context[-40:]:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        role = "Пользователь" if item.get("role") == "user" else "ИИ"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)[:30000]


def format_user_comment(text: str, user: Any) -> str:
    marker = f"{BOT_USER_PREFIX}{user.telegram_id}]"
    return f"{marker}\n{format_user_label(user)}:\n{text}"[:30000]


class JiraClient:
    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        token: str | None = None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.jira_base_url).rstrip("/")
        self.email = email if email is not None else settings.jira_email
        self.token = token if token is not None else settings.jira_api_token
        self.timeout = timeout
        self.transport = transport
        self._reporter_field: dict[str, str] | None = None
        self._reporter_fetched = False

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _http(self) -> httpx.AsyncClient:
        if not self.configured:
            raise JiraError("Jira не настроена: нужны JIRA_BASE_URL и JIRA_API_TOKEN/JIRO_API_KEY")
        headers = {"Accept": "application/json"}
        auth: tuple[str, str] | None = None
        if self.email:
            auth = (self.email, self.token)
        else:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            auth=auth,
            timeout=self.timeout,
            transport=self.transport,
            follow_redirects=True,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with self._http() as client:
            response = await client.request(method, path, **kwargs)
        if response.status_code == 404:
            raise JiraNotFound(f"Jira: объект не найден ({path})", 404)
        if response.status_code >= 400:
            detail = response.text[:500]
            raise JiraError(
                f"Jira API {response.status_code}: {detail}",
                response.status_code,
            )
        if not response.content:
            return None
        return response.json()

    async def _get_reporter_field(self) -> dict[str, str] | None:
        """Jira Cloud часто требует заполнить «Автор»; ставим аккаунт бота."""
        if self._reporter_fetched:
            return self._reporter_field
        self._reporter_fetched = True
        data = await self._request("GET", "/rest/api/2/myself")
        data = data or {}
        account_id = str(data.get("accountId") or "")
        name = str(data.get("name") or "")
        if account_id:
            self._reporter_field = {"id": account_id}
        elif name:
            self._reporter_field = {"name": name}
        return self._reporter_field

    async def create_issue(
        self,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str,
    ) -> tuple[str, str]:
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary[:255],
            "description": description[:30000],
            "issuetype": {"name": issue_type},
            "labels": ["barsik-bot"],
        }
        reporter = await self._get_reporter_field()
        if reporter:
            fields["reporter"] = reporter
        data = await self._request("POST", "/rest/api/2/issue", json={"fields": fields})
        key = str((data or {}).get("key") or "")
        if not key:
            raise JiraError("Jira не вернула ключ созданной задачи")
        return key, f"{self.base_url}/browse/{key}"

    async def add_comment(self, issue_key: str, body: str) -> int:
        path = f"/rest/api/2/issue/{quote(issue_key, safe='')}/comment"
        data = await self._request("POST", path, json={"body": body[:30000]})
        return _comment_id((data or {}).get("id"))

    async def add_attachment(
        self,
        issue_key: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> int:
        path = f"/rest/api/2/issue/{quote(issue_key, safe='')}/attachments"
        data = await self._request(
            "POST",
            path,
            files={"file": (filename, content, content_type)},
            headers={"X-Atlassian-Token": "no-check"},
        )
        if isinstance(data, list) and data:
            return _comment_id(data[0].get("id"))
        return 0

    async def list_attachments(self, issue_key: str) -> list[JiraAttachment]:
        path = f"/rest/api/2/issue/{quote(issue_key, safe='')}"
        data = await self._request("GET", path, params={"fields": "attachment"})
        fields = (data or {}).get("fields") or {}
        attachments: list[JiraAttachment] = []
        for raw in fields.get("attachment") or []:
            attachments.append(
                JiraAttachment(
                    id=str(raw.get("id") or ""),
                    filename=str(raw.get("filename") or ""),
                    mime_type=str(raw.get("mimeType") or ""),
                    size=int(raw.get("size") or 0),
                    content_url=str(raw.get("content") or ""),
                )
            )
        return attachments

    async def download(self, url: str) -> bytes:
        async with self._http() as client:
            response = await client.get(url, headers={"Accept": "*/*"})
        if response.status_code >= 400:
            raise JiraError(
                f"Jira не отдала вложение {response.status_code}",
                response.status_code,
            )
        return response.content

    async def get_issue_state(self, issue_key: str) -> JiraIssueState:
        path = f"/rest/api/2/issue/{quote(issue_key, safe='')}"
        data = await self._request("GET", path, params={"fields": "status,summary"})
        fields = (data or {}).get("fields") or {}
        status = fields.get("status") or {}
        category = status.get("statusCategory") or {}
        return JiraIssueState(
            key=str((data or {}).get("key") or issue_key),
            status_name=str(status.get("name") or ""),
            status_category=str(category.get("key") or ""),
            summary=str(fields.get("summary") or ""),
        )

    async def resolve_issue(self, issue_key: str) -> bool:
        """Перевести задачу в завершающий статус.

        Ищем переход, ведущий в категорию ``done`` (или статус из списка
        завершающих), и применяем его. ``True`` — переход выполнен.
        """
        path = f"/rest/api/2/issue/{quote(issue_key, safe='')}/transitions"
        data = await self._request("GET", path)
        transitions = (data or {}).get("transitions") or []
        target: dict[str, Any] | None = None
        for transition in transitions:
            to_status = transition.get("to") or {}
            category = str((to_status.get("statusCategory") or {}).get("key") or "").lower()
            name = str(to_status.get("name") or "").strip().lower()
            if category == "done" or name in DONE_STATUS_NAMES:
                target = transition
                break
        if target is None:
            logger.warning("В Jira %s нет перехода в завершающий статус", issue_key)
            return False
        await self._request("POST", path, json={"transition": {"id": str(target.get("id"))}})
        return True

    async def list_comments(self, issue_key: str, since_id: int = 0) -> list[JiraComment]:
        path = f"/rest/api/2/issue/{quote(issue_key, safe='')}/comment"
        comments: list[JiraComment] = []
        start_at = 0
        while True:
            data = await self._request(
                "GET",
                path,
                params={"startAt": start_at, "maxResults": 100, "orderBy": "created"},
            )
            data = data or {}
            batch = data.get("comments") or []
            for raw in batch:
                comment_id = _comment_id(raw.get("id"))
                if comment_id <= since_id:
                    continue
                author = raw.get("author") or {}
                comments.append(
                    JiraComment(
                        id=comment_id,
                        author=str(author.get("displayName") or author.get("name") or "Поддержка"),
                        author_email=str(author.get("emailAddress") or ""),
                        body=_jira_text(raw.get("body")).strip(),
                    )
                )
            total = int(data.get("total") or 0)
            start_at += len(batch)
            if not batch or start_at >= total:
                break
        return comments


DONE_STATUS_NAMES = {
    "done",
    "closed",
    "resolved",
    "готово",
    "закрыто",
    "выполнено",
}


def is_done_state(state: JiraIssueState) -> bool:
    if state.status_category.lower() == "done":
        return True
    return state.status_name.strip().lower() in DONE_STATUS_NAMES
