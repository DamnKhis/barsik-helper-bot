from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bars_kb.chunker import Chunker
from bars_kb.config import load_settings
from bars_kb.embeddings import build_embedder
from bars_kb.embeddings.hash_embedder import HashEmbedder
from bars_kb.embeddings.sparse import build_vocab, encode_sparse
from bars_kb.eval.runner import _is_relevant
from bars_kb.models import table_to_markdown
from bars_kb.parsers.clean import clean_text
from bars_kb.parsers.docx_parser import parse_docx
from bars_kb.providers import build_llm, build_vision
from bars_kb.providers.qwen import QwenChat, QwenEmbeddings

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
