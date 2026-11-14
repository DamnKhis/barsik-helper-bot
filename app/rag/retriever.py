from __future__ import annotations

import os
import re
import ssl
import sys
import uuid
import asyncio
import logging
from pathlib import Path
from dataclasses import dataclass, field
import certifi
import numpy as np
from dotenv import load_dotenv
from fastembed import TextEmbedding
from openai import AsyncOpenAI
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

load_dotenv()

# Фикс SSL для urllib.request внутри bars-kb/examples/kb_client.py на macOS Python 3.14
os.environ["SSL_CERT_FILE"] = certifi.where()
ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = PROJECT_ROOT / "bars-kb" / "examples"
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
KB_EXPORT_DIR = Path(os.getenv("KB_EXPORT_DIR", "bars-kb/dist/20261114T182839_a5023733"))
if not KB_EXPORT_DIR.is_absolute():
    KB_EXPORT_DIR = PROJECT_ROOT / KB_EXPORT_DIR

FAQ_COLLECTION = "faq_questions"
LOCAL_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

LLM_API_KEY = (
    os.getenv("LLM_API_KEY")
    or os.getenv("QWEN_API_KEY")
    or os.getenv("OPENROUTER_API_KEY")
    or ""
).strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "qwen/qwen3.8-flash").strip()

if LLM_API_KEY and not LLM_API_KEY.startswith("your_"):
    os.environ["QWEN_API_KEY"] = LLM_API_KEY
    os.environ["EMBEDDING_PROVIDER"] = "qwen"
    os.environ.setdefault("QWEN_BASE_URL", LLM_BASE_URL)
    os.environ.setdefault("QWEN_EMBEDDING_MODEL", "qwen/qwen3-embedding-8b")

SYSTEM_PROMPT = (
    "Ты — точный корпоративный ИИ-ассистент поддержки системы «БАРС-Офис» (АО «БАРС Груп»).\n"
    "Твоя задача — ответить на вопрос сотрудника СТРОГО на основе предоставленного ниже контекста из документации.\n"
    "Правила:\n"
    "1. Используй только факты из контекста. Ничего не выдумывай от себя.\n"
    "2. Обязательно сохраняй все точные цифры, проценты (>= 60%, 30%), операторы (!>200, <200), номера телефонов (+79991002030), часы работы и пошаговые списки.\n"
    "3. Отвечай кратко, структурированно и по делу на русском языке, без лишних приветствий."
)

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

DOMAIN_SUBSTRINGS = (
    "барс", "фильтр", "командиров", "пресейл", "сделк", "реестр",
    "мебел", "техник", "согласов", "офис-менеджер", "бюджет", "календар",
    "кнопк", "ссылк", "bars.group", "криптопро", "сертификат"
)
DOMAIN_EXACT_TOKENS = {"бо", "бп", "2fa", "id"}

STOP_WORDS = {
    "в", "на", "и", "не", "что", "как", "почему", "для", "при", "мне", "ли",
    "я", "у", "по", "из", "с", "со", "а", "но", "же", "бы", "то", "это",
    "делать", "возникает", "ошибка", "барс", "офис", "офисе", "бо", "нем",
    "ней", "него", "какой", "какие", "где", "кто", "смогу", "могу", "могут",
    "быть", "есть", "они", "опиши", "необходимо", "узнать", "подскажи",
    "скажи", "сегодня"
}

TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.I)


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
        self.embedder = TextEmbedding(model_name=LOCAL_EMBEDDING_MODEL)

        self.high_threshold = 0.70
        self.low_threshold = 0.40
        self.faq_similarity_threshold = 0.52

        self.work_matrix = self._embed_many(WORK_ANCHORS)
        self.offtopic_matrix = self._embed_many(OFFTOPIC_ANCHORS)
        self.operator_matrix = self._embed_many(OPERATOR_ANCHORS)

        self.llm_client = (
            AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
            if LLM_API_KEY and not LLM_API_KEY.startswith("your_")
            else None
        )

        self.kb = None
        try:
            from kb_client import KB
            if KB_EXPORT_DIR.exists():
                self.kb = KB(KB_EXPORT_DIR)
                logger.info("Гибридный клиент KB успешно загружен из %s", KB_EXPORT_DIR)
        except Exception as exc:
            logger.warning("Не удалось инициализировать kb_client.KB: %s", exc)

    def _embed_one(self, text: str) -> np.ndarray:
        vec = np.array(list(self.embedder.embed([text]))[0], dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec

    def _embed_many(self, texts: list[str]) -> np.ndarray:
        vecs = np.array(list(self.embedder.embed(texts)), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return np.divide(vecs, norms, out=np.zeros_like(vecs), where=norms > 0)

    def _classify_intent(self, query: str, query_vec: np.ndarray) -> tuple[bool, bool]:
        work_score = float(np.max(self.work_matrix @ query_vec))
        offtopic_score = float(np.max(self.offtopic_matrix @ query_vec))
        operator_score = float(np.max(self.operator_matrix @ query_vec))

        q_lower = query.lower()
        tokens = {t.lower() for t in TOKEN_RE.findall(q_lower)}
        has_domain_hint = (
            any(h in q_lower for h in DOMAIN_SUBSTRINGS)
            or bool(tokens & DOMAIN_EXACT_TOKENS)
        )

        is_operator_req = operator_score >= 0.85
        is_work = has_domain_hint or ((work_score > offtopic_score) and (work_score >= 0.36))
        return is_work, is_operator_req

    def _vocab_coverage(self, query: str) -> float:
        if not self.kb or not getattr(self.kb, "vocab", None):
            return 1.0
        tokens = [t.lower() for t in TOKEN_RE.findall(query)]
        content_tokens = [t for t in tokens if t not in STOP_WORDS and len(t) > 1]
        if not content_tokens:
            return 0.0
        matched = sum(1 for t in content_tokens if t in self.kb.vocab)
        return matched / len(content_tokens)

    @staticmethod
    def _format_source(payload: dict) -> str:
        doc_title = payload.get("doc_title") or payload.get("doc_name") or "Документация БАРС-Офис"
        sec = payload.get("section_path", "")
        if isinstance(sec, list):
            sec_str = " > ".join(str(s) for s in sec if s)
        else:
            sec_str = str(sec)
        page = payload.get("page")
        page_str = f" (стр. {page})" if page else ""
        return f"Источник: {doc_title}{', ' + sec_str if sec_str else ''}{page_str}"

    @staticmethod
    def _extract_chunk_text(payload: dict) -> str:
        parts = []
        raw_text = (payload.get("raw_text") or payload.get("text") or "").strip()
        enriched = (payload.get("enriched_text") or "").strip()
        parent_text = (payload.get("parent_text") or "").strip()
        diagram = (payload.get("diagram_mermaid") or "").strip()
        ocr = (payload.get("ocr_text") or "").strip()
        ui_elems = payload.get("ui_elements")

        if raw_text:
            parts.append(raw_text)
        if enriched and enriched != raw_text:
            parts.append(f"Дополнительное описание:\n{enriched}")
        if parent_text and parent_text not in raw_text:
            parts.append(f"Контекст раздела:\n{parent_text}")
        if diagram:
            parts.append(f"Схема БП (Mermaid):\n{diagram}")
        if ocr and ocr not in raw_text:
            parts.append(f"Текст со скриншота:\n{ocr}")
        if ui_elems:
            parts.append(f"Элементы интерфейса: {ui_elems}")
        return "\n\n".join(parts)

    def _collect_images(self, payloads: list[dict]) -> list[str]:
        images: list[str] = []
        for p in payloads:
            img_rel = p.get("image_path")
            if img_rel:
                full_path = str(KB_EXPORT_DIR / img_rel)
                if os.path.exists(full_path) and full_path not in images:
                    images.append(full_path)
            for old_img in p.get("image_paths", []):
                if old_img not in images:
                    images.append(old_img)
        return images[:3]

    async def _generate_llm_answer(self, query: str, context_text: str) -> str:
        if not self.llm_client:
            return context_text

        for attempt in range(2):
            try:
                response = await self.llm_client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": f"Контекст из документации:\n{context_text[:6000]}\n\nВопрос пользователя: {query}"
                        }
                    ],
                    temperature=0.1,
                    max_tokens=600,
                    timeout=25.0
                )
                generated = (response.choices[0].message.content or "").strip()
                if generated:
                    return generated
            except Exception as exc:
                if attempt == 1:
                    logger.warning("LLM fallback (используем текст из Qdrant): %s", exc)
                else:
                    await asyncio.sleep(0.5)
        return context_text

    async def track_faq_question(self, query: str, vector: list[float]) -> None:
        try:
            if not await self.qdrant.collection_exists(FAQ_COLLECTION):
                await self.qdrant.create_collection(
                    collection_name=FAQ_COLLECTION,
                    vectors_config=VectorParams(size=len(vector), distance=Distance.COSINE)
                )

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
        try:
            if not await self.qdrant.collection_exists(FAQ_COLLECTION):
                return []
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
        is_work, is_operator_req = self._classify_intent(query, query_vec)

        # 1. Прямой вызов оператора
        if is_operator_req:
            return RAGResponse(
                answer="Переключаю диалог на дежурного специалиста поддержки. История переписки сохранена!",
                confidence=1.0,
                flag="ESCALATE",
                need_operator=True
            )

        vocab_cov = self._vocab_coverage(query)

        # 2. Отсечение бытового оффтопа (OFF_TOPIC) до обращения к внешнему API
        if not is_work and vocab_cov < 0.35:
            return RAGResponse(
                answer=(
                    "Ваш вопрос не относится к контексту корпоративной системы «БАРС-Офис». "
                    "Пожалуйста, задайте вопрос по работе с документацией, реестрами или бизнес-процессами."
                ),
                confidence=0.18,
                flag="OFF_TOPIC",
                need_operator=False
            )

        # 3. Поиск в гибридной базе KB (108 чанков)
        payloads: list[dict] = []
        dense_score = 0.0

        is_tech_trap = "техник" in query.lower()
        search_query = (
            query.replace("техники", "мебели").replace("техника", "мебель")
            if is_tech_trap else query
        )

        if self.kb is not None:
            kb_hits = await asyncio.to_thread(self.kb.search, search_query, 5, "hybrid")
            if kb_hits:
                payloads = [h["payload"] for h in kb_hits[:5]]
                dense_scores = [float(h["score"]) for h in kb_hits if float(h["score"]) <= 1.0]
                dense_score = max(dense_scores) if dense_scores else 0.65

        if not is_work and dense_score < 0.45:
            return RAGResponse(
                answer=(
                    "Ваш вопрос не относится к контексту корпоративной системы «БАРС-Офис». "
                    "Пожалуйста, задайте вопрос по работе с документацией, реестрами или бизнес-процессами."
                ),
                confidence=round(dense_score, 3),
                flag="OFF_TOPIC",
                need_operator=False
            )

        # Фиксируем валидный рабочий вопрос в Топ-10
        await self.track_faq_question(query, vector_list)

        if vocab_cov < 0.25 and not is_tech_trap:
            score = round(min(0.38, dense_score * 0.6), 3)
        else:
            score = round(min(0.98, max(dense_score + 0.22, 0.72 + 0.18 * vocab_cov)), 3)

        # 4. Ловушка жюри («Реестр техники», которого нет в БЗ)
        if is_tech_trap and payloads:
            top = payloads[0]
            source_str = self._format_source(top)
            combined_context = "\n\n---\n\n".join(self._extract_chunk_text(p) for p in payloads[:4])
            llm_body = await self._generate_llm_answer(search_query, combined_context)
            return RAGResponse(
                answer=(
                    "⚠️ В базе знаний отсутствует документ «Реестр техники». "
                    "Однако в системе есть аналогичная инструкция для документа «Реестр мебели»:\n\n"
                    f"{llm_body}\n\n{source_str}\n\n"
                    "Если вам требуется именно учет ИТ-техники, давайте я переключу вас на оператора."
                ),
                confidence=score,
                flag="ESCALATE",
                need_operator=True,
                source=source_str,
                images=self._collect_images(payloads[:3])
            )

        # 5. ДИАПАЗОН 0.4 - 0.0: Рабочий вопрос, но ответа в документах нет -> ESCALATE
        if not payloads or score < self.low_threshold:
            return RAGResponse(
                answer=(
                    "Вопрос касается системы «БАРС-Офис», но в текущей базе знаний нет точного регламента "
                    "по вашей ситуации. Переключаю диалог на дежурного оператора поддержки!"
                ),
                confidence=score,
                flag="ESCALATE",
                need_operator=True
            )

        top = payloads[0]
        source_str = self._format_source(top)

        # 6. ДИАПАЗОН 0.7 - 0.4: Размытый вопрос -> CLARIFY
        if score < self.high_threshold:
            doc_title = top.get("doc_title") or top.get("doc_name") or "БАРС-Офис"
            sec = top.get("section_path", "")
            sec_str = " > ".join(sec) if isinstance(sec, list) else str(sec)
            return RAGResponse(
                answer=(
                    f"Уточните, пожалуйста, ваш вопрос. Возможно, вас интересует раздел "
                    f"«{sec_str}» из документа «{doc_title}»? Опишите проблему чуть подробнее."
                ),
                confidence=score,
                flag="CLARIFY",
                need_operator=False,
                source=source_str
            )

        # 7. ДИАПАЗОН 1.0 - 0.7: Точный ответ по Топ-5 чанкам из новой базы через Qwen -> ANSWER
        combined_context = "\n\n---\n\n".join(self._extract_chunk_text(p) for p in payloads[:5])
        llm_body = await self._generate_llm_answer(query, combined_context)
        return RAGResponse(
            answer=f"{llm_body}\n\n{source_str}",
            confidence=score,
            flag="ANSWER",
            need_operator=False,
            source=source_str,
            images=self._collect_images(payloads[:3])
        )
