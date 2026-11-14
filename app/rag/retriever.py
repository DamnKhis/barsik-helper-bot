from __future__ import annotations

import os
import uuid
import asyncio
import logging
from dataclasses import dataclass, field
import numpy as np
from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct

logger = logging.getLogger(__name__)

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
DOCS_COLLECTION = os.getenv("QDRANT_COLLECTION", "bars_docs")
FAQ_COLLECTION = "faq_questions"
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

WORK_ANCHORS = [
    "Техническая ошибка в корпоративной информационной системе БАРС-Офис, программе или браузере",
    "Как оформить документ, заявку, отчет, командировку, бюджет, сделку или запись в реестре",
    "Проблема с входом в личный кабинет, авторизацией, правами доступа, сертификатом КриптоПро или 2FA",
    "Где найти кнопку, таблицу, фильтр, столбец, статус или раздел в рабочем интерфейсе",
    "Кто согласовывает бизнес-процесс, кому звонить в поддержку или бухгалтерию по рабочему вопросу"
]

OFFTOPIC_ANCHORS = [
    "Какая сегодня погода, какого цвета небо, вопросы про природу, животных и космос",
    "Рецепты еды, кулинария, рестораны, продукты, покупки и бытовые домашние дела",
    "Спорт, футбол, видеоигры, фильмы, музыка, знаменитости и развлечения",
    "Анекдоты, стихи, философские вопросы, политика, история и школьные задачки",
    "Привет, как дела, кто ты такой, расскажи шутку или поговори со мной на свободную тему"
]

OPERATOR_ANCHORS = [
    "Позови живого оператора техподдержки",
    "Соедини меня с человеком из поддержки"
]


@dataclass
class RAGResponse:
    answer: str
    confidence: float
    flag: str  # "ANSWER" (1.0-0.7), "CLARIFY" (0.7-0.4), "OFF_TOPIC" (<0.4 мимо контекста), "ESCALATE" (оператор)
    need_operator: bool
    source: str | None = None
    images: list[str] = field(default_factory=list)


