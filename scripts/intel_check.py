#!/usr/bin/env python3
"""Report how much of the draftable board has researched intel.

Gaps are much cheaper to find now than during the draft, when a player with no
note is a player you have no opinion about beyond consensus.

    python scripts/intel_check.py
    python scripts/intel_check.py --top 250 --missing

Coverage is measured over the top N by *ADP*, not by our board rank: a player
our board dislikes but the market takes in round 4 is still someone we need a
view on, because we have to decide whether to let him go.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import load_board_from_config  # noqa: E402
from ffbot.config import Config  # noqa: E402
from ffbot.intel import coverage  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="path to config.yml")
    p.add_argument("--top", type=int, default=200, help="how many players by ADP to check (default: 200)")
    p.add_argument("--missing", action="store_true", help="list every uncovered player, not just a sample")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = Config.load(args.config)

    try:
        board = load_board_from_config(cfg)
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    covered, total, missing = coverage(board, args.top)
    if total == 0:
        print("No players with an ADP on the board — nothing to measure.", file=sys.stderr)
        return 1

    pct = 100 * covered / total
    print(f"Intel file : {cfg.draft.intel_file}")
    print(f"Coverage   : {covered}/{total} of the top {args.top} by ADP ({pct:.0f}%)\n")

    by_pos = Counter(bp.position for bp in missing)
    if by_pos:
        print("Uncovered by position:")
        for pos, n in by_pos.most_common():
            print(f"  {pos:<5} {n}")

    if missing:
        shown = missing if args.missing else missing[:20]
        print(f"\nUncovered{'' if args.missing else ' (first 20)'}:")
        for bp in shown:
            print(f"  adp {bp.adp:>6.1f}  {bp.name:<26}{bp.position:<5}{bp.team}")
        if not args.missing and len(missing) > len(shown):
            print(f"  ... and {len(missing) - len(shown)} more (--missing to see all)")
    else:
        print("Every player in range has intel.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
