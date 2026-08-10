"""File-backed operations for the live draft log: archive, save, load.

These touch the filesystem, so they live outside `ffbot.draft_ui` (pure
state machine, no I/O) and are invoked directly by `scripts/draft.py`'s
command loop before handing off to `handle()` — `reset`/`save`/`load` are
never lines `handle()` itself understands.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

SAVES_DIR = Path("draft/saves")


class DraftStoreError(ValueError):
    """A save/load/reset operation could not be completed as asked."""


def _safe_name(name: str) -> str:
    name = name.strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise DraftStoreError(f"invalid save name: {name!r}")
    return name


def archive_log(log_path: Path) -> Path | None:
    """Rename `log_path` to a timestamped sibling so a fresh log can start.

    Returns the archive path, or None if there was no log to archive (a
    reset before any picks were recorded).
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archived = log_path.with_name(f"{log_path.stem}.{stamp}{log_path.suffix}")
    log_path.rename(archived)
    return archived


def save_snapshot(log_path: Path, name: str, saves_dir: Path = SAVES_DIR) -> Path:
    """Copy the current log to `saves_dir/<name>.jsonl`, overwriting any
    prior save of the same name."""
    log_path = Path(log_path)
    if not log_path.exists():
        raise DraftStoreError(f"nothing to save — {log_path} does not exist yet")
    saves_dir = Path(saves_dir)
    saves_dir.mkdir(parents=True, exist_ok=True)
    dest = saves_dir / f"{_safe_name(name)}.jsonl"
    shutil.copyfile(log_path, dest)
    return dest


def list_snapshots(saves_dir: Path = SAVES_DIR) -> list[str]:
    saves_dir = Path(saves_dir)
    if not saves_dir.exists():
        return []
    return sorted(p.stem for p in saves_dir.glob("*.jsonl"))


def load_snapshot(name: str, saves_dir: Path = SAVES_DIR) -> Path:
    """Path to a saved snapshot's log file. Raises `DraftStoreError` if the
    name is unknown — callers should surface that as a message, not crash."""
    saves_dir = Path(saves_dir)
    path = saves_dir / f"{_safe_name(name)}.jsonl"
    if not path.exists():
        available = list_snapshots(saves_dir)
        hint = f" — available: {', '.join(available)}" if available else " — no saves exist yet"
        raise DraftStoreError(f"no save named {name!r}{hint}")
    return path
