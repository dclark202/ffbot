#!/usr/bin/env python3
"""Download and cache historical NFL data for backtesting (see docs/dev/BACKTEST.md).

    python scripts/history_fetch.py --seasons 2015-2025
    python scripts/history_fetch.py --seasons 2022,2023,2024 --sources stats_player_week games
    python scripts/history_fetch.py --seasons 2024 --refresh

Fetches every source in `ffbot.history.fetch.SOURCES` (nflverse box
scores/schedules/injuries/rosters, plus DynastyProcess's FantasyPros ECR
archive) for the requested seasons, caching each to `data/history/`. Past
seasons never change, so a second run is a no-op unless `--refresh` is
passed — this is meant to be safe to re-run at any time.

Prints a coverage table at the end: which (source, season) pairs are cached
locally right now, after this run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.history.fetch import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    SOURCES,
    FetchError,
    coverage_table,
    fetch,
    parse_seasons,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seasons", required=True, help='e.g. "2015-2025" or "2019,2021-2023"')
    p.add_argument("--sources", nargs="*", default=None, help=f"limit to these sources (default: all). Known: {sorted(SOURCES)}")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help=f"(default: {DEFAULT_CACHE_DIR})")
    p.add_argument("--refresh", action="store_true", help="re-download even if already cached")
    return p.parse_args(argv)


def _run_source(key: str, seasons: list[int], cache_dir: str, refresh: bool) -> list[tuple[str, "int | None", str]]:
    src = SOURCES[key]
    results: list[tuple[str, "int | None", str]] = []
    if not src.per_season:
        try:
            fetch(key, cache_dir=cache_dir, refresh=refresh)
            results.append((key, None, "ok"))
        except FetchError as exc:
            results.append((key, None, f"FAILED: {exc}"))
        return results

    for season in seasons:
        if src.first_season is not None and season < src.first_season:
            results.append((key, season, f"skipped (no data before {src.first_season})"))
            continue
        try:
            fetch(key, season=season, cache_dir=cache_dir, refresh=refresh)
            results.append((key, season, "ok"))
        except FetchError as exc:
            results.append((key, season, f"FAILED: {exc}"))
    return results


def _warn_legacy_uncompressed_ecr(cache_dir: str) -> None:
    """A pre-fix run of this script could have written an uncompressed
    `ff_ecr.csv` (~450MB) instead of today's `.csv.gz` (~105MB) — see
    `ffbot.history.fetch`'s module docstring. Never deleted automatically;
    that file is real disk space belonging to whoever's machine this is."""
    legacy = Path(cache_dir) / "ff_ecr.csv"
    if legacy.exists():
        mb = legacy.stat().st_size / 1_000_000
        print(
            f"note: {legacy} is {mb:.0f}MB and no longer used — "
            "fetches now cache the compressed ff_ecr.csv.gz instead. "
            "Safe to delete the old file manually."
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    seasons = parse_seasons(args.seasons)
    if not seasons:
        print("error: --seasons produced no years", file=sys.stderr)
        return 1

    _warn_legacy_uncompressed_ecr(args.cache_dir)

    keys = args.sources if args.sources else sorted(SOURCES)
    unknown = [k for k in keys if k not in SOURCES]
    if unknown:
        print(f"error: unknown source(s) {unknown}; known: {sorted(SOURCES)}", file=sys.stderr)
        return 1

    print(f"Fetching {len(keys)} source(s) for {len(seasons)} season(s): {seasons[0]}-{seasons[-1]}")
    failed = 0
    for key in keys:
        results = _run_source(key, seasons, args.cache_dir, args.refresh)
        ok = sum(1 for _, _, status in results if status == "ok")
        skipped = sum(1 for _, _, status in results if status.startswith("skipped"))
        bad = [r for r in results if r[2].startswith("FAILED")]
        failed += len(bad)
        print(f"  {key:<20} ok={ok:<4} skipped={skipped:<4} failed={len(bad)}")
        for _, season, status in bad:
            print(f"    {season}: {status}")

    print("\nCoverage (cached locally, after this run):")
    table = coverage_table(seasons, cache_dir=args.cache_dir)
    for key, by_season in sorted(table.items()):
        cached = sum(1 for v in by_season.values() if v)
        total = len(by_season)
        print(f"  {key:<20} {cached}/{total} cached")

    if failed:
        print(f"\n{failed} fetch(es) failed — see above. Cached data is still usable for what succeeded.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
