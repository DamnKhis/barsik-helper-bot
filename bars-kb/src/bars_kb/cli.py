from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Allow `python src/bars_kb/cli.py ...` without installing the package.
if __package__ in {None, ""}:
    import runpy

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    runpy.run_module("bars_kb.cli", run_name="__main__")
    raise SystemExit(0)

from .config import load_settings
from .eval.runner import run_eval
from .pipeline import ingest


LOG_FORMAT = "[%(asctime)s] %(levelname)s - %(name)s - %(message)s"
logger = logging.getLogger("bars_kb.cli")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bars-kb", description="BARS documentation ingestion pipeline")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level: DEBUG, INFO, WARNING, ERROR (default: INFO)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Build the knowledge base from a local folder")
    p_ingest.add_argument("--source", type=Path, default=None)
    p_ingest.add_argument("--no-export", action="store_true")

    p_sync = sub.add_parser("sync", help="Sync local folder or GitHub docs and rebuild")
    p_sync.add_argument("--source", type=Path, default=None)
    p_sync.add_argument("--github", action="store_true", help="Download docs from GitHub first")

    sub.add_parser("status", help="Show the last build manifest")
    p_eval = sub.add_parser("eval", help="Run retrieval evaluation")
    p_eval.add_argument("--golden", type=Path, default=None)
    p_eval.add_argument("--top-k", type=int, default=8)
    sub.add_parser("fixtures", help="Generate DOCX test fixtures")

    args = parser.parse_args(argv)
    _configure_logging(args.log_level)
    settings = load_settings()

    if args.command in {"ingest", "sync"}:
        source = args.source or settings.inbox
        if args.command == "sync" and args.github:
            from .sources.github import sync_github

            source = sync_github(settings)
        manifest = ingest(settings, source=source, export=not getattr(args, "no_export", False))
        logger.info("build: %s", manifest["build_id"])
        logger.info("collection: %s", manifest["collection"])
        logger.info("manifest: %s", settings.data_dir / "manifest.json")
        return 0

    if args.command == "status":
        path = settings.data_dir / "manifest.json"
        if not path.exists():
            logger.error("No build yet. Run: bars-kb ingest")
            return 1
        logger.info("%s", path.read_text(encoding="utf-8"))
        return 0

    if args.command == "eval":
        try:
            report = run_eval(settings, golden=args.golden, top_k=args.top_k)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 1
        logger.info("%s", json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("questions", 0) else 1

    if args.command == "fixtures":
        from .fixtures import make_fixtures

        make_fixtures(settings)
        logger.info("fixtures written to %s", settings.data_dir / "fixtures")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
