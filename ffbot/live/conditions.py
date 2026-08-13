"""Assembles this week's auto-fetched game conditions (schedule + weather +
market odds) into `{team: GameInfo}`, merged UNDER whatever
`weekly/week-NN.yml` already states — a human's `/gameday` research always
wins outright, team by team. This is the unattended-run analog of that
skill's research step: what fills a `games:` block when nobody ran
`/gameday` first.

Each of the three sources (schedule/weather/odds) degrades independently —
one source failing never blocks another, and a wholesale failure here
degrades to "no auto-fetched conditions this run," never a crash. See
CLAUDE.md's live-seam contract: every fetch failure is surfaced as an
alert, never a silent success and never a crash.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from ..config import GameConditionsConfig
from ..week import GameInfo, WeeklyIntel
from . import schedule as schedule_module
from . import weather as weather_module
from .schedule import ScheduleError


def fetch_conditions(
    season: int,
    week: int,
    cfg: GameConditionsConfig,
    cache_dir: Optional[str] = None,
    opener=None,
    kalshi_opener=None,
) -> tuple[dict[str, GameInfo], list[str]]:
    """`({team: GameInfo}, alerts)` — auto-fetched conditions for
    `(season, week)`, gated by `cfg.weather_source`/`cfg.odds_source`.
    Never raises: a source that's off, or that fails, degrades to "no data
    from that source," appended to `alerts` rather than propagated.
    `opener`/`kalshi_opener` are separate injection points because the two
    sources hit different hosts (Open-Meteo vs. Kalshi) and a test faking
    one must never accidentally also stand in for the other.
    """
    alerts: list[str] = []
    schedule_kwargs = {}
    if cache_dir or cfg.cache_dir:
        schedule_kwargs["cache_dir"] = cache_dir or cfg.cache_dir
    if opener is not None:
        schedule_kwargs["opener"] = opener

    try:
        games = schedule_module.this_week_games(season, week, **schedule_kwargs)
    except ScheduleError as exc:
        alerts.append(f"live schedule fetch failed ({exc}) — no auto-fetched conditions this run")
        return {}, alerts
    if not games:
        return {}, alerts

    weather: dict[str, dict[str, float]] = {}
    if cfg.weather_source == "open_meteo":
        try:
            weather_kwargs = dict(schedule_kwargs)
            weather_kwargs["ttl_minutes"] = cfg.cache_ttl_minutes
            weather = weather_module.forecast_weather(games, **weather_kwargs)
        except Exception as exc:  # noqa: BLE001 -- a weather failure must never block odds or the schedule already fetched
            alerts.append(f"live weather fetch failed ({exc}) — no auto-fetched weather this run")

    odds: dict[str, dict[str, float]] = {}
    if cfg.odds_source == "kalshi":
        from ..markets import kalshi_nfl  # local import: keeps ffbot.live importable with no network at module level
        try:
            odds_kwargs = {}
            if kalshi_opener is not None:
                odds_kwargs["opener"] = kalshi_opener
            odds = kalshi_nfl.game_odds(games, series=cfg.kalshi_odds_series, **odds_kwargs)
        except Exception as exc:  # noqa: BLE001 -- see weather note above
            alerts.append(f"live odds fetch failed ({exc}) — no auto-fetched odds this run")

    out: dict[str, GameInfo] = {}
    for team, game in games.items():
        w = weather.get(team, {})
        o = odds.get(team, {})
        out[team] = GameInfo(
            opponent=game.opponent,
            kickoff_et=game.kickoff.strftime("%Y-%m-%dT%H:%M") if game.kickoff else "",
            home=game.home,
            wind_mph=w.get("wind_mph"),
            precip_pct=w.get("precip_pct"),
            team_total=o.get("team_total"),
            opp_total=o.get("opp_total"),
            temp_f=w.get("temp_f"),
            wind_gust_mph=w.get("wind_gust_mph"),
            precip_mm=w.get("precip_mm"),
        )
    return out, alerts


def merge_conditions(intel: WeeklyIntel, auto: dict[str, GameInfo]) -> WeeklyIntel:
    """A copy of `intel` with `auto`'s games merged UNDER `intel.games`.

    Whole-entry precedence, not a per-field merge: `/gameday` always writes
    a complete entry (wind, precip, team_total together, both teams
    mirrored) in one research pass, so a team already present in
    `weekly/week-NN.yml` got that treatment and its entry wins outright: a
    team absent gets the auto-fetched entry verbatim. This is what lets an
    unattended scheduled run still produce real numbers while never
    silently overwriting research that has actually happened.
    """
    merged = dict(auto)
    merged.update(intel.games)  # hand-typed entries win outright, whole-entry
    return replace(intel, games=merged)