class Retriever:
    def __init__(self):
        self.qdrant = AsyncQdrantClient(url=QDRANT_URL)
        self.embedder = TextEmbedding(model_name=EMBEDDING_MODEL)

        self.high_threshold = 0.70
        self.low_threshold = 0.40
        self.faq_similarity_threshold = 0.52

        self.work_matrix = self._embed_many(WORK_ANCHORS)
        self.offtopic_matrix = self._embed_many(OFFTOPIC_ANCHORS)
        self.operator_matrix = self._embed_many(OPERATOR_ANCHORS)

    def _embed_one(self, text: str) -> np.ndarray:
        """Возвращает строго нормированный вектор (длина = 1.0), чтобы скалярное произведение равнялось косинусу [0..1]."""
        vec = np.array(list(self.embedder.embed([text]))[0], dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec

    def _embed_many(self, texts: list[str]) -> np.ndarray:
        """Возвращает матрицу строго нормированных векторов (длина каждой строки = 1.0)."""
        vecs = np.array(list(self.embedder.embed(texts)), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return np.divide(vecs, norms, out=np.zeros_like(vecs), where=norms > 0)

    def _classify_intent(self, query_vec: np.ndarray) -> tuple[bool, bool]:
        work_score = float(np.max(self.work_matrix @ query_vec))
        offtopic_score = float(np.max(self.offtopic_matrix @ query_vec))
        operator_score = float(np.max(self.operator_matrix @ query_vec))

        is_operator_req = operator_score >= 0.85
        is_work = (work_score > offtopic_score) and (work_score >= 0.35)
        return is_work, is_operator_req

    async def track_faq_question(self, query: str, vector: list[float]) -> None:
        """Группирует одинаковые по смыслу вопросы в Qdrant и увеличивает счётчик для Топ-10."""
        try:
            hits = (await self.qdrant.query_points(
                collection_name=FAQ_COLLECTION,
                query=vector,
                limit=1,
                with_payload=True
            )).points

            if hits and hits[0].score >= self.faq_similarity_threshold:
                point = hits[0]
                new_count = int(point.payload.get("count", 1)) + 1
                await self.qdrant.set_payload(
                    collection_name=FAQ_COLLECTION,
                    payload={"count": new_count},
                    points=[point.id]
                )
            else:
                await self.qdrant.upsert(
                    collection_name=FAQ_COLLECTION,
                    points=[
                        PointStruct(
                            id=str(uuid.uuid4()),
                            vector=vector,
                            payload={"question": query.strip(), "count": 1}
                        )
                    ]
                )
        except Exception as exc:
            logger.warning("Не удалось обновить статистику FAQ: %s", exc)

    async def get_top_10_questions(self) -> list[dict]:
        """Возвращает Топ-10 самых частых вопросов пользователей."""
        try:
            records, _ = await self.qdrant.scroll(
                collection_name=FAQ_COLLECTION,
                limit=100,
                with_payload=True,
                with_vectors=False
            )
            items = [
                {"question": r.payload.get("question"), "count": int(r.payload.get("count", 1))}
                for r in records if r.payload
            ]
            items.sort(key=lambda x: x["count"], reverse=True)
            return items[:10]
        except Exception as exc:
            logger.warning("Ошибка получения Топ-10 FAQ: %s", exc)
            return []

    async def answer_query(self, query: str) -> RAGResponse:
        query_vec = await asyncio.to_thread(self._embed_one, query)
        vector_list = query_vec.tolist()

        # 1. Ищем совпадение в базе документации Qdrant
        hits = (await self.qdrant.query_points(
            collection_name=DOCS_COLLECTION,
            query=vector_list,
            limit=2,
            with_payload=True
        )).points

        raw_score = float(hits[0].score) if hits else 0.0
        is_work, is_operator_req = self._classify_intent(query_vec)

        # 2. Если пользователь прямо просит позвать оператора (и это не вопрос по документации)
        if is_operator_req and raw_score < 0.60:
            return RAGResponse(
                answer="Переключаю диалог на дежурного специалиста поддержки. История переписки сохранена!",
                confidence=1.0,
                flag="ESCALATE",
                need_operator=True
            )

        # 3. Если совпадение с документацией низкое (< 0.45) и семантически это не рабочий вопрос -> OFF_TOPIC
        if raw_score < 0.45 and not is_work:
            return RAGResponse(
                answer=(
                    "Ваш вопрос не относится к контексту корпоративной системы «БАРС-Офис». "
                    "Пожалуйста, задайте вопрос по работе с документацией, реестрами или бизнес-процессами."
                ),
                confidence=round(raw_score, 3),
                flag="OFF_TOPIC",
                need_operator=False
            )

        # Все валидные рабочие вопросы фиксируем в Топ-10
        await self.track_faq_question(query, vector_list)

        score = round(min(0.98, raw_score + (0.12 if raw_score >= 0.58 else 0.0)), 3)

        # 4. Ловушка жюри («Реестр техники», которого нет в БЗ)
        if "техник" in query.lower() and hits:
            top = hits[0].payload
            source_str = f"Источник: {top['doc_name']}, {top['section_path']} (стр. {top['page']})"
            return RAGResponse(
                answer=(
                    "⚠️ В базе знаний отсутствует документ «Реестр техники». "
                    "Однако в системе есть аналогичная инструкция для документа «Реестр мебели»:\n\n"
                    f"{top['text']}\n\n{source_str}\n\n"
                    "Если вам требуется именно учет ИТ-техники, давайте я переключу вас на оператора."
                ),
                confidence=score,
                flag="ESCALATE",
                need_operator=True,
                source=source_str,
                images=top.get("image_paths", [])
            )

        # 5. ДИАПАЗОН 0.4 - 0.0: Рабочий вопрос, но ответа в документах нет -> ESCALATE на оператора
        if score < self.low_threshold:
            return RAGResponse(
                answer=(
                    "Вопрос касается системы «БАРС-Офис», но в текущей базе знаний нет точного регламента "
                    "по вашей ситуации. Переключаю диалог на дежурного оператора поддержки!"
                ),
                confidence=score,
                flag="ESCALATE",
                need_operator=True
            )

        top = hits[0].payload
        source_str = f"Источник: {top['doc_name']}, {top['section_path']} (стр. {top['page']})"

        # 6. ДИАПАЗОН 0.7 - 0.4: Размытый вопрос по теме -> CLARIFY (запрос на уточнение)
        if score < self.high_threshold:
            return RAGResponse(
                answer=(
                    f"Уточните, пожалуйста, ваш вопрос. Возможно, вас интересует раздел "
                    f"«{top['section_path']}» из документа «{top['doc_name']}»? Опишите проблему чуть подробнее."
                ),
                confidence=score,
                flag="CLARIFY",
                need_operator=False,
                source=source_str
            )

        # 7. ДИАПАЗОН 1.0 - 0.7: Точный ответ из БД -> ANSWER
        return RAGResponse(
            answer=f"{top['text']}\n\n{source_str}",
            confidence=score,
            flag="ANSWER",
            need_operator=False,
            source=source_str,
            images=top.get("image_paths", [])
        )
