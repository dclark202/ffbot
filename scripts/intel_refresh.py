#!/usr/bin/env python3
"""The deterministic half of an intel refresh. Offline; no network.

The research itself is LLM work — run it in Claude Code with `/intel-refresh`
(see .claude/commands/intel-refresh.md), which drives this script for
everything around the research:

    python scripts/intel_refresh.py --archive     # snapshot before researching
    python scripts/intel_refresh.py --diff        # what changed vs the snapshot
    python scripts/intel_refresh.py               # validate + coverage + exports

The same flow serves in-season weekly intel: archive, research against the
week's news, diff, and the notes ride along wherever the board is used.
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import load_board_from_config  # noqa: E402
from ffbot.config import Config  # noqa: E402
from ffbot.intel import IntelError, coverage, diff_intel, load_intel  # noqa: E402

ARCHIVE_DIR = "intel_history"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="path to config.yml")
    p.add_argument("--archive", action="store_true", help="snapshot the current intel file, dated, before a research pass")
    p.add_argument("--diff", action="store_true", help="report changes vs the most recent archive")
    p.add_argument("--no-export", action="store_true", help="skip regenerating draft/ exports during validation")
    return p.parse_args(argv)


def _archive_dir(intel_path: Path) -> Path:
    return intel_path.parent / ARCHIVE_DIR


def archive(intel_path: Path) -> Path | None:
    """Copy the intel file to intel_history/intel-YYYYMMDD-HHMM.yml."""
    if not intel_path.exists():
        print(f"nothing to archive — {intel_path} does not exist")
        return None
    out_dir = _archive_dir(intel_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    dest = out_dir / f"intel-{stamp}.yml"
    shutil.copy2(intel_path, dest)
    print(f"archived {intel_path} -> {dest}")
    return dest


def latest_archive(intel_path: Path) -> Path | None:
    out_dir = _archive_dir(intel_path)
    if not out_dir.exists():
        return None
    snaps = sorted(out_dir.glob("intel-*.yml"))
    return snaps[-1] if snaps else None


def show_diff(intel_path: Path) -> int:
    prev = latest_archive(intel_path)
    if prev is None:
        print("no archive to diff against — run --archive before researching")
        return 1
    d = diff_intel(load_intel(prev), load_intel(intel_path))
    print(f"vs {prev.name}:")
    for label in ("added", "removed", "changed"):
        lines = d[label]
        print(f"\n{label.upper()} ({len(lines)})")
        for line in lines:
            print(f"  {line}")
    if not any(d.values()):
        print("\n(no changes)")
    return 0


def validate(cfg_path: str, regenerate_exports: bool) -> int:
    """Load everything the draft depends on; loud about anything wrong."""
    cfg = Config.load(cfg_path)

    try:
        load_intel(cfg.draft.intel_file)
    except IntelError as exc:
        print(f"INTEL INVALID: {exc}", file=sys.stderr)
        return 1

    caught: list[warnings.WarningMessage] = []
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            board = load_board_from_config(cfg)
        except ValueError as exc:
            print(f"BOARD FAILED TO LOAD: {exc}", file=sys.stderr)
            return 1
        caught = list(w)

    for warning in caught:
        print(f"WARNING: {warning.message}", file=sys.stderr)

    covered, total, _ = coverage(board, 200)
    print(f"board: {len(board.players)} players; intel coverage {covered}/{total} of top {total} by ADP")

    if regenerate_exports:
        # Imported lazily: draft_export pulls in csv writers this path only
        # needs when actually regenerating.
        from scripts.draft_export import write_exports

        paths = write_exports(board, cfg, Path("draft"))
        print("exports regenerated:")
        for p in paths.values():
            print(f"  {p}")
        print("\nREMINDER: re-paste draft/board.txt into Yahoo's custom pre-draft "
              "rankings — the old paste does not update itself.")

    return 1 if caught else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = Config.load(args.config)
    intel_path = Path(cfg.draft.intel_file)

    if args.archive:
        archive(intel_path)
        return 0
    if args.diff:
        return show_diff(intel_path)
    return validate(args.config, regenerate_exports=not args.no_export)


if __name__ == "__main__":
    raise SystemExit(main())
