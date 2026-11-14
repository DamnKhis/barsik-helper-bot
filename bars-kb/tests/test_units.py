from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from bars_kb.chunker import Chunker
from bars_kb.config import load_settings
from bars_kb.embeddings import build_embedder
from bars_kb.embeddings.hash_embedder import HashEmbedder
from bars_kb.embeddings.sparse import build_vocab, encode_sparse
from bars_kb.eval.runner import _is_relevant
from bars_kb.exports import (
    list_builds,
    previous_build,
    prune_builds,
    read_latest,
    resolve_export_dir,
    write_latest,
)
from bars_kb.models import Block, Document, Section, table_to_markdown
from bars_kb.parsers.clean import clean_text
from bars_kb.parsers.docx_parser import parse_docx
from bars_kb.parsers.image_filter import inspect_image
from bars_kb.parsers.pdf_parser import parse_pdf
from bars_kb.pipeline import source_fingerprint
from bars_kb.providers import build_llm, build_vision
from bars_kb.providers.qwen import QwenChat, QwenEmbeddings
from bars_kb.publish import publish_dist

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "fixtures"


class TableTest(unittest.TestCase):
    def test_markdown_table(self) -> None:
        rows = [["Параметр", "Значение"], ["Модель", "Стол"], ["Номер", "2334-26"]]
        markdown = table_to_markdown(rows)
        self.assertIn("| Параметр | Значение |", markdown)
        self.assertIn("| Модель | Стол |", markdown)
        self.assertEqual(markdown.count("\n"), 3)


class EmbeddingTest(unittest.TestCase):
    def test_hash_embedder_is_deterministic(self) -> None:
        embedder = HashEmbedder(dim=64)
        first = embedder.embed_documents(["реестр мебели"])[0]
        second = embedder.embed_query("реестр мебели")
        self.assertEqual(len(first), 64)
        self.assertEqual(first, second)
        self.assertAlmostEqual(sum(v * v for v in first), 1.0, places=6)

    def test_sparse_encoding(self) -> None:
        vocab = build_vocab(["фильтр !> 200", "реестр мебели"])
        indices, values = encode_sparse("фильтр 200", vocab)
        self.assertEqual(len(indices), 2)
        self.assertEqual(indices, sorted(indices))
        self.assertTrue(all(value > 0 for value in values))
        self.assertEqual(encode_sparse("нет такого", vocab), ([], []))


class CleanTest(unittest.TestCase):
    def test_word_footer_removed(self) -> None:
        raw = "Полезный текст\nОшибка! Используйте вкладку \"Главная\"\nдолжен здесь отображаться\nЕщё текст"
        cleaned = clean_text(raw)
        self.assertIn("Полезный текст", cleaned)
        self.assertIn("Ещё текст", cleaned)
        self.assertNotIn("Ошибка", cleaned)
        self.assertNotIn("отображаться", cleaned)


class DocxParserTest(unittest.TestCase):
    def test_fixture_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            document = parse_docx(FIXTURES / "fixture_core.docx", Path(tmp), "test")
        self.assertEqual(document.title, "Тестовый регламент БАРС")
        self.assertGreaterEqual(len(document.sections), 3)
        self.assertEqual(sum(len(s.tables) for s in document.sections), 1)
        table = next(s for s in document.sections if s.tables).tables[0].table
        self.assertEqual(table[0][:3], ["Параметр", "Значение", "Примечание"])
        self.assertIn("2334-26", " ".join(cell for row in table for cell in row))


