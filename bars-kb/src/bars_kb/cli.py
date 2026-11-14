from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_settings
from .eval.runner import run_eval
from .pipeline import ingest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bars-kb", description="BARS documentation ingestion pipeline")
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
    settings = load_settings()

    if args.command in {"ingest", "sync"}:
        source = args.source or settings.inbox
        if args.command == "sync" and args.github:
            from .sources.github import sync_github

            source = sync_github(settings)
        manifest = ingest(settings, source=source, export=not getattr(args, "no_export", False))
        print(f"build: {manifest['build_id']}")
        print(f"collection: {manifest['collection']}")
        print(f"manifest: {settings.data_dir / 'manifest.json'}")
        return 0

    if args.command == "status":
        path = settings.data_dir / "manifest.json"
        if not path.exists():
            print("No build yet. Run: bars-kb ingest")
            return 1
        print(path.read_text(encoding="utf-8"))
        return 0

    if args.command == "eval":
        report = run_eval(settings, golden=args.golden, top_k=args.top_k)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("questions", 0) else 1

    if args.command == "fixtures":
        from .fixtures import make_fixtures

        make_fixtures(settings)
        print(f"fixtures written to {settings.data_dir / 'fixtures'}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
