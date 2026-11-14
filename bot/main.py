"""Telegram-бот первой линии поддержки. Роль 3.

Запуск: python -m bot.main   (нужен BOT_TOKEN в .env)

Логика сообщения:
  «оператор / человек / специалист» -> эскалация с историей (bot.escalation);
  иначе -> POST /ask в RAG-ядро -> ответ; при escalate=true бот предлагает
  позвать оператора. Приветствие и кнопки — из брифа: дружелюбно, на «Вы».
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from redis.asyncio import Redis

from bot.api_client import ApiError, ask_api
from bot.escalation import escalate_to_operator, wants_operator
from bot.history import load_history, save_history
from settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot")

WELCOME_TEXT = (
    "Здравствуйте! Я виртуальный помощник службы поддержки БАРС-Офис.\n\n"
    "Отвечу на вопросы о работе системы: командировки, мебель и рабочие места, "
    "фильтры и интерфейс. Задайте вопрос текстом или выберите тему кнопкой.\n\n"
    "Если я не справлюсь — позовите оператора, и я передам вопрос живому "
    "специалисту вместе с нашей перепиской."
)

TOPIC_BUTTONS = ["Командировки", "Мебель и рабочие места", "Интерфейс и фильтры", "Позвать оператора"]

router = Router()
_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


def main_keyboard() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=button)] for button in TOPIC_BUTTONS]
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True, input_field_placeholder="Задайте вопрос…"
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME_TEXT, reply_markup=main_keyboard())


@router.message(F.text)
async def handle_message(message: Message) -> None:
    if message.from_user is None or message.text is None:
        return
    redis = get_redis()
    user_id = message.from_user.id
    question = message.text.strip()
    history = await load_history(redis, user_id)

    if wants_operator(question):
        await escalate_to_operator(message.bot, redis, user_id, message.chat.id, question)
        return

    try:
        data = await ask_api(question, str(user_id), history)
    except ApiError as exc:
        logger.error("RAG-ядро недоступно: %s", exc)
        await message.answer(
            "Сервис временно недоступен. Попробуйте позже — или напишите «оператор», "
            "чтобы оставить вопрос людям."
        )
        return

    await message.answer(data["answer"])
    if data.get("escalate"):
        await message.answer(
            "Если ответ не помог — нажмите «Позвать оператора», и я передам вопрос "
            "живому специалисту вместе с историей диалога."
        )

    history += [
        {"role": "user", "content": question},
        {"role": "assistant", "content": data["answer"]},
    ]
    await save_history(redis, user_id, history)


async def main() -> None:
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit(
            "BOT_TOKEN не задан: скопируйте .env.example в .env и вставьте токен от @BotFather"
        )
    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    logger.info("Бот запущен, API=%s", settings.api_url)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