class ChunkerTest(unittest.TestCase):
    def test_tricky_fixture_produces_faq(self) -> None:
        settings = load_settings(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local = replace(settings, data_dir=tmp_path, images_dir=tmp_path / "images")
            document = parse_docx(FIXTURES / "fixture_tricky.docx", local.images_dir, "test")
            chunks = Chunker(local, llm=None, vision=None).document_chunks(document)
        self.assertTrue(chunks)
        self.assertTrue(all(chunk.chunk_id for chunk in chunks))
        self.assertEqual(len({chunk.chunk_id for chunk in chunks}), len(chunks))
        faq = [chunk for chunk in chunks if chunk.chunk_type == "faq"]
        self.assertTrue(faq)
        self.assertTrue(any("!=" in chunk.raw_text for chunk in faq))
        text = [chunk for chunk in chunks if chunk.chunk_type == "text"]
        self.assertTrue(all(chunk.parent_id and chunk.parent_text for chunk in text))


class QwenProviderTest(unittest.TestCase):
    def test_one_key_switches_all_models(self) -> None:
        settings = load_settings(ROOT)
        without = replace(settings, qwen_api_key="")
        self.assertIsNone(build_llm(without))
        self.assertIsNone(build_vision(without))
        self.assertIsInstance(build_embedder(without), HashEmbedder)

        with_key = replace(
            settings,
            qwen_api_key="test-key",
            llm_provider="qwen",
            vision_provider="qwen",
            embedding_provider="qwen",
        )
        llm = build_llm(with_key)
        vision = build_vision(with_key)
        embedder = build_embedder(with_key)
        self.assertIsInstance(llm, QwenChat)
        self.assertIsInstance(vision, QwenChat)
        self.assertIsInstance(embedder, QwenEmbeddings)
        self.assertEqual(llm.model, "qwen/qwen3.8-flash")
        self.assertEqual(vision.model, "qwen/qwen3.8-flash")
        self.assertEqual(embedder.model, "qwen/qwen3-embedding-8b")
        self.assertEqual(embedder.dim, 1024)

    def test_payload_uses_json_mode(self) -> None:
        chat = QwenChat("key", "https://openrouter.ai/api/v1", "qwen/qwen3.8-flash")
        payload = chat._payload([{"role": "user", "content": "test"}])
        self.assertEqual(payload["model"], "qwen/qwen3.8-flash")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["reasoning"], {"enabled": False})
        self.assertNotIn("enable_thinking", payload)

    def test_payload_supports_dashscope(self) -> None:
        chat = QwenChat("key", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash")
        payload = chat._payload([{"role": "user", "content": "test"}])
        self.assertEqual(payload["enable_thinking"], False)
        self.assertNotIn("reasoning", payload)


def _image_bytes(image: Image.Image, fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt)
    return buffer.getvalue()


def _screenshot_bytes(width: int = 400, height: int = 300) -> bytes:
    image = Image.new("RGB", (width, height))
    image.putdata(
        [
            ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
            for y in range(height)
            for x in range(width)
        ]
    )
    return _image_bytes(image)


class ImageFilterTest(unittest.TestCase):
    def test_small_icon_rejected(self) -> None:
        verdict = inspect_image(_image_bytes(Image.new("RGB", (32, 32), "white")))
        self.assertFalse(verdict.ok)
        self.assertIn("too small", verdict.reason)

    def test_flat_placeholder_rejected(self) -> None:
        verdict = inspect_image(_image_bytes(Image.new("RGB", (250, 250), (240, 240, 240))))
        self.assertFalse(verdict.ok)
        self.assertIn("placeholder", verdict.reason)

    def test_gradient_screenshot_kept(self) -> None:
        verdict = inspect_image(_screenshot_bytes())
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertEqual((verdict.width, verdict.height), (400, 300))

    def test_unreadable_bytes_rejected(self) -> None:
        verdict = inspect_image(b"not an image")
        self.assertFalse(verdict.ok)
        self.assertIn("unreadable", verdict.reason)


class PdfParserTest(unittest.TestCase):
    def test_heading_paragraph_and_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "sample.pdf"
            doc = pymupdf.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((85, 90), "1 Test section", fontsize=16)
            page.insert_text(
                (85, 130),
                "This paragraph describes the furniture registry in detail.",
                fontsize=10,
            )
            page.insert_image(pymupdf.Rect(85, 200, 385, 350), stream=_screenshot_bytes())
            doc.save(pdf_path)
            doc.close()

            document = parse_pdf(pdf_path, root / "images", "test")

            self.assertEqual(document.title, "1 Test section")
            self.assertEqual(document.page_count, 1)
            self.assertIn("1 Test section", [section.title for section in document.sections])
            self.assertIn("furniture registry", document.text)

            images = [block for section in document.sections for block in section.images]
            self.assertEqual(len(images), 1)
            self.assertTrue(Path(images[0].image_path).exists())
            self.assertEqual(document.metadata["images"], 1)


class EnrichmentPoolTest(unittest.TestCase):
    class _CountingVision:
        def __init__(self) -> None:
            self.calls = 0

        def describe_image(self, path: Path, prompt: str) -> dict:
            self.calls += 1
            return {
                "kind": "screenshot",
                "description": Path(path).name,
                "ocr_text": "",
                "questions": [],
            }

    def _document(self, root: Path, image_count: int, same_image: bool) -> Document:
        sections = []
        for index in range(image_count):
            image_path = root / "shared.png"
            if not same_image:
                image_path = root / f"image-{index}.png"
                Image.new("RGB", (200, 150), (index * 30, 120, 200)).save(image_path)
            sections.append(
                Section(
                    title=f"Section {index}",
                    level=1,
                    path=[f"Section {index}"],
                    blocks=[Block(kind="image", image_path=str(image_path), caption=f"pic {index}")],
                )
            )
        return Document(
            doc_id="pool-test",
            title="Pool test",
            source_path="memory",
            source_type="pdf",
            version="test",
            sections=sections,
        )

    def test_parallel_preserves_order(self) -> None:
        settings = load_settings(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Image.new("RGB", (200, 150), (0, 0, 0)).save(root / "shared.png")
            document = self._document(root, image_count=4, same_image=False)
            results = {}
            for workers in (1, 4):
                local = replace(
                    settings,
                    data_dir=root / f"cache-{workers}",
                    qwen_api_key="test",
                    llm_provider="qwen",
                    vision_provider="qwen",
                    enrichment_workers=workers,
                )
                vision = self._CountingVision()
                chunks = Chunker(local, llm=None, vision=vision).document_chunks(document)
                results[workers] = ([chunk.chunk_id for chunk in chunks], vision.calls)
            self.assertEqual(results[1], results[4])
            self.assertEqual(results[4][1], 4)

    def test_shared_image_enriched_once(self) -> None:
        settings = load_settings(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Image.new("RGB", (300, 200), (10, 120, 200)).save(root / "shared.png")
            document = self._document(root, image_count=4, same_image=True)
            local = replace(
                settings,
                data_dir=root / "cache",
                qwen_api_key="test",
                llm_provider="qwen",
                vision_provider="qwen",
                enrichment_workers=4,
            )
            vision = self._CountingVision()
            chunks = Chunker(local, llm=None, vision=vision).document_chunks(document)
            self.assertEqual(len(chunks), 4)
            self.assertEqual(vision.calls, 1)


class DistFlowTest(unittest.TestCase):
    BUILDS = [
        "20260101T000001_deadbeef",
        "20260102T000002_feedface",
        "20260103T000003_cafebabe",
    ]

    def _make_build(self, dist: Path, build_id: str, manifest: dict | None = None) -> Path:
        path = dist / build_id
        path.mkdir(parents=True)
        payload = {
            "build_id": build_id,
            "embedding": {"provider": "qwen"},
            "enrichment": {"llm_provider": "qwen"},
            "counts": {"chunks": 1, "images": 0},
        }
        payload.update(manifest or {})
        (path / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
        (path / "collection_config.json").write_text(
            json.dumps({"collection": f"bars_kb_{build_id}"}),
            encoding="utf-8",
        )
        (path / "sparse_vocab.json").write_text("{}", encoding="utf-8")
        return path

    @staticmethod
    def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_source_fingerprint_tracks_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.pdf").write_bytes(b"one")
            (root / "b.docx").write_bytes(b"two")
            files = sorted(root.iterdir())
            first, records = source_fingerprint(files, root)
            second, _ = source_fingerprint(files, root)
            self.assertEqual(first, second)
            self.assertEqual([record["path"] for record in records], ["a.pdf", "b.docx"])
            (root / "a.pdf").write_bytes(b"changed")
            third, _ = source_fingerprint(files, root)
            self.assertNotEqual(first, third)

    def test_latest_prune_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            for index, build_id in enumerate(self.BUILDS):
                self._make_build(dist, build_id, {"created_at": f"2026-01-0{index + 1}"})
            write_latest(dist, self.BUILDS[2])
            self.assertEqual(read_latest(dist), self.BUILDS[2])
            self.assertEqual(previous_build(dist, self.BUILDS[2]), self.BUILDS[1])

            removed = prune_builds(dist, keep=2, current=self.BUILDS[2])
            self.assertEqual(removed, [self.BUILDS[0]])
            remaining = sorted(path.name for path in dist.iterdir() if path.is_dir())
            self.assertEqual(remaining, self.BUILDS[1:])
            self.assertEqual(
                [build["build_id"] for build in list_builds(dist)],
                self.BUILDS[1:],
            )

    def test_resolve_export_dir_follows_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            for build_id in self.BUILDS[:2]:
                self._make_build(dist, build_id)
            write_latest(dist, self.BUILDS[1])
            self.assertEqual(resolve_export_dir(dist), (dist / self.BUILDS[1]).resolve())
            self.assertEqual(
                resolve_export_dir(dist / self.BUILDS[0]),
                (dist / self.BUILDS[0]).resolve(),
            )

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_publish_commits_only_dist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "-q")
            (repo / "README.md").write_text("base", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(
                repo, "-c", "user.name=test", "-c", "user.email=test@example.com",
                "commit", "-q", "-m", "init",
            )

            settings = replace(
                load_settings(ROOT),
                root=repo,
                dist_dir=repo / "dist",
                data_dir=repo / "data",
            )
            self._make_build(settings.dist_dir, self.BUILDS[0])
            write_latest(settings.dist_dir, self.BUILDS[0])

            result = publish_dist(settings, self.BUILDS[0], push=False)
            self.assertTrue(result["committed"])
            committed = self._git(repo, "show", "--name-only", "--pretty=format:", "HEAD")
            self.assertIn(f"dist/{self.BUILDS[0]}/manifest.json", committed.stdout)
            self.assertIn("dist/LATEST", committed.stdout)

            again = publish_dist(settings, self.BUILDS[0], push=False)
            self.assertFalse(again["committed"])

    def test_publish_refuses_mock_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            self._make_build(
                dist,
                self.BUILDS[0],
                {"embedding": {"provider": "hash"}, "enrichment": {"llm_provider": "mock"}},
            )
            write_latest(dist, self.BUILDS[0])
            settings = replace(
                load_settings(ROOT),
                root=root,
                dist_dir=dist,
                data_dir=root / "data",
            )
            with self.assertRaises(RuntimeError):
                publish_dist(settings, self.BUILDS[0], push=False)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_publish_refuses_unrelated_staged_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "-q")
            (repo / "README.md").write_text("base", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(
                repo, "-c", "user.name=test", "-c", "user.email=test@example.com",
                "commit", "-q", "-m", "init",
            )
            (repo / "notes.txt").write_text("draft", encoding="utf-8")
            self._git(repo, "add", "notes.txt")

            settings = replace(
                load_settings(ROOT),
                root=repo,
                dist_dir=repo / "dist",
                data_dir=repo / "data",
            )
            self._make_build(settings.dist_dir, self.BUILDS[0])
            write_latest(settings.dist_dir, self.BUILDS[0])
            with self.assertRaises(RuntimeError):
                publish_dist(settings, self.BUILDS[0], push=False)


class EvalRelevanceTest(unittest.TestCase):
    def test_relevance_exact_match(self) -> None:
        payload = {
            "doc_id": "reestr-mebeli",
            "section_path": ["4 Список полей"],
            "chunk_type": "faq",
            "raw_text": "Статус — текущий статус мебели",
        }
        item = {
            "expected_doc_id": "reestr-mebeli",
            "expected_section_contains": ["4 список полей"],
            "expected_chunk_type": "faq",
            "must_contain": ["статус"],
        }
        self.assertTrue(_is_relevant(payload, item))
        self.assertFalse(_is_relevant({**payload, "doc_id": "bp-komandirovka"}, item))
        self.assertFalse(_is_relevant({**payload, "chunk_type": "text"}, item))
        self.assertFalse(_is_relevant(payload, {**item, "must_contain": ["нет такого"]}))


if __name__ == "__main__":
    unittest.main()
