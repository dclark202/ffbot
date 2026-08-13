"""Lists and reads `scripts/autorun.py`'s generated report files
(`reports/YYYY-wNN-<trigger>.md`) for the GUI's `/reports` page. Pure
filesystem I/O, deliberately outside `scripts/gui.py`'s handler — the same
"testable without a live server" reasoning `ffbot/roster_editor.py` and
`ffbot/weekly_editor.py` already follow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ReportSummary:
    filename: str
    modified: str  # ISO timestamp, local time, minute precision
    size: int


class ReportNotFoundError(ValueError):
    """The requested report file doesn't exist, or isn't a valid one."""


def list_reports(reports_dir: str | Path) -> list[ReportSummary]:
    """Every `.md` file directly under `reports_dir`, newest-modified
    first. A missing directory (no scheduled run has fired yet) degrades
    to an empty list — the same "nothing here yet" no-op every other
    optional GUI input in this repo already uses, never an error.
    """
    p = Path(reports_dir)
    if not p.exists():
        return []
    out = []
    for f in p.glob("*.md"):
        if not f.is_file():
            continue
        stat = f.stat()
        out.append(
            ReportSummary(
                filename=f.name,
                modified=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="minutes"),
                size=stat.st_size,
            )
        )
    out.sort(key=lambda r: r.modified, reverse=True)
    return out


def read_report(reports_dir: str | Path, filename: str) -> str:
    """The raw content of one report file.

    Guards against path traversal — `filename` is reachable from an HTTP
    query parameter, so a value containing a path separator or resolving
    outside `reports_dir` (`../../config.yml`, an absolute path) must
    never be allowed to read anything outside the reports directory.
    """
    p = Path(reports_dir)
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ReportNotFoundError(f"invalid filename: {filename!r}")
    resolved_dir = p.resolve()
    target = (p / filename).resolve()
    try:
        target.relative_to(resolved_dir)
    except ValueError:
        raise ReportNotFoundError(f"invalid filename: {filename!r}") from None
    if not target.exists() or not target.is_file():
        raise ReportNotFoundError(f"no such report: {filename!r}")
    return target.read_text(encoding="utf-8")
