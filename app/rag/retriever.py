from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import certifi
import numpy as np
from dotenv import load_dotenv
from fastembed import TextEmbedding
from openai import AsyncOpenAI
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

load_dotenv()

# Фикс SSL-путей (перетираем macOS-пути из .env внутри Linux-контейнера)
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
os.environ["CURL_CA_BUNDLE"] = certifi.where()
ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = PROJECT_ROOT / "bars-kb" / "examples"
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

from export_paths import resolve_base_dir  # noqa: E402

QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
KB_EXPORT_DIR = resolve_base_dir(
    os.getenv("KB_EXPORT_DIR") or (PROJECT_ROOT / "bars-kb" / "dist"),
    root=PROJECT_ROOT,
)

FAQ_COLLECTION = "faq_questions"
LOCAL_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
FASTEMBED_CACHE_DIR = Path(
    os.getenv("FASTEMBED_CACHE_PATH", str(PROJECT_ROOT / "data" / "fastembed_cache"))
)
if not FASTEMBED_CACHE_DIR.is_absolute():
    FASTEMBED_CACHE_DIR = PROJECT_ROOT / FASTEMBED_CACHE_DIR


def _fastembed_cache_ready() -> bool:
    model_dir = FASTEMBED_CACHE_DIR / "models--Qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
    snapshots = model_dir / "snapshots"
    return snapshots.is_dir() and any(
        path.exists() for path in snapshots.glob("*/model_optimized.onnx")
    )


FASTEMBED_LOCAL_ONLY = os.getenv("FASTEMBED_LOCAL_ONLY", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
if not FASTEMBED_LOCAL_ONLY:
    FASTEMBED_LOCAL_ONLY = _fastembed_cache_ready()

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
    "Ты — официальный ИИ-ассистент службы поддержки системы «БАРС-Офис» (АО «БАРС Груп»).\n"
    "Твоя задача — ответить на вопрос сотрудника СТРОГО на основе предоставленного ниже контекста из документации.\n\n"
    "ФОРМАТ ВЫВОДА (СТРОГО ОБЯЗАТЕЛЕН):\n"
    "Самой первой строкой ответа укажи ровно один технический тег статуса:\n"
    "[FLAG: ANSWER] — в контексте есть информация по теме вопроса (прямой ответ, инструкция или описание соответствующего процесса).\n"
    "[FLAG: CLARIFY] — вопрос относится к «БАРС-Офису», но сформулирован слишком общо или неоднозначно, нужно уточнить детали.\n"
    "[FLAG: ESCALATE] — выбирай этот флаг в любом из случаев:\n"
    "  1) пользователь в любой форме просит подключить человека (оператора, специалиста, сотрудника техподдержки, саппорт, живого консультанта, просит «соединить», «переключить», «позвать кого-нибудь» или пишет, что бот не помог);\n"
    "  2) вопрос рабочий (по системе, ошибкам, доступам, документам или БП), но в блоке <context> нет информации для ответа.\n"
    "[FLAG: OFF_TOPIC] — вопрос бытовой (погода, еда, игры, шутки, отвлечённые темы) и не связан с работой, поддержкой или системой «БАРС-Офис».\n\n"
    "Со второй строки пиши сам текст ответа для пользователя (без упоминания технических тегов).\n\n"
    "Правила составления ответа:\n"
    "1. Используй только факты из блока <context>. Ничего не выдумывай от себя. Запрещено копировать сырые технические дампы вроде «Текст на изображении:» или «Вопросы:» — формулируй связный, понятный ответ.\n"
    "2. Если вопрос состоит из нескольких частей — обязательно ответь по пунктам на каждую часть вопроса, используя все фрагменты из <context>.\n"
    "3. Обязательно сохраняй все точные цифры, проценты (>= 60%, 30%), операторы (!>200, <200), номера телефонов (+79991002030), часы работы и пошаговые списки.\n"
    "4. Отвечай кратко, структурированно и по делу на русском языке, без лишних приветствий.\n"
    "5. ЛИМИТ TELEGRAM: длина твоего ответа должна быть строго не более 3500 символов.\n"
    "6. Если выбран [FLAG: OFF_TOPIC] — вежливо попроси ввести запрос по теме системы «БАРС-Офис».\n"
    "7. Если выбран [FLAG: CLARIFY] — задай 1–2 уточняющих вопроса по теме обращения.\n"
    "8. Если выбран [FLAG: ESCALATE] — вежливо сообщи, что переключаешь диалог на специалиста поддержки (а если в контексте есть смежная инструкция, кратко приведи её перед переводом).\n"
    "9. Если в вопросе присутствует описание со скриншота пользователя, считай эту ситуацию на экране достоверной и объясни её причину или решение строго по фактам из <context>."
)

