from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import kb_autosync


class AutoSyncConfigTest(unittest.TestCase):
    def test_enabled_by_default_and_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KB_AUTOSYNC", None)
            self.assertTrue(kb_autosync.enabled())
        for value in ("0", "false", "no", "off", "FALSE"):
            with mock.patch.dict(os.environ, {"KB_AUTOSYNC": value}, clear=False):
                self.assertFalse(kb_autosync.enabled())

    def test_command_uses_watch_and_passes_options(self) -> None:
        env = {
            "KB_AUTOSYNC_INTERVAL": "30",
            "KB_AUTOSYNC_SOURCE": "docs/source",
            "KB_AUTOSYNC_EVAL": "1",
            "KB_AUTOSYNC_ALLOW_MOCK": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            command = kb_autosync._command()
        self.assertEqual(command[1:4], ["-m", "bars_kb.cli", "watch"])
        self.assertEqual(command[command.index("--interval") + 1], "30")
        self.assertEqual(
            Path(command[command.index("--source") + 1]),
            (kb_autosync.KB_DIR / "docs/source").resolve(),
        )
        self.assertIn("--eval", command)
        self.assertNotIn("--allow-mock", command)

    def test_source_dir_defaults_and_resolves_relative(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KB_AUTOSYNC_SOURCE", None)
            self.assertEqual(
                kb_autosync.source_dir(),
                (kb_autosync.KB_DIR / "docs/source").resolve(),
            )
        with mock.patch.dict(os.environ, {"KB_AUTOSYNC_SOURCE": "docs/custom"}, clear=False):
            self.assertEqual(
                kb_autosync.source_dir(),
                (kb_autosync.KB_DIR / "docs/custom").resolve(),
            )

    def test_stage_document_copies_and_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "incoming.pdf"
            source.write_bytes(b"version-1")
            target_dir = root / "kb-source"
            with mock.patch.dict(
                os.environ, {"KB_AUTOSYNC_SOURCE": str(target_dir)}, clear=False
            ):
                staged = kb_autosync.stage_document(source)
                self.assertEqual(staged, target_dir / "incoming.pdf")
                self.assertEqual(staged.read_bytes(), b"version-1")

                source.write_bytes(b"version-2")
                kb_autosync.stage_document(source, "renamed.PDF")
                self.assertEqual((target_dir / "renamed.PDF").read_bytes(), b"version-2")
                self.assertEqual(list(target_dir.glob("*.tmp")), [])

    def test_stage_document_rejects_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "payload.exe"
            source.write_bytes(b"not a document")
            with self.assertRaises(ValueError):
                kb_autosync.stage_document(source)

    def test_stage_document_keeps_original_error_when_cleanup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "incoming.pdf"
            source.write_bytes(b"version-1")
            with mock.patch.dict(
                os.environ, {"KB_AUTOSYNC_SOURCE": str(root / "kb-source")}, clear=False
            ):
                with mock.patch.object(
                    kb_autosync.shutil,
                    "copy2",
                    side_effect=OSError(30, "Read-only file system"),
                ):
                    with mock.patch.object(
                        Path,
                        "unlink",
                        side_effect=OSError(30, "Read-only file system"),
                    ):
                        with self.assertRaisesRegex(OSError, "Read-only file system"):
                            kb_autosync.stage_document(source)

    @unittest.skipUnless(os.name == "posix", "pid check is POSIX-specific")
    def test_lock_prevents_second_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "autosync.lock"
            with mock.patch.object(kb_autosync, "LOCK_FILE", lock):
                self.assertTrue(kb_autosync._acquire_lock())
                self.assertFalse(kb_autosync._acquire_lock())
                kb_autosync._release_lock()
                self.assertTrue(kb_autosync._acquire_lock())
                kb_autosync._release_lock()

    @unittest.skipUnless(os.name == "posix", "pid check is POSIX-specific")
    def test_stale_lock_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "autosync.lock"
            lock.write_text("2147483647", encoding="utf-8")
            with mock.patch.object(kb_autosync, "LOCK_FILE", lock):
                self.assertTrue(kb_autosync._acquire_lock())
                kb_autosync._release_lock()

    def test_disabled_start_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"KB_AUTOSYNC": "0"}, clear=False):
            self.assertIsNone(asyncio.run(kb_autosync.start()))


if __name__ == "__main__":
    unittest.main()
