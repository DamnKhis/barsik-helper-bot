"""Автоматическая пересборка ``bars-kb/dist`` при изменении документации.

Запускается из lifespan API/бота (см. ``app/main.py``): держит дочерний процесс
``bars-kb watch``, который сравнивает digest документов и пересобирает пакет
только при реальном изменении. Отдельный запуск не нужен.

Управление через окружение:

* ``KB_AUTOSYNC=0`` — выключить (по умолчанию включено);
* ``KB_AUTOSYNC_INTERVAL=60`` — период проверки в секундах;
* ``KB_AUTOSYNC_SOURCE`` — папка с документами (по умолчанию
  ``bars-kb/docs/source``). Документы, загруженные админом через бота, тоже
  попадают сюда;
* ``KB_AUTOSYNC_SUPPORT`` — папка с решёнными обращениями поддержки (по
  умолчанию берётся ``SUPPORT_ANSWERS_DIR``, то есть
  ``bars-kb/docs/support_answers``); изменения в ней тоже запускают пересборку;
* ``KB_AUTOSYNC_EVAL=1`` — прогонять golden set после сборки;
* ``KB_AUTOSYNC_ALLOW_MOCK=1`` — разрешить mock/hash-сборку (для отладки).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from app.config import settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KB_DIR = PROJECT_ROOT / "bars-kb"
LOCK_FILE = KB_DIR / "data" / "autosync.lock"
SUPPORTED_SUFFIXES = frozenset({".pdf", ".docx", ".md", ".markdown", ".txt"})

_FALSE = {"0", "false", "no", "off"}


def enabled() -> bool:
    return os.getenv("KB_AUTOSYNC", "1").strip().lower() not in _FALSE


def interval() -> int:
    try:
        return max(5, int(os.getenv("KB_AUTOSYNC_INTERVAL", "60")))
    except ValueError:
        return 60


def source_dir() -> Path:
    """Папка-источник KB; относительный KB_AUTOSYNC_SOURCE считается от bars-kb."""
    raw = os.getenv("KB_AUTOSYNC_SOURCE", "").strip()
    path = Path(raw).expanduser() if raw else KB_DIR / "docs" / "source"
    if not path.is_absolute():
        path = KB_DIR / path
    return path.resolve()


def support_dir() -> Path:
    """Папка с решёнными обращениями поддержки (второй источник для сборки)."""
    raw = os.getenv("KB_AUTOSYNC_SUPPORT", "").strip()
    path = Path(raw).expanduser() if raw else Path(settings.support_answers_dir)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def is_supported_document(name: str | Path) -> bool:
    return Path(name).suffix.lower() in SUPPORTED_SUFFIXES


def stage_document(path: Path, target_name: str | None = None) -> Path:
    """Атомарно положить документ в источник KB для пересборки watcher-ом."""
    if not path.is_file():
        raise FileNotFoundError(path)
    name = Path(target_name or path.name).name
    if name in {"", ".", ".."} or not is_supported_document(name):
        raise ValueError(f"Unsupported document: {name}")

    target = source_dir() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        shutil.copy2(path, temporary)
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() not in _FALSE | {""}


def _python() -> str:
    """Prefer the venv that actually has bars-kb dependencies."""
    for candidate in (
        KB_DIR / ".venv" / "bin" / "python",
        PROJECT_ROOT / ".venv" / "bin" / "python",
        PROJECT_ROOT / "venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _command() -> list[str]:
    cmd = [
        _python(),
        "-m",
        "bars_kb.cli",
        "watch",
        "--interval",
        str(interval()),
        "--source",
        str(source_dir()),
    ]
    support = support_dir()
    if support.is_dir():
        cmd += ["--support-source", str(support)]
    else:
        cmd.append("--no-support")
    if _flag("KB_AUTOSYNC_ALLOW_MOCK"):
        cmd.append("--allow-mock")
    if _flag("KB_AUTOSYNC_EVAL"):
        cmd.append("--eval")
    return cmd


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_lock() -> bool:
    """Do not start a second watcher when two app processes run locally."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if _pid_alive(pid):
            logger.info("KB autosync уже работает в процессе %s", pid)
            return False
        logger.warning("удаляю устаревший lock KB autosync")
        try:
            LOCK_FILE.unlink()
        except OSError:
            return False
        return _acquire_lock()
    os.write(fd, str(os.getpid()).encode("ascii"))
    os.close(fd)
    return True


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


async def _run() -> None:
    env = os.environ.copy()
    src = str(KB_DIR / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    command = _command()
    logger.info("KB autosync: %s", " ".join(command))
    try:
        proc = await asyncio.create_subprocess_exec(*command, cwd=str(KB_DIR), env=env)
    except Exception as exc:
        logger.warning("KB autosync не запустился: %s", exc)
        _release_lock()
        return

    try:
        code = await proc.wait()
        if code not in (0, -15):
            logger.warning("KB autosync остановился с кодом %s", code)
    except asyncio.CancelledError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        raise
    finally:
        _release_lock()


async def start() -> asyncio.Task | None:
    """Start the watcher; returns a task to stop on shutdown."""
    if not enabled():
        logger.info("KB autosync выключен (KB_AUTOSYNC=0)")
        return None
    if not (KB_DIR / "src" / "bars_kb" / "cli.py").is_file():
        logger.warning("KB autosync: не найден %s", KB_DIR)
        return None
    if not _acquire_lock():
        return None
    return asyncio.create_task(_run(), name="kb-autosync")


async def stop(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("KB autosync: ошибка остановки: %s", exc)
