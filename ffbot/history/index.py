"""The point-in-time boundary for historical replay.

`as_of(season, week, ...)` is the ONLY function anything outside this
package should call to get a historical week's data — see the design
invariant in `ffbot/history/__init__.py` and CLAUDE.md. It assembles a
`WeekSnapshot` from cached nflverse files and adapts it directly into
`ffbot.week.WeeklyIntel`/`GameInfo`/`StadiumInfo`, so `ffbot/week.py` itself
needs zero changes to be replayed against real history.

What "point in time" does and doesn't cover here (see docs/BACKTEST.md's
leakage register for the full account): `games.csv`'s `temp`/`wind` are the
values nflverse recorded for the game as it happened, not the Thursday
forecast a real manager saw — a legitimate upper bound on weather's value,
not a simulation of forecast uncertainty. `spread_line`/`total_line` are
closing lines, which is pre-kickoff (so not leakage) but slightly sharper
than what a Sunday-morning manager saw. nflverse's `games.csv` carries no
precipitation field at all, so `GameInfo.precip_pct` is always `None` under
historical replay — the weather signal here is wind/temp/roof only, a real
(documented, not hidden) fidelity reduction from a live `weekly/week-NN.yml`
that might have a researched precip forecast.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from ..week import GameInfo, StadiumInfo, WeeklyIntel, WeeklyPlayerIntel
from ..names import normalize_name
from .fetch import DEFAULT_CACHE_DIR, UrlOpener, _default_opener, fetch_rows
from .names import canonical_team

# nflverse's `report_status` free-text values -> the Yahoo-style codes
# `models.STATUS_OUT`/`STATUS_QUESTIONABLE` already understand. Anything not
# in this table (including an empty/cleared report) maps to "" — no status
# override, same as a healthy player. IR/PUP/SUSP are deliberately NOT
# derived from the weekly injury report here: nflverse's `load_injuries`
# tracks the in-season practice/game report only, not roster-move
# designations, so a backtest under-detects those relative to a live Yahoo
# feed — a known, documented fidelity gap, not a silent one.
_INJURY_STATUS_MAP: dict[str, str] = {
    "out": "O",
    "doubtful": "D",
    "questionable": "Q",
}

_ROOF_DOME = {"dome", "closed"}
_ROOF_OPEN = {"outdoors", "open"}


def _num(row: dict, field_name: str) -> Optional[float]:
    val = row.get(field_name)
    if val is None or val == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None


@dataclass(frozen=True)
class WeekSnapshot:
    """Everything replay code may see for one (season, week): the week's
    games (real, in-range weather/Vegas), each team's actual stadium roof
    state that week, and pre-kickoff injury-report status.

    Two different channels, on purpose. `weekly_intel()`/`stadiums` are the
    DECISION view — exactly what `week.build_week_brief`/`adjusted_players`
    consume, and structurally incapable of leaking a result: `as_of()` never
    even fetches `stats_player_week`/`stats_team_week` (the only sources with
    actual outcomes) to build them. `game_rows` is the separate GROUND-TRUTH
    channel — the raw `games.csv` rows for this week, scores included,
    kept only so `ffbot.history.actuals.points_allowed_for` can look a
    result up after a decision has already been made. Decision code must
    never read `game_rows`; only evaluation/scoring code should.
    """

    season: int
    week: int
    games: dict[str, GameInfo] = field(default_factory=dict)  # team -> GameInfo
    stadiums: dict[str, StadiumInfo] = field(default_factory=dict)  # team -> that game's roof
    player_status: dict[str, WeeklyPlayerIntel] = field(default_factory=dict)  # normalized name -> intel
    game_rows: tuple[dict, ...] = field(default_factory=tuple)  # raw games.csv rows this week, for actuals.py

    def weekly_intel(self) -> WeeklyIntel:
        """Adapt into `ffbot.week.WeeklyIntel` — unchanged by anything
        downstream; `build_week_brief`/`adjusted_players`/`rank_streamers`
        take this exact type today."""
        return WeeklyIntel(
            week=self.week,
            generated=f"history:as_of({self.season}, {self.week})",
            players=dict(self.player_status),
            games=dict(self.games),
        )

    def with_signals(self, signals: dict[str, dict[str, float]]) -> "WeekSnapshot":
        """A copy of this snapshot with `signals` (a
        `ffbot.history.signals.SignalProvider`'s output — `{normalized_name:
        {"volatility": 0..100, "upside": 0..100}}`) merged into
        `player_status`.

        Deliberately NOT part of `as_of()` itself — see
        `ffbot.history.signals`'s module docstring for why providers live
        outside the point-in-time boundary rather than widening it. A name
        already present (e.g. carrying an injury `status` from the real
        report) keeps that field and gains `volatility`/`upside`; a name
        with no existing entry gets a new bare `WeeklyPlayerIntel`. Unknown
        signal keys (anything but `"volatility"`/`"upside"`) are ignored
        rather than raising, so a provider returning extra diagnostic
        fields doesn't break the merge.
        """
        merged = dict(self.player_status)
        for name, values in signals.items():
            existing = merged.get(name)
            volatility = values.get("volatility")
            upside = values.get("upside")
            if existing is not None:
                merged[name] = replace(
                    existing,
                    volatility=volatility if volatility is not None else existing.volatility,
                    upside=upside if upside is not None else existing.upside,
                )
            else:
                merged[name] = WeeklyPlayerIntel(name=name, volatility=volatility, upside=upside)
        return replace(self, player_status=merged)


def _implied_totals(spread_line: Optional[float], total_line: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """(home_implied, away_implied) from a closing spread/total, or (None,
    None) if either line is missing. `spread_line` is home-team-perspective
    (positive favors home) per `games.csv`'s own convention."""
    if spread_line is None or total_line is None:
        return None, None
    home = total_line / 2.0 + spread_line / 2.0
    away = total_line / 2.0 - spread_line / 2.0
    return home, away


def _roof_dome(roof: str) -> Optional[bool]:
    r = (roof or "").strip().lower()
    if r in _ROOF_DOME:
        return True
    if r in _ROOF_OPEN:
        return False
    return None  # unknown roof string ("" or anything unrecognized)


def _games_this_week(games_rows: list[dict], season: int, week: int) -> list[dict]:
    out = []
    for row in games_rows:
        try:
            if int(row.get("season", -1)) != season or int(row.get("week", -1)) != week:
                continue
        except (TypeError, ValueError):
            continue
        out.append(row)
    return out


def _build_games_and_stadiums(
    game_rows: list[dict],
) -> tuple[dict[str, GameInfo], dict[str, StadiumInfo]]:
    games: dict[str, GameInfo] = {}
    stadiums: dict[str, StadiumInfo] = {}

    for row in game_rows:
        home = canonical_team(row.get("home_team"))
        away = canonical_team(row.get("away_team"))
        if not home or not away:
            continue

        spread_line = _num(row, "spread_line")
        total_line = _num(row, "total_line")
        home_total, away_total = _implied_totals(spread_line, total_line)

        # `temp` is on the game row too but `week.py` has no consumer for it
        # today (only `wind_mph`/`precip_pct` feed `weather_severity`).
        wind = _num(row, "wind")
        dome = _roof_dome(row.get("roof", ""))
        # The stadium hosting this game is the HOME team's, for both sides —
        # `week.is_dome_game` already resolves via home/opponent, this just
        # supplies the roof state it looks up.
        if dome is not None:
            stadiums[home] = StadiumInfo(dome=dome)

        games[home] = GameInfo(
            opponent=away, home=True, wind_mph=wind, precip_pct=None,
            team_total=home_total, opp_total=away_total,
        )
        games[away] = GameInfo(
            opponent=home, home=False, wind_mph=wind, precip_pct=None,
            team_total=away_total, opp_total=home_total,
        )

    return games, stadiums


def _build_player_status(injury_rows: list[dict], season: int, week: int) -> dict[str, WeeklyPlayerIntel]:
    out: dict[str, WeeklyPlayerIntel] = {}
    for row in injury_rows:
        try:
            if int(row.get("season", -1)) != season or int(row.get("week", -1)) != week:
                continue
        except (TypeError, ValueError):
            continue
        raw_status = (row.get("report_status") or "").strip().lower()
        status = _INJURY_STATUS_MAP.get(raw_status, "")
        if not status:
            continue
        name = row.get("full_name") or row.get("player_name") or ""
        if not name:
            continue
        key = normalize_name(name)
        out[key] = WeeklyPlayerIntel(name=name, status=status)
    return out


def as_of(
    season: int,
    week: int,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    opener: UrlOpener = _default_opener,
) -> WeekSnapshot:
    """Assemble the point-in-time view for `(season, week)`.

    Fetches (cache-first — see `ffbot.history.fetch`) `games` and `injuries`
    for `season`, filters both to `week`, and returns a `WeekSnapshot`. This
    is the single seam `docs/BACKTEST.md` requires all replay code to go
    through — no backtest code should call `fetch`/`fetch_rows` directly for
    weekly game/injury data.
    """
    all_games = fetch_rows("games", cache_dir=cache_dir, opener=opener)
    week_games = _games_this_week(all_games, season, week)
    games, stadiums = _build_games_and_stadiums(week_games)

    try:
        injury_rows = fetch_rows("injuries", season=season, cache_dir=cache_dir, opener=opener)
    except Exception:
        # injuries only goes back to 2009 (`fetch.SOURCES["injuries"].first_season`)
        # and a missing/unreachable file degrades to "no status overrides" —
        # same contract as `week.load_weekly_intel`'s missing-file case.
        injury_rows = []
    player_status = _build_player_status(injury_rows, season, week)

    return WeekSnapshot(
        season=season,
        week=week,
        games=games,
        stadiums=stadiums,
        player_status=player_status,
        game_rows=tuple(week_games),
    )
