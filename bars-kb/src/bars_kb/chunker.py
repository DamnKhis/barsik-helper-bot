from __future__ import annotations

import hashlib
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from .config import Settings
from .enrich.cache import EnrichmentCache
from .enrich.faq import derive_faqs, faq_text
from .enrich.images import enrich_image
from .enrich.tables import enrich_table
from .enrich.text import enrich_text
from .models import Block, Chunk, Document, Section, table_to_markdown
from .providers.base import LLMProvider, VisionProvider

NAMESPACE = uuid.UUID("6f0f6a4e-4f4b-4e2e-9a6d-2a1c0b7d9e11")

logger = logging.getLogger(__name__)


@dataclass
class _SectionPlan:
    section: Section
    parent_id: str
    parent_text: str
    text_chunks: list[Chunk]


@dataclass
class _AssetTask:
    plan_index: int
    plan: _SectionPlan
    kind: str
    ordinal: int
    block: Block


class Chunker:
    def __init__(
        self,
        settings: Settings,
        llm: LLMProvider | None,
        vision: VisionProvider | None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.vision = vision
        self.cache = EnrichmentCache(settings.data_dir / "cache")

    def document_chunks(self, document: Document) -> list[Chunk]:
        plans = [
            plan
            for section in document.sections
            if (plan := self._prepare_section(document, section)) is not None
        ]

        tasks: list[_AssetTask] = []
        for plan_index, plan in enumerate(plans):
            for ordinal, block in enumerate(plan.section.tables):
                if block.table:
                    tasks.append(_AssetTask(plan_index, plan, "table", ordinal, block))
            for ordinal, block in enumerate(plan.section.images):
                tasks.append(_AssetTask(plan_index, plan, "image", ordinal, block))

        assets = self._run_asset_tasks(document, tasks)
        by_plan: dict[int, list[Chunk]] = {}
        for task, chunk in zip(tasks, assets):
            if chunk is not None:
                by_plan.setdefault(task.plan_index, []).append(chunk)

        chunks: list[Chunk] = []
        for plan_index, plan in enumerate(plans):
            chunks.extend(plan.text_chunks)
            chunks.extend(by_plan.get(plan_index, []))
        chunks.extend(self._faq_chunks(document))
        return _dedupe(chunks)

    def _prepare_section(self, document: Document, section: Section) -> _SectionPlan | None:
        text_blocks = [b for b in section.blocks if b.kind in {"paragraph", "list"} and b.text.strip()]
        section_text = "\n\n".join(b.text for b in text_blocks).strip()
        if not section_text and not section.tables and not section.images:
            return None

        enrichment = enrich_text(
            self.settings,
            self.llm,
            self.cache,
            section.title,
            section.path,
            section_text or section.title,
        )
        parent_text = self._parent_text(document, section, enrichment, section_text)
        parent_id = _uid(document.doc_id, "|".join(section.path), "parent")

        text_chunks: list[Chunk] = []
        if section_text:
            for ordinal, piece in enumerate(
                _pack_text(section_text, self.settings.chunk_max_chars, self.settings.chunk_overlap_chars)
            ):
                enriched = _join_enriched(
                    document.title,
                    section.path,
                    piece,
                    enrichment.get("summary", ""),
                    enrichment.get("questions", []),
                )
                text_chunks.append(
                    self._chunk(
                        document=document,
                        section=section,
                        chunk_type="text",
                        raw_text=piece,
                        enriched_text=enriched,
                        summary=enrichment.get("summary", ""),
                        parent_id=parent_id,
                        parent_text=parent_text,
                        ordinal=ordinal,
                        entities=enrichment.get("entities", []),
                    )
                )
        return _SectionPlan(section, parent_id, parent_text, text_chunks)

    def _run_asset_tasks(self, document: Document, tasks: list[_AssetTask]) -> list[Chunk | None]:
        if not tasks:
            return []

        workers = max(1, min(self.settings.enrichment_workers, len(tasks)))
        if workers == 1:
            return [self._build_asset_chunk(document, task) for task in tasks]

        logger.info(
            "enriching %d assets for %s with %d workers",
            len(tasks),
            document.doc_id,
            workers,
        )
        build = partial(self._build_asset_chunk, document)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bars-kb-enrich") as pool:
            return list(pool.map(build, tasks))

    def _build_asset_chunk(self, document: Document, task: _AssetTask) -> Chunk | None:
        if task.kind == "table":
            return self._table_chunk(document, task.plan, task.block, task.ordinal)
        return self._image_chunk(document, task.plan, task.block, task.ordinal)

    def _table_chunk(
        self,
        document: Document,
        plan: _SectionPlan,
        block: Block,
        ordinal: int,
    ) -> Chunk:
        section = plan.section
        table_enrichment = enrich_table(
            self.settings,
            self.llm,
            self.cache,
            section.title,
            block.table,
        )
        markdown = table_to_markdown(block.table)
        raw = f"Таблица: {section.title}\n\n{markdown}"
        enriched = _join_enriched(
            document.title,
            section.path,
            raw,
            table_enrichment.get("description", ""),
            table_enrichment.get("questions", []),
        )
        return self._chunk(
            document=document,
            section=section,
            chunk_type="table",
            raw_text=raw,
            enriched_text=enriched,
            summary=table_enrichment.get("description", ""),
            parent_id=plan.parent_id,
            parent_text=plan.parent_text,
            ordinal=ordinal,
            entities=table_enrichment.get("entities", []),
            table_json={
                "headers": block.table[0] if block.table else [],
                "rows": block.table[1:] if len(block.table) > 1 else [],
            },
        )

    def _image_chunk(
        self,
        document: Document,
        plan: _SectionPlan,
        block: Block,
        ordinal: int,
    ) -> Chunk | None:
        section = plan.section
        image_path = Path(block.image_path)
        if not image_path.exists():
            return None

        kind = "diagram" if _is_diagram(section, block) else "screenshot"
        image_enrichment = enrich_image(
            self.settings,
            self.vision,
            self.cache,
            image_path,
            block.caption or section.title,
            f"{document.title} > {' > '.join(section.path)}",
            kind_hint=kind,
        )
        description = image_enrichment.get("description", "")
        ocr_text = image_enrichment.get("ocr_text", "")
        elements = image_enrichment.get("ui_elements", [])
        element_text = "\n".join(
            f"- {e.get('label', '')} ({e.get('location', '')})" for e in elements if isinstance(e, dict)
        )
        raw = "\n\n".join(
            part
            for part in [
                f"Изображение: {block.caption or section.title}",
                description,
                f"Текст на изображении:\n{ocr_text}" if ocr_text else "",
                f"Элементы интерфейса:\n{element_text}" if element_text else "",
            ]
            if part
        )
        enriched = _join_enriched(
            document.title,
            section.path,
            raw,
            description,
            image_enrichment.get("questions", []),
        )
        relative_image = _relative_image(self.settings, image_path)
        return self._chunk(
            document=document,
            section=section,
            chunk_type=kind,
            raw_text=raw,
            enriched_text=enriched,
            summary=description,
            parent_id=plan.parent_id,
            parent_text=plan.parent_text,
            ordinal=ordinal,
            entities=[],
            image_path=relative_image,
            image_url=_image_url(self.settings, relative_image),
            diagram_mermaid=image_enrichment.get("diagram_mermaid", ""),
            meta={
                "ocr_text": ocr_text,
                "ui_elements": elements,
                "nodes": image_enrichment.get("nodes", []),
                "edges": image_enrichment.get("edges", []),
                "image_sha256": block.meta.get("sha256", ""),
            },
        )

    def _faq_chunks(self, document: Document) -> list[Chunk]:
        chunks: list[Chunk] = []
        for index, faq in enumerate(derive_faqs(document)):
            section = Section(title=faq["section_path"][-1], level=0, path=faq["section_path"], page=faq["page"])
            raw = faq_text(faq)
            enriched = f"{document.title}. {' > '.join(faq['section_path'])}\n{raw}"
            chunks.append(
                self._chunk(
                    document=document,
                    section=section,
                    chunk_type="faq",
                    raw_text=raw,
                    enriched_text=enriched,
                    summary=faq["answer"][:600],
                    parent_id=_uid(document.doc_id, "|".join(faq["section_path"]), "parent"),
                    parent_text=faq["answer"],
                    ordinal=index,
                    entities=[],
                    meta={"derived": True, "faq_tag": faq["tag"]},
                )
            )
        return chunks

    def _chunk(
        self,
        document: Document,
        section: Section,
        chunk_type: str,
        raw_text: str,
        enriched_text: str,
        summary: str,
        parent_id: str,
        parent_text: str,
        ordinal: int,
        entities: list,
        image_path: str = "",
        image_url: str = "",
        table_json: dict | None = None,
        diagram_mermaid: str = "",
        meta: dict | None = None,
    ) -> Chunk:
        content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        chunk_id = _uid(
            document.doc_id,
            "|".join(section.path),
            chunk_type,
            str(ordinal),
            content_hash,
        )
        return Chunk(
            chunk_id=chunk_id,
            doc_id=document.doc_id,
            doc_title=document.title,
            section_path=section.path,
            page=section.page,
            chunk_type=chunk_type,
            raw_text=raw_text,
            enriched_text=enriched_text,
            summary=summary,
            parent_id=parent_id,
            parent_text=parent_text[: self.settings.parent_max_chars],
            source_url=_source_url(document),
            image_path=image_path,
            image_url=image_url,
            table_json=table_json,
            diagram_mermaid=diagram_mermaid,
            entities=[str(e)[:180] for e in entities][:30],
            content_hash=content_hash,
            version=document.version,
            meta={
                "doc_type": document.source_type,
                "doc_kind": document.kind,
                "source_path": document.source_path,
                **(meta or {}),
            },
        )

    def _parent_text(
        self,
        document: Document,
        section: Section,
        enrichment: dict,
        section_text: str,
    ) -> str:
        parts = [
            f"{document.title} > {' > '.join(section.path)}",
            enrichment.get("summary", ""),
            section_text,
        ]
        return "\n\n".join(p for p in parts if p)[: self.settings.parent_max_chars]


def _pack_text(text: str, max_chars: int, overlap: int) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        for piece in _split_long(paragraph, max_chars):
            if current and current_len + len(piece) + 2 > max_chars:
                chunks.append("\n\n".join(current))
                tail = chunks[-1][-overlap:] if overlap else ""
                current = [tail] if tail else []
                current_len = len(tail)
            current.append(piece)
            current_len += len(piece) + 2
    if current:
        chunks.append("\n\n".join(current))
    return [c.strip() for c in chunks if c.strip()]


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    result: list[str] = []
    buffer = ""
    for sentence in sentences:
        if buffer and len(buffer) + len(sentence) + 1 > max_chars:
            result.append(buffer.strip())
            buffer = ""
        buffer += (" " if buffer else "") + sentence
    if buffer:
        result.append(buffer.strip())
    return result or [text[:max_chars]]


def _join_enriched(
    doc_title: str,
    section_path: list[str],
    raw: str,
    summary: str,
    questions: list[str],
) -> str:
    header = f"{doc_title} > {' > '.join(section_path)}"
    parts = [header, summary]
    if questions:
        parts.append("Вопросы: " + "; ".join(questions[:6]))
    parts.append(raw)
    return "\n\n".join(p for p in parts if p)[:12000]


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "|".join(parts)))


def _is_diagram(section: Section, block: Block) -> bool:
    title = " ".join(section.path).lower()
    return any(word in title for word in ("схема", "процесс", "диаграмма", "bpmn"))


def _relative_image(settings: Settings, image_path: Path) -> str:
    try:
        return str(image_path.resolve().relative_to(settings.data_dir.resolve()))
    except ValueError:
        return image_path.name


def _image_url(settings: Settings, relative: str) -> str:
    if not settings.image_base_url:
        return ""
    return f"{settings.image_base_url}/{relative.lstrip('/')}"


def _source_url(document: Document) -> str:
    if document.metadata.get("github_url"):
        return str(document.metadata["github_url"])
    return document.source_path


def _dedupe(chunks: list[Chunk]) -> list[Chunk]:
    seen: set[str] = set()
    result: list[Chunk] = []
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        result.append(chunk)
    return result
