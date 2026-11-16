"""Фоновый опрос Jira: комментарии поддержки -> Telegram, закрытие задачи -> конец сессии."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.exc import DBAPIError

from app.bot.keyboards.reply import user_main_menu
from app.bot.services.jira import (
    BOT_COMMENT_PREFIX,
    JiraClient,
    JiraComment,
    JiraNotFound,
    is_done_state,
)
from app.bot.services.redis_context import RedisContextService
from app.bot.services.support_answers import is_useful_status, save_support_answer
from app.config import settings
from app.db import (
    JiraSession,
    async_session_maker,
    close_jira_session,
    list_active_jira_sessions,
    set_jira_last_comment,
)

logger = logging.getLogger(__name__)

TELEGRAM_SAFE_LIMIT = 3800


class JiraPoller:
    def __init__(
        self,
        bot: Bot,
        redis_context: RedisContextService,
        client: JiraClient | None = None,
        interval: int | None = None,
    ) -> None:
        self.bot = bot
        self.redis_context = redis_context
        self.client = client or JiraClient()
        self.interval = interval or max(1, settings.jira_poll_interval_sec)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="jira-poller")
        logger.info("Jira poller запущен, интервал %s с", self.interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Ошибка фоновой синхронизации с Jira: %s", exc)
                logger.debug("Полный трейсбек поллера Jira", exc_info=True)
            await asyncio.sleep(self.interval)

    async def poll_once(self) -> None:
        """Одна иттерация. При обрыве соединения к БД пробуем ещё раз."""
        for attempt in range(2):
            try:
                await self._poll_once()
                return
            except DBAPIError as exc:
                if attempt == 0:
                    logger.debug("Повтор опроса Jira из-за БД: %s", exc)
                    await asyncio.sleep(1)
                    continue
                raise

    async def _poll_once(self) -> None:
        async with async_session_maker() as session:
            sessions = await list_active_jira_sessions(session)
            for jira_session in sessions:
                try:
                    await self._sync_session(session, jira_session)
                except JiraNotFound:
                    await close_jira_session(session, jira_session)
                    await self._send(
                        jira_session.user_id,
                        "Обращение больше не найдено в поддержке. Диалог завершён.",
                        reply_markup=user_main_menu(),
                    )
                except Exception as exc:
                    logger.warning(
                        "Не удалось синхронизировать Jira %s (user_id=%s): %s",
                        jira_session.issue_key,
                        jira_session.user_id,
                        exc,
                    )
                    logger.debug("Полный трейсбек синхронизации Jira", exc_info=True)
            await session.commit()

    async def _sync_session(self, session, jira_session: JiraSession) -> None:
        state = await self.client.get_issue_state(jira_session.issue_key)
        comments = await self.client.list_comments(
            jira_session.issue_key,
            since_id=jira_session.last_comment_id,
        )
        for comment in comments:
            await self._deliver_comment(jira_session, comment)
            await set_jira_last_comment(session, jira_session, comment.id)

        if is_useful_status(state.status_name):
            await self._save_useful_answer(jira_session, state)

        if is_done_state(state):
            await close_jira_session(session, jira_session)
            await self._delete_wait_message(jira_session.user_id)
            await self._send(
                jira_session.user_id,
                "Обращение закрыто. Можете задать новый вопрос.",
                reply_markup=user_main_menu(),
            )

    async def _save_useful_answer(self, jira_session: JiraSession, state) -> None:
        """Тикет помечен полезным — выгружаем переписку в Markdown для KB."""
        try:
            comments = await self.client.list_comments(jira_session.issue_key, since_id=0)
            issue_url = jira_session.issue_url or (
                f"{self.client.base_url}/browse/{jira_session.issue_key}"
            )
            saved = await save_support_answer(
                self.client,
                jira_session.issue_key,
                issue_url,
                getattr(state, "summary", ""),
                comments,
            )
        except Exception as exc:
            logger.warning(
                "Не удалось сохранить полезный ответ %s: %s", jira_session.issue_key, exc
            )
            return
        if saved is not None:
            logger.info("Полезный ответ %s -> %s", jira_session.issue_key, saved)

    async def _delete_wait_message(self, user_id: int) -> None:
        wait_id = await self.redis_context.pop_wait_message(user_id)
        if wait_id is None:
            return
        try:
            await self.bot.delete_message(user_id, wait_id)
        except TelegramBadRequest as exc:
            logger.debug("Не удалось удалить сообщение ожидания: %s", exc)

    async def _deliver_comment(self, jira_session: JiraSession, comment: JiraComment) -> None:
        if self._is_own_comment(comment):
            return
        if not comment.body:
            return
        await self._send(jira_session.user_id, comment.body)
        await self._delete_wait_message(jira_session.user_id)
        await self.redis_context.append_message(
            jira_session.user_id,
            "assistant",
            f"Поддержка: {comment.body}",
        )

    @staticmethod
    def _is_own_comment(comment: JiraComment) -> bool:
        # Не фильтруем по email: поддержка может писать с того же аккаунта, что и бот.
        return comment.body.startswith(BOT_COMMENT_PREFIX)

    async def _send(self, user_id: int, text: str, reply_markup=None) -> None:
        if len(text) > TELEGRAM_SAFE_LIMIT:
            text = text[: TELEGRAM_SAFE_LIMIT - 1] + "…"
        try:
            await self.bot.send_message(user_id, text, reply_markup=reply_markup)
        except TelegramBadRequest as exc:
            logger.warning("Не удалось отправить сообщение Jira пользователю %s: %s", user_id, exc)
