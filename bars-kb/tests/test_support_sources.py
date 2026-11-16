"""Решённые обращения поддержки как второй источник сборки.

Проверяем: разбор тикета (.md + вложения), режимы индексации, попадание тикета в
общий digest и полноценную сборку, где у чанков есть ``doc_kind=support`` и
картинки из ``attachments/<KEY>/``.
"""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bars_kb.config import load_settings
from bars_kb.parsers import parse_document
from bars_kb.pipeline import collect_sources, fingerprint_sources, ingest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

TICKET_MD = """# KAN-21: полезный ответ поддержки

- Тикет: KAN-21
- Тема: БАРС-Офис: позови оператора
- Ссылка: https://example.atlassian.net/browse/KAN-21

## История диалога с ИИ
Обращение пользователя в поддержку «БАРС-Офис».
Пользователь: читос. (@cheeeeeeeeeetos), Telegram ID 6868313569

Диалог с ИИ до вызова оператора:
Пользователь: оаоаво
ИИ: Пожалуйста, задайте вопрос по системе «БАРС-Офис».

## Вопрос 1
Как оформить пропуск и кто согласовывает заявку?

![step.png](attachments/KAN-21/step.png)

## Ответ поддержки 1
Заявку оформляет офис-менеджер после проверки Службой безопасности.
"""

DOC_MD = """# Регламент

## Раздел
Документация про командировки и мебель.
"""