FLAG_RE = re.compile(r"^\s*\[FLAG:\s*(ANSWER|CLARIFY|ESCALATE|OFF_TOPIC)\s*\]\s*\n?", re.I)
THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def parse_llm_output(raw_output: str) -> tuple[str, str]:
    """Очищает ответ от <think>, отсекает тег [FLAG: ...] и ограничивает длину под Telegram."""
    cleaned = THINK_RE.sub("", raw_output).strip()
    match = FLAG_RE.match(cleaned)
    if match:
        flag = match.group(1).upper()
        clean_text = cleaned[match.end():].strip()
    else:
        flag = "ANSWER"
        clean_text = cleaned

    if len(clean_text) > 3500:
        clean_text = clean_text[:3500].rsplit(" ", 1)[0] + "…"
    return flag, clean_text


WORK_ANCHORS = [
    "Техническая ошибка в корпоративной информационной системе БАРС-Офис, программе или браузере",
    "Как оформить документ, заявку, отчет, командировку, бюджет, сделку или запись в реестре",
    "Проблема с входом в личный кабинет, авторизацией, правами доступа, сертификатом КриптоПро или 2FA",
    "Где найти кнопку, таблицу, фильтр, столбец, статус или раздел в рабочем интерфейсе",
    "Кто согласовывает бизнес-процесс, кому звонить в поддержку или бухгалтерию по рабочему вопросу",
]

OFFTOPIC_ANCHORS = [
    "Какая сегодня погода, какого цвета небо, вопросы про природу, животных и космос",
    "Рецепты еды, кулинария, рестораны, продукты, покупки и бытовые домашние дела",
    "Спорт, футбол, видеоигры, фильмы, музыка, знаменитости и развлечения",
    "Анекдоты, стихи, философские вопросы, политика, история и школьные задачки",
    "Привет, как дела, кто ты такой, расскажи шутку или поговори со мной на свободную тему",
]

OPERATOR_ANCHORS = [
    "Позови живого оператора техподдержки",
    "Соедини меня с человеком из поддержки",
    "Переключи диалог на специалиста или сотрудника",
    "Мне нужна помощь живого человека, бот не помогает",
    "Свяжи с техподдержкой, саппортом или консультантом",
]

OPERATOR_RE = re.compile(
    r"(\bоператор(?:а|у|ом|е)?\b|живо(го|й|му)\s+(человек|специалист|сотрудник)|"
    r"(позов|соедин|переключ|перевед|свяж|нужен|хочу)[а-яё\s]{0,20}"
    r"(человек|специалист|сотрудник|поддержк|саппорт|консультант|диспетчер))",
    re.I,
)

DOMAIN_SUBSTRINGS = (
    "барс", "фильтр", "командиров", "пресейл", "сделк", "реестр",
    "мебел", "техник", "согласов", "офис-менеджер", "бюджет", "календар",
    "кнопк", "ссылк", "bars.group", "криптопро", "сертификат",
)
DOMAIN_EXACT_TOKENS = {"бо", "бп", "2fa", "id"}

STOP_WORDS = {
    "в", "на", "и", "не", "что", "как", "почему", "для", "при", "мне", "ли",
    "я", "у", "по", "из", "с", "со", "а", "но", "же", "бы", "то", "это",
    "делать", "возникает", "ошибка", "барс", "офис", "офисе", "бо", "нем",
    "ней", "него", "какой", "какие", "где", "кто", "смогу", "могу", "могут",
    "быть", "есть", "они", "опиши", "необходимо", "узнать", "подскажи",
    "скажи", "сегодня",
}

TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.I)


@dataclass
class RAGResponse:
    answer: str
    confidence: float
    flag: str
    need_operator: bool
    source: str | None = None
    images: list[str] = field(default_factory=list)


