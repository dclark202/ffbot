#!/usr/bin/env python3
"""Empirical shape of the weather effect on realized fantasy points, by
position and wind bucket — the diagnostic behind `ffbot/week.py`'s
`weather_severity`/`weather_multiplier` re-specification (see
docs/BACKTEST.md, milestone B4).

    python scripts/backtest_weather.py --seasons 2021-2024
    python scripts/backtest_weather.py --seasons 2021-2024 --source naive

For every outdoor game (dome/closed-roof games excluded — weather-neutral by
`week.is_dome_game`'s own definition) in the requested seasons, bins every
QB/RB/WR/TE player-week by that game's `wind_mph` and reports the mean ratio
of REALIZED points to that week's projected points within each bucket —
isolating the wind effect from player quality. A raw-points average would
just measure who happened to be good that week, not what wind did to them;
a ratio near 1.0 in a bucket means wind isn't actually discounting output at
that speed, whatever `SeasonConfig.wind_threshold_mph`/`weather_weight`
currently assume.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.config import Config, LeagueScoring  # noqa: E402
from ffbot.history.actuals import week_actuals  # noqa: E402
from ffbot.history.fetch import DEFAULT_CACHE_DIR, parse_seasons  # noqa: E402
from ffbot.history.index import as_of  # noqa: E402
from ffbot.history.projections import ecr_projections, naive_projections, player_pool  # noqa: E402
from ffbot.week import is_dome_game  # noqa: E402

_POSITIONS = ("QB", "RB", "WR", "TE")
_BUCKETS = [(0, 10), (10, 15), (15, 20), (20, 25), (25, 999)]
_MIN_PROJECTION = 3.0  # avoid a near-zero denominator blowing up the ratio


def _bucket(wind: float) -> tuple[int, int]:
    for lo, hi in _BUCKETS:
        if lo <= wind < hi:
            return (lo, hi)
    return _BUCKETS[-1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seasons", required=True, help='e.g. "2021-2024"')
    p.add_argument("--weeks", default="1-15", help="(default: %(default)s)")
    p.add_argument("--source", choices=["naive", "ecr"], default="ecr", help="(default: %(default)s)")
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"(default: {DEFAULT_CACHE_DIR})")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    seasons = parse_seasons(args.seasons)
    weeks = parse_seasons(args.weeks)
    cfg = Config.load(args.config)
    scoring = cfg.league or LeagueScoring.fantasypros_default()

    # {(position, bucket): [ratio, ratio, ...]}
    ratios: dict[tuple[str, tuple[int, int]], list[float]] = {}
    n_dome_skipped = 0
    n_no_projection = 0

    for season in seasons:
        for week in weeks:
            pool = player_pool(season, week, cache_dir=args.cache_dir)
            if args.source == "naive":
                proj = naive_projections(season, week, cfg, cache_dir=args.cache_dir)
            else:
                try:
                    proj = ecr_projections(season, week, cfg, cache_dir=args.cache_dir)
                except ValueError:
                    continue
            if not proj:
                continue
            actuals = week_actuals(season, week, scoring, cache_dir=args.cache_dir)
            snapshot = as_of(season, week, cache_dir=args.cache_dir)

            for row in pool:
                position = row["position"]
                if position not in _POSITIONS:
                    continue
                team = row["team"]
                game = snapshot.games.get(team)
                if game is None:
                    continue
                if is_dome_game(team, game, snapshot.stadiums):
                    n_dome_skipped += 1
                    continue

                proj_pts = proj.get(row["key"])
                if proj_pts is None or proj_pts < _MIN_PROJECTION:
                    n_no_projection += 1
                    continue
                actual_pts = actuals.get(row["key"], 0.0)

                wind = game.wind_mph or 0.0
                ratios.setdefault((position, _bucket(wind)), []).append(actual_pts / proj_pts)

    print(f"Weather diagnostic — {len(seasons)} season(s), source={args.source}")
    print(f"(skipped {n_dome_skipped} dome/closed-roof player-weeks, {n_no_projection} with projection < {_MIN_PROJECTION})\n")
    print(f"{'position':<10} {'wind (mph)':<12} {'n':>6} {'mean actual/proj':>18} {'stdev':>8}")
    for position in _POSITIONS:
        for bucket in _BUCKETS:
            vals = ratios.get((position, bucket))
            if not vals:
                continue
            lo, hi = bucket
            label = f"{lo}-{hi}" if hi < 999 else f"{lo}+"
            mean = statistics.fmean(vals)
            stdev = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            print(f"{position:<10} {label:<12} {len(vals):>6} {mean:>18.3f} {stdev:>8.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
