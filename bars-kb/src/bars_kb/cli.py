from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
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
from .pipeline import collect_sources, fingerprint_sources, ingest

LOG_FORMAT = "[%(asctime)s] %(levelname)s - %(name)s - %(message)s"
logger = logging.getLogger("bars_kb.cli")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


def _mock_export_blocked(settings, allow_mock: bool) -> bool:
    """True when an exported build would silently be a mock/hash one."""
    return not allow_mock and (settings.mock_llm or settings.mock_embedding)


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


def _resolve_sources(settings, args) -> list[tuple[Path, str]]:
    """Какие папки собирать: документация + решённые обращения поддержки.

    Если папки заданы явно (``--source``), берём ровно их: так локальные запуски
    и тесты остаются изолированными. Без явных папок документация берётся из
    ``DOCS_PATH``, а обращения — из ``SUPPORT_DOCS_PATH`` (если папка есть).
    """
    explicit = [Path(item) for item in (getattr(args, "source", None) or [])]
    explicit_support = [Path(item) for item in (getattr(args, "support_source", None) or [])]
    no_support = bool(getattr(args, "no_support", False))

    sources: list[tuple[Path, str]] = [(path, "doc") for path in explicit] or [(settings.inbox, "doc")]
    if no_support:
        return sources
    if explicit_support:
        sources.extend((path, "support") for path in explicit_support)
    elif not explicit:
        support = settings.support_inbox
        if Path(support).exists():
            sources.append((Path(support), "support"))
        else:
            logger.debug("support folder is missing, skipping: %s", support)
    return sources


def _needs_rebuild(settings, sources: list[tuple[Path, str]] | None = None) -> tuple[bool, str | None]:
    """Compare document contents with the build clients currently use."""
    files = collect_sources(settings, sources)
    if not files:
        return True, None
    digest, _ = fingerprint_sources(files)
    current = _current_manifest(settings)
    if current and (current.get("source") or {}).get("digest") == digest:
        return False, digest
    return True, digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bars-kb", description="BARS documentation ingestion pipeline")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Logging level: DEBUG, INFO, WARNING, ERROR (default: INFO)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_source_args(target) -> None:
        target.add_argument(
            "--source",
            type=Path,
            action="append",
            default=None,
            help="Папка с документацией (можно указать несколько раз)",
        )
        target.add_argument(
            "--support-source",
            type=Path,
            action="append",
            default=None,
            help="Папка с решёнными обращениями поддержки (по умолчанию docs/support_answers)",
        )
        target.add_argument(
            "--no-support",
            action="store_true",
            help="Не подключать решённые обращения к сборке",
        )

    p_ingest = sub.add_parser("ingest", help="Build the knowledge base from a local folder")
    add_source_args(p_ingest)
    p_ingest.add_argument("--no-export", action="store_true")
    p_ingest.add_argument("--keep", type=int, default=2, help="Builds to keep in dist/ (default: 2)")
    p_ingest.add_argument("--force", action="store_true", help="Rebuild even if documents are unchanged")
    p_ingest.add_argument("--eval", action="store_true", help="Run retrieval eval after the build")
    p_ingest.add_argument("--allow-mock", action="store_true", help="Allow exporting a mock/hash build")

    p_sync = sub.add_parser("sync", help="Sync local folder or GitHub docs and rebuild")
    add_source_args(p_sync)
    p_sync.add_argument("--github", action="store_true", help="Download docs from GitHub first")
    p_sync.add_argument("--keep", type=int, default=2, help="Builds to keep in dist/ (default: 2)")
    p_sync.add_argument("--force", action="store_true", help="Rebuild even if documents are unchanged")
    p_sync.add_argument("--eval", action="store_true", help="Run retrieval eval after the build")
    p_sync.add_argument("--publish", action="store_true", help="Commit and push the updated dist/")
    p_sync.add_argument("--no-push", action="store_true", help="With --publish: commit without pushing")
    p_sync.add_argument("--allow-mock", action="store_true", help="Allow building/publishing a mock/hash build")

    p_watch = sub.add_parser("watch", help="Watch docs and rebuild dist when they change")
    add_source_args(p_watch)
    p_watch.add_argument("--interval", type=int, default=60, help="Seconds between checks (default: 60)")
    p_watch.add_argument("--keep", type=int, default=2, help="Builds to keep in dist/ (default: 2)")
    p_watch.add_argument("--force", action="store_true", help="Rebuild once even if documents are unchanged")
    p_watch.add_argument("--once", action="store_true", help="Check once and exit")
    p_watch.add_argument("--eval", action="store_true", help="Run retrieval eval after a build")
    p_watch.add_argument("--allow-mock", action="store_true", help="Allow exporting a mock/hash build")

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
        sources = _resolve_sources(settings, args)
        if args.command == "sync" and args.github:
            from .sources.github import sync_github

            # Документация приходит из GitHub, обращений в нём может не быть —
            # их берём из локальной папки пакета, если она заведена.
            sources = [(sync_github(settings), "doc")]
            if not args.no_support and Path(settings.support_inbox).exists():
                sources.append((Path(settings.support_inbox), "support"))

        do_export = not getattr(args, "no_export", False)
        manifest = None
        if do_export and not args.force:
            changed, digest = _needs_rebuild(settings, sources)
            if not changed:
                manifest = _current_manifest(settings)
                logger.info(
                    "documents unchanged (digest %s); skipping rebuild",
                    (digest or "")[:12],
                )

        if manifest is None:
            # An exported build replaces dist/LATEST, so a mock/hash build must
            # never overwrite a real one by accident (a cron job without env is
            # the classic case).
            if do_export and _mock_export_blocked(settings, args.allow_mock):
                logger.error(
                    "refusing to export a mock/hash KB; set QWEN_API_KEY "
                    "(or run with --no-export / --allow-mock)"
                )
                return 2
            manifest = ingest(
                settings,
                sources=sources,
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

    if args.command == "watch":
        sources = _resolve_sources(settings, args)
        if _mock_export_blocked(settings, args.allow_mock):
            logger.error(
                "refusing to export a mock/hash KB; set QWEN_API_KEY (or pass --allow-mock)"
            )
            return 2

        interval = max(5, int(args.interval))
        logger.info(
            "watch: %s, check every %ss (Ctrl+C to stop)",
            ", ".join(f"{path} [{kind}]" for path, kind in sources),
            interval,
        )
        force_next = bool(args.force)
        while True:
            files = collect_sources(settings, sources)
            changed = False
            digest = None
            if not files:
                logger.debug("watch: no supported documents in %s", sources)
            elif force_next:
                changed = True
            else:
                changed, digest = _needs_rebuild(settings, sources)

            if changed and files:
                try:
                    manifest = ingest(
                        settings,
                        sources=sources,
                        export=True,
                        keep=args.keep,
                        evaluate=args.eval,
                    )
                    logger.info(
                        "watch: new build %s (digest %s)",
                        manifest["build_id"],
                        (digest or "")[:12] or "changed",
                    )
                except Exception as exc:
                    logger.warning("watch: rebuild failed, will retry: %s", exc)
                force_next = False
            elif files:
                logger.debug("watch: unchanged (digest %s)", (digest or "")[:12])

            if args.once:
                return 0
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                logger.info("watch: stopped")
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
            support = (report.get("by_type") or {}).get("support")
            if support:
                logger.info(
                    "support eval: questions=%s hit@1=%.4f hit@3=%.4f",
                    support["questions"],
                    support["hit@1"],
                    support["hit@3"],
                )
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
