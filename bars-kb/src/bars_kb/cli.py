from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
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
from .exports import latest_manifest, list_builds, previous_build, read_latest, write_latest
from .pipeline import ingest, iter_sources, source_fingerprint


LOG_FORMAT = "[%(asctime)s] %(levelname)s - %(name)s - %(message)s"
logger = logging.getLogger("bars_kb.cli")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


def _current_manifest(settings) -> dict | None:
    """Return the manifest of the build that clients currently resolve."""
    manifest = latest_manifest(settings.dist_dir)
    if manifest:
        build_id = manifest.get("build_id", "")
        if build_id and (settings.dist_dir / build_id).is_dir():
            return manifest
    path = settings.data_dir / "manifest.json"
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        build_id = manifest.get("build_id", "")
        if build_id and (settings.dist_dir / build_id).is_dir():
            return manifest
    return None


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
    p_ingest.add_argument("--keep", type=int, default=2, help="Builds to keep in dist/ (default: 2)")
    p_ingest.add_argument("--force", action="store_true", help="Rebuild even if documents are unchanged")
    p_ingest.add_argument("--eval", action="store_true", help="Run retrieval eval after the build")

    p_sync = sub.add_parser("sync", help="Sync local folder or GitHub docs and rebuild")
    p_sync.add_argument("--source", type=Path, default=None)
    p_sync.add_argument("--github", action="store_true", help="Download docs from GitHub first")
    p_sync.add_argument("--keep", type=int, default=2, help="Builds to keep in dist/ (default: 2)")
    p_sync.add_argument("--force", action="store_true", help="Rebuild even if documents are unchanged")
    p_sync.add_argument("--eval", action="store_true", help="Run retrieval eval after the build")
    p_sync.add_argument("--publish", action="store_true", help="Commit and push the updated dist/")
    p_sync.add_argument("--no-push", action="store_true", help="With --publish: commit without pushing")
    p_sync.add_argument("--allow-mock", action="store_true", help="Allow publishing a mock/hash build")

    p_builds = sub.add_parser("builds", help="List builds in dist/")
    p_builds.add_argument("--json", action="store_true", help="Print raw JSON")

    p_rollback = sub.add_parser("rollback", help="Point dist/LATEST at a previous build")
    p_rollback.add_argument("build_id", nargs="?", default=None)
    p_rollback.add_argument("--publish", action="store_true", help="Commit and push the pointer change")
    p_rollback.add_argument("--no-push", action="store_true", help="With --publish: commit without pushing")
    p_rollback.add_argument("--allow-mock", action="store_true", help="Allow publishing a mock/hash build")

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

        do_export = not getattr(args, "no_export", False)
        manifest = None
        if do_export and not args.force:
            files = iter_sources(source)
            if files:
                digest, _ = source_fingerprint(files, source)
                current = _current_manifest(settings)
                if current and (current.get("source") or {}).get("digest") == digest:
                    manifest = current
                    logger.info(
                        "documents unchanged (digest %s); skipping rebuild",
                        digest[:12],
                    )

        if manifest is None:
            manifest = ingest(
                settings,
                source=source,
                export=do_export,
                keep=args.keep,
                evaluate=getattr(args, "eval", False),
            )

        logger.info("build: %s", manifest["build_id"])
        logger.info("collection: %s", manifest["collection"])
        logger.info("manifest: %s", settings.data_dir / "manifest.json")

        if getattr(args, "publish", False):
            from .publish import publish_dist

            publish_dist(
                settings,
                manifest["build_id"],
                push=not args.no_push,
                allow_mock=args.allow_mock,
            )
        return 0

    if args.command == "builds":
        builds = list_builds(settings.dist_dir)
        current = read_latest(settings.dist_dir)
        if args.json:
            logger.info("%s", json.dumps({"latest": current, "builds": builds}, ensure_ascii=False, indent=2))
        else:
            for build in builds:
                marker = "*" if build["build_id"] == current else " "
                counts = build.get("counts") or {}
                logger.info(
                    "%s %s chunks=%s images=%s created=%s",
                    marker,
                    build["build_id"],
                    counts.get("chunks", "?"),
                    counts.get("images", "?"),
                    build.get("created_at", "?"),
                )
        return 0 if builds else 1

    if args.command == "rollback":
        current = read_latest(settings.dist_dir)
        target = args.build_id or previous_build(settings.dist_dir, current)
        if not target:
            logger.error("No previous build available in %s", settings.dist_dir)
            return 1
        manifest_path = settings.dist_dir / target / "manifest.json"
        if not manifest_path.is_file():
            logger.error("Build not found: %s", target)
            return 1
        write_latest(settings.dist_dir, target)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        (settings.data_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        vocab = settings.dist_dir / target / "sparse_vocab.json"
        if vocab.is_file():
            shutil.copy2(vocab, settings.data_dir / "sparse_vocab.json")
        logger.info("LATEST: %s -> %s", current or "none", target)
        if args.publish:
            from .publish import publish_dist

            publish_dist(
                settings,
                target,
                message=f"chore: rollback KB to {target}",
                push=not args.no_push,
                allow_mock=args.allow_mock,
            )
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
