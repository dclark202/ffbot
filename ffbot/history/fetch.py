"""Cache-first downloader for nflverse / DynastyProcess historical data.

Everything here is plain HTTPS + stdlib `csv`/`gzip` — no `nfl_data_py`
(deprecated) or `nflreadpy` (Polars) dependency, per docs/BACKTEST.md's
research. nflverse publishes each dataset as a CSV release asset on GitHub
(`nflverse/nflverse-data`); DynastyProcess publishes the free FantasyPros ECR
archive the same way, gzip-compressed.

Past seasons are immutable box scores — once a season's file is cached it is
never re-fetched, only `refresh=True` (the current in-progress season) forces
a re-download. `opener` is injectable everywhere a network call happens, so
every test in `tests/test_history_fetch.py` runs with zero network access.

Sources marked `compressed` (today, just the ECR archive) are cached
gzip-compressed on disk (`.csv.gz`, not `.csv`) — the source is already
gzipped, so this is several times smaller for free (the ECR archive: ~450MB
uncompressed vs. ~105MB as downloaded) rather than paying to decompress it
and then re-compress-on-read later. `load_csv_rows` streams straight through
`gzip.open` for a `.gz` path, so callers never see the difference.
"""

from __future__ import annotations

import csv
import gzip
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

DEFAULT_CACHE_DIR = Path("data/history")

_NFLVERSE_RELEASES = "https://github.com/nflverse/nflverse-data/releases/download"
_DYNASTYPROCESS_FILES = "https://github.com/dynastyprocess/data/raw/master/files"


class FetchError(RuntimeError):
    """A historical data source could not be downloaded."""


@dataclass(frozen=True)
class Source:
    """One fetchable historical dataset.

    `url_template` takes a `{season}` placeholder for per-season sources;
    fixed sources (schedules, players, the ECR archive) ignore `season`
    entirely. `first_season` documents the earliest year nflverse actually
    publishes — `source_url` refuses anything earlier rather than returning
    a URL that 404s.
    """

    url_template: str
    per_season: bool
    first_season: int | None = None
    compressed: bool = False  # gzip-compressed at the URL; cache stores raw CSV


# Verified live (HTTP 200/302) against nflverse-data and dynastyprocess/data
# during scoping — see docs/BACKTEST.md's data-sources table.
SOURCES: dict[str, Source] = {
    "stats_player_week": Source(
        f"{_NFLVERSE_RELEASES}/stats_player/stats_player_week_{{season}}.csv",
        per_season=True, first_season=1999,
    ),
    "stats_team_week": Source(
        f"{_NFLVERSE_RELEASES}/stats_team/stats_team_week_{{season}}.csv",
        per_season=True, first_season=1999,
    ),
    "games": Source(
        f"{_NFLVERSE_RELEASES}/schedules/games.csv",
        per_season=False,
    ),
    "injuries": Source(
        f"{_NFLVERSE_RELEASES}/injuries/injuries_{{season}}.csv",
        per_season=True, first_season=2009,
    ),
    "roster_weekly": Source(
        f"{_NFLVERSE_RELEASES}/weekly_rosters/roster_weekly_{{season}}.csv",
        per_season=True, first_season=2002,
    ),
    "players": Source(
        f"{_NFLVERSE_RELEASES}/players/players.csv",
        per_season=False,
    ),
    # Ranks only (ecr/sd/best/worst) — no points. `scrape_date` covers
    # Dec 2019 onward, not the full nflverse range; see docs/BACKTEST.md.
    "ff_ecr": Source(
        f"{_DYNASTYPROCESS_FILES}/db_fpecr.csv.gz",
        per_season=False, compressed=True,
    ),
    "ff_playerids": Source(
        f"{_DYNASTYPROCESS_FILES}/db_playerids.csv",
        per_season=False,
    ),
}


