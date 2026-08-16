#!/usr/bin/env python3
"""Fit the rank->points calibration curve from realized NFL seasons (B9).

    python scripts/fit_rank_curves.py --out data/history/rank_curves.json
    python scripts/fit_rank_curves.py --exclude-season 2024 --out data/history/rank_curves_no2024.json

Writes `{curve: {position: [pts at rank 1, ...]}, ...provenance}` for
`draft.rank_calibration` to load. The fitting seasons, week window, and
scoring source are recorded INSIDE the file so a curve can never be used
without its provenance being visible -- these are fit numbers, not
hand-chosen ones, and which seasons produced them is the thing that decides
whether a given backtest result is honest.

`--exclude-season` is how a leakage-free backtest cell is produced: fit on
every other season, grade on that one. `ffbot.history.calibration.
rank_points_curve` refuses an overlapping fit set outright, so a curve that
saw its own grading season cannot be produced by accident.

See `ffbot/history/calibration.py`'s module docstring for the measured
defect this exists to correct, and `DraftConfig.rank_calibration` for why
it is a rank remap rather than a per-position multiplier.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.config import Config, LeagueScoring  # noqa: E402
from ffbot.history.calibration import (  # noqa: E402
    CALIBRATED_POSITIONS,
    predictiveness,
    rank_points_curve,
    weekly_rank_points_curve,
)
from ffbot.history.fetch import DEFAULT_CACHE_DIR, parse_seasons  # noqa: E402
from ffbot.history.projections import ECR_CLEAN_SEASONS  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument(
        "--seasons", default=None,
        help=f'seasons to fit on, e.g. "2021-2023" (default: all of {ECR_CLEAN_SEASONS})',
    )
    p.add_argument(
        "--exclude-season", type=int, default=None,
        help="season being graded -- removed from the fit set; passing it inside --seasons raises",
    )
    p.add_argument("--weeks", default="1-15", help="scoring window (default: %(default)s)")
    p.add_argument("--max-rank", type=int, default=60, help="(default: %(default)s)")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"(default: {DEFAULT_CACHE_DIR})")
    p.add_argument("--out", default="data/history/rank_curves.json", help="(default: %(default)s)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = Config.load(args.config)
    weeks = parse_seasons(args.weeks)
    seasons = parse_seasons(args.seasons) if args.seasons else None

    try:
        curve = rank_points_curve(
            fit_seasons=seasons,
            exclude_season=args.exclude_season,
            cfg=cfg,
            weeks=weeks,
            cache_dir=args.cache_dir,
            max_rank=args.max_rank,
        )
        # The weekly sibling, for the in-season start/sit and waiver
        # rankings -- a separate fit, not the season curve divided by 17;
        # see `weekly_rank_points_curve` for why the shapes differ.
        weekly_curve = weekly_rank_points_curve(
            fit_seasons=seasons,
            exclude_season=args.exclude_season,
            cfg=cfg,
            weeks=weeks,
            cache_dir=args.cache_dir,
            max_rank=args.max_rank,
        )
        # How much of each position's projected spread is REAL -- the
        # measurement that lets K/DEF sink on their own arithmetic instead
        # of being gated to the last two rounds. See `predictiveness`.
        signal = predictiveness(
            fit_seasons=seasons,
            exclude_season=args.exclude_season,
            cfg=cfg,
            weeks=weeks,
            cache_dir=args.cache_dir,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not curve:
        print("error: no realized data found -- is data/history populated?", file=sys.stderr)
        return 1

    fitted = seasons if seasons else [s for s in ECR_CLEAN_SEASONS if s != args.exclude_season]
    payload = {
        "curve": curve,
        "weekly_curve": weekly_curve,
        "predictiveness": signal,
        "fit_seasons": list(fitted),
        "excluded_season": args.exclude_season,
        "weeks": weeks,
        "scoring": "league.yml" if cfg.league is not None else "fantasypros_default",
        "positions": list(CALIBRATED_POSITIONS),
        "note": (
            "Realized points by within-position rank, averaged over fit_seasons and forced "
            "monotone. Consumed by draft.rank_calibration; see ffbot/history/calibration.py."
        ),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"fit on seasons {fitted} (weeks {weeks[0]}-{weeks[-1]}), scoring={payload['scoring']}")
    for label, c in (("season", curve), ("weekly", weekly_curve)):
        print(f"  [{label}]")
        for pos in CALIBRATED_POSITIONS:
            vals = c.get(pos)
            if not vals:
                continue
            head = ", ".join(f"{v:.1f}" for v in vals[:5])
            print(f"    {pos}: depth={len(vals):3d}  ranks 1-5: {head}")
    if signal:
        print("  [predictiveness] " + ", ".join(f"{k}={v:.2f}" for k, v in sorted(signal.items())))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