class SupportSourceTest(unittest.TestCase):
    def _settings(self, root: Path):
        return replace(
            load_settings(ROOT),
            root=root,
            data_dir=root / "data",
            images_dir=root / "data" / "images",
            qdrant_path=root / "data" / "qdrant",
            dist_dir=root / "dist",
            inbox=root / "docs" / "source",
            support_inbox=root / "docs" / "support_answers",
            llm_provider="mock",
            vision_provider="mock",
            embedding_provider="hash",
            qwen_api_key="",
            qdrant_url="",
            qdrant_api_key="",
            enrichment_workers=1,
        )

    def _sources(self, root: Path) -> tuple[Path, Path]:
        docs = root / "docs" / "source"
        support = root / "docs" / "support_answers"
        (support / "attachments" / "KAN-21").mkdir(parents=True)
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "reglament.md").write_text(DOC_MD, encoding="utf-8")
        (support / "KAN-21.md").write_text(TICKET_MD, encoding="utf-8")
        # Картинка достаточно крупная и неоднородная, иначе inspect_image её отсеет
        Image.new("RGB", (300, 200), (10, 120, 200)).save(support / "attachments" / "KAN-21" / "step.png")
        return docs, support

    # --- разбор тикета ----------------------------------------------------

    def test_parse_support_answer_keeps_qa_and_drops_ai_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            _, support = self._sources(root)

            document = parse_document(
                support / "KAN-21.md",
                settings.images_dir,
                version="v1",
                kind="support",
                support_mode="qa",
            )

            self.assertEqual(document.kind, "support")
            self.assertTrue(document.title.startswith("KAN-21"))
            self.assertEqual(document.metadata["issue_key"], "KAN-21")
            titles = [section.title for section in document.sections]
            self.assertIn("Вопрос 1", titles)
            self.assertIn("Ответ поддержки 1", titles)
            self.assertNotIn("История диалога с ИИ", titles)

            images = [block for section in document.sections for block in section.images]
            self.assertEqual(len(images), 1)
            copied = Path(images[0].image_path)
            self.assertTrue(copied.is_file())
            self.assertIn(settings.images_dir, copied.parents)
            self.assertEqual(images[0].meta["source"], "markdown")
            self.assertTrue(images[0].meta["sha256"])

    def test_support_index_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            _, support = self._sources(root)
            ticket = support / "KAN-21.md"

            answers = parse_document(
                ticket, settings.images_dir, "v1", kind="support", support_mode="answers"
            )
            self.assertEqual([s.title for s in answers.sections], ["Ответ поддержки 1"])

            everything = parse_document(
                ticket, settings.images_dir, "v1", kind="support", support_mode="all"
            )
            self.assertIn("История диалога с ИИ", [s.title for s in everything.sections])

    def test_documentation_keeps_kind_doc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            docs, _ = self._sources(root)

            document = parse_document(docs / "reglament.md", settings.images_dir, "v1")

            self.assertEqual(document.kind, "doc")

    # --- тикеты без ответа поддержки --------------------------------------

    def test_ticket_without_support_answer_has_no_sections(self) -> None:
        """В тикете KAN-21 (в репозитории) ответа поддержки нет — учить нечему."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            _, support = self._sources(root)
            dialog_only = support / "KAN-22.md"
            dialog_only.write_text(
                "# KAN-22: полезный ответ поддержки\n\n"
                "- Тикет: KAN-22\n\n"
                "## История диалога с ИИ\nПользователь: оаоаво\n\n"
                "## Диалог\n\n**Пользователь:** еще раз\n",
                encoding="utf-8",
            )

            document = parse_document(
                dialog_only, settings.images_dir, "v1", kind="support", support_mode="qa"
            )

            self.assertEqual(document.sections, [])
            self.assertEqual(document.kind, "support")

    def test_dialogs_mode_keeps_dialog_when_no_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            _, support = self._sources(root)
            dialog_only = support / "KAN-22.md"
            dialog_only.write_text(
                "# KAN-22\n\n## История диалога с ИИ\nПользователь: оаоаво\n\n"
                "## Диалог\n\n**Пользователь:** еще раз\n**Поддержка:** смотри инструкцию\n",
                encoding="utf-8",
            )

            document = parse_document(
                dialog_only, settings.images_dir, "v1", kind="support", support_mode="dialogs"
            )

            titles = [section.title for section in document.sections]
            self.assertEqual(titles, ["Диалог"])

    def test_ingest_skips_tickets_without_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            self._sources(root)
            (settings.support_inbox / "KAN-99.md").write_text(
                "# KAN-99\n\n## Диалог\n\n**Пользователь:** не помогло\n",
                encoding="utf-8",
            )

            manifest = ingest(settings, export=True, keep=2)

            self.assertEqual(manifest["counts"]["documents"], 2)  # doc + KAN-21 с ответом
            self.assertEqual(manifest["counts"]["support_documents"], 1)
            doc_ids = {record["doc_id"] for record in manifest["documents"]}
            self.assertNotIn("kan-99", doc_ids)

    # --- источники и digest ----------------------------------------------

    def test_collect_sources_covers_both_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            self._sources(root)

            files = collect_sources(settings)

            kinds = sorted(entry.kind for entry in files)
            self.assertEqual(kinds, ["doc", "support"])
            keys = sorted(entry.key for entry in files)
            self.assertIn("support/support_answers/KAN-21.md", keys)
            self.assertIn("doc/source/reglament.md", keys)

    def test_digest_changes_when_ticket_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            _, support = self._sources(root)

            before, _ = fingerprint_sources(collect_sources(settings))
            (support / "KAN-21.md").write_text(TICKET_MD + "\nДополнили ответ.\n", encoding="utf-8")
            after, _ = fingerprint_sources(collect_sources(settings))

            self.assertNotEqual(before, after)

    def test_missing_support_folder_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            (root / "docs" / "source").mkdir(parents=True)
            (root / "docs" / "source" / "a.txt").write_text("текст", encoding="utf-8")

            files = collect_sources(settings)

            self.assertEqual([entry.kind for entry in files], ["doc"])

    # --- сборка -----------------------------------------------------------

    def test_ingest_marks_support_chunks_and_exports_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            self._sources(root)

            manifest = ingest(settings, export=True, keep=2)

            counts = manifest["counts"]
            self.assertEqual(counts["documents"], 2)
            self.assertEqual(counts["support_documents"], 1)
            self.assertGreaterEqual(counts["images"], 1)

            kinds = {entry["kind"] for entry in manifest["sources"]}
            self.assertEqual(kinds, {"doc", "support"})

            build = settings.dist_dir / manifest["build_id"]
            with gzip.open(build / "chunks.jsonl.gz", "rt", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]

            support_rows = [row for row in rows if row["payload"].get("doc_kind") == "support"]
            self.assertTrue(support_rows)
            answer_rows = [
                row
                for row in support_rows
                if "Ответ поддержки 1" in (row["payload"].get("section_path") or [])
            ]
            self.assertTrue(answer_rows)
            self.assertTrue(answer_rows[0]["payload"]["doc_title"].startswith("KAN-21"))
            self.assertTrue(answer_rows[0]["payload"]["source_path"].endswith("support_answers/KAN-21.md"))

            # шумный диалог с ИИ и персональные данные в индекс не попали
            joined = " ".join(row["payload"].get("raw_text", "") for row in support_rows)
            self.assertNotIn("6868313569", joined)
            self.assertNotIn("оаоаво", joined)

            # картинка тикета дошла до сборки
            picture_rows = [row for row in support_rows if row["payload"].get("image_path")]
            self.assertTrue(picture_rows)
            exported = build / picture_rows[0]["payload"]["image_path"]
            self.assertTrue(exported.is_file())

            # документация осталась обычной
            doc_rows = [row for row in rows if row["payload"].get("doc_kind") == "doc"]
            self.assertTrue(doc_rows)
            self.assertEqual(
                {row["payload"].get("doc_kind") for row in rows},
                {"doc", "support"},
            )


if __name__ == "__main__":
    unittest.main()
