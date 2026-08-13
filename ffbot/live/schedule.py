"""Live NFL schedule: this week's games, kickoff times, and roof state --
the CURRENT-season sibling of `ffbot.history.index`'s
`_build_games_and_stadiums`, built on the exact same nflverse `games.csv`
release nflverse publishes for every season (past AND future weeks alike --
it is one file, not one-per-season).

The one real difference from the historical path: `fetch_rows` is always
called with `refresh=True` here. A completed season's file never changes
(`ffbot.history.fetch`'s "past seasons are immutable" contract), but an
in-progress week's row genuinely can -- a flex-scheduled kickoff time move,
a stadium change, a postponement -- so trusting a stale cache hit the way
`as_of()` safely does for history would be wrong here.

This module supplies SCHEDULE facts only (opponent, home/away, roof,
kickoff) -- never weather or odds, which come from `ffbot.live.weather` and
`ffbot.markets.kalshi_nfl` respectively. `ffbot.live.conditions` is the
place all three get assembled into one `GameInfo` per team.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from ..history.fetch import DEFAULT_CACHE_DIR, FetchError, UrlOpener, _default_opener, fetch_rows
from ..history.names import canonical_team

# Roof strings that mean "weather cannot touch this game" -- the same set
# `ffbot.history.index._ROOF_DOME` / `ffbot.history.openmeteo._ROOF_DOME`
# use, kept as a third literal copy rather than a shared import: the three
# modules' roof-handling needs diverge just enough (three-state vs. skip-if,
# see each one's own comment) that a shared constant would invite one of
# them silently changing behavior when another's needs change.
_ROOF_DOME = {"dome", "closed"}


class ScheduleError(RuntimeError):
    """The live NFL schedule could not be fetched."""


@dataclass(frozen=True)
class LiveGame:
    """One team's scheduled game this week -- deliberately narrower than
    `ffbot.week.GameInfo`: only what a SCHEDULE can supply. Weather/odds
    fields live on `GameInfo` itself, filled in by
    `ffbot.live.conditions.merge_conditions` from separate providers.
    """

    opponent: str
    home: bool
    roof: str  # raw games.csv string ("dome"/"outdoors"/"closed"/"open"/""); see is_dome
    kickoff: Optional[datetime]  # naive datetime, nflverse's own gameday+gametime (US/Eastern)
    game_id: str = ""

    @property
    def is_dome(self) -> bool:
        return self.roof.strip().lower() in _ROOF_DOME


def _parse_kickoff(gameday: str, gametime: str) -> Optional[datetime]:
    if not gameday or not gametime:
        return None
    try:
        return datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def this_week_games(
    season: int,
    week: int,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
) -> dict[str, LiveGame]:
    """`{team: LiveGame}` for every game in `(season, week)`, both sides
    keyed to the same game -- mirrors `ffbot.history.index._build_games_and_
    stadiums`'s own home/away symmetry.

    Raises `ScheduleError` on a transport failure; callers (see
    `ffbot.live.conditions`) decide the fallback -- same contract as every
    other live seam in this repo (CLAUDE.md).
    """
    try:
        rows = fetch_rows("games", cache_dir=cache_dir, refresh=True, opener=opener)
    except FetchError as exc:
        raise ScheduleError(str(exc)) from exc

    out: dict[str, LiveGame] = {}
    for row in rows:
        try:
            if int(row.get("season", -1)) != season or int(row.get("week", -1)) != week:
                continue
        except (TypeError, ValueError):
            continue

        home = canonical_team(row.get("home_team"))
        away = canonical_team(row.get("away_team"))
        if not home or not away:
            continue

        roof = str(row.get("roof") or "")
        kickoff = _parse_kickoff(str(row.get("gameday") or ""), str(row.get("gametime") or ""))
        game_id = str(row.get("game_id") or "")

        out[home] = LiveGame(opponent=away, home=True, roof=roof, kickoff=kickoff, game_id=game_id)
        out[away] = LiveGame(opponent=home, home=False, roof=roof, kickoff=kickoff, game_id=game_id)

    return out


def current_week(
    season: int,
    today: Optional[date] = None,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
) -> int:
    """The NFL week currently in progress or next upcoming, for `season`,
    as of `today` (defaults to the real calendar date) -- "the week you'd
    want a report for right now." Defined as the SMALLEST week number
    whose games haven't all finished yet (its latest game date is `>=
    today`). Falls back to week 1 before the season's first game, and the
    final week once the whole season is in the past -- both sensible
    defaults rather than an error, since `scripts/autorun.py` calls this
    with no human present to handle an edge case.

    Raises `ScheduleError` on a transport failure, same contract as
    `this_week_games`.
    """
    resolved_today = today if today is not None else datetime.now().date()
    try:
        rows = fetch_rows("games", cache_dir=cache_dir, refresh=True, opener=opener)
    except FetchError as exc:
        raise ScheduleError(str(exc)) from exc

    by_week: dict[int, list[date]] = defaultdict(list)
    for row in rows:
        try:
            if int(row.get("season", -1)) != season:
                continue
            wk = int(row.get("week"))
        except (TypeError, ValueError):
            continue
        gameday = row.get("gameday")
        if not gameday:
            continue
        try:
            by_week[wk].append(date.fromisoformat(gameday))
        except ValueError:
            continue

    if not by_week:
        return 1

    for wk in sorted(by_week):
        if max(by_week[wk]) >= resolved_today:
            return wk
    return max(by_week)
