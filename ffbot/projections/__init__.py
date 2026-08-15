"""Live weekly + rest-of-season projections — the seam that replaces
`week_report.py`'s dead `--proj` hatch and `ros_blend`'s frozen-preseason
"ROS" side (see docs/REFERENCE.md's config.yml reference and CLAUDE.md's
design-invariants section for the full story of why this package exists).

Deliberately a sibling of `ffbot/history/`, not a member of it: that package
is for point-in-time REPLAY against seasons that already happened, with a
structural leakage guarantee (`history.index.as_of` never fetches a
results-bearing source). This package is for the LIVE, current season, where
"has this already happened" isn't a question the code needs to answer at
all — a live projection is just whatever the source currently says.

A `ProjectionProvider` is `(season, week) -> list[dict]`, returning rows in
the exact shape `ffbot.board.read_fantasypros` produces (`name`/`team`/
`position`/`points`/`bye`/optionally `stats`) — the same seam shape as
`ffbot.history.signals.SignalProvider`, and what lets a provider's rows drop
straight into `ffbot.roster_source.load_weekly_projection_rows`'s existing
path with nothing downstream needing to know the source changed. Network/
cache details (which source, what TTL, which opener) are bound into the
callable at construction time — see `sleeper_provider` — so call sites never
see them.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Callable, Optional

from ..board import apply_league_scoring
from ..config import LeagueScoring, ProjectionSourceConfig
from ..names import normalize_name

ProjectionProvider = Callable[[int, int], list[dict]]

# Sources that need no ProjectionProvider at all -- both are pre-existing
# code paths `ffbot.report.load_everything` already handles itself: "board"
# is the frozen-preseason-board fallback, "csv" is the `--proj` hatch.
_NO_PROVIDER_SOURCES = ("board", "csv")


def current_nfl_season(today: Optional[_dt.date] = None) -> int:
    """The season year a live projection source should be queried under.

    An NFL season named e.g. "2026" runs from September 2026 through the
    Super Bowl in February 2027 — so a January/February date still belongs
    to the PREVIOUS year's season, not the calendar year. Only a fallback:
    every live caller (`week_report.py --season`, `report.load_everything`'s
    `season` parameter) can override this explicitly, which matters most
    exactly when this heuristic is least trustworthy — the handful of weeks
    around a season boundary.
    """
    d = today if today is not None else _dt.date.today()
    return d.year - 1 if d.month <= 2 else d.year


def resolve_provider(
    source_cfg: ProjectionSourceConfig,
    cache_dir: Optional[str | Path] = None,
    opener=None,
    now: Optional[float] = None,
) -> Optional[ProjectionProvider]:
    """`source_cfg.source` -> a bound `ProjectionProvider`, or `None` for a
    source that doesn't need one (`"board"`/`"csv"` — see
    `_NO_PROVIDER_SOURCES`). Raises `ValueError` for anything else
    unrecognized, so a config typo fails loudly at load time rather than
    silently behaving like `"board"`.

    Network/cache details are bound into the returned callable here, at
    config-resolution time, so every call site downstream just sees
    `(season, week) -> rows`.
    """
    if source_cfg.source in _NO_PROVIDER_SOURCES:
        return None
    if source_cfg.source == "sleeper":
        from . import sleeper as _sleeper
        from .cache import DEFAULT_CACHE_DIR, _default_opener

        resolved_cache_dir = cache_dir or source_cfg.cache_dir or DEFAULT_CACHE_DIR
        resolved_opener = opener or _default_opener
        ttl = source_cfg.cache_ttl_minutes

        def _provider(season: int, week: int) -> list[dict]:
            return _sleeper.fetch_weekly_rows(
                season, week, cache_dir=resolved_cache_dir, ttl_minutes=ttl, opener=resolved_opener, now=now,
            )

        return _provider
    raise ValueError(
        f"unknown projection_source.source {source_cfg.source!r} (expected 'board', 'sleeper', or 'csv')"
    )


def weekly_rows(season: int, week: int, provider: ProjectionProvider) -> list[dict]:
    """One week's rows from `provider` — the stable name callers use,
    independent of which provider is configured."""
    return provider(season, week)


def ros_rows(
    season: int,
    from_week: int,
    through_week: int,
    provider: ProjectionProvider,
    league: LeagueScoring | None,
) -> list[dict]:
    """A REAL rest-of-season total: `provider`'s rows for every week in
    `[from_week, through_week]`, each scored under `league`'s rules via
    `apply_league_scoring`, then summed per player.

    Strictly better than the frozen-preseason-board number `ros_blend` has
    stood in for until now (see CLAUDE.md's design-invariants section): it
    naturally de-rates a player who stops appearing (injury, benching,
    trade to a bye-heavy role) and never counts a week that already
    happened, since the caller supplies `from_week`.

    Sums already-SCORED weekly points rather than merging raw `StatLine`s
    across weeks — deliberately: `StatLine.points_allowed_game` is a
    single-game figure, and `score_statline`'s season-total path
    (`points_allowed_season`) divides by `league.games_per_season` (a fixed
    17), which would silently misprice a PARTIAL-season sum. Scoring each
    week independently and adding the results sidesteps that unit mismatch
    entirely. `team`/`bye` take the LAST week seen for a player, so a
    mid-stream trade is reflected as of the most recent week, not the
    first.
    """
    totals: dict[tuple[str, str], dict] = {}
    for wk in range(from_week, through_week + 1):
        rows = weekly_rows(season, wk, provider)
        apply_league_scoring(rows, league)
        for row in rows:
            key = (normalize_name(row["name"]), row["position"])
            entry = totals.get(key)
            if entry is None:
                entry = {"name": row["name"], "team": row["team"], "position": row["position"], "points": 0.0, "bye": row.get("bye")}
                totals[key] = entry
            entry["team"] = row["team"]
            entry["bye"] = row.get("bye")
            entry["points"] += row["points"]
    return list(totals.values())
