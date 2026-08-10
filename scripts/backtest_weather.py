#!/usr/bin/env python3
"""Empirical shape of the weather effect on realized fantasy points, by
position and wind bucket — the diagnostic behind `ffbot/week.py`'s
`weather_severity`/`weather_multiplier` re-specification (see
docs/BACKTEST.md, milestone B4).

    python scripts/backtest_weather.py --seasons 2021-2024
    python scripts/backtest_weather.py --seasons 2021-2024 --source naive
    python scripts/backtest_weather.py --seasons 2021-2024 --game-weather openmeteo
    python scripts/backtest_weather.py --seasons 2021-2024 --game-weather openmeteo --climate-delta

For every outdoor game (dome/closed-roof games excluded — weather-neutral by
`week.is_dome_game`'s own definition) in the requested seasons, bins every
QB/RB/WR/TE player-week by that game's `wind_mph` and reports the mean ratio
of REALIZED points to that week's projected points within each bucket —
isolating the wind effect from player quality. A raw-points average would
just measure who happened to be good that week, not what wind did to them;
a ratio near 1.0 in a bucket means wind isn't actually discounting output at
that speed, whatever `SeasonConfig.wind_threshold_mph`/`weather_weight`
currently assume.

A player-week whose game has no researched `wind_mph` at all (missing data,
common in `games.csv`'s older/less-covered rows) is excluded from every
bucket rather than folded into 0-10mph "calm" — `ffbot/week.py`'s
`weather_severity` collapses unknown and confirmed-calm to the same 0.0
severity for live decisions (fail-open is the right call there), but doing
that here would quietly contaminate the calm-weather baseline this very
script's ratios get compared against with an unknown fraction of no-data
rows. Reported separately as `n_wind_unknown`.

`--game-weather openmeteo` (see `ffbot/history/openmeteo.py`) enriches every
outdoor game with real hourly wind/gust/temp/precip BEFORE bucketing — this
both closes the wind-coverage gap above (fewer `n_wind_unknown`) and unlocks
two tables `games.csv` alone cannot support at all: TEMPERATURE buckets
(games.csv's own `temp` column is never read into `GameInfo` today) and,
with `--climate-delta`, a CLIMATE MISMATCH table — for every AWAY team's
game, `|kickoff temp - that team's own home-game average temp, from this
same sample|`, testing the "a warm-climate team is out of its element in a
cold road environment" hypothesis independently of raw cold (a cold-climate
team playing in the same 20F has a small delta; a dome team does not).

Caveat printed with every climate-delta table: this is NOT residualized
against the Vegas line, so if the market already prices "dome team travels
to Buffalo in December," part of any effect measured here may already be
priced into `vegas_weight` rather than being something the climate signal
would newly capture. Treat a real effect here as an upper bound, the same
caveat `docs/BACKTEST.md`'s leakage register already applies to observed
(non-forecast) weather generally.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.config import Config, LeagueScoring  # noqa: E402
from ffbot.history.actuals import week_actuals  # noqa: E402
from ffbot.history.fetch import DEFAULT_CACHE_DIR, parse_seasons  # noqa: E402
from ffbot.history.index import as_of  # noqa: E402
from ffbot.history.openmeteo import open_meteo_game_weather  # noqa: E402
from ffbot.history.projections import ecr_projections, naive_projections, player_pool  # noqa: E402
from ffbot.week import is_dome_game  # noqa: E402

_POSITIONS = ("QB", "RB", "WR", "TE")
_WIND_BUCKETS = [(0, 10), (10, 15), (15, 20), (20, 25), (25, 999)]
_GUST_BUCKETS = [(0, 15), (15, 25), (25, 35), (35, 999)]
_TEMP_BUCKETS = [(-999, 20), (20, 32), (32, 45), (45, 60), (60, 999)]  # Fahrenheit
_CLIMATE_DELTA_BUCKETS = [(0, 15), (15, 25), (25, 35), (35, 999)]  # |degrees F from home baseline|
_MIN_PROJECTION = 3.0  # avoid a near-zero denominator blowing up the ratio


def _bucket(value: float, buckets: list[tuple[float, float]]) -> tuple[float, float]:
    for lo, hi in buckets:
        if lo <= value < hi:
            return (lo, hi)
    return buckets[-1]


def _print_table(title: str, unit: str, buckets: list[tuple[float, float]], ratios: dict) -> None:
    print(f"\n{title}")
    print(f"{'position':<10} {unit:<14} {'n':>6} {'mean actual/proj':>18} {'stdev':>8}")
    for position in _POSITIONS:
        for bucket in buckets:
            vals = ratios.get((position, bucket))
            if not vals:
                continue
            lo, hi = bucket
            label = f"{lo}-{hi}" if hi < 999 else f"{lo}+"
            mean = statistics.fmean(vals)
            stdev = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            print(f"{position:<10} {label:<14} {len(vals):>6} {mean:>18.3f} {stdev:>8.3f}")


@dataclass
class _PlayerWeekSample:
    position: str
    team: str
    home: bool
    ratio: float
    wind_mph: float | None
    wind_gust_mph: float | None
    temp_f: float | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seasons", required=True, help='e.g. "2021-2024"')
    p.add_argument("--weeks", default="1-15", help="(default: %(default)s)")
    p.add_argument("--source", choices=["naive", "ecr"], default="ecr", help="(default: %(default)s)")
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"(default: {DEFAULT_CACHE_DIR})")
    p.add_argument(
        "--game-weather", choices=["openmeteo"], default=None,
        help="enrich with real hourly weather (see ffbot/history/openmeteo.py); "
             "required for the temperature table and --climate-delta",
    )
    p.add_argument(
        "--climate-delta", action="store_true",
        help="also report the climate-mismatch table (requires --game-weather openmeteo)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    seasons = parse_seasons(args.seasons)
    weeks = parse_seasons(args.weeks)
    cfg = Config.load(args.config)
    scoring = cfg.league or LeagueScoring.fantasypros_default()

    if args.climate_delta and not args.game_weather:
        print("error: --climate-delta requires --game-weather openmeteo", file=sys.stderr)
        return 1

    samples: list[_PlayerWeekSample] = []
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
            if args.game_weather == "openmeteo":
                weather = open_meteo_game_weather(season, week, cfg, cache_dir=args.cache_dir)
                snapshot = snapshot.with_game_weather(weather)

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

                samples.append(_PlayerWeekSample(
                    position=position, team=team, home=game.home, ratio=actual_pts / proj_pts,
                    wind_mph=game.wind_mph, wind_gust_mph=game.wind_gust_mph, temp_f=game.temp_f,
                ))

    print(f"Weather diagnostic — {len(seasons)} season(s), source={args.source}, game_weather={args.game_weather or 'none'}")
    print(f"(skipped {n_dome_skipped} dome/closed-roof player-weeks, {n_no_projection} with projection < {_MIN_PROJECTION})")

    wind_ratios: dict[tuple[str, tuple[float, float]], list[float]] = defaultdict(list)
    n_wind_unknown = 0
    for s in samples:
        if s.wind_mph is None:
            n_wind_unknown += 1
            continue
        wind_ratios[(s.position, _bucket(s.wind_mph, _WIND_BUCKETS))].append(s.ratio)
    print(f"({n_wind_unknown} player-weeks with no wind_mph, excluded from the wind table)")
    _print_table("WIND", "wind (mph)", _WIND_BUCKETS, wind_ratios)

    if args.game_weather == "openmeteo":
        gust_ratios: dict[tuple[str, tuple[float, float]], list[float]] = defaultdict(list)
        temp_ratios: dict[tuple[str, tuple[float, float]], list[float]] = defaultdict(list)
        for s in samples:
            if s.wind_gust_mph is not None:
                gust_ratios[(s.position, _bucket(s.wind_gust_mph, _GUST_BUCKETS))].append(s.ratio)
            if s.temp_f is not None:
                temp_ratios[(s.position, _bucket(s.temp_f, _TEMP_BUCKETS))].append(s.ratio)
        _print_table("WIND GUST (openmeteo only)", "gust (mph)", _GUST_BUCKETS, gust_ratios)
        _print_table("TEMPERATURE (openmeteo only)", "temp (F)", _TEMP_BUCKETS, temp_ratios)

    if args.climate_delta:
        # Each team's own HOME-game average temp, from this same sample --
        # not a league-wide constant, so a cold-climate team's baseline is
        # genuinely cold and a dome/warm team's baseline is genuinely warm.
        # Filtered to `home=True` specifically, not every appearance -- an
        # away game reflects the OPPONENT's climate, not the team's own, and
        # mixing the two in would flatten every team's baseline toward the
        # league average.
        home_temps: dict[str, list[float]] = defaultdict(list)
        for s in samples:
            if s.home and s.temp_f is not None:
                home_temps[s.team].append(s.temp_f)
        home_baseline = {
            team: statistics.fmean(temps) for team, temps in home_temps.items() if len(temps) >= 4
        }

        # Climate mismatch is only a real question on the ROAD -- a team's
        # own home stadium is by definition its own climate baseline
        # (delta ~0 always), so including home games here would just dilute
        # every bucket with a pile of exact-zero deltas that say nothing
        # about acclimation.
        climate_ratios: dict[tuple[str, tuple[float, float]], list[float]] = defaultdict(list)
        for s in samples:
            if s.home or s.temp_f is None:
                continue
            baseline = home_baseline.get(s.team)
            if baseline is None:
                continue
            delta = abs(s.temp_f - baseline)
            climate_ratios[(s.position, _bucket(delta, _CLIMATE_DELTA_BUCKETS))].append(s.ratio)

        print(
            "\nCLIMATE MISMATCH (openmeteo only, ROAD games only) -- |kickoff temp - team's "
            "own HOME-temp average this sample|. NOT residualized against the Vegas line -- "
            "see module docstring's caveat; treat a real effect here as an upper bound."
        )
        _print_table("CLIMATE MISMATCH", "delta (F)", _CLIMATE_DELTA_BUCKETS, climate_ratios)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