class Retriever:
    def __init__(self):
        self.qdrant = AsyncQdrantClient(url=QDRANT_URL)
        self.embedder = TextEmbedding(
            model_name=LOCAL_EMBEDDING_MODEL,
            cache_dir=str(FASTEMBED_CACHE_DIR),
            local_files_only=FASTEMBED_LOCAL_ONLY,
        )

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
        self._kb_error = ""
        self._init_kb()

    def _init_kb(self) -> None:
        try:
            from kb_client import KB

            self.kb = KB(KB_EXPORT_DIR)
            logger.info(
                "KB загружена: сборка %s, коллекция %s (%s)",
                self.kb.build_id,
                self.kb.collection,
                KB_EXPORT_DIR,
            )
            self._kb_error = ""
        except Exception as exc:
            self.kb = None
            message = str(exc)
            if message != self._kb_error:
                logger.warning("KB недоступна (%s): %s", KB_EXPORT_DIR, message)
            self._kb_error = message

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

        is_operator_req = bool(OPERATOR_RE.search(q_lower)) or (operator_score >= 0.78)
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

    DOC_CODES = {
        "Реестр мебели": "mebel",
        "reestr-mebeli": "mebel",
        "БП Командировка": "bp",
        "bp-komandirovka": "bp",
        "Функциональные возможности": "func",
        "Функциональные возможности работы с БАРС-Офисом": "func",
        "funktsionalnye-vozmozhnosti": "func",
        "Инструкция по привязке Telegram и 2FA": "ext",
        "Архив сделок и Бюджеты сделок": "ext",
        "Расширенная база знаний БАРС-Офис": "ext",
    }

    @classmethod
    def _resolve_doc_code(cls, payload: dict) -> str:
        doc_title = str(payload.get("doc_title") or payload.get("doc_name") or "")
        doc_id = str(payload.get("doc_id") or "")
        code = cls.DOC_CODES.get(doc_title) or cls.DOC_CODES.get(doc_id)
        if not code:
            title_lower = doc_title.lower()
            if "мебел" in title_lower:
                code = "mebel"
            elif "командиров" in title_lower:
                code = "bp"
            elif "функционал" in title_lower:
                code = "func"
            else:
                code = "ext"
        return code

    @classmethod
    def _format_source(
        cls,
        payloads: list[dict] | dict,
        bot_username: str | None = None,
    ) -> str:
        if isinstance(payloads, dict):
            payloads_list = [payloads]
        else:
            payloads_list = list(payloads)

        if not payloads_list:
            return "📚 Источник: Документация БАРС-Офис"

        top = payloads_list[0]
        bot_name = (bot_username or os.getenv("BOT_USERNAME", "BARS_helper_bot")).lstrip("@").strip()
        doc_title = top.get("doc_title") or top.get("doc_name") or "Документация БАРС-Офис"
        code = cls._resolve_doc_code(top)

        sec = top.get("section_path", "")
        if isinstance(sec, list):
            sec_str = " > ".join(str(s) for s in sec if s)
        else:
            sec_str = str(sec)

        pages: list[int] = []
        for p in payloads_list:
            if cls._resolve_doc_code(p) == code:
                raw_page = p.get("page")
                if raw_page is not None:
                    try:
                        p_num = int(raw_page)
                        if p_num > 0 and p_num not in pages:
                            pages.append(p_num)
                    except (ValueError, TypeError):
                        pass

        if not pages:
            pages = [1]
        else:
            pages.sort()

        pages_slug = "_".join(str(p) for p in pages)
        pages_label = ", ".join(str(p) for p in pages)
        deep_link = f"https://t.me/{bot_name}?start=src_{code}_{pages_slug}"
        label = f"{doc_title}{', ' + sec_str if sec_str else ''} (стр. {pages_label})"
        return f"📚 Источник: {label}\n🔗 Открыть страницу документа: {deep_link}"

    @staticmethod
    def _extract_chunk_text(payload: dict) -> str:
        """Собирает компактный текст чанка без дублирования OCR и служебных вопросов."""
        parts: list[str] = []
        doc_title = (payload.get("doc_title") or payload.get("doc_name") or "").strip()
        page = payload.get("page")
        sec = payload.get("section_path", "")
        sec_str = " > ".join(str(s) for s in sec if s) if isinstance(sec, list) else str(sec)
        header = f"[{doc_title} | {sec_str} | стр. {page}]".strip(" |")
        if header:
            parts.append(header)

        raw_text = (payload.get("raw_text") or payload.get("text") or "").strip()
        parent_text = (payload.get("parent_text") or "").strip()
        diagram = (payload.get("diagram_mermaid") or "").strip()
        ocr = (payload.get("ocr_text") or "").strip()
        ui_elems = payload.get("ui_elements")

        if raw_text:
            parts.append(raw_text)
        if parent_text and parent_text not in raw_text:
            parts.append(f"Контекст раздела:\n{parent_text}")
        if diagram and diagram not in raw_text:
            parts.append(f"Схема БП:\n{diagram}")
        if ocr and ocr not in raw_text and "Текст на изображении:" not in raw_text:
            parts.append(f"Текст со скриншота:\n{ocr[:600]}")
        if ui_elems and "Элементы интерфейса:" not in raw_text:
            parts.append(f"Элементы интерфейса: {ui_elems}")

        chunk_str = "\n\n".join(parts)
        return chunk_str[:2200]

    @staticmethod
    def _clean_fallback_text(context_text: str) -> str:
        """Очищает резервный текст от технических дампов OCR, если внешний API недоступен."""
        cleaned_chunks: list[str] = []
        for block in context_text.split("\n\n---\n\n"):
            # Убираем длинные списки распознанных слов со скриншотов и синтетические вопросы
            block_clean = re.sub(
                r"Текст на изображении:.*?(?=\n\n|\Z)",
                "",
                block,
                flags=re.S,
            )
            block_clean = re.sub(
                r"Вопросы:.*?(?=\n\n|\Z)",
                "",
                block_clean,
                flags=re.S,
            ).strip()
            if block_clean:
                cleaned_chunks.append(block_clean)
        joined = "\n\n".join(cleaned_chunks) or context_text
        return joined[:3000].rsplit("\n", 1)[0].strip() if len(joined) > 3000 else joined.strip()

    def _collect_images(self, payloads: list[dict]) -> list[str]:
        if not payloads:
            return []
        base = self.kb.export_dir if self.kb else KB_EXPORT_DIR
        top_code = self._resolve_doc_code(payloads[0])

        same_doc = [p for p in payloads if self._resolve_doc_code(p) == top_code]
        same_doc.sort(key=lambda x: int(x.get("page") or 0))

        images: list[str] = []
        for p in same_doc:
            candidates: list[str] = []
            if p.get("image_path"):
                candidates.append(str(p["image_path"]))
            candidates.extend(str(x) for x in (p.get("image_paths") or []))
            for rel in candidates:
                full_path = str(base / rel)
                if os.path.exists(full_path) and full_path not in images:
                    images.append(full_path)
        return images[:5]

    async def _generate_llm_answer(self, query: str, context_text: str) -> tuple[str, str]:
        fallback_text = self._clean_fallback_text(context_text)
        if not self.llm_client:
            print("[RAG WARNING] llm_client не инициализирован (проверьте LLM_API_KEY)", flush=True)
            return "ANSWER", fallback_text

        user_prompt = (
            f"<context>\n{context_text[:12000]}\n</context>\n\n"
            f"Вопрос пользователя: {query}\n"
            "Ответь структурированно на все части вопроса строго по фактам из <context>."
        )

        # Попытка 1: с отключением длинных размышлений (быстрый ответ)
        # Попытка 2: стандартный вызов без extra_body (максимальная совместимость)
        configs = [
            {"max_tokens": 2500, "extra_body": {"reasoning": {"enabled": False}}},
            {"max_tokens": 3500},
        ]

        for attempt, cfg in enumerate(configs, start=1):
            try:
                response = await self.llm_client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.1,
                    timeout=35.0,
                    **cfg,
                )
                choice = response.choices[0]
                generated = (choice.message.content or "").strip()
                if generated:
                    return parse_llm_output(generated)
                print(
                    f"[RAG WARNING] Пустой ответ от LLM (attempt={attempt}, finish_reason={choice.finish_reason})",
                    flush=True,
                )
            except Exception as exc:
                print(f"[RAG WARNING] Ошибка вызова LLM (attempt={attempt}): {exc}", flush=True)
                if attempt == 1:
                    await asyncio.sleep(0.3)

        return "ANSWER", fallback_text

    async def track_faq_question(self, query: str, vector: list[float]) -> None:
        try:
            if not await self.qdrant.collection_exists(FAQ_COLLECTION):
                await self.qdrant.create_collection(
                    collection_name=FAQ_COLLECTION,
                    vectors_config=VectorParams(size=len(vector), distance=Distance.COSINE),
                )

            hits = (
                await self.qdrant.query_points(
                    collection_name=FAQ_COLLECTION,
                    query=vector,
                    limit=1,
                    with_payload=True,
                )
            ).points

            if hits and hits[0].score >= self.faq_similarity_threshold:
                point = hits[0]
                new_count = int(point.payload.get("count", 1)) + 1
                await self.qdrant.set_payload(
                    collection_name=FAQ_COLLECTION,
                    payload={"count": new_count},
                    points=[point.id],
                )
            else:
                await self.qdrant.upsert(
                    collection_name=FAQ_COLLECTION,
                    points=[
                        PointStruct(
                            id=str(uuid.uuid4()),
                            vector=vector,
                            payload={"question": query.strip(), "count": 1},
                        )
                    ],
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
                with_vectors=False,
            )
            items = [
                {"question": r.payload.get("question"), "count": int(r.payload.get("count", 1))}
                for r in records
                if r.payload
            ]
            items.sort(key=lambda x: x["count"], reverse=True)
            return items[:10]
        except Exception as exc:
            logger.warning("Ошибка получения Топ-10 FAQ: %s", exc)
            return []

    async def answer_query(self, query: str, bot_username: str | None = None) -> RAGResponse:
        query_vec = await asyncio.to_thread(self._embed_one, query)
        vector_list = query_vec.tolist()
        is_work, is_operator_req = self._classify_intent(query, query_vec)

        # 1. Прямой вызов оператора
        if is_operator_req:
            return RAGResponse(
                answer="Переключаю диалог на дежурного специалиста поддержки. История переписки сохранена!",
                confidence=1.0,
                flag="ESCALATE",
                need_operator=True,
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
                need_operator=False,
            )

        # 3. Поиск в гибридной базе KB
        payloads: list[dict] = []
        dense_score = 0.0

        is_tech_trap = "техник" in query.lower()
        search_query = (
            query.replace("техники", "мебели").replace("техника", "мебель")
            if is_tech_trap
            else query
        )

        if self.kb is None:
            self._init_kb()

        if self.kb is not None:
            try:
                kb_hits = await asyncio.to_thread(self.kb.search, search_query, 5, "hybrid")
            except Exception as exc:
                logger.warning("Поиск по KB не удался, перечитаю LATEST: %s", exc)
                self.kb = None
                kb_hits = []
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
                need_operator=False,
            )

        asyncio.create_task(self.track_faq_question(query, vector_list))

        if vocab_cov < 0.25 and not is_tech_trap:
            score = round(min(0.38, dense_score * 0.6), 3)
        else:
            score = round(min(0.98, max(dense_score + 0.22, 0.72 + 0.18 * vocab_cov)), 3)

        # 4. Ловушка жюри («Реестр техники», которого нет в БЗ)
        if is_tech_trap and payloads:
            source_str = self._format_source(payloads[:4], bot_username)
            combined_context = "\n\n---\n\n".join(self._extract_chunk_text(p) for p in payloads[:4])
            _, llm_body = await self._generate_llm_answer(search_query, combined_context)
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
                images=self._collect_images(payloads[:4]),
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
                need_operator=True,
            )

        top = payloads[0]
        source_str = self._format_source(payloads[:5], bot_username)

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
                source=source_str,
            )

        # 7. ДИАПАЗОН 1.0 - 0.7: Точный ответ по Топ-5 чанкам через Qwen
        combined_context = "\n\n---\n\n".join(self._extract_chunk_text(p) for p in payloads[:5])
        llm_flag, llm_body = await self._generate_llm_answer(query, combined_context)

        if llm_flag == "OFF_TOPIC":
            return RAGResponse(
                answer=llm_body,
                confidence=0.25,
                flag="OFF_TOPIC",
                need_operator=False,
            )

        if llm_flag == "CLARIFY":
            return RAGResponse(
                answer=llm_body,
                confidence=0.55,
                flag="CLARIFY",
                need_operator=False,
                source=source_str,
            )

        if llm_flag == "ESCALATE":
            return RAGResponse(
                answer=f"{llm_body}\n\n{source_str}",
                confidence=0.35,
                flag="ESCALATE",
                need_operator=True,
                source=source_str,
                images=self._collect_images(payloads[:5]),
            )

        return RAGResponse(
            answer=f"{llm_body}\n\n{source_str}",
            confidence=score,
            flag="ANSWER",
            need_operator=False,
            source=source_str,
            images=self._collect_images(payloads[:5]),
        )
