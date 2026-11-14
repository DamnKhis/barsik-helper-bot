"""Эскалация на оператора. Роль 3.

Требование брифа: при запросе оператора переслать вопрос И историю диалога.
MVP: форматированный тикет в чат дежурной смены (OPERATOR_CHAT_ID из .env).
TODO(роль 3, +баллы): инлайн-кнопки «взять в работу»/«решено», ответ оператора
обратным сообщением в чат пользователя.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from redis.asyncio import Redis

from bot.history import load_history
from settings import get_settings

logger = logging.getLogger(__name__)

OPERATOR_MARKERS = ("оператор", "человек", "специалист", "живой")


def wants_operator(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in OPERATOR_MARKERS)


def render_ticket(user_id: int | str, last_question: str, history: list[dict]) -> str:
    lines = [
        "Эскалация: пользователь " + str(user_id),
        "",
        "Последний вопрос: " + last_question,
        "",
        "История диалога:",
    ]
    for message in history:
        who = "Пользователь" if message["role"] == "user" else "Бот"
        lines.append(f"— {who}: {message['content']}")
    return "\n".join(lines)


async def escalate_to_operator(
    bot: Bot, redis: Redis, user_id: int | str, chat_id: int, last_question: str
) -> None:
    settings = get_settings()
    history = await load_history(redis, user_id)
    ticket = render_ticket(user_id, last_question, history)

    if settings.operator_chat_id:
        try:
            await bot.send_message(settings.operator_chat_id, ticket)
        except Exception as exc:  # noqa: BLE001 — Telegram мог отклонить чат/права
            logger.error("Не удалось отправить тикет операторам: %s", exc)
            await bot.send_message(
                chat_id, "Не удалось передать вопрос операторам — обратитесь в чат поддержки вручную."
            )
            return
    else:
        logger.warning("OPERATOR_CHAT_ID не задан — тикет только в лог:\n%s", ticket)

    await bot.send_message(
        chat_id,
        "Передал ваш вопрос дежурному оператору — он ответит здесь. "
        "К сообщению приложена история нашего диалога.",
    )