def cache_path(
    source_key: str, season: int | None = None, cache_dir: Path | str = DEFAULT_CACHE_DIR
) -> Path:
    """Local cache file for `source_key` (+ `season`, if per-season).

    A `compressed` source's cache file keeps the `.csv.gz` extension — see
    the module docstring for why this is stored compressed rather than
    inflated to disk.
    """
    src = _lookup(source_key)
    cache_dir = Path(cache_dir)
    ext = ".csv.gz" if src.compressed else ".csv"
    if src.per_season:
        if season is None:
            raise ValueError(f"{source_key!r} is per-season; season is required")
        return cache_dir / f"{source_key}_{season}{ext}"
    return cache_dir / f"{source_key}{ext}"


def source_url(source_key: str, season: int | None = None) -> str:
    """The download URL for `source_key` (+ `season`)."""
    src = _lookup(source_key)
    if not src.per_season:
        return src.url_template
    if season is None:
        raise ValueError(f"{source_key!r} is per-season; season is required")
    if src.first_season is not None and season < src.first_season:
        raise ValueError(
            f"{source_key!r} has no nflverse data before {src.first_season} (got {season})"
        )
    return src.url_template.format(season=season)


def _lookup(source_key: str) -> Source:
    try:
        return SOURCES[source_key]
    except KeyError:
        raise ValueError(
            f"unknown historical source {source_key!r}; known sources: {sorted(SOURCES)}"
        ) from None


UrlOpener = Callable[[str], bytes]


def _default_opener(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ffbot-history/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 - fixed https hosts only
        return resp.read()


def fetch(
    source_key: str,
    season: int | None = None,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    opener: UrlOpener = _default_opener,
) -> Path:
    """Download `source_key` (+ `season`) to the cache, returning the local
    path. A cache hit is trusted and returned unread unless `refresh=True` —
    past seasons never change, so re-fetching them is pure waste.
    """
    dest = cache_path(source_key, season, cache_dir)
    if dest.exists() and not refresh:
        return dest

    url = source_url(source_key, season)
    try:
        raw = opener(url)
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"{source_key} ({url}): {exc}") from exc

    # Compressed sources are cached AS-DOWNLOADED (still gzipped) — see the
    # module docstring. Decompressing here just to re-compress-on-read later
    # would be pure waste; `load_csv_rows` streams straight through `.gz`.
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return dest


def load_csv_rows(path: Path | str) -> list[dict]:
    """Read a cached CSV file into a list of row dicts. Transparently handles
    a gzip-compressed cache file (`.csv.gz`, see `cache_path`)."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def fetch_rows(
    source_key: str,
    season: int | None = None,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    opener: UrlOpener = _default_opener,
) -> list[dict]:
    """`fetch` + `load_csv_rows` in one call — the common case for callers
    that just want the rows and don't care about the cache path."""
    path = fetch(source_key, season=season, cache_dir=cache_dir, refresh=refresh, opener=opener)
    return load_csv_rows(path)


def parse_seasons(spec: str) -> list[int]:
    """Parse a `--seasons` CLI value like `"2015-2025"` or `"2019,2021-2023"`
    into a sorted, deduplicated list of years. Shared by
    `scripts/history_fetch.py` and `scripts/history_check.py` so the two
    scripts' `--seasons` flags behave identically.
    """
    seasons: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            seasons.update(range(int(lo), int(hi) + 1))
        else:
            seasons.add(int(part))
    return sorted(seasons)


def coverage_table(
    seasons: Iterable[int], cache_dir: Path | str = DEFAULT_CACHE_DIR
) -> dict[str, dict[int, bool]]:
    """`{source_key: {season: is_cached}}` for every source, over `seasons`.

    Non-per-season sources (games, players, the ECR/playerid archives) report
    a single entry under season `0` rather than once per requested season —
    used by `scripts/history_fetch.py`'s coverage printout.
    """
    cache_dir = Path(cache_dir)
    seasons = list(seasons)
    out: dict[str, dict[int, bool]] = {}
    for key, src in SOURCES.items():
        if src.per_season:
            out[key] = {s: cache_path(key, s, cache_dir).exists() for s in seasons}
        else:
            out[key] = {0: cache_path(key, None, cache_dir).exists()}
    return out
